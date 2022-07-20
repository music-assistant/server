"""MusicController: Orchestrates all data from music providers and sync to internal database."""
from __future__ import annotations

import asyncio
import statistics
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

from music_assistant.controllers.music.albums import AlbumsController
from music_assistant.controllers.music.artists import ArtistsController
from music_assistant.controllers.music.playlists import PlaylistController
from music_assistant.controllers.music.radio import RadioController
from music_assistant.controllers.music.tracks import TracksController
from music_assistant.helpers.database import TABLE_PLAYLOG, TABLE_TRACK_LOUDNESS
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.config import MusicProviderConfig
from music_assistant.models.enums import MediaType, MusicProviderFeature, ProviderType
from music_assistant.models.errors import (
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant.models.media_items import (
    BrowseFolder,
    MediaItem,
    MediaItemType,
    media_from_dict,
)
from music_assistant.models.music_provider import MusicProvider
from music_assistant.music_providers.filesystem import FileSystemProvider
from music_assistant.music_providers.qobuz import QobuzProvider
from music_assistant.music_providers.spotify import SpotifyProvider
from music_assistant.music_providers.tunein import TuneInProvider
from music_assistant.music_providers.url import PROVIDER_CONFIG as URL_CONFIG
from music_assistant.music_providers.url import URLProvider
from music_assistant.music_providers.ytmusic import YoutubeMusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PROV_MAP = {
    ProviderType.FILESYSTEM_LOCAL: FileSystemProvider,
    ProviderType.SPOTIFY: SpotifyProvider,
    ProviderType.QOBUZ: QobuzProvider,
    ProviderType.TUNEIN: TuneInProvider,
    ProviderType.YTMUSIC: YoutubeMusicProvider,
}


class MusicController:
    """Several helpers around the musicproviders."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.logger = mass.logger.getChild("music")
        self.mass = mass
        self.artists = ArtistsController(mass)
        self.albums = AlbumsController(mass)
        self.tracks = TracksController(mass)
        self.radio = RadioController(mass)
        self.playlists = PlaylistController(mass)
        self._providers: Dict[str, MusicProvider] = {}

    async def setup(self):
        """Async initialize of module."""
        # register providers
        for prov_conf in self.mass.config.providers:
            prov_cls = PROV_MAP[prov_conf.type]
            await self._register_provider(prov_cls(self.mass, prov_conf), prov_conf)
        # always register url provider
        await self._register_provider(URLProvider(self.mass, URL_CONFIG), URL_CONFIG)
        # add job to cleanup old records from db
        self.mass.add_job(
            self._cleanup_library(),
            "Cleanup removed items from database",
            allow_duplicate=False,
        )

    async def start_sync(
        self,
        media_types: Optional[Tuple[MediaType]] = None,
        prov_types: Optional[Tuple[ProviderType]] = None,
        schedule: Optional[float] = None,
    ) -> None:
        """
        Start running the sync of all registred providers.

        media_types: only sync these media types. None for all.
        prov_types: only sync these provider types. None for all.
        schedule: schedule syncjob every X hours, set to None for just a manual sync run.
        """

        async def do_sync():
            while True:
                for prov in self.providers:
                    if prov_types is not None and prov.type not in prov_types:
                        continue
                    self.mass.add_job(
                        prov.sync_library(media_types),
                        f"Library sync for provider {prov.name}",
                        allow_duplicate=False,
                    )
                if schedule is None:
                    return
                await asyncio.sleep(3600 * schedule)

        self.mass.create_task(do_sync())

    @property
    def provider_count(self) -> int:
        """Return count of all registered music providers."""
        return len(self._providers)

    @property
    def providers(self) -> Tuple[MusicProvider]:
        """Return all (available) music providers."""
        return tuple(x for x in self._providers.values() if x.available)

    def get_provider(self, provider_id: Union[str, ProviderType]) -> MusicProvider:
        """Return Music provider by id (or type)."""
        if prov := self._providers.get(provider_id):
            return prov
        for prov in self._providers.values():
            if provider_id in (prov.type, prov.id, prov.type.value):
                return prov
        raise ProviderUnavailableError(f"Provider {provider_id} is not available")

    async def search(
        self, search_query, media_types: List[MediaType], limit: int = 10
    ) -> List[MediaItemType]:
        """
        Perform global search for media items on all providers.

            :param search_query: Search query.
            :param media_types: A list of media_types to include.
            :param limit: number of items to return in the search (per type).
        """
        # include results from all music providers
        provider_ids = [item.id for item in self.providers]
        # TODO: sort by name and filter out duplicates ?
        return await asyncio.gather(
            *[
                self.search_provider(
                    search_query, media_types, provider_id=prov_id, limit=limit
                )
                for prov_id in provider_ids
            ]
        )

    async def search_provider(
        self,
        search_query: str,
        media_types: List[MediaType],
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[MediaItemType]:
        """
        Perform search on given provider.

            :param search_query: Search query
            :param provider_id: provider_id of the provider to perform the search on.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: number of items to return in the search (per type).
        """
        assert provider or provider_id, "Provider needs to be supplied"
        prov = self.get_provider(provider_id or provider)
        if MusicProviderFeature.SEARCH not in prov.supported_features:
            return []

        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")

        # prefer cache items (if any)
        cache_key = f"{prov.type.value}.search.{search_query}.{limit}"
        cache_key += "".join(media_types)

        if cache := await self.mass.cache.get(cache_key):
            return [media_from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.search(
            search_query,
            media_types,
            limit,
        )
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(
                cache_key, [x.to_dict() for x in items], expiration=86400 * 7
            )
        )
        return items

    async def browse(self, path: Optional[str] = None) -> BrowseFolder:
        """Browse Music providers."""
        # root level; folder per provider
        if not path or path == "root":
            return BrowseFolder(
                item_id="root",
                provider=ProviderType.DATABASE,
                path="root",
                label="browse",
                name="",
                items=[
                    BrowseFolder(
                        item_id="root",
                        provider=prov.type,
                        path=f"{prov.id}://",
                        name=prov.name,
                    )
                    for prov in self.providers
                    if MusicProviderFeature.BROWSE in prov.supported_features
                ],
            )
        # provider level
        provider_id = path.split("://", 1)[0]
        prov = self.get_provider(provider_id)
        return await prov.browse(path)

    async def get_item_by_uri(
        self, uri: str, force_refresh: bool = False, lazy: bool = True
    ) -> MediaItemType:
        """Fetch MediaItem by uri."""
        media_type, provider, item_id = parse_uri(uri)
        return await self.get_item(
            item_id=item_id,
            media_type=media_type,
            provider=provider,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    async def get_item(
        self,
        item_id: str,
        media_type: MediaType,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        force_refresh: bool = False,
        lazy: bool = True,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        if provider == ProviderType.URL or provider_id == "url":
            # handle special case of 'URL' MusicProvider which allows us to play regular url's
            return await self.get_provider(ProviderType.URL).parse_item(item_id)
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            provider_item_id=item_id,
            provider=provider,
            provider_id=provider_id,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    async def add_to_library(
        self,
        media_type: MediaType,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Add an item to the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.add_to_library(
            provider_item_id, provider=provider, provider_id=provider_id
        )

    async def remove_from_library(
        self,
        media_type: MediaType,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Remove item from the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.remove_from_library(
            provider_item_id, provider=provider, provider_id=provider_id
        )

    async def delete_db_item(
        self, media_type: MediaType, db_item_id: str, recursive: bool = False
    ) -> None:
        """Remove item from the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.delete_db_item(db_item_id, recursive)

    async def refresh_items(self, items: List[MediaItem]) -> None:
        """
        Refresh MediaItems to force retrieval of full info and matches.

        Creates background tasks to process the action.
        """
        for media_item in items:
            job_desc = f"Refresh metadata of {media_item.uri}"
            self.mass.add_job(self.refresh_item(media_item), job_desc)

    async def refresh_item(
        self,
        media_item: MediaItem,
    ):
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        try:
            return await self.get_item(
                media_item.item_id,
                media_item.media_type,
                provider=media_item.provider,
                force_refresh=True,
                lazy=False,
            )
        except MusicAssistantError:
            pass

        for item in await self.search(media_item.name, [media_item.media_type], 20):
            if item.available:
                await self.get_item(
                    item.item_id, item.media_type, item.provider, lazy=False
                )

    async def set_track_loudness(
        self, item_id: str, provider: ProviderType, loudness: int
    ):
        """List integrated loudness for a track in db."""
        await self.mass.database.insert(
            TABLE_TRACK_LOUDNESS,
            {"item_id": item_id, "provider": provider.value, "loudness": loudness},
            allow_replace=True,
        )

    async def get_track_loudness(
        self, provider_item_id: str, provider: ProviderType
    ) -> float | None:
        """Get integrated loudness for a track in db."""
        if result := await self.mass.database.get_row(
            TABLE_TRACK_LOUDNESS,
            {
                "item_id": provider_item_id,
                "provider": provider.value,
            },
        ):
            return result["loudness"]
        return None

    async def get_provider_loudness(self, provider: ProviderType) -> float | None:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        if provider == ProviderType.URL:
            # this is not a very good idea for random urls
            return None
        for db_row in await self.mass.database.get_rows(
            TABLE_TRACK_LOUDNESS,
            {
                "provider": provider.value,
            },
        ):
            all_items.append(db_row["loudness"])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(self, item_id: str, provider: ProviderType):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.mass.database.insert(
            TABLE_PLAYLOG,
            {"item_id": item_id, "provider": provider.value, "timestamp": timestamp},
            allow_replace=True,
        )

    async def library_add_items(self, items: List[MediaItem]) -> None:
        """
        Add media item(s) to the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            job_desc = f"Add {media_item.uri} to library"
            self.mass.add_job(
                self.add_to_library(
                    media_item.media_type, media_item.item_id, media_item.provider
                ),
                job_desc,
            )

    async def library_remove_items(self, items: List[MediaItem]) -> None:
        """
        Remove media item(s) from the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            job_desc = f"Remove {media_item.uri} from library"
            self.mass.add_job(
                self.remove_from_library(
                    media_item.media_type, media_item.item_id, media_item.provider
                ),
                job_desc,
            )

    def get_controller(
        self, media_type: MediaType
    ) -> ArtistsController | AlbumsController | TracksController | RadioController | PlaylistController:
        """Return controller for MediaType."""
        if media_type == MediaType.ARTIST:
            return self.artists
        if media_type == MediaType.ALBUM:
            return self.albums
        if media_type == MediaType.TRACK:
            return self.tracks
        if media_type == MediaType.RADIO:
            return self.radio
        if media_type == MediaType.PLAYLIST:
            return self.playlists

    async def _register_provider(
        self, provider: MusicProvider, conf: MusicProviderConfig
    ) -> None:
        """Register a music provider."""
        if provider.id in self._providers:
            raise SetupFailedError(
                f"Provider with id {provider.id} is already registered"
            )
        try:
            provider.config = conf
            provider.mass = self.mass
            provider.cache = self.mass.cache
            provider.logger = self.logger.getChild(provider.type.value)
            if await provider.setup():
                self._providers[provider.id] = provider
        except Exception as err:  # pylint: disable=broad-except
            raise SetupFailedError(
                f"Setup failed of provider {provider.type.value}: {str(err)}"
            ) from err

    async def _cleanup_library(self) -> None:
        """Cleanup deleted items from library/database."""
        prev_providers = await self.mass.cache.get("prov_ids", default=[])
        cur_providers = list(self._providers.keys())
        removed_providers = {x for x in prev_providers if x not in cur_providers}

        for prov_id in removed_providers:

            # clean cache items from deleted provider(s)
            await self.mass.cache.clear(prov_id)

            # cleanup media items from db matched to deleted provider
            for ctrl in (
                # order is important here to recursively cleanup bottom up
                self.mass.music.radio,
                self.mass.music.playlists,
                self.mass.music.tracks,
                self.mass.music.albums,
                self.mass.music.artists,
            ):
                prov_items = await ctrl.get_db_items_by_prov_id(provider_id=prov_id)
                for item in prov_items:
                    await ctrl.remove_prov_mapping(item.item_id, prov_id)
        await self.mass.cache.set("prov_ids", cur_providers)
