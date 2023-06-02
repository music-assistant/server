"""Models and helpers for media items."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from time import time
from typing import Any

from mashumaro import DataClassDictMixin

from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.helpers.uri import create_uri
from music_assistant.common.helpers.util import create_sort_name, merge_lists
from music_assistant.common.models.enums import (
    AlbumType,
    ContentType,
    ImageType,
    LinkType,
    MediaType,
)

MetadataTypes = int | bool | str | list[str]

JSON_KEYS = ("artists", "artist", "albums", "metadata", "provider_mappings")
JOINED_KEYS = ("barcode", "isrc")


@dataclass(frozen=True)
class ProviderMapping(DataClassDictMixin):
    """Model for a MediaItem's provider mapping details."""

    item_id: str
    provider_domain: str
    provider_instance: str
    available: bool = True
    # quality details (streamable content only)
    content_type: ContentType = ContentType.UNKNOWN
    sample_rate: int = 44100
    bit_depth: int = 16
    bit_rate: int = 320
    # optional details to store provider specific details
    details: str | None = None
    # url = link to provider details page if exists
    url: str | None = None

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

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.provider_instance, self.item_id))

    def __eq__(self, other: ProviderMapping) -> bool:
        """Check equality of two items."""
        return self.provider_instance == other.provider_instance and self.item_id == other.item_id


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


@dataclass
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
    lyrics: str | None = None
    ean: str | None = None
    label: str | None = None
    links: set[MediaItemLink] | None = None
    chapters: list[MediaItemChapter] | None = None
    performers: set[str] | None = None
    preview: str | None = None
    replaygain: float | None = None
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
            if cur_val is None or allow_overwrite:  # noqa: SIM114
                setattr(self, fld.name, new_val)
            elif isinstance(cur_val, list):
                new_val = merge_lists(cur_val, new_val)
                setattr(self, fld.name, new_val)
            elif isinstance(cur_val, set):
                new_val = cur_val.update(new_val)
                setattr(self, fld.name, new_val)
            elif new_val and fld.name in ("checksum", "popularity", "last_refresh"):
                # some fields are always allowed to be overwritten
                # (such as checksum and last_refresh)
                setattr(self, fld.name, new_val)
        return self


