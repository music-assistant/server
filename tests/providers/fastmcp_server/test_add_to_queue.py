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
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import MediaType, QueueOption

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


def _mock_queue(
    *, current_index: int | None = 0, index_in_buffer: int | None = None, items: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id="q1",
        current_index=current_index,
        index_in_buffer=index_in_buffer,
        items=items,
    )


def _mock_track(*, uri: str = "spotify://track/1", name: str = "Track One") -> SimpleNamespace:
    return SimpleNamespace(
        uri=uri,
        name=name,
        available=True,
        media_type=MediaType.TRACK,
        provider="spotify",
        item_id="1",
        image=None,
        duration=180,
    )


def _setup_index_add_mocks(
    mock_mass: MagicMock,
    *,
    uri: str,
    before: list[SimpleNamespace],
    after: list[SimpleNamespace],
    current_index: int = 1,
    index_in_buffer: int | None = None,
    items_count: int | None = None,
) -> None:
    mock_mass.player_queues.get.return_value = _mock_queue(
        current_index=current_index,
        index_in_buffer=index_in_buffer,
        items=items_count if items_count is not None else len(before),
    )
    # Index path fetches the lookahead window before and after the load.
    mock_mass.player_queues.items = MagicMock(side_effect=[before, after])
    mock_mass.music.get_item_by_uri = AsyncMock(return_value=_mock_track(uri=uri))
    mock_mass.player_queues._media_resolver._resolve_media_items = AsyncMock(
        return_value=[_mock_track(uri=uri)]
    )
    mock_mass.player_queues.load = AsyncMock()


async def test_add_to_queue_index_calls_load(mounted_queue: FastMCP, mock_mass: MagicMock) -> None:
    """Index path inserts via load() without calling play_media."""
    uri = "spotify://track/new"
    before = [_queue_item(item_id=f"i{i}", uri=f"u{i}", name=f"n{i}") for i in range(5)]
    after = [
        *before[:3],
        _queue_item(item_id="new-row", uri=uri, name="Track One"),
        *before[3:],
    ]
    _setup_index_add_mocks(mock_mass, uri=uri, before=before, after=after, current_index=1)
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "index": 3}
        )
    mock_mass.player_queues.play_media.assert_not_called()
    mock_mass.player_queues.load.assert_awaited_once()
    _, kwargs = mock_mass.player_queues.load.call_args
    assert kwargs["insert_at_index"] == 3
    assert kwargs["shuffle"] is False
    assert result.data.index == 3
    assert result.data.item_id == "new-row"


