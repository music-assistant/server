"""Models and helpers for the streamdetails of a MediaItem."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from mashumaro.serializer.base.dict import DataClassDictMixin
from music_assistant.models.media_types import MediaType


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
    S24 = "s24"
    S32 = "s32"
    S64 = "s64"


@dataclass
class StreamDetails(DataClassDictMixin):
    """Model for streamdetails."""

    type: StreamType
    provider: str
    item_id: str
    path: str
    content_type: ContentType
    player_id: str = ""
    details: Any = None
    seconds_played: int = 0
    gain_correct: float = 0
    loudness: Optional[float] = None
    sample_rate: int = 44100
    bit_depth: int = 16
    media_type: MediaType = MediaType.TRACK

    def to_dict(
        self,
        use_bytes: bool = False,
        use_enum: bool = False,
        use_datetime: bool = False,
        **kwargs,
    ):
        """Handle conversion to dict."""
        return {
            "provider": self.provider,
            "item_id": self.item_id,
            "content_type": self.content_type.value,
            "media_type": self.media_type.value,
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "gain_correct": self.gain_correct,
            "seconds_played": self.seconds_played,
        }

    def __str__(self):
        """Return pretty printable string of object."""
        return f"{self.type.value}/{self.content_type.value} - {self.provider}/{self.item_id}"
