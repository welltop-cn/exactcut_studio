"""One explicit read-only delivery check while the application is stopped."""
import argparse
import json
import os
import time
import uuid

from . import batch_control, stage_results, storage, workflow, webserver
from .runtime import maintenance_file, mutation_lock, project_env_file
from .util import load_env_file, read_json


def verify_delivery(batch_id, request_id, sha256):
    if os.environ.get('EXACTCUT_WORKER_ENABLED') != '0' or not maintenance_file().exists():
        raise RuntimeError('只读维护核实必须停用worker并保留维护标记')
    with mutation_lock():
        path = workflow.folder(batch_id)
        if any(batch_control.own_live_children(p) for p in workflow.BATCHES.glob('*')):
            raise RuntimeError('执行进程尚未停止，禁止维护核实')
        jobs = list(webserver.stored_jobs())
        if any(j.get('status') not in {'completed', 'failed', 'interrupted', 'unverified'} for j in jobs):
            raise RuntimeError('队列尚未排空，禁止维护核实')
        workflow.approval_evidence(batch_id, request_id, sha256)
        workflow.recover_verification_queue(jobs, batch_id=batch_id)
        entry = read_json(path / 'publications.json')[request_id]
        previous = max((j for j in jobs if j.get('batch_id') == batch_id and j.get('request_id') == request_id
                        and j.get('actor') == 'maintenance-cli'), key=lambda j: j.get('created', 0), default=None)
        if previous and previous.get('status') == 'unverified' and not workflow.delivery_blocked(entry):
            proof = stage_results.verified(previous, webserver.JOBS, workflow.BATCHES, workflow.ROOT)
            if proof and proof.get('status') == 'done' and proof.get('publication') == entry:
                webserver.finish_job(previous, 0)
            else:
                # The one-off process died before sealing its result. Keep the
                # original receipts fenced and allow only another explicit read.
                webserver.finish_job(previous, 2, '维护核实本地落盘中断', local_failure=True)
            jobs = list(webserver.stored_jobs())
            entry = read_json(path / 'publications.json')[request_id]
        binding = workflow.verification_binding(entry)
        if binding['approved_sha256'] != sha256:
            raise ValueError('校验值与原交付不一致')
        # A crash after persisting ready evidence but before resolving old jobs
        # resumes that proof, never an upload or another remote write.
        if entry.get('status') == 'fbbm_ready':
            for job in jobs:
                if (job.get('batch_id') == batch_id and job.get('request_id') == request_id
                        and job.get('action') == 'verify-delivery' and job.get('status') == 'completed'
                        and job.get('verification_only') == binding):
                    result = stage_results.verified(job, webserver.JOBS, workflow.BATCHES, workflow.ROOT)
                    if result and result.get('publication') == entry:
                        webserver.resolve_verified_delivery(job, result)
                        return {'status': 'verified', 'job_id': job['id']}
            raise RuntimeError('缺少已完成的只读核实证据，不能解除阻断')
        job = {'id': uuid.uuid4().hex, 'batch_id': batch_id, 'request_id': request_id,
               # Never put a one-off maintenance command in the worker queue,
               # including if this process crashes and the OLD image restarts.
               'action': 'verify-delivery', 'status': 'unverified', 'created': time.time(),
               'actor': 'maintenance-cli', 'verification_only': binding}
        try:
            workflow.queue_verify_delivery(batch_id, request_id, sha256, _job_id=job['id'])
            webserver.prepare_staged_job(job)
            webserver.save(job)
        except Exception:
            workflow.recover_verification_queue(list(webserver.stored_jobs()), batch_id=batch_id)
            raise
        workflow.recover_verification_queue(list(webserver.stored_jobs()), batch_id=batch_id)
        job.update(attempt=1, input_fingerprint=stage_results.fingerprint(job, workflow.BATCHES, workflow.ROOT))
        webserver.save(job)
        try:
            if not storage.reserve(job['id'], storage.job_budget(job, workflow.BATCHES, workflow.ROOT), webserver.JOBS):
                raise stage_results.ResourceDeferred(time.time() + 1, '核实磁盘资源不足')
            code = workflow.poll(batch_id, request_id)
            entry = read_json(path / 'publications.json')[request_id]
            if entry['status'] != 'fbbm_ready':
                # No scheduler is running here: a single explicit command must
                # leave uncertainty fenced for another explicit read, not queue
                # an unattended poll that would strand the old executor again.
                entry.update(status='unverified', error='维护核实尚未就绪，请再次只读查询原回执')
                entry['intent']['status'] = 'pending'
                workflow.store_publication(path, request_id, entry)
                code = 2
        except (stage_results.ResourceDeferred, OSError, ValueError, RuntimeError):
            entry = read_json(path / 'publications.json')[request_id]
            entry.update(status='unverified', error='维护核实未完成，可再次只读查询')
            entry['intent']['status'] = 'pending'
            workflow.store_publication(path, request_id, entry)
            code = 2
        finally:
            storage.release(job['id'], webserver.JOBS)
        stage_results.complete(job, webserver.JOBS, workflow.BATCHES, workflow.ROOT, code)
        webserver.finish_job(job, code)
        return {'status': 'verified' if code == 0 else 'unverified', 'job_id': job['id']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch_id')
    parser.add_argument('request_id')
    parser.add_argument('sha256')
    parser.add_argument('--once', action='store_true', required=True)
    args = parser.parse_args()
    load_env_file(project_env_file())
    result = verify_delivery(args.batch_id, args.request_id, args.sha256)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'verified' else 2


if __name__ == '__main__':
    raise SystemExit(main())
