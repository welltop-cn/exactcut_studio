"""User-approved Hook presentation; compatible with schema-1 manifests."""
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from .errors import ValidationError
from .hook_emphasis import plain_hook, manual_emphasis, resolve_emphasis
from .runtime import environment, feature_flags


class HookRange(BaseModel):
    start: int = Field(ge=0, le=20000)
    end: int = Field(gt=0, le=20000)

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('重点结束位置必须晚于开始位置')
        return self


class HookOptions(BaseModel):
    mode: Literal['ai', 'manual', 'none'] = 'ai'
    style: Literal['auto','default','neon_reversal','danger_warning','ceo_luxury','emotional_diary','cold_mystery','raw_conflict'] = 'auto'
    position: Literal['auto','top','middle','bottom'] = 'auto'
    duration_ms: int | None = Field(default=None, ge=1000, le=12000)
    manual_ranges: list[HookRange] = Field(default_factory=list, max_length=4)
    preview_key: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    font: Literal['auto', 'sans', 'sans_italic', 'serif', 'cjk'] = 'auto'
    text_color: str | None = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')
    accent_color: str | None = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')
    # Pixels at 1080-wide output, scaled proportionally for smaller renders.
    font_size: int | None = Field(default=None, ge=32, le=96, strict=True)


def approved_emphasis(text: str, options: HookOptions, cache_dir: Path) -> dict:
    clean = plain_hook(text)
    if options.mode == 'none':
        return {'version':1,'source':'user_none','text':clean,'highlights':[], 'reason':'已选择不做局部强调','style':'default'}
    if options.mode == 'manual':
        spans = []
        for selected in sorted(options.manual_ranges, key=lambda r:r.start):
            if selected.end > len(clean) or (spans and selected.start < spans[-1]['end']):
                raise ValidationError('手工重点已与文案不一致，请重新选词')
            phrase = clean[selected.start:selected.end]
            if not phrase.strip() or phrase != phrase.strip():
                raise ValidationError('重点不能只包含空格，请重新选词')
            spans.append({'start':selected.start,'end':selected.end,'phrase':phrase,'role':'manual'})
        return {'version':1,'source':'manual','text':clean,'highlights':spans,'reason':'采用已确认的手工选词','style':'default'}
    marked = manual_emphasis(text)
    if marked:
        return marked
    if environment() == 'production' or (environment() == 'development' and feature_flags()['ai']):
        return resolve_emphasis(text, cache_dir)
    return {'version':1,'source':'isolated_disabled','text':clean,'highlights':[],
            'reason':'隔离环境未调用 AI；可用手工选词测试排版','style':'default'}


def hook_duration(text: str, options: HookOptions, total_ms: int | None = None) -> int:
    clean = plain_hook(text)
    # About 3 English words/sec; character estimate also supports unspaced copy.
    reading_ms = round(max(len(clean.split()) / 3, len(clean) / 18) * 1000 + 800)
    desired = options.duration_ms or max(4500, min(9000, reading_ms))
    return min(desired, total_ms) if total_ms is not None else desired
