"""Optional MCP App registration and closed-action tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from music_assistant.providers.fastmcp_server.app_music_assistant import (
    APP_TOOL_NAME,
    MusicAssistantAction,
    _execute_action,
    _first_identifier,
    _load_state,
    _required_context,
    register_music_assistant_app,
)
from music_assistant.providers.fastmcp_server.meta_discovery import register_meta_discovery
from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.policy import PolicyProfile, policy_snapshot
from music_assistant.providers.fastmcp_server.server import build_tag_lookup


class FakeAdapter:
    """Minimal shared-pipeline double for App registration and rendering."""

    def __init__(self) -> None:
        """Create an empty canonical call log."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, arguments: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Return compact live-state fixtures and record canonical execution."""
        self.calls.append((name, arguments))
        data: Any
        if name == "ma_api:players/all":
            data = [{"player_id": "p1", "name": "Kitchen", "state": "playing"}]
        elif name == "ma_api:player_queues/all":
            data = []
        elif name == "ma_api:player_queues/get_active_queue":
            data = {"queue_id": "q1", "current_item": {"name": "Song"}}
        elif name == "ma_api:player_queues/items":
            data = [
                {"queue_item_id": "i1", "name": "Song", "index": 3},
                {"queue_item_id": 7, "name": "Invalid identifier", "index": 4},
            ]
        else:
            data = None
        return {"data": data, "bytes": 1}

    async def catalog_context(self) -> Any:
        """Expose only controls that the fake policy permits."""
        names = {
            "ma_api:players/cmd/play",
            "ma_api:players/cmd/volume_set",
            "ma_api:player_queues/move_item",
            "ma_api:player_queues/delete_item",
        }
        return SimpleNamespace(view=SimpleNamespace(by_name=dict.fromkeys(names, object())))


class UnexpectedResourceAuthorizer:
    """Fail when infrastructure is misclassified as an authorized data resource."""

    async def authorize(self, _uri: str, _tags: set[str]) -> None:
        """Synthetic renderer reads must bypass capability authorization."""
        raise AssertionError("Prefab renderer was classified as an unknown data resource")


class DenyingResourceAuthorizer:
    """Reject resources that do not resolve to known infrastructure."""

    async def authorize(self, _uri: str, _tags: set[str]) -> None:
        """Mirror the fail-closed unknown-resource contract."""


