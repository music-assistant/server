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
    RAW = "raw"
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
    sox_options: str = None
