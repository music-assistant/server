"""Models and helpers for media items."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, List, Mapping

import ujson
from mashumaro import DataClassDictMixin
from music_assistant.helpers.util import get_sort_name


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
    is_lazy: bool = False

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create MediaItem object from database row."""
        db_row = dict(db_row)
        for key in ["artists", "artist", "album", "metadata", "provider_ids"]:
            if key in db_row:
                db_row[key] = ujson.loads(db_row[key])
        db_row["provider"] = "database"
        if "in_library" in db_row:
            db_row["in_library"] = bool(db_row["in_library"])
        return cls.from_dict(db_row)

    @property
    def sort_name(self):
        """Return sort name."""
        return get_sort_name(self.name)

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
class AlbumArtist(DataClassDictMixin):
    """Representation of a minimized artist object."""

    item_id: str = ""
    provider: str = ""
    name: str = ""
    media_type: MediaType = MediaType.Artist


@dataclass
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.Album
    version: str = ""
    year: int = 0
    artist: AlbumArtist = None
    album_type: AlbumType = AlbumType.Album
    upc: str = ""


@dataclass
class TrackArtist(DataClassDictMixin):
    """Representation of a minimized artist object."""

    item_id: str = ""
    provider: str = ""
    name: str = ""
    media_type: MediaType = MediaType.Artist


@dataclass
class TrackAlbum(DataClassDictMixin):
    """Representation of a minimized album object."""

    item_id: str = ""
    provider: str = ""
    name: str = ""
    media_type: MediaType = MediaType.Album


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.Track
    duration: int = 0
    version: str = ""
    artists: List[TrackArtist] = field(default_factory=list)
    album: TrackAlbum = None
    disc_number: int = 1
    track_number: int = 1
    position: int = 0
    isrc: str = ""


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
