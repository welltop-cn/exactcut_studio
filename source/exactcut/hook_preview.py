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
    decision.update(duration_ms=visible,sampling=coverage)
    # Only an all-sources-available preview can be reused as the final, approved placement.
    doc = {'identity':preview_identity(request,timeline,sources),'shot_indexes':shot_index_identity(project,timeline),
           'decision':decision,'sources_complete':not missing and total is not None}
    key = json_sha256(doc)
    diag.event('hook_preview.result', frame_count=len(frames), sources_complete=doc['sources_complete'],
               sampling_capped=coverage.get('sampling_capped'), emphasis_source=emphasis.get('source'))
    background = Image.open(io.BytesIO(frames[0])).convert('RGBA').resize((config['width'],config['height'])) if frames else Image.new('RGBA',(config['width'],config['height']),'#22272d')
    with Image.open(overlay) as image:
        background.alpha_composite(image.convert('RGBA'))
    background.thumbnail((360,640))
    buffer = io.BytesIO()
    background.convert('RGB').save(buffer,format='PNG')
    return {'preview_key':key,'image':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode(),
            'decision':decision,'has_frame':bool(frames),'sources_complete':not missing and total is not None,
            'notice':f'预览覆盖 Hook 显示期间的 {len(frames)} 个抽样画面；请播放核对人物和原字幕遮挡。' if frames else '尚无原片，仅预览排版；下载后会重新检查位置。'}, doc, cache
