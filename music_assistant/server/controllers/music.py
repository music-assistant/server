"""MusicController: Orchestrates all data from music providers and sync to internal database."""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import statistics
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

from media.albums import AlbumsController
from media.artists import ArtistsController
from media.playlists import PlaylistController
from media.radio import RadioController
from media.tracks import TracksController

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import load_json_file
from music_assistant.common.helpers.uri import parse_uri
from music_assistant.common.models.config import MusicProviderConfig
from music_assistant.common.models.enums import (
    MediaType,
    MusicProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import (
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant.common.models.media_items import (
    BrowseFolder,
    MediaItem,
    MediaItemType,
    media_from_dict,
)
from music_assistant.common.models.provider import MusicProviderManifest
from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import api_command

from .database import TABLE_PLAYLOG, TABLE_TRACK_LOUDNESS
from .music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.music")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_PATH = os.path.join(BASE_DIR, "music_providers")


class MusicController:
    """Several helpers around the musicproviders."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.artists = ArtistsController(mass)
        self.albums = AlbumsController(mass)
        self.tracks = TracksController(mass)
        self.radio = RadioController(mass)
        self.playlists = PlaylistController(mass)
        self._available_providers: dict[MusicProviderManifest] = set()
        self._providers: dict[str, MusicProvider] = {}

    async def setup(self):
        """Async initialize of module."""
        # load available providers
        await self._load_available_providers()
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

    @api_command("music/providers")
    def get_providers(self) -> list[MusicProviderManifest]:
        """Return all loaded/running MusicProviders (instances)."""
        return list(self._available_providers.values())

    @api_command("music/providers/available")
    def get_available_providers(self) -> list[MusicProviderManifest]:
        """Return all available MusicProviders."""
        return list(self._available_providers.values())

    @api_command("music/sync")
    async def start_sync(
        self,
        media_types: Optional[Tuple[MediaType]] = None,
        providers: Optional[Tuple[str]] = None,
    ) -> None:
        """
        Start running the sync of (all or selected) musicproviders.

        media_types: only sync these media types. None for all.
        providers: only sync these provider domains. None for all.
        """

        for provider in self.providers:
            if providers is not None and prov.type not in provider_domains:
                continue
            self.mass.add_job(
                prov.sync_library(media_types),
                f"Library sync for provider {prov.name}",
                allow_duplicate=False,
            )


    def get_provider(
        self, provider_id_or_type: Union[str, ProviderType]
    ) -> MusicProvider:
        """Return Music provider by id (or type)."""
        if prov := self._providers.get(provider_id_or_type):
            return prov
        for prov in self._providers.values():
            if provider_id_or_type in (prov.type, prov.id, prov.type):
                return prov
        raise ProviderUnavailableError(
            f"Provider {provider_id_or_type} is not available"
        )

    async def search(
        self,
        search_query,
        media_types: List[MediaType] = MediaType.ALL,
        limit: int = 10,
    ) -> List[MediaItemType]:
        """
        Perform global search for media items on all providers.

            :param search_query: Search query.
            :param media_types: A list of media_types to include.
            :param limit: number of items to return in the search (per type).
        """
        # include results from all music providers
        provider_ids = (item.id for item in self.providers)
        # TODO: sort by name and filter out duplicates ?
        return itertools.chain.from_iterable(
            await asyncio.gather(
                *[
                    self.search_provider(
                        search_query, media_types, provider_id=provider_id, limit=limit
                    )
                    for provider_id in provider_ids
                ]
            )
        )

    async def search_provider(
        self,
        search_query: str,
        media_types: List[MediaType] = MediaType.ALL,
        provider_domain: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[MediaItemType]:
        """
        Perform search on given provider.

            :param search_query: Search query
            :param provider_domain: type of the provider to perform the search on.
            :param provider_id: id of the provider to perform the search on.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: number of items to return in the search (per type).
        """
        assert provider_domain or provider_id, "Provider needs to be supplied"
        prov = self.get_provider(provider_id or provider_domain)
        if MusicProviderFeature.SEARCH not in prov.supported_features:
            return []

        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")

        # prefer cache items (if any)
        cache_key = f"{prov.type}.search.{search_query}.{limit}"
        cache_key += "".join((x for x in media_types))

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
        media_type, provider_domain, item_id = parse_uri(uri)
        return await self.get_item(
            item_id=item_id,
            media_type=media_type,
            provider_domain=provider_domain,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    async def get_item(
        self,
        item_id: str,
        media_type: MediaType,
        provider_domain: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        force_refresh: bool = False,
        lazy: bool = True,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        assert (
            provider_domain or provider_id
        ), "provider_domain or provider_id must be supplied"
        if provider_domain == ProviderType.URL or provider_id == "url":
            # handle special case of 'URL' MusicProvider which allows us to play regular url's
            return await self.get_provider(ProviderType.URL).parse_item(item_id)
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            provider_item_id=item_id,
            provider_domain=provider_domain,
            provider_id=provider_id,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    async def add_to_library(
        self,
        media_type: MediaType,
        provider_item_id: str,
        provider_domain: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Add an item to the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.add_to_library(
            provider_item_id, provider_domain=provider_domain, provider_id=provider_id
        )

    async def remove_from_library(
        self,
        media_type: MediaType,
        provider_item_id: str,
        provider_domain: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Remove item from the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.remove_from_library(
            provider_item_id, provider_domain=provider_domain, provider_id=provider_id
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
                provider_domain=media_item.provider,
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
        self, item_id: str, provider_domain: ProviderType, loudness: int
    ):
        """List integrated loudness for a track in db."""
        await self.mass.database.insert(
            TABLE_TRACK_LOUDNESS,
            {"item_id": item_id, "provider": provider_domain, "loudness": loudness},
            allow_replace=True,
        )

    async def get_track_loudness(
        self, provider_item_id: str, provider_domain: ProviderType
    ) -> float | None:
        """Get integrated loudness for a track in db."""
        if result := await self.mass.database.get_row(
            TABLE_TRACK_LOUDNESS,
            {
                "item_id": provider_item_id,
                "provider": provider_domain,
            },
        ):
            return result["loudness"]
        return None

    async def get_provider_loudness(
        self, provider_domain: ProviderType
    ) -> float | None:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        if provider_domain == ProviderType.URL:
            # this is not a very good idea for random urls
            return None
        for db_row in await self.mass.database.get_rows(
            TABLE_TRACK_LOUDNESS,
            {
                "provider": provider_domain,
            },
        ):
            all_items.append(db_row["loudness"])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(self, item_id: str, provider_domain: ProviderType):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.mass.database.insert(
            TABLE_PLAYLOG,
            {
                "item_id": item_id,
                "provider": provider_domain,
                "timestamp": timestamp,
            },
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
            provider.logger = LOGGER.getChild(provider.type)
            if await provider.setup():
                self._providers[provider.id] = provider
        except Exception as err:  # pylint: disable=broad-except
            raise SetupFailedError(
                f"Setup failed of provider {provider.type}: {str(err)}"
            ) from err

    async def _cleanup_library(self) -> None:
        """Cleanup deleted items from library/database."""
        prev_providers = await self.mass.cache.get("prov_ids", default=[])
        cur_providers = list(self._providers.keys())
        removed_providers = {x for x in prev_providers if x not in cur_providers}

        for provider_id in removed_providers:

            # clean cache items from deleted provider(s)
            await self.mass.cache.clear(provider_id)

            # cleanup media items from db matched to deleted provider
            for ctrl in (
                # order is important here to recursively cleanup bottom up
                self.mass.music.radio,
                self.mass.music.playlists,
                self.mass.music.tracks,
                self.mass.music.albums,
                self.mass.music.artists,
            ):
                prov_items = await ctrl.get_db_items_by_prov_id(provider_id=provider_id)
                for item in prov_items:
                    await ctrl.remove_prov_mapping(item.item_id, provider_id)
        await self.mass.cache.set("prov_ids", cur_providers)

    async def _load_available_providers(self) -> None:
        """Preload all available provider manifest files."""
        for dir_str in os.listdir(PROVIDERS_PATH):
            dir_path = os.path.join(PROVIDERS_PATH, dir_str)
            if not os.path.isdir(dir_path):
                continue
            # get files in subdirectory
            for file_str in os.listdir(dir_path):
                file_path = os.path.join(dir_path, file_str)
                if not os.path.isfile(file_path):
                    continue
                if file_str != "manifest.json":
                    continue
                try:
                    manifest_dict = await load_json_file(file_path)
                    provider_manifest = MusicProviderManifest.from_dict(manifest_dict)
                    self._available_providers[
                        provider_manifest.domain
                    ] = provider_manifest
                    LOGGER.debug("Loaded manifest for MusicProvider %s", dir_str)
                except Exception as exc:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "Error while loading manifest for provider %s",
                        dir_str,
                        exc_info=exc,
                    )
