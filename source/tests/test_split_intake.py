from pathlib import Path
import openpyxl
import pytest
from exactcut.intake import read_excel, validate_draft


def workbook(tmp_path):
    w = openpyxl.Workbook()
    s = w.active
    s.title = '剪辑填写'
    s.append(['标准剪辑需求表'])
    s.append(['剧ID', '101573', '出表人', '徐明飞'])
    s.append(['素材编号', '标题EN/Hook', '片段1', None, None, '片段2', None, None])
    s.append([1, 'Hook', '集', '头', '尾', '集', '头', '尾'])
    s.append([None, None, 'EP2', '00:01:125', '00:08:00', 'EP4', None, None])
    s.append([2, None, '集', '头', '尾', '集', '头', '尾'])
    s.append([None, None, 'EP5', '01:22:00', None, 'EP1', None, '00:15:00'])
    p = tmp_path / 'split.xlsx'
    w.save(p)
    return w, s, p


def test_split_ranges_full_tail_and_provenance(tmp_path):
    w, s, p = workbook(tmp_path)
    d, _ = read_excel(p)
    assert len(d['rows']) == 2
    assert d['rows'][0]['hook_text'] == 'Hook'
    assert '00:01:125' in d['rows'][0]['original_cells']
    manifest, errors = validate_draft(d)
    assert not errors
    a, b = manifest.requests
    assert a.timeline[0].start_ms == 1125
    assert a.timeline[0].end_ms == 8000
    assert a.timeline[1].full_episode
    assert b.timeline[0].start_ms == 82000
    assert b.timeline[0].end_ms is None
    assert b.timeline[1].start_ms == 0
    assert b.timeline[1].end_ms == 15000
    assert len(read_excel(p, grouped=True)) == 1


@pytest.mark.parametrize('cell,value,location', [('D5','00:60:00','D5'),('D5','1.52s','D5'),('C5',None,'C5'),('E5','00:00:00','C5')])
def test_split_reports_bad_cell(tmp_path, cell, value, location):
    w, s, p = workbook(tmp_path)
    s[cell] = value
    w.save(p)
    with pytest.raises(ValueError, match=location):
        read_excel(p)


def test_published_template_skips_demo_and_empty_slots(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'templates/标准剪辑需求_模板.xlsx'
    with pytest.raises(ValueError, match='未找到可制作的素材'):
        read_excel(source)
    w = openpyxl.load_workbook(source)
    s = w['剪辑填写']
    s['C11'] = 'EP1'
    s['D11'] = '00:01:00'
    s['E11'] = '00:08:00'
    p = tmp_path / 'filled.xlsx'
    w.save(p)
    draft, _ = read_excel(p)
    assert len(draft['rows']) == 1
    manifest, errors = validate_draft(draft)
    assert not errors
    assert manifest.requests[0].timeline[0].start_ms == 1000
    assert manifest.requests[0].timeline[0].end_ms == 8000
