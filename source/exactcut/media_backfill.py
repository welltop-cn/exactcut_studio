"""Sequential, resumable OSS copies of existing media. No rendering or deletion."""
import argparse
import json
import re
from . import preview_oss, result_store
from .models import BatchManifest
from .util import read_json, load_env_file
from .runtime import project_env_file


def backfill(*, apply=False, batch_id=None, progress=None):
    from . import workflow
    if apply and not preview_oss.enabled():
        raise RuntimeError('生产 OSS 媒体存储未启用')
    report = []
    def emit(row):
        report.append(row)
        if progress: progress(row)
    for path in sorted(workflow.BATCHES.glob('*')):
        if not re.fullmatch(r'[0-9a-f]{32}', path.name) or path.is_symlink() or (batch_id and path.name != batch_id):
            continue
        from . import material_versions
        if material_versions.internal(path): continue
        if material_versions.managed(path):
            emit({'batch_id': path.name, 'status': 'managed'})
            continue
        if not (path / 'confirmed.json').is_file():
            continue
        manifest = BatchManifest.model_validate(read_json(path / 'confirmed.json'))
        for request in manifest.requests:
            record = result_store.read(path / 'production')['requests'].get(request.request_id, {})
            if record.get('status') not in workflow.SAFE or record.get('executor_running'):
                continue
            for original in (False, True):
                row = {'batch_id': path.name, 'request_id': request.request_id, 'kind': 'original' if original else 'preview'}
                try:
                    video, record = workflow.preview_file(path.name, request.request_id, lightweight=not original)
                    if not original and video.parent.name != 'previews':
                        emit({**row, 'status': 'missing_preview'})
                        continue
                    row['bytes'] = video.stat().st_size
                    if not apply:
                        emit({**row, 'status': 'planned'})
                        continue
                    receipt = record.get('original_oss') if original else (record.get('preview') or {}).get('oss')
                    verified = preview_oss._ensure_uploaded(video, path.name, request.request_id,
                                                          bucket=preview_oss._connect(), receipt=receipt)
                    preview_oss.save_media_receipt(path / 'production', request.request_id, record, video, verified, original=original)
                    emit({**row, 'status': 'ready'})
                except Exception as exc:
                    emit({**row, 'status': 'failed', 'error_type': type(exc).__name__})
    return report


def main():
    parser = argparse.ArgumentParser(description='补传历史预览与正式成片到私有 OSS；默认仅统计，不删除或重新制作')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--batch')
    args = parser.parse_args()
    config = project_env_file()
    if config.exists(): load_env_file(config)
    report = backfill(apply=args.apply, batch_id=args.batch, progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
    return 2 if any(row['status'] == 'failed' for row in report) else 0


if __name__ == '__main__':
    raise SystemExit(main())