@dataclass
class MediaItem(DataClassDictMixin):
    """Base representation of a media item."""

    item_id: str
    provider: str  # provider instance id or provider domain
    name: str
    provider_mappings: set[ProviderMapping] = field(default_factory=set)

    # optional fields below
    metadata: MediaItemMetadata = field(default_factory=MediaItemMetadata)
    in_library: bool = False
    media_type: MediaType = MediaType.UNKNOWN
    # sort_name and uri are auto generated, do not override unless really needed
    sort_name: str | None = None
    uri: str | None = None
    # timestamps to determine when the item was added/modified to the db
    timestamp_added: int = 0
    timestamp_modified: int = 0

    def __post_init__(self):
        """Call after init."""
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)
        if not self.sort_name:
            self.sort_name = create_sort_name(self.name)

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create MediaItem object from database row."""
        db_row = dict(db_row)
        db_row["provider"] = "database"
        for key in JSON_KEYS:
            if key in db_row and db_row[key] is not None:
                db_row[key] = json_loads(db_row[key])
        for key in JOINED_KEYS:
            if key not in db_row:
                continue
            db_row[key] = db_row[key].strip()
            db_row[key] = db_row[key].split(";") if db_row[key] else []
        if "in_library" in db_row:
            db_row["in_library"] = bool(db_row["in_library"])
        if db_row.get("albums"):
            db_row["album"] = db_row["albums"][0]
            db_row["disc_number"] = db_row["albums"][0]["disc_number"]
            db_row["track_number"] = db_row["albums"][0]["track_number"]
        db_row["item_id"] = str(db_row["item_id"])
        return cls.from_dict(db_row)

    def to_db_row(self) -> dict:
        """Create dict from item suitable for db."""

        def get_db_value(key, value) -> Any:
            """Transform value for db storage."""
            if key in JSON_KEYS:
                return json_dumps(value)
            if key in JOINED_KEYS:
                return ";".join(value)
            return value

        return {
            key: get_db_value(key, value)
            for key, value in self.to_dict().items()
            if key
            not in [
                "item_id",
                "provider",
                "media_type",
                "uri",
                "album",
                "position",
                "track_number",
                "disc_number",
            ]
        }

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

    def add_provider_mapping(self, prov_mapping: ProviderMapping) -> None:
        """Add provider ID, overwrite existing entry."""
        self.provider_mappings = {
            x
            for x in self.provider_mappings
            if not (
                x.item_id == prov_mapping.item_id
                and x.provider_instance == prov_mapping.provider_instance
            )
        }
        self.provider_mappings.add(prov_mapping)


@dataclass
class ItemMapping(DataClassDictMixin):
    """Representation of a minimized item object."""

    media_type: MediaType
    item_id: str
    provider: str  # provider instance id or provider domain
    name: str
    sort_name: str | None = None
    uri: str | None = None
    version: str = ""
    available: bool = True

    @classmethod
    def from_item(cls, item: MediaItem):
        """Create ItemMapping object from regular item."""
        result = cls.from_dict(item.to_dict())
        result.available = item.available
        return result

    def __post_init__(self):
        """Call after init."""
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)
        if not self.sort_name:
            self.sort_name = create_sort_name(self.name)

    def __hash__(self) -> int:
        """Return custom hash."""
        return hash((self.media_type.value, self.provider, self.item_id))


@dataclass
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.ARTIST
    musicbrainz_id: str | None = None


@dataclass
class Album(MediaItem):
    """Model for an album."""

    media_type: MediaType = MediaType.ALBUM
    version: str = ""
    year: int | None = None
    artists: list[Artist | ItemMapping] = field(default_factory=list)
    album_type: AlbumType = AlbumType.UNKNOWN
    barcode: set[str] = field(default_factory=set)
    musicbrainz_id: str | None = None  # release group id


@dataclass
class DbAlbum(Album):
    """Model for an album when retrieved from the db."""

    artists: list[ItemMapping] = field(default_factory=list)


@dataclass
class TrackAlbumMapping(ItemMapping):
    """Model for a track that is mapped to an album."""

    disc_number: int | None = None
    track_number: int | None = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))

    @classmethod
    def from_item(
        cls,
        item: MediaItemType | ItemMapping,
        disc_number: int | None = None,
        track_number: int | None = None,
    ) -> TrackAlbumMapping:
        """Create TrackAlbumMapping object from regular item."""
        result = super().from_item(item)
        result.disc_number = disc_number
        result.track_number = track_number
        return result


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    isrc: set[str] = field(default_factory=set)
    musicbrainz_id: str | None = None  # Recording ID
    artists: list[Artist | ItemMapping] = field(default_factory=list)
    # album track only
    album: Album | ItemMapping | None = None
    albums: list[TrackAlbumMapping] = field(default_factory=list)
    disc_number: int | None = None
    track_number: int | None = None
    # playlist track only
    position: int | None = None

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))

    @property
    def image(self) -> MediaItemImage | None:
        """Return (first/random) image/thumb from metadata (if any)."""
        if image := super().image:
            return image
        # fallback to album image (use getattr to guard for ItemMapping)
        if self.album:
            return getattr(self.album, "image", None)
        return None

    @property
    def has_chapters(self) -> bool:
        """
        Return boolean if this Track has chapters.

        This is often an indicator that this track is an episode from a
        Podcast or AudioBook.
        """
        return self.metadata and self.metadata.chapters and len(self.metadata.chapters) > 1


@dataclass
class DbTrack(Track):
    """Model for a track when retrieved from the db."""

    artists: list[ItemMapping] = field(default_factory=list)
    # album track only
    album: ItemMapping | None = None
    albums: list[TrackAlbumMapping] = field(default_factory=list)


@dataclass
class Playlist(MediaItem):
    """Model for a playlist."""

    media_type: MediaType = MediaType.PLAYLIST
    owner: str = ""
    is_editable: bool = False

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))


@dataclass
class Radio(MediaItem):
    """Model for a radio station."""

    media_type: MediaType = MediaType.RADIO
    duration: int = 172800

    def to_db_row(self) -> dict:
        """Create dict from item suitable for db."""
        val = super().to_db_row()
        val.pop("duration", None)
        return val

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))


@dataclass
class BrowseFolder(MediaItem):
    """Representation of a Folder used in Browse (which contains media items)."""

    media_type: MediaType = MediaType.FOLDER
    # path: the path (in uri style) to/for this browse folder
    path: str = ""
    # label: a labelid that needs to be translated by the frontend
    label: str = ""
    # subitems of this folder when expanding
    items: list[MediaItemType | BrowseFolder] | None = None

    def __post_init__(self):
        """Call after init."""
        super().__post_init__()
        if not self.path:
            self.path = f"{self.provider}://{self.item_id}"


MediaItemType = Artist | Album | Track | Radio | Playlist | BrowseFolder


@dataclass
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


@dataclass
class SearchResults(DataClassDictMixin):
    """Model for results from a search query."""

    artists: list[Artist | ItemMapping] = field(default_factory=list)
    albums: list[Album | ItemMapping] = field(default_factory=list)
    tracks: list[Track | ItemMapping] = field(default_factory=list)
    playlists: list[Playlist | ItemMapping] = field(default_factory=list)
    radio: list[Radio | ItemMapping] = field(default_factory=list)


def media_from_dict(media_item: dict) -> MediaItemType:
    """Return MediaItem from dict."""
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


@dataclass
class StreamDetails(DataClassDictMixin):
    """Model for streamdetails."""

    # NOTE: the actual provider/itemid of the streamdetails may differ
    # from the connected media_item due to track linking etc.
    # the streamdetails are only used to provide details about the content
    # that is going to be streamed.

    # mandatory fields
    provider: str
    item_id: str
    content_type: ContentType
    media_type: MediaType = MediaType.TRACK
    sample_rate: int = 44100
    bit_depth: int = 16
    channels: int = 2
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

    def __str__(self):
        """Return pretty printable string of object."""
        return self.uri

    @property
    def uri(self) -> str:
        """Return uri representation of item."""
        return f"{self.provider}://{self.media_type.value}/{self.item_id}"
