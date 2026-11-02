"""Business validation copy never exposes Pydantic internals to editors."""
import pytest
from exactcut.intake import validate_draft


def ending_draft(ending):
    return {'drama_id': '101687', 'author': 'test', 'business_date': '20261030',
            'rows': [{'request_id': 'R2', 'seq': 2, 'timeline_text': 'EP1 00:00-00:01', 'ending': ending}]}


@pytest.mark.parametrize('ending,message,field', [
    ({'mode': 'image', 'image': '', 'duration_ms': 3000}, '请上传结尾图片，或将结尾画面改为“不添加”', 'image'),
    ({'mode': 'last_frame', 'duration_ms': 999}, '结尾停留时长请填写 1～15 秒', 'duration_ms'),
    ({'mode': 'last_frame', 'duration_ms': 15001}, '结尾停留时长请填写 1～15 秒', 'duration_ms'),
    ({'mode': 'last_frame', 'guide': '字' * 201}, '结尾引导文案最多 200 字，请缩短文案', 'guide'),
    ({'mode': 'bad'}, '请选择有效的结尾画面', 'mode'),
])
def test_ending_validation_is_actionable_and_located(ending, message, field):
    issues = []
    manifest, errors = validate_draft(ending_draft(ending), validation_issues=issues)
    assert manifest is None
    assert errors == ['素材 2 · 结尾画面：' + message]
    assert issues[0]['section'] == 'ending'
    assert issues[0]['fields'] == [field]
    assert issues[0]['row_index'] == 0
    assert not any(token in errors[0] for token in ['CutRequest', 'validation error', 'input_value', 'pydantic', 'Value error'])


def test_other_model_errors_are_plain_chinese_without_input_values():
    draft = ending_draft({'mode': 'none'})
    draft['rows'][0]['hook_options'] = {'font_size': 1}
    _, errors = validate_draft(draft)
    assert errors
    assert '字号' in errors[0]
    assert not any(token in errors[0] for token in ['HookOptions', 'validation error', 'input_value', 'https://'])


def test_composition_ending_uses_the_same_friendly_message():
    draft = ending_draft({})
    row = draft['rows'][0]
    del row['ending']
    row['composition'] = {'opening': {}, 'plots': [{'episode': '1', 'start': '00:00', 'end': '00:01'}],
                          'ending': {'mode': 'image', 'image': ''}}
    _, errors = validate_draft(draft)
    assert errors == ['成片 2 · 结尾画面：请上传结尾图片，或将结尾画面改为“不添加”']
