"""Tests for queue_get_active_queue index visibility."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools.queue import build_queue_server


def _queue(*, current_index: int = 2, index_in_buffer: int | None = 4) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id="q1",
        current_index=current_index,
        index_in_buffer=index_in_buffer,
        items=10,
        shuffle_enabled=False,
        repeat_mode=SimpleNamespace(value="off"),
    )


def _items(count: int, *, start: int = 0) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            queue_item_id=f"id-{i}",
            name=f"track-{i}",
            duration=180,
            media_item=None,
        )
        for i in range(start, start + count)
    ]


async def test_get_active_queue_exposes_insert_index_fields(mock_mass: MagicMock) -> None:
    """get_active_queue returns next_insertable_index, index_in_buffer, and item.index."""
    queue = _queue(current_index=2, index_in_buffer=4)
    mock_mass.player_queues.get_active_queue = MagicMock(return_value=queue)
    mock_mass.player_queues.items = MagicMock(return_value=_items(3))

    mcp = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "queue_get_active_queue", {"player_id": "p1", "include_items": 3}
        )

    assert result.data.index_in_buffer == 4
    assert result.data.next_insertable_index == 5
    assert result.data.items_start_index == 0
    assert [item.index for item in result.data.items] == [0, 1, 2]
    mock_mass.player_queues.items.assert_called_once_with("q1", limit=3, offset=0)


async def test_get_active_queue_items_from_current_uses_offset(mock_mass: MagicMock) -> None:
    """items_from_current fetches from current_index and sets items_start_index."""
    queue = _queue(current_index=7, index_in_buffer=9)
    mock_mass.player_queues.get_active_queue = MagicMock(return_value=queue)
    mock_mass.player_queues.items = MagicMock(return_value=_items(2, start=7))

    mcp = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "queue_get_active_queue",
            {"player_id": "p1", "include_items": 2, "items_from_current": True},
        )

    assert result.data.items_start_index == 7
    assert [item.index for item in result.data.items] == [7, 8]
    assert result.data.next_insertable_index == 10
    mock_mass.player_queues.items.assert_called_once_with("q1", limit=2, offset=7)
