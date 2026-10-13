import pytest
from pydantic import ValidationError
from exactcut.models import TimelineItem, CutRequest
from exactcut.intake import blank_clip, compile_clips, compile_composition, validate_draft, clip_editor


def episode(**kw):
    return TimelineItem(type='episode', episode='EP1', start_ms=0, end_ms=2000, **kw)


def draft(row):
    return dict(drama_id='101500', author='test', business_date='20261013', rows=[dict(request_id='R1', seq=1, **row)])


def clip(**kw):
    return dict(blank_clip(), episode='1', end_s='02', **kw)


def test_caption_normalization_and_defaults():
    assert episode(segment_caption={'text': ' hi '}).segment_caption.model_dump() == {'text':'hi', 'duration_ms':1500}
    assert episode(segment_caption={'text':' \n '}).segment_caption is None
    assert episode().transition_in == 'none'
    row = {'clips': [clip(segment_caption={'text': '第一行\n第二行\n第三行'})]}
    compile_clips(row)
    assert validate_draft(draft(row))[1] == []


@pytest.mark.parametrize('caption', [{'text':'x','duration_ms':True}, {'text':'x','duration_ms':1.5}, {'text':'x','duration_ms':'1500'}, {'text':'x','duration_ms':99}, {'text':'x','duration_ms':10001}, {'text':'x'*121}, {'text':'a\nb\nc\nd'}])
def test_invalid_caption(caption):
    with pytest.raises(ValidationError):
        episode(segment_caption=caption)


@pytest.mark.parametrize('fields', [dict(full_episode=True), dict(to_end=True), dict(type='title_card',text='card',duration_ms=1500)])
def test_effects_require_explicit_range(fields):
    data=dict(type='episode',episode='EP1',start_ms=0,end_ms=2000,**{'segment_caption':{'text':'x'}})
    data.update(fields)
    with pytest.raises(ValidationError):
        TimelineItem(**data)


def test_first_transition_rejected():
    with pytest.raises(ValidationError):
        CutRequest(request_id='R1',seq=1,output_filename='x.mp4',timeline=[episode(transition_in='flash_white')])


def test_clip_effects_round_trip_and_structured_source_of_truth():
    row={'clips':[clip(), clip(segment_caption={'text':'second'},transition_in='flash_white')]}
    compile_clips(row)
    assert row['clips'][1]['segment_caption']['text']=='second'
    row['timeline_text']='EP9 00:00-00:03'
    manifest, errors=validate_draft(draft(row))
    assert errors==[]
    items=manifest.requests[0].timeline
    assert len(items)==2 and items[1].segment_caption.text=='second' and items[1].transition_in=='flash_white'
    assert clip_editor(manifest)['R1'][1]['segment_caption']['duration_ms']==1500
    assert items[0].duration_ms==items[1].duration_ms==2000


def test_incomplete_effect_clip_is_not_dropped_or_shifted():
    row={'clips':[blank_clip(), clip(segment_caption={'text':'second'})]}
    compile_clips(row)
    manifest, errors=validate_draft(draft(row))
    assert errors==[] and manifest.requests[0].timeline[0].segment_caption.text=='second'
    row['clips'][0]['segment_caption']={'text':'orphan'}
    compile_clips(row)
    assert validate_draft(draft(row))[1]


def test_composition_effects_preserved_and_incomplete_blocked():
    row={'composition':{'plots':[{'episode':'1','start':'00:00','end':'00:02','segment_caption':{'text':'plot'}}]}}
    compile_composition(row)
    manifest, errors=validate_draft(draft(row))
    assert errors==[] and manifest.requests[0].timeline[0].segment_caption.text=='plot'
    row['composition']['plots'].insert(0,dict(episode='',start='',end='',transition_in='flash_white'))
    compile_composition(row)
    assert validate_draft(draft(row))[1]


def test_mixed_structured_forms_rejected():
    assert validate_draft(draft({'clips':[clip()], 'composition':{'plots':[]}, 'timeline_text':'EP1 00:00-00:02'}))[1]


def test_caption_visual_overflow_blocks_save():
    row={'clips':[clip(segment_caption={'text':'W'*110})]}
    compile_clips(row)
    manifest, errors=validate_draft(draft(row))
    assert manifest is None and any('本段字幕过长' in error for error in errors)


@pytest.mark.parametrize('kind,limit', [('clips',40),('plots',12)])
def test_compile_does_not_drop_effect_rows_beyond_limit(kind,limit):
    if kind=='clips':
        row={'clips':[clip() for _ in range(limit)]+[clip(segment_caption={'text':'last'})]}
        compile_clips(row)
    else:
        row={'composition':{'plots':[dict(episode='1',start='00:00',end='00:02') for _ in range(limit)]+[dict(episode='2',start='',end='',segment_caption={'text':'last'})]}}
        compile_composition(row)
    assert validate_draft(draft(row))[1]


@pytest.mark.parametrize('kind', ['clips','plots'])
def test_compile_preserves_invalid_entries_for_validation(kind):
    if kind=='clips':
        row={'clips':[clip(), 'invalid']}
        compile_clips(row)
    else:
        row={'composition':{'plots':[dict(episode='1',start='00:00',end='00:02'), 'invalid']}}
        compile_composition(row)
    assert validate_draft(draft(row))[1]


@pytest.mark.parametrize('extra', [TimelineItem(type='title_card',text='card',duration_ms=1000), TimelineItem(type='episode',episode='EP2',full_episode=True)])
def test_effect_request_rejects_unprojectable_mixed_timeline(extra):
    with pytest.raises(ValidationError,match='最多30个明确范围剧情片段'):
        CutRequest(request_id='R1',seq=1,output_filename='x.mp4',timeline=[episode(segment_caption={'text':'x'}),extra])
    assert CutRequest(request_id='R1',seq=1,output_filename='x.mp4',timeline=[episode(),extra])


def test_effect_request_rejects_more_than_thirty_segments():
    with pytest.raises(ValidationError,match='最多30个明确范围剧情片段'):
        CutRequest(request_id='R1',seq=1,output_filename='x.mp4',timeline=[episode(segment_caption={'text':'x'})]+[episode() for _ in range(30)])


def test_flash_duration_defaults_validation_and_round_trip():
    assert episode(transition_in='flash_white').transition_duration_ms == 500
    assert episode(transition_in='flash_white', transition_duration_ms=5000).transition_duration_ms == 5000
    for invalid in (None, True, '500', 199, 5001):
        with pytest.raises(ValidationError):
            episode(transition_in='flash_white', transition_duration_ms=invalid)
    row = {'clips': [clip(), clip(transition_in='flash_white', transition_duration_ms=800)]}
    compile_clips(row)
    manifest, errors = validate_draft(draft(row))
    assert errors == []
    assert manifest.requests[0].timeline[1].transition_duration_ms == 800
    assert clip_editor(manifest)['R1'][1]['transition_duration_ms'] == 800
