"""Read common Excel layouts into an editable, provenance-preserving draft."""
from __future__ import annotations
import re
from datetime import date, datetime
from pathlib import Path
from zipfile import ZipFile
from openpyxl import load_workbook
from .catalog import load_catalog
from .compiler import parse_segment_cell, parse_standard_segment, production_date, STANDARD_TEMPLATE_TITLE, compile_workbook
from .models import BatchManifest, CutRequest
from .naming import build_material_filename
from .timecode import format_ms
from .errors import ValidationError
from .hook_visual import validate_hook_layout

ALIASES = {
    'drama_id': ['剧id', '剧集id', '短剧id', '项目id', 'dramaid', 'novelid', 'seriesid'],
    'drama_title': ['剧名', '剧集名称', '短剧名称', '项目名称', 'dramatitle'],
    'business_date': ['业务日期', '日期', '制作日期', 'businessdate'],
    'author': ['出表人', '制作人', '编导', '编导姓名', '编导名字', '作者', 'creator1', 'author'],
    'creator2': ['creator2'], 'material_type': ['素材类型', 'materialtype'],
    'request_id': ['需求编号', '需求id', '素材id', 'requestid'],
    'seq': ['素材编号', '序号', '编号', 'seq'],
    'title_cn': ['标题', '标题cn', '中文标题', '素材名称', '素材标题'],
    'hook_text': ['标题en/hook', '标题en', 'hook', '英文标题', 'hook文案', '标题短文案'],
    'structure': ['结构类型', '结构'], 'notes': ['备注/授权', '备注', '授权', '说明'],
    'timeline': ['时间线', '剪辑内容', '剪辑片段', '片段', 'timeline', '创意脚本'],
    'episode': ['集数', '剧集', 'episode'], 'start': ['开始时间', '开始', '起点', 'start'],
    'end': ['结束时间', '结束', '终点', 'end'],
}


def norm(value):
    return re.sub(r'[\s_：:]+', '', str(value or '')).lower()


def key_of(value):
    text = norm(value)
    return next((key for key, values in ALIASES.items() if text in values), None)


def text(value):
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y%m%d')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalized_timeline(value, dotted_mode='seconds'):
    lines = []
    chunks = []
    for source_line in text(value).splitlines():
        if re.match(r'^\s*(字卡|插入切|card|title_card)\s*[|：:]', source_line, re.I):
            chunks.append(source_line)
        else:
            source_line = re.sub(r'^\s*正文画面\s*[—–\-：:]*\s*', '', source_line)
            source_line = source_line.lstrip('、，,；; ')
            for chunk in re.split(r'[，,；;]+', source_line):
                match = re.match(r'^\s*(EP\s*\d+)\s*[:：]?\s*(.*)', chunk, re.I)
                if match and '+' in match[2]:
                    for part in match[2].split('+'):
                        chunks.append(f'{match[1]} {part.strip()}' if re.match(r'^\s*\d', part) else part)
                else:
                    chunks.append(chunk)
    for line in chunks:
        line = line.strip()
        if not line:
            continue
        line = re.sub(r'^第\s*(\d+)\s*集', r'EP\1', line)
        line = re.sub(r'(?i)^ep\s*0*(\d+)', r'EP\1', line)
        if re.match(r'^EP\d+', line):
            if dotted_mode == 'minutes_seconds':
                line = re.sub(r'(?<!\d)1\.\.44s', '1.44s', line, flags=re.I)
                if re.search(r'\d+\.\d{3,}s', line, re.I):
                    lines.append(line)
                    continue
            line = re.sub(r'[~～至到]', '-', line)
            line = line.replace('分', 'm').replace('秒', 's').replace('：', ':')
            if re.search(r'\d+\.\d{1,2}s', line, re.I):
                if dotted_mode == 'unconfirmed':
                    lines.append(line)
                    continue
                if dotted_mode == 'minutes_seconds':
                    def minute_second(match):
                        if int(match[2]) >= 60:
                            raise ValueError('点号时间的秒数必须小于 60')
                        return f'{int(match[1]):02d}:{int(match[2]):02d}'
                    try:
                        line = re.sub(r'(\d+)\.(\d{1,2})s', minute_second, line, flags=re.I)
                    except ValueError:
                        lines.append(line)
                        continue
        try:
            item = parse_segment_cell(line, 1500)
            if item.type == 'episode':
                line = f'{item.episode} 全集' if item.full_episode else f'{item.episode} {format_ms(item.start_ms)}-结尾' if item.to_end else f'{item.episode} {format_ms(item.start_ms)}-{format_ms(item.end_ms)}'
            else:
                line = f'字卡|{item.text}|{item.duration_ms / 1000:g}s'
        except (ValueError, RuntimeError):
            pass  # Preserve unrecognized input for human correction; validation blocks execution.
        lines.append(line)
    return '\n'.join(lines)


