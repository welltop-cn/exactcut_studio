"""Explicitly retire one permanently superseded, never-launched render job.

Defaults to inspection. This narrow recovery command preserves the job file so
the old scheduler cannot recreate its deterministic successor. Maintenance apply
also terminates the child runtime projection, never media authority or outputs.
"""
import argparse
import json
import os
import re
import time
import uuid

from . import workflow as w, material_versions as m, material_workers, batch_control
from .runtime import mutation_lock, maintenance_file
from .util import read_json

MESSAGE = '已恢复永久失效的未启动候选任务'


def retire(parent_id, child_id, job_id, *, apply=False):
    if apply and (os.environ.get('EXACTCUT_WORKER_ENABLED') != '0' or not maintenance_file().exists()):
        raise ValueError('恢复写入必须停用 worker 并保留维护标记')
    if not all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value)
               for value in (parent_id, child_id, job_id)):
        raise ValueError('无效恢复身份')
    parent, child = w.folder(parent_id), w.folder(child_id)
    jobs = w.ROOT / '.web-state/jobs'
    with mutation_lock(), m.locked(parent):
        state = m._read(parent)
        event = state['outbox'].get(child_id)
        if (not event or event.get('kind') != 'candidate' or event.get('child_id') != child_id
                or m.internal(child) != material_workers.lineage(parent, event)):
            raise ValueError('不是已登记候选，禁止恢复')
        asset, version = state['assets'][event['asset_id']], state['versions'][event['version_id']]
        if (version.get('status') != 'superseded'
                or event['version_id'] in (asset.get('current_version'), asset.get('candidate_version'))):
            raise ValueError('候选未永久失效，禁止恢复')
        job = read_json(jobs / (job_id + '.json'))
        expected = uuid.uuid5(uuid.NAMESPACE_URL, event['job_id'] + ':render:' + event['asset_id']).hex
        if (job_id != expected or job.get('id') != job_id or job.get('batch_id') != child_id
                or job.get('request_id') != event['asset_id'] or job.get('parent_id') != event['job_id']
                or job.get('phase') != 'render' or job.get('action') != 'start'
                or not batch_control.belongs(job)):
            raise ValueError('执行身份不匹配，禁止恢复')
        if job.get('attempt') not in (None, 0) or any(job.get(k) for k in
                ('pid', 'pgid', 'process_token', 'input_fingerprint', 'admission')):
            raise ValueError('任务可能已启动，禁止在线恢复')
        retired = job.get('status') == 'interrupted' and job.get('error') == MESSAGE
        if job.get('status') != 'queued' and not retired:
            raise ValueError('任务不是未启动排队状态')
        fetch = read_json(jobs / (event['job_id'] + '.json'))
        manifest = read_json(child / 'confirmed.json')
        if (fetch.get('status') != 'completed' or fetch.get('phase') != 'fetch'
                or fetch.get('batch_id') != child_id
                or [row['request_id'] for row in manifest['requests']] != [event['asset_id']]):
            raise ValueError('前置任务未完成或候选包含其他素材')
        control_file = child / 'control.json'
        control = read_json(control_file) if control_file.exists() else {}
        if control.get('children') or control.get('suspended'):
            raise ValueError('候选仍有进程记录，禁止在线恢复')
        for file in jobs.glob('*.json'):
            other = read_json(file)
            if (other.get('batch_id') == child_id and other.get('id') != job_id
                    and other.get('status') not in {'completed', 'failed', 'interrupted'}):
                raise ValueError('候选仍有其他未结束任务')
        if apply and not retired:
            if read_json(jobs / (job_id + '.json')) != job:
                raise ValueError('任务已变化，请重新检查')
            job.update(status='interrupted', finished=time.time(), error=MESSAGE)
            m.durable_write(jobs / (job_id + '.json'), job)
    if apply:
        # Maintenance has stopped the worker. Never nest the runtime lock under
        # the parent lock, and replay this projection if the first write crashed.
        w.end_execution(child, status='interrupted', message=MESSAGE)
    return {'status': 'already_retired' if retired else 'retired' if apply else 'eligible', 'job_id': job_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('parent_id', 'child_id', 'job_id'): parser.add_argument(name)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(retire(args.parent_id, args.child_id, args.job_id, apply=args.apply)))


if __name__ == '__main__':
    main()
