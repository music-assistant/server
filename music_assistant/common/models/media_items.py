"""Models and helpers for media items."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, TypeGuard, TypeVar, cast

from mashumaro import DataClassDictMixin

from music_assistant.common.helpers.global_cache import get_global_cache_value
from music_assistant.common.helpers.uri import create_uri
from music_assistant.common.helpers.util import create_sort_name, is_valid_uuid, merge_lists
from music_assistant.common.models.enums import (
    AlbumType,
    ContentType,
    ExternalID,
    ImageType,
    LinkType,
    MediaType,
)
from music_assistant.common.models.errors import InvalidDataError

MetadataTypes = int | bool | str | list[str]

_T = TypeVar("_T")


class UniqueList(list[_T]):
    """Custom list that ensures the inserted items are unique."""

    def __init__(self, iterable: Iterable[_T] | None = None) -> None:
        """Initialize."""
        if not iterable:
            super().__init__()
            return
        seen: set[_T] = set()
        seen_add = seen.add
        super().__init__(x for x in iterable if not (x in seen or seen_add(x)))

    def append(self, item: _T) -> None:
        """Append item."""
        if item in self:
            return
        super().append(item)

    def extend(self, other: Iterable[_T]) -> None:
        """Extend list."""
        other = [x for x in other if x not in self]
        super().extend(other)


@dataclass(kw_only=True)
class AudioFormat(DataClassDictMixin):
    """Model for AudioFormat details."""

    content_type: ContentType = ContentType.UNKNOWN
    sample_rate: int = 44100
    bit_depth: int = 16
    channels: int = 2
    output_format_str: str = ""
    bit_rate: int = 320  # optional

    def __post_init__(self) -> None:
        """Execute actions after init."""
        if not self.output_format_str and self.content_type.is_pcm():
            self.output_format_str = (
                f"pcm;codec=pcm;rate={self.sample_rate};"
                f"bitrate={self.bit_depth};channels={self.channels}"
            )
        elif not self.output_format_str:
            self.output_format_str = self.content_type.value

    @property
    def quality(self) -> int:
        """Calculate quality score."""
        if self.content_type.is_lossless():
            # lossless content is scored very high based on sample rate and bit depth
            return int(self.sample_rate / 1000) + self.bit_depth
        # lossy content, bit_rate is most important score
        # but prefer some codecs over others
        # calculate a rough score based on bit rate per channel
        bit_rate_score = (self.bit_rate / self.channels) / 100
        if self.content_type in (ContentType.AAC, ContentType.OGG):
            bit_rate_score += 1
        return int(bit_rate_score)

    @property
    def pcm_sample_size(self) -> int:
        """Return the PCM sample size."""
        return int(self.sample_rate * (self.bit_depth / 8) * self.channels)

    def __eq__(self, other: object) -> bool:
        """Check equality of two items."""
        if not isinstance(other, AudioFormat):
            return False
        return self.output_format_str == other.output_format_str


@dataclass(kw_only=True)
class ProviderMapping(DataClassDictMixin):
    """Model for a MediaItem's provider mapping details."""

    item_id: str
    provider_domain: str
    provider_instance: str
    available: bool = True
    # quality/audio details (streamable content only)
    audio_format: AudioFormat = field(default_factory=AudioFormat)
    # url = link to provider details page if exists
    url: str | None = None
    # optional details to store provider specific details
    details: str | None = None

    @property
    def quality(self) -> int:
        """Return quality score."""
        quality = self.audio_format.quality
        # append provider score so filebased providers are scored higher
        return quality + self.priority

    @property
    def priority(self) -> int:
        """Return priority score to sort local providers before online."""
        if not (local_provs := get_global_cache_value("non_streaming_providers")):
            # this is probably the client
            return 0
        if TYPE_CHECKING:
            local_provs = cast(set[str], local_provs)
        if self.provider_domain in ("filesystem_local", "filesystem_smb"):
            return 2
        if self.provider_instance in local_provs:
            return 1
        return 0

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.provider_instance, self.item_id))

    def __eq__(self, other: object) -> bool:
        """Check equality of two items."""
        if not isinstance(other, ProviderMapping):
            return False
        return self.provider_instance == other.provider_instance and self.item_id == other.item_id


