"""Exercise Jellyfin playlist editing through its authenticated HTTP contract."""

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import aiohttp
import pytest
from aiohttp import web
from aiojellyfin import Connection
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.jellyfin import SUPPORTED_FEATURES, JellyfinProvider

if TYPE_CHECKING:
    from pytest_aiohttp import AiohttpServer

    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def playlist_server(
    aiohttp_server: AiohttpServer, mass: MusicAssistant
) -> AsyncGenerator[tuple[JellyfinProvider, dict[str, Any]]]:
    """Serve a duplicate-containing playlist and the connected user's permissions."""
    track = json.loads((Path(__file__).parent / "fixtures/tracks/do_i_wanna_know.json").read_text())
    track["Id"] = "same-track"
    state: dict[str, Any] = {
        "items": [{**track, "PlaylistItemId": f"entry-{i}"} for i in range(105)],
        "can_edit": True,
        "status": 200,
        "writes": [],
        "reads": [],
    }

    async def permissions(request: web.Request) -> web.Response:
        assert request.match_info["user_id"] == "own-user"
        if state["status"] != 200:
            return web.Response(status=state["status"])
        return web.json_response({"UserId": "own-user", "CanEdit": state["can_edit"]})

    async def items(request: web.Request) -> web.Response:
        assert 'Token="test-token"' in request.headers["Authorization"]
        if request.method == "GET":
            assert request.query.get("userId", "own-user") == "own-user"
            start = int(request.query.get("startIndex", 0))
            limit = int(request.query.get("limit", 100))
            state["reads"].append(start)
            return web.json_response(
                {
                    "Items": state["items"][start : start + limit],
                    "TotalRecordCount": len(state["items"]),
                    "StartIndex": start,
                }
            )
        if not state["can_edit"]:
            return web.Response(status=403)
        if state["status"] != 200:
            return web.Response(status=state["status"])
        state["writes"].append((request.method, dict(request.query)))
        if request.method == "POST":
            assert request.query["userId"] == "own-user"
            state["items"].extend(
                {**track, "Id": item, "PlaylistItemId": f"added-{index}"}
                for index, item in enumerate(request.query["ids"].split(","))
            )
        else:
            removed = request.query["entryIds"].split(",")
            state["items"] = [x for x in state["items"] if x["PlaylistItemId"] not in removed]
        return web.Response(status=204)

    async def metadata(_request: web.Request) -> web.Response:
        return web.json_response(
            {"Id": "playlist", "Name": "Test", "Type": "Playlist", "ImageTags": {}}
        )

    app = web.Application()
    app.router.add_get("/jellyfin/Users/own-user/Items/playlist", metadata)
    app.router.add_get("/jellyfin/Playlists/playlist/Users/{user_id}", permissions)
    app.router.add_route("*", "/jellyfin/Playlists/playlist/Items", items)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        config = SessionConfiguration(
            session=session,
            url=str(server.make_url("/jellyfin")),
            app_name="test",
            app_version="1",
            device_name="test",
            device_id="test",
        )
        provider = object.__new__(JellyfinProvider)
        provider.mass = mass
        provider.logger = logging.getLogger("test.jellyfin")
        provider.config = mock.Mock(instance_id="jellyfin--playlist-test")
        provider.manifest = mock.Mock(domain="jellyfin", name="Jellyfin")
        provider._client = Connection(config, "own-user", "test-token")
        yield provider, state


async def test_append_tracks(playlist_server: tuple[JellyfinProvider, dict[str, Any]]) -> None:
    """Append in order without replacing existing entries or changing accounts."""
    provider, state = playlist_server
    assert ProviderFeature.PLAYLIST_TRACKS_EDIT in SUPPORTED_FEATURES
    await provider.add_playlist_tracks("playlist", ["track-a", "track-b"])
    assert [x["Id"] for x in state["items"]][-2:] == ["track-a", "track-b"]
    assert len(state["items"]) == 107


