import copy
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from exactcut import hook_emphasis as semantic
from exactcut import hook_presentation as display
from exactcut import renderer
from exactcut.errors import ValidationError
from exactcut.hook_options import HookOptions, approved_emphasis, hook_duration
from exactcut.models import CutRequest
from exactcut.util import file_sha256, json_sha256, write_json_atomic, load_config

TEXT='They abandoned the true heiress, but she returned as the Zombie Queen!'


def request(text=TEXT, **options):
    return CutRequest(request_id='R1',seq=1,output_filename='test.mp4',hook_text=text,hook_options=options,
        timeline=[{'type':'title_card','text':'One Hour Earlier','duration_ms':1500}])


def test_manual_and_none_are_explicit_fallbacks_without_ai(tmp_path,monkeypatch):
    monkeypatch.setattr('exactcut.hook_options.resolve_emphasis',lambda *a:pytest.fail('must not call AI'))
    text='👑 The true heiress returns'
    start=text.index('true heiress')
    r=request(text,mode='manual',manual_ranges=[{'start':start,'end':start+12}])
    result=approved_emphasis(text,r.hook_options,tmp_path)
    assert result['highlights'][0]['phrase']=='true heiress'
    assert approved_emphasis(text,HookOptions(mode='none'),tmp_path)['highlights']==[]
    assert approved_emphasis(text,HookOptions(mode='none'),tmp_path)['source']=='user_none'
    with pytest.raises(ValueError,match='手工重点'):
        request(text,mode='manual',manual_ranges=[{'start':0,'end':99}])
    with pytest.raises(ValueError,match='手工重点'):
        request(text,mode='manual',manual_ranges=[{'start':3,'end':7},{'start':5,'end':10}])


def test_adaptive_duration_is_bounded_and_never_outlasts_video():
    assert hook_duration('I survived.',HookOptions())==4500
    assert hook_duration('word '*40,HookOptions())==9000
    assert hook_duration(TEXT,HookOptions(duration_ms=7200))==7200
    assert hook_duration(TEXT,HookOptions(duration_ms=7200),1500)==1500
    with pytest.raises(ValueError):
        HookOptions(duration_ms=15000)


def test_placement_avoids_face_and_subtitle_and_explains_unavoidable_overlap():
    regions=[{'kind':'face','x':.2,'y':.06,'width':.6,'height':.25},
             {'kind':'subtitle','x':.1,'y':.60,'width':.8,'height':.18}]
    result=display.select_position(regions)
    assert result['position']=='middle' and not result['warning']
    result=display.select_position([{'kind':'face','x':0,'y':0,'width':1,'height':1}])
    assert result['warning'] and result['overlap_score']>0


def test_vision_failure_keeps_semantic_words_but_has_visible_warning(tmp_path,monkeypatch):
    monkeypatch.setattr(display,'environment',lambda:'production')
    monkeypatch.setattr(display,'ai_model',lambda:'test-model')
    monkeypatch.setattr(display,'request_regions',lambda *a:(_ for _ in ()).throw(ValidationError('vision failed')))
    result=display.placement([b'fake-image'],HookOptions(),{'source':'ai_semantic'},tmp_path)
    assert result['source']=='safe_default' and result['warning']


@pytest.mark.parametrize('position',['top','middle','bottom'])
def test_manual_position_never_needs_external_service_and_stays_in_frame(tmp_path,position):
    emphasis=approved_emphasis(TEXT,HookOptions(mode='none'),tmp_path)
    overlay,decision=display.presentation(TEXT,emphasis,HookOptions(mode='none',position=position),[],tmp_path,tmp_path)
    from PIL import Image
    decision['frame_profile']={'scrim_alpha':0}
    overlay=display.render_personalized_hook_png(TEXT,tmp_path,decision)
    bbox=Image.open(overlay).getbbox()
    assert bbox[0]>=1080*.07 and bbox[2]<=1080*.93
    assert bbox[1]>=int(1920*display.POSITIONS[position])
    assert bbox[3]<=1920*(display.POSITIONS[position]+.18)
    assert len(decision['layout']['lines'])<=3


