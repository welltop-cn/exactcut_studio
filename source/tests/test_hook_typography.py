from pathlib import Path

import pytest
from PIL import Image

from exactcut.fonts import font_path
from exactcut.hook_options import HookOptions, approved_emphasis
from exactcut.hook_presentation import presentation, preview_identity
from exactcut.hook_visual import render_personalized_hook_png
from exactcut.intake import read_excel, validate_draft
from exactcut.models import CutRequest


@pytest.mark.parametrize('options', [
    {'font': '../private.ttf'}, {'font_size': 31}, {'font_size': 97},
    {'font_size': 48.5}, {'text_color': 'red'}, {'accent_color': '#xyz123'},
])
def test_invalid_typography_is_rejected(options):
    with pytest.raises(ValueError):
        HookOptions(**options)


@pytest.mark.parametrize('font,profile', [('sans','default'), ('sans_italic','neon_reversal'),
                                         ('serif','ceo_luxury'), ('cjk','title_card')])
@pytest.mark.parametrize('width,height', [(1080,1920), (540,960)])
def test_selected_typography_reaches_rendered_pixels(tmp_path,font,profile,width,height):
    text='She returns\nA new life'
    options=HookOptions(mode='manual', style='default', position='top', font=font,
                        font_size=60, text_color='#12AB34', accent_color='#EF2468',
                        manual_ranges=[{'start':4,'end':11}])
    emphasis=approved_emphasis(text,options,tmp_path)
    path,decision=presentation(text,emphasis,options,[],tmp_path,tmp_path,width,height)
    assert decision['font_path']==font_path(profile)
    assert decision['layout']['font_size']==round(60*width/1080)
    assert decision['layout']['lines']==text.splitlines()
    with Image.open(path) as image:
        pixels={color for _,color in image.convert('RGB').getcolors(width*height)}
        assert (18,171,52) in pixels
        assert (239,36,104) in pixels
    before=path.read_bytes()
    assert render_personalized_hook_png(text,tmp_path,decision,video_w=width,video_h=height).read_bytes()==before


def test_custom_size_overflow_is_reported_in_draft_validation():
    draft,_=read_excel(Path(__file__).resolve().parents[1]/'templates/精确剪辑_FBBM_填写模板.xlsx')
    draft['rows'][0].update(hook_text='The truth was finally revealed and she returned to claim her crown after all these years',
                           hook_options={'font':'serif','font_size':96})
    manifest,errors=validate_draft(draft)
    assert manifest is None
    assert any('字号' in e for e in errors)
    draft['rows'][0]['hook_options']['font_size']=32
    assert validate_draft(draft)[1]==[]


def test_typography_changes_invalidate_preview_identity():
    request=CutRequest(request_id='R1',seq=1,output_filename='test.mp4',hook_text='She returns',
                       timeline=[{'type':'title_card','text':'Later','duration_ms':1500}])
    original=preview_identity(request,[],[])
    for field,value in [('font','serif'),('font_size',60),('text_color','#12AB34'),('accent_color','#EF2468')]:
        changed=request.model_copy(deep=True)
        setattr(changed.hook_options,field,value)
        assert preview_identity(changed,[],[])!=original


def test_composition_compilation_keeps_typography():
    from exactcut.intake import compile_composition
    options={'font':'serif','font_size':60,'text_color':'#12AB34','accent_color':'#EF2468'}
    row={'hook_options':options.copy(),'composition':{'opening':{'text':'She returns'},
         'plots':[{'episode':'1','start':'00:00','end':'00:03'}]}}
    compile_composition(row)
    for key,value in options.items():
        assert row['hook_options'][key]==value


def test_composition_preserves_manual_line_breaks(tmp_path):
    from exactcut.intake import compile_composition
    row={'hook_options':{'font_size':60}, 'composition':{'opening':{'text':'First line\r\nSecond line'},
         'plots':[{'episode':'1','start':'00:00','end':'00:03'}]}}
    compile_composition(row)
    assert row['hook_text']=='First line\nSecond line'
    assert row['composition']['opening']['text']==row['hook_text']
    options=HookOptions(**row['hook_options'])
    _,decision=presentation(row['hook_text'],approved_emphasis(row['hook_text'],options,tmp_path),
                            options,[],tmp_path,tmp_path)
    assert decision['layout']['lines']==['First line','Second line']


@pytest.mark.parametrize('size', [32,48])
def test_explicit_small_font_warning_suggests_size_change(tmp_path,size):
    options=HookOptions(mode='none',position='top',font_size=size)
    _,decision=presentation('She returns',approved_emphasis('She returns',options,tmp_path),
                            options,[],tmp_path,tmp_path)
    warning=decision['readability_warning']
    if size==32:
        assert '调大字号' in warning
        assert '精简文案' not in warning
    else:
        assert warning==''
