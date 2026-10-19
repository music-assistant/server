"""Tests for the Subsonic playlist endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import MultiDict, MultiDictProxy
from music_assistant_models.access import PlaylistAccess
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, ProviderSharing
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.media_items import ItemMapping, Playlist, Track

from music_assistant.providers.subsonic_server.browse import BrowseEndpoints
from music_assistant.providers.subsonic_server.playlists import PlaylistEndpoints
from music_assistant.providers.subsonic_server.request import SubsonicRequest
from music_assistant.providers.subsonic_server.response import SubsonicError

MEMBER = User(user_id="member-id", username="member", role=UserRole.USER)
GUEST = User(user_id="guest-id", username="guest", role=UserRole.GUEST)


def _track(item_id: str, position: int) -> Track:
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings=set(),
        duration=60,
        position=position,
        album=ItemMapping(media_type=MediaType.ALBUM, item_id="5", provider="library", name="A"),
    )


def _playlist(item_id: str = "3", sharing: ProviderSharing = ProviderSharing.PRIVATE) -> Playlist:
    return Playlist(
        item_id=item_id,
        provider="library",
        name="Road trip",
        provider_mappings=set(),
        is_editable=True,
        access=PlaylistAccess(owner=MEMBER.user_id, sharing=sharing),
    )


def _endpoints(tracks: list[Track] | None = None) -> tuple[PlaylistEndpoints, MagicMock]:
    mass = MagicMock()
    playlists = mass.music.playlists
    mass.music.get_controller.return_value = playlists
    playlists.get = AsyncMock(return_value=_playlist())
    playlists.tracks = AsyncMock(return_value=tracks or [])
    playlists.create_playlist = AsyncMock(return_value=_playlist("9"))
    playlists.update_playlist = AsyncMock()
    playlists.set_access = AsyncMock()
    playlists.add_playlist_track = AsyncMock()
    playlists.remove_playlist_track = AsyncMock()
    mass.music.remove_item_from_library = AsyncMock()
    mass.webserver.auth.get_user = AsyncMock(return_value=MEMBER)
    return PlaylistEndpoints(BrowseEndpoints(mass)), mass


async def _call(
    endpoints: PlaylistEndpoints, endpoint: str, /, user: User = MEMBER, **params: Any
) -> Any:
    multi: MultiDict[str] = MultiDict()
    for key, value in params.items():
        for item in value if isinstance(value, list) else [value]:
            multi.add(key, str(item))
    return await endpoints.endpoints()[endpoint](SubsonicRequest(MultiDictProxy(multi), user))


async def test_get_playlists() -> None:
    """Library playlists are listed with their track count, duration and owner."""
    endpoints, mass = _endpoints(tracks=[_track("1", 1), _track("2", 2)])

    mass.music.playlists.library_items = AsyncMock(return_value=[_playlist()])
    body = await _call(endpoints, "getPlaylists")
    entry = body["playlists"]["playlist"][0]
    assert entry["id"] == "pl-3"
    assert entry["songCount"] == 2
    assert entry["duration"] == 120
    assert entry["owner"] == "member"
    assert entry["public"] is False
    assert "entry" not in entry


async def test_get_playlist_includes_entries() -> None:
    """GetPlaylist returns the tracks in playlist order."""
    endpoints, _ = _endpoints(tracks=[_track("1", 1), _track("2", 2)])
    body = await _call(endpoints, "getPlaylist", id="pl-3")
    assert [song["id"] for song in body["playlist"]["entry"]] == ["tr-1", "tr-2"]


async def test_create_playlist_with_songs() -> None:
    """A new playlist is created on Music Assistant and filled in order."""
    endpoints, mass = _endpoints()
    body = await _call(endpoints, "createPlaylist", name="Road trip", songId=["tr-1", "tr-2"])
    mass.music.playlists.create_playlist.assert_awaited_once_with("Road trip")
    added = [call.args for call in mass.music.playlists.add_playlist_track.await_args_list]
    assert added == [("9", "library://track/1"), ("9", "library://track/2")]
    assert body["playlist"]["name"] == "Road trip"


async def test_create_playlist_replaces_songs_of_existing_playlist() -> None:
    """CreatePlaylist with a playlistId replaces every track, which clients use to reorder."""
    endpoints, mass = _endpoints(tracks=[_track("1", 1), _track("2", 2)])
    await _call(endpoints, "createPlaylist", playlistId="pl-3", songId=["tr-2", "tr-1"])
    removed = [call.args for call in mass.music.playlists.remove_playlist_track.await_args_list]
    assert removed == [("3", 2), ("3", 1)]
    added = [call.args[1] for call in mass.music.playlists.add_playlist_track.await_args_list]
    assert added == ["library://track/2", "library://track/1"]


async def test_update_playlist_removes_by_index_and_adds() -> None:
    """Subsonic indexes map onto the stored positions; removals happen before additions."""
    endpoints, mass = _endpoints(tracks=[_track("1", 1), _track("2", 4), _track("3", 7)])
    await _call(
        endpoints,
        "updatePlaylist",
        playlistId="pl-3",
        songIndexToRemove=["2", "0"],
        songIdToAdd="tr-9",
    )
    removed = [call.args for call in mass.music.playlists.remove_playlist_track.await_args_list]
    assert removed == [("3", 7), ("3", 1)]
    mass.music.playlists.add_playlist_track.assert_awaited_once_with("3", "library://track/9")


async def test_update_playlist_name_and_public() -> None:
    """Renaming updates the playlist; public maps onto sharing with everyone."""
    endpoints, mass = _endpoints()
    await _call(endpoints, "updatePlaylist", playlistId="pl-3", name="New name", public="true")
    renamed = mass.music.playlists.update_playlist.await_args
    assert renamed.args[1].name == "New name"
    assert renamed.kwargs == {"overwrite": True}
    mass.music.playlists.set_access.assert_awaited_once_with(
        "3", ProviderSharing.EVERYONE, owner=MEMBER.user_id
    )


async def test_refused_removal_propagates() -> None:
    """A removal Music Assistant refuses is not reported as done."""
    endpoints, mass = _endpoints(tracks=[_track("1", 1)])
    mass.music.playlists.remove_playlist_track = AsyncMock(
        side_effect=InsufficientPermissions("no")
    )
    with pytest.raises(InsufficientPermissions):
        await _call(endpoints, "updatePlaylist", playlistId="pl-3", songIndexToRemove="0")


async def test_permission_errors() -> None:
    """Guests may not edit, a refused edit propagates, provider playlists give error 50."""
    endpoints, mass = _endpoints()
    with pytest.raises(SubsonicError) as err:
        await _call(endpoints, "deletePlaylist", user=GUEST, id="pl-3")
    assert err.value.code == 50
    mass.music.remove_item_from_library = AsyncMock(side_effect=InsufficientPermissions("no"))
    with pytest.raises(InsufficientPermissions):
        await _call(endpoints, "deletePlaylist", id="pl-3")
    with pytest.raises(SubsonicError) as err:
        await _call(endpoints, "deletePlaylist", id="pl-pb3BlbnN1YnNvbmljCng")
    assert err.value.code == 50


async def test_delete_playlist() -> None:
    """DeletePlaylist removes the playlist from the library."""
    endpoints, mass = _endpoints()
    await _call(endpoints, "deletePlaylist", id="pl-3")
    mass.music.remove_item_from_library.assert_awaited_once_with(MediaType.PLAYLIST, "3")