def test_app_rejects_external_renderer_and_missing_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional surface stays local and fails closed outside a request."""
    monkeypatch.setenv("PREFAB_RENDERER_URL", "https://example.invalid/renderer")
    with pytest.raises(RuntimeError, match="External Prefab renderers"):
        register_music_assistant_app(FastMCP("test"), cast("Any", FakeAdapter()))
    with pytest.raises(ToolError, match=r"\[execution_failed\]"):
        _required_context(None)


async def test_app_state_selects_the_first_available_player() -> None:
    """State refresh has a deterministic default when no player is selected."""
    state = await _load_state(
        cast("Any", FakeAdapter()),
        None,
        cast("Any", object()),
    )
    assert state["selected_player_id"] == "p1"
    assert _first_identifier([{"player_id": 1}], "player_id") is None


async def test_app_adds_one_model_tool_and_keeps_backends_app_only() -> None:
    """Enabled App adds one model entry, two private tools and one local renderer."""
    adapter = FakeAdapter()
    mcp = FastMCP("test")
    register_music_assistant_app(mcp, cast("Any", adapter))
    register_meta_discovery(
        mcp,
        dynamic_adapter=cast("Any", adapter),
        additional_public_tools=frozenset({APP_TOOL_NAME}),
    )

    async with Client(mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        with pytest.raises(ToolError):
            await client.call_tool("ma_app_state", {})

    assert {tool.name for tool in tools} == {
        "search_tools",
        "get_tool_schema",
        "call_tool",
        APP_TOOL_NAME,
    }
    assert len(resources) == 1
    assert str(resources[0].uri).startswith("ui://prefab/tool/")
    ui_meta = (resources[0].meta or {}).get("ui", {})
    assert not ui_meta.get("csp", {}).get("connectDomains")
    assert not ui_meta.get("csp", {}).get("resourceDomains")
    assert not ui_meta.get("csp", {}).get("frameDomains")


async def test_synthetic_renderer_is_readable_through_policy_middleware() -> None:
    """A renderer advertised by FastMCP remains readable through the live middleware path."""
    mcp = FastMCP("test")
    register_music_assistant_app(mcp, cast("Any", FakeAdapter()))
    mcp.add_middleware(
        TagFilterMiddleware(
            build_tag_lookup(mcp),
            lambda: policy_snapshot(PolicyProfile.TRUSTED),
            resource_authorizer=cast("Any", UnexpectedResourceAuthorizer()),
        )
    )

    async with Client(mcp) as client:
        resources = await client.list_resources()
        renderer = next(
            resource for resource in resources if str(resource.uri).startswith("ui://prefab/tool/")
        )
        contents = await client.read_resource(renderer.uri)

    assert contents
    assert contents[0].mimeType == "text/html;profile=mcp-app"


async def test_unknown_prefab_renderer_remains_blocked() -> None:
    """A Prefab-looking URI is not infrastructure unless FastMCP actually publishes it."""
    mcp = FastMCP("test")
    register_music_assistant_app(mcp, cast("Any", FakeAdapter()))
    mcp.add_middleware(
        TagFilterMiddleware(
            build_tag_lookup(mcp),
            lambda: policy_snapshot(PolicyProfile.TRUSTED),
            resource_authorizer=cast("Any", DenyingResourceAuthorizer()),
        )
    )

    async with Client(mcp) as client:
        with pytest.raises(McpError, match="Resource is not permitted"):
            await client.read_resource("ui://prefab/tool/deadbeef0000/renderer.html")


async def test_app_entry_returns_prefab_and_text_fallback() -> None:
    """Apps hosts receive Prefab content while ordinary clients retain text."""
    adapter = FakeAdapter()
    mcp = FastMCP("test")
    register_music_assistant_app(mcp, cast("Any", adapter))
    register_meta_discovery(
        mcp,
        dynamic_adapter=cast("Any", adapter),
        additional_public_tools=frozenset({APP_TOOL_NAME}),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(APP_TOOL_NAME, {"player_id": "p1"})

    assert result.structured_content is not None
    assert "$prefab" in result.structured_content
    assert any(
        "Rendered Prefab UI" in block.text for block in result.content if hasattr(block, "text")
    )


async def test_app_backend_actions_share_execution_and_redact_validation_errors() -> None:
    """Renderer-only tools execute canonical calls and expose stable public errors."""
    adapter = FakeAdapter()
    mcp = FastMCP("test")
    register_music_assistant_app(mcp, cast("Any", adapter))
    app_provider = cast("Any", mcp.providers[-1])
    state_tool = await app_provider.get_tool("ma_app_state")
    action_tool = await app_provider.get_tool("ma_app_action")

    state = await state_tool.fn(player_id="p1", ctx=cast("Any", object()))
    result = await action_tool.fn(
        action=MusicAssistantAction.PLAY,
        player_id="p1",
        item_id=None,
        target_index=None,
        value=None,
        ctx=cast("Any", object()),
    )
    with pytest.raises(ToolError, match=r"\[invalid_arguments\]"):
        await action_tool.fn(
            action=MusicAssistantAction.POWER,
            player_id="p1",
            item_id=None,
            target_index=None,
            value=1,
            ctx=cast("Any", object()),
        )

    assert state["selected_player_id"] == "p1"
    assert result["ok"] is True
    assert result["action"] == "play"


async def test_queue_move_translates_to_canonical_relative_shift() -> None:
    """The closed queue action computes MA's relative move argument safely."""
    adapter = FakeAdapter()
    await _execute_action(
        cast("Any", adapter),
        MusicAssistantAction.QUEUE_MOVE,
        "p1",
        item_id="i1",
        target_index=1,
        value=None,
        ctx=cast("Any", object()),
    )
    assert adapter.calls[-1] == (
        "ma_api:player_queues/move_item",
        {"queue_id": "q1", "queue_item_id": "i1", "pos_shift": -2},
    )


