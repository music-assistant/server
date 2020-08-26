"""Models and helpers for media items."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import List, Optional


class MediaType(IntEnum):
    """Enum for MediaType."""
    Artist = 1
    Album = 2
    Track = 3
    Playlist = 4
    Radio = 5


def media_type_from_string(media_type_str: str) -> MediaType:
    """Convert a string to a MediaType."""
    media_type_str = media_type_str.lower()
    if "artist" in media_type_str or media_type_str == "1":
        return MediaType.Artist
    if "album" in media_type_str or media_type_str == "2":
        return MediaType.Album
    if "track" in media_type_str or media_type_str == "3":
        return MediaType.Track
    if "playlist" in media_type_str or media_type_str == "4":
        return MediaType.Playlist
    if "radio" in media_type_str or media_type_str == "5":
        return MediaType.Radio
    return None


class ContributorRole(IntEnum):
    """Enum for Contributor Role."""
    Artist = 1
    Writer = 2
    Producer = 3


class AlbumType(IntEnum):
    """Enum for Album type."""
    Album = 1
    Single = 2
    Compilation = 3


class TrackQuality(IntEnum):
    """Enum for Track Quality."""
    LOSSY_MP3 = 0
    LOSSY_OGG = 1
    LOSSY_AAC = 2
    FLAC_LOSSLESS = 6  # 44.1/48khz 16 bits HI-RES
    FLAC_LOSSLESS_HI_RES_1 = 7  # 44.1/48khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_2 = 8  # 88.2/96khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_3 = 9  # 176/192khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_4 = 10  # above 192khz 24 bits HI-RES


@dataclass
class MediaItemProviderId():
    """Model for a MediaItem's provider id."""
    provider: str
    item_id: str
    quality: Optional[TrackQuality]


@dataclass
class MediaItemExternalId():
    """Model for a MediaItem external ID."""
    provider: str
    item_id: str


@dataclass
class MediaItem(object):
    """Representation of a media item."""
    item_id: str
    provider: str
    name: Optional[str]
    metadata: Optional[dict]
    tags: Optional[List[str]]
    external_ids: Optional[List[MediaItemExternalId]]
    ids: Optional[List[MediaItemProviderId]]
    in_library: Optional[List[str]]
    media_type: Optional[MediaType]
    is_lazy: bool = False
    available: bool = True


@dataclass
class Artist(MediaItem):
    """Model for an artist"""
    sort_name: Optional[str] = ""
    media_type: MediaType = MediaType.Artist


@dataclass
class Album(MediaItem):
    """Model for an album"""
    version: str = ""
    year: int = 0
    artist: Optional[Artist] = None
    labels: List[str] = field(default_factory=list)
    album_type: AlbumType = AlbumType.Album
    media_type: MediaType = MediaType.Album


@dataclass
class Track(MediaItem):
    """Model for a track"""
    duration: int = 0
    version: str = ""
    artists: List[Artist] = field(default_factory=list)
    album: Optional[Album] = None
    disc_number: int = 1
    track_number: int = 1
    media_type: MediaType = MediaType.Track


@dataclass
class Playlist(MediaItem):
    """Model for a playlist"""
    owner: str = ""
    checksum: [Optional[str]] = None  # some value to detect playlist track changes
    is_editable: bool = False
    media_type: MediaType = MediaType.Playlist


@dataclass
class Radio(MediaItem):
    """Model for a radio station"""
    duration: int = 86400
    media_type: MediaType = MediaType.Radio


@dataclass
class SearchResult():
    """Model for Media Item Search result."""
    artists: Optional[List[Artist]]
    albums: Optional[List[Album]]
    tracks: Optional[List[Track]]
    playlists: Optional[List[Playlist]]
    radios: Optional[List[Radio]]


class StreamType(Enum):
    """Enum with stream types."""
    EXECUTABLE = "executable"
    URL = "url"
    FILE = "file"


class ContentType(Enum):
    """Enum with stream content types."""
    OGG = "ogg"
    FLAC = "flac"
    MP3 = "mp3"
    RAW = "raw"


@dataclass
class StreamDetails():
    type: StreamType
    path: str
    content_type: ContentType
    sample_rate: int
    bit_depth: int
    provider: str
    item_id: str