@dataclass(frozen=True, kw_only=True)
class MediaItemLink(DataClassDictMixin):
    """Model for a link."""

    type: LinkType
    url: str

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash(self.type)

    def __eq__(self, other: object) -> bool:
        """Check equality of two items."""
        if not isinstance(other, MediaItemLink):
            return False
        return self.url == other.url


@dataclass(frozen=True, kw_only=True)
class MediaItemImage(DataClassDictMixin):
    """Model for a image."""

    type: ImageType
    path: str
    provider: str  # provider lookup key (only use instance id for fileproviders)
    remotely_accessible: bool = False  # url that is accessible from anywhere

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.type.value, self.provider, self.path))

    def __eq__(self, other: object) -> bool:
        """Check equality of two items."""
        if not isinstance(other, MediaItemImage):
            return False
        return self.__hash__() == other.__hash__()


@dataclass(frozen=True, kw_only=True)
class MediaItemChapter(DataClassDictMixin):
    """Model for a chapter."""

    chapter_id: int
    position_start: float
    position_end: float | None = None
    title: str | None = None

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash(self.chapter_id)

    def __eq__(self, other: object) -> bool:
        """Check equality of two items."""
        if not isinstance(other, MediaItemChapter):
            return False
        return self.chapter_id == other.chapter_id


@dataclass(kw_only=True)
class MediaItemMetadata(DataClassDictMixin):
    """Model for a MediaItem's metadata."""

    description: str | None = None
    review: str | None = None
    explicit: bool | None = None
    # NOTE: images is a list of available images, sorted by preference
    images: UniqueList[MediaItemImage] | None = None
    genres: set[str] | None = None
    mood: str | None = None
    style: str | None = None
    copyright: str | None = None
    lyrics: str | None = None  # tracks only
    label: str | None = None
    links: set[MediaItemLink] | None = None
    chapters: UniqueList[MediaItemChapter] | None = None
    performers: set[str] | None = None
    preview: str | None = None
    popularity: int | None = None
    # last_refresh: timestamp the (full) metadata was last collected
    last_refresh: int | None = None

    def update(
        self,
        new_values: MediaItemMetadata,
    ) -> MediaItemMetadata:
        """Update metadata (in-place) with new values."""
        if not new_values:
            return self
        for fld in fields(self):
            new_val = getattr(new_values, fld.name)
            if new_val is None:
                continue
            cur_val = getattr(self, fld.name)
            if isinstance(cur_val, list) and isinstance(new_val, list):
                new_val = UniqueList(merge_lists(cur_val, new_val))
                setattr(self, fld.name, new_val)
            elif isinstance(cur_val, set) and isinstance(new_val, set | list | tuple):
                cur_val.update(new_val)
            elif new_val and fld.name in (
                "popularity",
                "last_refresh",
                "cache_checksum",
            ):
                # some fields are always allowed to be overwritten
                # (such as checksum and last_refresh)
                setattr(self, fld.name, new_val)
            elif cur_val is None:
                setattr(self, fld.name, new_val)
        return self


