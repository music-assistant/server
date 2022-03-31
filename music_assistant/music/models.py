"""Models and helpers for media items."""
from __future__ import annotations
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntEnum

from typing import Any, Dict, Generic, List, Mapping, Tuple, TypeVar
import ujson
from mashumaro import DataClassDictMixin

from music_assistant.helpers.cache import cached
from music_assistant.helpers.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant.helpers.typing import MusicAssistant


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
    quality: MediaQuality = MediaQuality.UNKNOWN
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
    provider_ids: List[MediaItemProviderId] = field(default_factory=list)
    in_library: bool = False
    media_type: MediaType = MediaType.UNKNOWN
    uri: str = ""

    def __post_init__(self):
        """Call after init."""
        if not self.uri:
            self.uri = create_uri(self.media_type, self.provider, self.item_id)

    @classmethod
    def from_db_row(cls, db_row: Mapping):
        """Create MediaItem object from database row."""
        db_row = dict(db_row)
        for key in ["artists", "artist", "metadata", "provider_ids"]:
            if key in db_row:
                db_row[key] = ujson.loads(db_row[key])
        db_row["provider"] = "database"
        if "in_library" in db_row:
            db_row["in_library"] = bool(db_row["in_library"])
        if db_row.get("albums"):
            db_row["album"] = db_row["albums"][0]
        db_row["item_id"] = str(db_row["item_id"])
        return cls.from_dict(db_row)

    def to_db_row(self) -> dict:
        """Create dict from item suitable for db."""
        return {
            key: ujson.dumps(val) if isinstance(val, (list, dict)) else val
            for key, val in self.to_dict().items()
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
    def from_item(cls, item: "MediaItemType"):
        """Create ItemMapping object from regular item."""
        return cls.from_dict(item.to_dict())

    def __hash__(self):
        """Return custom hash."""
        return hash((self.media_type, self.provider, self.item_id))


@dataclass
class Artist(MediaItem):
    """Model for an artist."""

    media_type: MediaType = MediaType.ARTIST
    musicbrainz_id: str = ""


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
    year: int = 0
    artist: ItemMapping | Artist | None = None
    album_type: AlbumType = AlbumType.UNKNOWN
    upc: str = ""

    def __hash__(self):
        """Return custom hash."""
        return hash((self.provider, self.item_id))


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    isrc: str = ""
    artists: List[ItemMapping | Artist] = field(default_factory=list)
    # album track only
    album: ItemMapping | Album | None = None
    disc_number: int | None = None
    track_number: int | None = None
    # playlist track only
    position: int | None = None

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


def create_uri(media_type: MediaType, provider: str, item_id: str):
    """Create uri for mediaitem."""
    return f"{provider}://{media_type.value}/{item_id}"


MediaItemType = Artist | Album | Track | Radio | Playlist

ItemCls = TypeVar("ItemCls", bound="MediaControllerBase")


class MediaControllerBase(Generic[ItemCls], metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: MediaItemType
    db_table: str

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.logger = mass.logger.getChild(f"music.{self.media_type.value}")

    @abstractmethod
    async def setup(self):
        """Async initialize of module."""

    @abstractmethod
    async def add(self, item: ItemCls) -> ItemCls:
        """Add item to local db and return the database item."""
        raise NotImplementedError

    async def library(self) -> List[ItemCls]:
        """Get all in-library items."""
        match = {"in_library": True}
        return [
            self.item_cls.from_db_row(db_row)
            for db_row in await self.mass.database.get_rows(self.db_table, match)
        ]

    async def get(
        self,
        provider_item_id: str,
        provider: str,
        refresh: bool = False,
        lazy: bool = True,
        details: ItemCls = None,
    ) -> ItemCls:
        """Return (full) details for a single media item."""
        db_item = await self.get_db_item_by_prov_id(provider, provider_item_id)
        if db_item and len(db_item.provider_ids) != self.mass.music.provider_count:
            # force refresh of item if provider count changed
            refresh = True
        if db_item and refresh:
            provider, provider_item_id = await self.get_provider_id(db_item)
        elif db_item:
            return db_item
        if not details:
            details = await self.get_provider_item(provider_item_id, provider)
        if not lazy:
            return await self.add(details)
        self.mass.tasks.add(f"Add {details.uri} to database", self.add, details)
        return db_item if db_item else details

    async def search(
        self, search_query: str, provider_id: str, limit: int = 25
    ) -> List[ItemCls]:
        """Search database or provider with given query."""
        if provider_id == "database":
            return [
                self.item_cls.from_db_row(db_row)
                for db_row in await self.mass.database.search(
                    self.db_table, search_query
                )
            ]

        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            return {}
        cache_key = (
            f"{provider_id}.search.{self.media_type.value}.{search_query}.{limit}"
        )
        return await cached(
            self.mass.cache,
            cache_key,
            provider.search,
            search_query,
            [self.media_type],
            limit,
        )

    async def add_to_library(self, provider_item_id: str, provider: str) -> None:
        """Add an item to the library."""
        # make sure we have a valid full item
        db_item = await self.get(provider_item_id, provider, lazy=False)
        # add to provider libraries
        for prov_id in db_item.provider_ids:
            if prov := self.mass.music.get_provider(prov_id.provider):
                await prov.library_add(prov_id.item_id, self.media_type)
        # mark as library item in internal db
        if not db_item.in_library:
            await self.set_db_library(db_item.item_id, True)

    async def remove_from_library(self, provider_item_id: str, provider: str) -> None:
        """Remove item from the library."""
        # make sure we have a valid full item
        db_item = await self.get(provider_item_id, provider, lazy=False)
        # add to provider's libraries
        for prov_id in db_item.provider_ids:
            if prov := self.mass.music.get_provider(prov_id.provider):
                await prov.library_remove(prov_id.item_id, self.media_type)
        # unmark as library item in internal db
        if db_item.in_library:
            await self.set_db_library(db_item.item_id, False)

    async def get_provider_id(self, item: ItemCls) -> Tuple[str, str]:
        """Return provider and item id."""
        if item.provider == "database":
            # make sure we have a full object
            item = await self.get_db_item(item.item_id)
        for prov in item.provider_ids:
            # returns the first provider that is available
            if not prov.available:
                continue
            if self.mass.music.get_provider(prov.provider):
                return (prov.provider, prov.item_id)
        return None, None

    async def get_db_items(self, custom_query: str | None = None) -> List[ItemCls]:
        """Fetch all records from database."""
        if custom_query is not None:
            func = self.mass.database.get_rows_from_query(custom_query)
        else:
            func = self.mass.database.get_rows(self.db_table)
        return [self.item_cls.from_db_row(db_row) for db_row in await func]

    async def get_db_item(self, item_id: int) -> ItemCls:
        """Get record by id."""
        match = {"item_id": int(item_id)}
        if db_row := await self.mass.database.get_row(self.db_table, match):
            return self.item_cls.from_db_row(db_row)
        return None

    async def get_db_item_by_prov_id(
        self,
        provider: str,
        provider_item_id: str,
    ) -> ItemCls | None:
        """Get the database album for the given prov_id."""
        if provider == "database":
            return await self.get_db_item(provider_item_id)
        if item_id := await self.mass.music.get_provider_mapping(
            self.media_type, provider, provider_item_id
        ):
            return await self.get_db_item(item_id)
        return None

    async def set_db_library(self, item_id: int, in_library: bool) -> None:
        """Set the in-library bool on a database item."""
        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {"in_library": in_library},
        )

    async def get_provider_item(self, item_id: str, provider_id: str) -> ItemCls:
        """Return item details for the given provider item id."""
        if provider_id == "database":
            return await self.get_db_item(item_id)
        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            raise ProviderUnavailableError(f"Provider {provider_id} is not available!")
        cache_key = f"{provider_id}.get_{self.media_type.value}.{item_id}"
        item = await cached(
            self.mass.cache, cache_key, provider.get_item, self.media_type, item_id
        )
        if not item:
            raise MediaNotFoundError(
                f"{self.media_type.value} {item_id} not found on provider {provider_id}"
            )
        return item


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
    PCM_S16LE = "s16le"  # PCM signed 16-bit little-endian
    PCM_S24LE = "s24le"  # PCM signed 24-bit little-endian
    PCM_S32LE = "s32le"  # PCM signed 32-bit little-endian
    PCM_F32LE = "f32le"  # PCM 32-bit floating-point little-endian
    PCM_F64LE = "f64le"  # PCM 64-bit floating-point little-endian

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
    loudness: float | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    media_type: MediaType = MediaType.TRACK
    queue_id: str = None

    def __post_serialize__(self, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Exclude internal fields from dict."""
        d.pop("path")
        d.pop("details")
        return d

    def __str__(self):
        """Return pretty printable string of object."""
        return f"{self.type.value}/{self.content_type.value} - {self.provider}/{self.item_id}"
