"""Compute reviewable Hook previews without holding the global mutation barrier."""
import base64
import io
import shutil
from pathlib import Path
from PIL import Image
from .errors import ValidationError
from .hook_options import approved_emphasis, hook_duration
from .hook_presentation import presentation, sample_timeline_frames, preview_identity, shot_index_identity
from .renderer import source_fingerprints
from .util import json_sha256, load_config
from . import diagnostics as diag


def compute(project: Path, request, work: Path):
    if not request.hook_text.strip():
        raise ValidationError('这条素材没有 Hook，无需生成 Hook 预览')
    cache = work/'cache'
    # Read existing complete cache records; new results stay private until revision/access are rechecked.
    for directory in ('hook-emphasis','hook-placement'):
        target = cache/directory
        target.mkdir(parents=True,exist_ok=True)
        for existing in (project/'cache'/directory).glob('*.json'):
            if not existing.is_symlink() and len(existing.stem)==64:
                shutil.copyfile(existing,target/existing.name)
    with diag.span('hook_preview.emphasis', mode=request.hook_options.mode):
        emphasis = approved_emphasis(request.hook_text,request.hook_options,cache/'hook-emphasis')
    timeline = [item.model_dump(mode='json') for item in request.timeline]
    config = load_config()['render']
    frames, sources, missing = [], [], False
    try:
        sources = source_fingerprints(project,timeline)
    except ValidationError:
        missing = True
    from .timing_review import local_timeline
    timeline,states=local_timeline(project,timeline)
    if any(s['error'] for s in states):raise ValidationError('；'.join(s['error'] for s in states if s['error']))
    missing=missing or any(s['source_status']!='ready' for s in states)
    total = None if any(i['type']=='episode' and i['end_ms'] is None for i in timeline) else sum(
        i['end_ms']-i['start_ms'] if i['type']=='episode' else i['duration_ms'] for i in timeline)
    visible=hook_duration(request.hook_text,request.hook_options,total)
    with diag.span('hook_preview.sampling', visible_ms=visible):
        frames,coverage=sample_timeline_frames(project,timeline,work,visible,width=config['width'],height=config['height'])
    with diag.span('hook_preview.presentation', frame_count=len(frames), position=request.hook_options.position):
        overlay, decision = presentation(request.hook_text,emphasis,request.hook_options,frames,work,
                                        cache/'hook-placement',config['width'],config['height'])
    from .brand_overlay import render as render_brand, active as brand_active
    brand_png, floor = (None, 0)
    if brand_active(getattr(request, 'brand_overlay', None)):
        if request.brand_overlay.title_enabled and request.brand_overlay.title_mode == 'cover':
            from .title_image.extraction import extract, asset_file, AssetUnavailable
            brand = request.brand_overlay
            if brand.title_asset:
                try:
                    asset_file(brand.title_asset, brand.title_drama_id)
                except AssetUnavailable:
                    brand = brand.model_copy(update={'title_asset': ''})
            if not brand.title_asset:
                brand = brand.model_copy(update={'title_asset': extract(brand.title_drama_id)})
            request = request.model_copy(update={'brand_overlay': brand})
        brand_png, floor = render_brand(request.brand_overlay, config['width'], config['height'], work/'brand.png')
    decision.update(duration_ms=visible,sampling=coverage)
    # Only an all-sources-available preview can be reused as the final, approved placement.
    doc = {'identity':preview_identity(request,timeline,sources),'shot_indexes':shot_index_identity(project,timeline),
           'decision':decision,'sources_complete':not missing and total is not None}
    key = json_sha256(doc)
    diag.event('hook_preview.result', frame_count=len(frames), sources_complete=doc['sources_complete'],
               sampling_capped=coverage.get('sampling_capped'), emphasis_source=emphasis.get('source'))
    background = Image.open(io.BytesIO(frames[0])).convert('RGBA').resize((config['width'],config['height'])) if frames else Image.new('RGBA',(config['width'],config['height']),'#22272d')
    if brand_png is not None:
        with Image.open(brand_png) as brand:
            background.alpha_composite(brand.convert('RGBA'))
    with Image.open(overlay) as image:
        background.alpha_composite(image.convert('RGBA'))
    background.thumbnail((360,640))
    buffer = io.BytesIO()
    background.convert('RGB').save(buffer,format='PNG')
    return {'preview_key':key,'image':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode(),
            'decision':decision,'has_frame':bool(frames),'sources_complete':not missing and total is not None,
            'notice':f'预览覆盖 Hook 显示期间的 {len(frames)} 个抽样画面；请播放核对人物和原字幕遮挡。' if frames else '尚无原片，仅预览排版；下载后会重新检查位置。'}, doc, cache


