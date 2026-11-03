"""Queued immutable snapshot preparation for the existing cuts/mosaic sessions."""
import re
from . import cutting,cutting_access,workflow,interactive_queue
from .util import json_sha256,read_json,now_iso


def module(family):
    if family=='cuts':return cutting
    from . import mosaic_session
    return mosaic_session


def submit(family,bid,rid,sha,actor):
    if not isinstance(rid,str) or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):raise cutting.CutError('成片版本参数无效')
    readonly=family=='mosaics'
    cutting_access.require_editable(workflow.folder(bid),rid,source_sha=sha,readonly=readonly)
    sid=json_sha256([bid,rid,sha])[:32];owner=module(family);root=owner.session_path(bid,sid)
    if (root/'session.json').is_file():return owner.get_session(bid,sid)
    path=root.parent/'.preparations'/(sid+'.json')
    with cutting_access.gate(workflow.folder(bid),rid,readonly=readonly),cutting.lock_file(path.parent/'state.lock',blocking=True):
        cutting_access.require_editable(workflow.folder(bid),rid,source_sha=sha,readonly=readonly)
        if path.is_file():
            previous=read_json(path)
            if previous['status'] in {'queued','running'}:return previous
        session={'batch_id':bid,'id':sid,'request_id':rid,'source_sha256':sha,'reset_at':''}
        job={'schema_version':1,'id':sid,'status':'queued','actor':actor,'request_id':rid,'source_sha256':sha,'reset_at':'','created_at':now_iso(),'progress':'原片准备已接收，等待处理'}
        interactive_queue.enqueue('cut_prepare' if family=='cuts' else 'mosaic_prepare',session,path,job,{},state_lock=path.parent/'state.lock',payload={'family':family})
    return job


def state(family,bid,jid):
    if not re.fullmatch('[a-f0-9]{32}',jid):raise cutting.CutError('原片准备任务不存在',404)
    path=workflow.folder(bid)/family/'.preparations'/(jid+'.json')
    if not path.is_file():raise cutting.CutError('原片准备任务不存在',404)
    return read_json(path)


def run(entry,leases):
    from . import accounts,processing_cancel
    session=entry['session'];bid=session['batch_id'];family=entry['payload']['family']
    db=workflow.BATCHES.parent/'accounts.sqlite3'
    if db.is_file():
        user=accounts.get(db,entry['actor'])
        if not user or not user.get('enabled') or not accounts.allowed(workflow.folder(bid),user):raise cutting.CutError('无权继续原片准备',403)
    try:
        value=module(family).open_session(bid,session['request_id'],session['source_sha256'])
        processing_cancel.check()
        interactive_queue._update(entry,status='completed',result={'session':value},completed_at=now_iso(),progress='原片已准备好')
    except cutting.CutError as exc:
        if exc.status==409:interactive_queue._update(entry,status='superseded',progress='成片版本已变化，请重新打开')
        else:raise
