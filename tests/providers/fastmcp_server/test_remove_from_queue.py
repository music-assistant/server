"""Tests for the ``queue_remove_item`` MCP tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


def _queue_item(*, item_id: str, name: str = "Track") -> SimpleNamespace:
    return SimpleNamespace(queue_item_id=item_id, name=name, uri=f"library://track/{item_id}")


def _mock_removable_queue(
    mock_mass: MagicMock,
    *,
    queue_id: str,
    items: list[SimpleNamespace],
    index_in_buffer: int | None = None,
    current_index: int | None = None,
) -> dict[str, int]:
    """
    Wire get, index_by_id and delete_item for remove_item tests.

    ``delete_item`` removes the id from the index map so the tool's
    post-delete verification observes the row as gone — mirroring MA.
    Returns the live id→index map for tests that need to tweak it.
    """
    id_to_index = {it.queue_item_id: idx for idx, it in enumerate(items)}
    queue = SimpleNamespace(
        queue_id=queue_id,
        current_index=current_index,
        index_in_buffer=index_in_buffer,
        items=len(items),
    )
    mock_mass.player_queues.get = MagicMock(return_value=queue)
    mock_mass.player_queues.index_by_id = MagicMock(
        side_effect=lambda _qid, item_id: id_to_index.get(item_id)
    )
    mock_mass.player_queues.delete_item = MagicMock(
        side_effect=lambda _qid, item_id: id_to_index.pop(item_id, None)
    )
    return id_to_index


async def test_remove_item_deletes_each_id(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Each removable item_id is forwarded to player_queues.delete_item."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="abc"), _queue_item(item_id="def")],
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["abc", "def"]},
        )
    assert result.data.removed == ["abc", "def"]
    assert result.data.skipped_buffered == []
    assert result.data.skipped_played == []
    assert result.data.not_found == []
    assert mock_mass.player_queues.delete_item.call_args_list == [
        call("q1", "abc"),
        call("q1", "def"),
    ]


async def test_remove_item_skips_buffered_row(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Rows at or before index_in_buffer are skipped without calling delete_item."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="g0"), _queue_item(item_id="g1")],
        index_in_buffer=0,
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["g0"]},
        )
    assert result.data.removed == []
    assert result.data.skipped_buffered == ["g0"]
    assert result.data.skipped_played == []
    mock_mass.player_queues.delete_item.assert_not_called()


async def test_remove_item_skips_played_row(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Rows at or before current_index are skipped without calling delete_item."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="played"), _queue_item(item_id="upnext")],
        current_index=0,
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["played"]},
        )
    assert result.data.removed == []
    assert result.data.skipped_buffered == []
    assert result.data.skipped_played == ["played"]
    mock_mass.player_queues.delete_item.assert_not_called()


async def test_remove_item_mixed_batch(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Buffered and removable ids in one call land in separate ack buckets."""
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="g0"), _queue_item(item_id="g1")],
        index_in_buffer=0,
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["g0", "g1"]},
        )
    assert result.data.removed == ["g1"]
    assert result.data.skipped_buffered == ["g0"]
    assert result.data.skipped_played == []
    mock_mass.player_queues.delete_item.assert_called_once_with("q1", "g1")


async def test_remove_item_classifies_played_before_buffered(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """
    History rows land in skipped_played even when index_in_buffer is set.

    Regression guard for the upstream #4410 bug: the buffer check ran first,
    and since MA keeps ``index_in_buffer >= current_index`` every played row
    was misfiled into skipped_buffered, leaving skipped_played unreachable.
    """
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id=f"i{n}") for n in range(5)],
        current_index=2,
        index_in_buffer=3,
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["i1", "i3", "i4"]},
        )
    assert result.data.skipped_played == ["i1"]
    assert result.data.skipped_buffered == ["i3"]
    assert result.data.removed == ["i4"]
    mock_mass.player_queues.delete_item.assert_called_once_with("q1", "i4")


async def test_remove_item_reports_unknown_id_in_not_found(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """An unknown item_id lands in not_found instead of raising."""
    _mock_removable_queue(mock_mass, queue_id="q1", items=[])
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["missing"]},
        )
    assert result.data.not_found == ["missing"]
    assert result.data.removed == []
    mock_mass.player_queues.delete_item.assert_not_called()


async def test_remove_item_partial_batch_keeps_ack(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """
    A stale id mid-batch does not lose the ack for rows already removed.

    Regression guard for the upstream #4410 bug: a ToolError raised midway
    through the loop discarded the ``removed`` list, so the agent retried
    ids that were in fact already deleted.
    """
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="a"), _queue_item(item_id="b")],
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["a", "missing", "b"]},
        )
    assert result.data.removed == ["a", "b"]
    assert result.data.not_found == ["missing"]
    assert mock_mass.player_queues.delete_item.call_args_list == [
        call("q1", "a"),
        call("q1", "b"),
    ]


async def test_remove_item_reports_silently_ignored_delete_as_skipped(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """
    A delete MA quietly ignores is acknowledged as skipped, not removed.

    MA's delete_item logs a warning and returns without deleting when the
    row is already loaded in the player buffer; the ack must reflect what
    actually happened, so the tool re-checks the row after the call.
    """
    _mock_removable_queue(
        mock_mass,
        queue_id="q1",
        items=[_queue_item(item_id="racy")],
    )
    mock_mass.player_queues.delete_item = MagicMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_remove_item",
            {"queue_id": "q1", "item_ids": ["racy"]},
        )
    assert result.data.removed == []
    assert result.data.skipped_buffered == ["racy"]
    mock_mass.player_queues.delete_item.assert_called_once_with("q1", "racy")


async def test_remove_item_requires_ids(mounted_queue: FastMCP) -> None:
    """An empty item_ids list raises ToolError."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="item_id"):
            await client.call_tool("queue_remove_item", {"queue_id": "q1", "item_ids": []})


async def test_remove_item_raises_tool_error_on_unknown_queue(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Unknown queue_id surfaces as ToolError before any delete."""
    mock_mass.player_queues.get = MagicMock(return_value=None)
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="Queue 'q-missing' not found"):
            await client.call_tool(
                "queue_remove_item",
                {"queue_id": "q-missing", "item_ids": ["abc"]},
            )
