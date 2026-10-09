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
    needed=reserve+max(0,int(estimated_bytes))
    if free<needed:
        raise ValueError(f'磁盘空间不足：剩余 {free/GIB:.1f} GB，本次预计需要 {needed/GIB:.1f} GB（含 {reserve/GIB:g} GB 预留）。请先检查缓存或腾出空间后重试')


def render_budget(timeline,render):
    duration=sum(i['end_ms']-i['start_ms'] if i['type']=='episode' else i['duration_ms'] for i in timeline)
    # Conservative allowance for units, joined video, cached base and final output.
    rate=max(1024**2,int(render['width']*render['height']*render['fps']/24))
    return int(duration/1000*rate*4)


def prepare_render(project,output,timeline,render):
    root=data_root()
    # Only the environment's own data root is eligible for automatic pruning.
    if project.is_relative_to(root):cleanup(root)
    budget=render_budget(timeline,render)
    for path in {project,output.parent,Path(os.environ.get('TMPDIR','/tmp'))}:
        require_space(path,budget)
