"""
Rotation playlist provider for Music Assistant.

A Rotation is a dynamic playlist built from simple steps (source, filter, sort, limit, expand,
flatten). It plays endlessly in rounds and remembers per user what already played.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import uuid
from contextlib import aclosing, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType, MediaType, PlaybackState, ProviderFeature
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    InvalidProviderID,
    InvalidProviderURI,
    MediaNotFoundError,
)
from music_assistant_models.media_items import Playlist, ProviderMapping, Track, UniqueList
from music_assistant_models.media_items.metadata import MediaItemImage, MediaItemMetadata
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.music.favorites import filter_disliked
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.plugin import PluginProvider

from .constants import (
    BATCH_MAX_TRACKS,
    HOUSEHOLD,
    PREVIEW_COUNT_CAP,
    PREVIEW_MAX_LIMIT,
    PROGRESS_FILE,
    ROTATIONS_FILE,
    STORAGE_FOLDER,
    STORAGE_VERSION,
    TRANSLATION_OWNER,
)
from .engine import Node, RunContext, iter_units, playable_tracks
from .explain import explain_steps
from .fields import FIELDS, FieldType, StepsDescription, describe
from .models import (
    FilterStep,
    ItemKind,
    RotationDetails,
    Step,
    StepError,
    StepsError,
    item_kinds,
    parse_steps,
    steps_json_schema,
    steps_to_dict,
)
from .recipes import RECIPES, RecipeDescription
from .rounds import (
    ProgressStore,
    RoundState,
    TrackFilter,
    UnitTracks,
    new_seed,
    next_batch,
    played_for,
    round_status,
    track_uris,
)
from .storage import read_json, write_json
from .validation import validate_steps

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# SCROBBLE delivers the playback reports, which count the played tracks
SUPPORTED_FEATURES: set[ProviderFeature] = {ProviderFeature.BROWSE, ProviderFeature.SCROBBLE}
PLAYLIST_OWNER = "Rotation"
PROGRESS_SAVE_DELAY = 5


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RotationPlaylistProvider(mass, manifest, config, SUPPORTED_FEATURES)


def user_key() -> str:
    """Return the progress key of the current user, or the household key without a user."""
    user = get_current_user()
    return user.user_id if user else HOUSEHOLD


class RotationPlaylistProvider(PluginProvider):
    """Provides the Rotation playlists and the API to build them."""

    # the queue keeps the batches of this provider in the order they are returned
    keeps_batch_order = True

    _folder: str
    _names: dict[str, str]
    # entries this version cannot read, written back unchanged until they are deleted
    _unparsed: dict[str, Any]
    _unreadable_file: bool
    # a user whose handed out batch already belonged to the next round: the stored round
    # then, and the uris the user's queues played at that moment (still the old round)
    _pending_rounds: dict[tuple[str, str], _RoundMarker]
    # the tracks that played when the last round of a user ended; their late reports are old
    _ended_rounds: dict[tuple[str, str], frozenset[str]]
    # the queue item that was last seen to start playing, per queue
    _started_items: dict[str, str]
    _handed_units: dict[tuple[str, str], _HandedUnits]
    # the playlist description of each Rotation, built from its steps
    _descriptions: dict[str, str]
    _steps: dict[str, list[Step]]
    _progress: ProgressStore
    _unregister_handles: list[Callable[[], None]]
    _write_lock: asyncio.Lock
    _progress_dirty: bool
    _save_scheduled: bool

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries of this provider; Rotations need none."""
        return ()

    async def handle_async_init(self) -> None:
        """Load the Rotations and the progress from disk."""
        self._names = {}
        self._unparsed = {}
        self._unreadable_file = False
        self._pending_rounds = {}
        self._ended_rounds = {}
        self._started_items = {}
        self._handed_units = {}
        self._descriptions = {}
        self._steps = {}
        self._unregister_handles = []
        self._write_lock = asyncio.Lock()
        self._progress_dirty = False
        self._save_scheduled = False
        self._folder = os.path.join(self.mass.storage_path, STORAGE_FOLDER)
        await asyncio.to_thread(os.makedirs, self._folder, exist_ok=True)
        await self._load_rotations()
        self._progress = ProgressStore(os.path.join(self._folder, PROGRESS_FILE))
        await asyncio.to_thread(self._progress.load)

    async def loaded_in_mass(self) -> None:
        """Register the API commands and events, and add missing playlists to the library."""
        commands: list[tuple[str, Callable[..., Any], Scope]] = [
            ("rotations/create", self.create, Scope.LIBRARY_WRITE),
            ("rotations/update", self.update, Scope.LIBRARY_WRITE),
            ("rotations/delete", self.delete, Scope.LIBRARY_WRITE),
            ("rotations/get", self.get, Scope.LIBRARY_READ),
            ("rotations/list", self.list_rotations, Scope.LIBRARY_READ),
            ("rotations/validate", self.validate, Scope.LIBRARY_READ),
            ("rotations/preview", self.preview, Scope.LIBRARY_READ),
            ("rotations/explain", self.explain, Scope.LIBRARY_READ),
            ("rotations/describe_steps", self.describe_steps, Scope.LIBRARY_READ),
            ("rotations/recipes", self.recipes, Scope.LIBRARY_READ),
            ("rotations/reset_round", self.reset_round, Scope.LIBRARY_WRITE),
        ]
        for command, handler, scope in commands:
            self._unregister_handles.append(
                self.mass.register_api_command(command, handler, required_scope=scope)
            )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_queue_updated, EventType.QUEUE_UPDATED)
        )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_player_removed, EventType.PLAYER_REMOVED)
        )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_media_item_deleted, EventType.MEDIA_ITEM_DELETED)
        )
        self._unregister_handles.append(
            self.mass.subscribe(self._on_media_item_updated, EventType.MEDIA_ITEM_UPDATED)
        )
        self.mass.create_task(self._reconcile_library(), task_id=self._reconcile_task_id)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload of the provider; save pending progress.

        :param is_removed: True when the provider is removed, which also removes its playlists.
        """
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        self.mass.cancel_task(self._reconcile_task_id)
        self.mass.cancel_timer(self._save_task_id)
        if is_removed:
            for rotation_id in [*self._steps, *self._unparsed]:
                await self._remove_from_library(rotation_id)
            for name in (ROTATIONS_FILE, PROGRESS_FILE):
                with suppress(FileNotFoundError):
                    await asyncio.to_thread(os.remove, os.path.join(self._folder, name))
            return
        if self._progress_dirty:
            await self._save_progress()

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse the Rotations.

        :param path: The path to browse; there are no sub folders.
        """
        library_items = await self._library_playlists()
        return [
            await self._build_playlist(rotation_id, library_items.get(rotation_id))
            for rotation_id in self._steps
        ]

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Return the playlist of a Rotation.

        :param prov_playlist_id: The Rotation id.
        """
        rotation_id = await self._resolve_to_provider_id(prov_playlist_id)
        if rotation_id is None:
            raise MediaNotFoundError(
                f"Rotation {prov_playlist_id} not found",
                translation_key="rotation_not_found",
                translation_args=[prov_playlist_id],
                translation_owner=TRANSLATION_OWNER,
            )
        return await self._build_playlist(rotation_id)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Return the next unplayed tracks of the current user's round on page 0.

        Handing out tracks does not count as played; later pages are always empty.

        :param prov_playlist_id: The Rotation id.
        :param page: The page; only page 0 has tracks.
        """
        if page > 0:
            return []
        rotation_id = await self._resolve_to_provider_id(prov_playlist_id)
        if rotation_id is None:
            return []
        key = user_key()
        stored = self._progress.get(rotation_id, key)
        current = self._current_items(rotation_id, key)
        # a copy, so browsing never changes the progress, not even when a round is used up
        state = self._state_with_current(rotation_id, key, stored, current)
        units: UnitTracks = {}
        try:
            # the caller asks for a fresh batch and bypasses the cache for it; the album and
            # artist listings the steps read must still come from the cache
            async with self.mass.cache.handle_refresh(False):
                batch = await next_batch(
                    self._steps[rotation_id],
                    state,
                    self._ctx_factory,
                    BATCH_MAX_TRACKS,
                    await self._track_filter(key),
                    units,
                )
        except StepsError as err:
            self.logger.warning("Rotation %s cannot run: %s", self._names[rotation_id], err)
            return []
        self._remember_units(rotation_id, key, state, units)
        if state.round == stored.round:
            return batch
        pending = self._pending_rounds.get((rotation_id, key))
        # the first marker stays: a later refill may already run while round 2 plays
        if pending is None or pending.round != stored.round:
            self._pending_rounds[(rotation_id, key)] = _RoundMarker(
                stored.round,
                frozenset(str(item.media_item.uri) for item in current if item.media_item),
                frozenset(item.queue_item_id for item in current),
            )
        playing = {
            uri for item in current if item.media_item for uri in track_uris(item.media_item)
        }
        if batch and track_uris(batch[0]) & playing:
            # the new round would start with the track that ends the old one, right behind it
            return [*batch[1:], batch[0]]
        return batch

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """
        Mark a started track as played for the user of the queue that played it.

        :param report: The playback progress report of the played item.
        """
        if not report.player_id:
            return
        queue_data = self.mass.player_queues.queue_data_or_none(report.player_id)
        if queue_data is None:
            return
        rotation_ids = self._rotation_ids_in(queue_data.source_items)
        if not rotation_ids:
            return
        key = report.userid or HOUSEHOLD
        # the queue item knows the provider mappings of the reported track
        item = _reported_item(queue_data, report)
        aliases = track_uris(item.media_item) if item and item.media_item else set()
        item_id = item.queue_item_id if item else None
        changed = False
        for rotation_id in rotation_ids:
            pending = self._pending_rounds.get((rotation_id, key))
            new_round_item = (
                pending is not None and report.is_playing and report.uri not in pending.uris
            )
            if self._started(
                rotation_id,
                key,
                report.uri,
                aliases,
                new_round_item,
                item_id,
                late=not report.is_playing,
            ):
                changed = True
        if changed:
            self._schedule_progress_save()

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Return the provider icon as the fallback image.

        :param path: The image path.
        """
        if path == "icon.svg":
            icon_path = Path(__file__).parent / "icon.svg"
            return await asyncio.to_thread(icon_path.read_bytes)
        return path

    async def create(self, name: str, steps: list[dict[str, Any]]) -> Playlist:
        """
        Create a Rotation and add its playlist to the library.

        :param name: The name of the Rotation.
        :param steps: The steps document.
        :raises StepsError: When the steps are not valid.
        """
        parsed = self._checked_steps(steps)
        rotation_id = str(uuid.uuid4())
        self._names[rotation_id] = self._checked_name(name)
        self._steps[rotation_id] = parsed
        await self._save_rotations()
        playlist = await self.mass.music.playlists.add_item_to_library(
            await self._build_playlist(rotation_id, library_item=None)
        )
        self.mass.metadata.schedule_update_metadata(playlist)
        return playlist

    async def update(
        self,
        rotation_id: str,
        name: str | None = None,
        steps: list[dict[str, Any]] | None = None,
    ) -> Playlist:
        """
        Change the name and/or the steps of a Rotation.

        :param rotation_id: The Rotation id or the id of its library playlist.
        :param name: The new name, or None to keep it.
        :param steps: The new steps document, or None to keep the steps.
        :raises StepsError: When the steps are not valid.
        """
        prov_id = await self._require(rotation_id)
        parsed = self._checked_steps(steps) if steps is not None else None
        if name is not None:
            self._names[prov_id] = self._checked_name(name)
        if parsed is not None:
            self._steps[prov_id] = parsed
            self._descriptions.pop(prov_id, None)
            # a played unit may hold tracks the new steps keep and the old ones left out
            self._drop_handed_units(prov_id)
            if self._progress.clear_played_units(prov_id):
                self._schedule_progress_save()
        await self._save_rotations()
        library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
            prov_id, self.instance_id
        )
        if library_item is None:
            return await self._build_playlist(prov_id, library_item=None)
        updated = Playlist.from_dict(library_item.to_dict())
        updated.name = self._names[prov_id]
        updated.metadata.description = await self._description(prov_id)
        return await self.mass.music.playlists.update_item_in_library(
            library_item.item_id, updated, overwrite=True
        )

    async def delete(self, rotation_id: str) -> None:
        """
        Delete a Rotation, its playlist and the progress of all users.

        :param rotation_id: The Rotation id or the id of its library playlist.
        """
        prov_id = rotation_id if rotation_id in self._unparsed else await self._require(rotation_id)
        await self._forget(prov_id)
        await self._remove_from_library(prov_id)

    async def get(self, rotation_id: str) -> RotationDetails:
        """
        Return a Rotation with its steps and the round status of the current user.

        :param rotation_id: The Rotation id or the id of its library playlist.
        """
        prov_id = await self._require(rotation_id)
        steps = self._steps[prov_id]
        key = user_key()
        state = self._progress.get(prov_id, key)
        error_key: str | None = None
        error_args: list[Any] = []
        try:
            status: dict[str, Any] = await round_status(
                steps, state, self._ctx_factory, await self._track_filter(key)
            )
        except StepsError as err:
            error_key, error_args = err.translation_key, err.translation_args
            status = {"round": state.round, "error": str(err)}
        return RotationDetails(
            id=prov_id,
            name=self._names[prov_id],
            steps=steps_to_dict(steps),
            playlist=(await self._playlist_for(prov_id)).to_dict(),
            round_status=status,
            round_error_key=error_key,
            round_error_args=error_args,
        )

    async def list_rotations(self) -> list[dict[str, Any]]:
        """Return all Rotations with their names and playlists."""
        library_items = await self._library_playlists()
        return [
            {
                "id": prov_id,
                "name": name,
                "playlist": (
                    library_items.get(prov_id)
                    or await self._build_playlist(prov_id, library_item=None)
                ).to_dict(),
            }
            for prov_id, name in self._names.items()
        ]

    async def validate(self, steps: list[dict[str, Any]]) -> list[StepError]:
        """
        Check a steps document and return its problems, or an empty list when it is valid.

        :param steps: The steps document.
        """
        try:
            parsed = parse_steps(steps)
        except StepsError as err:
            return err.errors
        return self._step_errors(parsed)

    async def preview(
        self,
        steps: list[dict[str, Any]],
        limit: int,
        include_removed: bool = False,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """
        Return the first tracks of a steps document in play order and the count per step.

        :param steps: The steps document.
        :param limit: The number of tracks to return, from 1 to 200.
        :param include_removed: Also return the tracks a filter left out, marked as removed.
        :param seed: The seed of the random order; a new one when None.
        :raises StepsError: When the steps are not valid.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise InvalidDataError(
                "The limit must be a whole number",
                translation_key="preview_limit_not_number",
                translation_owner=TRANSLATION_OWNER,
            )
        if not 1 <= limit <= PREVIEW_MAX_LIMIT:
            raise InvalidDataError(
                f"The limit must be between 1 and {PREVIEW_MAX_LIMIT}",
                translation_key="preview_limit_out_of_range",
                translation_args=[PREVIEW_MAX_LIMIT],
                translation_owner=TRANSLATION_OWNER,
            )
        parsed = self._checked_steps(steps)
        ctx = RunContext(
            self.mass,
            seed=seed if seed is not None else new_seed(),
            collect_removed=include_removed,
            count_cap=PREVIEW_COUNT_CAP,
            logger=self.logger,
        )
        groups: list[dict[str, Any]] = []
        shown = total = duration = 0
        counted_all = item_kinds(parsed)[-1][0] == ItemKind.TRACK
        async with aclosing(iter_units(parsed, ctx)) as units:
            async for unit in units:
                if shown < limit:
                    entries = await self._preview_tracks(unit, ctx)
                    kept = [entry for entry in entries if not entry["removed"]]
                    entries = self._cut(entries, limit - shown)
                    shown += sum(1 for entry in entries if not entry["removed"])
                    groups.append(
                        {
                            "item": unit.item.to_dict(),
                            "tracks": [
                                {**entry, "track": entry["track"].to_dict()} for entry in entries
                            ],
                        }
                    )
                    tracks = [entry["track"] for entry in kept]
                    # the rest is only counted, which must not cost provider calls
                    ctx.count_only_library = shown >= limit
                else:
                    # album or artist leaves would need a fetch, so only the tracks count
                    tracks = [leaf for leaf in _leaf_items(unit) if isinstance(leaf, Track)]
                total += len(tracks)
                duration += sum(track.duration or 0 for track in tracks)
        step_counts = [asdict(stats) for stats in ctx.stats]
        return {
            "groups": groups,
            "step_counts": step_counts,
            "total_tracks": total,
            "total_exact": counted_all
            and not ctx.skipped_provider
            and all(stats["exact"] for stats in step_counts),
            "duration_seconds": duration,
        }

    async def explain(self, steps: list[dict[str, Any]]) -> list[str]:
        """
        Return the steps as plain English sentences, one per step.

        :param steps: The steps document.
        :raises StepsError: When the steps do not have the right shape.
        """
        return await self._explain(parse_steps(steps))

    async def describe_steps(self, media_type: str | None = None) -> StepsDescription:
        """
        Describe the step types, fields, operators and sort keys, and this install's providers.

        :param media_type: Only describe this media type (artist, album or track).
        """
        kind: ItemKind | None = None
        if media_type is not None:
            try:
                kind = ItemKind(media_type)
            except ValueError as err:
                kinds = ", ".join(item.value for item in ItemKind)
                raise InvalidDataError(
                    f"Unknown media type {media_type!r}; use {kinds}",
                    translation_key="unknown_media_type",
                    translation_args=[repr(media_type), kinds],
                    translation_owner=TRANSLATION_OWNER,
                ) from err
        providers = [
            {"instance_id": provider.instance_id, "domain": provider.domain, "name": provider.name}
            for provider in self.mass.music.providers
        ]
        result = describe(kind, providers)
        result.schema = steps_json_schema()
        return result

    async def recipes(self) -> list[RecipeDescription]:
        """Return the recipes as complete steps documents with a one line goal."""
        return [
            RecipeDescription(
                id=recipe.id,
                name=recipe.name,
                goal=recipe.goal,
                steps=steps_to_dict(recipe.steps),
            )
            for recipe in RECIPES
        ]

    async def reset_round(self, rotation_id: str) -> None:
        """
        Start a new round of a Rotation for the current user.

        :param rotation_id: The Rotation id or the id of its library playlist.
        """
        prov_id = await self._require(rotation_id)
        key = user_key()
        self._pending_rounds.pop((prov_id, key), None)
        self._ended_rounds.pop((prov_id, key), None)
        self._handed_units.pop((prov_id, key), None)
        self._progress.reset(prov_id, key)
        await self._save_progress()

    @property
    def _save_task_id(self) -> str:
        return f"rotation_playlist_save_progress_{self.instance_id}"

    @property
    def _reconcile_task_id(self) -> str:
        return f"rotation_playlist_reconcile_{self.instance_id}"

    def _ctx_factory(self, seed: int) -> RunContext:
        """Return a fresh run context for a seed."""
        return RunContext(self.mass, seed=seed, logger=self.logger)

    async def _track_filter(self, key: str) -> TrackFilter:
        """Return the filter that keeps the tracks the queue would play for a user."""
        # the queue leaves out unavailable tracks and the user's dislikes, by track, album, artist
        # or genre, so they are not part of the user's round either; the household has no dislikes
        store = self.mass.music.favorites
        dislikes = await store.disliked_track_keys(key) if key != HOUSEHOLD else None
        genres = bool(await store.disliked_genre_ids(key)) if key != HOUSEHOLD else False

        async def keep(tracks: list[Track]) -> list[Track]:
            available = [track for track in tracks if track.available]
            if dislikes:
                available = filter_disliked(available, dislikes)
            if not genres or not available:
                return available
            in_genre = await store.in_disliked_genres(key, available)
            return [track for track in available if (track.provider, track.item_id) not in in_genre]

        return keep

    def _checked_steps(self, steps: Any) -> list[Step]:
        """Return parsed steps, or raise StepsError with every problem found."""
        parsed = parse_steps(steps)
        if errors := self._step_errors(parsed):
            raise StepsError(errors)
        return parsed

    def _step_errors(self, steps: list[Step]) -> list[StepError]:
        """Return the field problems of parsed steps."""
        return validate_steps(steps)

    def _checked_name(self, name: Any) -> str:
        """Return a cleaned name, or raise when it is empty."""
        if not isinstance(name, str) or not name.strip():
            raise InvalidDataError(
                "A Rotation needs a name",
                translation_key="name_missing",
                translation_owner=TRANSLATION_OWNER,
            )
        return name.strip()

    def _rotation_ids_in(self, items: Sequence[MediaItemType], unparsed: bool = False) -> set[str]:
        """Return the ids of the known Rotations among media items, optionally unreadable ones."""
        found: set[str] = set()
        for item in items:
            if item.provider == self.instance_id:
                found.add(item.item_id)
            found.update(
                mapping.item_id
                for mapping in item.provider_mappings
                if mapping.provider_instance == self.instance_id
            )
        return {
            rotation_id
            for rotation_id in found
            if rotation_id in self._steps or (unparsed and rotation_id in self._unparsed)
        }

    def _current_items(self, rotation_id: str, key: str) -> list[QueueItem]:
        """Return the items that the queues of a user currently play from a Rotation."""
        items: list[QueueItem] = []
        for queue in self.mass.player_queues.all():
            queue_data = self.mass.player_queues.queue_data_or_none(queue.queue_id)
            if queue_data is None or (queue_data.userid or HOUSEHOLD) != key:
                continue
            if rotation_id not in self._rotation_ids_in(queue_data.source_items):
                continue
            current = queue.current_item
            if current is not None and current.media_item is not None:
                items.append(current)
        return items

    def _state_with_current(
        self, rotation_id: str, key: str, stored: RoundState, current: list[QueueItem]
    ) -> RoundState:
        """Return a copy of the stored state in which the tracks that play now count as started."""
        state = stored.copy()
        pending = self._pending_rounds.get((rotation_id, key))
        # their start may not be counted yet; an item that started after the old round ended
        # already belongs to the next round
        newer = [
            item
            for item in current
            if pending is not None
            and pending.round == stored.round
            and item.queue_item_id not in pending.item_ids
        ]
        if newer:
            state.advance()
        units = self._units_of(rotation_id, key, state)
        for item in newer or current:
            if item.media_item is None or state.has_play(item.queue_item_id):
                continue
            uris = track_uris(item.media_item)
            state.add_play(uris, item.queue_item_id, played_for(state, units, uris))
        return state

    def _started(
        self,
        rotation_id: str,
        key: str,
        uri: str,
        aliases: set[str],
        new_round_item: bool,
        item_id: str | None,
        late: bool = False,
    ) -> bool:
        """
        Count a started track for a user and start the pending next round when it is due.

        :param rotation_id: The Rotation.
        :param key: The user, or the household key.
        :param uri: The uri of the track.
        :param aliases: Other uris the same track is known by.
        :param new_round_item: Whether the track would belong to the pending next round.
        :param item_id: The queue item that plays the track, when it is known.
        :param late: Whether this is a report of a track that no longer plays.
        """
        changed = False
        pending = self._pending_rounds.get((rotation_id, key))
        if pending is not None and pending.round != self._progress.get(rotation_id, key).round:
            self._pending_rounds.pop((rotation_id, key))
        elif pending is not None and new_round_item:
            # a new item started after the old round ended: the next round plays now
            self._pending_rounds.pop((rotation_id, key))
            self._ended_rounds[(rotation_id, key)] = pending.uris
            self._progress.next_round(rotation_id, key)
            changed = True
        elif pending is None and late and uri in self._ended_rounds.get((rotation_id, key), ()):
            # the last report of the track that ended the previous round
            return False
        state = self._progress.get(rotation_id, key)
        if item_id is not None and state.has_play(item_id):
            # a later report of a play that is already counted
            return changed
        unit = played_for(state, self._units_of(rotation_id, key, state), {uri, *aliases})
        if self._progress.mark_started(rotation_id, key, uri, aliases, item_id, unit):
            changed = True
        return self._mark_finished_unit(rotation_id, key, unit) or changed

    def _remember_units(
        self, rotation_id: str, key: str, state: RoundState, units: UnitTracks
    ) -> None:
        """Keep the units a batch of a user's round took tracks from, for the played reports."""
        handed = self._handed_units.get((rotation_id, key))
        if handed is None or (handed.round, handed.seed) != (state.round, state.seed):
            self._handed_units[(rotation_id, key)] = _HandedUnits(state.round, state.seed, units)
            return
        handed.units.update(units)

    def _drop_handed_units(self, rotation_id: str) -> None:
        """Forget the handed out units of a Rotation for all users."""
        for handed in [key for key in self._handed_units if key[0] == rotation_id]:
            self._handed_units.pop(handed)

    def _units_of(self, rotation_id: str, key: str, state: RoundState) -> UnitTracks:
        """Return the units handed out to a user in the round of a state, in their order."""
        handed = self._handed_units.get((rotation_id, key))
        if handed is None or (handed.round, handed.seed) != (state.round, state.seed):
            return {}
        return handed.units

    def _mark_finished_unit(self, rotation_id: str, key: str, unit: str | None) -> bool:
        """Mark a unit as played once all its kept tracks played as part of it; True on a change."""
        state = self._progress.get(rotation_id, key)
        handed = self._units_of(rotation_id, key, state).get(unit) if unit else None
        if unit is None or handed is None or not handed.complete or unit in state.played_units:
            return False
        plays = state.plays_of(unit)
        for track in handed.tracks:
            if (play := next((play for play in plays if play & track), None)) is None:
                return False
            plays.remove(play)
        return self._progress.mark_unit_played(rotation_id, key, unit)

    def _schedule_progress_save(self) -> None:
        """Save the progress once, a few seconds after the first unsaved change."""
        self._progress_dirty = True
        if self._save_scheduled:
            return
        self._save_scheduled = True
        self.mass.call_later(PROGRESS_SAVE_DELAY, self._save_progress, task_id=self._save_task_id)

    def _cut(self, entries: list[dict[str, Any]], wanted: int) -> list[dict[str, Any]]:
        """Return the entries up to the wanted number of kept tracks."""
        result: list[dict[str, Any]] = []
        kept = 0
        for entry in entries:
            if kept >= wanted:
                break
            result.append(entry)
            kept += 0 if entry["removed"] else 1
        return result

    async def _preview_tracks(self, node: Node, ctx: RunContext) -> list[dict[str, Any]]:
        """Return the tracks of a unit in play order, with the removed tracks in place."""
        if node.children is None:
            if isinstance(node.item, Track):
                return [{"track": node.item, "removed": False}]
            return [
                {"track": track, "removed": False} for track in await playable_tracks(node, ctx)
            ]
        entries: list[dict[str, Any]] = []
        for child in node.children:
            entries.extend(await self._preview_tracks(child, ctx))
        removed = [
            {"track": child.item, "removed": True}
            for child in node.removed
            if isinstance(child.item, Track)
        ]
        return _merge_removed(entries, removed)

    async def _explain(self, steps: list[Step]) -> list[str]:
        """Explain steps, with the names of the library items the conditions refer to."""
        kinds = item_kinds(steps)
        names: dict[str, str] = {}
        for index, step in enumerate(steps):
            if not isinstance(step, FilterStep) or index == 0:
                continue
            kind = kinds[index - 1][0]
            for condition in step.conditions:
                definition = FIELDS[kind].get(condition.field)
                if definition is None or definition.type != FieldType.REF:
                    continue
                values = condition.value if isinstance(condition.value, list) else [condition.value]
                for value in values:
                    if isinstance(value, str) and value not in names:
                        if found := await self._library_name(value):
                            names[value] = found
        return explain_steps(steps, names)

    async def _library_name(self, uri: str) -> str | None:
        """Return the name of a library artist or album uri; other values give None."""
        if not uri.startswith("library://"):
            return None
        try:
            media_type, _provider, item_id = await parse_uri(uri)
        except InvalidProviderURI, InvalidProviderID:
            return None
        if media_type not in (MediaType.ARTIST, MediaType.ALBUM):
            return None
        music = self.mass.music
        controller = music.artists if media_type == MediaType.ARTIST else music.albums
        try:
            item = await controller.get_library_item(item_id)
        except (MediaNotFoundError, ValueError) as err:
            self.logger.debug("Unable to look up %s: %s", uri, err)
            return None
        return str(item.name)

    async def _description(self, rotation_id: str) -> str:
        """Return the playlist description: the steps as sentences."""
        if (description := self._descriptions.get(rotation_id)) is None:
            description = ". ".join(await self._explain(self._steps[rotation_id])) + "."
            self._descriptions[rotation_id] = description
        return description

    async def _library_playlists(self) -> dict[str, Playlist]:
        """Return the library playlists of the Rotations by Rotation id, in one query."""
        rotation_ids = list(self._steps)
        found = await self.mass.music.playlists.get_library_items_by_prov_id(
            provider_instance=self.instance_id,
            provider_item_ids=rotation_ids,
            limit=max(len(rotation_ids), 1),
        )
        return {
            mapping.item_id: playlist
            for playlist in found
            for mapping in playlist.provider_mappings
            if mapping.provider_instance == self.instance_id
        }

    async def _build_playlist(
        self, rotation_id: str, library_item: Playlist | bool | None = True
    ) -> Playlist:
        """
        Return the provider playlist of a Rotation.

        :param rotation_id: The Rotation id.
        :param library_item: The library playlist for its images, True to look it up.
        """
        if library_item is True:
            library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
                rotation_id, self.instance_id
            )
        images: UniqueList[MediaItemImage] = UniqueList([])
        if isinstance(library_item, Playlist) and library_item.metadata.images:
            images = library_item.metadata.images
        playlist = Playlist(
            item_id=rotation_id,
            provider=self.instance_id,
            name=self._names[rotation_id],
            owner=PLAYLIST_OWNER,
            is_editable=True,
            provider_mappings={
                ProviderMapping(
                    item_id=rotation_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                    in_library=True,
                )
            },
        )
        playlist.is_dynamic = True
        playlist.metadata = MediaItemMetadata(
            description=await self._description(rotation_id), images=images
        )
        return playlist

    async def _playlist_for(self, rotation_id: str) -> Playlist:
        """Return the library playlist of a Rotation, or its provider playlist."""
        library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
            rotation_id, self.instance_id
        )
        return library_item or await self._build_playlist(rotation_id, library_item=None)

    async def _require(self, rotation_id: str) -> str:
        """Return the Rotation id for a Rotation or library playlist id, or raise."""
        prov_id = await self._resolve_to_provider_id(rotation_id)
        if prov_id is None:
            raise MediaNotFoundError(
                f"Rotation {rotation_id} not found",
                translation_key="rotation_not_found",
                translation_args=[rotation_id],
                translation_owner=TRANSLATION_OWNER,
            )
        return prov_id

    async def _resolve_to_provider_id(self, playlist_id: str) -> str | None:
        """Resolve a library playlist id or a Rotation id to the Rotation id."""
        if playlist_id in self._steps:
            return playlist_id
        try:
            library_item = await self.mass.music.playlists.get_library_item(playlist_id)
        except (MediaNotFoundError, ValueError) as err:
            self.logger.debug("Could not resolve playlist id %s: %s", playlist_id, err)
            return None
        for mapping in library_item.provider_mappings:
            if mapping.provider_instance == self.instance_id and mapping.item_id in self._steps:
                return mapping.item_id
        return None

    async def _reconcile_library(self) -> None:
        """Add Rotations that are missing from the library again (for example after a reset)."""
        for rotation_id in list(self._steps):
            try:
                existing = await self.mass.music.playlists.get_library_item_by_prov_id(
                    rotation_id, self.instance_id
                )
                if existing is not None:
                    continue
                self.logger.info("Adding missing Rotation %s to the library", rotation_id)
                await self.mass.music.playlists.add_item_to_library(
                    await self._build_playlist(rotation_id, library_item=None)
                )
            except (
                MediaNotFoundError,
                InvalidDataError,
                InsufficientPermissions,
                sqlite3.Error,
            ) as err:
                # the error text is left out, as it may hold details of the library
                self.logger.warning(
                    "Could not add Rotation %s again: %s", rotation_id, type(err).__name__
                )

    async def _remove_from_library(self, rotation_id: str) -> None:
        """Remove the library playlist of a Rotation, when there is one."""
        try:
            library_item = await self.mass.music.playlists.get_library_item_by_prov_id(
                rotation_id, self.instance_id
            )
            if library_item is not None:
                await self.mass.music.remove_item_from_library(
                    MediaType.PLAYLIST, library_item.item_id
                )
        except (MediaNotFoundError, InsufficientPermissions, sqlite3.Error) as err:
            self.logger.debug("Could not remove Rotation %s from the library: %s", rotation_id, err)

    async def _forget(self, rotation_id: str) -> None:
        """Drop a Rotation and its progress from memory and disk."""
        if rotation_id not in self._steps and rotation_id not in self._unparsed:
            return
        self._steps.pop(rotation_id, None)
        self._descriptions.pop(rotation_id, None)
        self._names.pop(rotation_id, None)
        self._unparsed.pop(rotation_id, None)
        for pending in [key for key in self._pending_rounds if key[0] == rotation_id]:
            self._pending_rounds.pop(pending)
        for ended in [key for key in self._ended_rounds if key[0] == rotation_id]:
            self._ended_rounds.pop(ended)
        self._drop_handed_units(rotation_id)
        self._progress.drop_rotation(rotation_id)
        await self._save_rotations()
        await self._save_progress()

    async def _on_queue_updated(self, event: MassEvent) -> None:
        """Mark a track as played as soon as it starts, also when it is skipped right away."""
        # the playback reports leave out tracks that played only a few seconds
        queue = event.data
        if not isinstance(queue, PlayerQueue) or queue.state != PlaybackState.PLAYING:
            return
        item = queue.current_item
        if item is None or item.media_item is None:
            return
        if self._started_items.get(queue.queue_id) == item.queue_item_id:
            return
        self._started_items[queue.queue_id] = item.queue_item_id
        queue_data = self.mass.player_queues.queue_data_or_none(queue.queue_id)
        if queue_data is None:
            return
        key = queue_data.userid or HOUSEHOLD
        changed = False
        for rotation_id in self._rotation_ids_in(queue_data.source_items):
            pending = self._pending_rounds.get((rotation_id, key))
            new_round_item = pending is not None and item.queue_item_id not in pending.item_ids
            uri = str(item.media_item.uri)
            aliases = track_uris(item.media_item)
            if self._started(rotation_id, key, uri, aliases, new_round_item, item.queue_item_id):
                changed = True
        if changed:
            self._schedule_progress_save()

    async def _on_player_removed(self, event: MassEvent) -> None:
        """Forget the started item of the queue of a removed player."""
        if event.object_id is not None:
            self._started_items.pop(event.object_id, None)

    async def _on_media_item_deleted(self, event: MassEvent) -> None:
        """Delete the Rotation of a playlist that was removed from the library."""
        item = event.data
        if isinstance(item, Playlist):
            for rotation_id in self._rotation_ids_in([item], unparsed=True):
                await self._forget(rotation_id)

    async def _on_media_item_updated(self, event: MassEvent) -> None:
        """Keep the name of a Rotation in line with its renamed library playlist."""
        item = event.data
        if not isinstance(item, Playlist) or item.provider != "library":
            return
        for rotation_id in self._rotation_ids_in([item]):
            if self._names.get(rotation_id) != item.name and item.name:
                self._names[rotation_id] = item.name
                await self._save_rotations()

    async def _load_rotations(self) -> None:
        """Load the Rotations; entries that cannot be read are kept aside with a warning."""
        path = os.path.join(self._folder, ROTATIONS_FILE)
        rotations = (await asyncio.to_thread(read_json, path)).get("rotations")
        if not isinstance(rotations, dict):
            self._unreadable_file = await asyncio.to_thread(_has_content, path)
            return
        for rotation_id, entry in rotations.items():
            steps: list[Step] | None = None
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                try:
                    steps = parse_steps(entry.get("steps"))
                except StepsError as err:
                    self.logger.warning("Unable to read the Rotation %s: %s", rotation_id, err)
            else:
                self.logger.warning("Unable to read the Rotation %s: it has no name", rotation_id)
            if steps is None:
                self._unparsed[str(rotation_id)] = entry
                continue
            self._names[str(rotation_id)] = entry["name"]
            self._steps[str(rotation_id)] = steps

    async def _save_rotations(self) -> None:
        """Write all Rotations to disk."""
        rotations: dict[str, Any] = dict(self._unparsed)
        rotations.update(
            {
                rotation_id: {"name": self._names[rotation_id], "steps": steps_to_dict(steps)}
                for rotation_id, steps in self._steps.items()
            }
        )
        path = os.path.join(self._folder, ROTATIONS_FILE)
        async with self._write_lock:
            if self._unreadable_file:
                self.logger.warning(
                    "Keeping the unreadable %s as %s.broken before writing it again", path, path
                )
                await asyncio.to_thread(shutil.copyfile, path, f"{path}.broken")
                self._unreadable_file = False
            await write_json(path, {"version": STORAGE_VERSION, "rotations": rotations})

    async def _save_progress(self) -> None:
        """Write the progress of all users to disk."""
        self._save_scheduled = False
        self._progress_dirty = False
        async with self._write_lock:
            await self._progress.save()


