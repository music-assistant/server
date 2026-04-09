"""Smart Playlist Music Provider for Music Assistant.

Allows creating rule-based playlists (dynamic or fixed) from library tracks,
filtered by genres, artists, albums, favorites, popularity and similar tracks.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid as _uuid
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import Playlist, ProviderMapping, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata

from music_assistant.constants import PlaylistPlayableItem
from music_assistant.helpers.security import is_safe_name
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.smart_playlist.helpers import (
    LOGIC_AND,
    MAX_SIMILAR_TRACKS,
    RULES_FILENAME,
    SmartPlaylistRules,
    read_json,
    validate_rules,
    write_json,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
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
    return ()


class SmartPlaylistProvider(MusicProvider):
    """Smart Playlist music provider for Music Assistant."""

    _rules_dir: str
    _rules_store: dict[str, SmartPlaylistRules]
    _names_store: dict[str, str]

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: library and catalog are identical (local rules)."""
        return False

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        self._rules_store = {}
        self._names_store = {}
        self._rules_dir = os.path.join(self.mass.storage_path, "smart_playlists")
        if not await asyncio.to_thread(os.path.exists, self._rules_dir):
            await asyncio.to_thread(os.makedirs, self._rules_dir, exist_ok=True)
        await self._load_rules_from_disk()

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self.mass.register_api_command("smart_playlists/create", self.create_smart_playlist)
        self.mass.register_api_command("smart_playlists/generate", self.generate_playlist)
        self.mass.register_api_command("smart_playlists/get_rules", self.get_smart_playlist_rules)
        self.mass.register_api_command(
            "smart_playlists/update_rules", self.update_smart_playlist_rules
        )
        self.mass.register_api_command("smart_playlists/list", self.list_smart_playlists)
        self.mass.register_api_command("smart_playlists/preview_tracks", self.preview_tracks)
        self.logger.info(
            "Smart Playlist provider loaded with %d stored playlists", len(self._rules_store)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if is_removed:
            for filename in await asyncio.to_thread(os.listdir, self._rules_dir):
                filepath = os.path.join(self._rules_dir, filename)
                await asyncio.to_thread(os.remove, filepath)

    # --- MusicProvider interface ---

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Yield all smart playlists."""
        for playlist_id, rules in self._rules_store.items():
            yield self._build_playlist(playlist_id, rules)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by provider id."""
        rules = self._rules_store.get(prov_playlist_id)
        if rules is None:
            msg = f"Smart playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg)
        return self._build_playlist(prov_playlist_id, rules)

    async def get_playlist_tracks(
        self, prov_playlist_id: str, page: int = 0
    ) -> Sequence[PlaylistPlayableItem]:
        """Evaluate rules and return fresh tracks.

        Returns a full batch on page 0; empty list on subsequent pages.
        Because is_dynamic=True, MA always calls with force_refresh so results stay fresh.
        """
        if page > 0:
            return []
        rules = self._rules_store.get(prov_playlist_id)
        if rules is None:
            return []
        return await self._evaluate_rules(rules)

    async def library_add(self, item: Playlist) -> bool:  # type: ignore[override]
        """Mark the playlist as added to the library (no-op)."""
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove a smart playlist."""
        if prov_item_id in self._rules_store:
            del self._rules_store[prov_item_id]
            self._names_store.pop(prov_item_id, None)
            await self._flush_rules_to_disk()
        return True

    # --- API commands ---

    async def create_smart_playlist(
        self,
        name: str,
        rules: dict[str, Any],
        is_dynamic: bool = True,
    ) -> Playlist:
        """Create a new smart playlist with the given rules.

        :param name: Name for the new playlist.
        :param rules: Dictionary of SmartPlaylistRules fields.
        :param is_dynamic: If True, tracks are re-evaluated fresh on each play.
        :return: The created library Playlist.
        """
        if not is_safe_name(name):
            msg = f"{name} is not a valid playlist name"
            raise InvalidDataError(msg)

        parsed_rules = SmartPlaylistRules.from_dict(rules)
        parsed_rules.is_dynamic = is_dynamic
        self._validate_rules(parsed_rules)

        playlist_id = str(_uuid.uuid4())
        self._names_store[playlist_id] = name
        await self._save_rules(playlist_id, parsed_rules)

        playlist = self._build_playlist(playlist_id, parsed_rules)
        return await self.mass.music.playlists.add_item_to_library(playlist)

    async def generate_playlist(
        self,
        name: str,
        rules: dict[str, Any],
        count: int | None = None,
    ) -> Playlist:
        """Evaluate rules once and create a static (non-dynamic) builtin playlist.

        :param name: Name for the new playlist.
        :param rules: Dictionary of SmartPlaylistRules fields.
        :param count: Optional track count override.
        :return: The created library Playlist.
        """
        if not is_safe_name(name):
            msg = f"{name} is not a valid playlist name"
            raise InvalidDataError(msg)

        parsed_rules = SmartPlaylistRules.from_dict(rules)
        self._validate_rules(parsed_rules)

        if count is not None and count > 0:
            parsed_rules.limit = count

        tracks = await self._evaluate_rules(parsed_rules)

        playlist = await self.mass.music.playlists.create_playlist(name)
        db_playlist_id = int(playlist.item_id)

        if tracks:
            uris = [t.uri for t in tracks if t.uri]
            if uris:
                # Call directly (not background task) so tracks are added before we return
                await self.mass.music.playlists._handle_add_playlist_tracks(db_playlist_id, uris)

        final_playlist = await self.mass.music.playlists.get_library_item(db_playlist_id)
        # Schedule an immediate metadata refresh to build the collage image and detect genres
        self.mass.metadata.schedule_update_metadata(final_playlist)
        return final_playlist

    async def get_smart_playlist_rules(self, playlist_id: str) -> dict[str, Any] | None:
        """Return the smart playlist rules for the given playlist id.

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
        """Update the rules for an existing smart playlist.

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
        await self._save_rules(prov_id, parsed_rules)

        library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
            prov_id, self.instance_id
        )
        if library_item:
            await self._update_playlist_description(library_item.item_id, parsed_rules)

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

    async def preview_tracks(
        self,
        rules: dict[str, Any],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Preview which tracks would be selected by the given rules.

        :param rules: SmartPlaylistRules fields as dict.
        :param limit: Maximum number of preview tracks to return.
        :return: List of track info dicts.
        """
        parsed_rules = SmartPlaylistRules.from_dict(rules)
        self._validate_rules(parsed_rules)
        original_limit = parsed_rules.limit
        parsed_rules.limit = limit
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

    def _build_playlist(self, playlist_id: str, rules: SmartPlaylistRules) -> Playlist:
        """Build a Playlist object from stored rules."""
        name = self._names_store.get(playlist_id, playlist_id)
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=name,
            owner="smart_playlist",
            is_editable=True,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                )
            },
        )
        playlist.is_dynamic = rules.is_dynamic
        playlist.metadata = MediaItemMetadata(
            description=f"[Smart Playlist] {rules.human_readable()}"
        )
        return playlist

    def _validate_rules(self, rules: SmartPlaylistRules) -> None:
        """Delegate to module-level validate_rules helper."""
        validate_rules(rules)

    async def _evaluate_rules(self, rules: SmartPlaylistRules) -> list[Track]:
        """Evaluate the rules and return a list of matching Track objects."""
        has_genre_filter = bool(rules.genre_ids)
        has_artist_filter = bool(rules.artist_ids)
        has_seed_filter = bool(rules.seed_track_uri)

        if rules.logic == LOGIC_AND:
            tracks = await self._evaluate_and(rules)
        else:
            tracks = await self._evaluate_or(rules)

        if rules.min_popularity is not None:
            tracks = [
                t
                for t in tracks
                if t.metadata
                and t.metadata.popularity is not None
                and t.metadata.popularity >= rules.min_popularity
            ]

        if (
            has_seed_filter
            and not has_artist_filter
            and not rules.album_ids
            and rules.seed_track_uri
        ):
            seed_tracks = await self._get_similar_tracks(rules.seed_track_uri, MAX_SIMILAR_TRACKS)
            if rules.min_popularity is not None:
                seed_tracks = [
                    t
                    for t in seed_tracks
                    if t.metadata
                    and t.metadata.popularity is not None
                    and t.metadata.popularity >= rules.min_popularity
                ]
            if rules.favorites_only:
                seed_tracks = [t for t in seed_tracks if t.favorite]
            if has_genre_filter and rules.logic == LOGIC_AND:
                seed_tracks = list(seed_tracks)
            existing_uris = {t.uri for t in tracks}
            for st in seed_tracks:
                if st.uri not in existing_uris:
                    tracks.append(st)
                    existing_uris.add(st.uri)

        random.shuffle(tracks)
        return tracks[: rules.limit]

    async def _evaluate_and(self, rules: SmartPlaylistRules) -> list[Track]:
        """Evaluate rules with AND logic: track must match ALL active filters."""
        has_genre = bool(rules.genre_ids)
        has_artist = bool(rules.artist_ids)
        has_album = bool(rules.album_ids)
        has_seed = bool(rules.seed_track_uri)

        no_structural_filter = not has_genre and not has_artist and not has_album
        if has_seed and no_structural_filter and not rules.favorites_only:
            return await self._get_library_tracks(
                favorite=None, genre_ids=None, limit=rules.limit * 3
            )
        if no_structural_filter and not rules.favorites_only and not has_seed:
            return await self._get_library_tracks(
                favorite=None, genre_ids=None, limit=rules.limit * 3
            )

        favorite = True if rules.favorites_only else None
        genre_ids = rules.genre_ids if has_genre else None
        base_tracks = await self._get_library_tracks(
            favorite=favorite, genre_ids=genre_ids, limit=rules.limit * 5
        )

        if not has_artist and not has_album:
            return base_tracks

        if has_artist:
            artist_id_set = set(rules.artist_ids)
            base_tracks = [
                t
                for t in base_tracks
                if {int(a.item_id) for a in t.artists if a.item_id} & artist_id_set
            ]
        if has_album:
            album_id_set = set(rules.album_ids)
            base_tracks = [
                t for t in base_tracks if t.album and int(t.album.item_id) in album_id_set
            ]
        return base_tracks

    async def _evaluate_or(self, rules: SmartPlaylistRules) -> list[Track]:
        """Evaluate rules with OR logic: track must match ANY active filter."""
        track_sets: dict[str, Track] = {}
        fetch_limit = rules.limit * 5

        if rules.favorites_only:
            for track in await self._get_library_tracks(favorite=True, limit=fetch_limit):
                if track.uri:
                    track_sets[track.uri] = track

        for genre_id in rules.genre_ids:
            for track in await self._get_library_tracks(genre_ids=[genre_id], limit=fetch_limit):
                if track.uri:
                    track_sets[track.uri] = track

        if rules.artist_ids:
            all_tracks = await self._get_library_tracks(limit=fetch_limit * 2)
            artist_id_set = set(rules.artist_ids)
            for track in all_tracks:
                if {
                    int(a.item_id) for a in track.artists if a.item_id
                } & artist_id_set and track.uri:
                    track_sets[track.uri] = track

        if rules.album_ids:
            all_tracks = (
                await self._get_library_tracks(limit=fetch_limit * 2)
                if not rules.artist_ids
                else list(track_sets.values())
            )
            album_id_set = set(rules.album_ids)
            for track in all_tracks:
                if track.album and int(track.album.item_id) in album_id_set and track.uri:
                    track_sets[track.uri] = track

        no_filters = (
            not rules.favorites_only
            and not rules.genre_ids
            and not rules.artist_ids
            and not rules.album_ids
            and not rules.seed_track_uri
        )
        if no_filters:
            for track in await self._get_library_tracks(limit=fetch_limit):
                if track.uri:
                    track_sets[track.uri] = track

        return list(track_sets.values())

    async def _get_library_tracks(
        self,
        favorite: bool | None = None,
        genre_ids: list[int] | None = None,
        limit: int = 500,
    ) -> list[Track]:
        """Fetch library tracks with optional filters."""
        return await self.mass.music.tracks.library_items(
            favorite=favorite,
            genre=genre_ids,
            limit=limit,
            order_by="random",
        )

    async def _get_similar_tracks(self, seed_track_uri: str, limit: int) -> list[Track]:
        """Get similar tracks for the given seed track URI."""
        try:
            _media_type, item_id, provider = await parse_uri(seed_track_uri)
        except Exception:
            self.logger.warning("Cannot parse seed_track_uri: %s", seed_track_uri)
            return []
        try:
            return await self.mass.music.tracks.similar_tracks(
                item_id=item_id,
                provider_instance_id_or_domain=provider,
                limit=limit,
            )
        except Exception as exc:
            self.logger.warning("Could not get similar tracks for %s: %s", seed_track_uri, exc)
            return []

    async def _update_playlist_description(
        self, library_item_id: int | str, rules: SmartPlaylistRules
    ) -> None:
        """Update the library playlist description with the rules summary."""
        try:
            playlist = await self.mass.music.playlists.get_library_item(library_item_id)
            updated = Playlist.from_dict(playlist.to_dict())
            updated.metadata.description = f"[Smart Playlist] {rules.human_readable()}"
            await self.mass.music.playlists.update_item_in_library(library_item_id, updated)
        except Exception as exc:
            self.logger.debug("Could not update description for %s: %s", library_item_id, exc)

    async def _load_rules_from_disk(self) -> None:
        """Load all persisted rules from the rules directory."""
        rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
        if not await asyncio.to_thread(os.path.isfile, rules_file):
            return
        try:
            data = await asyncio.to_thread(read_json, rules_file)
            for playlist_id, entry in data.items():
                if isinstance(entry, dict) and "rules" in entry:
                    # New format: {"name": "...", "rules": {...}}
                    self._rules_store[playlist_id] = SmartPlaylistRules.from_dict(entry["rules"])
                    self._names_store[playlist_id] = entry.get("name", playlist_id)
                else:
                    # Legacy format: entry is the rules dict directly
                    self._rules_store[playlist_id] = SmartPlaylistRules.from_dict(entry)
                    self._names_store[playlist_id] = playlist_id
        except Exception as exc:
            self.logger.warning("Failed to load smart playlist rules: %s", exc)

    async def _save_rules(self, playlist_id: str, rules: SmartPlaylistRules) -> None:
        """Persist rules to disk and update in-memory store."""
        self._rules_store[playlist_id] = rules
        await self._flush_rules_to_disk()

    async def _flush_rules_to_disk(self) -> None:
        """Write all rules + names to disk as a single JSON file."""
        rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
        data = {
            pid: {"name": self._names_store.get(pid, pid), "rules": r.to_dict()}
            for pid, r in self._rules_store.items()
        }
        await asyncio.to_thread(write_json, rules_file, data)
