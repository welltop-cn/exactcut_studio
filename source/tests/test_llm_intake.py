from pathlib import Path
import pytest
from exactcut import llm_intake as m
from exactcut.intake import validate_draft

TEMPLATE=Path(__file__).resolve().parents[1]/'templates/标准剪辑需求_模板.xlsx'

def interpreted():
    return m.Interpretation(groups=[m.Group(drama_id='101574',drama_title='Test drama',author='徐明飞',business_date='20260927',rows=[
        m.Material(seq='1',title_cn='',hook_text='',segments=[m.Segment(type='episode',episode='EP5',start_ms=65000,end_ms=79000,text='',duration_ms=0), m.Segment(type='title_card',episode='',start_ms=0,end_ms=None,text='One Hour Earlier',duration_ms=1500),m.Segment(type='episode',episode='EP4',start_ms=0,end_ms=None,text='',duration_ms=0)],
                   notes='过渡字卡建议1.5秒，请确认',source_cells=['剪辑填写!C8','剪辑填写!F8','剪辑填写!I8'])])],warnings=['请确认字卡时长'])

def test_llm_draft_supports_transition_and_preserves_evidence(monkeypatch):
    monkeypatch.setattr(m,'interpret',lambda payload: interpreted())
    draft,warnings=m.read_excel(TEMPLATE)
    assert draft['interpretation_method']=='llm'
    assert '剪辑填写!F8：一小时前' in draft['rows'][0]['original_cells']
    manifest,errors=validate_draft(draft)
    assert not errors
    assert manifest.requests[0].timeline[1].text=='One Hour Earlier'
    assert manifest.requests[0].timeline[1].duration_ms==1500
    assert any('确认' in x for x in warnings)

def test_no_fallback_on_service_failure(monkeypatch):
    def fail(payload): raise ValueError('AI 服务不可用')
    monkeypatch.setattr(m,'interpret',fail)
    with pytest.raises(ValueError,match='AI 服务不可用'): m.read_excel(TEMPLATE)

def test_no_formal_material_is_not_a_production_job(monkeypatch):
    monkeypatch.setattr(m,'interpret',lambda payload:m.Interpretation(groups=[],warnings=['仅示例']))
    with pytest.raises(ValueError,match='未找到正式制作素材'):m.read_excel(TEMPLATE)

def test_invalid_evidence_is_rejected(monkeypatch):
    result=interpreted();result.groups[0].rows[0].source_cells=['不存在!A1']
    monkeypatch.setattr(m,'interpret',lambda payload:result)
    with pytest.raises(ValueError,match='原文引用'):m.read_excel(TEMPLATE)


def test_llm_draft_allows_missing_drama_title(monkeypatch):
    result = interpreted()
    result.groups[0].drama_title = ''
    monkeypatch.setattr(m, 'interpret', lambda payload: result)
    draft, _ = m.read_excel(TEMPLATE)
    # Even if the catalog cannot supply a title, production is still valid.
    draft.pop('drama_title', None)
    manifest, errors = validate_draft(draft)
    assert not errors
    assert manifest.drama_title == ''