def conversion_notes(value, director=False):
    notes = []
    if director and re.search(r'(?<!\d)1\.\.44s', value, re.I):
        notes.append('按已确认笔误修正：1..44s → 1.44s → 01:44；原文保留')
    if '+' in value:
        notes.append('加号分隔的同集选段分别保留，未包含两段之间的空档')
    if re.search(r'^\s*[、，,]', value) or re.search(r'正文画面\s*[—–\-：:]*\s*、', value):
        notes.append('清理开头多余分隔符，原文保留')
    if re.search(r'20\d{6}_\d{6}_', value):
        notes.append('含旧素材文件引用，需定位并核对当前剧 ID；暂不支持直接执行')
    return notes


def expand_split_segments(values, sheet_name):
    """Collapse paired material/segment rows without interpreting times as HH:MM:SS."""
    from openpyxl.utils import get_column_letter
    for index, header in enumerate(values[:50]):
        columns = [c for c, v in enumerate(header) if re.fullmatch(r'片段\d+', norm(v))]
        if not columns or index + 1 >= len(values):
            continue
        first = values[index + 1]
        if not any(tuple(text(v) for v in first[c:c+3]) == ('集', '头', '尾') for c in columns):
            continue
        result, originals = list(values), {}
        def fail(row, col, message):
            raise ValueError(f'{sheet_name}!{get_column_letter(col+1)}{row+1}：{message}')
        def timestamp(value, row, col):
            raw = text(value)
            match = re.fullmatch(r'(\d+):([0-5]\d):(\d{1,3})', raw)
            if not match:
                fail(row, col, '时间请填写 分:秒:毫秒，例如 01:52:00；毫秒为 0～999')
            minutes, seconds, millis = map(int, match.groups())
            return format_ms(minutes*60000 + seconds*1000 + millis)
        row = index + 1
        while row < len(values):
            top = values[row]
            marker = text(top[0]) if top else ''
            if re.search(r'不剪辑|不参与剪辑', marker):
                result[row] = tuple(None for _ in top)
                if any(tuple(text(v) for v in top[c:c+3]) == ('集', '头', '尾') for c in columns) and row + 1 < len(values):
                    result[row+1] = tuple(None for _ in values[row+1])
                    row += 2
                else:
                    row += 1
                continue
            if not any(text(v) for v in top):
                row += 1
                continue
            bottom = values[row + 1] if row + 1 < len(values) else ()
            if not any(text(v) for c in columns for v in bottom[c:c+3]):
                result[row] = tuple(None for _ in top)
                if row + 1 < len(values):
                    result[row+1] = tuple(None for _ in bottom)
                row += 2
                continue
            for c in columns:
                if not any(text(v) for v in top[c:c+3]) and not any(text(v) for v in bottom[c:c+3]):
                    continue
                if tuple(text(v) for v in top[c:c+3]) != ('集', '头', '尾'):
                    fail(row, c, '每条素材须包含“集、头、尾”表头及下一行片段内容')
            if row + 1 >= len(values):
                fail(row, columns[0], '缺少下一行片段内容')
            bottom = values[row + 1]
            merged = list(top)
            empty_seen = False
            for c in columns:
                ep, start, end = [text(v) for v in bottom[c:c+3]]
                if not ep and not start and not end:
                    empty_seen = True
                    merged[c:c+3] = [None]*3
                    continue
                if empty_seen:
                    fail(row+1, c, '片段之间不能留空组')
                if not re.fullmatch(r'EP[1-9]\d{0,2}', ep):
                    fail(row+1, c, '集数请填写 EP1～EP500')
                a = timestamp(start, row+1, c+1) if start else '00:00.000'
                b = timestamp(end, row+1, c+2) if end else '片尾'
                segment = f'{ep} 全集' if not start and not end else f'{ep} {a}-{b}'
                try:
                    parse_standard_segment(segment, 1500)
                except (ValueError, ValidationError) as exc:
                    fail(row+1, c, str(exc))
                merged[c:c+3] = [segment, None, None]
            if all(not merged[c] for c in columns):
                fail(row+1, columns[0], '请填写至少一个片段')
            # Keep row numbers stable for original-cell provenance and error reports.
            result[row] = tuple(merged)
            result[row+1] = tuple(None for _ in bottom)
            originals[row] = [text(v) for v in top] + [text(v) for v in bottom]
            row += 2
        return result, originals
    return values, {}


