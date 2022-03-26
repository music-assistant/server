"""Models and helpers for media items."""

import logging
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, List, Mapping, Optional, OrderedDict, Tuple, Type

from mashumaro import DataClassDictMixin

from music_assistant.config.models import ConfigEntry, ConfigEntryType, ConfigItem
from music_assistant.constants import CONF_ENABLED
from music_assistant.helpers.cache import cached
from music_assistant.helpers.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant.helpers.typing import MusicAssistant, StreamDetails


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

    # @classmethod
    # def from_db_row(cls, db_row: Mapping):
    #     """Create MediaItem object from database row."""
    #     db_row = dict(db_row)
    #     for key in ["artists", "artist", "album", "metadata", "provider_ids", "albums"]:
    #         if key in db_row:
    #             db_row[key] = ujson.loads(db_row[key])
    #     db_row["provider"] = "database"
    #     if "in_library" in db_row:
    #         db_row["in_library"] = bool(db_row["in_library"])
    #     if db_row.get("albums"):
    #         db_row["album"] = db_row["albums"][0]
    #     db_row["item_id"] = str(db_row["item_id"])
    #     return cls.from_dict(db_row)

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
    artist: ItemMapping | None = None
    album_type: AlbumType = AlbumType.UNKNOWN
    upc: str = ""


@dataclass
class FullAlbum(Album):
    """Model for an album with full details."""

    artist: Artist | None = None


@dataclass
class Track(MediaItem):
    """Model for a track."""

    media_type: MediaType = MediaType.TRACK
    duration: int = 0
    version: str = ""
    isrc: str = ""
    artists: List[ItemMapping] = field(default_factory=list)
    albums: List[ItemMapping] = field(default_factory=list)
    # album track only
    album: ItemMapping | None = None
    disc_number: int = 0
    track_number: int = 0
    # playlist track only
    position: int = 0


@dataclass
class FullTrack(Track):
    """Model for an album with full details."""

    artists: List[Artist] = field(default_factory=list)
    albums: List[Album] = field(default_factory=list)
    album: Album | None = None


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


@dataclass
class SearchResult(DataClassDictMixin):
    """Model for Media Item Search result."""

    artists: List[Artist] = field(default_factory=list)
    albums: List[Album] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=list)
    radios: List[Radio] = field(default_factory=list)


DEFAULT_CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_ENABLED,
        entry_type=ConfigEntryType.BOOL,
        default_value=True,
        label="Enabled",
    )
]


def create_uri(media_type: MediaType, provider: str, item_id: str):
    """Create uri for mediaitem."""
    return f"{provider}://{media_type.value}/{item_id}"


class MusicProvider:
    """Model for a Music Provider."""

    def __init__(self, mass: MusicAssistant, id: str, name: str) -> None:
        """Initialize the provider."""
        # pylint: disable=redefined-builtin
        self.mass = mass
        self.logger = logging.getLogger(id)
        self._attr_id = id
        self._attr_name = name
        self._attr_available = False
        self._attr_supported_mediatypes: List[MediaType] = [
            MediaType.ALBUM,
            MediaType.ARTIST,
            MediaType.PLAYLIST,
            MediaType.RADIO,
            MediaType.TRACK,
        ]
        self.mass.config.register_config_entries(DEFAULT_CONFIG_ENTRIES)

    @abstractmethod
    async def setup(self) -> None:
        """
        Handle async initialization of the provider.

        Called at startup and when configuration changes.
        """

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return self._attr_id

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return self._attr_name

    @property
    def available(self) -> bool:
        """Return boolean if this provider is available/initialized."""
        return self._attr_available

    @property
    def config(self) -> OrderedDict[str, ConfigItem]:
        """Return the current configuration for this provider."""
        return self.mass.config.get_config(self._attr_id)

    @property
    def supported_mediatypes(self) -> List[MediaType]:
        """Return MediaTypes the provider supports."""
        return self._attr_supported_mediatypes

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> SearchResult:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        raise NotImplementedError

    async def get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from the provider."""
        if MediaType.ARTIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_playlists(self) -> List[Playlist]:
        """Retrieve library/subscribed playlists from the provider."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        if MediaType.RADIO in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if MediaType.ARTIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of all albums for the given artist."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if MediaType.RADIO in self.supported_mediatypes:
            raise NotImplementedError

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on succes."""
        raise NotImplementedError

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on succes."""
        raise NotImplementedError

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Add track(s) to playlist. Return true on succes."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Remove track(s) from playlist. Return true on succes."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError


