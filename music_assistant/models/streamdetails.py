"""
    Models and helpers for the streamdetails of a MediaItem.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class StreamType(str, Enum):
    """Enum with stream types."""
    EXECUTABLE = "executable"
    URL = "url"
    FILE = "file"


class ContentType(str, Enum):
    """Enum with stream content types."""
    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    RAW = "raw"
    AAC = "aac"


@dataclass
class StreamDetails():
    """Model for streamdetails."""
    type: StreamType
    provider: str
    item_id: str
    path: str
    content_type: ContentType
    sample_rate: int
    bit_depth: int
    player_id: str = ""
    details: Optional[Any] = None
    seconds_played: int = None
