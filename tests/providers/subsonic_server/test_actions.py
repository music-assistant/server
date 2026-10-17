"""Tests for the Subsonic favorites, lyrics, play queue and scan endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import MultiDict, MultiDictProxy
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, Track, UniqueList

from music_assistant.providers.subsonic_server.actions import ActionEndpoints
from music_assistant.providers.subsonic_server.browse import BrowseEndpoints
from music_assistant.providers.subsonic_server.ids import to_subsonic_id
from music_assistant.providers.subsonic_server.request import SubsonicRequest
from music_assistant.providers.subsonic_server.response import SubsonicError

NAVIDROME = "opensubsonic--Bnc7DaHk"
MEMBER = User(user_id="member-id", username="member", role=UserRole.USER)
GUEST = User(user_id="guest-id", username="guest", role=UserRole.GUEST)
LRC = "[ar:Someone]\n[00:01.50]First line\n[01:02.25]Second line\n"


def _track(item_id: str = "1") -> Track:
    return Track(
        item_id=item_id,
        provider="library",
        name="Song",
        provider_mappings=set(),
        duration=120,
        artists=UniqueList(),
        album=ItemMapping(media_type=MediaType.ALBUM, item_id="5", provider="library", name="A"),
    )


def _actions() -> tuple[ActionEndpoints, MagicMock]:
    mass = MagicMock()
    mass.music.set_item_favorite = AsyncMock()
    mass.music.tracks.get_library_item = AsyncMock(side_effect=lambda item_id: _track(item_id))
    mass.music.get_controller.return_value = mass.music.tracks
    return ActionEndpoints(BrowseEndpoints(mass)), mass


async def _call(actions: ActionEndpoints, name: str, user: User = MEMBER, **params: Any) -> Any:
    multi: MultiDict[str] = MultiDict()
    for key, value in params.items():
        for item in value if isinstance(value, list) else [value]:
            multi.add(key, str(item))
    return await actions.endpoints()[name](SubsonicRequest(MultiDictProxy(multi), user))


async def test_star_and_unstar_use_item_uris() -> None:
    """Star likes every given item, unstar removes the like."""
    actions, mass = _actions()
    provider_track = to_subsonic_id(MediaType.TRACK, "abc", NAVIDROME)
    await _call(actions, "star", id=["tr-1", provider_track], albumId="al-5", artistId="ar-7")
    await _call(actions, "unstar", id="tr-1")
    calls = [call.args for call in mass.music.set_item_favorite.await_args_list]
    assert calls == [
        ("library://track/1", True),
        (f"{NAVIDROME}://track/abc", True),
        ("library://album/5", True),
        ("library://artist/7", True),
        ("library://track/1", None),
    ]


async def test_guest_may_not_star() -> None:
    """Changing favorites requires the library.write scope."""
    actions, _ = _actions()
    with pytest.raises(SubsonicError) as err:
        await _call(actions, "star", user=GUEST, id="tr-1")
    assert err.value.code == 50


async def test_scrobble_is_accepted() -> None:
    """Scrobbles are accepted; the jukebox queue already records the plays."""
    actions, mass = _actions()
    assert await _call(actions, "scrobble", id="tr-1", submission="true") == {}
    mass.music.mark_item_played.assert_not_called()


async def test_synced_lyrics_by_song_id() -> None:
    """LRC lyrics become synced structured lyrics with millisecond starts."""
    actions, mass = _actions()
    mass.metadata.get_track_lyrics = AsyncMock(return_value=(None, LRC))
    body = await _call(actions, "getLyricsBySongId", id="tr-1")
    lyrics = body["lyricsList"]["structuredLyrics"][0]
    assert lyrics["synced"] is True
    assert lyrics["line"] == [
        {"start": 1500, "value": "First line"},
        {"start": 62250, "value": "Second line"},
    ]


async def test_plain_lyrics_by_song_id() -> None:
    """Plain lyrics become unsynced structured lyrics; none gives an empty list."""
    actions, mass = _actions()
    mass.metadata.get_track_lyrics = AsyncMock(return_value=("One\nTwo", None))
    body = await _call(actions, "getLyricsBySongId", id="tr-1")
    lyrics = body["lyricsList"]["structuredLyrics"][0]
    assert lyrics["synced"] is False
    assert [line["value"] for line in lyrics["line"]] == ["One", "Two"]
    mass.metadata.get_track_lyrics = AsyncMock(return_value=(None, None))
    body = await _call(actions, "getLyricsBySongId", id="tr-1")
    assert body == {"lyricsList": {"structuredLyrics": []}}


async def test_play_queue_is_saved_per_user() -> None:
    """A saved play queue is returned to the same user only."""
    actions, _ = _actions()
    assert (await _call(actions, "getPlayQueue"))["playQueue"] == {"entry": []}
    await _call(
        actions, "savePlayQueue", id=["tr-1", "tr-2"], current="tr-2", position=5000, c="Feishin"
    )
    play_queue = (await _call(actions, "getPlayQueue"))["playQueue"]
    assert [entry["id"] for entry in play_queue["entry"]] == ["tr-1", "tr-2"]
    assert play_queue["current"] == "tr-2"
    assert play_queue["position"] == 5000
    assert play_queue["changedBy"] == "Feishin"
    assert (await _call(actions, "getPlayQueue", user=GUEST))["playQueue"] == {"entry": []}


async def test_scan_status_and_start_scan_rights() -> None:
    """Scan status reports syncs and the track count; starting one needs library.manage."""
    actions, mass = _actions()
    mass.music.active_sync_tasks = [object()]
    mass.music.tracks.library_count = AsyncMock(return_value=7784)
    mass.music.start_sync = AsyncMock()
    body = await _call(actions, "getScanStatus")
    assert body == {"scanStatus": {"scanning": True, "count": 7784}}
    with pytest.raises(SubsonicError):
        await _call(actions, "startScan")
    admin = User(user_id="admin-id", username="admin", role=UserRole.ADMIN)
    await _call(actions, "startScan", user=admin)
    mass.music.start_sync.assert_awaited_once()