# Queued HTTP contract: the report is authoritative, the queue owns waiting order.
def request_fingerprint(request):
    value=request.model_dump(mode='json')
    # The approved preview cache pointer is a receipt, not a visual input.
    if isinstance(value.get('hook_options'),dict):value['hook_options'].pop('preview_key',None)
    return json_sha256(value)


def source_identity(project,request):
    """Cheap receipt invalidation; compute still hashes sources and validates placement caches."""
    from .media import resolve_episode_video
    from .runtime import protect_path
    def identity(path):
        protect_path(path)
        try:
            stat=path.stat()
            return [stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns]
        except FileNotFoundError:return None
    sources=[]
    for episode in sorted({item.episode for item in request.timeline if item.type=='episode'}):
        try:source=identity(resolve_episode_video(project,episode))
        except ValidationError:source=None
        sources.append([episode,source,identity(project/'data/episodes'/f'{episode}.video.json')])
    return json_sha256(sources)


def current_request(batch_id,request_id):
    from . import workflow
    from .intake import validate_draft
    latest=workflow.detail(batch_id)
    manifest,errors=validate_draft(latest['draft'],str(workflow.folder(batch_id)/'source.xlsx'))
    if errors:raise ValueError('制作内容需要修正')
    request=next((row for row in manifest.requests if row.request_id==request_id),None)
    if request is None:raise ValueError('该素材已变化')
    return latest,manifest,request


def submit(batch_id,snapshot,index,request,actor):
    from . import workflow,cutting,interactive_queue
    from .util import read_json,write_json_atomic,now_iso
    fingerprint=request_fingerprint(request);config_fingerprint=json_sha256(load_config())
    source_fingerprint=source_identity(workflow.ROOT/'projects'/str(snapshot['draft']['drama_id']).strip().strip('#'),request)
    jid=json_sha256([batch_id,request.request_id,fingerprint,config_fingerprint,source_fingerprint])[:32]
    path=workflow.folder(batch_id)/'hook-previews'/(jid+'.json')
    with cutting.lock_file(path.parent/'state.lock',blocking=True):
        if path.is_file():
            previous=read_json(path)
            if previous['status'] in {'queued','running','completed'} and not previous.get('cancel_requested'):return previous
        job={'schema_version':1,'id':jid,'status':'queued','actor':actor,'index':index,'request_id':request.request_id,
             'revision':snapshot['revision'],'source_sha256':fingerprint,'configuration_sha256':config_fingerprint,
             'source_identity_sha256':source_fingerprint,'reset_at':str(snapshot['revision']),
             'created_at':now_iso(),'progress':'Hook 预览已接收，等待处理'}
        session={'batch_id':batch_id,'id':request.request_id,'request_id':request.request_id,'source_sha256':fingerprint,'reset_at':str(snapshot['revision'])}
        interactive_queue.enqueue('hook',session,path,job,{},state_lock=path.parent/'state.lock',
            payload={'index':index,'revision':snapshot['revision'],'fingerprint':fingerprint,'config_fingerprint':config_fingerprint,
                     'source_identity_sha256':source_fingerprint})
    return job


def state(batch_id,jid):
    import re
    from . import workflow
    from .util import read_json
    if not isinstance(jid,str) or not re.fullmatch('[a-f0-9]{32}',jid):raise ValueError('Hook 预览任务不存在')
    path=workflow.folder(batch_id)/'hook-previews'/(jid+'.json')
    if not path.is_file():raise ValueError('Hook 预览任务不存在')
    job=read_json(path)
    try:
        _,manifest,request=current_request(batch_id,job['request_id'])
        job['input_current']=(request_fingerprint(request)==job['source_sha256'] and json_sha256(load_config())==job['configuration_sha256']
                              and source_identity(workflow.ROOT/'projects'/manifest.drama_id,request)==job.get('source_identity_sha256'))
    except (ValueError,OSError):job['input_current']=False
    return job


