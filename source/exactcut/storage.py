"""Bound rebuildable base-cut caches; originals, outputs and approved Hooks are excluded."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from functools import partial
from .runtime import protect_path, data_root
from .util import load_config, read_json, now_iso

GIB=1024**3
CACHE_LIMIT=20*GIB
CACHE_DAYS=7
RESERVE=5*GIB
KEY=re.compile(r'[a-f0-9]{64}')
USAGE_TTL = 45
USAGE_SCAN_SECONDS = 10
_usage_lock = threading.Lock()
_usage_cache = {}


class UsageUnavailable(RuntimeError):
    pass


def usage_report(root):
    """One read-only scan per 45 seconds, outside the workflow's mutation locks.

    A concurrent cold/expired request fails promptly instead of queuing another
    full scan. Failed scans also cool down; never publish partial byte totals.
    """
    root = Path(root).resolve()
    protect_path(root)
    if not _usage_lock.acquire(blocking=False):
        raise UsageUnavailable('用量正在统计，请稍后重试')
    try:
        cached = _usage_cache.get(root)
        if cached and time.monotonic() - cached[0] < USAGE_TTL:
            if isinstance(cached[1], str): raise UsageUnavailable(cached[1])
            return cached[1]
        try:
            result = _scan_usage(root)
        except (OSError, ValueError, KeyError, TypeError, UsageUnavailable) as exc:
            message = str(exc) if isinstance(exc, UsageUnavailable) else '用量统计失败，请稍后重试'
            _usage_cache[root] = (time.monotonic(), message)
            raise UsageUnavailable(message) from exc
        # Only one data root is served in this process; do not retain old roots.
        _usage_cache.clear()
        _usage_cache[root] = (time.monotonic(), result)
        return result
    finally:
        _usage_lock.release()


def _scan_usage(root):
    from . import accounts

    deadline = time.monotonic() + USAGE_SCAN_SECONDS
    total, batches, shared = 0, {}, {}

    def check_time():
        if time.monotonic() >= deadline:
            raise UsageUnavailable('用量统计超时，请稍后重试')

    def walk_error(error):
        # Deletion during a live scan is normal; unreadable trees are not zero.
        if not isinstance(error, FileNotFoundError): raise error

    def drama_entry(drama_id):
        return shared.setdefault(drama_id, {'drama_id': drama_id, 'sources_bytes': 0,
                                 'cache_bytes': 0, 'episodes_bytes': 0, 'referenced_by': set()})

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        check_time()
        path = Path(directory)
        dirs[:] = [name for name in dirs if not (path / name).is_symlink()]
        parts = path.relative_to(root).parts
        batch_parts = parts[1:] if parts[:1] == ('.web-state',) else parts
        batch_path = None
        if len(batch_parts) >= 2 and batch_parts[0] == 'batches':
            prefix = root / '.web-state' if parts[:1] == ('.web-state',) else root
            batch_path = prefix / 'batches' / batch_parts[1]
            batches.setdefault(batch_path, 0)
        field = None
        if len(parts) >= 2 and parts[0] == 'sources':
            field = 'sources_bytes'
        elif len(parts) >= 3 and parts[0] == 'projects':
            if parts[2] == 'cache': field = 'cache_bytes'
            elif parts[2:4] == ('data', 'episodes'): field = 'episodes_bytes'
            elif parts[2:4] == ('source', 'videos'): field = 'sources_bytes'
        entry = drama_entry(parts[1]) if field else None
        for name in files:
            check_time()
            try:
                info = (path / name).lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode): continue
            total += info.st_size
            if batch_path is not None: batches[batch_path] += info.st_size
            if entry is not None: entry[field] += info.st_size

    projects, users = [], {}
    for path, size in batches.items():
        check_time()
        if not path.exists(): continue
        # Do not follow metadata links even though media links were excluded.
        for name in ('draft.json', 'runtime.json', 'access.json'):
            if (path / name).is_symlink(): raise ValueError('用量元数据不能为符号链接')
        item = read_json(path / 'draft.json') if (path / 'draft.json').exists() else {}
        runtime = read_json(path / 'runtime.json') if (path / 'runtime.json').exists() else {}
        if not isinstance(item, dict) or not isinstance(runtime, dict):
            raise ValueError('用量元数据必须为对象')
        draft = item.get('draft', {})
        if not isinstance(draft, dict): raise ValueError('项目草稿必须为对象')
        owner = accounts.owner(path)
        drama = draft.get('drama_id', '')
        projects.append({'batch_id': path.name,
                         'name': item.get('display_name') or draft.get('drama_title') or item.get('filename') or path.name,
                         'owner': owner, 'drama_id': drama, 'bytes': size,
                         'assets': len(draft.get('rows', [])), 'status': runtime.get('status', 'draft')})
        user = users.setdefault(owner, {'username': owner, 'projects': 0, 'bytes': 0})
        user['projects'] += 1
        user['bytes'] += size
        if drama: drama_entry(drama)['referenced_by'].add(owner)
    for entry in shared.values(): entry['referenced_by'] = sorted(entry['referenced_by'])
    disk = shutil.disk_usage(root)
    check_time()
    return {'generated_at': now_iso(),
            'totals': {'bytes': total, 'free_bytes': disk.free, 'total_bytes': disk.total},
            'projects': sorted(projects, key=lambda p: (-p['bytes'], p['batch_id'])),
            'users': sorted(users.values(), key=lambda u: (-u['bytes'], u['username'])),
            'shared': sorted(shared.values(), key=lambda s: (-(s['sources_bytes'] + s['cache_bytes'] + s['episodes_bytes']), s['drama_id']))}


def policy():
    values=load_config()['storage']
    for key in ('reserve_gib','base_cache_gib','base_cache_days','source_cache_gib',
                'source_evict_below_gib','episode_budget_gib'):
        value=values.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError(f'storage.{key} 必须是有限的正数')
    if values['source_evict_below_gib']<values['reserve_gib']:
        raise ValueError('storage.source_evict_below_gib 不能小于 reserve_gib')
    if values['episode_budget_gib']>values['source_cache_gib']:
        raise ValueError('storage.episode_budget_gib 不能大于 source_cache_gib')
    return {**{key.replace('_gib','_bytes'):int(value*GIB) for key,value in values.items() if key.endswith('_gib')},
            'base_cache_days':values['base_cache_days']}


def safe_directory(path,root):
    if not path.is_relative_to(root):return False
    return not any(p.is_symlink() for p in [path,*path.parents] if p==root or p.is_relative_to(root))


def cache_entries(root):
    protect_path(root)
    entries=[]
    for project in (root/'projects').glob('[0-9]'*6):
        cache=project/'cache/render-base'
        if not safe_directory(cache,root) or not cache.is_dir():continue
        for p in cache.glob('*.mp4'):
            if not KEY.fullmatch(p.stem) or p.is_symlink() or not p.is_file():continue
            files=[p]
            meta=p.with_suffix('.json')
            if meta.is_symlink():continue
            if meta.is_file():files.append(meta)
            signatures=[(f.relative_to(root).as_posix(),f.stat().st_ino,f.stat().st_size,f.stat().st_mtime_ns) for f in files]
            entries.append({'path':p,'files':files,'bytes':sum(s[2] for s in signatures),
                            'used_at':p.stat().st_mtime,'signature':signatures})
    return sorted(entries,key=lambda e:(e['used_at'],str(e['path'])))


def plan(root, *, all_idle=False):
    limits=policy()
    entries=cache_entries(root);total=sum(e['bytes'] for e in entries)
    remaining=total;chosen=[];threshold=time.time()-limits['base_cache_days']*86400
    for entry in entries:
        if all_idle or entry['used_at']<threshold or remaining>limits['base_cache_bytes']:
            chosen.append(entry);remaining-=entry['bytes']
    token=hashlib.sha256(json.dumps([e['signature'] for e in chosen],sort_keys=True).encode()).hexdigest()
    return entries,chosen,token


def report(root):
    limits=policy()
    entries,chosen,token=plan(root,all_idle=True)
    disk=shutil.disk_usage(root)
    return {'free_bytes':disk.free,'total_bytes':disk.total,'reserve_bytes':limits['reserve_bytes'],
            'cache_bytes':sum(e['bytes'] for e in entries),'cleanup_bytes':sum(e['bytes'] for e in chosen),
            'cleanup_count':len(chosen),'cleanup_token':token,'cache_limit_bytes':limits['base_cache_bytes'],
            'cache_days':limits['base_cache_days'],'source_cache_limit_bytes':limits['source_cache_bytes']}


@contextmanager
def maintenance_lock(root):
    state=root/'.web-state';state.mkdir(parents=True,exist_ok=True)
    if not safe_directory(state,root):raise ValueError('缓存管理目录无效')
    p=state/'cache-maintenance.lock'
    if p.is_symlink():raise ValueError('缓存锁目录无效')
    with p.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def cleanup(root, *, token=None):
    """Nonblocking per-entry locks protect rendering readers; lock files are never removed."""
    protect_path(root)
    removed=freed=skipped=0
    with maintenance_lock(root):
        _,chosen,current=plan(root,all_idle=token is not None)
        if token is not None and token!=current:raise ValueError('缓存清单已变化，请刷新后重新确认')
        for entry in chosen:
            p=entry['path'];lock=p.with_suffix('.lock')
            if lock.is_symlink() or not safe_directory(p.parent,root):skipped+=1;continue
            try:
                with lock.open('a') as stream:
                    try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:skipped+=1;continue
                    current=[]
                    for f in entry['files']:
                        if f.is_symlink() or not f.is_file():break
                        s=f.stat();current.append((f.relative_to(root).as_posix(),s.st_ino,s.st_size,s.st_mtime_ns))
                    if current!=entry['signature']:skipped+=1;continue
                    # Only the exact named base video and its metadata can be removed.
                    for f in entry['files']:f.unlink()
                    removed+=1;freed+=entry['bytes']
            except FileNotFoundError:skipped+=1
    return {'removed':removed,'freed_bytes':freed,'skipped_busy_or_changed':skipped}


def require_space(path, estimated_bytes=0):
    reserve=policy()['reserve_bytes']
    protect_path(path)
    ancestor=path
    while not ancestor.exists():ancestor=ancestor.parent
    free=shutil.disk_usage(ancestor).free
    import sys
    token = sys._xoptions.get('exactcut_process_token')
    jobs = data_root() / '.web-state/jobs'
    from .util import read_json
    own = next((read_json(p)['id'] for p in jobs.glob('*.json') if token and read_json(p).get('process_token') == token), None)
    device = str(ancestor.stat().st_dev)
    with reservation_lock() as ledger:
        other = sum(row.get(device, {}).get('bytes', 0) for key, row in ledger['jobs'].items() if key != own)
        own_bytes = ledger['jobs'].get(own, {}).get(device, {}).get('bytes', 0)
    # The stage's peak is already reserved; never charge the same estimate twice.
    demand = 0 if own is not None and own_bytes >= max(0, int(estimated_bytes)) else max(0, int(estimated_bytes))
    needed=reserve+other+demand
    if free<needed:
        raise ValueError(f'磁盘空间不足：剩余 {free/GIB:.1f} GB，本次预计需要 {needed/GIB:.1f} GB（含 {reserve/GIB:g} GB 预留）。请先检查缓存或腾出空间后重试')


def render_budget(timeline,render):
    duration=sum(i['end_ms']-i['start_ms'] if i['type']=='episode' else i['duration_ms'] for i in timeline)
    # Conservative allowance for units, joined video, cached base and final output.
    rate=max(1024**2,int(render['width']*render['height']*render['fps']/24))
    return int(duration/1000*rate*4)



def render_allocations(project, output, budget):
    # render_budget() is the full conservative output peak (final + preview).
    # Reserve another full budget for temporary work: base cache uses 1/4,
    # cut units/joined intermediates use 3/4 in TMPDIR. These can coexist with
    # the output, so same-device allocations add up to two full budgets.
    # These are admission estimates, not hard byte limits for CRF encoding.
    paths = {}
    for directory, size in ((project, budget // 4), (output, budget),
                            (Path(os.environ.get('TMPDIR', '/tmp')), budget - budget // 4)):
        paths[str(directory)] = paths.get(str(directory), 0) + size
    return paths

def prepare_render(project,output,timeline,render):
    root=data_root()
    # Only the environment's own data root is eligible for automatic pruning.
    if project.is_relative_to(root):cleanup(root)
    budget=render_budget(timeline,render)
    for allocation in device_allocations(render_allocations(project, output.parent, budget)).values():
        require_space(Path(allocation['path']), allocation['bytes'])


@contextmanager
def reservation_lock(jobs=None):
    from .util import read_json, write_json_atomic
    state = (jobs.parent if jobs is not None else data_root() / '.web-state')
    state.mkdir(parents=True, exist_ok=True)
    file = state / 'reservations.json'
    with (state / 'reservations.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        ledger = read_json(file) if file.exists() else {'version': 1, 'jobs': {}}
        yield ledger
        write_json_atomic(file, ledger)


def device_allocations(paths):
    """Sum independent peak allocations, including paths on the same filesystem."""
    devices = {}
    for value, size in paths.items():
        path = Path(value)
        protect_path(path)
        while not path.exists(): path = path.parent
        device = str(path.stat().st_dev)
        entry = devices.setdefault(device, {'path': str(path), 'bytes': 0})
        entry['bytes'] += max(0, int(size))
    return devices


def reserve(job_id, paths, jobs=None):
    from . import diagnostics as diag
    allocations = device_allocations(paths)
    with reservation_lock(jobs) as ledger:
        for device, allocation in allocations.items():
            used = sum(row.get(device, {}).get('bytes', 0) for key, row in ledger['jobs'].items() if key != job_id)
            free = shutil.disk_usage(allocation['path']).free
            if free < policy()['reserve_bytes'] + used + allocation['bytes']:
                diag.throttled('storage.admission_denied', job_id, level='warning', job_id=job_id,
                               free_bytes=free, reserved_bytes=used, requested_bytes=allocation['bytes'],
                               safety_bytes=policy()['reserve_bytes'])
                return False
        ledger['jobs'][job_id] = allocations
    return True


def release(job_id, jobs=None):
    with reservation_lock(jobs) as ledger: ledger['jobs'].pop(job_id, None)


def rebuild_reservations(jobs, live_ids, batches=None, root=None, *, inline_ids=(), scheduler=False):
    """Durable admission survives the spawn-before-PID crash window."""
    from .util import read_json, write_json_atomic
    with reservation_lock(jobs) as ledger:
        recovered = {key: value for key, value in ledger['jobs'].items() if key in inline_ids}
        for file in jobs.glob('*.json'):
            job = read_json(file)
            if job['id'] in live_ids:
                admission = job.get('admission') or {}
                paths = admission.get('paths')
                previous = admission.get('allocations') or ledger['jobs'].get(job['id'])
                if paths is not None:
                    allocations = device_allocations(paths)
                elif previous is not None:
                    paths = {}
                    for allocation in previous.values():
                        directory = allocation['path']
                        paths[directory] = paths.get(directory, 0) + allocation['bytes']
                    allocations = device_allocations(paths)
                elif batches is not None and root is not None:
                    try: paths = (scheduler_job_budget if scheduler else job_budget)(job, batches, root)
                    except (ValueError, RuntimeError, OSError):
                        # Existing executors cannot be denied admission retroactively.
                        peak = policy()['episode_budget_bytes'] * 4
                        paths = {str(root): peak, str(Path(os.environ.get('TMPDIR', '/tmp'))): peak}
                    allocations = device_allocations(paths)
                else: continue
                recovered[job['id']] = allocations
                job['admission'] = {**admission, 'paths': paths, 'allocations': allocations, 'rebuilt': True}
                write_json_atomic(file, job)

        ledger['jobs'] = recovered


def job_budget(job, batches, root, *, config=None):
    """Reserve fetch bytes and render's independent output/work/cache peaks."""
    from .runtime import source_root
    from .util import read_json
    path = batches / job.get('batch_id', '')
    from .concurrency import lane
    phase = job.get('phase') or lane(job)
    if phase == 'fetch':
        count = len(job.get('episodes', []))
        if (path / 'confirmed.json').exists():
            from .models import BatchManifest
            from .workflow import download_plan
            count = len(download_plan(BatchManifest.model_validate(read_json(path / 'confirmed.json')))['required'])
        return {str(source_root()): count * policy()['episode_budget_bytes']}
    if phase in {'upload', 'poll'} and job.get('batch_id'):
        from .workflow import preview_file
        video, _ = preview_file(job['batch_id'], job['request_id'])
        return {str(video.parent): video.stat().st_size}
    manifest = None
    if phase == 'render' and not job.get('batch_id'):
        from .compiler import compile_workbook
        command = job.get('command') or []
        if '--input' in command and '--project' in command:
            project = Path(command[command.index('--project') + 1])
            manifest = compile_workbook(Path(command[command.index('--input') + 1]), load_config() if config is None else config)
            path = project / 'outputs' / manifest.business_date
    if phase == 'render' and ((path / 'confirmed.json').exists() or manifest is not None):
        from .models import BatchManifest
        from .media import resolve_full_episodes
        manifest = manifest or BatchManifest.model_validate(read_json(path / 'confirmed.json'))
        render = (load_config() if config is None else config)['render']
        project = root / 'projects' / manifest.drama_id
        from .stage_results import selected_manifest
        manifest = resolve_full_episodes(selected_manifest(manifest, job), project)
        budget = sum(render_budget([item.model_dump(mode='json') for item in request.timeline], render) for request in manifest.requests)
        return render_allocations(project, path / 'production', budget)
    return {str(root): 0}