def read_excel(path: Path, *, grouped=False):
    with ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist()) > 50_000_000:
            raise ValueError('Excel 解压后超过 50 MB，请拆分文件')
    book = load_workbook(path, read_only=True, data_only=False)
    standard_v2 = any(sheet.title == '剪辑填写' and text(sheet['A1'].value) == STANDARD_TEMPLATE_TITLE for sheet in book.worksheets)
    warnings, rows, metas = [], [], {}
    ambiguous_dotted = False
    try:
        if standard_v2:
            compile_workbook(path, {'render': {'title_card_duration_ms': 1500}})
        for sheet in book.worksheets:
            if standard_v2 and sheet.title != '剪辑填写':
                continue
            if sheet.max_row is None or sheet.max_column is None:
                sheet.calculate_dimension(force=True)
            if sheet.max_row > 5000 or sheet.max_column > 200:
                raise ValueError(f'{sheet.title} 表格过大，请限制在 5000 行、200 列内')
            values, split_originals = expand_split_segments(list(sheet.iter_rows(values_only=True)), sheet.title)
            sheet_standard = standard_v2 or bool(split_originals)
            header = None
            for idx, row in enumerate(values[:50]):
                fields = {col: key_of(value) for col, value in enumerate(row) if key_of(value)}
                segments = [col for col, value in enumerate(row) if re.fullmatch(r'(片段|segment)\d+', norm(value))]
                if segments or 'timeline' in fields.values() or {'episode', 'start', 'end'} <= set(fields.values()):
                    header = (idx, fields, segments)
                    break
            if header is None:
                continue
            index, fields, segments = header
            creative_layout = any(norm(value) == '创意脚本' for value in values[index])
            sheet_meta = {}
            for row in values[:index]:
                for col, value in enumerate(row):
                    key = key_of(value)
                    if key in ('drama_id', 'drama_title', 'business_date', 'author', 'creator2', 'material_type') and col + 1 < len(row):
                        sheet_meta[key] = text(row[col + 1])
            for rownum, row in enumerate(values[index + 1:], index + 2):
                if not any(v is not None and text(v) for v in row):
                    continue
                fields_data = {key: text(row[col]) if col < len(row) else '' for col, key in fields.items()}
                segment_values = [text(row[col]) for col in segments if col < len(row) and text(row[col])]
                if not any(fields_data.values()) and not segment_values and all(not text(v) or text(v).startswith('=') for v in row):
                    continue  # Empty template rows containing only helper formulas.
                if fields_data.get('timeline'):
                    segment_values.append(fields_data['timeline'])
                if fields_data.get('episode') and fields_data.get('start') and fields_data.get('end'):
                    ep = fields_data['episode']
                    if ep.isdigit():
                        ep = 'EP' + ep
                    segment_values.append(f"{ep} {fields_data['start']}-{fields_data['end']}")
                row_meta = {}
                for key in ('drama_id', 'drama_title', 'business_date', 'author', 'creator2', 'material_type'):
                    value = fields_data.get(key) or sheet_meta.get(key)
                    if value:
                        if key == 'drama_id':
                            value = value.strip('#')
                        metas.setdefault(key, set()).add(value)
                        row_meta[key] = value
                # Never drop a populated row silently; empty/unrecognized timelines are editable errors.
                raw_timeline = '\n'.join(segment_values)
                ambiguous_dotted |= creative_layout and bool(re.search(r'\d+\.\d{1,2}s', raw_timeline, re.I))
                rows.append({
                    'request_id': fields_data.get('request_id') or f'R{len(rows)+1:03}',
                    'seq': fields_data.get('seq') or str(len(rows)+1),
                    'title_cn': fields_data.get('title_cn', ''), 'hook_text': fields_data.get('hook_text', ''),
                    'structure': fields_data.get('structure', ''), 'notes': fields_data.get('notes', ''),
                    'timeline_text': raw_timeline if sheet_standard else normalized_timeline(raw_timeline, 'minutes_seconds' if creative_layout else 'seconds'),
                    'source': f'{sheet.title} · 第 {rownum} 行',
                    'original_cells': split_originals.get(rownum-1, [text(v) for v in row]),
                    'original_script': raw_timeline, 'source_metadata': row_meta,
                    'input_format': 'standard_v2' if sheet_standard else ('director' if creative_layout else 'standard'),
                    'conversion_notes': conversion_notes(raw_timeline, creative_layout),
                })
        if not rows:
            raise ValueError('未找到可制作的素材。标注“不剪辑／不参与剪辑”的示例和空白行不会导入；请在正式素材区域填写集数与剪辑内容后重新上传。支持片段列、时间线或集数/开始时间/结束时间表头。')
        if len(rows) > 200:
            raise ValueError('单批最多 200 条素材，请拆分 Excel')
        if grouped:
            groups = {}
            for row in rows:
                groups.setdefault(row['source_metadata'].get('drama_id', ''), []).append(row)
            results = []
            for group_rows in groups.values():
                group_meta = {}
                for row in group_rows:
                    for key, value in row['source_metadata'].items():
                        group_meta.setdefault(key, set()).add(value)
                results.append(finish_draft(group_rows, group_meta, any(r['input_format']=='director' for r in group_rows)))
            return results
        if len(metas.get('drama_id', set())) > 1:
            raise ValueError('检测到多个剧 ID，请按剧拆分为独立 Excel 后上传')
        return finish_draft(rows, metas, ambiguous_dotted)
    finally:
        book.close()


