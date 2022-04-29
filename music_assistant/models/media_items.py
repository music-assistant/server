"""Models and helpers for media items."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum, IntEnum
from typing import Any, Dict, List, Mapping, Optional, Set, Union

from mashumaro import DataClassDictMixin

from music_assistant.helpers.json import json
from music_assistant.helpers.util import create_sort_name

MetadataTypes = Union[int, bool, str, List[str]]

JSON_KEYS = ("artists", "artist", "album", "metadata", "provider_ids")


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

    UNKNOWN = 0
    LOSSY_MP3 = 1
    LOSSY_OGG = 2
    LOSSY_AAC = 3
    FLAC_LOSSLESS = 10  # 44.1/48khz 16 bits
    FLAC_LOSSLESS_HI_RES_1 = 20  # 44.1/48khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_2 = 21  # 88.2/96khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_3 = 22  # 176/192khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_4 = 23  # above 192khz 24 bits HI-RES


@dataclass(frozen=True)
class MediaItemProviderId(DataClassDictMixin):
    """Model for a MediaItem's provider id."""

    provider: str
    item_id: str
    available: bool = True
    quality: Optional[MediaQuality] = None
    details: Optional[str] = None
    url: Optional[str] = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id, self.quality))


class LinkType(Enum):
    """Enum wth link types."""

    WEBSITE = "website"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    LASTFM = "lastfm"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    SNAPCHAT = "snapchat"
    TIKTOK = "tiktok"
    DISCOGS = "discogs"
    WIKIPEDIA = "wikipedia"
    ALLMUSIC = "allmusic"


@dataclass(frozen=True)
class MediaItemLink(DataClassDictMixin):
    """Model for a link."""

    type: LinkType
    url: str

    def __hash__(self):
        """Return custom hash."""
        return hash((self.type.value))


class ImageType(Enum):
    """Enum wth image types."""

    THUMB = "thumb"
    WIDE_THUMB = "wide_thumb"
    FANART = "fanart"
    LOGO = "logo"
    CLEARART = "clearart"
    BANNER = "banner"
    CUTOUT = "cutout"
    BACK = "back"
    CDART = "cdart"
    OTHER = "other"


@dataclass(frozen=True)
class MediaItemImage(DataClassDictMixin):
    """Model for a image."""

    type: ImageType
    url: str

    def __hash__(self):
        """Return custom hash."""
        return hash((self.url))


@dataclass
class MediaItemMetadata(DataClassDictMixin):
    """Model for a MediaItem's metadata."""

    description: Optional[str] = None
    review: Optional[str] = None
    explicit: Optional[bool] = None
    images: Optional[Set[MediaItemImage]] = None
    genres: Optional[Set[str]] = None
    mood: Optional[str] = None
    style: Optional[str] = None
    copyright: Optional[str] = None
    lyrics: Optional[str] = None
    ean: Optional[str] = None
    label: Optional[str] = None
    links: Optional[Set[MediaItemLink]] = None
    performers: Optional[Set[str]] = None
    preview: Optional[str] = None
    replaygain: Optional[float] = None
    popularity: Optional[int] = None
    # last_refresh: timestamp the (full) metadata was last collected
    last_refresh: Optional[int] = None

    def update(
        self,
        new_values: "MediaItemMetadata",
        allow_overwrite: bool = False,
    ) -> "MediaItemMetadata":
        """Update metadata (in-place) with new values."""
        for fld in fields(self):
            new_val = getattr(new_values, fld.name)
            if new_val is None:
                continue
            cur_val = getattr(self, fld.name)
            if isinstance(cur_val, set):
                cur_val.update(new_val)
            elif cur_val is None or allow_overwrite:
                setattr(self, fld.name, new_val)
        return self


@dataclass
class MediaItem(DataClassDictMixin):
    """Base representation of a media item."""

    item_id: str
    provider: str
    name: str
    # optional fields below
    provider_ids: Set[MediaItemProviderId] = field(default_factory=set)
    sort_name: Optional[str] = None
    metadata: MediaItemMetadata = field(default_factory=MediaItemMetadata)
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
            self.add_provider_id(MediaItemProviderId(self.provider, self.item_id))

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create MediaItem object from database row."""
        db_row = dict(db_row)
        db_row["provider"] = "database"
        for key in JSON_KEYS:
            if key in db_row and db_row[key] is not None:
                db_row[key] = json.loads(db_row[key])
        if "in_library" in db_row:
            db_row["in_library"] = bool(db_row["in_library"])
        if db_row.get("albums"):
            db_row["album"] = db_row["albums"][0]
        db_row["item_id"] = str(db_row["item_id"])
        return cls.from_dict(db_row)

    def to_db_row(self) -> dict:
        """Create dict from item suitable for db."""
        return {
            key: json.dumps(value) if key in JSON_KEYS else value
            for key, value in self.to_dict().items()
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

    @property
    def image(self) -> str | None:
        """Return (first/random) image/thumb from metadata (if any)."""
        if self.metadata is None or self.metadata.images is None:
            return None
        return next(
            (x.url for x in self.metadata.images if x.type == ImageType.THUMB), None
        )

    def add_provider_id(self, prov_id: MediaItemProviderId) -> None:
        """Add provider ID, overwrite existing entry."""
        self.provider_ids = {
            x
            for x in self.provider_ids
            if not (x.item_id == prov_id.item_id and x.provider == prov_id.provider)
        }
        self.provider_ids.add(prov_id)

    @property
    def last_refresh(self) -> int:
        """Return timestamp the metadata was last refreshed (0 if full data never retrieved)."""
        return self.metadata.last_refresh or 0


@dataclass(frozen=True)
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
    musicbrainz_id: Optional[str] = None


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
    musicbrainz_id: Optional[str] = None  # release group id

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    isrc: Optional[str] = None
    musicbrainz_id: Optional[str] = None  # Recording ID
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
    WAV = "wav"
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian

    @classmethod
    def try_parse(
        cls: "ContentType", string: str, fallback: str = "mp3"
    ) -> "ContentType":
        """Try to parse ContentType from (url)string."""
        tempstr = string.lower()
        if "." in tempstr:
            tempstr = tempstr.split(".")[-1]
        tempstr = tempstr.split("?")[0]
        tempstr = tempstr.split("&")[0]
        try:
            return cls(tempstr)
        except ValueError:
            return cls(fallback)

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
    sample_rate: int = 44100
    bit_depth: int = 16
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
