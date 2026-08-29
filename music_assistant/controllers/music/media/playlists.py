"""Manage MediaItems of type Playlist."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderURI,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import Playlist, PlaylistSummary

from music_assistant.constants import DB_TABLE_PLAYLISTS, PLAYLIST_MEDIA_TYPES, PlaylistPlayableItem
from music_assistant.controllers.tasks.context import (
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
from .tracks import TracksController

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


class PlaylistMatchPolicy(StrEnum):
    """
    Allowed fallback depth when matching a playlist track on another provider.

    Shared between playlist import and (future) playlist migration: both only fall back to a
    provider search once a track's own reference (its original URI, or its library mapping)
    is no longer available.

    EXACT requires release-track evidence (e.g. a MusicBrainz track/release ID) pinning a
    specific release, not just the underlying recording. M3U playlists exported by a current
    Music Assistant persist this evidence when the source track carries it, so imports of
    those files can reach EXACT; legacy or third-party M3U files typically only carry an ISRC
    or recording ID and cap out at SAME_RECORDING or BEST_EFFORT.
    """

    EXACT = "exact"
    SAME_RECORDING = "same_recording"
    BEST_EFFORT = "best_effort"


# minimum TrackMatchConfidence accepted for each policy tier
_MATCH_POLICY_MINIMUM_CONFIDENCE: Final[dict[PlaylistMatchPolicy, TrackMatchConfidence]] = {
    PlaylistMatchPolicy.EXACT: TrackMatchConfidence.EXACT,
    PlaylistMatchPolicy.SAME_RECORDING: TrackMatchConfidence.LIKELY,
    PlaylistMatchPolicy.BEST_EFFORT: TrackMatchConfidence.LOOSE,
}


def match_policy_minimum_confidence(match_policy: PlaylistMatchPolicy) -> TrackMatchConfidence:
    """Return the minimum track-match confidence accepted by a match policy."""
    return _MATCH_POLICY_MINIMUM_CONFIDENCE[match_policy]


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
        match_policy: PlaylistMatchPolicy | None = None,
    ) -> Playlist:
        """
        Import a playlist from M3U8 format.

        Creates a new builtin playlist from the provided M3U data. Entries whose original
        source is confirmed still playable - available and, once probed, still
        resolvable, or merely unreachable right now - are kept as-is; a background task
        then searches other providers for a substitute for entries whose original source
        is confirmed missing, when requested.

        :param m3u_data: The M3U8 playlist data as a string.
        :param library_matching: Deprecated, use match_policy instead. When True and
            match_policy is not set, matching runs at PlaylistMatchPolicy.BEST_EFFORT.
        :param match_providers: Optional list of provider instance IDs or domains to search
            when matching runs. Defaults to all providers available to the current user.
        :param match_policy: Lowest track-match confidence accepted for a substitute when
            an entry's original source is confirmed missing. Leave unset together with
            library_matching=False to skip matching and leave those entries unresolved.
        """
        provider = self.mass.get_provider("builtin")
        if not provider or not isinstance(provider, MusicProvider):
            raise ProviderUnavailableError("Builtin provider is not available")
        builtin_prov = cast("BuiltinProvider", provider)
        playlist = await builtin_prov.import_playlist(m3u_data)
        for prov_mapping in playlist.provider_mappings:
            prov_mapping.in_library = True
        db_playlist = await self.add_item_to_library(playlist, False)
        effective_match_policy = match_policy or (
            PlaylistMatchPolicy.BEST_EFFORT if library_matching else None
        )
        if effective_match_policy is not None:
            prov_playlist_id = playlist.item_id
            user = get_current_user()
            # snapshot the current user's allowed provider instances now: the matching
            # itself runs later in an unattended background task, without the request's
            # user context, so provider-instance isolation must be captured up front.
            # Source ownership is checked against every provider instance the user has
            # configured and enabled, not just the ones currently loaded, so a provider
            # that failed setup or is temporarily down is not mistaken for one the user
            # removed - only actually searching for a substitute needs a loaded provider.
            # Each instance is snapshotted together with its domain so a domain-only
            # reference can also be expanded from this configured set, independent of
            # whether that instance happens to be loaded right now.
            user_provider_filter = user.provider_filter if user else None
            configured_providers = await self.mass.config.get_provider_configs(
                provider_type=ProviderType.MUSIC
            )
            allowed_provider_instances = {
                conf.instance_id: conf.domain
                for conf in configured_providers
                if conf.enabled
                and (not user_provider_filter or conf.instance_id in user_provider_filter)
            }
            # match_providers only narrows which providers are searched for a substitute;
            # it must not narrow source validation, or a playable original on a provider
            # outside that list would look unavailable and get replaced unnecessarily.
            # An explicit empty list (all providers deselected) must narrow the search
            # to nothing, so it is checked against None rather than emptiness.
            search_provider_instances = {item.instance_id for item in self.mass.music.providers}
            if match_providers is not None:
                search_provider_instances = {
                    item.instance_id
                    for item in self.mass.music.providers
                    if item.instance_id in match_providers or item.domain in match_providers
                }
            self.mass.tasks.run_background_task(
                name=f"Import playlist {db_playlist.name}",
                handler=lambda: builtin_prov.match_imported_playlist_tracks(
                    prov_playlist_id,
                    effective_match_policy,
                    tuple(sorted(allowed_provider_instances.items())),
                    tuple(sorted(search_provider_instances)),
                ),
                translation_key="import_playlist_matching",
                translation_owner=self.translation_owner,
                translation_args=[db_playlist.name],
                user_id=user.user_id if user else None,
                metadata={
                    "task_domain": "playlist_import_matching",
                    "playlist_id": str(db_playlist.item_id),
                    "playlist_name": db_playlist.name,
                    "match_policy": effective_match_policy.value,
                },
                allow_retry=True,
                allow_cancel=True,
                priority=True,
            )
        return db_playlist

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
        await playlist_prov.add_playlist_tracks(playlist_prov_item_id, ids_to_add)
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
