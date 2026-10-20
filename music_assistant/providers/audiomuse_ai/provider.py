"""
AudioMuseAiPlugin — the provider class for the AudioMuse-AI plugin.

See the package ``__init__.py`` docstring for the overall design. This module
holds only the provider class; the framework-facing ``setup`` entry point lives in
the package init and the one-time setup input is collected by ``setup_flow.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from itertools import zip_longest
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, MediaType
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import RecommendationFolder, SearchResults
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.audiomuse_ai.client import AudioMuseClient, AudioMuseError
from music_assistant.providers.audiomuse_ai.constants import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_ENABLE_DISCOVER_ROW,
    CONF_ENABLE_TEXT_SEARCH,
    CONF_LABEL_STATUS,
    CONF_MEDIA_PROVIDER,
    DEFAULT_SIMILAR_LIMIT,
    ITEM_ID_PREFIXES,
    RECOMMEND_ITEM_LIMIT,
    RECOMMEND_PER_SEED_LIMIT,
    RECOMMEND_SEED_COUNT,
    RESOLVE_CONCURRENCY,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import (
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Track,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class AudioMuseAiPlugin(PluginProvider):
    """Plugin backing Similar Tracks, a discover row, and search via AudioMuse-AI."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the AudioMuse-AI plugin."""
        super().__init__(mass, manifest, config, supported_features)
        self._client: AudioMuseClient | None = None
        self._media_provider = str(self.get_setup_value(CONF_MEDIA_PROVIDER) or "")
        self._unregister_handles: list[Callable[[], None]] = []

    async def handle_async_init(self) -> None:
        """
        Build the HTTP client and verify the server is reachable and accepts the token.

        A misconfigured URL or unreachable server raises SetupFailedError; a
        rejected API token raises LoginFailed.
        """
        base_url = str(self.get_setup_value(CONF_BASE_URL) or "").strip()
        if not base_url:
            msg = "AudioMuse-AI server URL is not configured"
            raise SetupFailedError(msg)
        if not self._media_provider:
            msg = "No Music Assistant media provider selected for id mapping"
            raise SetupFailedError(msg)
        token = self.get_setup_value(CONF_API_TOKEN)
        self._client = AudioMuseClient(
            self.mass.http_session, base_url, str(token) if token else None, self.logger
        )
        if not await self._client.health():
            msg = f"Could not reach AudioMuse-AI server at {base_url}"
            raise SetupFailedError(msg)
        await self._client.check_auth()
        self.logger.info("Connected to AudioMuse-AI at %s", base_url)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the options and connection status entries for this provider instance."""
        status = await self._handle_status()
        base_url = status.get("base_url") or "(no URL)"
        if status.get("reachable"):
            parts = [f"Connected to {base_url}"]
            clap = status.get("clap") or {}
            if isinstance(clap.get("num_embeddings"), int):
                parts.append(f"{clap['num_embeddings']:,} CLAP embeddings")
            status_label = " · ".join(parts)
        else:
            status_label = f"Cannot reach AudioMuse-AI server at {base_url}"
        return (
            ConfigEntry(
                key=CONF_ENABLE_TEXT_SEARCH,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
            ),
            ConfigEntry(
                key=CONF_ENABLE_DISCOVER_ROW,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_LABEL_STATUS,
                type=ConfigEntryType.LABEL,
                label=status_label,
                category="status",
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Register the plugin's API commands."""
        self._unregister_handles.append(
            self.mass.register_api_command(
                "audiomuse_ai/similar", self._handle_similar, required_scope=Scope.LIBRARY_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "audiomuse_ai/status", self._handle_status, required_scope=Scope.LIBRARY_READ
            )
        )
        if bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH)):
            self._unregister_handles.append(
                self.mass.register_api_command(
                    "audiomuse_ai/search",
                    self._handle_text_search,
                    required_scope=Scope.LIBRARY_READ,
                )
            )

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        await super().unload(is_removed)

    # ------------------------------------------------------------------
    # Cross-provider SIMILAR_TRACKS hook
    # ------------------------------------------------------------------

    async def get_similar_tracks(self, track: Track, limit: int = 25) -> list[Track]:
        """
        Implement ProviderFeature.SIMILAR_TRACKS via AudioMuse-AI.

        :param track: Full Track (with provider_mappings) from the dispatcher.
        :param limit: Max number of similar tracks to return.
        :raises ProviderUnavailableError: When the AudioMuse-AI server cannot answer.
        """
        if self._client is None:
            return []
        seed = self._seed_item_id(track)
        if seed is None:
            self.logger.debug(
                "No %s mapping for %s; cannot query AudioMuse-AI", self._media_provider, track.uri
            )
            return []
        try:
            raw = await self._client.similar_tracks(seed, limit)
        except AudioMuseError as err:
            self.logger.warning("Similar tracks lookup failed: %s", err)
            raise ProviderUnavailableError(str(err)) from err
        item_ids = [r["item_id"] for r in raw if r.get("item_id") and r["item_id"] != seed]
        return await self._resolve_tracks(item_ids[:limit])

    # ------------------------------------------------------------------
    # Cross-provider RECOMMENDATIONS hook (home/discover page)
    # ------------------------------------------------------------------

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get the 'Inspired by recently played' row descriptor, without items.

        Returns [] when the discover row is disabled in the plugin config.
        """
        if not bool(self.config.get_value(CONF_ENABLE_DISCOVER_ROW)):
            return []
        return [
            RecommendationFolder(
                item_id="inspired_by_recently_played",
                provider=self.instance_id,
                name="Inspired by recently played",
                translation_key="inspired_by_recently_played",
                icon="mdi-shimmer",
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for the 'Inspired by recently played' row.

        Returns an empty list for an unknown row, when the row is disabled or
        when no recent track maps into the configured provider.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if item_id != "inspired_by_recently_played":
            return UniqueList()
        # recently played is personal, so the cached row is keyed on the user asking
        user = get_current_user()
        folder = await self._get_inspired_recommendations(user.user_id if user else None)
        if folder is None:
            return UniqueList()
        return folder.items

    # ------------------------------------------------------------------
    # Free-text SEARCH (CLAP + lyrics)
    # ------------------------------------------------------------------

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Implement ProviderFeature.SEARCH via AudioMuse-AI's text engines."""
        if self._client is None or MediaType.TRACK not in media_types:
            return SearchResults()
        item_ids = await self._text_search_item_ids(search_query, limit)
        tracks = await self._resolve_tracks(item_ids)
        return SearchResults(tracks=tracks)

    # ------------------------------------------------------------------
    # Identity mapping (MA track <-> AudioMuse-AI media-server item id)
    # ------------------------------------------------------------------

    @property
    def _id_prefix(self) -> str:
        """Prefix the configured provider adds to AudioMuse-AI item ids."""
        # stored config, so the prefix is known while the provider is still (re)loading
        domain = self.mass.config.get_raw_provider_config_value(self._media_provider, "domain")
        return ITEM_ID_PREFIXES.get(str(domain), "")

    def _seed_item_id(self, track: Track) -> str | None:
        """Return the AudioMuse-AI item id for `track` on the configured provider, if mapped."""
        for mapping in track.provider_mappings or ():
            if mapping.provider_instance == self._media_provider:
                return mapping.item_id.removeprefix(self._id_prefix)
        return None

    async def _resolve_tracks(self, audiomuse_ids: list[str]) -> list[Track]:
        """
        Resolve AudioMuse-AI item ids back to Tracks on the configured provider.

        Lookup misses are dropped, preserving input order for the survivors.
        """
        prefix = self._id_prefix
        item_ids = [f"{prefix}{iid}" for iid in audiomuse_ids]
        wanted = set(item_ids)
        resolved: dict[str, Track] = {}
        library_items = await self.mass.music.tracks.get_library_items_by_prov_id(
            provider_instance=self._media_provider,
            provider_item_ids=item_ids,
            limit=len(item_ids),
        )
        for track in library_items:
            for mapping in track.provider_mappings:
                if mapping.provider_instance == self._media_provider and mapping.item_id in wanted:
                    resolved[mapping.item_id] = track

        semaphore = asyncio.Semaphore(RESOLVE_CONCURRENCY)

        async def _fetch(item_id: str) -> None:
            async with semaphore:
                with suppress(MediaNotFoundError):
                    resolved[item_id] = await self.mass.music.tracks.get(
                        item_id, self._media_provider, allow_update_metadata=False, recursive=False
                    )

        await asyncio.gather(*(_fetch(i) for i in wanted.difference(resolved)))
        return [resolved[i] for i in item_ids if i in resolved]

    @use_cache(60, base_class=RecommendationFolder, allow_expired_cache=True)
    async def _get_inspired_recommendations(
        self, user_id: str | None
    ) -> RecommendationFolder | None:
        """
        Build the 'Inspired by recently played' folder with its items, or None when empty.

        :param user_id: Only shapes the cache key: the query reads the calling user's own
            recently played tracks.
        """
        if self._client is None or not bool(self.config.get_value(CONF_ENABLE_DISCOVER_ROW)):
            return None
        recent = await self.mass.music.recently_played(
            limit=RECOMMEND_SEED_COUNT,
            media_types=[MediaType.TRACK],
            fully_played_only=False,
        )
        if not recent:
            return None

        # Walk each recent item into a media-server seed id our provider knows.
        seeds: list[str] = []
        seen_seeds: set[str] = set()
        for mapping in recent:
            try:
                track = await self.mass.music.tracks.get(
                    mapping.item_id, mapping.provider, allow_update_metadata=False, recursive=False
                )
            except MusicAssistantError:
                continue
            seed = self._seed_item_id(track)
            if seed and seed not in seen_seeds:
                seeds.append(seed)
                seen_seeds.add(seed)
        if not seeds:
            return None

        # Fan out similar_tracks per seed; union, first occurrence wins (seeds
        # are already recency-ordered, so earlier seeds get priority).
        ordered: list[str] = []
        seen_results: set[str] = set(seeds)
        for seed in seeds:
            try:
                raw = await self._client.similar_tracks(seed, RECOMMEND_PER_SEED_LIMIT)
            except AudioMuseError as err:
                # a failed refresh must not replace the cached row with an empty one
                raise ProviderUnavailableError(str(err)) from err
            for entry in raw:
                iid = entry.get("item_id")
                if not iid or iid in seen_results:
                    continue
                seen_results.add(iid)
                ordered.append(iid)
                if len(ordered) >= RECOMMEND_ITEM_LIMIT:
                    break
            if len(ordered) >= RECOMMEND_ITEM_LIMIT:
                break
        if not ordered:
            return None

        items = await self._resolve_tracks(ordered)
        if not items:
            return None
        return RecommendationFolder(
            item_id="inspired_by_recently_played",
            provider=self.instance_id,
            name="Inspired by recently played",
            translation_key="inspired_by_recently_played",
            icon="mdi-shimmer",
            items=UniqueList(items),
        )

    async def _text_search_item_ids(self, query: str, limit: int) -> list[str]:
        """
        Run CLAP + lyrics search, interleave results, dedupe; return item ids.

        :raises ProviderUnavailableError: When both engines failed, so nothing gets cached.
        """
        if self._client is None:
            return []
        clap, lyrics = await asyncio.gather(
            self._safe_search(self._client.clap_search, query, limit),
            self._safe_search(self._client.lyrics_search, query, limit),
        )
        if clap is None and lyrics is None:
            raise ProviderUnavailableError("AudioMuse-AI text search is unavailable")
        ordered: list[str] = []
        seen: set[str] = set()
        # Interleave so neither engine dominates the head of the result list.
        for clap_entry, lyrics_entry in zip_longest(clap or (), lyrics or ()):
            for entry in (clap_entry, lyrics_entry):
                if entry is None:
                    continue
                iid = entry.get("item_id")
                if iid and iid not in seen:
                    seen.add(iid)
                    ordered.append(iid)
        return ordered[:limit]

    async def _safe_search(
        self,
        fn: Callable[[str, int], Awaitable[list[dict[str, Any]]]],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]] | None:
        """Run one text-search call, returning None when the engine failed."""
        try:
            return await fn(query, limit)
        except AudioMuseError as err:
            self.logger.debug("Text search failed: %s", err)
            return None

    # ------------------------------------------------------------------
    # API commands
    # ------------------------------------------------------------------

    async def _handle_similar(
        self, item_id: str, limit: int = DEFAULT_SIMILAR_LIMIT
    ) -> dict[str, Any]:
        """
        Return AudioMuse-AI neighbours for a track on the configured provider.

        :param item_id: Item id of the seed track on the configured provider.
        :param limit: Max neighbours to return.
        """
        if self._client is None:
            return {
                "analyzed": False,
                "reason": "not_loaded",
                "seed_track_id": item_id,
                "items": [],
            }
        prefix = self._id_prefix
        seed = item_id.removeprefix(prefix)
        try:
            raw = await self._client.similar_tracks(seed, limit)
        except AudioMuseError as err:
            return {
                "analyzed": False,
                "reason": str(err),
                "seed_track_id": item_id,
                "items": [],
            }
        items = [
            {
                "item_id": f"{prefix}{entry['item_id']}",
                "provider": self._media_provider,
                "name": entry.get("title"),
                "artist": entry.get("author"),
                "distance": entry.get("distance"),
            }
            for entry in raw
            if entry.get("item_id") and entry.get("item_id") != seed
        ]
        return {"analyzed": True, "seed_track_id": item_id, "items": items[:limit]}

    async def _handle_text_search(
        self, query: str, limit: int = DEFAULT_SIMILAR_LIMIT
    ) -> dict[str, Any]:
        """Return tracks matching a free-text query across CLAP + lyrics engines."""
        if self._client is None:
            return {"analyzed": False, "reason": "not_loaded", "query": query, "items": []}
        item_ids = await self._text_search_item_ids(query, limit)
        prefix = self._id_prefix
        return {
            "analyzed": True,
            "query": query,
            "items": [
                {"item_id": f"{prefix}{iid}", "provider": self._media_provider} for iid in item_ids
            ],
        }

    async def _handle_status(self) -> dict[str, Any]:
        """Return current connection status and CLAP index stats."""
        status: dict[str, Any] = {
            "base_url": str(self.get_setup_value(CONF_BASE_URL) or ""),
            "media_provider": self._media_provider,
            "text_search_enabled": bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH)),
            "reachable": False,
        }
        if self._client is not None:
            status["reachable"] = await self._client.health()
            stats = await self._client.clap_stats()
            if stats:
                status["clap"] = stats
        return status
