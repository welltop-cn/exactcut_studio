from pathlib import Path
from exactcut.intake import read_excel, validate_draft
from exactcut.hook_visual import validate_hook_layout
from exactcut.hook_visual import AUTO_STYLE_IDS, choose_hook_visual, render_personalized_hook_png
import pytest

TEMPLATE=Path(__file__).resolve().parents[1]/'templates/精确剪辑_FBBM_填写模板.xlsx'

def test_long_hook_is_blocked_before_any_task_or_render():
    draft,_=read_excel(TEMPLATE)
    original='word '*150
    draft['rows'][0]['hook_text']=original
    manifest, errors=validate_draft(draft)
    assert manifest is None
    assert any('素材 1' in e and 'Hook 文案过长' in e for e in errors)
    assert draft['rows'][0]['hook_text']==original
    draft['rows'][0]['hook_text']='She married her enemy.\nHe knows her secret.'
    assert validate_draft(draft)[1]==[]
    validate_hook_layout('')


@pytest.mark.parametrize('copy', [
    "Cast Out of the Shelter by My Birth Parents, \nI Answered My Adoptive Mother's Call\nand Became the Zombie Queen!",
    'The Scheming Woman Completely Panicked!\nThe True Heiress Not Only Survived，\nShe Rose Again as the Zombie Queen!',
    'The love bestowed by her adoptive mother transformed\nthe Zombie Queen into a princess!',
    'With blind, boundless maternal love, her\nadoptive mother raises the Zombie Queen\nlike a little princess!',
])
def test_historical_101597_hooks_reflow_without_losing_copy(tmp_path, copy):
    validate_hook_layout(copy)
    for style in AUTO_STYLE_IDS | {'default'}:
        decision=choose_hook_visual({'hook_text':copy},{},forced_style=style)
        render_personalized_hook_png(copy,tmp_path,decision)
        lines=decision['layout']['lines']
        assert len(lines)<=3
        assert ' '.join(lines)==' '.join(copy.split())


@pytest.mark.parametrize('size', [(1080, 1920), (540, 960)])
@pytest.mark.parametrize('style', sorted(AUTO_STYLE_IDS | {'default'}))
def test_three_line_hook_keeps_all_text_and_decorations_inside_safe_zone(tmp_path, size, style):
    from PIL import Image
    copy = 'The truth was finally revealed\nShe survived the darkest night\nAnd returned to claim her crown'
    decision = choose_hook_visual({'hook_text': copy}, {}, forced_style=style)
    # Remove only the backing to measure all real text, shadow and glow pixels.
    decision['frame_profile'] = {'scrim_alpha': 0}
    output = render_personalized_hook_png(copy, tmp_path, decision, video_w=size[0], video_h=size[1])
    layout = decision['layout']
    assert layout['lines'] == copy.splitlines()
    assert layout['max_lines'] == 3
    assert layout['font_size'] >= round(40 * size[0] / 1080)
    with Image.open(output) as image:
        left, top, right, bottom = image.getbbox()
        assert left >= int(size[0] * 0.07)
        assert right <= size[0] - int(size[0] * 0.07)
        assert top > 0
        assert bottom <= int(size[1] * 0.07) + int(size[1] * 0.18)
        assert bottom - top <= int(size[1] * 0.18)


def test_unbroken_overlong_word_is_rejected_instead_of_clipped(tmp_path):
    from exactcut.errors import ValidationError
    decision = choose_hook_visual({'hook_text': 'W' * 150}, {}, forced_style='default')
    with pytest.raises(ValidationError, match='三行'):
        render_personalized_hook_png('W' * 150, tmp_path, decision)