def scheduler_job_budget(job, batches, root):
    """Workbook compilation and full-episode probes share bounded off-lock prep."""
    from . import stage_results
    from .concurrency import lane
    from .util import read_json, json_sha256
    if lane(job) != 'render': return job_budget(job, batches, root)
    files = []
    path = batches / job.get('batch_id', '') / 'confirmed.json'
    if job.get('batch_id') and path.exists():
        manifest = read_json(path)
        if not any(item.get('full_episode') or item.get('to_end')
                   for request in manifest['requests'] if not job.get('request_id') or request['request_id'] == job['request_id'] for item in request['timeline']):
            return job_budget(job, batches, root)
        files = [path, *[p for p in stage_results.source_paths({**job, 'phase': 'render'}, batches, root).values() if p is not None]]
    elif not job.get('batch_id') and '--input' in (job.get('command') or []):
        command = job['command']
        files = [Path(command[command.index('--input') + 1])]
        # Legacy workbooks may request whole episodes; fence their local originals too.
        if '--project' in command:
            project = Path(command[command.index('--project') + 1])
            files += list((project / 'source/videos').glob('*.mp4'))
    if not files: return job_budget(job, batches, root)
    snapshot = {str(file): stage_results.file_identity(file) for file in files}
    config = load_config()
    key = ('budget', json_sha256({'job': job, 'snapshot': snapshot, 'config': config}))
    value = stage_results.background(key, partial(prepare_job_budget, dict(job), batches, root, config, snapshot))
    if snapshot != {str(file): stage_results.file_identity(file) for file in files}:
        raise stage_results.EvidencePending('制作输入变化，等待重新准备预算')
    return value


def prepare_job_budget(job, batches, root, config, snapshot):
    from . import stage_results
    value = job_budget(job, batches, root, config=config)
    if snapshot != {file: stage_results.file_identity(file) for file in snapshot}:
        raise stage_results.EvidenceMismatch('制作输入在预算准备期间发生变化')
    return value
