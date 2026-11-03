#!/usr/bin/env python3
"""Portable Docker releases. Code rollback always retains current business data."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import build_opener, ProxyHandler


ACTIVE = {'queued', 'downloading', 'indexing', 'producing', 'publishing', 'running'}
PHASE_TERMINAL = ('completed', 'failed', 'interrupted')


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else ({} if default is None else default)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args, **kwargs):
    return run(*args, capture_output=True, text=True, **kwargs).stdout.strip()


def version_path(root, version):
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', version):
        raise ValueError('无效版本标识')
    return root / 'releases' / version


@contextmanager
def operation_lock(root):
    with (root / 'deployment.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def validate_resources(settings):
    cpus, memory = settings.get('cpus'), settings.get('memory')
    if (isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not math.isfinite(cpus)
            or cpus <= 0 or not isinstance(memory, str) or not re.fullmatch(r'[1-9][0-9]*[mg]', memory)):
        raise ValueError('无效或缺失资源限制')


def initialize(root, profile, *, instance, backend_port, public_port, cpus, memory):
    if not root.is_absolute() or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', instance):
        raise ValueError('部署目录必须绝对路径，实例名仅允许小写字母、数字、下划线和连字符')
    if not 1024 <= backend_port <= 65535 or not 1 <= public_port <= 65535 or backend_port == public_port:
        raise ValueError('前台与后端端口必须不同；后端1024–65535，公网1–65535')
    validate_resources({'cpus': cpus, 'memory': memory})
    runtime = read(profile)
    if runtime.get('environment') != 'production':
        raise RuntimeError('运行配置必须属于production')
    root.mkdir(parents=True, exist_ok=True)
    with operation_lock(root):
        settings = root / 'config/deployment.json'
        if settings.exists():
            raise RuntimeError('部署配置已存在；禁止初始化时覆盖')
        for directory in ('config', 'data', 'bin', 'releases', 'history'):
            (root / directory).mkdir(exist_ok=True)
        owner = read(root / 'data/.web-state/environment.json').get('environment')
        if owner and owner != 'production':
            raise RuntimeError('数据目录属于其他环境')
        target = root / 'config/runtime.json'
        if target.exists():
            if read(target).get('environment') != 'production':
                raise RuntimeError('运行配置属于其他环境')
        else:
            write(target, runtime)
        write(settings, {'instance': instance, 'backend_port': backend_port, 'public_port': public_port,
                         'cpus': cpus, 'memory': memory})
    return read(settings)


class Docker:
    def editor_protocol(self, image):
        item = json.loads(output('docker', 'image', 'inspect', image))[0]
        return (item.get('Config', {}).get('Labels') or {}).get('io.exactcut.editor-protocol') == '1'

    def media_protocol(self, image):
        item = json.loads(output('docker', 'image', 'inspect', image))[0]
        return (item.get('Config', {}).get('Labels') or {}).get('io.exactcut.media-protocol') == '1'

    def stage_protocol(self, image):
        item = json.loads(output('docker', 'image', 'inspect', image))[0]
        return (item.get('Config', {}).get('Labels') or {}).get('io.exactcut.stage-protocol') == '1'

    def inspect_image(self, image):
        item = json.loads(output('docker', 'image', 'inspect', image))[0]
        return item['Id'], (item.get('Config', {}).get('Labels') or {}).get('org.opencontainers.image.revision')

    def occupied(self, settings):
        return bool(output('docker', 'ps', '-a', '--filter',
                           'label=com.docker.compose.project=' + settings['instance'], '--format', '{{.ID}}'))

    def compose(self, root, release, settings, *arguments):
        validate_resources(settings)
        # --env-file prevents Compose's automatic discovery of a neighboring legacy .env.
        env = dict(os.environ, EXACTCUT_DEPLOY_ROOT=str(root), EXACTCUT_INSTANCE=settings['instance'],
                   EXACTCUT_BACKEND_PORT=str(settings['backend_port']), EXACTCUT_CPUS=str(settings['cpus']),
                   EXACTCUT_MEMORY=settings['memory'], EXACTCUT_IMAGE=release['image'],
                   EXACTCUT_RELEASE_ID=release['version'])
        return run('docker', 'compose', '--env-file', '/dev/null', '-f',
                   version_path(root, release['version']) / 'compose.yml', '-p', settings['instance'],
                   *arguments, env=env, capture_output=True, text=True)

    def apply(self, root, release, settings):
        require_media_compatibility(root, release, self)
        self.compose(root, release, settings, 'up', '-d', '--no-build', '--pull', 'never', 'web')

    def stop(self, root, release, settings):
        self.compose(root, release, settings, 'stop', '-t', '60', 'web')

    def remove(self, root, release, settings):
        self.compose(root, release, settings, 'rm', '-f', 'web')

    def verify_delivery(self, root, release, settings, batch_id, request_id, sha256):
        # Use the sealed NEW image without its web/worker entrypoint or ports.
        # Credentials/config/data mounts come from the verified release compose.
        require_media_compatibility(root, release, self)
        self.compose(root, release, settings, 'run', '--rm', '--no-deps', '-T',
                     '--entrypoint', 'python', '-e', 'EXACTCUT_WORKER_ENABLED=0',
                     '-e', 'EXACTCUT_ALLOW_AI=0', '-e', 'EXACTCUT_ALLOW_MATERIAL_FETCH=0',
                     'web', '-m', 'exactcut.maintenance_verify', batch_id, request_id, sha256, '--once')
        return {'status': 'queried'}

    def retire_candidate(self, root, release, settings, parent_id, child_id, job_id, *, apply=False):
        # No production.env/config mounts and no network or service entrypoint.
        require_media_compatibility(root, release, self)
        command = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
                   '--cpus', '1', '--memory', '2g', '--pids-limit', '64',
                   '-v', str(root / 'data') + ':/data', '--entrypoint', 'python',
                   '-e', 'EXACTCUT_DATA_ROOT=/data', '-e', 'EXACTCUT_ENVIRONMENT=production',
                   '-e', 'EXACTCUT_WORKER_ENABLED=0', '-e', 'EXACTCUT_ALLOW_EXTERNAL_WRITES=0',
                   release['image'], '-m', 'exactcut.retire_candidate', parent_id, child_id, job_id]
        if apply: command.append('--apply')
        return json.loads(output(*command))

    def health(self, settings, version, timeout):
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while True:
            try:
                with opener.open(f'http://127.0.0.1:{settings["backend_port"]}/healthz', timeout=3) as reply:
                    value = json.load(reply)
                if (value.get('status') == 'ok' and value.get('environment') == 'production'
                        and value.get('release') == version and value.get('worker_alive') is True):
                    return value
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('版本健康检查失败')
            time.sleep(1)


def verify(root, version, engine):
    folder = version_path(root, version)
    release = read(folder / 'release.json')
    if release.get('version') != version or not re.fullmatch(r'[a-f0-9]{40}', release.get('commit', '')):
        raise RuntimeError('版本元数据校验失败')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', release.get('image', '')):
        raise RuntimeError('必须使用固定镜像ID')
    if release.get('tests', {}).get('exit_code') != 0 or release.get('tests', {}).get('passed', 0) < 1:
        raise RuntimeError('版本缺少通过的测试证据')
    for name, key in [('compose.yml', 'compose_sha256'), ('tests.log', 'tests_sha256')]:
        if sha(folder / name) != release.get(key):
            raise RuntimeError('版本文件校验失败')
    schema = read(root / 'data/.web-state/data-schema.json', {'version': 1}).get('version')
    policy = release.get('data_schema', {})
    if schema != 1 or policy.get('version') != schema or schema not in policy.get('readable', []):
        raise RuntimeError('data schema兼容检查失败；禁止自动迁移或还原旧数据')
    image_id, commit = engine.inspect_image(release['image'])
    if image_id != release['image'] or commit != release['commit']:
        raise RuntimeError('镜像与提交校验失败')
    return release


def active_records(root):
    state = root / 'data/.web-state'
    records = [*(state / 'jobs').glob('*.json'), *(state / 'batches').glob('*/runtime.json')]
    active = [str(path.relative_to(root)) for path in records if read(path).get('status') in ACTIVE]
    # Maintenance pauses queued subtitles. Only a running report (or an occupied
    # durable claim below) must drain before code can switch.
    reports = (state / 'batches').glob('*/preview-subtitles/*/report.json')
    active.extend(str(path.relative_to(root)) for path in reports if read(path).get('status') == 'running')
    subtitle_queue = state / 'queue.json'
    if subtitle_queue.exists():
        try:
            queue = read(subtitle_queue)
            if (not isinstance(queue,dict) or queue.get('schema_version') != 1
                    or not isinstance(queue.get('pending'),list)
                    or (queue.get('active') is not None and not isinstance(queue['active'],dict))
                    or not isinstance(queue.get('active_extra',[]),list)):
                raise ValueError('invalid queue')
            occupied = bool(queue.get('active') or queue.get('active_extra'))
        except (OSError,ValueError,TypeError):
            occupied = True  # unknown active work must block a service stop
        if occupied:
            active.append(str(subtitle_queue.relative_to(root)))
    mosaic_queue = state / 'mosaic-queue.json'
    if mosaic_queue.exists():
        try:
            data=read(mosaic_queue)
            if data.get('schema_version')!=1 or not isinstance(data.get('tasks'),list):raise ValueError('invalid mosaic queue')
            occupied=any(item['state']=='active' for item in data['tasks'])
        except (OSError,ValueError,TypeError,KeyError):occupied=True
        if occupied:active.append(str(mosaic_queue.relative_to(root)))
    return active


def phase_blockers(root):
    """Old executors cannot interpret unfinished stages or uncertain delivery intents."""
    state = root / 'data/.web-state'
    blockers = []
    for path in (state / 'jobs').glob('*.json'):
        try:
            job = read(path)
            if not isinstance(job, dict): raise ValueError('job must be an object')
            if 'phase' not in job: continue
            blocked = job.get('status') not in PHASE_TERMINAL
            if not blocked and job.get('batch_id') and job.get('request_id'):
                publications = read(state / 'batches' / job['batch_id'] / 'publications.json')
                entry = publications.get(job['request_id'], {})
                blocked = entry.get('status') == 'unverified' or entry.get('intent', {}).get('status') == 'pending'
            if blocked: blockers.append(str(path.relative_to(root)))
        except (OSError, ValueError, TypeError, AttributeError):
            blockers.append(str(path.relative_to(root)))
    return blockers


def supports_stages(engine, release):
    inspect = getattr(engine, 'stage_protocol', None)
    return inspect(release['image']) is True if inspect else False


def require_stage_compatibility(root, release, engine):
    if not supports_stages(engine, release) and phase_blockers(root):
        raise RuntimeError('目标镜像不支持阶段任务协议；未完成阶段或待核实交付阻止切换')


def media_state_records(root):
    """Presence, including incomplete/corrupt enrollment, forbids legacy writers."""
    state = root / 'data/.web-state'
    registry = state / 'media-registry'
    records = [*registry.glob('*.json'), *(state / 'batches').glob('*/media-state.json')]
    if registry.is_symlink():
        records.append(registry)
    return sorted(str(path.relative_to(root)) for path in records)


def require_editor_compatibility(root, release, engine):
    """Old writers silently discard frame/effect fields despite shared schema 1."""
    batches = root / 'data/.web-state/batches'
    paths = [p for name in ('draft.json', 'confirmed.json', 'media-state.json') for p in batches.glob('*/' + name)]
    paths.extend((root / 'data/projects').glob('*/requests/*/normalized.json'))
    if not paths:
        return
    inspect = getattr(engine, 'editor_protocol', None)
    if inspect and inspect(release['image']) is True:
        return
    def requires_editor(value):
        if isinstance(value, dict):
            if (value.get('editor_time_format') == 'source_frames_v1'
                    or value.get('time_format') == 'source_frames_v1'
                    or value.get('segment_caption') is not None
                    or value.get('transition_in', 'none') != 'none'):
                return True
            return any(requires_editor(v) for v in value.values())
        return isinstance(value, list) and any(requires_editor(v) for v in value)
    for path in paths:
        try:
            if path.is_symlink() or requires_editor(read(path)):
                raise RuntimeError('已有帧时间或片段效果数据；目标镜像不支持编辑协议，禁止回退')
        except (OSError, ValueError, RecursionError) as exc:
            raise RuntimeError('无法核实编辑数据兼容性，禁止旧镜像写入') from exc


def require_media_compatibility(root, release, engine):
    records = media_state_records(root)
    inspect = getattr(engine, 'media_protocol', None)
    if records and (inspect is None or inspect(release['image']) is not True):
        raise RuntimeError('已有素材版本数据；目标镜像不支持素材版本协议，禁止旧程序写入或回退')
    require_editor_compatibility(root, release, engine)


def require_phase_drained(root):
    if phase_blockers(root):
        raise RuntimeError('阶段任务或待核实交付未排空，禁止发布切换')


def check_switch(root, version, *, engine=None):
    """Read-only code guard and drain diagnostics; does not acquire maintenance."""
    engine = engine or Docker()
    result = {'target': version, 'target_stage_protocol': False, 'phase_blockers': [],
              'active_tasks': [], 'allowed': False}
    try:
        target = verify(root, version, engine)
        validate_resources(read(root / 'config/deployment.json'))
        result['media_state_records'] = media_state_records(root)
        require_media_compatibility(root, target, engine)
        result['target_stage_protocol'] = supports_stages(engine, target)
        result['phase_blockers'] = phase_blockers(root)
        result['active_tasks'] = active_records(root)
        result['allowed'] = not result['active_tasks'] and not result['phase_blockers']
    except subprocess.CalledProcessError as exc:
        # Provider output can contain credentials; diagnostics expose only the type.
        result['guard_error'] = type(exc).__name__
    except (RuntimeError, ValueError, OSError, TypeError, AttributeError) as exc:
        result['guard_error'] = str(exc)
    return result


def await_idle(root, timeout):
    deadline = time.monotonic() + timeout
    while active_records(root):
        if time.monotonic() >= deadline:
            raise RuntimeError('现有任务未结束，停止发布；不会强制终止任务')
        time.sleep(.25)


@contextmanager
def write_barrier(root, timeout):
    state = root / 'data/.web-state'
    state.mkdir(parents=True, exist_ok=True)
    gate = state / 'maintenance.json'
    if gate.exists():
        raise RuntimeError('维护标记已存在，请检查状态并执行recover')
    write(gate, {'reason': 'release', 'at': datetime.now(timezone.utc).isoformat()})
    keep = {'maintenance': False}
    try:
        with (state / 'mutation.lock').open('a') as stream:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('进行中的请求未结束，停止发布')
                    time.sleep(.1)
            yield keep
    finally:
        if not keep['maintenance']:
            gate.unlink(missing_ok=True)


def transition(root, version, *, engine=None, timeout=300, health_timeout=60):
    engine = engine or Docker()
    with operation_lock(root):
        settings = read(root / 'config/deployment.json')
        validate_resources(settings)
        target = verify(root, version, engine)
        require_stage_compatibility(root, target, engine)
        require_media_compatibility(root, target, engine)
        require_phase_drained(root)
        current = read(root / 'current.json')
        if current.get('version') == version:
            raise RuntimeError('已经运行此版本')
        old = verify(root, current['version'], engine) if current.get('version') else None
        if not old and engine.occupied(settings):
            raise RuntimeError('同名实例已有未登记容器，禁止接管')
        owner = read(root / 'data/.web-state/environment.json').get('environment')
        if owner and owner != 'production':
            raise RuntimeError('数据目录属于其他环境')
        if read(root / 'config/runtime.json').get('environment') != 'production':
            raise RuntimeError('运行配置属于其他环境')
        config = root / 'config/production.env'
        if not config.is_file() or (config.stat().st_mode & 0o077):
            raise RuntimeError('生产密钥配置必须存在且权限为0600')
        with write_barrier(root, timeout) as gate:
            require_stage_compatibility(root, target, engine)
            require_media_compatibility(root, target, engine)
            require_phase_drained(root)
            await_idle(root, timeout)
            require_stage_compatibility(root, target, engine)
            require_media_compatibility(root, target, engine)
            require_phase_drained(root)
            attempted = False
            receipt = {'at': datetime.now(timezone.utc).isoformat(), 'from': current.get('version'), 'to': version}
            history = root / 'history' / f'{time.time_ns()}-{version}.json'
            try:
                attempted = True
                if old:
                    engine.stop(root, old, settings)
                    await_idle(root, 0)
                require_stage_compatibility(root, target, engine)
                require_media_compatibility(root, target, engine)
                require_phase_drained(root)
                engine.apply(root, target, settings)
                check = engine.health(settings, version, health_timeout)
                write(root / 'data/.web-state/data-schema.json', {'version': 1})
                write(history, {**receipt, 'status': 'ok', 'health': check})
                write(root / 'current.json', {'version': version, 'previous': current.get('version'),
                                            'commit': target['commit'], 'image': target['image']})
                return {**receipt, 'status': 'ok', 'health': check}
            except Exception as exc:
                recovery_error = None
                if attempted:
                    try:
                        engine.stop(root, target, settings)
                        if old:
                            # Emergency restore is ONLY the exact verified release
                            # just stopped, never a new downgrade target. Do not
                            # strand both services if uncertainty appeared meanwhile.
                            if phase_blockers(root): gate['maintenance'] = True
                            require_media_compatibility(root, old, engine)
                            engine.apply(root, old, settings)
                            engine.health(settings, old['version'], health_timeout)
                        else:
                            engine.remove(root, target, settings)
                    except Exception as restore:
                        recovery_error = type(restore).__name__
                        gate['maintenance'] = True
                write(root / 'current.json', current)
                write(history, {**receipt, 'status': 'failed', 'error_type': type(exc).__name__,
                                'restored': old['version'] if old and not recovery_error else None,
                                'recovery_error_type': recovery_error, 'maintenance_retained': gate['maintenance']})
                if recovery_error:
                    raise RuntimeError('版本切换失败且恢复失败；维护标记保留，请核对当前数据协议并恢复兼容版本') from exc
                if gate['maintenance']:
                    raise RuntimeError('版本切换失败，已恢复旧容器；维护标记保留。显式核实阶段交付并排空后执行 '
                                       f'{root}/bin/linux-ops --root {root} recover') from exc
                raise RuntimeError('版本切换失败，已恢复上一状态；业务数据未还原') from exc


@contextmanager
def maintenance_request_lock(root):
    state = root / 'data/.web-state'
    if not (state / 'maintenance.json').exists():
        raise RuntimeError('必须先处于维护态，禁止在线核实')
    with (state / 'mutation.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('进行中的请求未结束，停止维护操作') from exc
        yield


def retire_candidate_in_maintenance(root, version, parent_id, child_id, job_id, *, engine=None):
    """Recover one stale never-launched candidate with the old worker stopped."""
    if not all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value)
               for value in (parent_id, child_id, job_id)):
        raise ValueError('无效恢复身份')
    engine = engine or Docker()
    with operation_lock(root):
        settings = read(root / 'config/deployment.json'); validate_resources(settings)
        target = verify(root, version, engine)
        current = verify(root, read(root / 'current.json').get('version'), engine)
        require_stage_compatibility(root, target, engine)
        require_media_compatibility(root, target, engine)
        gate_file = root / 'data/.web-state/maintenance.json'
        allowed = {f'data/.web-state/jobs/{job_id}.json',
                   f'data/.web-state/batches/{child_id}/runtime.json'}
        def exclusive():
            if set(active_records(root)) - allowed or set(phase_blockers(root)) - {f'data/.web-state/jobs/{job_id}.json'}:
                raise RuntimeError('仍有其他任务或交付阻断，禁止恢复')
            for file in (root / 'data/.web-state/jobs').glob('*.json'):
                job = read(file)
                if file.name == job_id + '.json':
                    if job.get('status') not in {'queued', 'interrupted'}:
                        raise RuntimeError('目标任务已不再是未启动排队任务')
                elif job.get('status') not in {'completed', 'failed', 'interrupted'}:
                    raise RuntimeError('仍有其他未结束任务，禁止恢复')
        exclusive()
        engine.retire_candidate(root, target, settings, parent_id, child_id, job_id)
        stopped = False
        receipt = {'action': 'retire-candidate', 'at': datetime.now(timezone.utc).isoformat(),
                   'executor_release': version, 'restored': current['version'], 'job_id': job_id}
        try:
            with write_barrier(root, 30) as gate:
                exclusive()
                # Mark before stop: even a partially failed stop must restore.
                stopped = True; gate['maintenance'] = True
                engine.stop(root, current, settings)
            # The child acquires mutation.lock itself; do not keep the host lock.
            exclusive()
            result = engine.retire_candidate(root, target, settings, parent_id, child_id, job_id, apply=True)
            require_phase_drained(root)
            await_idle(root, 0)
            receipt['status'] = 'retired'
        except Exception:
            receipt['status'] = 'failed'
            raise
        finally:
            if stopped:
                try:
                    engine.apply(root, current, settings)
                    engine.health(settings, current['version'], 60)
                except Exception as exc:
                    receipt['status'] = 'restore_failed'
                    raise RuntimeError('恢复原服务失败；维护标记保留，请检查后恢复兼容版本') from exc
                finally:
                    write(root / 'history' / f'{time.time_ns()}-retire-candidate.json', receipt)
        require_phase_drained(root)
        await_idle(root, 0)
        gate_file.unlink(missing_ok=True)
        return result


def verify_delivery_in_maintenance(root, version, batch_id, request_id, sha256, *, engine=None):
    """Drain saved delivery receipts even if the restored image has no new API."""
    engine = engine or Docker()
    if (not re.fullmatch(r'[a-f0-9]{32}', batch_id) or not re.fullmatch(r'[a-f0-9]{64}', sha256)
            or not request_id or request_id.startswith('-')):
        raise ValueError('无效核实批次、素材序号或校验值')
    with operation_lock(root):
        gate = root / 'data/.web-state/maintenance.json'
        if not gate.exists(): raise RuntimeError('必须先处于维护态，禁止在线核实')
        settings = read(root / 'config/deployment.json')
        validate_resources(settings)
        target = verify(root, version, engine)
        if not supports_stages(engine, target): raise RuntimeError('核实镜像必须支持阶段任务协议')
        require_media_compatibility(root, target, engine)
        current = verify(root, read(root / 'current.json').get('version'), engine)
        await_idle(root, 0)
        # No HTTP API is needed. Stop the exact current release before mounting
        # its durable data in the one-off verified image; restore it on ALL exits.
        stopped = False
        try:
            # Drain in-flight HTTP writes before stopping web, then release the
            # barrier: the verifier child takes mutation.lock itself.
            with maintenance_request_lock(root):
                await_idle(root, 0)
                stopped = True
                engine.stop(root, current, settings)
                await_idle(root, 0)
                require_media_compatibility(root, target, engine)
            result = engine.verify_delivery(root, target, settings, batch_id, request_id, sha256)
        finally:
            if stopped:
                require_media_compatibility(root, current, engine)
                engine.apply(root, current, settings)
                engine.health(settings, current['version'], 60)
        result = {**result, 'maintenance': True, 'remaining_phase_blockers': phase_blockers(root)}
        write(root / 'history' / f'{time.time_ns()}-verify-delivery.json',
              {'at': datetime.now(timezone.utc).isoformat(), 'action': 'verify-delivery',
               'executor_release': version, 'restored': current['version'],
               'batch_id': batch_id, 'request_id': request_id, **result})
        return result


def recover(root, *, version=None, engine=None, health_timeout=60):
    """Recover retained maintenance using a sealed data-compatible image."""
    engine = engine or Docker()
    with operation_lock(root):
        current = read(root / 'current.json')
        gate = root / 'data/.web-state/maintenance.json'
        if version is not None and not gate.exists():
            raise RuntimeError('指定恢复版本必须处于维护态；正常切换请使用publish')
        target = verify(root, version or current.get('version'), engine)
        settings = read(root / 'config/deployment.json')
        validate_resources(settings)
        require_stage_compatibility(root, target, engine)
        require_media_compatibility(root, target, engine)
        if version is not None:
            await_idle(root, 0)
        state = root / 'data/.web-state'
        with (state / 'mutation.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('进行中的请求未结束，停止恢复') from exc
            require_media_compatibility(root, target, engine)
            if version is not None:
                await_idle(root, 0)
            try:
                if version is not None and current.get('version') != target['version']:
                    old = verify(root, current['version'], engine) if current.get('version') else None
                    if old:
                        engine.stop(root, old, settings)
                engine.apply(root, target, settings)
                result = engine.health(settings, target['version'], health_timeout)
            except Exception:
                if version is not None:
                    # No attempt to start the data-incompatible previous image.
                    try:
                        engine.stop(root, target, settings)
                    except Exception:
                        pass
                raise
            if version is not None:
                write(root / 'current.json', {'version': target['version'],
                      'previous': current.get('version'), 'commit': target['commit'], 'image': target['image']})
                write(root / 'history' / f'{time.time_ns()}-recover.json',
                      {'action': 'recover', 'from': current.get('version'), 'to': target['version'],
                       'status': 'ok', 'health': result, 'at': datetime.now(timezone.utc).isoformat()})
            gate.unlink(missing_ok=True)
            return result


def build(root, repo, version):
    """Archive clean committed source, build once, run isolated Linux tests, seal metadata."""
    with operation_lock(root):
        if output('git', '-C', repo, 'status', '--porcelain'):
            raise RuntimeError('只允许从已提交且干净的仓库构建版本')
        commit = output('git', '-C', repo, 'rev-parse', 'HEAD')
        folder = version_path(root, version)
        if folder.exists():
            raise RuntimeError('版本目录已存在，禁止覆盖')
        folder.mkdir(parents=True)
        source = folder / 'source'
        source.mkdir()
        with tempfile.TemporaryFile() as archive:
            run('git', '-C', repo, 'archive', '--format=tar', 'HEAD', stdout=archive)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as stream:
                for member in stream:
                    relative = Path(member.name)
                    if relative.is_absolute() or '..' in relative.parts or not (member.isdir() or member.isfile()):
                        raise RuntimeError('工程归档含不安全路径或链接')
                    if relative.name == '.env' or (relative.name.startswith('.env.') and not relative.name.endswith('.example')):
                        raise RuntimeError('工程归档含真实环境配置')
                    business_directories = {'.web-state', '.dev-state', 'sources', 'material-local',
                                            'projects', 'feedback', 'outputs', 'output', 'logs',
                                            'backups', '.work', '.venv'}
                    project_readme = relative == Path('source/projects/README.md')
                    private_file = relative.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.mp4', '.mov', '.mkv'}
                    if (private_file or (member.isfile() and not project_readme
                            and any(part in business_directories for part in relative.parts))):
                        raise RuntimeError('工程归档含业务数据或本地环境')
                    destination = source / relative
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with stream.extractfile(member) as incoming, destination.open('wb') as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
                        destination.chmod(member.mode & 0o777)
        policy = read(source / 'source/deploy/data-schema.json')
        if policy != {'version': 1, 'readable': [1]}:
            raise RuntimeError('此阶段只允许data schema 1')
        image = 'exactcut-web:' + version.lower()
        with (folder / 'build.log').open('w') as log:
            run('docker', 'build', '--build-arg', 'EXACTCUT_COMMIT=' + commit, '-t', image,
                '-f', source / 'source/Dockerfile', source, stdout=log, stderr=subprocess.STDOUT)
        image_id, revision = Docker().inspect_image(image)
        if revision != commit:
            raise RuntimeError('镜像提交标签不匹配')
        if not Docker().stage_protocol(image_id):
            raise RuntimeError('镜像缺少阶段任务协议标签')
        test_image = 'exactcut-validation:' + version.lower()
        # BuildKit treats a bare sha256 ID in FROM as a Docker Hub repository.
        # Use the local tag only for the validation build, then recheck its ID.
        # The runtime image carries only requirements.lock.txt; test tools (pytest, xdist)
        # are added to this throwaway validation image from requirements-dev.lock.txt.
        dockerfile = (f'FROM {image}\nRUN apt-get update && apt-get install -y --no-install-recommends git nodejs '
                      '&& rm -rf /var/lib/apt/lists/*\n'
                      'COPY source/requirements.lock.txt source/requirements-dev.lock.txt /tmp/exactcut-deps/\n'
                      'RUN pip install --no-cache-dir -r /tmp/exactcut-deps/requirements-dev.lock.txt\n')
        with (folder / 'test-image.log').open('w') as log:
            run('docker', 'build', '-t', test_image, '-f', '-', source, input=dockerfile, text=True,
                stdout=log, stderr=subprocess.STDOUT)
        if Docker().inspect_image(image) != (image_id, commit):
            raise RuntimeError('测试镜像构建期间运行镜像被替换')
        command = ['docker', 'run', '--rm', '--cpus=1', '--memory=2g', '--pids-limit=256',
                   '--entrypoint', 'python', '-v', str(source) + ':/acceptance:ro', '-w', '/acceptance/source',
                   '-e', 'PYTHONPATH=/acceptance/source', '-e', 'EXACTCUT_DATA_ROOT=/tmp/exactcut-tests',
                   '-e', 'EXACTCUT_ENVIRONMENT=development', '-e', 'EXACTCUT_WORKER_ENABLED=1',
                   '-e', 'EXACTCUT_ALLOW_AI=0', '-e', 'EXACTCUT_ALLOW_PUBLISH=0',
                   '-e', 'EXACTCUT_ALLOW_MATERIAL_FETCH=0', '-e', 'EXACTCUT_ALLOW_EXTERNAL_WRITES=0',
                   test_image, '-m', 'pytest', '-o', 'addopts=', '-q', '--tb=short', '-p', 'no:cacheprovider']
        with (folder / 'tests.log').open('w') as log:
            run(*command, stdout=log, stderr=subprocess.STDOUT)
        summary = (folder / 'tests.log').read_text()
        passed = re.search(r'(\d+) passed', summary)
        if not passed:
            raise RuntimeError('缺少测试通过结果')
        shutil.copyfile(source / 'source/docker-compose.production.yml', folder / 'compose.yml')
        record = {'version': version, 'commit': commit, 'image': image_id, 'data_schema': policy,
                  'tests': {'exit_code': 0, 'passed': int(passed[1])},
                  'compose_sha256': sha(folder / 'compose.yml'), 'tests_sha256': sha(folder / 'tests.log')}
        write(folder / 'release.json', record)
        verify(root, version, Docker())
        return record


def nginx_config(settings, base_path=''):
    if base_path:
        if not re.fullmatch(r'(?:/[a-zA-Z0-9_-]+)+', base_path):
            raise ValueError('无效web.base_path')
        return '''location = %(base_path)s {
    return 301 %(base_path)s/;
}
location %(base_path)s/ {
    client_max_body_size 15m;
    proxy_pass http://127.0.0.1:%(backend_port)s;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 180s;
    proxy_buffering off;
}
''' % {**settings, 'base_path': base_path}
    return '''server {
    listen %(public_port)s;
    server_name _;
    client_max_body_size 15m;
    location / {
        proxy_pass http://127.0.0.1:%(backend_port)s;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
        proxy_buffering off;
    }
}
''' % settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/srv/exactcut'))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--runtime-config', type=Path, required=True)
    init.add_argument('--instance', default='exactcut-production')
    init.add_argument('--backend-port', type=int, default=18792)
    init.add_argument('--public-port', type=int, default=80)
    init.add_argument('--cpus', type=float, default=1)
    init.add_argument('--memory', default='2g')
    build_command = sub.add_parser('build')
    build_command.add_argument('version')
    build_command.add_argument('--repo', type=Path, required=True)
    for command in ('publish', 'rollback'):
        item = sub.add_parser(command)
        item.add_argument('version', nargs='?' if command == 'rollback' else None)
        item.add_argument('--drain-timeout', type=float, default=300)
        item.add_argument('--health-timeout', type=float, default=60)
    sub.add_parser('status')
    sub.add_parser('check-switch').add_argument('version')
    sub.add_parser('nginx')
    sub.add_parser('recover').add_argument('--version', help='维护态下恢复到指定的兼容已封存版本')
    check = sub.add_parser('verify-delivery')
    for field in ('version', 'batch_id', 'request_id', 'sha256'): check.add_argument(field)
    retire = sub.add_parser('retire-candidate')
    for field in ('version', 'parent_id', 'child_id', 'job_id'): retire.add_argument(field)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.command in {'build', 'publish', 'rollback', 'recover', 'verify-delivery', 'retire-candidate'} and sys.platform != 'linux':
        raise RuntimeError('此运维操作只适用于Linux Docker，不操作Mac历史服务')
    if args.command == 'init':
        result = initialize(root, args.runtime_config, instance=args.instance, backend_port=args.backend_port,
                            public_port=args.public_port, cpus=args.cpus, memory=args.memory)
    elif args.command == 'build':
        result = build(root, args.repo.resolve(), args.version)
    elif args.command == 'check-switch':
        result = check_switch(root, args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['allowed']: raise SystemExit(1)
        return
    elif args.command in {'publish', 'rollback'}:
        version = args.version or read(root / 'current.json').get('previous')
        if not version:
            raise RuntimeError('没有上一版本可回滚')
        result = transition(root, version, timeout=args.drain_timeout, health_timeout=args.health_timeout)
    elif args.command == 'verify-delivery':
        result = verify_delivery_in_maintenance(root, args.version, args.batch_id, args.request_id, args.sha256)
    elif args.command == 'retire-candidate':
        result = retire_candidate_in_maintenance(root, args.version, args.parent_id, args.child_id, args.job_id)
    elif args.command == 'recover':
        result = recover(root, version=args.version)
    elif args.command == 'nginx':
        base_path = read(root / 'config/runtime.json').get('web', {}).get('base_path', '')
        print(nginx_config(read(root / 'config/deployment.json'), base_path), end='')
        return
    else:
        result = {'current': read(root / 'current.json'), 'settings': read(root / 'config/deployment.json'),
                  'active_tasks': active_records(root),
                  'phase_blockers': phase_blockers(root),
                  'maintenance': (root / 'data/.web-state/maintenance.json').exists()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Compose/provider errors may echo env values; only our own messages are safe.
        message = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
        print(message, file=sys.stderr)
        raise SystemExit(1)
