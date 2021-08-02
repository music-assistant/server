"""Models and helpers for media items."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, List, Mapping, Optional, Set

import ujson
from mashumaro import DataClassDictMixin
from music_assistant.helpers.util import create_uri


class MediaType(Enum):
    """Enum for MediaType."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    RADIO = "radio"
    UNKNOWN = "unknown"


class ContributorRole(Enum):
    """Enum for Contributor Role."""

    ARTIST = "artist"
    WRITER = "writer"
    PRODUCER = "producer"


class AlbumType(Enum):
    """Enum for Album type."""

    ALBUM = "album"
    SINGLE = "single"
    COMPILATION = "compilation"
    UNKNOWN = "unknown"


class TrackQuality(IntEnum):
    """Enum for Track Quality."""

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
    quality: TrackQuality = TrackQuality.UNKNOWN
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
    name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    provider_ids: Set[MediaItemProviderId] = field(default_factory=set)
    in_library: bool = False
    media_type: MediaType = MediaType.UNKNOWN
    uri: str = ""

    def __post_init__(self):
        """Call after init."""
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)

    @classmethod
    def from_dict(cls, dict_obj: dict):
        # pylint: disable=arguments-differ
        """Parse MediaItem from dict."""
        if dict_obj["media_type"] == "artist":
            return Artist.from_dict(dict_obj)
        if dict_obj["media_type"] == "album":
            return Album.from_dict(dict_obj)
        if dict_obj["media_type"] == "track":
            return Track.from_dict(dict_obj)
        if dict_obj["media_type"] == "playlist":
            return Playlist.from_dict(dict_obj)
        if dict_obj["media_type"] == "radio":
            return Radio.from_dict(dict_obj)
        return super().from_dict(dict_obj)

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create MediaItem object from database row."""
        db_row = dict(db_row)
        for key in ["artists", "artist", "album", "metadata", "provider_ids", "albums"]:
            if key in db_row:
                db_row[key] = ujson.loads(db_row[key])
        db_row["provider"] = "database"
        if "in_library" in db_row:
            db_row["in_library"] = bool(db_row["in_library"])
        if db_row.get("albums"):
            db_row["album"] = db_row["albums"][0]
        db_row["item_id"] = str(db_row["item_id"])
        return cls.from_dict(db_row)

    @property
    def sort_name(self):
        """Return sort name."""
        sort_name = self.name
        for item in ["The ", "De ", "de ", "Les "]:
            if self.name.startswith(item):
                sort_name = "".join(self.name.split(item)[1:])
        return sort_name.lower()

    @property
    def available(self):
        """Return (calculated) availability."""
        return any(x.available for x in self.provider_ids)

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))

    def __str__(self):
        """Return string representation, used for logging."""
        return f"{self.name} ({self.uri})"


@dataclass
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.ARTIST
    musicbrainz_id: str = ""

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


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
    def from_item(cls, item: Mapping):
        """Create ItemMapping object from regular item."""
        return cls.from_dict(item.to_dict())

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.ALBUM
    version: str = ""
    year: int = 0
    artist: Optional[ItemMapping] = None
    album_type: AlbumType = AlbumType.UNKNOWN
    upc: str = ""

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class FullAlbum(Album):
    """Model for an album with full details."""

    artist: Optional[Artist] = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    isrc: str = ""
    artists: Set[ItemMapping] = field(default_factory=set)
    albums: Set[ItemMapping] = field(default_factory=set)
    # album track only
    album: Optional[ItemMapping] = None
    disc_number: int = 0
    track_number: int = 0
    # playlist track only
    position: int = 0

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class FullTrack(Track):
    """Model for an album with full details."""

    artists: Set[Artist] = field(default_factory=set)
    albums: Set[Album] = field(default_factory=set)
    album: Optional[Album] = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class Playlist(MediaItem):
    """Model for a playlist."""

    media_type: MediaType = MediaType.PLAYLIST
    owner: str = ""
    checksum: str = ""  # some value to detect playlist track changes
    is_editable: bool = False

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class Radio(MediaItem):
    """Model for a radio station."""

    media_type: MediaType = MediaType.RADIO
    duration: int = 86400

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class SearchResult(DataClassDictMixin):
    """Model for Media Item Search result."""

    artists: List[Artist] = field(default_factory=list)
    albums: List[Album] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=list)
    radios: List[Radio] = field(default_factory=list)
