"""Prepare only the episodes named by an editable draft, using the durable runner."""
from copy import deepcopy
import json
import re
import sys
from types import SimpleNamespace

from . import source_cache, workflow
from .intake import compile_clips, compile_composition, normalized_timeline, timing_explanation
from .compiler import parse_segment_cell
from .runtime import instance_marker, project_env_file
from .util import load_env_file, now_iso, read_json, write_json_atomic


def snapshot(draft):
    if not isinstance(draft, dict):
        raise ValueError('正片准备内容格式错误')
    drama = str(draft.get('drama_id') or '').strip()
    if not re.fullmatch(r'\d{6}', drama) or not 101050 <= int(drama) <= 999999:
        raise ValueError('剧 ID 必须为 101050～999999 范围内的六位数字')
    rows = draft.get('rows')
    if not isinstance(rows, list) or len(rows) > 200 or len(json.dumps(draft)) > 1_000_000:
        raise ValueError('正片准备内容格式错误')
    requests = []
    frame_episodes = set()
    for original in rows:
        if not isinstance(original, dict):
            continue
        row = deepcopy(original)
        # Frame drafts need the source before time fields can be validated.
        for clip in row.get('clips', []) if isinstance(row.get('clips'), list) else []:
            if not isinstance(clip, dict) or clip.get('time_format') != 'source_frames_v1':
                continue
            episode = re.sub(r'(?i)^ep', '', str(clip.get('episode') or '').strip())
            if episode.isdigit() and 1 <= int(episode) <= 500:
                frame_episodes.add('EP' + str(int(episode)))
        compile_composition(row)
        compile_clips(row)
        raw = str(row.get('timeline_text') or '')
        mode = draft.get('time_notation', 'seconds')
        if mode == 'unconfirmed' and re.search(r'\d+\.\d{1,2}s', raw, re.I):
            continue
        timeline = []
        for line in normalized_timeline(raw, mode).splitlines():
            try:
                item = parse_segment_cell(line, 1500)
                if item and item.type == 'episode':
                    source_cache.check_episode(item.episode)
                    if item.start_ms < 0 or (item.end_ms is not None and item.end_ms <= item.start_ms):
                        continue
                if item:
                    timeline.append(item)
            except (ValueError, RuntimeError, TypeError):
                continue
        requests.append(SimpleNamespace(request_id=str(row.get('request_id') or ''), timeline=timeline))
    episodes = sorted(frame_episodes | {t.episode for r in requests for t in r.timeline if t.type == 'episode'},
                      key=lambda ep: int(ep[2:]))
    return drama, episodes, SimpleNamespace(requests=requests, frame_episodes=frame_episodes)


def ensure_editable(path):
    from . import material_versions as m, batch_control
    if m.managed(path):
        m._editable(m.read(path), execution=True)
    else:
        batch_control.ensure_editable(path)
        if workflow.runtime(path)['status'] != 'draft' or (path / 'confirmed.json').exists():
            raise ValueError('仅草稿可提前准备正片')


def editable(path):
    try:
        ensure_editable(path)
        return True
    except ValueError:
        return False


def result_path(path, job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id or ''):
        raise ValueError('无效正片任务 ID')
    return path / 'source-prefetch-results' / (job_id + '.json')


def signature(drama, episode):
    from .media import resolve_episode_video
    try:
        video = resolve_episode_video(workflow.ROOT / 'projects' / drama, episode)
        stat = video.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    except (OSError, RuntimeError, ValueError):
        return None


def cached(drama, episode):
    video = source_cache.source_root() / drama / (episode + '.mp4')
    meta = source_cache._read_json(video.with_suffix('.json'))
    return bool(isinstance(meta, dict) and meta.get('complete') and video.is_file()
                and not video.is_symlink() and video.stat().st_size > 0
                and video.stat().st_size == meta.get('bytes'))


def frame_index_ready(path, drama, episode):
    from .source_picker import _video, _fingerprint, _load
    from .util import json_sha256
    try:
        key = json_sha256({'version': 1, 'drama': drama, 'episode': episode,
                          'signature': _fingerprint(_video(drama, episode))})
        _load(path, key)
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False


