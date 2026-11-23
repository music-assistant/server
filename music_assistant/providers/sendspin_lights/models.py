"""Light-agnostic types exchanged between the analyzer and a light provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LightChannel(Protocol):
    """A light (or segment of a light) the analyzer renders a color for."""

    @property
    def channel_id(self) -> int:
        """Identifier echoed back in the rendered `LightColor`."""
        ...

    @property
    def position(self) -> tuple[float, float, float]:
        """Position in the room (x, y, z), each axis typically in [-1, 1]."""
        ...


@dataclass(frozen=True, slots=True)
class LightColor:
    """Color rendered for one channel, as 16-bit RGB."""

    channel_id: int
    red: int = 0
    green: int = 0
    blue: int = 0