async def test_add_to_queue_index_resolves_via_media_resolver(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """
    Index path resolves media through the controller's MediaResolver.

    Regression (issue #159): the private ``_resolve_media_items`` moved from
    ``PlayerQueuesController`` onto an internal ``_media_resolver`` upstream, so
    calling it on the controller directly raises ``AttributeError`` at runtime.
    """
    uri = "spotify://track/new"
    before = [_queue_item(item_id=f"i{i}", uri=f"u{i}", name=f"n{i}") for i in range(5)]
    after = [*before[:3], _queue_item(item_id="new-row", uri=uri, name="Track One"), *before[3:]]
    mock_mass.player_queues.get.return_value = _mock_queue(current_index=1, items=len(before))
    mock_mass.player_queues.items = MagicMock(side_effect=[before, after])
    mock_mass.music.get_item_by_uri = AsyncMock(return_value=_mock_track(uri=uri))
    # The resolver lives on the controller's MediaResolver, not the controller.
    mock_mass.player_queues._media_resolver._resolve_media_items = AsyncMock(
        return_value=[_mock_track(uri=uri)]
    )
    mock_mass.player_queues.load = AsyncMock()
    async with Client(mounted_queue) as client:
        await client.call_tool("queue_add_to_queue", {"queue_id": "q1", "uri": uri, "index": 3})
    mock_mass.player_queues._media_resolver._resolve_media_items.assert_awaited_once()


async def test_add_to_queue_index_rejects_past_current(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Index before the next insertable position raises ToolError."""
    mock_mass.player_queues.get.return_value = _mock_queue(current_index=2)
    mock_mass.player_queues.items = MagicMock(
        return_value=[_queue_item(item_id="i0", uri="u", name="n")] * 5
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="insertable position"):
            await client.call_tool(
                "queue_add_to_queue",
                {"queue_id": "q1", "uri": "spotify://track/1", "index": 1},
            )


async def test_add_to_queue_index_rejects_buffered(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Index at or before the buffered row raises ToolError."""
    mock_mass.player_queues.get.return_value = _mock_queue(current_index=1, index_in_buffer=2)
    mock_mass.player_queues.items = MagicMock(
        return_value=[_queue_item(item_id="i0", uri="u", name="n")] * 5
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="index_in_buffer=2"):
            await client.call_tool(
                "queue_add_to_queue",
                {"queue_id": "q1", "uri": "spotify://track/1", "index": 2},
            )


async def test_add_to_queue_index_rejects_replace_combo(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """replace/replace_next cannot be combined with index."""
    mock_mass.player_queues.get.return_value = _mock_queue(current_index=0)
    mock_mass.player_queues.items = MagicMock(
        return_value=[_queue_item(item_id="i0", uri="u", name="n")] * 3
    )
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="cannot be combined"):
            await client.call_tool(
                "queue_add_to_queue",
                {
                    "queue_id": "q1",
                    "uri": "spotify://track/1",
                    "index": 2,
                    "option": "replace",
                },
            )


async def test_add_to_queue_index_returns_ack_with_index(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Successful index insert returns AddToQueueResult with index set."""
    uri = "spotify://track/new"
    before = [_queue_item(item_id=f"i{i}", uri=f"u{i}", name=f"n{i}") for i in range(4)]
    after = [
        *before[:2],
        _queue_item(item_id="inserted", uri=uri, name="Track One"),
        *before[2:],
    ]
    _setup_index_add_mocks(mock_mass, uri=uri, before=before, after=after, current_index=0)
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "index": 2}
        )
    assert result.data.index == 2
    assert result.data.item_id == "inserted"
    assert result.data.uri == uri


async def test_add_to_queue_index_expands_album(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """Album URIs expand to multiple rows in a single load() call."""
    album_uri = "library://album/203"
    track_a = _mock_track(uri="library://track/206", name="One Week")
    track_b = _mock_track(uri="library://track/207", name="It's All Been Done")
    before = [_queue_item(item_id="playing", uri="library://track/93", name="Adrift")]
    after = [
        *before,
        _queue_item(item_id="stunt-1", uri="library://track/206", name="One Week"),
        _queue_item(item_id="stunt-2", uri="library://track/207", name="It's All Been Done"),
    ]
    mock_mass.player_queues.get.return_value = _mock_queue(current_index=0, items=len(before))
    mock_mass.player_queues.items = MagicMock(side_effect=[before, after])
    mock_mass.music.get_item_by_uri = AsyncMock(
        return_value=_mock_track(uri=album_uri, name="Stunt")
    )
    mock_mass.player_queues._media_resolver._resolve_media_items = AsyncMock(
        return_value=[track_a, track_b]
    )
    mock_mass.player_queues.load = AsyncMock()
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue",
            {"queue_id": "q1", "uri": album_uri, "index": 1},
        )
    queue_items = mock_mass.player_queues.load.call_args[0][1]
    assert len(queue_items) == 2
    assert result.data.item_id == "stunt-1"
    assert result.data.index == 1


async def test_add_to_queue_add_tracks_growing_tail_window(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """
    option=add recomputes the tail window from the post-add total (regression: #1).

    For a queue longer than MAX_QUEUE_ITEMS the appended row lands beyond the
    pre-add window; the after-window offset must follow the new total or the
    successful add is wrongly reported as 'could not locate the new row'.
    """
    uri = "spotify://track/1"
    mock_mass.player_queues.get = MagicMock(
        side_effect=[
            SimpleNamespace(items=501, current_index=0, queue_id="q1"),
            SimpleNamespace(items=502, current_index=0, queue_id="q1"),
        ]
    )
    mock_mass.player_queues.items = MagicMock(
        side_effect=[[], [_queue_item(item_id="tail-new", uri=uri, name="Tail Track")]]
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "option": "add"}
        )
    assert result.data.item_id == "tail-new"
    # before-window offset from total 501, after-window offset from total 502.
    assert mock_mass.player_queues.items.call_args_list[0].kwargs["offset"] == 1
    assert mock_mass.player_queues.items.call_args_list[1].kwargs["offset"] == 2


async def test_add_to_queue_index_count_uses_queue_total_not_page(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """
    A valid index past the 500-row page cap is accepted (regression: #2).

    item_count must come from the queue's own row count, not len() of a capped
    items() page, or inserts beyond index 500 are wrongly rejected.
    """
    uri = "spotify://track/new"
    before = [_queue_item(item_id="i0", uri="u0", name="n0")]
    after = [*before, _queue_item(item_id="deep-row", uri=uri, name="Deep Track")]
    _setup_index_add_mocks(
        mock_mass, uri=uri, before=before, after=after, current_index=0, items_count=1000
    )
    async with Client(mounted_queue) as client:
        result = await client.call_tool(
            "queue_add_to_queue", {"queue_id": "q1", "uri": uri, "index": 600}
        )
    assert result.data.index == 600
    assert result.data.item_id == "deep-row"


async def test_add_to_queue_index_rejects_invalid_option(
    mounted_queue: FastMCP, mock_mass: MagicMock
) -> None:
    """An invalid option is rejected even when index is set (regression: #3)."""
    mock_mass.player_queues.get.return_value = _mock_queue(current_index=0, items=5)
    async with Client(mounted_queue) as client:
        with pytest.raises(ToolError, match="Invalid option"):
            await client.call_tool(
                "queue_add_to_queue",
                {"queue_id": "q1", "uri": "spotify://track/1", "option": "bogus", "index": 2},
            )
