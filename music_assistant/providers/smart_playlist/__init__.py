"""
Smart Playlist Plugin Provider for Music Assistant.

Allows creating rule-based playlists (dynamic or fixed) from library tracks,
filtered by genres, artists, albums, favorites, popularity and similar tracks.

# TODO (future PR): refactor this file into a package (e.g. __init__.py + evaluator.py +
# filters.py) — it is growing large and the evaluation / filter logic would benefit from
# being split into separate modules.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid as _uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace as dc_replace
from itertools import zip_longest
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    EventType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)
from music_assistant_models.media_items.metadata import MediaItemImage, MediaItemMetadata

from music_assistant.constants import DYNAMIC_PLAYLIST_SAMPLE_SIZE
from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.security import is_safe_name
from music_assistant.helpers.track_filter import filter_tracks
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.smart_playlist.helpers import (
    LOGIC_AND,
    RULES_FILENAME,
    SmartPlaylistRules,
    read_json,
    validate_rules,
    write_json,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

FETCH_LIMIT = 2000
CACHE_CATEGORY_DYNAMIC_SAMPLE = 0
DYNAMIC_SAMPLE_CACHE_EXPIRATION = 24 * 3600  # 24h; stale entries are still served via SWR

CONF_AI_DESCRIPTIONS = "ai_descriptions"
DESCRIPTION_PREFIX = "[Smart Playlist] "

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SmartPlaylistProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_AI_DESCRIPTIONS,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=True,
        ),
    )


def _filter_by_explicit(tracks: list[Track], explicit_rule: bool | None) -> list[Track]:
    """
    Filter tracks based on the explicit content rule.

    :param tracks: List of tracks to filter.
    :param explicit_rule: True = only explicit tracks, False = exclude explicit tracks,
                          None = no filter.
    :return: Filtered list of tracks.
    """
    if explicit_rule is True:
        # Only include tracks explicitly marked as explicit
        return [t for t in tracks if t.metadata.explicit is True]
    if explicit_rule is False:
        # Exclude tracks explicitly marked as explicit; pass through unknown
        return [t for t in tracks if t.metadata.explicit is not True]
    return tracks


def _filter_by_duration(
    tracks: list[Track], min_duration: int | None, max_duration: int | None
) -> list[Track]:
    """
    Filter tracks based on duration bounds.

    :param tracks: List of tracks to filter.
    :param min_duration: Minimum duration in seconds (None = no minimum).
    :param max_duration: Maximum duration in seconds (None = no maximum).
    :return: Filtered list of tracks.
    """
    if min_duration is not None:
        tracks = [t for t in tracks if t.duration and t.duration >= min_duration]
    if max_duration is not None:
        tracks = [t for t in tracks if t.duration and t.duration <= max_duration]
    return tracks


def _filter_by_last_played(tracks: list[Track], value: int | None, unit: str | None) -> list[Track]:
    """
    Filter tracks not played within a specified time period.

    :param tracks: List of tracks to filter.
    :param value: Time value (None = no filter).
    :param unit: Time unit: "hours", "days", "weeks", "months" (None = no filter).
    :return: Filtered list of tracks not played in the specified period (includes never-played).
    """
    if value is None or unit is None:
        return tracks

    # Convert to seconds based on unit
    seconds_per_unit = {"hours": 3600, "days": 86400, "weeks": 604800, "months": 2592000}
    seconds = value * seconds_per_unit.get(unit, 86400)  # Default to days if unknown

    threshold_timestamp = int(time.time()) - seconds
    return [t for t in tracks if not t.last_played or t.last_played < threshold_timestamp]


class SmartPlaylistProvider(PluginProvider):
    """Smart Playlist plugin provider for Music Assistant."""

    _rules_dir: str
    _rules_store: dict[str, SmartPlaylistRules]
    _names_store: dict[str, str]
    _descriptions_store: dict[str, str]
    _unregister_handles: list[Callable[[], None]]
    _flush_lock: asyncio.Lock

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        self._rules_store = {}
        self._names_store = {}
        self._descriptions_store = {}
        self._unregister_handles = []
        self._flush_lock = asyncio.Lock()
        self._rules_dir = os.path.join(self.mass.storage_path, "smart_playlists")
        if not await asyncio.to_thread(os.path.exists, self._rules_dir):
            await asyncio.to_thread(os.makedirs, self._rules_dir, exist_ok=True)
        await self._load_rules_from_disk()

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/create",
                self.create_smart_playlist,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/generate",
                self.generate_playlist,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/get_rules",
                self.get_smart_playlist_rules,
                required_scope=Scope.LIBRARY_READ,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/update_rules",
                self.update_smart_playlist_rules,
                required_scope=Scope.LIBRARY_WRITE,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/list", self.list_smart_playlists, required_scope=Scope.LIBRARY_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/preview_tracks",
                self.preview_tracks,
                required_scope=Scope.LIBRARY_READ,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/count_tracks", self.count_tracks, required_scope=Scope.LIBRARY_READ
            )
        )
        # Subscribe to library events to handle playlist deletion and renaming.
        self._unregister_handles.append(
            self.mass.subscribe(self._on_media_item_deleted, EventType.MEDIA_ITEM_DELETED)
        )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_media_item_updated, EventType.MEDIA_ITEM_UPDATED)
        )
        self.logger.info(
            "Smart Playlist provider loaded with %d stored playlists", len(self._rules_store)
        )
        # Re-add playlists missing from the library (e.g. after a DB reset).
        self.mass.create_task(self._reconcile_library())
        # One-time migration: remove legacy icon.svg from smart playlists
        # TODO: remove after 2.10 release
        self.mass.create_task(self._migrate_legacy_icon())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        if is_removed:
            # Remove all library entries — MA has no other mechanism to clean them up on removal.
            for playlist_id in list(self._rules_store):
                try:
                    library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
                        playlist_id, self.instance_id
                    )
                    if library_item is None:
                        continue
                    await self.mass.music.remove_item_from_library(
                        MediaType.PLAYLIST, library_item.item_id
                    )
                except Exception as exc:
                    self.logger.debug(
                        "Could not remove playlist %s from library: %s", playlist_id, exc
                    )
            for filename in await asyncio.to_thread(os.listdir, self._rules_dir):
                filepath = os.path.join(self._rules_dir, filename)
                await asyncio.to_thread(os.remove, filepath)
            await self.mass.cache.clear(
                category_filter=CACHE_CATEGORY_DYNAMIC_SAMPLE,
                provider_filter=self.instance_id,
            )

    # --- PluginProvider interface ---

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse smart playlists."""
        playlists = []
        for pid, rules in self._rules_store.items():
            playlists.append(await self._build_playlist(pid, rules))
        return playlists

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return smart playlists as a recommendation folder."""
        playlists = []
        for pid, r in self._rules_store.items():
            playlists.append(await self._build_playlist(pid, r))
        if playlists:
            return [
                RecommendationFolder(
                    item_id="smart_playlists",
                    provider=self.domain,
                    name="Smart Playlists",
                    translation_key="smart_playlists",
                    items=playlists,  # type: ignore[arg-type]
                )
            ]
        return []

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by provider id."""
        resolved_id, rules = await self._resolve_rules_for_playlist_id(prov_playlist_id)
        if rules is None:
            msg = f"Smart playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg)

        # Build playlist from rules
        return await self._build_playlist(resolved_id, rules)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Evaluate rules and return tracks.

        Returns a full batch on page 0; empty list on subsequent pages. Dynamic playlists
        return a bounded buffer (``DYNAMIC_PLAYLIST_SAMPLE_SIZE``) cached for
        ``DYNAMIC_SAMPLE_CACHE_EXPIRATION`` so browsing stays snappy. Stale entries are
        still served from cache while a fresh sample is rebuilt in the background
        (stale-while-revalidate). Callers that wrap this in
        ``mass.cache.handle_refresh(True)`` — notably the player queue when refilling a
        dynamic playlist — bypass the cache entirely and get a freshly-evaluated sample.
        """
        if page > 0:
            return []
        resolved_id, rules = await self._resolve_rules_for_playlist_id(prov_playlist_id)
        if rules is None:
            return []
        if not rules.is_dynamic:
            return await self._evaluate_rules(rules)
        user = get_current_user()
        # Tuple ensures a stable cache key and carries the filter into background SWR refreshes.
        user_provider_filter = (
            tuple(sorted(user.provider_filter)) if user and user.provider_filter else ()
        )
        # Filter the cached sample at the boundary (not inside the cached evaluation) so a
        # recency-filtered batch from a queue refill never gets cached and served to browse.
        sample = await self._cached_dynamic_sample(resolved_id, user_provider_filter)
        return filter_tracks(sample)

    @use_cache(
        expiration=DYNAMIC_SAMPLE_CACHE_EXPIRATION,
        category=CACHE_CATEGORY_DYNAMIC_SAMPLE,
        base_class=Track,
        allow_expired_cache=True,
    )
    async def _cached_dynamic_sample(
        self,
        prov_playlist_id: str,
        user_provider_filter: tuple[str, ...] = (),
    ) -> list[Track]:
        """Evaluate a fresh sample for a dynamic playlist (wrapped in SWR cache)."""
        rules = self._rules_store.get(prov_playlist_id)
        if rules is None:
            return []
        sample_rules = dc_replace(rules, limit=DYNAMIC_PLAYLIST_SAMPLE_SIZE)
        return await self._evaluate_rules(
            sample_rules, list(user_provider_filter) if user_provider_filter else None
        )

    async def _reconcile_library(self) -> None:
        """Re-add smart playlists that are missing from the library (e.g. after a DB reset)."""
        for playlist_id, rules in list(self._rules_store.items()):
            try:
                existing = await self.mass.music.playlists.get_library_item_by_prov_id(
                    playlist_id, self.instance_id
                )
                if existing is not None:
                    continue
                self.logger.info("Re-adding missing smart playlist '%s' to library", playlist_id)
                playlist = await self._build_playlist(playlist_id, rules)
                await self.mass.music.playlists.add_item_to_library(playlist)
            except Exception as exc:
                self.logger.warning("Could not re-add smart playlist %s: %s", playlist_id, exc)

    async def _migrate_legacy_icon(self) -> None:
        """Remove legacy icon.svg from smart playlists (added by versions prior to PR #4447)."""
        try:
            migrated_count = 0
            async for playlist in self.mass.music.playlists.iter_library_items(
                provider=self.instance_id
            ):
                if not playlist.metadata.images:
                    continue

                old_images = [
                    img
                    for img in playlist.metadata.images
                    if not (img.path == "icon.svg" and img.provider == self.instance_id)
                ]

                if len(old_images) != len(playlist.metadata.images):
                    playlist.metadata.images = UniqueList(old_images)
                    await self.mass.music.playlists.update_item_in_library(
                        playlist.item_id, playlist, overwrite=True
                    )
                    migrated_count += 1
                    self.logger.debug(
                        "Migrated smart playlist '%s' - removed legacy icon.svg", playlist.name
                    )
            if migrated_count > 0:
                self.logger.info(
                    "Migrated %d smart playlist(s) - removed legacy icon.svg", migrated_count
                )
        except Exception as exc:
            self.logger.warning("Failed to migrate legacy icons: %s", exc)

    async def _on_media_item_deleted(self, event: MassEvent) -> None:
        """Remove the rules for a deleted smart playlist."""
        item = event.data
        if not isinstance(item, Playlist):
            return
        for mapping in item.provider_mappings:
            if mapping.provider_instance == self.instance_id:
                prov_id = mapping.item_id
                self._rules_store.pop(prov_id, None)
                self._names_store.pop(prov_id, None)
                self._descriptions_store.pop(prov_id, None)
                await self._invalidate_dynamic_sample_cache(prov_id)
                await self._flush_rules_to_disk()
                break

    async def _on_media_item_updated(self, event: MassEvent) -> None:
        """Sync library playlist name back to the in-memory store."""
        item = event.data
        if not isinstance(item, Playlist):
            return
        for mapping in item.provider_mappings:
            if mapping.provider_instance == self.instance_id:
                prov_id = mapping.item_id
                if prov_id in self._names_store and self._names_store[prov_id] != item.name:
                    self._names_store[prov_id] = item.name
                    await self._flush_rules_to_disk()
                break

    # --- API commands ---

    async def create_smart_playlist(
        self,
        name: str,
        rules: dict[str, Any],
        is_dynamic: bool = True,
    ) -> Playlist:
        """
        Create a new smart playlist with the given rules.

        :param name: Name for the new playlist.
        :param rules: Dictionary of SmartPlaylistRules fields.
        :param is_dynamic: If True, tracks are re-evaluated fresh on each play.
        :return: The created library Playlist.
        """
        if not is_safe_name(name):
            msg = f"{name} is not a valid playlist name"
            raise InvalidDataError(
                msg,
                translation_key="invalid_name",
                translation_owner=self.translation_owner,
                translation_args=[name],
            )

        parsed_rules = SmartPlaylistRules.from_dict(rules)
        parsed_rules.is_dynamic = is_dynamic
        self._validate_rules(parsed_rules)

        playlist_id = str(_uuid.uuid4())
        self._names_store[playlist_id] = name
        await self._save_rules(playlist_id, parsed_rules)

        playlist = await self._build_playlist(playlist_id, parsed_rules)
        library_playlist = await self.mass.music.playlists.add_item_to_library(playlist)
        self.mass.metadata.schedule_update_metadata(library_playlist)
        self._schedule_ai_description_refresh(playlist_id)
        return library_playlist

    async def generate_playlist(
        self,
        name: str,
        rules: dict[str, Any],
        count: int | None = None,
    ) -> Playlist:
        """
        Evaluate rules once and create a static (non-dynamic) builtin playlist.

        :param name: Name for the new playlist.
        :param rules: Dictionary of SmartPlaylistRules fields.
        :param count: Optional track count override.
        :return: The created library Playlist.
        """
        if not is_safe_name(name):
            msg = f"{name} is not a valid playlist name"
            raise InvalidDataError(
                msg,
                translation_key="invalid_name",
                translation_owner=self.translation_owner,
                translation_args=[name],
            )

        parsed_rules = SmartPlaylistRules.from_dict(rules)
        self._validate_rules(parsed_rules)

        if count is not None:
            if not 1 <= count <= 2000:
                msg = "Playlist count must be between 1 and 2000"
                raise InvalidDataError(msg)
            parsed_rules.limit = count

        tracks = await self._evaluate_rules(parsed_rules)

        playlist = await self.mass.music.playlists.create_playlist(name)
        db_playlist_id = int(playlist.item_id)

        if tracks:
            uris = [t.uri for t in tracks if t.uri]
            if uris:
                # Use the internal method directly: the public add_playlist_tracks() schedules
                # a background task and returns immediately, but we need the tracks present
                # before returning the final playlist to the caller.
                await self.mass.music.playlists._handle_add_playlist_tracks(db_playlist_id, uris)

        final_playlist = await self.mass.music.playlists.get_library_item(db_playlist_id)
        # Schedule an immediate metadata refresh to build the collage image and detect genres
        self.mass.metadata.schedule_update_metadata(final_playlist)
        return final_playlist

    async def get_smart_playlist_rules(self, playlist_id: str) -> dict[str, Any] | None:
        """
        Return the smart playlist rules for the given playlist id.

        :param playlist_id: Provider playlist id (UUID) or library DB id (integer string).
        :return: Rules dict or None if not found.
        """
        prov_id = await self._resolve_to_provider_id(playlist_id)
        if prov_id is None:
            return None
        rules = self._rules_store.get(prov_id)
        if rules is None:
            return None
        return rules.to_dict()

    async def update_smart_playlist_rules(
        self,
        playlist_id: str,
        rules: dict[str, Any],
    ) -> None:
        """
        Update the rules for an existing smart playlist.

        :param playlist_id: Provider playlist id (UUID) or library DB id (integer string).
        :param rules: Updated SmartPlaylistRules fields as dict.
        """
        prov_id = await self._resolve_to_provider_id(playlist_id)
        if prov_id is None:
            msg = f"Smart playlist {playlist_id} not found"
            raise MediaNotFoundError(msg)
        existing = self._rules_store.get(prov_id)
        parsed_rules = SmartPlaylistRules.from_dict(rules)
        if existing is not None:
            parsed_rules.is_dynamic = existing.is_dynamic
        self._validate_rules(parsed_rules)

        rules_changed = existing is None or existing.to_dict() != parsed_rules.to_dict()

        # Drop the stale AI description before saving so it is invalidated on disk in the
        # same flush as the rule change, not left behind until the background refresh runs.
        self._descriptions_store.pop(prov_id, None)
        await self._save_rules(prov_id, parsed_rules)

        library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
            prov_id, self.instance_id
        )
        if library_item:
            await self._update_playlist_description(
                library_item.item_id, self._description_for(prov_id, parsed_rules)
            )
            if rules_changed:
                self.mass.call_later(
                    5,
                    self.mass.metadata.update_metadata,
                    library_item,
                    task_id=f"smart_playlist_metadata_refresh_{prov_id}",
                    force_refresh=True,
                )
        self._schedule_ai_description_refresh(prov_id)

    async def list_smart_playlists(self) -> list[dict[str, Any]]:
        """Return list of all smart playlist IDs and their rule summaries."""
        return [
            {
                "playlist_id": playlist_id,
                "name": self._names_store.get(playlist_id, playlist_id),
                "rules": rules.to_dict(),
                "summary": rules.human_readable(),
            }
            for playlist_id, rules in self._rules_store.items()
        ]

    async def count_tracks(self, rules: dict[str, Any]) -> dict[str, Any]:
        """
        Return the track count and approximate total duration for the given rules.

        :param rules: SmartPlaylistRules fields as dict.
        :return: Dict with ``count`` (int) and ``duration_seconds`` (int).
        """
        parsed_rules = SmartPlaylistRules.from_dict(rules)
        self._validate_rules(parsed_rules)
        # Override limit so count reflects all matching tracks, not just the playback limit.
        parsed_rules.limit = FETCH_LIMIT
        tracks = await self._evaluate_rules(parsed_rules)
        duration = sum(t.duration or 0 for t in tracks)
        return {"count": len(tracks), "duration_seconds": duration}

    async def preview_tracks(
        self,
        rules: dict[str, Any],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Return a preview of tracks matching the given rules.

        :param rules: SmartPlaylistRules fields as dict.
        :param limit: Maximum number of preview tracks to return.
        :return: List of track info dicts.
        """
        parsed_rules = SmartPlaylistRules.from_dict(rules)
        self._validate_rules(parsed_rules)
        original_limit = parsed_rules.limit
        parsed_rules.limit = max(1, min(limit, 2000))
        tracks = await self._evaluate_rules(parsed_rules)
        parsed_rules.limit = original_limit
        return [
            {
                "item_id": t.item_id,
                "uri": t.uri,
                "name": t.name,
                "artists": [a.name for a in t.artists],
                "album": t.album.name if t.album else None,
            }
            for t in tracks
        ]

    # --- Internal helpers ---

    async def _resolve_rules_for_playlist_id(
        self, playlist_id: str
    ) -> tuple[str, SmartPlaylistRules | None]:
        """Resolve playlist id and return (resolved_or_input_id, matching_rules_or_none)."""
        if rules := self._rules_store.get(playlist_id):
            return playlist_id, rules

        resolved_id = await self._resolve_to_provider_id(playlist_id)
        if resolved_id is None:
            return playlist_id, None
        return resolved_id, self._rules_store.get(resolved_id)

    async def _resolve_to_provider_id(self, playlist_id: str) -> str | None:
        """Resolve a library DB id or provider UUID to the provider UUID."""
        # If it's directly in the rules store, it's already a provider UUID
        if playlist_id in self._rules_store:
            return playlist_id
        # Try to resolve as a library DB id
        try:
            library_item = await self.mass.music.playlists.get_library_item(playlist_id)
            for mapping in library_item.provider_mappings:
                if mapping.provider_instance == self.instance_id:
                    return mapping.item_id
        except Exception as err:
            self.logger.debug("Could not resolve playlist id %s: %s", playlist_id, err)
        return None

    async def _build_playlist(self, playlist_id: str, rules: SmartPlaylistRules) -> Playlist:
        """Build a Playlist object from stored rules."""
        name = self._names_store.get(playlist_id, playlist_id)
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=name,
            owner="Smart Playlist",
            is_editable=True,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                    in_library=True,
                )
            },
        )
        playlist.is_dynamic = rules.is_dynamic
        playlist.metadata = MediaItemMetadata(
            description=self._description_for(playlist_id, rules),
            images=await self._images_for(playlist_id),
        )
        return playlist

    async def _images_for(self, playlist_id: str) -> UniqueList[MediaItemImage]:
        """Return images for the playlist from the library, or empty list if none available."""
        library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
            playlist_id, self.instance_id
        )
        if library_item and library_item.metadata and library_item.metadata.images:
            return library_item.metadata.images
        return UniqueList([])

    async def resolve_image(self, path: str) -> str | bytes:
        """Return the smart playlist provider icon as fallback image."""
        if path == "icon.svg":
            icon_path = Path(__file__).parent / "icon.svg"
            async with asyncio.timeout(5):
                return await asyncio.to_thread(icon_path.read_bytes)
        return path

    def _validate_rules(self, rules: SmartPlaylistRules) -> None:
        """Delegate to module-level validate_rules helper."""
        validate_rules(rules)

    async def _evaluate_rules(
        self,
        rules: SmartPlaylistRules,
        user_provider_filter: list[str] | None = None,
    ) -> list[Track]:
        """Evaluate the rules and return a list of matching Track objects."""
        seed_uris = rules.all_seed_uris()
        if seed_uris:
            # Seed mode: a similar-tracks pool derived from the seeds is the exclusive source.
            # artist_ids and album_ids are ignored per design.
            tracks = await self._tracks_from_seeds(seed_uris, target_size=rules.limit)
            tracks = await self._apply_seed_post_filters(tracks, rules)
        else:
            if rules.logic == LOGIC_AND:
                tracks = await self._evaluate_and(rules, user_provider_filter)
            else:
                tracks = await self._evaluate_or(rules, user_provider_filter)

            if rules.min_popularity is not None:
                tracks = [
                    t
                    for t in tracks
                    if t.metadata
                    and t.metadata.popularity is not None
                    and t.metadata.popularity >= rules.min_popularity
                ]

            # Explicit filter is now handled at SQL level via _get_library_tracks()

            if rules.year_from is not None or rules.year_to is not None:
                tracks = [
                    t
                    for t in tracks
                    if t.album is None
                    or t.album.year is None
                    or (
                        (rules.year_from is None or t.album.year >= rules.year_from)
                        and (rules.year_to is None or t.album.year <= rules.year_to)
                    )
                ]

            if rules.album_types:
                allowed_album_ids = await self._get_album_ids_for_types(rules.album_types)
                tracks = self._filter_by_album_ids(tracks, allowed_album_ids)

            # Apply duration and last_played filters
            tracks = _filter_by_duration(tracks, rules.min_duration, rules.max_duration)
            tracks = _filter_by_last_played(
                tracks, rules.last_played_before_value, rules.last_played_before_unit
            )

            # In non-seed mode, _evaluate_and/_evaluate_or already query by genre_ids.
            # Only enrich if excluded_genre_ids is set (needs track.metadata.genres).
            if rules.excluded_genre_ids:
                await self._enrich_tracks_with_db_genres(tracks)

        # Apply exclusions regardless of source mode. Recency (don't repeat recently-played) is
        # no longer handled here: the player queue owns it globally for dynamic playlists.
        excluded_genre_names, excl_album_type_ids = await asyncio.gather(
            self._resolve_excluded_genre_names(rules),
            self._get_album_ids_for_types(rules.excluded_album_types)
            if rules.excluded_album_types
            else asyncio.sleep(0),
        )
        tracks = self._apply_exclusions(
            tracks, rules, excluded_genre_names, excl_album_type_ids or None
        )
        tracks = self._deduplicate_tracks(tracks)
        random.shuffle(tracks)
        return tracks[: rules.limit]

    async def _apply_seed_post_filters(
        self,
        tracks: list[Track],
        rules: SmartPlaylistRules,
    ) -> list[Track]:
        """Apply post-filters (popularity, favorites, genre, year) to a seed-derived track list."""
        has_genre_filter = bool(rules.genre_ids or rules.excluded_genre_ids)

        if rules.min_popularity is not None:
            tracks = [
                t
                for t in tracks
                if t.metadata
                and t.metadata.popularity is not None
                and t.metadata.popularity >= rules.min_popularity
            ]
        if rules.favorites_only:
            tracks = [t for t in tracks if t.favorite]

        # Apply explicit filter in seed/discover mode post-filtering
        tracks = _filter_by_explicit(tracks, rules.explicit)

        if has_genre_filter:
            await self._enrich_tracks_with_db_genres(tracks)

        if has_genre_filter and rules.logic == LOGIC_AND:
            # Genre filter: resolve names and match against track genre metadata.
            # Note: Library tracks have been enriched with DB genres above.
            # Non-library streaming tracks may still lack genre metadata and will be kept
            # (don't exclude for missing data).
            genre_id_to_name = dict(rules.genre_names)
            for genre_id in rules.genre_ids:
                if genre_id not in genre_id_to_name:
                    with suppress(Exception):
                        genre = await self.mass.music.genres.get_library_item(genre_id)
                        genre_id_to_name[genre_id] = genre.name
            allowed_genre_names = {v.lower() for v in genre_id_to_name.values()}
            if allowed_genre_names:
                tracks = [
                    t
                    for t in tracks
                    if not t.metadata
                    or not t.metadata.genres
                    or any(g.lower() in allowed_genre_names for g in t.metadata.genres)
                ]
        if rules.year_from is not None or rules.year_to is not None:
            tracks = [
                t
                for t in tracks
                if t.album is None
                or t.album.year is None
                or (
                    (rules.year_from is None or t.album.year >= rules.year_from)
                    and (rules.year_to is None or t.album.year <= rules.year_to)
                )
            ]
        if rules.album_types:
            allowed_album_ids = await self._get_album_ids_for_types(rules.album_types)
            tracks = self._filter_by_album_ids(tracks, allowed_album_ids)

        # Apply duration and last_played filters in seed mode
        tracks = _filter_by_duration(tracks, rules.min_duration, rules.max_duration)
        return _filter_by_last_played(
            tracks, rules.last_played_before_value, rules.last_played_before_unit
        )

    def _apply_exclusions(
        self,
        tracks: list[Track],
        rules: SmartPlaylistRules,
        excluded_genre_names: set[str] | None = None,
        excl_album_type_ids: set[int] | None = None,
    ) -> list[Track]:
        """Filter out tracks whose artist, album, URI, genre or album type is in the exclusion lists."""
        if (
            not rules.excluded_artist_ids
            and not rules.excluded_album_ids
            and not rules.excluded_track_uris
            and not excluded_genre_names
            and not excl_album_type_ids
        ):
            return tracks
        excl_artists = set(rules.excluded_artist_ids)
        excl_albums = set(rules.excluded_album_ids)
        excl_uris = set(rules.excluded_track_uris)
        result = []
        for track in tracks:
            if track.uri and track.uri in excl_uris:
                continue
            if (
                excl_artists
                and {
                    int(a.item_id) for a in track.artists if a.item_id and str(a.item_id).isdigit()
                }
                & excl_artists
            ):
                continue
            if (
                excl_albums
                and track.album
                and track.album.item_id
                and str(track.album.item_id).isdigit()
                and int(track.album.item_id) in excl_albums
            ):
                continue
            if (
                excluded_genre_names
                and track.metadata
                and track.metadata.genres
                and any(g.lower() in excluded_genre_names for g in track.metadata.genres)
            ):
                continue
            if (
                excl_album_type_ids
                and track.album is not None
                and track.album.item_id
                and str(track.album.item_id).isdigit()
                and int(track.album.item_id) in excl_album_type_ids
            ):
                continue
            result.append(track)
        return result

    def _filter_by_album_ids(self, tracks: list[Track], allowed_album_ids: set[int]) -> list[Track]:
        """Keep only tracks whose album ID is in the allowed set; pass through tracks with no resolvable album ID."""
        return [
            t
            for t in tracks
            if t.album is None
            or not (t.album.item_id and str(t.album.item_id).isdigit())
            or int(t.album.item_id) in allowed_album_ids
        ]

    async def _get_album_ids_for_types(self, album_types: list[str]) -> set[int]:
        """Return library album IDs matching the given album type values."""
        album_type_enums = [AlbumType(t) for t in album_types]
        album_ids: set[int] = set()
        offset = 0
        chunk = 500
        while True:
            page = await self.mass.music.albums.library_items(
                album_types=album_type_enums,
                limit=chunk,
                offset=offset,
                summary=False,
            )
            for a in page:
                if a.item_id and str(a.item_id).isdigit():
                    album_ids.add(int(a.item_id))
            if len(page) < chunk:
                break
            offset += chunk
        return album_ids

    def _deduplicate_tracks(self, tracks: list[Track]) -> list[Track]:
        """Remove duplicates and skip unavailable tracks while keeping order stable."""
        seen: set[Track] = set()
        result: list[Track] = []
        for track in tracks:
            if not track.available:
                continue
            if track in seen:
                continue
            seen.add(track)
            result.append(track)
        return result

    async def _resolve_excluded_genre_names(self, rules: SmartPlaylistRules) -> set[str]:
        """Resolve excluded_genre_ids to a lowercase name set for matching."""
        if not rules.excluded_genre_ids:
            return set()
        genre_id_to_name = dict(rules.excluded_genre_names)
        for genre_id in rules.excluded_genre_ids:
            if genre_id not in genre_id_to_name:
                with suppress(Exception):
                    genre = await self.mass.music.genres.get_library_item(genre_id)
                    genre_id_to_name[genre_id] = genre.name
        return {v.lower() for v in genre_id_to_name.values() if v}

    async def _build_track_id_map(
        self,
        tracks: list[Track],
        skip_tracks_with_genres: bool = False,
    ) -> dict[int, list[Track]]:
        """
        Build mapping of library track ID to track objects, resolving provider tracks to library.

        :param tracks: Tracks to process
        :param skip_tracks_with_genres: Skip tracks that already have genre metadata
        :return: Mapping of library track ID to list of track objects
        """
        track_id_to_tracks: dict[int, list[Track]] = {}
        provider_track_tasks: list[tuple[Track, str, str]] = []

        for track in tracks:
            if skip_tracks_with_genres and track.metadata and track.metadata.genres:
                continue

            if track.provider == "library" and str(track.item_id).isdigit():
                track_id = int(track.item_id)
                if track_id not in track_id_to_tracks:
                    track_id_to_tracks[track_id] = []
                track_id_to_tracks[track_id].append(track)
            elif track.provider_mappings:
                for mapping in track.provider_mappings:
                    provider_track_tasks.append(
                        (
                            track,
                            mapping.item_id,
                            mapping.provider_instance or mapping.provider_domain,
                        )
                    )
                    break

        # Resolve all provider tracks to library tracks in parallel
        if provider_track_tasks:
            library_tracks = await asyncio.gather(
                *(
                    self.mass.music.tracks.get_library_item_by_prov_id(
                        item_id=item_id,
                        provider_instance_id_or_domain=provider_instance_or_domain,
                    )
                    for _, item_id, provider_instance_or_domain in provider_track_tasks
                )
            )

            for (track, _, _), library_track in zip(
                provider_track_tasks, library_tracks, strict=True
            ):
                if library_track:
                    track_id = int(library_track.item_id)
                    if track_id not in track_id_to_tracks:
                        track_id_to_tracks[track_id] = []
                    track_id_to_tracks[track_id].append(track)

        return track_id_to_tracks

    async def _enrich_tracks_with_db_genres(self, tracks: list[Track]) -> None:
        """
        Enrich library tracks with genre data from the database.

        For tracks missing genre metadata, retrieves genre associations from the database
        and populates track.metadata.genres.
        """
        if not tracks:
            return

        track_id_to_tracks = await self._build_track_id_map(tracks, skip_tracks_with_genres=True)
        if not track_id_to_tracks:
            return

        # Fetch genres for all track IDs in parallel
        track_ids = list(track_id_to_tracks.keys())
        genre_results = await asyncio.gather(
            *(
                self.mass.music.genres.get_genres_for_media_item(MediaType.TRACK, track_id)
                for track_id in track_ids
            )
        )

        # Apply genres to tracks
        for track_id, genres in zip(track_ids, genre_results, strict=True):
            tracks_list = track_id_to_tracks[track_id]
            if not genres:
                continue
            for track in tracks_list:
                if not track.metadata:
                    track.metadata = MediaItemMetadata()
                if not track.metadata.genres:
                    track.metadata.genres = set()
                for genre in genres:
                    track.metadata.genres.add(genre.name)

    async def _filter_tracks_with_all_genres(
        self, tracks: list[Track], required_genre_ids: list[int]
    ) -> list[Track]:
        """Filter tracks to only those that have ALL required genre IDs in the database."""
        if not tracks or not required_genre_ids:
            return tracks

        required_ids = set(required_genre_ids)

        track_id_to_tracks = await self._build_track_id_map(tracks)
        if not track_id_to_tracks:
            return []

        # Fetch genres for all track IDs in parallel
        # With typical limits (20-50 tracks), this results in 100-250 parallel DB calls
        # which is acceptable. Only at very high limits would we approach the 2000 max.
        track_ids = list(track_id_to_tracks.keys())
        genre_results = await asyncio.gather(
            *(
                self.mass.music.genres.get_genres_for_media_item(MediaType.TRACK, track_id)
                for track_id in track_ids
            )
        )

        # Build set of track IDs that have all required genres
        matching_track_ids: set[int] = set()
        for track_id, genres in zip(track_ids, genre_results, strict=True):
            track_genre_ids = {
                int(g.item_id) for g in genres if g.item_id and str(g.item_id).isdigit()
            }
            if required_ids.issubset(track_genre_ids):
                matching_track_ids.add(track_id)

        # Return tracks that match, preserving original order
        result: list[Track] = []
        for track_id in track_ids:
            if track_id in matching_track_ids:
                result.extend(track_id_to_tracks[track_id])

        return result

    async def _evaluate_and(
        self,
        rules: SmartPlaylistRules,
        user_provider_filter: list[str] | None = None,
    ) -> list[Track]:
        """Evaluate rules with AND logic: track must match ALL active filters."""
        has_genre = bool(rules.genre_ids)
        has_artist = bool(rules.artist_ids)
        has_album = bool(rules.album_ids)

        no_structural_filter = not has_genre and not has_artist and not has_album

        if no_structural_filter and not rules.favorites_only:
            return await self._get_library_tracks(
                favorite=None,
                genre_ids=None,
                explicit=rules.explicit,
                limit=min(rules.limit * 3, 2000),
                user_provider_filter=user_provider_filter,
            )

        favorite = True if rules.favorites_only else None
        genre_ids = rules.genre_ids if has_genre else None
        base_tracks = await self._get_library_tracks(
            favorite=favorite,
            genre_ids=genre_ids,
            explicit=rules.explicit,
            limit=min(rules.limit * 5, 2000),
            user_provider_filter=user_provider_filter,
        )

        # When multiple genres are specified with AND logic, filter to tracks that have ALL genres
        if has_genre and len(rules.genre_ids) > 1:
            base_tracks = await self._filter_tracks_with_all_genres(base_tracks, rules.genre_ids)

        if not has_artist and not has_album:
            return base_tracks

        if has_artist:
            artist_id_set = set(rules.artist_ids)
            base_tracks = [
                t
                for t in base_tracks
                if {int(a.item_id) for a in t.artists if a.item_id and str(a.item_id).isdigit()}
                & artist_id_set
            ]
        if has_album:
            album_id_set = set(rules.album_ids)
            base_tracks = [
                t
                for t in base_tracks
                if t.album
                and t.album.item_id
                and str(t.album.item_id).isdigit()
                and int(t.album.item_id) in album_id_set
            ]
        return base_tracks

    async def _evaluate_or(
        self,
        rules: SmartPlaylistRules,
        user_provider_filter: list[str] | None = None,
    ) -> list[Track]:
        """Evaluate rules with OR logic: track must match ANY active filter."""
        track_sets: dict[str, Track] = {}
        fetch_limit = min(rules.limit * 5, FETCH_LIMIT)

        if rules.favorites_only:
            for track in await self._get_library_tracks(
                favorite=True,
                explicit=rules.explicit,
                limit=fetch_limit,
                user_provider_filter=user_provider_filter,
            ):
                if track.uri:
                    track_sets[track.uri] = track

        if rules.genre_ids:
            for track in await self._get_library_tracks(
                genre_ids=rules.genre_ids,
                explicit=rules.explicit,
                limit=fetch_limit,
                user_provider_filter=user_provider_filter,
            ):
                if track.uri:
                    track_sets[track.uri] = track

        if rules.artist_ids or rules.album_ids:
            all_tracks = await self._get_library_tracks(
                explicit=rules.explicit,
                limit=min(fetch_limit * 2, FETCH_LIMIT),
                user_provider_filter=user_provider_filter,
            )
            if rules.artist_ids:
                artist_id_set = set(rules.artist_ids)
                for track in all_tracks:
                    if {
                        int(a.item_id)
                        for a in track.artists
                        if a.item_id and str(a.item_id).isdigit()
                    } & artist_id_set and track.uri:
                        track_sets[track.uri] = track
            if rules.album_ids:
                album_id_set = set(rules.album_ids)
                for track in all_tracks:
                    if (
                        track.album
                        and track.album.item_id
                        and str(track.album.item_id).isdigit()
                        and int(track.album.item_id) in album_id_set
                        and track.uri
                    ):
                        track_sets[track.uri] = track

        no_filters = (
            not rules.favorites_only
            and not rules.genre_ids
            and not rules.artist_ids
            and not rules.album_ids
        )
        if no_filters:
            for track in await self._get_library_tracks(
                explicit=rules.explicit,
                limit=fetch_limit,
                user_provider_filter=user_provider_filter,
            ):
                if track.uri:
                    track_sets[track.uri] = track

        return list(track_sets.values())

    async def _get_library_tracks(
        self,
        favorite: bool | None = None,
        genre_ids: list[int] | None = None,
        explicit: bool | None = None,
        limit: int = 500,
        user_provider_filter: list[str] | None = None,
    ) -> list[Track]:
        """Fetch library tracks with optional filters."""
        return await self.mass.music.tracks.library_items(
            favorite=favorite,
            genre=genre_ids,
            explicit=explicit,
            limit=limit,
            order_by="random",
            provider=user_provider_filter,
            summary=False,
        )

    async def _tracks_from_seeds(self, seed_uris: list[str], target_size: int) -> list[Track]:
        """Build a pool of each seed's own tracks plus tracks similar to them."""
        seeds: list[MediaItemType] = []
        for uri in seed_uris:
            try:
                media_type, provider, item_id = await parse_uri(uri)
            except Exception:
                self.logger.warning("Cannot parse seed URI: %s", uri)
                continue
            try:
                ctrl = self.mass.music.get_controller(media_type)
                seeds.append(await ctrl.get(item_id, provider))
            except Exception as exc:
                self.logger.warning("Could not resolve seed %s: %s", uri, exc)
        if not seeds:
            return []
        # round-robin each seed's own pool so seeds contribute evenly; `seen` dedupes across them
        pool_cap = target_size * 3
        per_seed_cap = -(-pool_cap // len(seeds))  # ceil, so the pools can still fill the cap
        seen: set[Track] = set()
        per_seed_pools: list[list[Track]] = []
        for seed in seeds:
            seed_pool: list[Track] = []
            with suppress(MusicAssistantError):
                seed_tracks = await self.mass.player_queues.get_tracks_for_playback(seed)
                # shuffle so seeds are drawn from across the whole playlist, not just its top
                random.shuffle(seed_tracks)
                # Limit similar_tracks lookups to reduce initial evaluation time
                base_track_limit = max(3, per_seed_cap // 15)
                base_count = 0
                for base in seed_tracks:
                    if len(seed_pool) >= per_seed_cap:
                        break
                    if base not in seen:
                        seen.add(base)
                        seed_pool.append(base)
                        if base_count < base_track_limit:
                            base_count += 1
                            with suppress(MusicAssistantError):
                                for track in await self.mass.music.tracks.similar_tracks(
                                    base.item_id, base.provider
                                ):
                                    if len(seed_pool) >= per_seed_cap:
                                        break
                                    if track not in seen:
                                        seen.add(track)
                                        seed_pool.append(track)
            per_seed_pools.append(seed_pool)
        pool: list[Track] = []
        for round_tracks in zip_longest(*per_seed_pools):
            pool.extend(track for track in round_tracks if track is not None)
        return pool[:pool_cap]

    async def _update_playlist_description(
        self, library_item_id: int | str, description: str
    ) -> None:
        """Update the library playlist description with the given text."""
        try:
            playlist = await self.mass.music.playlists.get_library_item(library_item_id)
            if playlist.metadata and playlist.metadata.description == description:
                # Already up to date; skip the redundant write and update event.
                return
            updated = Playlist.from_dict(playlist.to_dict())
            updated.metadata.description = description
            await self.mass.music.playlists.update_item_in_library(
                library_item_id, updated, overwrite=True
            )
        except Exception as exc:
            self.logger.debug("Could not update description for %s: %s", library_item_id, exc)

    def _description_for(self, playlist_id: str, rules: SmartPlaylistRules) -> str:
        """Return the stored AI description when enabled, else the rules summary."""
        if self.config.get_value(CONF_AI_DESCRIPTIONS) and (
            stored := self._descriptions_store.get(playlist_id)
        ):
            return stored
        return f"{DESCRIPTION_PREFIX}{rules.human_readable()}"

    def _schedule_ai_description_refresh(self, playlist_id: str) -> None:
        """Schedule a background AI description refresh, deduped per playlist."""
        if not self.config.get_value(CONF_AI_DESCRIPTIONS):
            return
        self.mass.create_task(
            self._refresh_ai_description(playlist_id),
            task_id=f"smart_playlist_ai_desc_{playlist_id}",
            abort_existing=True,
        )

    async def _refresh_ai_description(self, playlist_id: str) -> None:
        """Regenerate and persist the AI description for a playlist, updating the library item."""
        rules = self._rules_store.get(playlist_id)
        if rules is None:
            return
        name = self._names_store.get(playlist_id, playlist_id)
        description = await self._generate_ai_description(name, rules)
        previous = self._descriptions_store.get(playlist_id)
        if description:
            self._descriptions_store[playlist_id] = description
        else:
            self._descriptions_store.pop(playlist_id, None)
        if self._descriptions_store.get(playlist_id) != previous:
            await self._flush_rules_to_disk()
        library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
            playlist_id, self.instance_id
        )
        if library_item:
            await self._update_playlist_description(
                library_item.item_id, self._description_for(playlist_id, rules)
            )

    async def _generate_ai_description(self, name: str, rules: SmartPlaylistRules) -> str | None:
        """
        Generate a natural-language description via the first AI provider that responds.

        :param name: The playlist name, included in the prompt for context.
        :param rules: The rules whose summary the description should reflect.
        :return: The AI-generated description, or None when disabled, unavailable, or on error.
        """
        if not self.config.get_value(CONF_AI_DESCRIPTIONS):
            return None
        locale = self.mass.metadata.locale
        for provider in self.mass.get_providers_supporting_feature(ProviderFeature.AI_QUERY):
            if not isinstance(provider, PluginProvider):
                continue
            try:
                response = await provider.ai_query(self._build_ai_prompt(name, rules, locale))
            except Exception as exc:
                self.logger.debug("AI description generation failed for '%s': %s", name, exc)
                continue
            if cleaned := response.strip():
                return cleaned
        return None

    def _build_ai_prompt(self, name: str, rules: SmartPlaylistRules, locale: str) -> str:
        """Build the prompt asking an AI provider to describe the smart playlist."""
        return (
            "Write a short, friendly description (one or two sentences) for a music playlist. "
            f"Write it in the language matching the locale '{locale}'. "
            "Reply with only the description, no quotes or preamble.\n"
            f"Playlist name: {name}\n"
            f"It contains tracks matching these rules: {rules.human_readable()}"
        )

    async def _load_rules_from_disk(self) -> None:
        """Load all persisted rules from the rules directory."""
        rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
        if not await asyncio.to_thread(os.path.isfile, rules_file):
            return
        try:
            data = await read_json(rules_file)
            for playlist_id, entry in data.items():
                self._rules_store[playlist_id] = SmartPlaylistRules.from_dict(entry["rules"])
                self._names_store[playlist_id] = entry.get("name", playlist_id)
                if description := entry.get("ai_description"):
                    self._descriptions_store[playlist_id] = description
        except Exception as exc:
            self.logger.warning("Failed to load smart playlist rules: %s", exc)

    async def _save_rules(self, playlist_id: str, rules: SmartPlaylistRules) -> None:
        """Persist rules to disk and update in-memory store."""
        self._rules_store[playlist_id] = rules
        await self._invalidate_dynamic_sample_cache(playlist_id)
        await self._flush_rules_to_disk()

    async def _invalidate_dynamic_sample_cache(self, playlist_id: str) -> None:
        """Drop the cached dynamic sample for this playlist so the next browse refreshes."""
        await self.mass.cache.clear(
            key_filter=playlist_id,
            category_filter=CACHE_CATEGORY_DYNAMIC_SAMPLE,
            provider_filter=self.instance_id,
        )

    async def _flush_rules_to_disk(self) -> None:
        """Write all rules + names to disk as a single JSON file."""
        async with self._flush_lock:
            rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
            data = {
                pid: {
                    "name": self._names_store.get(pid, pid),
                    "rules": r.to_dict(),
                    "ai_description": self._descriptions_store.get(pid),
                }
                for pid, r in self._rules_store.items()
            }
            await write_json(rules_file, data)
