"""
AudioMuseAiPlugin — the provider class for the AudioMuse-AI plugin.

See the package ``__init__.py`` docstring for the overall design. This module
holds only the provider class; the framework-facing entry points (``setup``,
``get_config_entries``) live in the package init.
"""

from __future__ import annotations

import asyncio
from itertools import zip_longest
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError, SetupFailedError
from music_assistant_models.media_items import RecommendationFolder, SearchResults
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.audiomuse_ai.client import AudioMuseClient, AudioMuseError
from music_assistant.providers.audiomuse_ai.constants import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_ENABLE_DISCOVER_ROW,
    CONF_ENABLE_TEXT_SEARCH,
    CONF_MEDIA_PROVIDER,
    DEFAULT_SIMILAR_LIMIT,
    RECOMMEND_ITEM_LIMIT,
    RECOMMEND_PER_SEED_LIMIT,
    RECOMMEND_SEED_COUNT,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import Track
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
        self._media_provider = str(config.get_value(CONF_MEDIA_PROVIDER) or "")
        self._unregister_handles: list[Callable[[], None]] = []

    @property
    def priority(self) -> int:
        """Consult AudioMuse-AI before the track's own music provider (default 50)."""
        return 25

    @property
    def ordered_similarity(self) -> bool:
        """AudioMuse-AI returns similar tracks best-match-first (sorted by sonic distance)."""
        return True

    async def handle_async_init(self) -> None:
        """
        Build the HTTP client and verify the server is reachable.

        Failures raise SetupFailedError so the loader surfaces a misconfigured
        URL / unreachable server through MA's standard provider-failure UI.
        """
        base_url = str(self.config.get_value(CONF_BASE_URL) or "").strip()
        if not base_url:
            msg = "AudioMuse-AI server URL is not configured"
            raise SetupFailedError(msg)
        if not self._media_provider:
            msg = "No Music Assistant media provider selected for id mapping"
            raise SetupFailedError(msg)
        token = self.config.get_value(CONF_API_TOKEN)
        self._client = AudioMuseClient(
            self.mass.http_session, base_url, str(token) if token else None, self.logger
        )
        if not await self._client.health():
            msg = f"Could not reach AudioMuse-AI server at {base_url}"
            raise SetupFailedError(msg)
        self.logger.info("Connected to AudioMuse-AI at %s", base_url)

    async def loaded_in_mass(self) -> None:
        """Register the plugin's API commands."""
        self._unregister_handles.append(
            self.mass.register_api_command("audiomuse_ai/similar", self._handle_similar)
        )
        self._unregister_handles.append(
            self.mass.register_api_command("audiomuse_ai/status", self._handle_status)
        )
        if bool(self.config.get_value(CONF_ENABLE_TEXT_SEARCH)):
            self._unregister_handles.append(
                self.mass.register_api_command("audiomuse_ai/search", self._handle_text_search)
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
            return []
        item_ids = [r["item_id"] for r in raw if r.get("item_id") and r["item_id"] != seed]
        return await self._resolve_tracks(item_ids[:limit])

    # ------------------------------------------------------------------
    # Cross-provider RECOMMENDATIONS hook (home/discover page)
    # ------------------------------------------------------------------

    @use_cache(60, base_class=RecommendationFolder, allow_expired_cache=True)
    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Yield an 'Inspired by recently played' folder for the discover page.

        Returns [] when disabled or when no recent track maps into the
        configured provider — the dispatcher then omits us (no empty card).
        """
        if self._client is None or not bool(self.config.get_value(CONF_ENABLE_DISCOVER_ROW)):
            return []
        try:
            recent = await self.mass.music.recently_played(
                limit=RECOMMEND_SEED_COUNT,
                media_types=[MediaType.TRACK],
                fully_played_only=False,
            )
        except Exception as err:
            self.logger.debug("recently_played failed: %s", err)
            return []
        if not recent:
            return []

        # Walk each recent item into a media-server seed id our provider knows.
        seeds: list[str] = []
        seen_seeds: set[str] = set()
        for mapping in recent:
            try:
                track = await self.mass.music.tracks.get(
                    mapping.item_id, mapping.provider, allow_update_metadata=False
                )
            except MusicAssistantError:
                continue
            seed = self._seed_item_id(track)
            if seed and seed not in seen_seeds:
                seeds.append(seed)
                seen_seeds.add(seed)
        if not seeds:
            return []

        # Fan out similar_tracks per seed; union, first occurrence wins (seeds
        # are already recency-ordered, so earlier seeds get priority).
        ordered: list[str] = []
        seen_results: set[str] = set(seeds)
        for seed in seeds:
            try:
                raw = await self._client.similar_tracks(seed, RECOMMEND_PER_SEED_LIMIT)
            except AudioMuseError as err:
                self.logger.debug("similar_tracks failed for %s: %s", seed, err)
                continue
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
            return []

        items = await self._resolve_tracks(ordered)
        if not items:
            return []
        return [
            RecommendationFolder(
                item_id="inspired_by_recently_played",
                provider=self.instance_id,
                name="Inspired by recently played",
                translation_key="inspired_by_recently_played",
                icon="mdi-shimmer",
                items=UniqueList(items),
            ),
        ]

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

    def _seed_item_id(self, track: Track) -> str | None:
        """
        Return the media-server item id for `track` on the configured provider.

        AudioMuse-AI keys tracks on the same media-server id Music Assistant
        stores in the matching provider mapping, so a direct lookup suffices.
        """
        for mapping in track.provider_mappings or ():
            if mapping.provider_instance == self._media_provider:
                return mapping.item_id
        return None

    async def _resolve_tracks(self, item_ids: list[str]) -> list[Track]:
        """
        Resolve AudioMuse-AI item ids back to Tracks on the configured provider.

        Lookup misses are dropped, preserving input order for the survivors.
        """

        async def _one(item_id: str) -> Track | None:
            try:
                return await self.mass.music.tracks.get(item_id, self._media_provider)
            except MusicAssistantError:
                return None

        resolved = await asyncio.gather(*[_one(i) for i in item_ids])
        return [t for t in resolved if t is not None]

    async def _text_search_item_ids(self, query: str, limit: int) -> list[str]:
        """Run CLAP + lyrics search, interleave results, dedupe; return item ids."""
        if self._client is None:
            return []
        clap, lyrics = await asyncio.gather(
            self._safe_search(self._client.clap_search, query, limit),
            self._safe_search(self._client.lyrics_search, query, limit),
        )
        ordered: list[str] = []
        seen: set[str] = set()
        # Interleave so neither engine dominates the head of the result list.
        for clap_entry, lyrics_entry in zip_longest(clap, lyrics):
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
    ) -> list[dict[str, Any]]:
        """Run one text-search call, swallowing API errors into an empty list."""
        try:
            return await fn(query, limit)
        except AudioMuseError as err:
            self.logger.debug("Text search failed: %s", err)
            return []

    # ------------------------------------------------------------------
    # API commands
    # ------------------------------------------------------------------

    async def _handle_similar(
        self, item_id: str, limit: int = DEFAULT_SIMILAR_LIMIT
    ) -> dict[str, Any]:
        """
        Return AudioMuse-AI neighbours for a media-server item id.

        :param item_id: Media-server item id of the seed track (the same id
            AudioMuse-AI and the configured provider share).
        :param limit: Max neighbours to return.
        """
        if self._client is None:
            return {
                "analyzed": False,
                "reason": "not_loaded",
                "seed_track_id": item_id,
                "items": [],
            }
        try:
            raw = await self._client.similar_tracks(item_id, limit)
        except AudioMuseError as err:
            return {
                "analyzed": False,
                "reason": str(err),
                "seed_track_id": item_id,
                "items": [],
            }
        items = [
            {
                "item_id": entry.get("item_id"),
                "provider": self._media_provider,
                "name": entry.get("title"),
                "artist": entry.get("author"),
                "distance": entry.get("distance"),
            }
            for entry in raw
            if entry.get("item_id") and entry.get("item_id") != item_id
        ]
        return {"analyzed": True, "seed_track_id": item_id, "items": items[:limit]}

    async def _handle_text_search(
        self, query: str, limit: int = DEFAULT_SIMILAR_LIMIT
    ) -> dict[str, Any]:
        """Return tracks matching a free-text query across CLAP + lyrics engines."""
        if self._client is None:
            return {"analyzed": False, "reason": "not_loaded", "query": query, "items": []}
        item_ids = await self._text_search_item_ids(query, limit)
        return {
            "analyzed": True,
            "query": query,
            "items": [{"item_id": iid, "provider": self._media_provider} for iid in item_ids],
        }

    async def _handle_status(self) -> dict[str, Any]:
        """Return current connection status and CLAP index stats."""
        status: dict[str, Any] = {
            "base_url": str(self.config.get_value(CONF_BASE_URL) or ""),
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
