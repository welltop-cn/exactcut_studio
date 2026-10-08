from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from .hook_options import HookOptions
from .hook_emphasis import plain_hook


class TimelineItem(BaseModel):
    type: Literal["episode", "title_card"]
    episode: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    text: str | None = None
    duration_ms: int | None = None
    full_episode: bool = False
    to_end: bool = False

    @model_validator(mode="after")
    def validate_item(self) -> "TimelineItem":
        if self.type == "episode":
            if self.full_episode or self.to_end:
                if not self.episode:
                    raise ValueError('open-ended episode requires episode')
                if self.full_episode:
                    self.start_ms = 0
                elif self.start_ms is None or self.start_ms < 0:
                    raise ValueError('to-end episode requires a nonnegative start')
                self.end_ms = None
                self.duration_ms = None
                return self
            if not self.episode or self.start_ms is None or self.end_ms is None:
                raise ValueError("episode item requires episode/start_ms/end_ms")
            if self.end_ms <= self.start_ms:
                raise ValueError("episode end_ms must be greater than start_ms")
            self.duration_ms = self.end_ms - self.start_ms
        else:
            if not self.text or not self.duration_ms or self.duration_ms <= 0:
                raise ValueError("title_card requires text and positive duration_ms")
        return self


class CutRequest(BaseModel):
    request_id: str
    title_cn: str = ""
    hook_text: str = ""
    hook_options: HookOptions = Field(default_factory=HookOptions)
    structure: str = "顺叙"
    notes: str = ""
    seq: int = Field(gt=0)
    output_filename: str
    timeline: list[TimelineItem]

    @model_validator(mode='after')
    def validate_manual_hook(self):
        if self.hook_options.mode == 'manual':
            clean = plain_hook(self.hook_text)
            previous = 0
            for span in sorted(self.hook_options.manual_ranges, key=lambda r: r.start):
                phrase = clean[span.start:span.end]
                if span.start < previous or span.end > len(clean) or not phrase.strip() or phrase != phrase.strip():
                    raise ValueError('手工重点与文案不一致，请重新选词')
                previous = span.end
        return self

    @field_validator("timeline")
    @classmethod
    def timeline_not_empty(cls, value: list[TimelineItem]) -> list[TimelineItem]:
        if not value:
            raise ValueError("timeline must not be empty")
        return value


class BatchManifest(BaseModel):
    schema_version: int = 1
    drama_id: str
    drama_title: str
    business_date: str
    author: str
    creator2: str = "agent"
    material_type: str = "typezxraw"
    source_workbook: str
    requests: list[CutRequest]

    @field_validator("drama_id")
    @classmethod
    def numeric_drama_id(cls, value: str) -> str:
        value = str(value).strip().strip("#")
        if not value.isdigit():
            raise ValueError("drama_id must contain digits only")
        return value

    @field_validator("business_date")
    @classmethod
    def eight_digit_date(cls, value: str) -> str:
        value = value.replace("-", "").replace("/", "")
        if len(value) != 8 or not value.isdigit():
            raise ValueError("business_date must be YYYYMMDD")
        return value

    @field_validator("requests")
    @classmethod
    def unique_requests(cls, value: list[CutRequest]) -> list[CutRequest]:
        if not value:
            raise ValueError("manifest must contain at least one request")
        ids = [item.request_id for item in value]
        filenames = [item.output_filename for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("request_id must be unique")
        if len(filenames) != len(set(filenames)):
            raise ValueError("output_filename must be unique")
        return value
