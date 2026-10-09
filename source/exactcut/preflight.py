"""Production connection checks without uploads, table writes or material ingestion."""
import argparse
import fcntl
import hashlib
import math
import json
import os
import time

import oss2

from .hook_emphasis import responses_json
from .publish import CfAdsClient, FeishuClient, FEISHU_FIELD_DEFS, STATUS_FIELD
from .runtime import data_root, environment, project_env_file, protect_path
from .util import load_env_file, now_iso, read_json, write_json_atomic
from .stage_results import ResourceDeferred


REQUIRED = {
    'oss': ('ALIYUN_OSS_ACCESS_KEY_ID', 'ALIYUN_OSS_ACCESS_KEY_SECRET', 'ALIYUN_OSS_ENDPOINT', 'ALIYUN_OSS_BUCKET'),
    'ai': ('OPENAI_API_KEY', 'OPENAI_BASE_URL'),
    'feishu': ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_BITABLE_APP_TOKEN', 'FEISHU_BITABLE_TABLE_ID'),
    'cf_ads': ('CF_ADS_BASE_URL', 'CF_ADS_USERNAME', 'CF_ADS_PASSWORD'),
}


def check_oss(drama_id, sample_mib):
    auth = oss2.Auth(os.environ['ALIYUN_OSS_ACCESS_KEY_ID'], os.environ['ALIYUN_OSS_ACCESS_KEY_SECRET'])
    endpoint = os.environ['ALIYUN_OSS_ENDPOINT']
    target = oss2.Bucket(auth, endpoint, os.environ['ALIYUN_OSS_BUCKET'], connect_timeout=15)
    source = oss2.Bucket(auth, endpoint, os.environ.get('ALIYUN_OSS_SOURCE_BUCKET') or 'aigc-us02', connect_timeout=15)
    # Keys and source bucket follow MaterialHub.target_key / SOURCE_PREFIX.
    keys = [(target, f'assets/{drama_id}/video/EP1.mp4'),
            (source, f'tb/tbl5wK7ykMP9ZNKb/{drama_id}/EP1.mp4')]
    checks = []
    for bucket, key in keys:
        bucket.session.session.trust_env = False
        total = int(bucket.head_object(key).content_length)
        requested = min(total, sample_mib * 1024**2)
        if requested <= 0:
            raise RuntimeError('empty OSS object')
        started = time.monotonic()
        result = bucket.get_object(key, byte_range=(0, requested - 1))
        received = 0
        try:
            while received < requested:
                chunk = result.read(min(1024**2, requested - received))
                if not chunk:
                    break
                received += len(chunk)
        finally:
            result.close()
        elapsed = time.monotonic() - started
        if received != requested:
            raise RuntimeError('incomplete OSS response')
        checks.append({'bucket': bucket.bucket_name, 'object_bytes': total, 'sample_bytes': received,
                       'seconds': round(elapsed, 3), 'mib_per_second': round(received / 1024**2 / elapsed, 3)})
    return {'status': 'ok', 'downloads': checks, 'upload_permission': 'not_verified'}


def check_ai():
    fallback = os.environ.get('EXACTCUT_HELP_MODEL') or 'gpt-5.6-sol'
    intake = os.environ.get('EXACTCUT_INTAKE_MODEL') or fallback
    models = list(dict.fromkeys([os.environ.get('EXACTCUT_HOOK_MODEL') or intake, intake, fallback]))
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
              'required': ['ok'], 'additionalProperties': False}
    prompt, content = 'This is a connection test. Return {"ok":true}.', 'Connection test only.'
    identity = {'models': models, 'prompt': prompt, 'content': content, 'schema': schema,
                'endpoint': os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/'),
                'auth_fingerprint': hashlib.sha256(os.environ.get('OPENAI_API_KEY', '').strip().encode()).hexdigest()}
    directory = data_root() / '.web-state'
    protect_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    progress_path, lock_path = directory / 'ai-preflight.json', directory / 'ai-preflight.lock'
    protect_path(progress_path); protect_path(lock_path)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResourceDeferred(time.time() + 1, 'AI 连接检查正在进行，请稍后继续') from None
        now = time.time()
        checked = {}
        if progress_path.exists():
            try:
                progress = read_json(progress_path)
                if progress.get('identity') == identity:
                    checked = {model: stamp for model, stamp in progress.get('checked', {}).items()
                               if model in models and not isinstance(stamp, bool)
                               and isinstance(stamp, (int, float)) and math.isfinite(stamp)
                               and 0 <= now - stamp <= 300}
            except (ValueError, TypeError, AttributeError, OSError):
                pass
        try:
            for model in models:
                if model in checked:
                    continue
                result = responses_json(prompt, content, schema, 'connection_test', model)
                if result != {'ok': True}:
                    raise RuntimeError('invalid structured Responses output')
                checked[model] = time.time()
                write_json_atomic(progress_path, {'version': 1, 'identity': identity, 'checked': checked})
        except ResourceDeferred:
            raise
        except Exception:
            progress_path.unlink(missing_ok=True)
            raise
        # Progress is only for a deferred diagnostic. A completed invocation never caches success.
        progress_path.unlink(missing_ok=True)
        return {'status': 'ok', 'models': models, 'structured_responses': True,
                'model_checked_at': checked, 'application_scenarios': 'not_verified'}


