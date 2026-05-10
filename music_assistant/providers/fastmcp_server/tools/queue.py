"""Queue: read state and edit / delete queue items."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from ..models import QueueBrief
from ..tags import Tag
from ._common import TIMEOUT_FAST, TIMEOUT_MUTATION, confirm_or_raise, to_brief_queue

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def build_queue_server(mass: MusicAssistant, *, require_confirmation: bool = True) -> FastMCP:
    """Construct the ``queue/*`` sub-server."""
    sub: FastMCP = FastMCP(name="queue")

    @sub.tool(
        tags={Tag.QUERY_QUEUE},
        annotations=ToolAnnotations(
            title="Get active queue",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_active_queue(player_id: str, include_items: int = 25) -> QueueBrief | None:
        """Return the active queue for a player, or ``None`` if the player is idle."""
        queue = mass.player_queues.get_active_queue(player_id)
        if queue is None:
            return None
        raw = mass.player_queues.items(queue.queue_id)
        items = list(raw)[: max(0, include_items)] if include_items > 0 else []
        return to_brief_queue(queue, items=items)

    @sub.tool(
        tags={Tag.EDIT_QUEUE},
        annotations=ToolAnnotations(
            title="Toggle queue shuffle",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def set_shuffle(queue_id: str, enabled: bool) -> None:
        """Enable or disable shuffle on the given queue."""
        await mass.player_queues.set_shuffle(queue_id, enabled)

    @sub.tool(
        tags={Tag.DELETE_QUEUE},
        annotations=ToolAnnotations(
            title="Clear queue",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def clear_queue(queue_id: str, ctx: Context | None = None) -> None:
        """Clear all items from the given queue.

        Implementation note: MA's ``player_queues`` exposes a ``clear`` method;
        if the API name diverges, this is the single integration point to fix.
        """
        await confirm_or_raise(
            ctx,
            f"Clear all items from queue {queue_id!r}? This cannot be undone.",
            enabled=require_confirmation,
        )
        clear = getattr(mass.player_queues, "clear", None)
        if clear is None:
            msg = "mass.player_queues.clear is not available on this MA build"
            raise RuntimeError(msg)
        clear(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=ToolAnnotations(
            title="Transfer queue between players",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def transfer_queue(source_queue_id: str, target_queue_id: str) -> None:
        """Move a queue from one player to another."""
        await mass.player_queues.transfer_queue(source_queue_id, target_queue_id)

    return sub
