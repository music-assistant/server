"""Subsonic playlist endpoints, backed by Music Assistant library playlists."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, ProviderSharing
from music_assistant_models.media_items import Playlist, Track

from music_assistant.controllers.webserver.helpers.auth_middleware import has_scope

from . import mappers
from .constants import ErrorCode
from .ids import ItemRef, parse_subsonic_id
from .library import collect_pages
from .response import SubsonicError

if TYPE_CHECKING:
    from .browse import BrowseEndpoints
    from .request import EndpointHandler, SubsonicRequest

# getPlaylists reads the tracks of every playlist; bound how many it reads at once
PLAYLIST_FETCH_CONCURRENCY = 4


class PlaylistEndpoints:
    """Listing, reading and editing playlists."""

    def __init__(self, browse: BrowseEndpoints) -> None:
        """
        Initialize the endpoints.

        :param browse: The browse endpoints, used to resolve Subsonic ids to media items.
        """
        self.browse = browse
        self.mass = browse.mass

    def endpoints(self) -> dict[str, EndpointHandler]:
        """Return the handlers of this group, keyed by endpoint name."""
        return {
            "getPlaylists": self._get_playlists,
            "getPlaylist": self._get_playlist,
            "createPlaylist": self._create_playlist,
            "updatePlaylist": self._update_playlist,
            "deletePlaylist": self._delete_playlist,
        }

    async def _get_playlists(self, request: SubsonicRequest) -> dict[str, Any]:
        playlists = await collect_pages(
            lambda limit, offset: self.mass.music.playlists.library_items(
                limit=limit, offset=offset
            )
        )
        owners: dict[str, str] = {}
        semaphore = asyncio.Semaphore(PLAYLIST_FETCH_CONCURRENCY)

        async def payload(playlist: Playlist) -> dict[str, Any]:
            async with semaphore:
                return await self._playlist_payload(playlist, request, owners)

        entries = await asyncio.gather(*(payload(playlist) for playlist in playlists))
        return {"playlists": {"playlist": list(entries)}}

    async def _get_playlist(self, request: SubsonicRequest) -> dict[str, Any]:
        ref = parse_subsonic_id(request.require("id"), MediaType.PLAYLIST)
        playlist = cast("Playlist", await self.browse.get_item(ref))
        return {"playlist": await self._playlist_payload(playlist, request, include_songs=True)}

    async def _create_playlist(self, request: SubsonicRequest) -> dict[str, Any]:
        _require_library_write(request)
        uris = _track_uris(request.get_all("songId"))
        if (playlist_id := request.get("playlistId")) is not None:
            ref = _library_playlist_ref(playlist_id)
            tracks = await self._tracks(ref)
            await self._remove_positions(ref, [track.position for track in tracks])
        else:
            created = await self.mass.music.playlists.create_playlist(request.require("name"))
            ref = ItemRef(MediaType.PLAYLIST, str(created.item_id))
        await self._add_tracks(ref, uris)
        playlist = cast("Playlist", await self.browse.get_item(ref))
        return {"playlist": await self._playlist_payload(playlist, request, include_songs=True)}

    async def _update_playlist(self, request: SubsonicRequest) -> dict[str, Any]:
        _require_library_write(request)
        ref = _library_playlist_ref(request.require("playlistId"))
        uris = _track_uris(request.get_all("songIdToAdd"))
        indexes = [_parse_index(value) for value in request.get_all("songIndexToRemove")]
        playlist = await self.mass.music.playlists.get(ref.item_id, "library", False)
        if (name := request.get("name")) is not None and name != playlist.name:
            playlist.name = name
            await self.mass.music.playlists.update_playlist(ref.item_id, playlist, overwrite=True)
        if (public := request.get("public")) is not None and playlist.access is not None:
            await self.mass.music.playlists.set_access(
                ref.item_id,
                ProviderSharing.EVERYONE if public == "true" else ProviderSharing.PRIVATE,
                owner=playlist.access.owner,
            )
        if indexes:
            tracks = await self._tracks(ref)
            positions = [tracks[index].position for index in indexes if index < len(tracks)]
            await self._remove_positions(ref, positions)
        await self._add_tracks(ref, uris)
        return {}

    async def _delete_playlist(self, request: SubsonicRequest) -> dict[str, Any]:
        _require_library_write(request)
        ref = _library_playlist_ref(request.require("id"))
        await self.mass.music.remove_item_from_library(MediaType.PLAYLIST, ref.item_id)
        return {}

    async def _playlist_payload(
        self,
        playlist: Playlist,
        request: SubsonicRequest,
        owners: dict[str, str] | None = None,
        include_songs: bool = False,
    ) -> dict[str, Any]:
        ref = ItemRef(MediaType.PLAYLIST, str(playlist.item_id), playlist.provider)
        tracks = await self._tracks(ref)
        songs = [mappers.song(track) for track in tracks]
        duration = sum(track.duration or 0 for track in tracks)
        owner = await self._owner_name(playlist, request, owners if owners is not None else {})
        return mappers.playlist(playlist, owner, songs, duration, include_songs)

    async def _tracks(self, ref: ItemRef) -> list[Track]:
        items = await self.mass.music.playlists.tracks(ref.item_id, ref.provider, limit=0)
        return [item for item in items if isinstance(item, Track)]

    async def _owner_name(
        self, playlist: Playlist, request: SubsonicRequest, owners: dict[str, str]
    ) -> str:
        owner_id = playlist.access.owner if playlist.access else None
        if owner_id is None:
            return playlist.owner or request.user.username
        if owner_id not in owners:
            user = await self.mass.webserver.auth.get_user(owner_id, include_disabled=True)
            owners[owner_id] = user.username if user else owner_id
        return owners[owner_id]

    async def _remove_positions(self, ref: ItemRef, positions: list[int | None]) -> None:
        # highest position first, so the positions still to remove keep pointing at their item
        for position in sorted({pos for pos in positions if pos is not None}, reverse=True):
            await self.mass.music.playlists.remove_playlist_track(ref.item_id, position)

    async def _add_tracks(self, ref: ItemRef, uris: list[str]) -> None:
        for uri in uris:
            await self.mass.music.playlists.add_playlist_track(ref.item_id, uri)


def _require_library_write(request: SubsonicRequest) -> None:
    if not has_scope(request.user, Scope.LIBRARY_WRITE):
        raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "User may not edit playlists")


def _library_playlist_ref(value: str) -> ItemRef:
    ref = parse_subsonic_id(value, MediaType.PLAYLIST)
    if not ref.is_library:
        raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "Only library playlists can be edited")
    return ref


def _track_uris(subsonic_ids: list[str]) -> list[str]:
    return [parse_subsonic_id(value, MediaType.TRACK).uri for value in subsonic_ids]


def _parse_index(value: str) -> int:
    if not value.isdigit():
        raise SubsonicError(ErrorCode.GENERIC, f"Invalid song index: {value}")
    return int(value)
