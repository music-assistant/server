"""Pydantic models for MSX JSON API."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MsxTemplateType(str, Enum):
    """MSX Template types."""

    SEPARATE = "separate"
    LIST = "list"


class MsxTemplate(BaseModel):
    """MSX Template model."""

    type: MsxTemplateType | str = MsxTemplateType.SEPARATE
    layout: str | None = None
    icon: str | None = None
    action: str | None = None
    image_filler: str | None = Field(default=None, serialization_alias="imageFiller")


class MsxItem(BaseModel):
    """MSX Content Item model."""

    title: str | None = None
    label: str | None = None
    image: str | None = None
    icon: str | None = None
    action: str | None = None
    background: str | None = None
    player_label: str | None = Field(default=None, serialization_alias="playerLabel")
    title_footer: str | None = Field(default=None, serialization_alias="titleFooter")
    duration: int | None = None
    next_action: str | None = Field(default=None, serialization_alias="nextAction")
    prev_action: str | None = Field(default=None, serialization_alias="prevAction")
    content: str | None = None
    url: str | None = None
    type: str | None = None


class MsxContent(BaseModel):
    """MSX Content Page model."""

    type: str = "list"
    headline: str | None = None
    template: MsxTemplate | None = None
    items: list[MsxItem] | None = None
    action: str | None = None
    hint: str | None = None
