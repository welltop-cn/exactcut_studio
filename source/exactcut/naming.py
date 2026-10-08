from __future__ import annotations

import re

from .errors import ValidationError


MATERIAL_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<type>type[A-Za-z0-9]+)_(?P<creator1>[^_]+)_(?P<creator2>[^_]+)_(?P<seq>\d+)_#(?P<novel_id>\d+)#\.mp4$"
)


def clean_token(value: str, label: str) -> str:
    token = re.sub(r"\s+", "-", str(value).strip())
    if not token:
        raise ValidationError(f"{label}不能为空")
    if "_" in token or "/" in token or "\\" in token:
        raise ValidationError(f"{label}不能包含下划线或路径符号：{value}")
    return token


def normalize_type(value: str) -> str:
    token = clean_token(value, "素材类型")
    if not token.startswith("type"):
        token = f"type{token}"
    if not re.fullmatch(r"type[A-Za-z0-9]+", token):
        raise ValidationError("素材类型必须是 type 加字母或数字，例如 typezxraw")
    return token


def build_material_filename(
    business_date: str,
    material_type: str,
    author: str,
    creator2: str,
    seq: int,
    drama_id: str,
) -> str:
    if str(creator2).strip().lower() == 'agent':
        creator2 = 'agent'
    filename = (
        f"{business_date}_{normalize_type(material_type)}_{clean_token(author, '编导姓名')}_"
        f"{clean_token(creator2, 'Creator 2')}_{int(seq)}_#{str(drama_id).strip('#')}#.mp4"
    )
    validate_material_filename(filename)
    return filename


def validate_material_filename(filename: str) -> dict[str, str]:
    match = MATERIAL_RE.fullmatch(filename)
    if not match:
        raise ValidationError(
            "文件名不符合 {date}_type{X}_{编导姓名}_{creator2}_{seq}_#{novel_id}#.mp4："
            f"{filename}"
        )
    return match.groupdict()
