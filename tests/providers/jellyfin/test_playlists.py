"""Exercise Jellyfin playlist editing through its authenticated HTTP contract."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import aiohttp
import pytest
from aiohttp import web
from aiojellyfin import Connection
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ItemMapping, Playlist

from music_assistant.providers.jellyfin import SUPPORTED_FEATURES, JellyfinProvider

if TYPE_CHECKING:
    from pytest_aiohttp import AiohttpServer

    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def playlist_server(
    aiohttp_server: AiohttpServer, mass: MusicAssistant
) -> AsyncGenerator[tuple[JellyfinProvider, dict[str, Any]]]:
    """Serve a duplicate-containing playlist and the connected user's permissions."""
    app, state = _playlist_app()
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
        provider._playlist_permission_semaphore = asyncio.Semaphore(5)
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
    assert (await provider.get_playlist("playlist")).is_editable
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


@pytest.mark.parametrize("read_path", ["detail", "search", "library"])
async def test_permission_authentication_errors_propagate(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], read_path: str
) -> None:
    """Keep failed authentication visible on every public playlist read path."""
    provider, state = playlist_server
    state["permission_status_by_id"] = {"playlist-0": 401}
    with pytest.raises((aiohttp.ClientResponseError, ExceptionGroup)) as raised:
        await _read_playlists(provider, read_path)
    error = raised.value
    if isinstance(error, ExceptionGroup):
        assert error.subgroup(
            lambda item: isinstance(item, aiohttp.ClientResponseError) and item.status == 401
        )
    else:
        assert isinstance(error, aiohttp.ClientResponseError)
        assert error.status == 401


@pytest.mark.parametrize("read_path", ["detail", "search", "library"])
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_permission_http_failures_keep_playlists_readable(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
    read_path: str,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One unavailable permission check must not discard the readable playlist listing."""
    provider, state = playlist_server
    state["permission_status_by_id"] = {"playlist-0": status}
    items = await _read_playlists(provider, read_path)
    _assert_readonly_fallback(items, read_path)
    assert "keeping it read-only" in caplog.text
    assert "test-token" not in caplog.text


@pytest.mark.parametrize("read_path", ["detail", "search", "library"])
@pytest.mark.parametrize("failure", [TimeoutError, aiohttp.ClientConnectionError])
async def test_permission_transport_failures_keep_playlists_readable(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
    read_path: str,
    failure: type[Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Timeouts and connection failures fail closed for editing, not for browsing."""
    provider, _state = playlist_server
    original = provider._client._session.get_json

    async def get_json(url: str, params: Any) -> dict[str, Any]:
        if url == "/Playlists/playlist-0/Users/own-user":
            raise failure("sensitive diagnostic that should not be logged")
        return await original(url, params)

    with mock.patch.object(provider._client._session, "get_json", side_effect=get_json):
        items = await _read_playlists(provider, read_path)
    _assert_readonly_fallback(items, read_path)
    assert "keeping it read-only" in caplog.text
    assert "sensitive diagnostic" not in caplog.text


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


async def test_large_append_is_batched(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
) -> None:
    """Append hundreds of real-sized IDs in order without oversized request lines."""
    provider, state = playlist_server
    ids = [f"{index:032x}" for index in range(305)]
    await provider.add_playlist_tracks("playlist", ids)
    assert [x["Id"] for x in state["items"]][-305:] == ids
    assert len(state["writes"]) > 1
    assert max(state["request_sizes"]) < 8192


async def test_large_removal_resolves_before_batches(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
) -> None:
    """Resolve every occurrence before any mutation can shift later positions."""
    provider, state = playlist_server
    original = [{**state["items"][0], "PlaylistItemId": f"{index:032x}"} for index in range(550)]
    state["items"] = original.copy()
    await provider.remove_playlist_tracks("playlist", tuple(range(1, 501)))
    assert state["items"] == original[500:]
    assert len(state["writes"]) > 1
    assert state["reads_at_write"] == [5] * len(state["writes"])
    assert max(state["request_sizes"]) < 8192


@pytest.mark.parametrize("status", [200, 403, 404, 500])
async def test_permission_cache_shared_by_read_paths(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], status: int
) -> None:
    """Reuse positive and negative permissions across detail, search and library reads."""
    provider, state = playlist_server
    state["status"] = status
    first = await provider.get_playlist("playlist-0")
    await _wait_for_permission_cache(provider, ["playlist-0"])
    search = await provider.search("first", [MediaType.PLAYLIST], 12)
    await _wait_for_permission_cache(provider, [p["Id"] for p in state["playlists"]])
    assert len(state["permission_reads"]) == 12
    library = [item async for item in provider.get_library_playlists()]
    assert len(state["permission_reads"]) == 12
    again = await provider.search("second", [MediaType.PLAYLIST], 12)
    assert [p.item_id for p in search.playlists] == [p["Id"] for p in state["playlists"]]
    assert [p.item_id for p in library] == [p.item_id for p in search.playlists]
    assert all(
        isinstance(p, Playlist) and p.is_editable == (status == 200)
        for p in [first, *library, *again.playlists]
    )
    assert len(state["permission_reads"]) == 12


