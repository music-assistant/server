"""Models and helpers for media items."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, List, Mapping

import ujson
from mashumaro import DataClassDictMixin


class MediaType(Enum):
    """Enum for MediaType."""

    Artist = "artist"
    Album = "album"
    Track = "track"
    Playlist = "playlist"
    Radio = "radio"


class ContributorRole(Enum):
    """Enum for Contributor Role."""

    Artist = "artist"
    Writer = "writer"
    Producer = "producer"


class AlbumType(Enum):
    """Enum for Album type."""

    Album = "album"
    Single = "single"
    Compilation = "compilation"
    Unknown = "unknown"


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


@dataclass
class MediaItem(DataClassDictMixin):
    """Representation of a media item."""

    item_id: str = ""
    provider: str = ""
    name: str = ""
    metadata: Any = field(default_factory=dict)
    provider_ids: List[MediaItemProviderId] = field(default_factory=list)
    in_library: bool = False
    media_type: MediaType = MediaType.Track

    @classmethod
    def from_dict(cls, dict_obj):
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
        for item in self.provider_ids:
            if item.available:
                return True


@dataclass
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.Artist
    musicbrainz_id: str = ""


@dataclass
class ItemMapping(DataClassDictMixin):
    """Representation of a minimized item object."""

    item_id: str = ""
    provider: str = ""
    name: str = ""
    media_type: MediaType = MediaType.Artist

    @classmethod
    def from_item(cls, item: Mapping):
        """Create ItemMapping object from regular item."""
        return cls.from_dict(item.to_dict())


@dataclass
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.Album
    version: str = ""
    year: int = 0
    artist: ItemMapping = None
    album_type: AlbumType = AlbumType.Unknown
    upc: str = ""


@dataclass
class FullAlbum(Album):
    """Model for an album with full details."""

    artist: Artist = None


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.Track
    duration: int = 0
    version: str = ""
    isrc: str = ""
    artists: List[ItemMapping] = field(default_factory=list)
    albums: List[ItemMapping] = field(default_factory=list)
    # album track only
    album: ItemMapping = None
    disc_number: int = 0
    track_number: int = 0
    # playlist track only
    position: int = 0


@dataclass
class FullTrack(Track):
    """Model for an album with full details."""

    artists: List[Artist] = field(default_factory=list)
    albums: List[Album] = field(default_factory=list)
    album: Album = None


@dataclass
class Playlist(MediaItem):
    """Model for a playlist."""

    media_type: MediaType = MediaType.Playlist
    owner: str = ""
    checksum: str = ""  # some value to detect playlist track changes
    is_editable: bool = False


@dataclass
class Radio(MediaItem):
    """Model for a radio station."""

    media_type: MediaType = MediaType.Radio
    duration: int = 86400


@dataclass
class SearchResult(DataClassDictMixin):
    """Model for Media Item Search result."""

    artists: List[Artist] = field(default_factory=list)
    albums: List[Album] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=list)
    radios: List[Radio] = field(default_factory=list)
