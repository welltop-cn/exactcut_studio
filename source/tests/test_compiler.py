from pathlib import Path
import pytest
import openpyxl
from exactcut.compiler import parse_standard_segment, STANDARD_TEMPLATE_TITLE
from exactcut.errors import ValidationError

from exactcut.compiler import compile_workbook
from exactcut.util import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_compile_packaged_template():
    manifest = compile_workbook(ROOT / "templates" / "精确剪辑_FBBM_填写模板.xlsx", load_config())
    assert manifest.drama_id == "101460"
    assert manifest.author == "张三"
    assert manifest.requests[0].output_filename == "20260902_typezxraw_张三_agent_1_#101460#.mp4"
    assert [item.type for item in manifest.requests[0].timeline] == ["episode", "title_card"]


@pytest.mark.parametrize('text', ['EP1', 'EP1 0.5s-1.49s', 'EP1 00:60-01:30', 'EP1 01:00', 'EP1 00:01-00:02+00:03-00:04', '字卡|hello', '字卡| |1.5s', '字卡|hello|0s'])
def test_standard_rejects_ambiguous_segments(text):
    with pytest.raises((ValidationError, ValueError)):
        parse_standard_segment(text, 1500)


def test_standard_formats_and_subsecond():
    assert parse_standard_segment('EP1 00:00.500-01:49', 1500).start_ms == 500
    assert parse_standard_segment('EP2 全集', 1500).full_episode
    assert parse_standard_segment('EP5 01:22-片尾', 1500).start_ms == 82000
    assert parse_standard_segment('字卡|One hour earlier|1.5s', 1500).duration_ms == 1500


def test_standard_header_today_and_gap(monkeypatch):
    w = openpyxl.Workbook()
    s = w.active
    s.title = '剪辑填写'
    s['A1'] = STANDARD_TEMPLATE_TITLE
    for cell, value in {'A2':'剧ID', 'B2':'101584', 'C2':'出表人', 'D2':'测试', 'A6':'需求编号', 'B6':'素材编号', 'C6':'片段1', 'D6':'片段2', 'E6':'片段3', 'A7':'R001', 'B7':1, 'C7':'EP1 00:05-01:49', 'D7':'EP2 全集'}.items():
        s[cell] = value
    monkeypatch.setattr('exactcut.compiler.load_workbook', lambda *a, **k: w)
    monkeypatch.setattr('exactcut.compiler.production_date', lambda: '20260925')
    result = compile_workbook(ROOT / 'README.md', load_config())
    assert result.business_date == '20260925'
    assert result.drama_title == ''
    assert result.requests[0].output_filename.startswith('20260925_')
    s['D7'] = None
    s['E7'] = 'EP2 全集'
    with pytest.raises(ValidationError, match='E7'):
        compile_workbook(ROOT / 'README.md', load_config())


def test_v2_intake_ignores_examples_and_refreshes_date(monkeypatch):
    from exactcut.intake import read_excel, validate_draft
    path = ROOT / 'templates' / '标准剪辑需求_模板_v2.xlsx'
    w = openpyxl.load_workbook(path)
    for cell, value in {'B2':'101584', 'D2':'测试', 'A7':'R001', 'B7':1, 'D7':'EP1 00:05-01:49', 'E7':'EP2 全集'}.items():
        w['剪辑填写'][cell] = value
    monkeypatch.setattr('exactcut.compiler.load_workbook', lambda *a, **k: w)
    monkeypatch.setattr('exactcut.intake.load_workbook', lambda *a, **k: w)
    monkeypatch.setattr('exactcut.intake.load_catalog', lambda: {'dramas': []})
    monkeypatch.setattr('exactcut.intake.production_date', lambda: '20260925')
    draft, warnings = read_excel(path)
    assert len(draft['rows']) == 1
    assert draft['business_date'] == '20260925'
    monkeypatch.setattr('exactcut.intake.production_date', lambda: '20260926')
    manifest, errors = validate_draft(draft)
    assert errors == []
    assert manifest.business_date == '20260926'
    draft['rows'][0]['timeline_text'] = 'EP1 0.5s-1.49s'
    manifest, errors = validate_draft(draft)
    assert manifest is None and errors