def status(path, drama, episodes, jobs, manifest):
    states = []
    for episode in episodes:
        state = {'episode': episode, 'status': 'waiting', 'received_bytes': 0, 'total_bytes': 0}
        matches = [job for job in jobs if job.get('project') == drama and episode in job.get('episodes', [])]
        job = max(matches, key=lambda j: j.get('created', 0), default=None)
        if job:
            file = result_path(path, job['id'])
            result = read_json(file) if file.exists() else {}
            saved = result.get('episodes', {}).get(episode, {})
            state.update({k: v for k, v in saved.items() if k != 'signature'})
            if saved.get('status') == 'skipped':
                state.update(status='waiting')
            elif saved.get('status') == 'ready' and saved.get('signature') != signature(drama, episode):
                state.update(status='waiting')
            elif job.get('status') in {'failed', 'interrupted'} and state['status'] != 'ready':
                state.update(status='failed')
            elif state['status'] == 'waiting' and job.get('status') in {'queued', 'running'}:
                state.update(status='queued')
        if cached(drama, episode):
            state.update(status='ready')
            state.pop('error_info', None)
        if episode in getattr(manifest, 'frame_episodes', set()):
            if frame_index_ready(path, drama, episode):
                state.update(status='ready')
                state.pop('error_info', None)
            elif job and job.get('status') in {'queued', 'running'}:
                state.update(status='queued')
            elif job and job.get('status') in {'failed', 'interrupted'}:
                state.update(status='failed')
            else:
                state.update(status='waiting')
        states.append(state)
    explanation = timing_explanation(manifest, workflow.ROOT / 'projects' / drama, wait=False)
    by_episode = {state['episode']: state for state in states}
    for request in explanation:
        for segment in request['segments']:
            state = by_episode.get(segment['episode'])
            if state and segment.get('source_status') == 'missing':
                segment['source_status'] = state['status'] if state['status'] != 'ready' else 'checking'
    return {'drama_id': drama, 'episodes': states, 'timing_explanation': explanation}


def download_progress(path, drama, episodes, jobs):
    """Read queued production's required sources without mutating its runtime."""
    if not episodes or not any(job.get('project') == drama and set(episodes).intersection(job.get('episodes', []))
                               for job in jobs):
        return {}
    states = status(path, drama, episodes, jobs, None)['episodes']
    ready = [state for state in states if state['status'] == 'ready']
    active = next((state for state in states if state['status'] == 'downloading'), None)
    received = active['received_bytes'] if active else 0
    completed_bytes = 0
    for state in ready:
        file_signature = signature(drama, state['episode'])
        completed_bytes += file_signature[2] if file_signature else state['total_bytes']
    return {'download_prefetch': active is not None, 'download_complete': len(ready) == len(states),
            'download_files': len(ready), 'download_bytes': completed_bytes + received,
            'download_current_episode': active['episode'] if active else None,
            'download_current_received': received,
            'download_current_total': active['total_bytes'] if active else 0,
            'download_speed_bps': None}


def run(batch_id, job_id):
    path = workflow.folder(batch_id)
    target = result_path(path, job_id)
    job = read_json(workflow.ROOT / '.web-state/jobs' / (job_id + '.json'))
    if job.get('batch_id') != batch_id or job.get('instance') != instance_marker():
        raise ValueError('正片任务实例不匹配')
    result = {'status': 'running', 'episodes': {}, 'updated_at': now_iso()}
    def store():
        result['updated_at'] = now_iso()
        write_json_atomic(target, result)
    failed = False
    for episode in job['episodes']:
        desired = read_json(path / 'source-prefetch.json')
        if (not editable(path) or desired['drama_id'] != job['project']
                or episode not in desired['episodes']):
            result['episodes'][episode] = {'status': 'skipped'}
            store()
            continue
        state = result['episodes'][episode] = {'status': 'downloading', 'received_bytes': 0, 'total_bytes': 0}
        store()
        def progress(ep, received, total):
            state.update(received_bytes=received, total_bytes=total)
            store()
        try:
            needs_frames = episode in desired.get('frame_episodes', [])
            if not needs_frames or signature(job['project'], episode) is None:
                source_cache.ensure(job['project'], [episode], on_progress=progress)
            if needs_frames:
                from .source_picker import prepare
                prepare(path, {'drama_id': job['project'], 'rows': [{'clips': [
                    {'episode': episode, 'time_format': 'source_frames_v1'}]}]}, episode)
            state.update(status='ready', signature=signature(job['project'], episode))
        except (ValueError, RuntimeError, OSError) as exc:
            from .stage_results import ResourceDeferred
            if isinstance(exc, ResourceDeferred):
                state.update(status='waiting_resource')
                result.update(status='waiting_resource', not_before=exc.not_before)
                store()
                raise
            from .error_guidance import guidance
            state.update(status='failed', error_info=guidance(str(exc), 'download'))
            failed = True
        store()
    result['status'] = 'failed' if failed else 'completed'
    store()
    return 2 if failed else 0


if __name__ == '__main__':
    load_env_file(project_env_file())
    from . import diagnostics as diag
    diag.configure()
    diag.install_logging()
    from .stage_results import run_child
    raise SystemExit(run_child(run, sys.argv[1], sys.argv[2]))