@pytest.mark.parametrize("read_path", ["search", "library"])
async def test_listing_permissions_have_bounded_concurrency(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], read_path: str
) -> None:
    """Overlap permission round trips without exceeding five in-flight requests."""
    provider, state = playlist_server
    gate = asyncio.Event()
    state["permission_gate"] = gate

    async def read() -> list[str]:
        if read_path == "search":
            result = await provider.search("test", [MediaType.PLAYLIST], 12)
            return [item.item_id for item in result.playlists]
        return [item.item_id async for item in provider.get_library_playlists()]

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(state["permission_parallel"].wait(), timeout=2)
        assert 1 < state["permission_peak"] <= 5
    finally:
        gate.set()
        result = await task
    assert result == [p["Id"] for p in state["playlists"]]
    assert state["permission_peak"] <= 5


def _playlist_app() -> tuple[web.Application, dict[str, Any]]:
    """Build the HTTP fixture for playlist edits and permission listings."""
    track = json.loads((Path(__file__).parent / "fixtures/tracks/do_i_wanna_know.json").read_text())
    track["Id"] = "same-track"
    state: dict[str, Any] = {
        "items": [{**track, "PlaylistItemId": f"entry-{i}"} for i in range(105)],
        "can_edit": True,
        "status": 200,
        "writes": [],
        "reads": [],
        "permission_reads": [],
        "permission_active": 0,
        "permission_peak": 0,
        "permission_parallel": asyncio.Event(),
        "permission_gate": None,
        "request_sizes": [],
        "reads_at_write": [],
        "fail_write_number": None,
        "playlists": [
            {
                "Id": f"playlist-{index}",
                "Name": f"Test {index}",
                "Type": "Playlist",
                "ImageTags": {},
            }
            for index in range(12)
        ],
    }

    async def permissions(request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"]
        state["permission_reads"].append(request.match_info["playlist_id"])
        state["permission_active"] += 1
        state["permission_peak"] = max(state["permission_peak"], state["permission_active"])
        if state["permission_active"] > 1:
            state["permission_parallel"].set()
        try:
            if state["permission_gate"] is not None:
                await state["permission_gate"].wait()
            if (
                status := state.get("permission_status_by_id", {}).get(
                    request.match_info["playlist_id"], state["status"]
                )
            ) != 200:
                return web.Response(status=status)
            return web.json_response(
                {
                    "UserId": user_id,
                    "CanEdit": state["can_edit"] if user_id == "own-user" else False,
                }
            )
        finally:
            state["permission_active"] -= 1

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
        state["request_sizes"].append(len(request.raw_path.encode()))
        if len(request.raw_path.encode()) > 8192:
            return web.Response(status=414)
        if state["fail_write_number"] == len(state["writes"]) + 1:
            return web.Response(status=503)
        if not state["can_edit"]:
            return web.Response(status=403)
        if state["status"] != 200:
            return web.Response(status=state["status"])
        state["reads_at_write"].append(len(state["reads"]))
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

    async def metadata(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "Id": request.match_info["playlist_id"],
                "Name": "Test",
                "Type": "Playlist",
                "ImageTags": {},
            }
        )

    async def listing(request: web.Request) -> web.Response:
        if not request.query:
            return await folders(request)
        rows = state["playlists"]
        start = int(request.query.get("startIndex", 0))
        limit = int(request.query.get("limit", 100))
        return web.json_response(
            {
                "Items": rows[start : start + limit],
                "TotalRecordCount": len(rows),
                "StartIndex": start,
            }
        )

    async def folders(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "Items": [{"Id": "folder", "Name": "Playlists", "CollectionType": "playlists"}],
                "TotalRecordCount": 1,
                "StartIndex": 0,
            }
        )

    app = web.Application()
    app.router.add_get("/jellyfin/Users/{user_id}/Items/{playlist_id}", metadata)
    app.router.add_get("/jellyfin/Items", listing)
    app.router.add_get("/jellyfin/Playlists/{playlist_id}/Users/{user_id}", permissions)
    app.router.add_route("*", "/jellyfin/Playlists/playlist/Items", items)
    return app, state


