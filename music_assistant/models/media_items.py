"""Models and helpers for media items."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, List, Mapping, Optional, Union

from mashumaro import DataClassDictMixin
from music_assistant.helpers.json import json
from music_assistant.helpers.util import create_sort_name


class MediaType(Enum):
    """Enum for MediaType."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    RADIO = "radio"
    UNKNOWN = "unknown"


class MediaQuality(IntEnum):
    """Enum for Media Quality."""

    LOSSY_MP3 = 0
    LOSSY_OGG = 1
    LOSSY_AAC = 2
    FLAC_LOSSLESS = 6  # 44.1/48khz 16 bits
    FLAC_LOSSLESS_HI_RES_1 = 7  # 44.1/48khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_2 = 8  # 88.2/96khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_3 = 9  # 176/192khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_4 = 10  # above 192khz 24 bits HI-RES
    UNKNOWN = 99


@dataclass
class MediaItemProviderId(DataClassDictMixin):
    """Model for a MediaItem's provider id."""

    provider: str
    item_id: str
    quality: MediaQuality = MediaQuality.UNKNOWN
    details: str = None
    available: bool = True

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id, self.quality))


@dataclass
class MediaItem(DataClassDictMixin):
    """Base representation of a media item."""

    item_id: str
    provider: str
    name: str
    sort_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    provider_ids: List[MediaItemProviderId] = field(default_factory=list)
    in_library: bool = False
    media_type: MediaType = MediaType.UNKNOWN
    uri: str = ""

    def __post_init__(self):
        """Call after init."""
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)
        if not self.sort_name:
            self.sort_name = create_sort_name(self.name)
        if not self.provider_ids:
            self.provider_ids.append(
                MediaItemProviderId(provider=self.provider, item_id=self.item_id)
            )

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create MediaItem object from database row."""
        db_row = dict(db_row)
        for key in ["artists", "artist", "metadata", "provider_ids"]:
            if key in db_row:
                db_row[key] = json.loads(db_row[key])
        db_row["provider"] = "database"
        if "in_library" in db_row:
            db_row["in_library"] = bool(db_row["in_library"])
        if db_row.get("albums"):
            db_row["album"] = db_row["albums"][0]
        db_row["item_id"] = str(db_row["item_id"])
        return cls.from_dict(db_row)

    def to_db_row(self) -> dict:
        """Create dict from item suitable for db."""
        return {
            key: json.dumps(val) if isinstance(val, (list, dict)) else val
            for key, val in self.to_dict().items()
            if key
            not in [
                "item_id",
                "provider",
                "media_type",
                "uri",
                "album",
                "disc_number",
                "track_number",
                "position",
            ]
        }

    @property
    def available(self):
        """Return (calculated) availability."""
        return any(x.available for x in self.provider_ids)


@dataclass
class ItemMapping(DataClassDictMixin):
    """Representation of a minimized item object."""

    item_id: str
    provider: str
    name: str = ""
    media_type: MediaType = MediaType.ARTIST
    uri: str = ""

    def __post_init__(self):
        """Call after init."""
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)

    @classmethod
    def from_item(cls, item: "MediaItem"):
        """Create ItemMapping object from regular item."""
        return cls.from_dict(item.to_dict())

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.ARTIST
    musicbrainz_id: str = ""


class AlbumType(Enum):
    """Enum for Album type."""

    ALBUM = "album"
    SINGLE = "single"
    COMPILATION = "compilation"
    UNKNOWN = "unknown"


@dataclass
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.ALBUM
    version: str = ""
    year: Optional[int] = None
    artist: Union[ItemMapping, Artist, None] = None
    album_type: AlbumType = AlbumType.UNKNOWN
    upc: Optional[str] = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    isrc: str = ""
    artists: List[Union[ItemMapping, Artist]] = field(default_factory=list)
    # album track only
    album: Union[ItemMapping, Album, None] = None
    disc_number: Optional[int] = None
    track_number: Optional[int] = None
    # playlist track only
    position: Optional[int] = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))


@dataclass
class Playlist(MediaItem):
    """Model for a playlist."""

    media_type: MediaType = MediaType.PLAYLIST
    owner: str = ""
    checksum: str = ""  # some value to detect playlist track changes
    is_editable: bool = False


@dataclass
class Radio(MediaItem):
    """Model for a radio station."""

    media_type: MediaType = MediaType.RADIO
    duration: int = 86400

    def to_db_row(self) -> dict:
        """Create dict from item suitable for db."""
        val = super().to_db_row()
        val.pop("duration", None)
        return val


def create_uri(media_type: MediaType, provider_id: str, item_id: str):
    """Create uri for mediaitem."""
    return f"{provider_id}://{media_type.value}/{item_id}"


MediaItemType = Union[Artist, Album, Track, Radio, Playlist]


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

    @classmethod
    def from_bit_depth(
        cls, bit_depth: int, floating_point: bool = False
    ) -> "ContentType":
        """Return (PCM) Contenttype from PCM bit depth."""
        if floating_point and bit_depth > 32:
            return cls.PCM_F64LE
        if floating_point:
            return cls.PCM_F32LE
        if bit_depth == 16:
            return cls.PCM_S16LE
        if bit_depth == 24:
            return cls.PCM_S24LE
        return cls.PCM_S32LE


@dataclass
class StreamDetails(DataClassDictMixin):
    """Model for streamdetails."""

    type: StreamType
    provider: str
    item_id: str
    path: str
    content_type: ContentType
    player_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    seconds_played: int = 0
    gain_correct: float = 0
    loudness: Optional[float] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: int = 2
    media_type: MediaType = MediaType.TRACK
    queue_id: str = None

    def __post_serialize__(self, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Exclude internal fields from dict."""
        # pylint: disable=invalid-name,no-self-use
        d.pop("path")
        d.pop("details")
        return d

    def __str__(self):
        """Return pretty printable string of object."""
        return f"{self.type.value}/{self.content_type.value} - {self.provider}/{self.item_id}"
