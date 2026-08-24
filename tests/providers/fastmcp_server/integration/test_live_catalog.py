"""Opt-in authenticated smoke coverage for the mounted live MA MCP catalog."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

LIBRARY_ITEM_COMMANDS = [
    "music/albums/library_items",
    "music/artists/library_items",
    "music/audiobooks/library_items",
    "music/genres/library_items",
    "music/playlists/library_items",
    "music/podcasts/library_items",
    "music/tracks/library_items",
]

type LiveClient = Client[Any]


async def _accept_elicitation(message: str, response_type: Any, params: Any, context: Any) -> Any:
    """Accept only in the explicit, reversible integration environment."""
    del message, params, context
    return response_type(value=True)


def _live_settings() -> tuple[str, str]:
    url = os.getenv("MA_MCP_URL")
    token = os.getenv("MA_MCP_TOKEN")
    if not url or not token:
        pytest.skip("set MA_MCP_URL and MA_MCP_TOKEN for Docker integration tests")
    return url, token


def _restricted_live_settings() -> tuple[str, str]:
    """Require a separately scoped token for cursor-isolation coverage."""
    url = os.getenv("MA_MCP_URL")
    token = os.getenv("MA_MCP_RESTRICTED_TOKEN")
    if not url or not token:
        pytest.skip("set MA_MCP_URL and MA_MCP_RESTRICTED_TOKEN for restricted catalog tests")
    return url, token


def _confirm_live_settings() -> tuple[str, str]:
    """Require the token assigned a Confirm debug-provider policy."""
    url = os.getenv("MA_MCP_URL")
    token = os.getenv("MA_MCP_CONFIRM_TOKEN")
    if not url or not token:
        pytest.skip("set MA_MCP_URL and MA_MCP_CONFIRM_TOKEN for Confirm acceptance")
    return url, token


@pytest.fixture
async def live_client() -> AsyncIterator[LiveClient]:
    """Yield a live authenticated client only when credentials were supplied."""
    url, token = _live_settings()
    transport = StreamableHttpTransport(url, auth=token)
    async with Client(transport, elicitation_handler=_accept_elicitation) as client:
        yield client


@pytest.fixture
async def restricted_live_client() -> AsyncIterator[LiveClient]:
    """Yield an explicitly restricted MA user for visibility-isolation coverage."""
    url, token = _restricted_live_settings()
    transport = StreamableHttpTransport(url, auth=token)
    async with Client(transport, elicitation_handler=_accept_elicitation) as client:
        yield client


@pytest.fixture
async def confirm_live_client() -> AsyncIterator[LiveClient]:
    """Yield a Confirm-policy client without an elicitation implementation."""
    url, token = _confirm_live_settings()
    transport = StreamableHttpTransport(url, auth=token)
    async with Client(transport) as client:
        yield client


def structured_content(result: Any) -> Mapping[str, Any]:
    """Require FastMCP's typed structured result instead of display-oriented data."""
    assert not result.is_error, result.content
    assert result.structured_content is not None, result.content
    return cast("Mapping[str, Any]", result.structured_content)


