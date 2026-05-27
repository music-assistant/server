"""Smart Playlist Plugin Provider for Music Assistant.

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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType, ImageType, MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)
from music_assistant_models.media_items.metadata import MediaItemMetadata

from music_assistant.helpers.security import is_safe_name
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.plugin import PluginProvider
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
    from collections.abc import Sequence

    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

FETCH_LIMIT = 2000

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
    return ()


class SmartPlaylistProvider(PluginProvider):
    """Smart Playlist plugin provider for Music Assistant."""

    _rules_dir: str
    _rules_store: dict[str, SmartPlaylistRules]
    _names_store: dict[str, str]
    _unregister_handles: list[Callable[[], None]]
    _flush_lock: asyncio.Lock

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        self._rules_store = {}
        self._names_store = {}
        self._unregister_handles = []
        self._flush_lock = asyncio.Lock()
        self._rules_dir = os.path.join(self.mass.storage_path, "smart_playlists")
        if not await asyncio.to_thread(os.path.exists, self._rules_dir):
            await asyncio.to_thread(os.makedirs, self._rules_dir, exist_ok=True)
        await self._load_rules_from_disk()

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self._unregister_handles.append(
            self.mass.register_api_command("smart_playlists/create", self.create_smart_playlist)
        )
        self._unregister_handles.append(
            self.mass.register_api_command("smart_playlists/generate", self.generate_playlist)
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/get_rules", self.get_smart_playlist_rules
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "smart_playlists/update_rules", self.update_smart_playlist_rules
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command("smart_playlists/list", self.list_smart_playlists)
        )
        self._unregister_handles.append(
            self.mass.register_api_command("smart_playlists/preview_tracks", self.preview_tracks)
        )
        self._unregister_handles.append(
            self.mass.register_api_command("smart_playlists/count_tracks", self.count_tracks)
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

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        if is_removed:
            # Smart playlists only exist as long as this provider is installed.
            # When the provider is removed, the playlists must be explicitly removed
            # from the MA library — otherwise orphaned entries remain, because MA
            # has no other mechanism to clean up provider-owned playlists on removal.
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

    # --- PluginProvider interface ---

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse smart playlists."""
        return [self._build_playlist(pid, rules) for pid, rules in self._rules_store.items()]

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return smart playlists as a recommendation folder."""
        playlists = [self._build_playlist(pid, r) for pid, r in self._rules_store.items()]
        if not playlists:
            return []
        return [
            RecommendationFolder(
                item_id="smart_playlists",
                provider=self.domain,
                name="Smart Playlists",
                items=playlists,  # type: ignore[arg-type]
            )
        ]

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by provider id."""
        rules = self._rules_store.get(prov_playlist_id)
        if rules is None:
            msg = f"Smart playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg)
        return self._build_playlist(prov_playlist_id, rules)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Evaluate rules and return fresh tracks.

        Returns a full batch on page 0; empty list on subsequent pages.
        Because is_dynamic=True, MA always calls with force_refresh so results stay fresh.
        For dynamic playlists a small buffer of 5 tracks is returned per call so that
        dedup and shuffle stay effective across successive refreshes.
        """
        if page > 0:
            return []
        rules = self._rules_store.get(prov_playlist_id)
        if rules is None:
            return []
        if rules.is_dynamic:
            rules = dc_replace(rules, limit=5)
        return await self._evaluate_rules(rules)

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
                await self._flush_rules_to_disk()
                break

    async def _on_media_item_updated(self, event: MassEvent) -> None:
        """Sync library playlist name changes back to the in-memory/disk store."""
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
        library_playlist = await self.mass.music.playlists.add_item_to_library(playlist)
        self.mass.metadata.schedule_update_metadata(library_playlist)
        return library_playlist

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

    async def count_tracks(self, rules: dict[str, Any]) -> dict[str, Any]:
        """Return the track count and approximate total duration for the given rules.

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
        """Return a preview of tracks matching the given rules.

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
            description=f"[Smart Playlist] {rules.human_readable()}",
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path="icon.svg",
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            ),
        )
        return playlist

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

    async def _evaluate_rules(self, rules: SmartPlaylistRules) -> list[Track]:
        """Evaluate the rules and return a list of matching Track objects."""
        has_genre_filter = bool(rules.genre_ids)

        if rules.seed_track_uri or rules.seed_artist_uri:
            # Seed mode: a similar-tracks/artists pool is the exclusive source.
            # artist_ids and album_ids are ignored per design.
            if rules.seed_track_uri:
                tracks = await self._get_similar_tracks(rules.seed_track_uri, MAX_SIMILAR_TRACKS)
            else:
                assert rules.seed_artist_uri is not None  # guaranteed by outer condition
                tracks = await self._get_similar_artists_tracks(
                    rules.seed_artist_uri,
                    MAX_SIMILAR_TRACKS,
                    library_only=rules.seed_artist_library_only,
                )
            tracks = await self._apply_seed_post_filters(tracks, rules, has_genre_filter)
        else:
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

        # Apply exclusions and dedup regardless of source mode
        excluded_genre_names = await self._resolve_excluded_genre_names(rules)
        tracks = self._apply_exclusions(tracks, rules, excluded_genre_names)
        if rules.dedup_hours is not None:
            deduped = self._apply_dedup(tracks, rules.dedup_hours)
            if len(deduped) >= rules.limit:
                tracks = deduped
            elif deduped:
                # Pool partially exhausted: fill remainder with least-recently-played tracks
                # so playback never stops when the dedup window covers most of the library.
                deduped_uris = {t.uri for t in deduped}
                played_remainder = sorted(
                    (t for t in tracks if t.uri not in deduped_uris),
                    key=lambda t: t.last_played,
                )
                tracks = deduped + played_remainder[: rules.limit - len(deduped)]
            else:
                # All matching tracks were played recently — ignore dedup entirely so
                # the playlist never goes empty.
                self.logger.debug(
                    "dedup_hours=%d exhausted the full track pool; ignoring dedup",
                    rules.dedup_hours,
                )

        random.shuffle(tracks)
        return tracks[: rules.limit]

    async def _apply_seed_post_filters(
        self,
        tracks: list[Track],
        rules: SmartPlaylistRules,
        has_genre_filter: bool,
    ) -> list[Track]:
        """Apply post-filters (popularity, favorites, genre, year) to a seed-derived track list."""
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
        if has_genre_filter and rules.logic == LOGIC_AND:
            # Best-effort genre filter: resolve names then match against track genre metadata.
            # Tracks without genre metadata are kept (don't exclude for missing data).
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
        return tracks

    def _apply_exclusions(
        self,
        tracks: list[Track],
        rules: SmartPlaylistRules,
        excluded_genre_names: set[str] | None = None,
    ) -> list[Track]:
        """Filter out tracks whose artist, album, URI, or genre is in the exclusion lists."""
        if (
            not rules.excluded_artist_ids
            and not rules.excluded_album_ids
            and not rules.excluded_track_uris
            and not excluded_genre_names
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

    def _apply_dedup(self, tracks: list[Track], dedup_hours: int) -> list[Track]:
        """Filter out tracks last played within dedup_hours hours."""
        cutoff_ts = time.time() - dedup_hours * 3600
        # last_played is a Unix timestamp (int); 0 means never played.
        return [t for t in tracks if t.last_played == 0 or t.last_played < cutoff_ts]

    async def _evaluate_and(self, rules: SmartPlaylistRules) -> list[Track]:
        """Evaluate rules with AND logic: track must match ALL active filters."""
        has_genre = bool(rules.genre_ids)
        has_artist = bool(rules.artist_ids)
        has_album = bool(rules.album_ids)

        no_structural_filter = not has_genre and not has_artist and not has_album

        if no_structural_filter and not rules.favorites_only:
            return await self._get_library_tracks(
                favorite=None, genre_ids=None, limit=min(rules.limit * 3, 2000)
            )

        favorite = True if rules.favorites_only else None
        genre_ids = rules.genre_ids if has_genre else None
        base_tracks = await self._get_library_tracks(
            favorite=favorite, genre_ids=genre_ids, limit=min(rules.limit * 5, 2000)
        )

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

    async def _evaluate_or(self, rules: SmartPlaylistRules) -> list[Track]:
        """Evaluate rules with OR logic: track must match ANY active filter."""
        track_sets: dict[str, Track] = {}
        fetch_limit = min(rules.limit * 5, FETCH_LIMIT)

        if rules.favorites_only:
            for track in await self._get_library_tracks(favorite=True, limit=fetch_limit):
                if track.uri:
                    track_sets[track.uri] = track

        if rules.genre_ids:
            for track in await self._get_library_tracks(
                genre_ids=rules.genre_ids, limit=fetch_limit
            ):
                if track.uri:
                    track_sets[track.uri] = track

        if rules.artist_ids or rules.album_ids:
            all_tracks = await self._get_library_tracks(limit=min(fetch_limit * 2, FETCH_LIMIT))
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
            _media_type, provider, item_id = await parse_uri(seed_track_uri)
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

    async def _get_similar_artists_tracks(
        self, seed_artist_uri: str, limit: int, library_only: bool
    ) -> list[Track]:
        """Get tracks for artists similar to the given seed artist URI."""
        try:
            _media_type, provider, item_id = await parse_uri(seed_artist_uri)
        except Exception:
            self.logger.warning("Cannot parse seed_artist_uri: %s", seed_artist_uri)
            return []
        try:
            similar_artists = await self.mass.music.artists.similar_artists(
                item_id=item_id,
                provider_instance_id_or_domain=provider,
                limit=20,
            )
        except Exception as exc:
            self.logger.warning("Could not get similar artists for %s: %s", seed_artist_uri, exc)
            return []

        similar_names = {a.name.lower() for a in similar_artists}
        if not similar_names:
            return []

        if library_only:
            # Match against library tracks by artist name.
            all_tracks = await self._get_library_tracks(limit=min(limit * 20, 2000))
            result: dict[str, Track] = {}
            for track in all_tracks:
                for artist in track.artists:
                    if artist.name.lower() in similar_names and track.uri:
                        result[track.uri] = track
                        break
            return list(result.values())

        # Provider mode: fetch top tracks for each similar artist directly from the provider.
        result_provider: dict[str, Track] = {}
        per_artist = max(1, limit // max(len(similar_artists), 1))
        self.logger.debug(
            "seed_artist provider mode: %d similar artists, per_artist=%d",
            len(similar_artists),
            per_artist,
        )
        for artist in similar_artists:
            if len(result_provider) >= limit:
                break
            mapping = next(
                (
                    m
                    for m in artist.provider_mappings
                    if provider in {m.provider_instance, m.provider_domain}
                ),
                None,
            ) or next(iter(artist.provider_mappings), None)
            if not mapping:
                self.logger.debug("No mapping found for artist %s", artist.name)
                continue
            try:
                artist_tracks = await self.mass.music.artists.get_provider_artist_toptracks(
                    item_id=mapping.item_id,
                    provider_instance_id_or_domain=mapping.provider_instance,
                )
                self.logger.debug(
                    "Artist %s (%s): %d top tracks",
                    artist.name,
                    mapping.item_id,
                    len(artist_tracks),
                )
            except Exception as exc:
                self.logger.debug("Error fetching tracks for artist %s: %s", artist.name, exc)
                continue
            for track in artist_tracks[:per_artist]:
                if track.uri:
                    result_provider[track.uri] = track
        self.logger.debug("seed_artist provider mode: %d total tracks", len(result_provider))
        return list(result_provider.values())

    async def _update_playlist_description(
        self, library_item_id: int | str, rules: SmartPlaylistRules
    ) -> None:
        """Update the library playlist description with the rules summary."""
        try:
            playlist = await self.mass.music.playlists.get_library_item(library_item_id)
            updated = Playlist.from_dict(playlist.to_dict())
            updated.metadata.description = f"[Smart Playlist] {rules.human_readable()}"
            await self.mass.music.playlists.update_item_in_library(
                library_item_id, updated, overwrite=True
            )
        except Exception as exc:
            self.logger.debug("Could not update description for %s: %s", library_item_id, exc)

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
        except Exception as exc:
            self.logger.warning("Failed to load smart playlist rules: %s", exc)

    async def _save_rules(self, playlist_id: str, rules: SmartPlaylistRules) -> None:
        """Persist rules to disk and update in-memory store."""
        self._rules_store[playlist_id] = rules
        await self._flush_rules_to_disk()

    async def _flush_rules_to_disk(self) -> None:
        """Write all rules + names to disk as a single JSON file."""
        async with self._flush_lock:
            rules_file = os.path.join(self._rules_dir, RULES_FILENAME)
            data = {
                pid: {"name": self._names_store.get(pid, pid), "rules": r.to_dict()}
                for pid, r in self._rules_store.items()
            }
            await write_json(rules_file, data)