def finish_draft(rows, metas, ambiguous_dotted):
    warnings = []
    for key, values in metas.items():
        if len(values) > 1:
            warnings.append(f'{key} 有多个值，暂取首个，请确认：' + ' / '.join(sorted(values)))
    draft = {key: sorted(values)[0] for key, values in metas.items()}
    if rows and all(r.get('input_format') == 'standard_v2' for r in rows):
        draft['standard_template_version'] = 2
        draft['business_date'] = production_date()
    if ambiguous_dotted:
        draft['time_notation'] = 'minutes_seconds'
        warnings.append('按已确认写法整理：1.52s 表示 01:52；单独集号表示全集；单个时间点表示从该点剪到结尾。Excel 原文可展开核对。')
    draft.setdefault('drama_id', '')
    if not draft['drama_id']:
        warnings.append('未识别到剧 ID，请在标准化表中补充')
    catalog = {r['id']: r for r in load_catalog()['dramas']}
    known = catalog.get(draft['drama_id'])
    if known and not draft.get('drama_title'):
        draft['drama_title'] = known['title']
    elif known and draft.get('drama_title') != known['title']:
        warnings.append(f"Excel 剧名与候选不同，保留原文。候选剧名：{known['title']}")
    if not draft.get('business_date'):
        draft['business_date'] = ''
        warnings.append('业务日期缺失，已留空，请补充')
    draft.setdefault('author', '')
    draft.setdefault('drama_title', '')
    draft.setdefault('creator2', 'agent')
    draft.setdefault('material_type', 'typezxraw')
    draft['rows'] = rows
    return draft, warnings


