"""Models and helpers for media items."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List

from music_assistant.helpers.util import CustomIntEnum


class MediaType(CustomIntEnum):
    """Enum for MediaType."""

    Artist = 1
    Album = 2
    Track = 3
    Playlist = 4
    Radio = 5


class ContributorRole(CustomIntEnum):
    """Enum for Contributor Role."""

    Artist = 1
    Writer = 2
    Producer = 3


class AlbumType(CustomIntEnum):
    """Enum for Album type."""

    Album = 1
    Single = 2
    Compilation = 3


class TrackQuality(CustomIntEnum):
    """Enum for Track Quality."""

    LOSSY_MP3 = 0
    LOSSY_OGG = 1
    LOSSY_AAC = 2
    FLAC_LOSSLESS = 6  # 44.1/48khz 16 bits HI-RES
    FLAC_LOSSLESS_HI_RES_1 = 7  # 44.1/48khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_2 = 8  # 88.2/96khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_3 = 9  # 176/192khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_4 = 10  # above 192khz 24 bits HI-RES
    UNKNOWN = 99


@dataclass
class MediaItemProviderId:
    """Model for a MediaItem's provider id."""

    provider: str
    item_id: str
    quality: TrackQuality = TrackQuality.UNKNOWN
    details: str = None


class ExternalId(Enum):
    """Enum with external id's."""

    MUSICBRAINZ = "musicbrainz"
    UPC = "upc"
    ISRC = "isrc"


@dataclass
class MediaItem:
    """Representation of a media item."""

    item_id: str = ""
    provider: str = ""
    name: str = ""
    metadata: Any = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    external_ids: Any = field(default_factory=dict)
    provider_ids: List[MediaItemProviderId] = field(default_factory=list)
    in_library: List[str] = field(default_factory=list)
    is_lazy: bool = False
    available: bool = True


@dataclass
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.Artist
    sort_name: str = ""


@dataclass
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.Album
    version: str = ""
    year: int = 0
    artist: Artist = None
    labels: List[str] = field(default_factory=list)
    album_type: AlbumType = AlbumType.Album


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.Track
    duration: int = 0
    version: str = ""
    artists: List[Artist] = field(default_factory=list)
    album: Album = None
    disc_number: int = 1
    track_number: int = 1


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
class SearchResult:
    """Model for Media Item Search result."""

    artists: List[Artist] = field(default_factory=list)
    albums: List[Album] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=list)
    radios: List[Radio] = field(default_factory=list)
