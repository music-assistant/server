"""Manage MediaItems of type Playlist."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from itertools import batched
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientError
from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderURI,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import Playlist, PlaylistSummary, ProviderMapping, Track

from music_assistant.constants import DB_TABLE_PLAYLISTS, PLAYLIST_MEDIA_TYPES, PlaylistPlayableItem
from music_assistant.controllers.tasks.context import (
    get_current_task,
    report_current_task_failure,
    set_current_task_report,
    update_current_task_progress,
    update_current_task_progress_text,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.compare import TrackMatchConfidence
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.helpers.playlists import (
    PlaylistItem,
    generate_m3u,
    media_item_to_playlist_item,
)
from music_assistant.helpers.security import is_safe_name
from music_assistant.helpers.uri import create_uri, parse_uri
from music_assistant.helpers.util import guard_single_request
from music_assistant.models.music_provider import MusicProvider

from .audiobooks import AudiobooksController
from .base import MediaControllerBase
from .radio import RadioController
from .tracks import TrackProviderMatch, TracksController

_PROVIDER_PLAYLIST_ADD_BATCH_SIZE = 100
_MIGRATION_REPORT_DETAIL_LIMIT = 200
_MIGRATION_RESOLVE_BATCH_SIZE = 5
_MIGRATION_VERIFY_RETRY_DELAYS = (1, 2, 4)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.background_task import BackgroundTask

    from music_assistant import MusicAssistant
    from music_assistant.providers.builtin import BuiltinProvider


def _update_stage_progress(
    current: int,
    total: int,
    start: int,
    end: int,
    text: str,
) -> None:
    """Update progress for a bounded task stage without resetting overall progress."""
    if total <= 0:
        update_current_task_progress_text(text)
        return
    progress = start + int((current * (end - start)) / total)
    update_current_task_progress(min(progress, end), text)


class PlaylistMigrationMatchPolicy(StrEnum):
    """Allowed fallback depth for playlist track matching."""

    EXACT = "exact"
    SAME_RECORDING = "same_recording"
    BEST_EFFORT = "best_effort"


@dataclass(frozen=True, slots=True)
class _PlaylistMigrationTrackResult:
    """Resolved destination details for one source track."""

    track: Track | None = None
    mapping: ProviderMapping | None = None
    confidence: TrackMatchConfidence = TrackMatchConfidence.NO_MATCH
    provider_matches: tuple[TrackProviderMatch, ...] = ()
    ambiguous_providers: tuple[str, ...] = ()
    failed_providers: tuple[str, ...] = ()
    used_library_item: bool = False
    ambiguous: bool = False
    error: str | None = None


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = DB_TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist
    summary_item_cls = PlaylistSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/create_playlist",
            self.create_playlist,
            required_scope=Scope.LIBRARY_WRITE,
        )
        self.mass.register_api_command(
            "music/playlists/playlist_tracks", self.tracks, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            "music/playlists/add_playlist_tracks",
            self.add_playlist_tracks,
            required_scope=Scope.LIBRARY_WRITE,
        )
        self.mass.register_api_command(
            "music/playlists/remove_playlist_tracks",
            self.remove_playlist_tracks,
            required_scope=Scope.LIBRARY_WRITE,
        )
        self.mass.register_api_command(
            "music/playlists/export_playlist",
            self.export_playlist,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/playlists/import_playlist",
            self.import_playlist,
            required_scope=Scope.LIBRARY_WRITE,
        )
        self.mass.register_api_command(
            "music/playlists/migrate_playlist",
            self.migrate_playlist,
            required_scope=Scope.LIBRARY_WRITE,
        )

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for playlist summary listings."""
        query = f"""
        SELECT
            {self._summary_base_columns()},
            playlists.owner,
            playlists.is_editable,
            playlists.is_dynamic,
            playlists.supported_mediatypes,
            playlists.translation_key,
            playlists.translation_params,
            json_extract(playlists.metadata, '$.description') AS description,
            {self._provider_mappings_query()} AS provider_mappings
            FROM playlists"""
        return query, {}

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[PlaylistPlayableItem]:
        """Return playlist tracks for the given provider playlist id."""
        if provider_instance_id_or_domain == "library":
            library_item = await self.get_library_item(item_id)
            provider_instance_id_or_domain, item_id = self._select_provider_id(library_item)

        # Playback/refill requests for dynamic playlists need fresh tracks from the provider.
        # Browse requests may reuse cached tracks.
        if allow_dynamic_tracks:
            force_refresh = True

        # playlist tracks are not stored in the db, we always fetch them (cached) from the
        # provider. The provider decides how many tracks to return; for a dynamic playlist it
        # returns a bounded sample/batch and terminates by yielding no further pages.
        page = 0
        while True:
            tracks = await self._get_provider_playlist_tracks(
                item_id,
                provider_instance_id_or_domain,
                page=page,
                force_refresh=force_refresh,
            )
            if not tracks:
                break
            for track in tracks:
                yield track
            page += 1

    async def create_playlist(
        self,
        name: str,
        media_types: list[MediaType] | None = None,
        provider_instance_or_domain: str | None = None,
    ) -> Playlist:
        """Create new playlist."""
        # if provider is omitted, just pick builtin provider
        if provider_instance_or_domain:
            provider = self.mass.get_provider(provider_instance_or_domain)
            if provider is None:
                raise ProviderUnavailableError
        else:
            provider = self.mass.get_provider("builtin")

        # Default is track for backwards compatibility.
        media_types_set = {MediaType.TRACK} if not media_types else set(media_types)
        if MediaType.ALBUM in media_types_set:
            # an album is unwrapped, so we remove that and use tracks instead
            media_types_set.remove(MediaType.ALBUM)
            media_types_set.add(MediaType.TRACK)
        if not provider_instance_or_domain and not media_types_set:
            # builtin can handle all media_types
            media_types_set.update(
                (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE, MediaType.RADIO)
            )

        provider = cast("MusicProvider", provider)

        mix_allowed = ProviderFeature.PLAYLIST_CREATE_MIXED in provider.supported_features
        supported_types: set[MediaType] = set()
        if (
            ProviderFeature.PLAYLIST_CREATE in provider.supported_features
            or ProviderFeature.PLAYLIST_CREATE_TRACKS in provider.supported_features
        ):
            # PLAYLIST_CREATE is deprecated
            supported_types.add(MediaType.TRACK)
        if ProviderFeature.PLAYLIST_CREATE_AUDIOBOOKS in provider.supported_features:
            supported_types.add(MediaType.AUDIOBOOK)
        if ProviderFeature.PLAYLIST_CREATE_PODCAST_EPISODES in provider.supported_features:
            supported_types.add(MediaType.PODCAST_EPISODE)
        if ProviderFeature.PLAYLIST_CREATE_RADIOS in provider.supported_features:
            supported_types.add(MediaType.RADIO)

        if not supported_types:
            msg = f"Provider {provider.name} does not support creating playlists"
            raise InvalidDataError(msg)

        if not is_safe_name(name):
            msg = f"{name} is not a valid Playlist name"
            raise InvalidDataError(msg)

        if len(media_types_set.difference(supported_types)) > 0:
            msg = f"Provider {provider.name} only supports {supported_types} in playlists."
            raise InvalidDataError(msg)
        if len(media_types_set) > 1 and not mix_allowed:
            msg = f"Provider {provider.name} does not support mixed media_types in playlists."
            raise InvalidDataError(msg)

        # create playlist on the provider
        playlist = await provider.create_playlist(name, media_types=media_types_set)
        for prov_mapping in playlist.provider_mappings:
            # when manually creating a playlist, it's always in the library
            prov_mapping.in_library = True
        # add the new playlist to the library
        return await self.add_item_to_library(playlist, False)

    async def add_playlist_tracks(
        self, db_playlist_id: str | int, uris: list[str]
    ) -> BackgroundTask:
        """
        Queue adding items to a playlist.

        :param db_playlist_id: Library playlist id.
        :param uris: Item URIs to add to the playlist.
        :return: Managed background task for the requested playlist update.
        """
        playlist_name = str(db_playlist_id)
        with suppress(MediaNotFoundError):
            playlist_name = (await self.get_library_item(int(db_playlist_id))).name
        user = get_current_user()
        return self.mass.tasks.run_background_task(
            name=f"Add items to playlist {playlist_name}",
            handler=lambda: self._handle_add_playlist_tracks(db_playlist_id, uris),
            translation_key="add_playlist_tracks",
            translation_owner=self.translation_owner,
            translation_args=[playlist_name],
            user_id=user.user_id if user else None,
            metadata={
                "task_domain": "playlist_add_tracks",
                "playlist_id": str(db_playlist_id),
                "playlist_name": playlist_name,
                "item_count": len(uris),
            },
            allow_retry=True,
            priority=True,
        )

    async def add_playlist_track(self, db_playlist_id: str | int, track_uri: str) -> None:
        """Add (single) track to playlist."""
        await self._handle_add_playlist_tracks(db_playlist_id, [track_uri])

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> BackgroundTask:
        """
        Queue removing items from a playlist.

        :param db_playlist_id: Library playlist id.
        :param positions_to_remove: Provider playlist positions to remove.
        :return: Managed background task for the requested playlist update.
        """
        playlist_name = str(db_playlist_id)
        with suppress(MediaNotFoundError):
            playlist_name = (await self.get_library_item(int(db_playlist_id))).name
        user = get_current_user()
        return self.mass.tasks.run_background_task(
            name=f"Remove items from playlist {playlist_name}",
            handler=lambda: self._handle_remove_playlist_tracks(
                db_playlist_id, positions_to_remove
            ),
            translation_key="remove_playlist_tracks",
            translation_owner=self.translation_owner,
            translation_args=[playlist_name],
            user_id=user.user_id if user else None,
            metadata={
                "task_domain": "playlist_remove_tracks",
                "playlist_id": str(db_playlist_id),
                "playlist_name": playlist_name,
                "item_count": len(positions_to_remove),
            },
            priority=True,
        )

    async def match_providers(self, db_item: Playlist) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) item.

        This is used to link objects of different providers/qualities together.
        """
        # playlists can only be matched on the same provider (if not unique)
        if self.mass.music.match_provider_instances(db_item):
            await self.add_provider_mappings(db_item.item_id, db_item.provider_mappings)

    async def export_playlist(self, db_playlist_id: str | int) -> str:
        """
        Export a playlist to M3U8 format.

        :param db_playlist_id: The library database ID of the playlist.
        """
        db_id = int(db_playlist_id)
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        items: list[PlaylistItem] = []
        async for track in self.tracks(
            item_id=str(db_id),
            provider_instance_id_or_domain="library",
        ):
            items.append(media_item_to_playlist_item(track))
        playlist_image_url = playlist.image.path if playlist.image else None
        return generate_m3u(playlist.name, items, playlist_image_url)

    async def import_playlist(
        self,
        m3u_data: str,
        library_matching: bool = False,
        match_providers: list[str] | None = None,
    ) -> Playlist:
        """
        Import a playlist from M3U8 format.

        Creates a new builtin playlist from the provided M3U data.

        :param m3u_data: The M3U8 playlist data as a string.
        :param library_matching: When True, attempt to find tracks by searching
            providers using metadata when the original URI's provider is not
            available. Defaults to False.
        :param match_providers: Optional list of provider instance IDs or domains
            to search when library_matching is enabled.
        """
        provider = self.mass.get_provider("builtin")
        if not provider or not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError("Builtin provider is not available")
        builtin_prov = cast("BuiltinProvider", provider)
        playlist = await builtin_prov.import_playlist(m3u_data)
        for prov_mapping in playlist.provider_mappings:
            prov_mapping.in_library = True
        db_playlist = await self.add_item_to_library(playlist, False)
        if library_matching:
            prov_playlist_id = playlist.item_id
            user = get_current_user()
            self.mass.tasks.run_background_task(
                name=f"Import playlist {db_playlist.name}",
                handler=lambda: builtin_prov.match_imported_playlist_tracks(
                    prov_playlist_id, match_providers
                ),
                translation_key="import_playlist_matching",
                translation_owner=self.translation_owner,
                translation_args=[db_playlist.name],
                user_id=user.user_id if user else None,
                metadata={
                    "task_domain": "playlist_import_matching",
                    "playlist_id": str(db_playlist.item_id),
                    "playlist_name": db_playlist.name,
                },
                allow_retry=True,
                allow_cancel=True,
                priority=True,
            )
        return db_playlist

    async def migrate_playlist(
        self,
        db_playlist_id: str | int,
        destination_provider: str = "builtin",
        name: str | None = None,
        match_policy: PlaylistMigrationMatchPolicy = PlaylistMigrationMatchPolicy.SAME_RECORDING,
    ) -> BackgroundTask:
        """
        Queue copying a playlist to another provider or Music Assistant.

        :param db_playlist_id: Library database ID of the source playlist.
        :param destination_provider: Destination provider instance ID or domain.
        :param name: Optional destination playlist name.
        :param match_policy: Lowest track-match confidence that may be accepted.
        :return: Managed background task performing the migration.
        """
        source_playlist = await self.get_library_item(int(db_playlist_id))
        if source_playlist.is_dynamic:
            raise InvalidDataError("Dynamic playlists can not be migrated")
        available_providers = self.mass.music.providers
        provider = next(
            (item for item in available_providers if item.instance_id == destination_provider),
            None,
        ) or next(
            (item for item in available_providers if item.domain == destination_provider),
            None,
        )
        if provider is None and destination_provider == "builtin":
            provider = self.mass.get_provider(
                "builtin",
                provider_type=MusicProvider,
            )
        if not provider or not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError(f"Provider {destination_provider} is not available")
        if provider.domain != "builtin" and not provider.is_streaming_provider:
            raise InvalidDataError(
                "Playlists can only be migrated to Music Assistant or a streaming provider"
            )
        if not (
            {
                ProviderFeature.PLAYLIST_CREATE,
                ProviderFeature.PLAYLIST_CREATE_TRACKS,
            }
            & provider.supported_features
        ):
            raise InvalidDataError(
                f"Provider {provider.name} does not support creating track playlists"
            )
        if ProviderFeature.PLAYLIST_TRACKS_EDIT not in provider.supported_features:
            raise InvalidDataError(f"Provider {provider.name} does not support editing playlists")
        if MediaType.TRACK not in provider.supported_media_types:
            raise InvalidDataError(f"Provider {provider.name} does not support track playlists")
        destination_name = name or source_playlist.name
        if not is_safe_name(destination_name):
            raise InvalidDataError(f"{destination_name} is not a valid Playlist name")

        user = get_current_user()
        source_provider, source_item_id = self._select_provider_id(source_playlist)
        allowed_provider_instances = {item.instance_id for item in available_providers}
        if source_provider not in allowed_provider_instances:
            source_provider_obj = self.mass.get_provider(
                source_provider,
                provider_type=MusicProvider,
            )
            if not source_provider_obj or source_provider_obj.domain != "builtin":
                raise ProviderUnavailableError(f"Provider {source_provider} is not available")
            allowed_provider_instances.add(source_provider)
        allowed_provider_instances.add(provider.instance_id)
        return self.mass.tasks.run_background_task(
            name=f"Migrate playlist {source_playlist.name}",
            handler=lambda: self._handle_migrate_playlist(
                source_playlist.item_id,
                source_item_id,
                source_provider,
                provider.instance_id,
                destination_name,
                match_policy,
                tuple(sorted(allowed_provider_instances)),
            ),
            user_id=user.user_id if user else None,
            metadata={
                "task_domain": "playlist_migration",
                "source_playlist_id": source_playlist.item_id,
                "source_playlist_name": source_playlist.name,
                "destination_provider": provider.instance_id,
                "destination_provider_name": provider.name,
                "match_policy": match_policy.value,
            },
            allow_cancel=True,
            priority=True,
        )

    async def _handle_migrate_playlist(
        self,
        source_playlist_id: str,
        source_playlist_item_id: str,
        source_provider: str,
        destination_provider: str,
        destination_name: str,
        match_policy: PlaylistMigrationMatchPolicy,
        allowed_provider_instances: tuple[str, ...],
    ) -> None:
        """Resolve and copy a playlist inside a managed task."""
        source_playlist = await self.get_library_item(source_playlist_id)
        if source_playlist.is_dynamic:
            raise InvalidDataError("Dynamic playlists can not be migrated")
        provider = self.mass.get_provider(destination_provider)
        if not provider or not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError(f"Provider {destination_provider} is not available")
        source_provider_obj = self.mass.get_provider(source_provider)
        if not source_provider_obj or not isinstance(source_provider_obj, MusicProvider):
            raise ProviderUnavailableError(f"Provider {source_provider} is not available")
        trust_source_mappings = source_provider_obj.domain != "builtin"
        update_current_task_progress(0, "Loading source playlist")
        source_tracks: list[Track] = []
        unsupported_items: list[tuple[str, str]] = []
        async for item in self.tracks(
            source_playlist_item_id,
            source_provider,
            force_refresh=True,
        ):
            if isinstance(item, Track):
                source_tracks.append(item)
                continue
            reason = (
                f"{item.media_type.value.replace('_', ' ').capitalize()} entries are not supported"
            )
            report_current_task_failure(f"{item.name}: {reason.lower()}")
            unsupported_items.append((item.name, reason))
        if not source_tracks:
            raise InvalidDataError("The source playlist has no tracks to migrate")

        minimum_confidence = self._minimum_match_confidence(match_policy)
        unique_tracks = {self._migration_track_key(track): track for track in source_tracks}
        allowed_provider_instance_set = set(allowed_provider_instances)
        failed_provider_instances: set[str] = set()
        completed = 0

        async def resolve_track(
            key: str, track: Track
        ) -> tuple[str, _PlaylistMigrationTrackResult]:
            nonlocal completed
            result = await self._resolve_migration_track(
                track,
                provider,
                minimum_confidence,
                allowed_provider_instance_set,
                trust_source_mappings,
                failed_provider_instances,
            )
            completed += 1
            _update_stage_progress(
                completed,
                len(unique_tracks),
                5,
                80,
                f"Matching track {completed}/{len(unique_tracks)}",
            )
            return key, result

        resolved_items: list[tuple[str, _PlaylistMigrationTrackResult]] = []
        for track_batch in batched(
            unique_tracks.items(),
            _MIGRATION_RESOLVE_BATCH_SIZE,
            strict=False,
        ):
            resolved_items.extend(
                await asyncio.gather(*(resolve_track(key, track) for key, track in track_batch))
            )
        resolved_tracks = dict(resolved_items)
        target_ids: list[str] = []
        target_results: list[tuple[Track, _PlaylistMigrationTrackResult]] = []
        builtin_entries: list[PlaylistItem] = []
        counts = {
            "total": len(source_tracks) + len(unsupported_items),
            "exact": 0,
            "same_recording": 0,
            "best_effort": 0,
            "skipped": len(unsupported_items),
            "ambiguous": 0,
            "library_matches": 0,
            "provider_matches": 0,
        }
        substitutions_by_track: dict[str, tuple[str, str, str]] = {}
        skipped_items = unsupported_items.copy()
        provider_issues: list[tuple[str, str]] = []
        for key, result in resolved_tracks.items():
            track = unique_tracks[key]
            track_label = self._migration_track_label(track)
            if result.used_library_item:
                counts["library_matches"] += 1
            counts["provider_matches"] += len(result.provider_matches)
            if result.error:
                report_current_task_failure(f"{track_label}: {result.error}")
                if provider.domain == "builtin":
                    provider_issues.append((track_label, result.error))
            for provider_name in result.failed_providers:
                issue = f"Matching failed on {provider_name}"
                report_current_task_failure(f"{track_label}: {issue.lower()}")
                provider_issues.append((track_label, issue))
            for provider_name in result.ambiguous_providers:
                issue = f"Ambiguous match on {provider_name}"
                report_current_task_failure(f"{track_label}: {issue.lower()}")
                provider_issues.append((track_label, issue))
            if provider.domain == "builtin" and result.track:
                continue
            if result.mapping:
                if result.track and result.confidence != TrackMatchConfidence.EXACT:
                    substitutions_by_track[key] = (
                        track_label,
                        self._migration_track_label(result.track),
                        "Same recording"
                        if result.confidence == TrackMatchConfidence.LIKELY
                        else "Best effort",
                    )
                continue
            if result.error:
                skipped_items.append((track_label, result.error))
                continue
            reason = (
                "multiple equally likely matches" if result.ambiguous else "no acceptable match"
            )
            report_current_task_failure(f"{track_label}: {reason}")
            skipped_items.append((track_label, reason.capitalize()))

        seen_target_ids: set[str] = set()
        for track in source_tracks:
            result = resolved_tracks[self._migration_track_key(track)]
            if provider.domain == "builtin" and result.track:
                builtin_entries.append(media_item_to_playlist_item(result.track))
                continue
            if not result.mapping:
                counts["skipped"] += 1
                if result.ambiguous:
                    counts["ambiguous"] += 1
                continue
            if (
                not provider.playlist_duplicates_supported
                and result.mapping.item_id in seen_target_ids
            ):
                reason = f"{provider.name} does not support duplicate playlist entries"
                counts["skipped"] += 1
                report_current_task_failure(
                    f"{self._migration_track_label(track)}: {reason.lower()}"
                )
                skipped_items.append((self._migration_track_label(track), reason))
                continue
            seen_target_ids.add(result.mapping.item_id)
            target_ids.append(result.mapping.item_id)
            target_results.append((track, result))
            self._adjust_migration_match_count(
                counts,
                result.confidence,
                1,
            )

        prepared_count = (
            len(builtin_entries) if provider.domain == "builtin" else len(target_results)
        )
        set_current_task_report(
            self._build_migration_report(
                source_playlist.name,
                destination_name,
                provider.name,
                prepared_count,
                counts,
                list(substitutions_by_track.values()),
                skipped_items,
                provider_issues,
                completed=False,
                builtin_destination=provider.domain == "builtin",
            )
        )
        if not prepared_count:
            raise InvalidDataError("No tracks could be migrated")
        update_current_task_progress(85, "Creating destination playlist")
        if provider.domain == "builtin":
            destination_playlist = await self._create_builtin_migration_playlist(
                destination_name,
                builtin_entries,
                source_playlist.image.path if source_playlist.image else None,
            )
            migrated_count = prepared_count
        else:
            destination_playlist = await self.create_playlist(
                destination_name,
                media_types=[MediaType.TRACK],
                provider_instance_or_domain=provider.instance_id,
            )
            playlist_provider, playlist_item_id = self._select_provider_id(destination_playlist)
            if playlist_provider != provider.instance_id:
                raise ProviderUnavailableError(
                    f"Created playlist is not available on provider {provider.name}"
                )
            update_current_task_progress(
                90,
                f"Adding {len(target_ids)} tracks to destination playlist",
            )
            await self._add_provider_playlist_tracks(
                provider,
                playlist_item_id,
                target_ids,
            )
            update_current_task_progress(95, "Verifying destination playlist")
            try:
                confirmed_indexes, destination_mismatch = await self._verify_migration_results(
                    playlist_item_id,
                    provider.instance_id,
                    target_results,
                )
            except (
                ResourceTemporarilyUnavailable,
                ProviderUnavailableError,
                MediaNotFoundError,
                ClientError,
                OSError,
                TimeoutError,
            ) as err:
                migrated_count = len(target_results)
                issue = (
                    "Could not verify destination playlist: "
                    f"{self._migration_error_message(err, 'Verification failed')}"
                )
                self.logger.warning(
                    "Could not verify migrated playlist %s on provider %s: %s",
                    destination_playlist.name,
                    provider.name,
                    err,
                )
                report_current_task_failure(issue)
                provider_issues.append(("Destination playlist", issue))
            else:
                missing_results = [
                    target_result
                    for index, target_result in enumerate(target_results)
                    if index not in confirmed_indexes
                ]
                migrated_count = len(confirmed_indexes)
                for track, result in missing_results:
                    reason = f"{provider.name} did not add this track in the expected order"
                    counts["skipped"] += 1
                    self._adjust_migration_match_count(
                        counts,
                        result.confidence,
                        -1,
                    )
                    report_current_task_failure(
                        f"{self._migration_track_label(track)}: {reason.lower()}"
                    )
                    skipped_items.append((self._migration_track_label(track), reason))
                if destination_mismatch:
                    issue = "Destination playlist contains unexpected or reordered tracks"
                    report_current_task_failure(issue)
                    provider_issues.append(("Destination playlist", issue))
                confirmed_track_keys = {
                    self._migration_track_key(target_results[index][0])
                    for index in confirmed_indexes
                }
                substitutions_by_track = {
                    key: substitution
                    for key, substitution in substitutions_by_track.items()
                    if key in confirmed_track_keys
                }
            destination_playlist.metadata.last_refresh = None
            await self.update_item_in_library(
                destination_playlist.item_id,
                destination_playlist,
            )

        if current_task := get_current_task():
            current_task.metadata.update(
                {
                    "playlist_id": destination_playlist.item_id,
                    "playlist_name": destination_playlist.name,
                    "migrated_count": migrated_count,
                    **counts,
                }
            )
        set_current_task_report(
            self._build_migration_report(
                source_playlist.name,
                destination_playlist.name,
                provider.name,
                migrated_count,
                counts,
                list(substitutions_by_track.values()),
                skipped_items,
                provider_issues,
                completed=True,
                builtin_destination=provider.domain == "builtin",
            )
        )
        if not migrated_count:
            raise InvalidDataError("The destination provider did not add any tracks")
        update_current_task_progress(
            100,
            f"Migrated {migrated_count} of {counts['total']} playlist items",
        )

    async def _resolve_migration_track(
        self,
        track: Track,
        provider: MusicProvider,
        minimum_confidence: TrackMatchConfidence,
        allowed_provider_instances: set[str],
        trust_source_mappings: bool,
        failed_provider_instances: set[str],
    ) -> _PlaylistMigrationTrackResult:
        """Resolve one source track for a migration destination."""
        if provider.domain != "builtin" and provider.instance_id in failed_provider_instances:
            return _PlaylistMigrationTrackResult(
                error=f"Matching unavailable on {provider.name} after an earlier failure"
            )
        try:
            if provider.domain == "builtin":
                enrichment = await self.mass.music.tracks.enrich_provider_mappings(
                    track,
                    minimum_confidence=minimum_confidence,
                    provider_instance_ids=allowed_provider_instances,
                    trust_track_mappings=trust_source_mappings,
                    failed_provider_instances=failed_provider_instances,
                )
                return _PlaylistMigrationTrackResult(
                    track=enrichment.track if enrichment.track.provider_mappings else None,
                    provider_matches=enrichment.matches,
                    ambiguous_providers=enrichment.ambiguous_providers,
                    failed_providers=enrichment.failed_providers,
                    used_library_item=enrichment.used_library_item,
                )
            library_track = await self.mass.music.tracks.get_library_match(track)
            result = await self.mass.music.tracks.find_provider_match(
                track,
                provider,
                minimum_confidence=minimum_confidence,
                mapping_source=library_track,
                allowed_provider_instances=allowed_provider_instances,
                trust_base_mapping=trust_source_mappings,
            )
            if not result.match:
                return _PlaylistMigrationTrackResult(
                    used_library_item=library_track is not None,
                    ambiguous=result.ambiguous,
                )
            return _PlaylistMigrationTrackResult(
                track=result.match.track,
                mapping=result.match.mapping,
                confidence=result.match.confidence,
                used_library_item=library_track is not None,
            )
        except (
            ResourceTemporarilyUnavailable,
            ProviderUnavailableError,
            ClientError,
            OSError,
            TimeoutError,
        ) as err:
            failed_provider_instances.add(provider.instance_id)
            return _PlaylistMigrationTrackResult(
                error=self._migration_error_message(
                    err,
                    f"Matching failed on {provider.name}",
                )
            )
        except (InvalidDataError, MediaNotFoundError) as err:
            return _PlaylistMigrationTrackResult(
                error=self._migration_error_message(
                    err,
                    f"Matching failed on {provider.name}",
                ),
            )

    async def _create_builtin_migration_playlist(
        self,
        name: str,
        entries: list[PlaylistItem],
        image_url: str | None,
    ) -> Playlist:
        """Create a Music Assistant playlist from resolved entries."""
        provider = self.mass.get_provider("builtin")
        if not provider or not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError("Builtin provider is not available")
        builtin_provider = cast("BuiltinProvider", provider)
        playlist = await builtin_provider.import_playlist(generate_m3u(name, entries, image_url))
        for mapping in playlist.provider_mappings:
            mapping.in_library = True
        return await self.add_item_to_library(playlist, False)

    async def _verify_migration_results(
        self,
        playlist_item_id: str,
        provider_instance_id: str,
        target_results: Sequence[tuple[Track, _PlaylistMigrationTrackResult]],
    ) -> tuple[set[int], bool]:
        """Verify which requested tracks appear in the destination playlist."""
        confirmed_indexes: set[int] = set()
        destination_mismatch = False
        for retry_delay in (0, *_MIGRATION_VERIFY_RETRY_DELAYS):
            if retry_delay:
                await asyncio.sleep(retry_delay)
            actual_target_ids = [
                item.item_id
                async for item in self.tracks(
                    playlist_item_id,
                    provider_instance_id,
                    force_refresh=True,
                )
            ]
            confirmed_indexes, destination_mismatch = self._reconcile_migration_results(
                actual_target_ids,
                target_results,
            )
            if len(confirmed_indexes) == len(target_results) and not destination_mismatch:
                break
        return confirmed_indexes, destination_mismatch

    @staticmethod
    def _reconcile_migration_results(
        actual_target_ids: list[str],
        target_results: Sequence[tuple[Track, _PlaylistMigrationTrackResult]],
    ) -> tuple[set[int], bool]:
        """Return confirmed result indexes and whether destination content is unexpected."""
        expected_positions: dict[str, list[int]] = {}
        for index, (_track, result) in enumerate(target_results):
            assert result.mapping is not None
            expected_positions.setdefault(result.mapping.item_id, []).append(index)

        confirmed_indexes: set[int] = set()
        last_confirmed_index = -1
        destination_mismatch = False
        for actual_target_id in actual_target_ids:
            positions = expected_positions.get(actual_target_id)
            if not positions:
                destination_mismatch = True
                continue
            position_index = bisect_right(positions, last_confirmed_index)
            if position_index == len(positions):
                destination_mismatch = True
                continue
            last_confirmed_index = positions[position_index]
            confirmed_indexes.add(last_confirmed_index)
        return confirmed_indexes, destination_mismatch

    @staticmethod
    def _adjust_migration_match_count(
        counts: dict[str, int],
        confidence: TrackMatchConfidence,
        amount: int,
    ) -> None:
        """Adjust the aggregate count for a migration match confidence."""
        count_key = {
            TrackMatchConfidence.EXACT: "exact",
            TrackMatchConfidence.LIKELY: "same_recording",
            TrackMatchConfidence.LOOSE: "best_effort",
        }[confidence]
        counts[count_key] += amount

    @staticmethod
    def _minimum_match_confidence(
        match_policy: PlaylistMigrationMatchPolicy,
    ) -> TrackMatchConfidence:
        """Return the minimum confidence accepted by a migration policy."""
        return {
            PlaylistMigrationMatchPolicy.EXACT: TrackMatchConfidence.EXACT,
            PlaylistMigrationMatchPolicy.SAME_RECORDING: TrackMatchConfidence.LIKELY,
            PlaylistMigrationMatchPolicy.BEST_EFFORT: TrackMatchConfidence.LOOSE,
        }[match_policy]

    @staticmethod
    def _migration_track_key(track: Track) -> str:
        """Return a stable key for reusing duplicate track resolutions."""
        return track.uri or f"{track.provider}://track/{track.item_id}"

    @classmethod
    def _build_migration_report(
        cls,
        source_name: str,
        destination_name: str,
        destination_provider: str,
        migrated_count: int,
        counts: Mapping[str, int],
        substitutions: list[tuple[str, str, str]],
        skipped_items: list[tuple[str, str]],
        provider_issues: list[tuple[str, str]],
        *,
        completed: bool,
        builtin_destination: bool,
    ) -> str:
        """Build the human-readable Markdown report for a migration task."""
        source = cls._escape_markdown(source_name)
        destination = cls._escape_markdown(destination_name)
        provider = cls._escape_markdown(destination_provider)
        action = "Migrated" if completed else "Prepared"
        lines = [
            f"## Playlist migration {'complete' if completed else 'analysis'}",
            "",
            f"{action} **{migrated_count}** of **{counts['total']}** playlist items "
            f"from **{source}** for **{destination}** on **{provider}**.",
            "",
            "| Result | Items |",
            "| --- | ---: |",
        ]
        if builtin_destination:
            lines.extend(
                (
                    f"| Included | {migrated_count} |",
                    f"| Existing library matches used | {counts['library_matches']} |",
                    f"| Additional provider mappings found | {counts['provider_matches']} |",
                    f"| Skipped | {counts['skipped']} |",
                )
            )
        else:
            lines.extend(
                (
                    f"| Exact release | {counts['exact']} |",
                    f"| Same recording | {counts['same_recording']} |",
                    f"| Best effort | {counts['best_effort']} |",
                    f"| Skipped | {counts['skipped']} |",
                    f"| Ambiguous | {counts['ambiguous']} |",
                )
            )
        cls._add_report_table(
            lines,
            "Substitutions",
            ("Source", "Destination", "Match"),
            substitutions,
        )
        cls._add_report_table(
            lines,
            "Skipped items",
            ("Item", "Reason"),
            skipped_items,
        )
        cls._add_report_table(
            lines,
            "Provider lookup issues",
            ("Track", "Issue"),
            provider_issues,
        )
        return "\n".join(lines)

    @classmethod
    def _add_report_table(
        cls,
        lines: list[str],
        title: str,
        headers: tuple[str, ...],
        rows: Sequence[tuple[str, ...]],
    ) -> None:
        """Append a Markdown report table when it has rows."""
        if not rows:
            return
        visible_rows = rows[:_MIGRATION_REPORT_DETAIL_LIMIT]
        lines.extend(
            (
                "",
                f"### {title}",
                "",
                f"| {' | '.join(headers)} |",
                f"| {' | '.join('---' for _ in headers)} |",
            )
        )
        lines.extend(
            f"| {' | '.join(cls._escape_markdown(value, table=True) for value in row)} |"
            for row in visible_rows
        )
        if omitted_count := len(rows) - len(visible_rows):
            lines.extend(
                (
                    "",
                    f"_{omitted_count} additional rows omitted._",
                )
            )

    @staticmethod
    def _migration_track_label(track: Track) -> str:
        """Return a readable artist and title label."""
        return f"{track.artist_str} - {track.name}" if track.artist_str else track.name

    @staticmethod
    def _migration_error_message(error: BaseException, fallback: str) -> str:
        """Return a non-empty error message for a migration report."""
        return str(error).strip() or f"{fallback} ({type(error).__name__})"

    @staticmethod
    def _escape_markdown(value: str, table: bool = False) -> str:
        """Escape provider text before adding it to a Markdown report."""
        value = value.replace("\\", "\\\\").replace("\n", " ")
        for character in ("`", "*", "_", "[", "]", "<", ">"):
            value = value.replace(character, f"\\{character}")
        return value.replace("|", "\\|") if table else value

    def _verify_update_allowed(self, current_item: Playlist, update: Playlist) -> None:
        """
        Verify that the update is allowed from a security perspective.

        Prevents updating item_id for non-streaming providers to prevent path traversal attacks.
        """
        # Build lookup dict of current mappings: provider_instance -> item_id
        current_mappings = {
            mapping.provider_instance: mapping.item_id for mapping in current_item.provider_mappings
        }

        # Check if any existing mapping's item_id has been modified for non-streaming providers
        for update_mapping in update.provider_mappings:
            # Only check if this is an existing mapping being modified
            if update_mapping.provider_instance in current_mappings:
                current_item_id = current_mappings[update_mapping.provider_instance]

                # Disallow item_id changes for filesystem-based providers (filesystem, builtin)
                if (
                    current_item_id != update_mapping.item_id
                    and update_mapping.provider_instance.startswith(("filesystem", "builtin"))
                ):
                    msg = (
                        f"Updating item_id is not allowed for filesystem-based providers: "
                        f"attempted to change '{current_item_id}' to '{update_mapping.item_id}'"
                    )
                    raise InvalidDataError(msg)

    async def _add_library_item(self, item: Playlist, overwrite_existing: bool = False) -> int:
        """Add a new record to the database."""
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                # persist the localizable name key + its params so the localized name survives
                # the library round-trip (e.g. builtin playlists, Spotify's per-account
                # "Liked Songs {0}"). params are re-stamped from the provider on each sync.
                "translation_key": item.translation_key,
                "translation_params": serialize_to_json(item.translation_params)
                if item.translation_params
                else None,
                "owner": item.owner,
                "is_editable": item.is_editable,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
                "supported_mediatypes": serialize_to_json(item.supported_mediatypes),
                "is_dynamic": item.is_dynamic,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(db_id, item.external_ids)
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Playlist, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        self._verify_update_allowed(cur_item, update)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        # adopt the synced item's translation_key + params (as a unit) when it supplies a key, so
        # the localized name follows the provider, existing rows backfill, and a stale param (e.g.
        # a renamed Spotify account) self-heals; otherwise keep what we have (unless overwriting).
        if overwrite or update.translation_key is not None:
            translation_key = update.translation_key
            translation_params = update.translation_params
        else:
            translation_key = cur_item.translation_key
            translation_params = cur_item.translation_params
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                # always prefer name/owner from updated item here
                "name": name,
                "sort_name": sort_name,
                "translation_key": translation_key,
                "translation_params": serialize_to_json(translation_params)
                if translation_params
                else None,
                "owner": update.owner or cur_item.owner,
                "is_editable": update.is_editable,
                "metadata": serialize_to_json(metadata),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "supported_mediatypes": serialize_to_json(update.supported_mediatypes),
                "is_dynamic": update.is_dynamic,
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(
            db_id, update.external_ids if overwrite else cur_item.external_ids
        )
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    @guard_single_request
    async def _get_provider_playlist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        page: int = 0,
        force_refresh: bool = False,
    ) -> Sequence[PlaylistPlayableItem]:
        """Return playlist tracks for the given provider playlist id."""
        assert provider_instance_id_or_domain != "library"
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        provider = cast("MusicProvider", provider)
        async with self.mass.cache.handle_refresh(force_refresh):
            return await provider.get_playlist_tracks(item_id, page=page)

    async def _handle_add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:
        """Handle adding playlist items inside a managed task."""
        # ruff: noqa: PLR0915
        total_requested = len(uris)
        update_current_task_progress(0, "Preparing playlist update")
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
        # Validate uris to prevent code injection
        for index, uri in enumerate(uris, start=1):
            _update_stage_progress(
                index,
                total_requested,
                0,
                10,
                f"Validating request {index}/{total_requested}",
            )
            # Prevent code injection via newlines in URIs
            if "\n" in uri or "\r" in uri:
                msg = "Invalid URI: newlines not allowed"
                raise InvalidProviderURI(msg)
            await parse_uri(uri)
        # grab all existing track ids in the playlist so we can check for duplicates
        # use _select_provider_id to respect user's provider filter
        playlist_prov_instance, playlist_prov_item_id = self._select_provider_id(playlist)
        playlist_prov = self.mass.get_provider(playlist_prov_instance)
        if not playlist_prov or not playlist_prov.available:
            raise ProviderUnavailableError(f"Provider {playlist_prov_instance} is not available")
        playlist_prov = cast("MusicProvider", playlist_prov)

        if ProviderFeature.PLAYLIST_TRACKS_EDIT not in playlist_prov.supported_features:
            msg = f"Provider {playlist_prov.name} does not support editing playlists"
            raise InvalidDataError(msg)

        # sets to track existing tracks
        cur_playlist_track_ids: set[str] = set()
        cur_playlist_track_uris: set[str] = set()

        # collect current track IDs and URIs
        update_current_task_progress_text("Loading current playlist items")
        async for item in self.tracks(playlist.item_id, playlist.provider):
            if item.item_id:
                cur_playlist_track_ids.add(item.item_id)
            if item.uri:
                cur_playlist_track_uris.add(item.uri)

        # unwrap URIs to individual track URIs
        unwrapped_uris: list[str] = []
        for index, uri in enumerate(uris, start=1):
            _update_stage_progress(
                index,
                total_requested,
                10,
                35,
                f"Expanding request {index}/{total_requested}",
            )
            # URI could be a playlist or album uri, unwrap it
            if not ("://" in uri and len(uri.split("/")) >= 4):
                # NOT a music assistant-style uri (provider://media_type/item_id)
                self.logger.warning(
                    "Not adding %s to playlist %s - not a valid uri", uri, playlist.name
                )
                continue
            # music assistant-style uri
            # provider://media_type/item_id
            provider_instance_id_or_domain, rest = uri.split("://", 1)
            media_type_str, item_id = rest.split("/", 1)
            media_type = MediaType(media_type_str)
            if media_type == MediaType.ALBUM:
                album_tracks = await self.mass.music.albums.tracks(
                    item_id, provider_instance_id_or_domain
                )
                for track in album_tracks:
                    if track.uri is not None:
                        unwrapped_uris.append(track.uri)
            elif media_type == MediaType.PLAYLIST:
                async for item in self.tracks(item_id, provider_instance_id_or_domain):
                    if item.uri is not None:
                        unwrapped_uris.append(item.uri)
            elif media_type in PLAYLIST_MEDIA_TYPES:
                unwrapped_uris.append(uri)
            else:
                self.logger.warning(
                    "Not adding %s to playlist %s - media type not supported in playlists",
                    uri,
                    playlist.name,
                )
                continue

        # work out the track id's that need to be added
        # filter out duplicates and items that not exist on the provider.
        ids_to_add: list[str] = []
        total_candidates = len(unwrapped_uris)
        for index, uri in enumerate(unwrapped_uris, start=1):
            _update_stage_progress(
                index,
                total_candidates,
                35,
                85,
                f"Matching item {index}/{total_candidates}",
            )
            # skip if item already in the playlist
            if uri in cur_playlist_track_uris:
                self.logger.info(
                    "Not adding %s to playlist %s - it already exists",
                    uri,
                    playlist.name,
                )
                continue

            # special: the builtin provider can handle uri's from all providers (with uri as id)
            if playlist_prov.domain == "builtin":
                ids_to_add.append(uri)
                continue

            # parse uri for further processing
            media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)

            if media_type not in playlist.supported_mediatypes:
                self.logger.warning(
                    "Not adding %s to playlist %s, "
                    "the target playlist doesn't support this media type.",
                    uri,
                    playlist.name,
                )
                continue

            # skip if item already in the playlist
            if item_id in cur_playlist_track_ids:
                self.logger.warning(
                    "Not adding %s to playlist %s - it already exists",
                    uri,
                    playlist.name,
                )
                continue

            # if target playlist is an exact provider match, we can add it
            if provider_instance_id_or_domain in (playlist_prov.instance_id, playlist_prov.domain):
                ids_to_add.append(item_id)
                continue

            if media_type == MediaType.PODCAST_EPISODE:
                # in practice we should not be able to reach here but guard just in case
                self.logger.warning(
                    "Not adding %s to playlist %s - "
                    "podcast episodes must be added to a provider-specific playlist",
                    uri,
                    playlist.name,
                )
                continue

            # not exact match - try to get a match for the item on the playlist's provider
            full_item: PlaylistPlayableItem
            controller = cast(
                "AudiobooksController | RadioController | TracksController",
                self.mass.music.get_controller(media_type),
            )
            if media_type == MediaType.TRACK:
                assert isinstance(controller, TracksController)  # for type checking
                full_item = await controller.get(
                    item_id,
                    provider_instance_id_or_domain,
                    allow_update_metadata=False,
                    recursive=provider_instance_id_or_domain != "library",
                )
            else:
                full_item = await controller.get(
                    item_id,
                    provider_instance_id_or_domain,
                    allow_update_metadata=False,
                )
            track_prov_domains = {x.provider_domain for x in full_item.provider_mappings}
            if (
                playlist_prov.is_streaming_provider
                and playlist_prov.domain not in track_prov_domains
            ):
                # try to match the track to the playlist's provider
                full_item.provider_mappings.update(
                    await controller.match_provider(
                        full_item,  # type: ignore[arg-type]
                        playlist_prov,
                        strict=False,
                    )
                )

            # a track can contain multiple versions on the same provider
            # simply sort by quality and just add the first available version
            for item_mapping in sorted(
                full_item.provider_mappings, key=lambda x: x.quality, reverse=True
            ):
                if not item_mapping.available:
                    continue
                if item_mapping.item_id in cur_playlist_track_ids:
                    break  # already existing in the playlist
                item_prov = self.mass.get_provider(item_mapping.provider_instance)
                if not item_prov:
                    continue
                track_version_uri = create_uri(
                    media_type,
                    item_prov.instance_id,
                    item_mapping.item_id,
                )
                if track_version_uri in cur_playlist_track_uris:
                    self.logger.warning(
                        "Not adding %s to playlist %s - it already exists",
                        full_item.name,
                        playlist.name,
                    )
                    break  # already existing in the playlist
                # Add item to provider-specific playlist
                if item_prov.instance_id == playlist_prov.instance_id:
                    if item_mapping.item_id not in ids_to_add:
                        ids_to_add.append(item_mapping.item_id)
                    self.logger.info(
                        "Adding %s to playlist %s",
                        full_item.name,
                        playlist.name,
                    )
                    break
            else:
                self.logger.warning(
                    "Can't add %s to playlist %s - it is not available on provider %s",
                    full_item.name,
                    playlist.name,
                    playlist_prov.name,
                )

        if not ids_to_add:
            update_current_task_progress(100, "No new playlist items to add")
            return

        # actually add the tracks to the playlist on the provider
        update_current_task_progress(90, f"Adding {len(ids_to_add)} item(s) to playlist")
        await self._add_provider_playlist_tracks(
            playlist_prov,
            playlist_prov_item_id,
            ids_to_add,
        )
        # reset 'last_refresh' to force a refresh of the playlist's metadata
        # in the next scheduled run of the playlist metadata task
        playlist.metadata.last_refresh = None
        await self.update_item_in_library(db_playlist_id, playlist)
        update_current_task_progress(100, f"Added {len(ids_to_add)} item(s) to playlist")

    async def _handle_remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Handle removing playlist items inside a managed task."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
        # use _select_provider_id to respect user's provider filter
        playlist_prov_instance, playlist_prov_item_id = self._select_provider_id(playlist)
        provider = self.mass.get_provider(playlist_prov_instance)
        if not provider or not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError(f"Provider {playlist_prov_instance} is not available")
        if ProviderFeature.PLAYLIST_TRACKS_EDIT not in provider.supported_features:
            msg = f"Provider {provider.name} does not support editing playlists"
            raise InvalidDataError(msg)
        await provider.remove_playlist_tracks(playlist_prov_item_id, positions_to_remove)
        # reset 'last_refresh' to force a refresh of the playlist's metadata
        # in the next scheduled run of the playlist metadata task
        playlist.metadata.last_refresh = None
        await self.update_item_in_library(db_playlist_id, playlist)

    @staticmethod
    async def _add_provider_playlist_tracks(
        provider: MusicProvider,
        playlist_item_id: str,
        track_ids: list[str],
    ) -> None:
        """Add tracks in ordered batches accepted by provider APIs."""
        for track_id_batch in batched(
            track_ids,
            _PROVIDER_PLAYLIST_ADD_BATCH_SIZE,
            strict=False,
        ):
            await provider.add_playlist_tracks(
                playlist_item_id,
                list(track_id_batch),
            )

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> PlaylistSummary:
        """Parse a raw summary db row into a PlaylistSummary object."""
        item = cast("PlaylistSummary", super()._parse_summary_row(db_row))
        item.owner = db_row["owner"]
        item.is_editable = bool(db_row["is_editable"])
        item.is_dynamic = bool(db_row["is_dynamic"])
        item.metadata.description = db_row["description"]
        item.supported_mediatypes = {
            MediaType(x) for x in json_loads(db_row["supported_mediatypes"])
        }
        if translation_key := db_row["translation_key"]:
            item.translation_key = translation_key
        if translation_params := db_row["translation_params"]:
            item.translation_params = json_loads(translation_params)
        return item