def test_approved_preview_requires_matching_text_options_timeline_sources_and_digest(tmp_path):
    r=request(mode='none')
    timeline=[i.model_dump(mode='json') for i in r.timeline]
    doc={'identity':display.preview_identity(r,timeline,[]),'decision':{'emphasis':{'highlights':[]}},'sources_complete':True}
    key=json_sha256(doc)
    write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json',doc)
    r.hook_options.preview_key=key
    assert display.approved_preview(tmp_path,r,timeline,[])==doc['decision']
    assert display.approved_preview(tmp_path,r,timeline,[{'episode':'EP1','sha256':'changed'}]) is None
    r.hook_text+=' extra'
    assert display.approved_preview(tmp_path,r,timeline,[]) is None
    r.hook_text=TEXT
    r.hook_options.position='bottom'
    assert display.approved_preview(tmp_path,r,timeline,[]) is None
    r.hook_options.position='auto'
    doc['decision']['emphasis']['highlights']=['tampered']
    write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json',doc)
    assert display.approved_preview(tmp_path,r,timeline,[]) is None


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='requires ffmpeg')
def test_hook_only_remake_reuses_checked_base_and_corruption_or_timeline_changes_rebuild(tmp_path,monkeypatch):
    config=load_config()
    config['render'].update(width=540,height=960,preset='ultrafast')
    r=request(mode='none',position='top')
    timeline=[i.model_dump(mode='json') for i in r.timeline]
    first=renderer.render_request(tmp_path,r,timeline,tmp_path/'first.mp4',config)
    original_hash=file_sha256(tmp_path/'first.mp4')
    assert not first['base_cache_reused']
    original_cut=renderer._render_card_unit
    monkeypatch.setattr(renderer,'_render_card_unit',lambda *a:pytest.fail('Hook-only edit should reuse base'))
    r.hook_text='She survived and returned!'
    r.hook_options.style='danger_warning'
    second=renderer.render_request(tmp_path,r,timeline,tmp_path/'second.mp4',config)
    assert second['base_cache_reused'] and first['base_cache_key']==second['base_cache_key']
    assert second['expected_duration_ms']==1500 and abs(second['hook_visual']['duration_ms']-1500)<30
    assert file_sha256(tmp_path/'first.mp4')==original_hash
    monkeypatch.setattr(renderer,'_render_card_unit',original_cut)
    base=tmp_path/'cache/render-base'/f"{first['base_cache_key']}.mp4"
    base.write_bytes(b'corrupted cache')
    third=renderer.render_request(tmp_path,r,timeline,tmp_path/'third.mp4',config)
    assert not third['base_cache_reused']
    changed=copy.deepcopy(timeline)
    changed[0]['duration_ms']=1800
    fourth=renderer.render_request(tmp_path,r,changed,tmp_path/'fourth.mp4',config)
    assert not fourth['base_cache_reused'] and fourth['base_cache_key']!=first['base_cache_key']
    assert fourth['expected_duration_ms']==1800


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='requires ffmpeg')
def test_twenty_second_episode_is_rendered_as_twenty_seconds_and_source_changes_invalidate_cache(tmp_path):
    source=tmp_path/'source/videos/EP1.mp4'
    source.parent.mkdir(parents=True)
    def make_source(color):
        subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'color={color}:s=180x320:r=30:d=23',
            '-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','23','-c:v','libx264','-preset','ultrafast',
            '-c:a','aac',str(source)],check=True)
    make_source('navy')
    r=CutRequest(request_id='R20',seq=1,output_filename='20.mp4',timeline=[dict(type='episode',episode='EP1',start_ms=2000,end_ms=22000)])
    timeline=[i.model_dump(mode='json') for i in r.timeline]
    config=load_config();config['render'].update(width=180,height=320,preset='ultrafast')
    sha=file_sha256(source)
    first=renderer.render_request(tmp_path,r,timeline,tmp_path/'first.mp4',config)
    assert first['unit_checks'][0]['expected_ms']==20000
    assert abs(first['unit_checks'][0]['actual_ms']-20000)<100
    assert abs(renderer.duration_ms(tmp_path/'first.mp4')-20000)<100
    assert file_sha256(source)==sha
    make_source('maroon')
    second=renderer.render_request(tmp_path,r,timeline,tmp_path/'second.mp4',config)
    assert not second['base_cache_reused'] and first['base_cache_key']!=second['base_cache_key']


def test_browser_manual_token_offsets_match_python_after_emoji_newlines_and_punctuation():
    if not shutil.which('node'):
        pytest.skip('requires node')
    script=(Path(__file__).resolve().parents[1]/'exactcut/web/app.js').read_text()
    functions='\n'.join(line for line in script.splitlines() if line.startswith(('function hookPlain(','function hookTokens(')))
    text='👑 They abandoned the **true heiress**,\n but she returned as the Zombie Queen!'
    command=functions+'\nconsole.log(JSON.stringify(hookTokens('+json.dumps(text)+')));'
    tokens=json.loads(subprocess.check_output(['node','-e',command],text=True))
    clean=semantic.plain_hook(text)
    assert [t['text'] for t in tokens][-2:]==['Zombie','Queen']
    for token in tokens:
        assert clean[token['start']:token['end']]==token['text']


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='requires ffmpeg')
def test_production_render_uses_approved_preview_without_reanalysing(tmp_path,monkeypatch):
    r=request(mode='manual',position='middle',manual_ranges=[{'start':19,'end':31}])
    timeline=[i.model_dump(mode='json') for i in r.timeline]
    work=tmp_path/'preview-work';work.mkdir()
    emphasis=approved_emphasis(r.hook_text,r.hook_options,work)
    overlay,decision=display.presentation(r.hook_text,emphasis,r.hook_options,[],work,work)
    doc={'identity':display.preview_identity(r,timeline,[]),'decision':decision,'sources_complete':True}
    key=json_sha256(doc)
    write_json_atomic(tmp_path/'cache/hook-previews'/f'{key}.json',doc)
    r.hook_options.preview_key=key
    monkeypatch.setattr(renderer,'approved_emphasis',lambda *a:pytest.fail('must use approved analysis'))
    monkeypatch.setattr(renderer,'presentation',lambda *a:pytest.fail('must use approved placement/style'))
    config=load_config();config['render'].update(width=540,height=960,preset='ultrafast')
    result=renderer.render_request(tmp_path,r,timeline,tmp_path/'approved.mp4',config)
    assert result['hook_visual']['preview_approved']
    assert result['hook_visual']['placement']['position']=='middle'
    assert result['hook_visual']['emphasis']==emphasis
