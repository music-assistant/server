"""Manage MediaItems of type Playlist."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderURI,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import Playlist, Track

from music_assistant.constants import DB_TABLE_PLAYLISTS
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json
from music_assistant.helpers.uri import create_uri, parse_uri
from music_assistant.helpers.util import guard_single_request
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = DB_TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/create_playlist", self.create_playlist)
        self.mass.register_api_command("music/playlists/playlist_tracks", self.tracks)
        self.mass.register_api_command(
            "music/playlists/add_playlist_tracks", self.add_playlist_tracks
        )
        self.mass.register_api_command(
            "music/playlists/remove_playlist_tracks", self.remove_playlist_tracks
        )

    def _verify_update_allowed(self, current_item: Playlist, update: Playlist) -> None:
        """Verify that the update is allowed from a security perspective.

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

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
    ) -> AsyncGenerator[Track, None]:
        """Return playlist tracks for the given provider playlist id."""
        if provider_instance_id_or_domain == "library":
            library_item = await self.get_library_item(item_id)
            provider_instance_id_or_domain, item_id = self._select_provider_id(library_item)
        # playlist tracks are not stored in the db,
        # we always fetched them (cached) from the provider
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
        self, name: str, provider_instance_or_domain: str | None = None
    ) -> Playlist:
        """Create new playlist."""
        # if provider is omitted, just pick builtin provider
        if provider_instance_or_domain:
            provider = self.mass.get_provider(provider_instance_or_domain)
            if provider is None:
                raise ProviderUnavailableError
        else:
            provider = self.mass.get_provider("builtin")
        # grab all existing track ids in the playlist so we can check for duplicates
        provider = cast("MusicProvider", provider)

        if "/" in name or "\\" in name or ".." in name:
            msg = f"{name} is not a valid Playlist name"
            raise InvalidDataError(msg)
        # create playlist on the provider
        playlist = await provider.create_playlist(name)
        for prov_mapping in playlist.provider_mappings:
            # when manually creating a playlist, it's always in the library
            prov_mapping.in_library = True
        # add the new playlist to the library
        return await self.add_item_to_library(playlist, False)

    async def add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:
        """Add tracks to playlist."""
        # ruff: noqa: PLR0915
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
        # Validate uris to prevent code injection
        for uri in uris:
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

        # sets to track existing tracks
        cur_playlist_track_ids: set[str] = set()
        cur_playlist_track_uris: set[str] = set()

        # collect current track IDs and URIs
        async for item in self.tracks(playlist.item_id, playlist.provider):
            if item.item_id:
                cur_playlist_track_ids.add(item.item_id)
            if item.uri:
                cur_playlist_track_uris.add(item.uri)

        # unwrap URIs to individual track URIs
        unwrapped_uris: list[str] = []
        for uri in uris:
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
                async for track in self.tracks(item_id, provider_instance_id_or_domain):
                    if track.uri is not None:
                        unwrapped_uris.append(track.uri)
            elif media_type == MediaType.TRACK:
                unwrapped_uris.append(uri)
            else:
                self.logger.warning(
                    "Not adding %s to playlist %s - not a track", uri, playlist.name
                )
                continue

        # work out the track id's that need to be added
        # filter out duplicates and items that not exist on the provider.
        ids_to_add: list[str] = []
        for uri in unwrapped_uris:
            # skip if item already in the playlist
            if uri in cur_playlist_track_uris:
                self.logger.info(
                    "Not adding %s to playlist %s - it already exists",
                    uri,
                    playlist.name,
                )
                continue

            # parse uri for further processing
            media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)

            # skip if item already in the playlist
            if item_id in cur_playlist_track_ids:
                self.logger.warning(
                    "Not adding %s to playlist %s - it already exists",
                    uri,
                    playlist.name,
                )
                continue

            # special: the builtin provider can handle uri's from all providers (with uri as id)
            if provider_instance_id_or_domain != "library" and playlist_prov.domain == "builtin":
                # note: we try not to add library uri's to the builtin playlists
                # so we can survive db rebuilds
                if uri not in ids_to_add:
                    ids_to_add.append(uri)
                self.logger.info(
                    "Adding %s to playlist %s",
                    uri,
                    playlist.name,
                )
                continue

            # if target playlist is an exact provider match, we can add it
            if provider_instance_id_or_domain != "library":
                item_prov = self.mass.get_provider(provider_instance_id_or_domain)
                if not item_prov or not item_prov.available:
                    self.logger.warning(
                        "Skip adding %s to playlist: Provider %s is not available",
                        uri,
                        provider_instance_id_or_domain,
                    )
                    continue
                if item_prov.instance_id == playlist_prov.instance_id:
                    if item_id not in ids_to_add:
                        ids_to_add.append(item_id)
                    continue

            # ensure we have a full (library) track (including all provider mappings)
            full_track = await self.mass.music.tracks.get(
                item_id,
                provider_instance_id_or_domain,
                recursive=provider_instance_id_or_domain != "library",
            )
            track_prov_domains = {x.provider_domain for x in full_track.provider_mappings}
            if (
                playlist_prov.domain != "builtin"
                and playlist_prov.is_streaming_provider
                and playlist_prov.domain not in track_prov_domains
            ):
                # try to match the track to the playlist provider
                full_track.provider_mappings.update(
                    await self.mass.music.tracks.match_provider(
                        full_track, playlist_prov, strict=False
                    )
                )

            # a track can contain multiple versions on the same provider
            # simply sort by quality and just add the first available version
            for track_version in sorted(
                full_track.provider_mappings, key=lambda x: x.quality, reverse=True
            ):
                if not track_version.available:
                    continue
                if track_version.item_id in cur_playlist_track_ids:
                    break  # already existing in the playlist
                item_prov = self.mass.get_provider(track_version.provider_instance)
                if not item_prov:
                    continue
                track_version_uri = create_uri(
                    MediaType.TRACK,
                    item_prov.instance_id,
                    track_version.item_id,
                )
                if track_version_uri in cur_playlist_track_uris:
                    self.logger.warning(
                        "Not adding %s to playlist %s - it already exists",
                        full_track.name,
                        playlist.name,
                    )
                    break  # already existing in the playlist
                if playlist_prov.domain == "builtin":
                    # the builtin provider can handle uri's from all providers (with uri as id)
                    if track_version_uri not in ids_to_add:
                        ids_to_add.append(track_version_uri)
                    self.logger.info(
                        "Adding %s to playlist %s",
                        full_track.name,
                        playlist.name,
                    )
                    break
                if item_prov.instance_id == playlist_prov.instance_id:
                    if track_version.item_id not in ids_to_add:
                        ids_to_add.append(track_version.item_id)
                    self.logger.info(
                        "Adding %s to playlist %s",
                        full_track.name,
                        playlist.name,
                    )
                    break
            else:
                self.logger.warning(
                    "Can't add %s to playlist %s - it is not available on provider %s",
                    full_track.name,
                    playlist.name,
                    playlist_prov.name,
                )

        if not ids_to_add:
            return

        # actually add the tracks to the playlist on the provider
        await playlist_prov.add_playlist_tracks(playlist_prov_item_id, ids_to_add)
        # invalidate cache so tracks get refreshed
        self._refresh_playlist_tracks(playlist)
        await self.update_item_in_library(db_playlist_id, playlist)

    async def add_playlist_track(self, db_playlist_id: str | int, track_uri: str) -> None:
        """Add (single) track to playlist."""
        await self.add_playlist_tracks(db_playlist_id, [track_uri])

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove multiple tracks from playlist."""
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

        await self.update_item_in_library(db_playlist_id, playlist)

    async def _add_library_item(self, item: Playlist, overwrite_existing: bool = False) -> int:
        """Add a new record to the database."""
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "owner": item.owner,
                "is_editable": item.is_editable,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
            },
        )
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
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                # always prefer name/owner from updated item here
                "name": name,
                "sort_name": sort_name,
                "owner": update.owner or cur_item.owner,
                "is_editable": update.is_editable,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
            },
        )
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    @guard_single_request  # type: ignore[type-var]  # TODO: fix typing in util.py
    async def _get_provider_playlist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        page: int = 0,
        force_refresh: bool = False,
    ) -> list[Track]:
        """Return playlist tracks for the given provider playlist id."""
        assert provider_instance_id_or_domain != "library"
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        provider = cast("MusicProvider", provider)
        async with self.mass.cache.handle_refresh(force_refresh):
            return await provider.get_playlist_tracks(item_id, page=page)

    async def radio_mode_base_tracks(
        self,
        item: Playlist,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks from the controller used to calculate the dynamic radio.

        :param item: The Playlist to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        return [
            x
            async for x in self.tracks(item.item_id, item.provider)
            # filter out unavailable tracks
            if x.available
        ]

    async def match_providers(self, db_item: Playlist) -> None:
        """Try to find match on all (streaming) providers for the provided (database) item.

        This is used to link objects of different providers/qualities together.
        """
        # playlists can only be matched on the same provider (if not unique)
        if self.mass.music.match_provider_instances(db_item):
            await self.add_provider_mappings(db_item.item_id, db_item.provider_mappings)

    def _refresh_playlist_tracks(self, playlist: Playlist) -> None:
        """Refresh playlist tracks by forcing a cache refresh."""

        async def _refresh(playlist: Playlist) -> None:
            # simply iterate all tracks with force_refresh=True to refresh the cache
            async for _ in self.tracks(playlist.item_id, playlist.provider, force_refresh=True):
                pass

        task_id = f"refresh_playlist_tracks_{playlist.item_id}"
        self.mass.call_later(5, _refresh, playlist, task_id=task_id)  # debounce multiple calls
