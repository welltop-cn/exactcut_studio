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
import time
from functools import partial
from .runtime import protect_path, data_root
from .util import load_config

GIB=1024**3
CACHE_LIMIT=20*GIB
CACHE_DAYS=7
RESERVE=5*GIB
KEY=re.compile(r'[a-f0-9]{64}')


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
    allocations = device_allocations(paths)
    with reservation_lock(jobs) as ledger:
        for device, allocation in allocations.items():
            used = sum(row.get(device, {}).get('bytes', 0) for key, row in ledger['jobs'].items() if key != job_id)
            if shutil.disk_usage(allocation['path']).free < policy()['reserve_bytes'] + used + allocation['bytes']:
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
        manifest = resolve_full_episodes(manifest, project)
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
                   for request in manifest['requests'] for item in request['timeline']):
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