@dataclass(kw_only=True)
class _MediaItemBase(DataClassDictMixin):
    """Base representation of a Media Item or ItemMapping item object."""

    item_id: str
    provider: str  # provider instance id or provider domain
    name: str
    version: str = ""
    # sort_name will be auto generated if omitted
    sort_name: str | None = None
    # uri is auto generated, do not override unless really needed
    uri: str | None = None
    external_ids: set[tuple[ExternalID, str]] = field(default_factory=set)
    media_type: MediaType = MediaType.UNKNOWN

    def __post_init__(self) -> None:
        """Call after init."""
        if self.uri is None:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)
        if self.sort_name is None:
            self.sort_name = create_sort_name(self.name)

    def get_external_id(self, external_id_type: ExternalID) -> str | None:
        """Get (the first instance) of given External ID or None if not found."""
        for ext_id in self.external_ids:
            if ext_id[0] != external_id_type:
                continue
            return ext_id[1]
        return None

    def add_external_id(self, external_id_type: ExternalID, value: str) -> None:
        """Add ExternalID."""
        if external_id_type.is_musicbrainz and not is_valid_uuid(value):
            msg = f"Invalid MusicBrainz identifier: {value}"
            raise InvalidDataError(msg)
        if external_id_type.is_unique and (
            existing := next((x for x in self.external_ids if x[0] == external_id_type), None)
        ):
            self.external_ids.remove(existing)
        self.external_ids.add((external_id_type, value))

    @property
    def mbid(self) -> str | None:
        """Return MusicBrainz ID."""
        if self.media_type == MediaType.ARTIST:
            return self.get_external_id(ExternalID.MB_ARTIST)
        if self.media_type == MediaType.ALBUM:
            return self.get_external_id(ExternalID.MB_ALBUM)
        if self.media_type == MediaType.TRACK:
            return self.get_external_id(ExternalID.MB_RECORDING)
        return None

    @mbid.setter
    def mbid(self, value: str) -> None:
        """Set MusicBrainz External ID."""
        if self.media_type == MediaType.ARTIST:
            self.add_external_id(ExternalID.MB_ARTIST, value)
        elif self.media_type == MediaType.ALBUM:
            self.add_external_id(ExternalID.MB_ALBUM, value)
        elif self.media_type == MediaType.TRACK:
            # NOTE: for tracks we use the recording id to
            # differentiate a unique recording
            # and not the track id (as that is just the reference
            #  of the recording on a specific album)
            self.add_external_id(ExternalID.MB_RECORDING, value)
            return

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash(self.uri)

    def __eq__(self, other: object) -> bool:
        """Check equality of two items."""
        if not isinstance(other, MediaItem | ItemMapping):
            return False
        return self.uri == other.uri


@dataclass(kw_only=True)
class MediaItem(_MediaItemBase):
    """Base representation of a media item."""

    __eq__ = _MediaItemBase.__eq__

    provider_mappings: set[ProviderMapping]
    # optional fields below
    metadata: MediaItemMetadata = field(default_factory=MediaItemMetadata)
    favorite: bool = False
    position: int | None = None  # required for playlist tracks, optional for all other

    def __hash__(self) -> int:
        """Return hash of MediaItem."""
        return super().__hash__()

    @property
    def available(self) -> bool:
        """Return (calculated) availability."""
        if not (available_providers := get_global_cache_value("unique_providers")):
            # this is probably the client
            return any(x.available for x in self.provider_mappings)
        if TYPE_CHECKING:
            available_providers = cast(set[str], available_providers)
        for x in self.provider_mappings:
            if available_providers.intersection({x.provider_domain, x.provider_instance}):
                return True
        return False

    @property
    def image(self) -> MediaItemImage | None:
        """Return (first/random) image/thumb from metadata (if any)."""
        if self.metadata is None or self.metadata.images is None:
            return None
        return next((x for x in self.metadata.images if x.type == ImageType.THUMB), None)


@dataclass(kw_only=True)
class ItemMapping(_MediaItemBase):
    """Representation of a minimized item object."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    available: bool = True
    image: MediaItemImage | None = None

    @classmethod
    def from_item(cls, item: MediaItem | ItemMapping) -> ItemMapping:
        """Create ItemMapping object from regular item."""
        if isinstance(item, ItemMapping):
            return item
        thumb_image = None
        if item.metadata and item.metadata.images:
            for img in item.metadata.images:
                if img.type != ImageType.THUMB:
                    continue
                thumb_image = img
                break
        return cls.from_dict(
            {**item.to_dict(), "image": thumb_image.to_dict() if thumb_image else None}
        )


@dataclass(kw_only=True)
class Artist(MediaItem):
    """Model for an artist."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.ARTIST