def _has_content(path: str) -> bool:
    """Return whether a file exists and is not empty."""
    try:
        return Path(path).stat().st_size > 0
    except OSError:
        return False


def _reported_item(
    queue_data: PlayerQueueData, report: MediaItemPlaybackProgressReport
) -> QueueItem | None:
    """Return the queue item a playback report is about, or None when it is not found."""
    queue = queue_data.queue
    current = queue.current_item
    if (
        report.is_playing
        and current
        and current.media_item
        and current.media_item.uri == report.uri
    ):
        return current
    # a report of a track that ended: the last item with its uri before the current one
    index = queue.current_index if isinstance(queue.current_index, int) else None
    found = next(
        (
            item
            for item in reversed(queue_data.items[:index])
            if item.media_item is not None and item.media_item.uri == report.uri
        ),
        None,
    )
    if found is None and current and current.media_item and current.media_item.uri == report.uri:
        return current
    return found


def _leaf_items(node: Node) -> Sequence[MediaItemType]:
    """Return the items at the leaves of a unit."""
    if node.children is None:
        return [node.item]
    return [item for child in node.children for item in _leaf_items(child)]


def _merge_removed(
    entries: list[dict[str, Any]], removed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Put removed tracks back at their place in track order, or after the kept tracks."""
    if not removed:
        return entries

    def position(entry: dict[str, Any]) -> tuple[int, int]:
        track = entry["track"]
        return (track.disc_number or 0, track.track_number or 0)

    kept_positions = [position(entry) for entry in entries]
    if kept_positions != sorted(kept_positions):
        return entries + removed
    return sorted(entries + removed, key=position)


@dataclass(frozen=True)
class _RoundMarker:
    """A handed out batch that already belonged to the next round of a user."""

    # the stored round when the batch was handed out
    round: int
    # the tracks and the queue items that played then, still of the old round
    uris: frozenset[str]
    item_ids: frozenset[str]


@dataclass
class _HandedUnits:
    """The units the batches of a user's round took tracks from, with their kept tracks."""

    round: int
    seed: int
    units: UnitTracks