def validate_draft(draft, source=''):
    errors, requests = [], []
    drama_id = str(draft.get('drama_id', '')).strip().strip('#')
    if not re.fullmatch(r'\d{6}', drama_id) or not 101050 <= int(drama_id) <= 999999:
        errors.append('剧 ID 必须为 101050～999999 范围内的六位数字')
    standard_v2 = draft.get('standard_template_version') == 2
    business_date = production_date() if standard_v2 else str(draft.get('business_date', '')).replace('-', '').replace('/', '')
    try:
        datetime.strptime(business_date, '%Y%m%d')
    except ValueError:
        errors.append('业务日期必须为真实日期 YYYYMMDD')
    if not str(draft.get('author', '')).strip():
        errors.append('编导姓名不能为空')
    rows = draft.get('rows', [])
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        return None, errors + ['素材数量必须为 1～200 条']
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f'素材 {i}：内容必须是字段对象')
            continue
        try:
            request_id = str(row.get('request_id', '')).strip()
            if not re.fullmatch(r'[\w\-]{1,80}', request_id):
                raise ValueError('需求编号只能包含文字、数字、下划线或连字符，最多 80 字')
            timeline = []
            raw_timeline = str(row.get('timeline_text', ''))
            mode = draft.get('time_notation', 'seconds')
            if mode == 'unconfirmed' and re.search(r'\d+\.\d{1,2}s', raw_timeline, re.I):
                raise ValueError('请先选择点号时间的含义（分.秒或小数秒）')
            if mode == 'minutes_seconds' and any(int(m[1]) >= 60 for m in re.finditer(r'\d+\.(\d{1,2})s', raw_timeline, re.I)):
                raise ValueError('分秒写法的秒位不能达到 60，请核对原文')
            if mode == 'minutes_seconds' and re.search(r'\d+\.\d{3,}s', raw_timeline, re.I):
                raise ValueError('点号后超过两位的时间写法尚未确认，请使用明确的分:秒格式')
            for line in (raw_timeline if standard_v2 else normalized_timeline(raw_timeline, mode)).splitlines():
                if re.search(r'20\d{6}_\d{6}_', line):
                    raise ValueError('旧素材文件引用待定位并核对剧 ID，暂不支持执行：' + line)
                if not re.match(r'EP\d+|(?:字卡|插入切|card|title_card)\s*[|：:]', line, re.I):
                    raise ValueError('未确定的文字或转场，请确认呈现方式、语言和时长：' + line)
                item = (parse_standard_segment if standard_v2 else parse_segment_cell)(line, 1500)
                if item:
                    if item.type == 'episode' and (not re.fullmatch(r'EP[1-9]\d{0,2}', item.episode) or int(item.episode[2:]) > 500 or item.start_ms < 0):
                        raise ValueError('集数需为 EP1～EP500，起点不能为负数')
                    timeline.append(item)
            if not timeline:
                raise ValueError('请填写至少一个有效片段')
            validate_hook_layout(str(row.get('hook_text', '')))
            seq = int(row.get('seq', i))
            if not str(draft.get('author', '')).strip():
                continue  # Report missing director once, while still checking every timeline.
            filename = build_material_filename(business_date, str(draft.get('material_type') or 'typezxraw'), str(draft.get('author', '')), 'agent', seq, drama_id)
            requests.append(CutRequest(request_id=request_id, seq=seq, output_filename=filename, timeline=timeline,
                                       hook_options=row.get('hook_options') or {},
                                       **{k: str(row.get(k, '')) for k in ('title_cn', 'hook_text', 'structure', 'notes')}))
        except (ValueError, TypeError, RuntimeError) as exc:
            errors.append(f'素材 {i}：{exc}')
    if errors:
        return None, errors
    try:
        manifest = BatchManifest(drama_id=drama_id, drama_title=draft.get('drama_title', ''), business_date=business_date,
                                 author=draft['author'], creator2='agent',
                                 material_type=draft.get('material_type') or 'typezxraw', source_workbook=source, requests=requests)
        return manifest, []
    except ValueError as exc:
        return None, [str(exc)]


def timing_explanation(manifest, project_dir=None, *, wait=True):
    """Explain the parsed timeline without guessing unknown episode endings."""
    if manifest is None:
        return []
    result = []
    for request in manifest.requests:
        timeline=[item.model_dump(mode='json') for item in request.timeline]
        states=[{} for item in timeline]
        if project_dir is not None:
            from .timing_review import local_timeline
            timeline,states=local_timeline(project_dir,timeline,wait=wait)
        segments, known, pending = [], 0, 0
        for index, (item,state) in enumerate(zip(timeline,states), 1):
            duration = (item['end_ms'] - item['start_ms'] if item['end_ms'] is not None else None) if item['type'] == 'episode' else item['duration_ms']
            if state.get('error'):duration=None
            if duration is None:
                pending += 1
            else:
                known += duration
            segments.append({'segment': index, 'type': item['type'], 'episode': item['episode'],
                             'start_ms': item['start_ms'], 'end_ms': item['end_ms'],
                             'text': item['text'], 'duration_ms': duration, **state})
        result.append({'request_id': request.request_id, 'segments': segments,
                       'known_duration_ms': known, 'pending_endings': pending,
                       'planned_duration_ms': None if pending else known})
    return result


def download_plan(manifest):
    required = sorted({int(t.episode[2:]) for r in manifest.requests for t in r.timeline if t.type == 'episode'})
    limit = max([10, *required])
    return {'required': [f'EP{n}' for n in required], 'preferred': [f'EP{n}' for n in range(1, 11)],
            'episode_limit': limit, 'extra': [f'EP{n}' for n in required if n > 10]}
