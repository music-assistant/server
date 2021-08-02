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
    """Enum with audio content types supported by ffmpeg."""

    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    MPEG = "mpeg"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian

    def is_pcm(self):
        """Return if contentype is PCM."""
        return self.name.startswith("PCM")

    def sox_supported(self):
        """Return if ContentType is supported by SoX."""
        return self not in [ContentType.AAC, ContentType.MPEG]

    def sox_format(self):
        """Convert the ContentType to SoX compatible format."""
        if not self.sox_supported():
            raise NotImplementedError
        return self.value.replace("le", "")


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
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
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