def check_feishu():
    fields = FeishuClient().fields()
    definitions = [STATUS_FIELD, *FEISHU_FIELD_DEFS]
    missing = [field['field_name'] for field in definitions if field['field_name'] not in fields]
    wrong = [field['field_name'] for field in definitions
             if field['field_name'] in fields and fields[field['field_name']].get('type') != field['type']]
    options = {item.get('name') for item in fields.get('状态', {}).get('property', {}).get('options', [])}
    missing_options = [item['name'] for item in STATUS_FIELD['property']['options'] if item['name'] not in options]
    return {'status': 'failed' if missing or wrong or missing_options else 'ok', 'missing_fields': missing,
            'wrong_types': wrong, 'missing_status_options': missing_options, 'write_permission': 'not_verified'}


def check_cf_ads():
    usage = CfAdsClient().app_usage()
    # is_available is consumed by sync_record_once; do not infer other quota fields.
    return {'status': 'ok' if usage.get('is_available') is True else 'failed',
            'facebook_available': usage.get('is_available'), 'ingestion_and_sync': 'not_verified'}


def run_checks(drama_id, sample_mib):
    if environment() != 'production':
        raise RuntimeError('External preflight requires production environment')
    if not drama_id.isdigit() or len(drama_id) != 6 or not 1 <= sample_mib <= 64:
        raise ValueError('Need a six-digit drama ID and a 1–64 MiB sample')
    probes = {'oss': lambda: check_oss(drama_id, sample_mib), 'ai': check_ai,
              'feishu': check_feishu, 'cf_ads': check_cf_ads}
    report = {'checked_at': now_iso(), 'environment': environment(), 'checks': {}}
    for name, probe in probes.items():
        missing = [key for key in REQUIRED[name] if not os.environ.get(key, '').strip()]
        if missing:
            report['checks'][name] = {'status': 'missing_config', 'missing': missing}
            continue
        started = time.monotonic()
        try:
            result = probe()
        except Exception as exc:
            from .stage_results import ResourceDeferred
            if isinstance(exc, ResourceDeferred): raise
            # Providers can echo passwords or tokens in errors; never print response bodies.
            result = {'status': 'failed', 'error_type': type(exc).__name__}
            status = getattr(exc, 'status', None)
            response = getattr(exc, 'response', None)
            status = status or getattr(response, 'status_code', None)
            if isinstance(status, int):
                result['http_status'] = status
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['checks'][name] = result
    report['status'] = 'ok' if all(item['status'] == 'ok' for item in report['checks'].values()) else 'failed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drama-id', required=True, help='Existing drama used for read-only OSS sampling')
    parser.add_argument('--sample-mib', type=int, default=32)
    args = parser.parse_args()
    if environment() != 'production':
        raise RuntimeError('External preflight requires production environment')
    load_env_file(project_env_file())
    try:
        report = run_checks(args.drama_id, args.sample_mib)
    except ResourceDeferred as exc:
        report = {'status': 'waiting_resource', 'not_before': exc.not_before,
                  'checked_at': now_iso(), 'environment': environment()}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 75
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