async def test_queue_move_and_unknown_action_fail_closed() -> None:
    """Stale queue items and values outside the enum cannot mutate the queue."""

    class MissingItemAdapter(FakeAdapter):
        async def call(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if name == "ma_api:player_queues/items":
                self.calls.append((name, arguments))
                return {"data": [], "bytes": 1}
            return await super().call(name, arguments, **kwargs)

    with pytest.raises(ValueError, match="no longer exists"):
        await _execute_action(
            cast("Any", MissingItemAdapter()),
            MusicAssistantAction.QUEUE_MOVE,
            "p1",
            item_id="gone",
            target_index=1,
            value=None,
            ctx=cast("Any", object()),
        )
    with pytest.raises(ValueError, match="Unsupported"):
        await _execute_action(
            cast("Any", FakeAdapter()),
            cast("Any", "unknown"),
            "p1",
            item_id="i1",
            target_index=None,
            value=None,
            ctx=cast("Any", object()),
        )


@pytest.mark.parametrize(
    ("action", "value", "expected_command", "expected_arguments"),
    [
        (MusicAssistantAction.PLAY, None, "players/cmd/play", {"player_id": "p1"}),
        (
            MusicAssistantAction.POWER,
            True,
            "players/cmd/power",
            {"player_id": "p1", "powered": True},
        ),
        (
            MusicAssistantAction.MUTE,
            False,
            "players/cmd/volume_mute",
            {"player_id": "p1", "muted": False},
        ),
        (
            MusicAssistantAction.VOLUME,
            42,
            "players/cmd/volume_set",
            {"player_id": "p1", "volume_level": 42},
        ),
        (
            MusicAssistantAction.QUEUE_REMOVE,
            None,
            "player_queues/delete_item",
            {"queue_id": "q1", "item_id_or_index": "i1"},
        ),
    ],
)
async def test_app_actions_translate_to_fixed_canonical_commands(
    action: MusicAssistantAction,
    value: int | bool | None,
    expected_command: str,
    expected_arguments: dict[str, Any],
) -> None:
    """Every app action uses a fixed canonical command and typed arguments."""
    adapter = FakeAdapter()

    await _execute_action(
        cast("Any", adapter),
        action,
        "p1",
        item_id="i1",
        target_index=None,
        value=value,
        ctx=cast("Any", object()),
    )

    assert adapter.calls[-1] == (f"ma_api:{expected_command}", expected_arguments)


@pytest.mark.parametrize(
    ("action", "item_id", "target_index", "value"),
    [
        (MusicAssistantAction.POWER, "i1", None, 1),
        (MusicAssistantAction.VOLUME, "i1", None, 101),
        (MusicAssistantAction.QUEUE_MOVE, "i1", None, None),
        (MusicAssistantAction.QUEUE_REMOVE, None, None, None),
    ],
)
async def test_app_action_validation_fails_before_mutation(
    action: MusicAssistantAction,
    item_id: str | None,
    target_index: int | None,
    value: int | bool | None,
) -> None:
    """Invalid typed action inputs never reach a mutating MA command."""
    adapter = FakeAdapter()

    with pytest.raises((TypeError, ValueError)):
        await _execute_action(
            cast("Any", adapter),
            action,
            "p1",
            item_id=item_id,
            target_index=target_index,
            value=value,
            ctx=cast("Any", object()),
        )

    assert not any("/cmd/" in command or "delete_item" in command for command, _ in adapter.calls)