async def collect_tool_catalog(client: LiveClient) -> tuple[list[str], str]:
    """Traverse every alphabetical command page through the discovery tool."""
    names: list[str] = []
    cursor: str | None = None
    revision: str | None = None
    total: int | None = None
    while True:
        arguments: dict[str, Any] = {"limit": 50}
        if cursor is None:
            arguments["query"] = ""
        else:
            arguments["cursor"] = cursor
        result = await client.call_tool("search_tools", arguments)
        page = structured_content(result)
        assert page["mode"] == "catalog"
        assert all(set(item) == {"name", "policy_mode"} for item in page["items"])
        assert all(item["policy_mode"] in {"allow", "confirm"} for item in page["items"])
        revision = revision or str(page["catalog_revision"])
        total = int(page["total"]) if total is None else total
        assert page["catalog_revision"] == revision
        assert page["total"] == total
        names.extend(str(item["name"]) for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert len(names) == total
            assert revision is not None
            return names, revision


async def collect_resource_catalog(client: LiveClient) -> tuple[list[str], str]:
    """Traverse the same alphabetical catalog through its resource pages."""
    names: list[str] = []
    uri: str | None = "catalog://commands?limit=50"
    revision: str | None = None
    total: int | None = None
    while uri is not None:
        contents = await client.read_resource(uri)
        page = json.loads(next(item.text for item in contents if hasattr(item, "text")))
        assert set(page) == {"items", "total", "next_cursor", "next_uri", "catalog_revision"}
        assert all(set(item) == {"name", "policy_mode"} for item in page["items"])
        assert all(item["policy_mode"] in {"allow", "confirm"} for item in page["items"])
        revision = revision or str(page["catalog_revision"])
        total = int(page["total"]) if total is None else total
        assert page["catalog_revision"] == revision
        assert page["total"] == total
        names.extend(str(item["name"]) for item in page["items"])
        uri = page["next_uri"]
    assert len(names) == total
    assert revision is not None
    return names, revision


async def call_ma(client: LiveClient, command: str, arguments: Mapping[str, Any]) -> Any:
    """Invoke a canonical MA command and return its JSON-compatible payload."""
    result = await client.call_tool(
        "call_tool", {"name": f"ma_api:{command}", "arguments": dict(arguments)}
    )
    envelope = structured_content(result)
    assert envelope["command"] == f"ma_api:{command}"
    json.dumps(envelope["data"])
    return envelope["data"]


def require_env(name: str) -> str:
    """Require explicit opt-in before any state-changing integration operation."""
    value = os.getenv(name)
    if not value:
        pytest.skip(f"set {name} to run the queue mutation test")
    return value


def item_id(item: Mapping[str, Any]) -> str:
    """Normalize MA queue-item identifiers across current server versions."""
    return str(item.get("queue_item_id") or item["item_id"])


async def queue_items(client: LiveClient, queue_id: str) -> list[dict[str, Any]]:
    """Read enough queue items to compare ordering after reversible cleanup."""
    return cast(
        "list[dict[str, Any]]",
        await call_ma(client, "player_queues/items", {"queue_id": queue_id, "limit": 500}),
    )


async def find_test_track_uri(client: LiveClient, *, purpose: str) -> str:
    """Find one provider-backed item, or explicitly skip unavailable live coverage."""
    search = await call_ma(
        client,
        "music/search",
        {
            "search_query": "Daft Punk Random Access Memories",
            "media_types": ["track"],
            "limit": 5,
            "library_only": False,
        },
    )
    if not (tracks := search.get("tracks", [])):
        pytest.skip(f"configured providers returned no track for {purpose}")
    for track in tracks:
        uri = str(track.get("uri", ""))
        provider = str(track.get("provider", ""))
        if uri.startswith("yandex_music") and provider.startswith("yandex_music"):
            return uri
    pytest.skip(f"configured provider search returned no provider-backed track URI for {purpose}")


async def wait_for_own_added_item(
    client: LiveClient, queue_id: str, before_ids: set[str], track_uri: str
) -> dict[str, Any]:
    """Find only this test's one new URI, never a concurrent caller's row."""
    for _attempt in range(20):
        added = [
            item for item in await queue_items(client, queue_id) if item_id(item) not in before_ids
        ]
        owned = [item for item in added if str(item.get("uri", "")) == track_uri]
        if len(owned) == 1:
            return owned[0]
        if len(owned) > 1:
            raise AssertionError("concurrent queue update made the test-added item ambiguous")
        await asyncio.sleep(0.25)
    raise AssertionError("test-added queue item did not appear within five seconds")


async def remove_added_item(client: LiveClient, queue_id: str, added_id: str) -> None:
    """Remove the exact queue item ID established from this test's unique diff."""
    removed = await call_ma(
        client,
        "fastmcp/queue/remove_items_safe",
        {"queue_id": queue_id, "item_ids": [added_id]},
    )
    assert removed["removed"] == [added_id]


@pytest.mark.integration
async def test_live_meta_surface_and_discovery_latency(live_client: LiveClient) -> None:
    """The mounted endpoint exposes only discovery tools and has bounded lookup latency."""
    assert {tool.name for tool in await live_client.list_tools()} == {
        "search_tools",
        "get_tool_schema",
        "call_tool",
    }
    started = time.monotonic()
    await asyncio.gather(
        *(live_client.call_tool("search_tools", {"query": "album tracks"}) for _ in range(10))
    )
    cold_elapsed = time.monotonic() - started
    started = time.monotonic()
    await live_client.call_tool("search_tools", {"query": "album tracks"})
    warm_elapsed = time.monotonic() - started
    print(f"discovery cold={cold_elapsed:.3f}s warm={warm_elapsed:.3f}s")  # noqa: T201
    assert cold_elapsed < 5.0
    assert warm_elapsed < 1.0


@pytest.mark.integration
async def test_live_paginated_catalog_tool_resource_parity(live_client: LiveClient) -> None:
    """Tool and resource traversal enumerate one stable visible MA catalog."""
    templates = {str(item.uriTemplate) for item in await live_client.list_resource_templates()}
    assert "catalog://commands{?cursor,limit}" in templates
    tool_names, tool_revision = await collect_tool_catalog(live_client)
    resource_names, resource_revision = await collect_resource_catalog(live_client)
    assert tool_names == sorted(tool_names)
    assert len(tool_names) == len(set(tool_names))
    assert resource_names == tool_names
    assert resource_revision == tool_revision
    for name in (
        "ma_api:music/search",
        "ma_api:music/albums/library_items",
        "ma_api:providers",
        "ma_api:players/all",
        "ma_api:player_queues/items",
        "ma_api:config/providers",
        "ma_api:fastmcp/debug/health",
    ):
        assert name in tool_names
        schema = await live_client.call_tool("get_tool_schema", {"tool_name": name})
        structured_schema = structured_content(schema)
        assert structured_schema["name"] == name
        assert structured_schema["policy_mode"] in {"allow", "confirm"}


@pytest.mark.integration
async def test_live_restricted_catalog_isolated_from_broader_cursor(
    live_client: LiveClient, restricted_live_client: LiveClient
) -> None:
    """A cursor issued to a broader user never crosses the restricted view."""
    broad_first = await live_client.call_tool("search_tools", {"query": "", "limit": 1})
    restricted_names, _revision = await collect_tool_catalog(restricted_live_client)
    broad_names, _revision = await collect_tool_catalog(live_client)
    assert set(restricted_names) < set(broad_names)
    with pytest.raises(ToolError, match="catalog_changed"):
        await restricted_live_client.call_tool(
            "search_tools", {"cursor": structured_content(broad_first)["next_cursor"]}
        )


@pytest.mark.integration
async def test_live_track_album_and_player_calls_are_json_serializable(
    live_client: LiveClient,
) -> None:
    """Provider-backed item, album, and player paths serialize through one envelope."""
    providers = await call_ma(live_client, "providers", {})
    assert any(
        provider.get("domain") == "yandex_music" and provider.get("available") is True
        for provider in providers
    )
    track_uri = await find_test_track_uri(live_client, purpose="track/album serialization")
    track = await call_ma(live_client, "music/item_by_uri", {"uri": track_uri})
    assert track["uri"] == track_uri
    album = track.get("album")
    if not isinstance(album, Mapping) or not (album_uri := album.get("uri")):
        pytest.skip("provider-backed track did not include an album URI")
    album_details = await call_ma(live_client, "music/item_by_uri", {"uri": album_uri})
    await call_ma(
        live_client,
        "music/albums/album_tracks",
        {
            "item_id": str(album_details["item_id"]),
            "provider_instance_id_or_domain": str(album_details["provider"]),
        },
    )
    players = await call_ma(live_client, "players/all", {})
    if not players:
        pytest.skip("configured MA instance has no player for players/get regression")
    player_id = str(players[0]["player_id"])
    await call_ma(live_client, "players/get", {"player_id": player_id})
    active_queue = await call_ma(
        live_client, "player_queues/get_active_queue", {"player_id": player_id}
    )
    if not active_queue:
        pytest.skip("configured player has no active queue for queue read regression")
    queue_id = str(active_queue["queue_id"])
    await call_ma(live_client, "player_queues/get", {"queue_id": queue_id})
    await call_ma(live_client, "player_queues/items", {"queue_id": queue_id, "limit": 2})


@pytest.mark.integration
@pytest.mark.parametrize("command", LIBRARY_ITEM_COMMANDS)
async def test_live_library_items_have_truthful_schema_and_execute(
    live_client: LiveClient, command: str
) -> None:
    """Library list schemas have no synthetic kwargs and return JSON data."""
    schema_result = await live_client.call_tool(
        "get_tool_schema", {"tool_name": f"ma_api:{command}"}
    )
    schema = structured_content(schema_result)
    assert "kwargs" not in schema["inputSchema"].get("properties", {})
    assert "kwargs" not in schema["inputSchema"].get("required", [])
    output = schema.get("outputSchema", {})
    assert not (output.get("type") == "string" and "list[" in output.get("x-python-type", ""))
    await call_ma(live_client, command, {"limit": 2})


@pytest.mark.integration
async def test_live_reversible_queue_cycle(live_client: LiveClient) -> None:
    """Only an explicitly selected non-current queue item is added and removed."""
    player_id = require_env("MA_TEST_PLAYER_ID")
    queue = await call_ma(live_client, "player_queues/get_active_queue", {"player_id": player_id})
    queue_id = str(queue["queue_id"])
    before = await queue_items(live_client, queue_id)
    if not before or queue.get("current_index") is None:
        pytest.skip("queue test requires a dedicated player with an active non-empty queue")
    before_ids = {item_id(item) for item in before}
    track_uri = await find_test_track_uri(live_client, purpose="queue mutation")
    if any(str(item.get("uri", "")) == track_uri for item in before):
        pytest.skip("dedicated queue already contains the selected test track URI")
    added_id: str | None = None
    add_attempted = False
    cleaned_up = False
    try:
        # The transport might fail after MA accepted this request, so recovery starts
        # before the request is made and never relies on its response arriving.
        add_attempted = True
        await call_ma(
            live_client,
            "player_queues/play_media",
            {
                "queue_id": queue_id,
                "media": track_uri,
                "option": "add",
            },
        )
        added = await wait_for_own_added_item(live_client, queue_id, before_ids, track_uri)
        added_id = item_id(added)
        assert added.get("played", False) is False
        refreshed = await call_ma(live_client, "player_queues/get", {"queue_id": queue_id})
        current_index = refreshed.get("current_index")
        buffer_index = refreshed.get("index_in_buffer")
        protected = max(
            int(current_index) if current_index is not None else -1,
            int(buffer_index) if buffer_index is not None else -1,
        )
        assert int(added["index"]) > protected
        await call_ma(
            live_client,
            "player_queues/move_item_end",
            {"queue_id": queue_id, "queue_item_id": added_id},
        )
        await remove_added_item(live_client, queue_id, added_id)
        added_id = None
        cleaned_up = True
    finally:
        if add_attempted and not cleaned_up:
            if added_id is None:
                # Recover the exact test-owned row if the first visibility poll
                # timed out or its response was interrupted. Ambiguous concurrent
                # rows are rejected by wait_for_own_added_item rather than removed.
                added = await wait_for_own_added_item(live_client, queue_id, before_ids, track_uri)
                added_id = item_id(added)
            await remove_added_item(live_client, queue_id, added_id)
    assert [item_id(item) for item in await queue_items(live_client, queue_id)] == [
        item_id(item) for item in before
    ]


@pytest.mark.integration
async def test_live_annotations_remain_truthful(
    live_client: LiveClient,
) -> None:
    """Destructive queue schemas and read-only health annotations remain truthful."""
    for command in (
        "player_queues/delete_item",
        "player_queues/clear",
        "fastmcp/queue/remove_items_safe",
    ):
        result = await live_client.call_tool("get_tool_schema", {"tool_name": f"ma_api:{command}"})
        schema = structured_content(result)
        assert schema["annotations"]["destructiveHint"] is True
    health = await live_client.call_tool(
        "get_tool_schema", {"tool_name": "ma_api:fastmcp/debug/health"}
    )
    health_schema = structured_content(health)
    assert health_schema["annotations"]["readOnlyHint"] is True


@pytest.mark.integration
async def test_v2_acceptance_allow_debug_health_executes(live_client: LiveClient) -> None:
    """The primary Docker token is assigned debug:providers=Allow."""
    schema = structured_content(
        await live_client.call_tool(
            "get_tool_schema",
            {"tool_name": "ma_api:fastmcp/debug/health"},
        )
    )
    assert schema["policy_mode"] == "allow"
    health = await call_ma(live_client, "fastmcp/debug/health", {})
    assert isinstance(health, Mapping)


@pytest.mark.integration
async def test_v2_acceptance_confirm_without_elicitation_does_not_execute(
    confirm_live_client: LiveClient,
) -> None:
    """The Confirm token cannot execute debug health without elicitation support."""
    schema = structured_content(
        await confirm_live_client.call_tool(
            "get_tool_schema",
            {"tool_name": "ma_api:fastmcp/debug/health"},
        )
    )
    assert schema["policy_mode"] == "confirm"
    with pytest.raises(ToolError, match="requires confirmation"):
        await call_ma(confirm_live_client, "fastmcp/debug/health", {})


@pytest.mark.integration
async def test_v2_acceptance_two_tokens_observe_assigned_profiles(
    live_client: LiveClient,
    restricted_live_client: LiveClient,
) -> None:
    """Broad and restricted Docker tokens retain distinct catalog profiles."""
    broad_names, _revision = await collect_tool_catalog(live_client)
    restricted_names, _revision = await collect_tool_catalog(restricted_live_client)
    command = "ma_api:fastmcp/debug/health"
    assert command in broad_names
    assert command not in restricted_names
