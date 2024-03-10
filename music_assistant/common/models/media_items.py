"""Models and helpers for media items."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from time import time
from typing import Any, Self

from mashumaro import DataClassDictMixin

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
            return int(self.sample_rate / 1000) + self.bit_depth
        # lossy content, bit_rate is most important score
        # but prefer some codecs over others
        score = self.bit_rate / 100
        if self.content_type in (ContentType.AAC, ContentType.OGG):
            score += 1
        return int(score)

    @property
    def pcm_sample_size(self) -> int:
        """Return the PCM sample size."""
        return int(self.sample_rate * (self.bit_depth / 8) * self.channels)


@dataclass(frozen=True, kw_only=True)
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
        return self.audio_format.quality

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
    # set to instance_id of provider if the path needs to be resolved
    # if the path is just a plain (remotely accessible) URL, set it to 'url'
    provider: str = "url"

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.type.value, self.path))

    def __eq__(self, other: MediaItemImage) -> bool:
        """Check equality of two items."""
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

    def __eq__(self, other: MediaItemChapter) -> bool:
        """Check equality of two items."""
        return self.chapter_id == other.chapter_id


@dataclass(kw_only=True)
class MediaItemMetadata(DataClassDictMixin):
    """Model for a MediaItem's metadata."""

    description: str | None = None
    review: str | None = None
    explicit: bool | None = None
    images: list[MediaItemImage] | None = None
    genres: set[str] | None = None
    mood: str | None = None
    style: str | None = None
    copyright: str | None = None
    lyrics: str | None = None  # tracks only
    label: str | None = None
    links: set[MediaItemLink] | None = None
    chapters: list[MediaItemChapter] | None = None
    performers: set[str] | None = None
    preview: str | None = None
    popularity: int | None = None
    # last_refresh: timestamp the (full) metadata was last collected
    last_refresh: int | None = None
    # checksum: optional value to detect changes (e.g. playlists)
    checksum: str | None = None

    def update(
        self,
        new_values: MediaItemMetadata,
        allow_overwrite: bool = True,
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
            elif isinstance(cur_val, set) and isinstance(new_val, list):
                new_val = cur_val.update(new_val)
                setattr(self, fld.name, new_val)
            elif new_val and fld.name in ("checksum", "popularity", "last_refresh"):
                # some fields are always allowed to be overwritten
                # (such as checksum and last_refresh)
                setattr(self, fld.name, new_val)
            elif cur_val is None or (allow_overwrite and new_val):
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
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)
        if not self.sort_name:
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

    provider_mappings: set[ProviderMapping]
    # optional fields below
    metadata: MediaItemMetadata = field(default_factory=MediaItemMetadata)
    favorite: bool = False
    # timestamps to determine when the item was added/modified to the db
    timestamp_added: int = 0
    timestamp_modified: int = 0

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

    @classmethod
    def from_item_mapping(cls: type, item: ItemMapping) -> Self:
        """Instantiate MediaItem from ItemMapping."""
        # NOTE: This will not work for albums and tracks!
        return cls.from_dict(
            {
                **item.to_dict(),
                "provider_mappings": {
                    "item_id": item.item_id,
                    "provider_domain": item.provider,
                    "provider_instance": item.provider,
                    "available": item.available,
                },
            }
        )


@dataclass(kw_only=True)
class ItemMapping(_MediaItemBase):
    """Representation of a minimized item object."""

    available: bool = True

    @classmethod
    def from_item(cls, item: MediaItem) -> ItemMapping:
        """Create ItemMapping object from regular item."""
        return cls.from_dict(item.to_dict())


@dataclass(kw_only=True)
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.ARTIST


@dataclass(kw_only=True)
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.ALBUM
    version: str = ""
    year: int | None = None
    artists: list[Artist | ItemMapping] = field(default_factory=list)
    album_type: AlbumType = AlbumType.UNKNOWN


@dataclass(kw_only=True)
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    artists: list[Artist | ItemMapping] = field(default_factory=list)
    album: Album | ItemMapping | None = None  # optional

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
    """Model for a track on an album."""

    album: Album | ItemMapping  # required
    disc_number: int = 0
    track_number: int = 0


@dataclass(kw_only=True)
class PlaylistTrack(Track):
    """Model for a track on a playlist."""

    position: int  # required


@dataclass(kw_only=True)
class Playlist(MediaItem):
    """Model for a playlist."""

    media_type: MediaType = MediaType.PLAYLIST
    owner: str = ""
    is_editable: bool = False


@dataclass(kw_only=True)
class Radio(MediaItem):
    """Model for a radio station."""

    media_type: MediaType = MediaType.RADIO
    duration: int = 172800


@dataclass(kw_only=True)
class BrowseFolder(MediaItem):
    """Representation of a Folder used in Browse (which contains media items)."""

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


MediaItemType = Artist | Album | Track | Radio | Playlist | BrowseFolder


@dataclass(kw_only=True)
class PagedItems(DataClassDictMixin):
    """Model for a paged listing."""

    items: list[MediaItemType]
    count: int
    limit: int
    offset: int
    total: int | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any], item_type: type) -> PagedItems:
        """Parse PagedItems object including correct item type."""
        return PagedItems(
            items=[item_type.from_dict(x) for x in raw["items"]],
            count=raw["count"],
            limit=raw["limit"],
            offset=raw["offset"],
            total=raw["total"],
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


@dataclass(kw_only=True)
class StreamDetails(DataClassDictMixin):
    """Model for streamdetails."""

    # NOTE: the actual provider/itemid of the streamdetails may differ
    # from the connected media_item due to track linking etc.
    # the streamdetails are only used to provide details about the content
    # that is going to be streamed.

    # mandatory fields
    provider: str
    item_id: str
    audio_format: AudioFormat
    media_type: MediaType = MediaType.TRACK

    # stream_title: radio streams can optionally set this field
    stream_title: str | None = None
    # duration of the item to stream, copied from media_item if omitted
    duration: int | None = None
    # total size in bytes of the item, calculated at eof when omitted
    size: int | None = None
    # expires: timestamp this streamdetails expire
    expires: float = time() + 3600
    # data: provider specific data (not exposed externally)
    data: Any = None
    # if the url/file is supported by ffmpeg directly, use direct stream
    direct: str | None = None
    # bool to indicate that the providers 'get_audio_stream' supports seeking of the item
    can_seek: bool = True
    # callback: optional callback function (or coroutine) to call when the stream completes.
    # needed for streaming provivders to report what is playing
    # receives the streamdetails as only argument from which to grab
    # details such as seconds_streamed.
    callback: Any = None

    # the fields below will be set/controlled by the streamcontroller
    queue_id: str | None = None
    seconds_streamed: float | None = None
    seconds_skipped: float | None = None
    gain_correct: float | None = None
    loudness: float | None = None

    def __post_serialize__(self, d: dict[Any, Any]) -> dict[Any, Any]:
        """Exclude internal fields from dict."""
        d.pop("data")
        d.pop("direct")
        d.pop("expires")
        d.pop("queue_id")
        d.pop("callback")
        return d

    def __str__(self) -> str:
        """Return pretty printable string of object."""
        return self.uri

    @property
    def uri(self) -> str:
        """Return uri representation of item."""
        return f"{self.provider}://{self.media_type.value}/{self.item_id}"