def bind_confirmed_previews(path, project, manifest):
    """Freeze a matching receipt from this batch into a new execution snapshot only."""
    import re
    from .util import read_json
    from .timing_review import local_timeline
    from .hook_presentation import approved_preview
    reports = []
    for file in (path/'hook-previews').glob('*.json'):
        try:
            job = read_json(file)
            if job.get('status') == 'completed': reports.append(job)
        except (OSError, ValueError):
            continue
    config = json_sha256(load_config())
    source_cache = {}
    for request in manifest.requests:
        # Old confirmed manifests keep their pointer; new snapshots trust server reports.
        request.hook_options.preview_key = None
        matching = [job for job in reports if job.get('request_id') == request.request_id
                    and job.get('source_sha256') == request_fingerprint(request)
                    and job.get('configuration_sha256') == config]
        if not matching: continue
        try:
            source = source_identity(project, request)
            timeline, states = local_timeline(project, [i.model_dump(mode='json') for i in request.timeline])
            if any(s['error'] or s['source_status'] != 'ready' for s in states): continue
            sources = source_fingerprints(project, timeline, source_cache)
            for job in sorted(matching, key=lambda row: row.get('completed_at', ''), reverse=True):
                key = (job.get('result') or {}).get('preview_key')
                if job.get('source_identity_sha256') != source or not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key): continue
                request.hook_options.preview_key = key
                if approved_preview(project, request, timeline, sources) is not None: break
                request.hook_options.preview_key = None
        except (OSError, ValueError, ValidationError):
            request.hook_options.preview_key = None


def run_queued(entry,leases):
    import tempfile
    from . import workflow,accounts,interactive_queue,processing_cancel,webserver
    from .intake import validate_draft
    from .runtime import mutation_lock,maintenance_file
    from .stage_results import ResourceDeferred
    from .util import read_json,write_json_atomic,now_iso
    bid=entry['session']['batch_id'];binding=entry['payload'];path=interactive_queue.report(entry)
    def current():
        processing_cancel.check()
        try:latest,manifest,request=current_request(bid,entry['job']['request_id'])
        except ValueError:
            interactive_queue._update(entry,status='superseded',progress='输入已变化，旧预览不再采用');return None
        db=workflow.BATCHES.parent/'accounts.sqlite3'
        if db.is_file():
            user=accounts.get(db,entry['actor'])
            if not user or not user.get('enabled') or not accounts.allowed(workflow.folder(bid),user):raise ValueError('无权继续此预览')
        if (latest['runtime'].get('archived') or request_fingerprint(request)!=binding['fingerprint']
                or json_sha256(load_config())!=binding['config_fingerprint']
                or source_identity(workflow.ROOT/'projects'/manifest.drama_id,request)!=binding.get('source_identity_sha256')):
            interactive_queue._update(entry,status='superseded',progress='设置已变化，旧预览结果不再采用');return None
        if not latest.get('media_managed') and latest['runtime']['status']!='draft':
            interactive_queue._update(entry,status='superseded',progress='项目状态已变化，旧预览不再采用');return None
        return latest,manifest,request
    current_value=current()
    if current_value is None:return
    latest,manifest,request=current_value
    project=workflow.ROOT/'projects'/manifest.drama_id
    admission=webserver.admit_hook_preview(project)
    try:
        with tempfile.TemporaryDirectory(prefix='exactcut-hook-preview-') as temp:
            deferred=None
            try:result,doc,cache=compute(project,request,Path(temp))
            except ResourceDeferred as exc:
                diag.event('hook_preview.deferred',phase='compute',not_before=exc.not_before)
                deferred=exc;cache=Path(temp)/'cache'
            with mutation_lock():
                if current() is None:return
                if maintenance_file().exists():raise ResourceDeferred(__import__('time').time()+1,'等待维护结束后继续预览')
                for directory in ('hook-emphasis','hook-placement'):
                    for file in (cache/directory).glob('*.json'):
                        write_json_atomic(project/'cache'/directory/file.name,read_json(file))
                if deferred is not None:raise deferred
                write_json_atomic(project/'cache/hook-previews'/f"{result['preview_key']}.json",doc)
                interactive_queue._update(entry,status='completed',result=result,completed_at=now_iso(),progress='Hook 预览已生成')
    finally:webserver.release_hook_preview(admission)


def handle(handler,batch_id,jid=None):
    from . import workflow,cutting
    from .util import read_json
    if not handler.authorized():return
    if not handler.can_access(batch_id):return handler.respond(404,{'error':'项目不存在或无权访问'})
    try:
        if jid:return handler.respond(200,state(batch_id,jid))
        jobs=[]
        for path in (workflow.folder(batch_id)/'hook-previews').glob('*.json'):
            job=read_json(path);jobs.append({key:value for key,value in job.items() if key!='result'})
        return handler.respond(200,{'jobs':sorted(jobs,key=lambda row:row['created_at'],reverse=True)})
    except (ValueError,OSError):return handler.respond(404,{'error':'Hook 预览任务不存在'})