@dataclass(kw_only=True)
class Album(MediaItem):
    """Model for an album."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.ALBUM
    version: str = ""
    year: int | None = None
    artists: UniqueList[Artist | ItemMapping] = field(default_factory=UniqueList)
    album_type: AlbumType = AlbumType.UNKNOWN

    @property
    def artist_str(self) -> str:
        """Return (combined) artist string for track."""
        return "/".join(x.name for x in self.artists)


@dataclass(kw_only=True)
class Track(MediaItem):
    """Model for a track."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    artists: UniqueList[Artist | ItemMapping] = field(default_factory=UniqueList)
    album: Album | ItemMapping | None = None  # required for album tracks
    disc_number: int = 0  # required for album tracks
    track_number: int = 0  # required for album tracks

    @property
    def has_chapters(self) -> bool:
        """
        Return boolean if this Track has chapters.

        This is often an indicator that this track is an episode from a
        Podcast or AudioBook.
        """
        if not self.metadata:
            return False
        if not self.metadata.chapters:
            return False
        return len(self.metadata.chapters) > 1

    @property
    def image(self) -> MediaItemImage | None:
        """Return (first) image from metadata (prefer album)."""
        if isinstance(self.album, Album) and self.album.image:
            return self.album.image
        return super().image

    @property
    def artist_str(self) -> str:
        """Return (combined) artist string for track."""
        return "/".join(x.name for x in self.artists)


@dataclass(kw_only=True)
class PlaylistTrack(Track):
    """
    Model for a track on a playlist.

    Same as regular Track but with explicit and required definition of position.
    """

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    position: int


@dataclass(kw_only=True)
class Playlist(MediaItem):
    """Model for a playlist."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.PLAYLIST
    owner: str = ""
    is_editable: bool = False

    # cache_checksum: optional value to (in)validate cache
    # detect changes to the playlist tracks listing
    cache_checksum: str | None = None


@dataclass(kw_only=True)
class Radio(MediaItem):
    """Model for a radio station."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.RADIO
    duration: int = 172800


@dataclass(kw_only=True)
class BrowseFolder(MediaItem):
    """Representation of a Folder used in Browse (which contains media items)."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    media_type: MediaType = MediaType.FOLDER
    # path: the path (in uri style) to/for this browse folder
    path: str = ""
    # label: a labelid that needs to be translated by the frontend
    label: str = ""
    provider_mappings: set[ProviderMapping] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Call after init."""
        super().__post_init__()
        if not self.path:
            self.path = f"{self.provider}://{self.item_id}"
        if not self.provider_mappings:
            self.provider_mappings.add(
                ProviderMapping(
                    item_id=self.item_id,
                    provider_domain=self.provider,
                    provider_instance=self.provider,
                )
            )


MediaItemType = Artist | Album | PlaylistTrack | Track | Radio | Playlist | BrowseFolder


@dataclass(kw_only=True)
class SearchResults(DataClassDictMixin):
    """Model for results from a search query."""

    artists: Sequence[Artist | ItemMapping] = field(default_factory=list)
    albums: Sequence[Album | ItemMapping] = field(default_factory=list)
    tracks: Sequence[Track | ItemMapping] = field(default_factory=list)
    playlists: Sequence[Playlist | ItemMapping] = field(default_factory=list)
    radio: Sequence[Radio | ItemMapping] = field(default_factory=list)


def media_from_dict(media_item: dict[str, Any]) -> MediaItemType | ItemMapping:
    """Return MediaItem from dict."""
    if "provider_mappings" not in media_item:
        return ItemMapping.from_dict(media_item)
    if media_item["media_type"] == "artist":
        return Artist.from_dict(media_item)
    if media_item["media_type"] == "album":
        return Album.from_dict(media_item)
    if media_item["media_type"] == "track":
        return Track.from_dict(media_item)
    if media_item["media_type"] == "playlist":
        return Playlist.from_dict(media_item)
    if media_item["media_type"] == "radio":
        return Radio.from_dict(media_item)
    raise InvalidDataError("Unknown media type")


def is_track(val: MediaItem) -> TypeGuard[Track]:
    """Return true if this MediaItem is a track."""
    return val.media_type == MediaType.TRACK
