"""Models and helpers for media items."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

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
        seen = set()
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

    def __post_init__(self):
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

    def __eq__(self, other: AudioFormat) -> bool:
        """Check equality of two items."""
        if not other:
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
        if "filesystem" in self.provider_domain:
            # always prefer local file over online media
            quality += 1
        return quality

    def __post_init__(self):
        """Call after init."""
        # having items for unavailable providers can have all sorts
        # of unpredictable results so ensure we have accurate availability status
        if not (available_providers := get_global_cache_value("unique_providers")):
            # this is probably the client
            self.available = self.available
            return
        if TYPE_CHECKING:
            available_providers = cast(set[str], available_providers)
        if not available_providers.intersection({self.provider_domain, self.provider_instance}):
            self.available = False

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.provider_instance, self.item_id))

    def __eq__(self, other: ProviderMapping) -> bool:
        """Check equality of two items."""
        if not other:
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

    def __eq__(self, other: MediaItemLink) -> bool:
        """Check equality of two items."""
        return self.url == other.url


@dataclass(frozen=True, kw_only=True)
class MediaItemImage(DataClassDictMixin):
    """Model for a image."""

    type: ImageType
    path: str
    provider: str
    remotely_accessible: bool = False  # url that is accessible from anywhere

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.type.value, self.path))

    def __eq__(self, other: MediaItemImage) -> bool:
        """Check equality of two items."""
        return self.__hash__() == other.__hash__()

    @classmethod
    def __pre_deserialize__(cls, d: dict[Any, Any]) -> dict[Any, Any]:
        """Handle actions before deserialization."""
        # migrate from url provider --> builtin
        # TODO: remove this after 2.0 is launched
        if d["provider"] == "url":
            d["provider"] = "builtin"
            d["remotely_accessible"] = True
        return d


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

    def __eq__(self, other: MediaItemChapter) -> bool:
        """Check equality of two items."""
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
    # cache_checksum: optional value to (in)validate cache / detect changes (used for playlists)
    cache_checksum: str | None = None
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
                new_val = merge_lists(cur_val, new_val)
                setattr(self, fld.name, new_val)
            elif isinstance(cur_val, set) and isinstance(new_val, set | list | tuple):
                new_val = cur_val.update(new_val)
                setattr(self, fld.name, new_val)
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
    # sort_name and uri are auto generated, do not override unless really needed
    sort_name: str | None = None
    uri: str | None = None
    external_ids: set[tuple[ExternalID, str]] = field(default_factory=set)
    media_type: MediaType = MediaType.UNKNOWN

    def __post_init__(self):
        """Call after init."""
        if self.name is None:
            # we've got some reports where the name was empty, causing weird issues.
            # e.g. here: https://github.com/music-assistant/hass-music-assistant/issues/1515
            self.name = "[Unknown]"
        if self.uri is None:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)
        if self.sort_name is None:
            self.sort_name = create_sort_name(self.name)

    @property
    def mbid(self) -> str | None:
        """Return MusicBrainz ID."""
        return self.get_external_id(ExternalID.MUSICBRAINZ)

    @mbid.setter
    def mbid(self, value: str) -> None:
        """Set MusicBrainz External ID."""
        if not value:
            return
        if not is_valid_uuid(value):
            msg = f"Invalid MusicBrainz identifier: {value}"
            raise InvalidDataError(msg)
        if existing := next((x for x in self.external_ids if x[0] == ExternalID.MUSICBRAINZ), None):
            # Musicbrainz ID is unique so remove existing entry
            self.external_ids.remove(existing)
        self.external_ids.add((ExternalID.MUSICBRAINZ, value))

    def get_external_id(self, external_id_type: ExternalID) -> str | None:
        """Get (the first instance) of given External ID or None if not found."""
        for ext_id in self.external_ids:
            if ext_id[0] != external_id_type:
                continue
            return ext_id[1]
        return None

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash(self.uri)

    def __eq__(self, other: MediaItem | ItemMapping) -> bool:
        """Check equality of two items."""
        return self.uri == other.uri


@dataclass(kw_only=True)
class MediaItem(_MediaItemBase):
    """Base representation of a media item."""

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    provider_mappings: set[ProviderMapping]
    # optional fields below
    metadata: MediaItemMetadata = field(default_factory=MediaItemMetadata)
    favorite: bool = False
    position: int | None = None  # required for playlist tracks, optional for all other

    @property
    def available(self):
        """Return (calculated) availability."""
        return any(x.available for x in self.provider_mappings)

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
    def from_item(cls, item: MediaItem) -> ItemMapping:
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
    album: Album | ItemMapping | None = None  # optional
    disc_number: int | None = None  # required for album tracks
    track_number: int | None = None  # required for album tracks

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))

    @property
    def has_chapters(self) -> bool:
        """
        Return boolean if this Track has chapters.

        This is often an indicator that this track is an episode from a
        Podcast or AudioBook.
        """
        return self.metadata and self.metadata.chapters and len(self.metadata.chapters) > 1

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
class AlbumTrack(Track):
    """
    Model for a track on an album.

    Same as regular Track but with explicit and required definitions of
    album, disc_number and track_number
    """

    __hash__ = _MediaItemBase.__hash__
    __eq__ = _MediaItemBase.__eq__

    album: Album | ItemMapping
    disc_number: int
    track_number: int

    @classmethod
    def from_track(
        cls: type,
        track: Track,
        album: Album | None = None,
        disc_number: int | None = None,
        track_number: int | None = None,
    ) -> Self:
        """Cast Track to AlbumTrack."""
        if album is None:
            album = track.album
        if disc_number is None:
            disc_number = track.disc_number
        if track_number is None:
            track_number = track.track_number
        # let mushmumaro instantiate a new object - this will ensure that valididation takes place
        return AlbumTrack.from_dict(
            {
                **track.to_dict(),
                "album": album.to_dict(),
                "disc_number": disc_number,
                "track_number": track_number,
            }
        )


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

    def __post_init__(self):
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


MediaItemType = (
    Artist | Album | PlaylistTrack | AlbumTrack | Track | Radio | Playlist | BrowseFolder
)


@dataclass(kw_only=True)
class SearchResults(DataClassDictMixin):
    """Model for results from a search query."""

    artists: list[Artist | ItemMapping] = field(default_factory=list)
    albums: list[Album | ItemMapping] = field(default_factory=list)
    tracks: list[Track | ItemMapping] = field(default_factory=list)
    playlists: list[Playlist | ItemMapping] = field(default_factory=list)
    radio: list[Radio | ItemMapping] = field(default_factory=list)


def media_from_dict(media_item: dict) -> MediaItemType:
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
    return MediaItem.from_dict(media_item)
