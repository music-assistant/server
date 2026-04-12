"""Data models for the Hue Lights Sync provider."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LightChannel:
    """A single light channel in a Hue Entertainment area."""

    channel_id: int
    service_id: str
    name: str
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class EntertainmentArea:
    """A Hue Entertainment configuration/area."""

    id: str
    name: str
    channels: list[LightChannel] = field(default_factory=list)


@dataclass
class LightColorCommand:
    """A color command for a single light channel (16-bit RGB)."""

    channel_id: int
    red: int = 0  # 0-65535
    green: int = 0  # 0-65535
    blue: int = 0  # 0-65535
