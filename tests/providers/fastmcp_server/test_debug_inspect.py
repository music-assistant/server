"""End-to-end tests for debug_inspect_player/queue/provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


def _player_full(
    player_id: str, *, available: bool = True, enabled: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        player_id=player_id,
        name=f"Player {player_id}",
        available=available,
        enabled=enabled,
        hidden=False,
        powered=True,
        volume_level=42,
        playback_state=SimpleNamespace(value="idle"),
        current_media=None,
        active_group=None,
        synced_to=None,
        last_error=None,
        extra_data={"flavor": "test"},
        state=SimpleNamespace(power="on", active_group=None, synced_to=None),
    )


async def _call(mcp: Any, tool_name: str, **kwargs: Any) -> Any:
    async with Client(mcp) as client:
        return await client.call_tool(f"debug_{tool_name}", kwargs)


async def test_inspect_player_returns_raw_and_state(mounted_debug: Any, mock_mass: Any) -> None:
    """``debug_inspect_player`` returns raw object and state object separately."""
    player = _player_full("kitchen")
    mock_mass.players.get_player = lambda pid: player if pid == "kitchen" else None
    result = await _call(mounted_debug, "inspect_player", player_id="kitchen")
    data = result.data
    assert data.player_id == "kitchen"
    assert data.raw["name"] == "Player kitchen"
    assert data.state["power"] == "on"
    assert data.truncated is False


async def test_inspect_unavailable_player_still_returns_state(
    mounted_debug: Any, mock_mass: Any
) -> None:
    """Unavailable players still surface full raw and state — the point of debug_inspect."""
    player = _player_full("broken", available=False)
    player.last_error = "auth expired"
    mock_mass.players.get_player = lambda pid: player if pid == "broken" else None
    result = await _call(mounted_debug, "inspect_player", player_id="broken")
    assert result.data.raw["available"] is False
    assert result.data.raw["last_error"] == "auth expired"


async def test_inspect_disabled_player_still_returns_state(
    mounted_debug: Any, mock_mass: Any
) -> None:
    """Disabled players still surface full raw and state — the point of debug_inspect."""
    player = _player_full("off", enabled=False)
    mock_mass.players.get_player = lambda pid: player if pid == "off" else None
    result = await _call(mounted_debug, "inspect_player", player_id="off")
    assert result.data.raw["enabled"] is False


async def test_inspect_unknown_player_raises_tool_error(mounted_debug: Any, mock_mass: Any) -> None:
    """Unknown player_id raises ToolError (AC #9)."""
    mock_mass.players.get_player = lambda pid: None  # noqa: ARG005
    with pytest.raises(ToolError, match="player_id='nope' not found"):
        await _call(mounted_debug, "inspect_player", player_id="nope")


async def test_inspect_queue_returns_raw_and_current_item(
    mounted_debug: Any, mock_mass: Any
) -> None:
    """``debug_inspect_queue`` returns raw queue and current_item resolved."""
    queue = SimpleNamespace(queue_id="kitchen", current_index=0, items_count=3, state="playing")
    current = SimpleNamespace(uri="library://track/1", title="Test", duration=180)
    mock_mass.player_queues.get = lambda qid: queue if qid == "kitchen" else None
    mock_mass.player_queues.get_active_queue = lambda pid: queue  # noqa: ARG005
    queue.current_item = current
    result = await _call(mounted_debug, "inspect_queue", queue_id="kitchen")
    assert result.data.queue_id == "kitchen"
    assert result.data.current_item is not None
    assert result.data.current_item["title"] == "Test"


async def test_inspect_provider_returns_raw_and_manifest(
    mounted_debug: Any, mock_mass: Any
) -> None:
    """``debug_inspect_provider`` returns raw provider and manifest separately."""
    manifest = SimpleNamespace(domain="yandex_music", name="Yandex Music", multi_instance=False)
    prov = SimpleNamespace(
        instance_id="yandex_music_1",
        domain="yandex_music",
        name="Yandex Music",
        available=True,
        last_error=None,
        lookup_key="yandex_music_1",
        supported_features=["search", "browse"],
        manifest=manifest,
    )
    mock_mass.get_provider = lambda iid: prov if iid == "yandex_music_1" else None
    result = await _call(mounted_debug, "inspect_provider", instance_id="yandex_music_1")
    assert result.data.raw["domain"] == "yandex_music"
    assert result.data.manifest["domain"] == "yandex_music"


async def test_inspect_property_raise_renders_placeholder(
    mounted_debug: Any, mock_mass: Any
) -> None:
    """Property that raises renders ``<raise: ExceptionType>`` placeholder (AC #2)."""

    class _Bad:
        player_id = "broken"
        name = "Broken"
        available = True
        enabled = True
        state = SimpleNamespace(power="on")

        @property
        def volume_level(self) -> None:
            raise RuntimeError("not ready")

    mock_mass.players.get_player = lambda pid: _Bad() if pid == "broken" else None
    result = await _call(mounted_debug, "inspect_player", player_id="broken")
    assert result.data.raw["volume_level"] == "<raise: RuntimeError>"