MediaItemType = Artist | Album | Track | Radio | Playlist


class MediaControllerBase:
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    model: Type[MediaItemType]
    db_table: str

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.logger = mass.music.logger.getChild(self.media_type.value)

    @abstractmethod
    async def setup(self):
        """Async initialize of module."""

    async def library(self) -> List[MediaItemType]:
        """Get all in-library items."""
        match = {"in_library": True}
        return [
            self.model.from_dict(db_row)
            for db_row in await self.mass.database.get_rows(self.db_table, match)
        ]

    async def get(
        self,
        provider_item_id: str,
        provider: str,
        refresh: bool = False,
        lazy: bool = True,
        details: MediaItemType = None,
    ) -> MediaItemType:
        """Return item details for the given provider item id."""
        if provider == "database" and not refresh:
            return await self.get_db_item(provider_item_id)
        db_item = await self.get_db_item_by_prov_id(provider, provider_item_id)
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

    @abstractmethod
    async def add(self, item: MediaItemType) -> MediaItemType:
        """Add item to local db and return the database item."""
        raise NotImplementedError

    async def search(
        self, search_query: str, provider_id: str, limit: int = 25
    ) -> List[Artist | Album | Track | Playlist | Radio]:
        """Search database or provider with given query."""
        if provider_id == "database":
            return [
                self.model.from_dict(db_row)
                for db_row in await self.mass.database.search(
                    self.db_table, search_query
                )
            ]

        provider = self.mass.music.get_provider(provider_id)
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
        db_item = await self.get(
            provider_item_id, provider, lazy=False
        )
        # add to provider's libraries
        for prov_id in db_item.provider_ids:
            prov = self.mass.music.get_provider(prov_id.provider)
            if prov:
                await prov.library_add(prov_id.item_id, self.media_type)
        # mark as library item in internal db
        if not db_item.in_library:
            match = {"item_id": db_item.item_id}
            await self.mass.database.update(
                self.db_table,
                match,
                {
                    "in_library": True,
                },
            )

    async def remove_from_library(self, provider_item_id: str, provider: str) -> None:
        """Remove item from the library."""
        # make sure we have a valid full item
        db_item = await self.get(
            provider_item_id, provider, lazy=False
        )
        # add to provider's libraries
        for prov_id in db_item.provider_ids:
            prov = self.mass.music.get_provider(prov_id.provider)
            if prov:
                await prov.library_remove(prov_id.item_id, self.media_type)
        # unmark as library item in internal db
        if db_item.in_library:
            match = {"item_id": db_item.item_id}
            await self.mass.database.update(
                self.db_table,
                match,
                {
                    "in_library": False,
                },
            )

    async def get_provider_id(self, item: MediaItemType) -> Tuple[str, str]:
        """Return provider and item id."""
        if item.provider == "database":
            # make sure we have a full object
            item = await self.get_db_item_by_prov_id("database", item.item_id)
        for prov in item.provider_ids:
            # returns the first provider that is available
            if not prov.available:
                continue
            if self.mass.music.get_provider(prov.provider):
                return (prov.provider, prov.item_id)
        return None, None

    async def get_db_items(self) -> List[MediaItemType]:
        """Fetch all records from database."""
        return [
            self.model.from_dict(db_row)
            for db_row in await self.mass.database.get_rows(self.db_table)
        ]

    async def get_db_item(self, item_id: int) -> MediaItemType:
        """Get record by id."""
        match = {"item_id": item_id}
        if db_row := await self.mass.database.get_row(self.db_table, match):
            return self.model.from_dict(db_row)
        return None

    async def get_db_item_by_prov_id(
        self,
        provider: str,
        provider_item_id: str,
    ) -> MediaItemType | None:
        """Get the database album for the given prov_id."""
        if provider == "database":
            return await self.get_db_item(provider_item_id)
        if item_id := await self.mass.music.get_provider_mapping(
            self.media_type, provider, provider_item_id
        ):
            return await self.get_db_item(item_id)
        return None

    async def get_provider_item(self, item_id: str, provider_id: str) -> MediaItemType:
        """Return item details for the given provider item id."""
        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            raise ProviderUnavailableError(f"Provider {provider_id} is not available!")
        cache_key = f"{provider_id}.get_{self.media_type.value}.{item_id}"
        func = getattr(provider, f"get_{self.media_type.value}")
        item = await cached(self.mass.cache, cache_key, func, item_id)
        if not item:
            raise MediaNotFoundError(
                f"{self.media_type.value} {item_id} not found on provider {provider_id}"
            )
        return item