async def test_remove_occurrences(playlist_server: tuple[JellyfinProvider, dict[str, Any]]) -> None:
    """Translate positions beyond page one to entry IDs, leaving duplicate songs intact."""
    provider, state = playlist_server
    await provider.remove_playlist_tracks("playlist", (2, 101, 2))
    assert len(state["items"]) == 103
    assert state["items"][0]["PlaylistItemId"] == "entry-0"
    assert state["items"][1]["PlaylistItemId"] == "entry-2"
    assert state["reads"] == [0, 100]
    assert state["writes"] == [("DELETE", {"entryIds": "entry-1,entry-100"})]


@pytest.mark.parametrize("positions", [(0,), (-1,), (106,), (1, 106)])
async def test_invalid_positions(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], positions: tuple[int, ...]
) -> None:
    """Refuse unavailable positions before making any partial removal."""
    provider, state = playlist_server
    with pytest.raises(InvalidDataError):
        await provider.remove_playlist_tracks("playlist", positions)
    assert not state["writes"]
    assert len(state["items"]) == 105


@pytest.mark.parametrize("editable", [True, False])
async def test_edit_permission(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], editable: bool
) -> None:
    """Advertise only the connected user's server-confirmed edit rights."""
    provider, state = playlist_server
    state["can_edit"] = editable
    playlist = await provider.get_playlist("playlist")
    assert playlist.is_editable is editable


@pytest.mark.parametrize("status", [403, 404])
async def test_missing_permission_is_readonly(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], status: int
) -> None:
    """Keep readable playlists read-only if edit permissions cannot be established."""
    provider, state = playlist_server
    state["status"] = status
    playlist = await provider.get_playlist("playlist")
    assert not playlist.is_editable


async def test_permission_revoked(playlist_server: tuple[JellyfinProvider, dict[str, Any]]) -> None:
    """Let the server reject a write when permission changed after listing."""
    provider, state = playlist_server
    state["can_edit"] = False
    with pytest.raises(aiohttp.ClientResponseError, match="403"):
        await provider.add_playlist_tracks("playlist", ["track-a"])
    assert not state["writes"]


async def test_empty_edits(playlist_server: tuple[JellyfinProvider, dict[str, Any]]) -> None:
    """Empty edits neither fetch entries nor send mutation requests."""
    provider, state = playlist_server
    await provider.add_playlist_tracks("playlist", [])
    await provider.remove_playlist_tracks("playlist", ())
    assert not state["writes"]
    assert not state["reads"]


async def test_missing_entry_id(playlist_server: tuple[JellyfinProvider, dict[str, Any]]) -> None:
    """Do not substitute recording IDs when the server omits an occurrence ID."""
    provider, state = playlist_server
    del state["items"][1]["PlaylistItemId"]
    with pytest.raises(InvalidDataError):
        await provider.remove_playlist_tracks("playlist", (1, 2))
    assert not state["writes"]


@pytest.mark.parametrize("status", [401, 500])
async def test_permission_errors_propagate(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], status: int
) -> None:
    """Do not conceal failed authentication or an unavailable Jellyfin server."""
    provider, state = playlist_server
    state["status"] = status
    with pytest.raises(aiohttp.ClientResponseError):
        await provider.get_playlist("playlist")


async def test_edit_invalidates_all_playlist_pages(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
) -> None:
    """Read fresh tracks after edits without evicting other playlists or sync state."""
    provider, state = playlist_server
    cache = provider.mass.cache
    await cache.set("sync-tracks", ["keep"], provider=provider.instance_id, category=1)
    await cache.set("other-playlist", ["keep"], provider=provider.instance_id)
    assert len(await provider.get_playlist_tracks("playlist", 0)) == 100
    assert len(await provider.get_playlist_tracks("playlist", 1)) == 5
    state["reads"].clear()
    await provider.remove_playlist_tracks("playlist", (1,))
    assert len(await provider.get_playlist_tracks("playlist", 0)) == 100
    assert len(await provider.get_playlist_tracks("playlist", 1)) == 4
    assert state["reads"] == [0, 0, 100]
    assert await cache.get("sync-tracks", provider=provider.instance_id, category=1) == ["keep"]
    assert await cache.get("other-playlist", provider=provider.instance_id) == ["keep"]
