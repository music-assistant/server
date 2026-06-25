"""
Tests for the add_to_queue MCP tool.

Validates that:
1. Valid option values (add/next/play/replace/replace_next) are accepted and forwarded to MA.
2. Invalid option values raise a clean ToolError.
3. A successful add returns AddToQueueResult with item_id, uri, name, and option.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import QueueOption

from music_assistant.providers.fastmcp_server.tools.queue import build_queue_server


@pytest.fixture
def mounted_queue(mock_mass: Any) -> FastMCP:
    """Build a root FastMCP with the queue sub-server mounted."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    return mcp


@pytest.fixture
def mounted_queue_no_delete(mock_mass: Any) -> FastMCP:
    """Queue server with delete:queue permission disabled."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass, delete_queue_enabled=False), namespace="queue")
    return mcp


def _queue_item(*, item_id: str, uri: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(queue_item_id=item_id, uri=uri, name=name, media_item=None)


def _mock_items_before_after(
    mock_mass: MagicMock, *, before: list[SimpleNamespace], after: list[SimpleNamespace]
) -> None:
    mock_mass.player_queues.items = MagicMock(side_effect=[before, after])


async def test_add_to_queue_accepts_valid_options(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Each valid option value is accepted and forwarded to MA."""
    uri = "spotify://track/1"
    for opt in ("add", "next", "play", "replace", "replace_next"):
        mock_mass.player_queues.play_media.reset_mock()
        _mock_items_before_after(
            mock_mass,
            before=[],
            after=[_queue_item(item_id=f"item-{opt}", uri=uri, name="Track One")],
        )
        async with Client(mounted_queue) as client:
            result = await client.call_tool(
                "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "option": opt}
            )
        mock_mass.player_queues.play_media.assert_awaited_once_with(
            "q1", uri, option=QueueOption(opt)
        )
        assert result.data.item_id == f"item-{opt}"
        assert result.data.uri == uri
        assert result.data.option == opt


async def test_add_to_queue_rejects_invalid_option(mounted_queue: FastMCP) -> None:
    """Invalid option raises ToolError with the list of valid options."""
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="bogus") as exc_info:
            await client.call_tool(
                "queue_add_to_queue",
                {"queue_id": "q1", "uri": "spotify://track/1", "option": "bogus"},
            )
    msg = str(exc_info.value)
    assert "``add``" in msg
    assert "``replace_next``" in msg


async def test_add_to_queue_defaults_to_add(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Calling add_to_queue without option defaults to 'add'."""
    uri = "spotify://track/1"
    mock_mass.player_queues.play_media.reset_mock()
    _mock_items_before_after(
        mock_mass,
        before=[],
        after=[_queue_item(item_id="item-1", uri=uri, name="Track One")],
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool("queue_add_to_queue", {"queue_id": "q1", "uri": uri})
    mock_mass.player_queues.play_media.assert_awaited_once_with("q1", uri, option=QueueOption.ADD)
    assert result.data.option == "add"
    assert result.data.name == "Track One"


async def test_add_to_queue_returns_ack_for_new_row(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Returns the newly added row when the same uri already exists elsewhere."""
    uri = "library://track/169"
    _mock_items_before_after(
        mock_mass,
        before=[_queue_item(item_id="old-dup", uri=uri, name="If I Had $1000000")],
        after=[
            _queue_item(item_id="old-dup", uri=uri, name="If I Had $1000000"),
            _queue_item(item_id="new-row", uri=uri, name="If I Had $1000000"),
        ],
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "option": "add"}
        )
    assert result.data.item_id == "new-row"
    assert result.data.uri == uri
    assert result.data.name == "If I Had $1000000"
    assert result.data.option == "add"


async def test_add_to_queue_raises_when_row_not_found(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Surfaces ToolError when play_media succeeds but the new row cannot be located."""
    mock_mass.player_queues.items = MagicMock(side_effect=[[], []])
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="could not locate"):
            await client.call_tool(
                "queue_add_to_queue",
                {"queue_id": "q1", "uri": "spotify://track/1", "option": "add"},
            )


async def test_add_to_queue_finds_album_expanded_track_by_id(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Album URIs expand to track rows — detection uses id-diff, not input URI."""
    album_uri = "library://album/42"
    track_uri = "library://track/99"
    _mock_items_before_after(
        mock_mass,
        before=[_queue_item(item_id="old-1", uri=track_uri, name="Existing")],
        after=[
            _queue_item(item_id="old-1", uri=track_uri, name="Existing"),
            _queue_item(item_id="new-album-track", uri=track_uri, name="From Album"),
        ],
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": album_uri, "option": "add"}
        )
    assert result.data.item_id == "new-album-track"
    assert result.data.uri == album_uri


async def test_add_to_queue_uses_tail_window_for_long_queues(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Queues longer than 500 items fetch the tail window to locate appended rows."""
    uri = "spotify://track/1"
    mock_mass.player_queues.get = MagicMock(
        return_value=SimpleNamespace(items=501, current_index=0, queue_id="q1")
    )
    mock_mass.player_queues.items = MagicMock(
        side_effect=[
            [],
            [_queue_item(item_id="tail-new", uri=uri, name="Tail Track")],
        ]
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "option": "add"}
        )
    assert result.data.item_id == "tail-new"
    assert mock_mass.player_queues.items.call_args_list[0].kwargs["offset"] == 1
    assert mock_mass.player_queues.items.call_args_list[1].kwargs["offset"] == 1


async def test_add_to_queue_replace_requires_delete_permission(
    mounted_queue_no_delete: FastMCP,
) -> None:
    """Replace and replace_next require delete:queue permission."""
    async with Client(mounted_queue_no_delete) as client:
        for opt in ("replace", "replace_next"):
            with pytest.raises(ToolError, match="delete:queue"):
                await client.call_tool(
                    "queue_add_to_queue",
                    {"queue_id": "q1", "uri": "spotify://track/1", "option": opt},
                )
