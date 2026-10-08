from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from .errors import ValidationError
from .models import BatchManifest, CutRequest, TimelineItem
from .naming import build_material_filename, normalize_type
from .timecode import parse_range, parse_timepoint
from .util import write_json_atomic


HEADER_ALIASES = {
    "request_id": {"需求编号", "request_id", "需求id"},
    "title_cn": {"标题", "标题cn", "中文标题"},
    "hook_text": {"标题en/hook", "标题en", "hook", "英文标题"},
    "structure": {"结构类型", "结构"},
    "notes": {"备注/授权", "备注", "授权"},
    "seq": {"素材编号", "序号", "seq"},
}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _meta_value(sheet: Any, label: str) -> Any:
    target = _norm(label)
    for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 8)):
        for index, cell in enumerate(row):
            if _norm(cell.value) == target:
                return row[index + 1].value if index + 1 < len(row) else None
    return None


def _date8(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip().replace("-", "").replace("/", "")
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 8 or not text.isdigit():
        raise ValidationError("业务日期必须填写为 YYYYMMDD 或 Excel 日期")
    return text


EPISODE_RE = re.compile(r"^(?P<episode>EP\s*\d+)\s*[:：]?\s*(?P<range>.+)$", re.I | re.S)
STANDARD_TEMPLATE_TITLE = "标准剪辑需求表 v2"
STANDARD_TIME = r"\d{2,}:[0-5]\d(?:\.\d{3})?"


def production_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def parse_standard_segment(value: Any, default_card_ms: int) -> TimelineItem | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    episode = rf"EP[1-9]\d* (?:全集|{STANDARD_TIME}-(?:{STANDARD_TIME}|片尾))"
    card = r"字卡\|[^|\r\n]+\|(?:0|[1-9]\d*)(?:\.\d{1,3})?s"
    if not (re.fullmatch(episode, text) or re.fullmatch(card, text)):
        raise ValidationError("片段须使用标准格式：EP1 00:05-01:49、EP2 全集、EP5 01:22-片尾或字卡|文案|1.5s；每格只填一段")
    if text.startswith("字卡|") and not text.split("|")[1].strip():
        raise ValidationError("字卡文案不能为空")
    return parse_segment_cell(text, default_card_ms)


def parse_segment_cell(value: Any, default_card_ms: int) -> TimelineItem | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    whole = re.fullmatch(r'EP\s*(\d+)\s*(?:[：:]\s*)?(?:全集|整集)?', text, re.I)
    if whole:
        return TimelineItem(type='episode', episode=f'EP{int(whole[1])}', full_episode=True)
    tail = re.fullmatch(r'EP\s*(\d+)\s*[:：]?\s*([\d.:hms]+)\s*(?:[-~～至到]\s*(?:结尾|片尾|结束))?', text, re.I)
    if tail:
        return TimelineItem(type='episode', episode=f'EP{int(tail[1])}', start_ms=parse_timepoint(tail[2]), to_end=True)
    episode_match = EPISODE_RE.match(text.replace("\n", " "))
    if episode_match:
        episode = episode_match.group("episode").replace(" ", "").upper()
        start_ms, end_ms = parse_range(episode_match.group("range"))
        return TimelineItem(type="episode", episode=episode, start_ms=start_ms, end_ms=end_ms)

    card_text = text
    duration_ms = default_card_ms
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if parts[0].lower() in {"card", "title_card", "字卡", "插入切"}:
            if len(parts) < 2 or not parts[1]:
                raise ValidationError(f"字卡缺少文案：{text}")
            card_text = parts[1]
            if len(parts) >= 3 and parts[2]:
                duration_ms = parse_timepoint(parts[2])
    elif text.lower().startswith(("字卡", "插入切", "card")):
        lines = [line.strip() for line in re.split(r"[\n：:]", text) if line.strip()]
        card_text = " ".join(lines[1:]) if len(lines) > 1 else "just a few minutes ago"
    else:
        raise ValidationError(f"无法识别片段：{text}")
    return TimelineItem(type="title_card", text=card_text, duration_ms=duration_ms)


def _header_map(sheet: Any) -> tuple[int, dict[str, int], list[int]]:
    for row_index in range(1, min(sheet.max_row, 20) + 1):
        values = [_norm(sheet.cell(row_index, col).value) for col in range(1, sheet.max_column + 1)]
        if _norm("需求编号") not in values:
            continue
        mapping: dict[str, int] = {}
        segments: list[int] = []
        for col, normalized in enumerate(values, 1):
            for key, aliases in HEADER_ALIASES.items():
                if normalized in {_norm(item) for item in aliases}:
                    mapping[key] = col
            if re.fullmatch(r"片段\d+", normalized):
                segments.append(col)
        if "request_id" not in mapping or not segments:
            raise ValidationError("表头至少需要需求编号和片段1")
        return row_index, mapping, segments
    raise ValidationError("未找到剪辑填写表头")


def compile_workbook(workbook_path: Path, config: dict[str, Any], output_path: Path | None = None) -> BatchManifest:
    workbook_path = workbook_path.expanduser().resolve()
    if not workbook_path.exists():
        raise ValidationError(f"表格不存在：{workbook_path}")
    workbook = load_workbook(workbook_path, data_only=False)
    sheet = workbook["剪辑填写"] if "剪辑填写" in workbook.sheetnames else workbook.active
    standard = sheet["A1"].value == STANDARD_TEMPLATE_TITLE

    drama_id = str(_meta_value(sheet, "剧ID") or "").strip().strip("#")
    drama_title = str(_meta_value(sheet, "剧名") or "").strip()
    business_date = production_date() if standard else _date8(_meta_value(sheet, "业务日期"))
    author = str(_meta_value(sheet, "出表人") or "").strip()
    material_type = normalize_type(str(_meta_value(sheet, "素材类型") or "typezxraw"))
    creator2 = str(_meta_value(sheet, "Creator 2") or "agent").strip()
    if creator2.lower() == 'agent':
        creator2 = 'agent'
    if standard and (not re.fullmatch(r"\d{6}", drama_id) or not author):
        raise ValidationError("剧ID须为6位数字，出表人必填")
    if not standard and (not drama_id or not drama_title or not author):
        raise ValidationError("剧ID、剧名、出表人均为必填项")

    header_row, mapping, segment_columns = _header_map(sheet)
    if standard and [str(sheet.cell(header_row, c).value) for c in segment_columns] != [f"片段{i}" for i in range(1, len(segment_columns) + 1)]:
        raise ValidationError("片段列必须从片段1开始连续编号并按顺序排列")
    requests: list[CutRequest] = []
    default_card_ms = int(config["render"]["title_card_duration_ms"])
    next_seq = 1
    for row in range(header_row + 1, sheet.max_row + 1):
        request_id = str(sheet.cell(row, mapping["request_id"]).value or "").strip()
        if not request_id:
            if standard and any(sheet.cell(row, c).value is not None for c in set(mapping.values()) | set(segment_columns)):
                raise ValidationError(f"第{row}行缺少需求编号")
            continue
        timeline = []
        blank_seen = False
        for col in segment_columns:
            cell = sheet.cell(row, col)
            empty = cell.value is None or not str(cell.value).strip()
            if standard and not empty and blank_seen:
                raise ValidationError(f"{cell.coordinate}：片段须从片段1连续填写，中间不能留空")
            blank_seen = blank_seen or empty
            try:
                item = (parse_standard_segment if standard else parse_segment_cell)(cell.value, default_card_ms)
            except ValueError as exc:
                raise ValidationError(f"{cell.coordinate}：{exc}") from exc
            except ValidationError as exc:
                raise ValidationError(f"{cell.coordinate}：{exc}") from exc
            if item is not None:
                timeline.append(item)
        if standard and not timeline:
            raise ValidationError(f"第{row}行缺少片段1")
        raw_seq = sheet.cell(row, mapping["seq"]).value if "seq" in mapping else None
        if standard and (isinstance(raw_seq, bool) or not re.fullmatch(r"[1-9]\d*", str(raw_seq or ""))):
            raise ValidationError(f"第{row}行素材编号须为正整数")
        if raw_seq is None or str(raw_seq).strip() == "":
            seq = next_seq
        else:
            match = re.search(r"\d+", str(raw_seq))
            if not match:
                raise ValidationError(f"{request_id} 素材编号必须包含数字")
            seq = int(match.group())
        next_seq = max(next_seq + 1, seq + 1)
        output_filename = build_material_filename(business_date, material_type, author, creator2, seq, drama_id)
        requests.append(CutRequest(
            request_id=request_id,
            title_cn=str(sheet.cell(row, mapping.get("title_cn", 0)).value or "") if mapping.get("title_cn") else "",
            hook_text=str(sheet.cell(row, mapping.get("hook_text", 0)).value or "") if mapping.get("hook_text") else "",
            structure=str(sheet.cell(row, mapping.get("structure", 0)).value or "顺叙") if mapping.get("structure") else "顺叙",
            notes=str(sheet.cell(row, mapping.get("notes", 0)).value or "") if mapping.get("notes") else "",
            seq=seq,
            output_filename=output_filename,
            timeline=timeline,
        ))

    manifest = BatchManifest(
        drama_id=drama_id,
        drama_title=drama_title,
        business_date=business_date,
        author=author,
        creator2=creator2,
        material_type=material_type,
        source_workbook=str(workbook_path),
        requests=requests,
    )
    if output_path:
        write_json_atomic(output_path, manifest.model_dump(mode="json"))
    return manifest
