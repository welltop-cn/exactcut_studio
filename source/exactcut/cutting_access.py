"""Shared admission rules for pre-delivery edits and current-media changes."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256

from .util import read_json


@dataclass(frozen=True)
class InteractiveReadPermit:
    """Internal admission identity, never reconstructed from a client request."""
    batch_id:str
    session_id:str
    request_id:str
    job_id:str
    source_sha256:str
    reset_at:str
    actor:str

    @classmethod
    def issue(cls,session,job):
        from .cutting import CutError
        if job.get('queued_task') or job.get('status')!='running':raise CutError('交互只读许可仅用于已准入任务',503)
        return cls(session['batch_id'],session['id'],session['request_id'],job['id'],session['source_sha256'],session.get('reset_at',''),job.get('actor','admin'))

    def require(self,session,job):
        from . import processing_cancel,accounts,workflow
        from .cutting import CutError
        processing_cancel.check()
        actual=(session['batch_id'],session['id'],session['request_id'],job['id'],job['source_sha256'],job.get('reset_at',''),job.get('actor','admin'))
        expected=(self.batch_id,self.session_id,self.request_id,self.job_id,self.source_sha256,self.reset_at,self.actor)
        if actual!=expected or job.get('queued_task') or session['source_sha256']!=self.source_sha256 or session.get('reset_at','')!=self.reset_at:
            raise CutError('交互任务的来源或许可已失效',409)
        db=workflow.BATCHES.parent/'accounts.sqlite3'
        if db.is_file():
            user=accounts.get(db,self.actor)
            if not user or not user.get('enabled') or not accounts.allowed(workflow.folder(self.batch_id),user):raise CutError('交互任务账号已停用或无权访问项目',403)


@contextmanager
def gate(path, request_id=None, *, allow_job=None, readonly=False):
    """Cross-process gate. Whole-batch writers exclude per-asset operations."""
    from .cutting import lock_file
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ValueError('无效素材 ID')
    directory = path.parent.parent / 'media-edit-locks'
    from .cutting import CutError
    try:
        batch = lock_file(directory / (path.name + '.lock'), shared=request_id is not None)
    except CutError as exc:
        raise CutError('素材正在执行其他操作，请稍后重试', 409) from exc
    asset = None
    try:
        if request_id is not None:
            key = sha256(request_id.encode()).hexdigest()
            try:
                asset = lock_file(directory / (path.name + '-' + key + '.lock'))
            except CutError as exc:
                raise CutError('该素材正在执行其他操作，请稍后重试', 429) from exc
        if not readonly:require_unfrozen(path,request_id,allow_job=allow_job)
        yield
    finally:
        if asset: asset.close()
        batch.close()


def delivery_locked(entry):
    if not entry:
        return False
    intent = entry.get('intent') or {}
    if any(entry.get(k) for k in ('publication', 'fbbm', 'verification_only', 'verification_history')) or intent.get('receipt') or intent.get('feishu_record_id'):
        return True
    if entry.get('status') in {'pending', 'upload_queued', 'uploading', 'fbbm_queued', 'fbbm_ready', 'unverified'}:
        return True
    # Only a specifically proven no-effect failure releases editing.
    return not (entry.get('status') == 'publish_failed' and intent.get('status') == 'preparation_failed')


def require_unfrozen(path,request_id=None,*,allow_job=None):
    from . import mosaic_queue
    from .cutting import CutError
    if mosaic_queue.blocked(path.name,request_id,allow_job):
        raise CutError('素材已提交剪切打码，请取消任务或确认/放弃结果后再编辑',409)


def require_editable(path, request_id, *, source_sha=None, allow_job=None, readonly=False):
    from . import workflow as w, material_versions as m, batch_control
    from .cutting import CutError
    if not readonly:require_unfrozen(path,request_id,allow_job=allow_job)
    batch_control.ensure_editable(path, execution=True)
    record = w.result_records(path).get(request_id, {})
    if record.get('status') in {'fbbm_ready', 'fbbm_queued', 'upload_queued', 'uploading', 'unverified'} or record.get('publication') or record.get('fbbm'):
        raise CutError('该素材已有交付记录，不能再剪切或打码', 409)
    if m.managed(path):
        state = m.read(path)
        m._editable(state, execution=True)
        asset = state['assets'].get(request_id)
        if state['deleted_at'] or not asset or asset['deleted_at']:
            raise CutError('素材不存在', 404)
        if any(d['asset_id'] == request_id and (d.get('status') != 'publish_failed' or delivery_locked(d.get('publication') or {'status': 'unverified'})) for d in state['deliveries'].values()):
            raise CutError('该素材已提交 FBBM，不能再剪切或打码', 409)
        if asset['candidate_version']:
            raise CutError('素材正在制作新成片，请完成后再编辑', 409)
    else:
        pubs = path / 'publications.json'
        entry = read_json(pubs).get(request_id, {}) if pubs.exists() else {}
        if delivery_locked(entry):
            raise CutError('该素材已提交 FBBM，不能再剪切或打码', 409)
        runtime = read_json(path / 'runtime.json')
        record = w.result_records(path).get(request_id, {})
        if record.get('executor_running') or (not runtime.get('request_parallel') and (runtime.get('executor_running') or runtime.get('status') in w.ACTIVE)):
            raise CutError('视频正在处理，请完成后再编辑', 409)
    for file in (path.parent.parent / 'jobs').glob('*.json'):
        job = read_json(file)
        if job.get('batch_id') == path.name and job.get('status') in {'queued', 'running', 'waiting_resource'} and (not job.get('request_id') or job['request_id'] == request_id):
            raise CutError('素材仍有处理任务，请完成后再编辑', 409)
    if source_sha is not None:
        _, record = w.preview_file(path.name, request_id)
        if record.get('output_sha256') != source_sha:
            raise CutError('当前成片已变化，请刷新后重新编辑', 409)


def can_edit(path, request_id):
    try:
        require_editable(path, request_id, readonly=True)
        return True
    except (ValueError, OSError, KeyError):
        return False
