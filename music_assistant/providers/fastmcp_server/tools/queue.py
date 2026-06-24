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

# Matches MA's default queue page size (and the ``queue://`` resource cap).
MAX_QUEUE_ITEMS = 500


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
        """
        Return the active queue for a player, or ``None`` if the player is idle.

        Returns ``QueueBrief`` with ``queue_id``, ``current_index``,
        ``item_count``, shuffle / repeat flags, ``available`` and up to
        ``include_items`` lookahead ``items``. Note that
        ``QueueBrief.queue_id`` is the identifier the mutation tools
        (``set_shuffle``, ``clear_queue``, ``transfer_queue``) expect — it is
        distinct from ``player_id``. For a queue fed by an external plugin
        source (Connect / AirPlay / Ynison), the current item's ``name`` is
        the real track title rather than the source wrapper name.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        :param include_items: How many lookahead items to materialise. Clamped
            to the ``[0, 500]`` range — 500 matches MA's own queue page size
            and the ``queue://`` resource cap, preventing a hostile or
            sloppy client from forcing the server to load thousands of rows
            on every call.
        """
        queue = mass.player_queues.get_active_queue(player_id)
        if queue is None:
            return None
        limit = min(max(include_items, 0), MAX_QUEUE_ITEMS)
        items = mass.player_queues.items(queue.queue_id, limit=limit) if limit > 0 else []
        return to_brief_queue(queue, items=list(items))

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
        """
        Enable or disable shuffle on the given queue.

        Setting the current value again is a no-op. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id`` (distinct
            from ``PlayerBrief.player_id``).
        :param enabled: ``True`` to shuffle, ``False`` to play in queue order.
        """
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
        """
        Clear all items from the given queue. Cannot be undone.

        When ``Confirm destructive operations`` is enabled in the plugin
        settings the client is asked to confirm before the queue is cleared.
        Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        """
        await confirm_or_raise(
            ctx,
            f"Clear all items from queue {queue_id!r}? This cannot be undone.",
            enabled=require_confirmation,
        )
        mass.player_queues.clear(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=ToolAnnotations(
            title="Transfer queue between players",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def transfer_queue(source_queue_id: str, target_queue_id: str) -> None:
        """
        Move the contents and playback state of one queue onto another player.

        The source player stops playing and its queue is emptied. Returns
        nothing.

        :param source_queue_id: Queue identifier of the player currently
            holding the queue (from ``QueueBrief.queue_id``).
        :param target_queue_id: Queue identifier of the player that should
            receive the queue.
        """
        await mass.player_queues.transfer_queue(source_queue_id, target_queue_id)

    return sub