async def _wait_for_permission_cache(provider: JellyfinProvider, ids: list[str]) -> None:
    """Wait for use_cache's background writes before testing a subsequent cache hit."""
    async with asyncio.timeout(3):
        for item_id in ids:
            while (
                await provider.mass.cache.get(
                    f"_can_edit_playlist.{item_id}", provider=provider.instance_id
                )
                is None
            ):
                await asyncio.sleep(0.01)


@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_failed_later_batch_invalidates_cache(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]], operation: str
) -> None:
    """Expose a partial edit after a later batch fails, without continuing to write."""
    provider, state = playlist_server
    if operation == "add":
        page, before_count, after_count = 1, 5, 100
    else:
        state["items"] = [
            {**state["items"][0], "PlaylistItemId": f"{index:032x}"} for index in range(305)
        ]
        page, before_count, after_count = 2, 100, 5
    assert len(await provider.get_playlist_tracks("playlist", page)) == before_count
    state["fail_write_number"] = 2
    edit = (
        provider.add_playlist_tracks("playlist", [f"{index:032x}" for index in range(205)])
        if operation == "add"
        else provider.remove_playlist_tracks("playlist", tuple(range(1, 206)))
    )
    with pytest.raises(aiohttp.ClientResponseError, match="503"):
        await edit
    assert len(state["writes"]) == 1
    assert len(state["request_sizes"]) == 2
    assert len(await provider.get_playlist_tracks("playlist", page)) == after_count


async def test_invalid_late_position_makes_no_batched_removal(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
) -> None:
    """Validate the entire selection before deleting even the first batch."""
    provider, state = playlist_server
    state["items"] = [
        {**state["items"][0], "PlaylistItemId": f"{index:032x}"} for index in range(305)
    ]
    with pytest.raises(InvalidDataError):
        await provider.remove_playlist_tracks("playlist", (*range(1, 301), 999))
    assert not state["writes"]
    assert len(state["items"]) == 305


async def test_permission_cache_isolated_between_accounts(
    playlist_server: tuple[JellyfinProvider, dict[str, Any]],
) -> None:
    """A cached owner permission must not make another account's playlist editable."""
    provider, state = playlist_server
    assert (await provider.get_playlist("playlist-0")).is_editable
    await _wait_for_permission_cache(provider, ["playlist-0"])
    other = copy(provider)
    other.config = mock.Mock(instance_id="jellyfin--other-account")
    other._client = Connection(provider._client._session_config, "other-user", "other-token")
    assert not (await other.get_playlist("playlist-0")).is_editable
    await _wait_for_permission_cache(other, ["playlist-0"])
    assert (await provider.get_playlist("playlist-0")).is_editable
    assert len(state["permission_reads"]) == 2


async def _read_playlists(
    provider: JellyfinProvider, read_path: str
) -> list[Playlist | ItemMapping]:
    """Exercise the three public read paths that enrich edit permissions."""
    if read_path == "detail":
        return [await provider.get_playlist("playlist-0")]
    if read_path == "search":
        return list((await provider.search("test", [MediaType.PLAYLIST], 12)).playlists)
    return [item async for item in provider.get_library_playlists()]


def _assert_readonly_fallback(items: list[Playlist | ItemMapping], read_path: str) -> None:
    """Keep the failed item read-only without changing other items' edit permissions."""
    assert [item.item_id for item in items] == (
        ["playlist-0"] if read_path == "detail" else [f"playlist-{index}" for index in range(12)]
    )
    for item in items:
        assert isinstance(item, Playlist)
        assert item.is_editable == (item.item_id != "playlist-0")
