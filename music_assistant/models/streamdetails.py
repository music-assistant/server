"""Models and helpers for the streamdetails of a MediaItem."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StreamType(Enum):
    """Enum with stream types."""

    EXECUTABLE = "executable"
    URL = "url"
    FILE = "file"
    CACHE = "cache"


class ContentType(Enum):
    """Enum with stream content types."""

    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    MPEG = "mpeg"


@dataclass
class StreamDetails:
    """Model for streamdetails."""

    type: StreamType
    provider: str
    item_id: str
    path: str
    content_type: ContentType
    sample_rate: int
    bit_depth: int
    player_id: str = ""
    details: Any = None
    seconds_played: int = 0
    gain_correct: float = 0

    def to_dict(
        self,
        use_bytes: bool = False,
        use_enum: bool = False,
        use_datetime: bool = False,
    ):
        """Handle conversion to dict."""
        return {
            "provider": self.provider,
            "item_id": self.item_id,
            "content_type": self.content_type.value,
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "gain_correct": self.gain_correct,
            "seconds_played": self.seconds_played,
        }
