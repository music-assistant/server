"""Queue: read state and edit / delete queue items."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from music_assistant_models.errors import InvalidDataError

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
        (``set_shuffle``, ``move_item``, ``move_item_to_end``, ``remove_item``,
        ``clear_queue``, ``transfer_queue``) expect — it is
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

    @sub.tool(
        tags={Tag.DELETE_QUEUE},
        annotations=ToolAnnotations(
            title="Remove items from queue",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def remove_item(
        queue_id: str,
        item_ids: list[str],
        ctx: Context | None = None,
    ) -> None:
        """
        Remove one or more items from a queue by ``item_id``.

        Call ``get_active_queue`` first to list items and their stable
        ``item_id`` values. Pass all ids in a single call rather than
        removing one at a time. The currently playing or buffered item
        cannot be removed — MA ignores that request.

        When ``Confirm destructive operations`` is enabled the client is
        asked to confirm before items are removed. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param item_ids: ``item_id`` values from ``QueueItemBrief`` returned
            by ``get_active_queue``. At least one id is required.
        """
        if not item_ids:
            raise ToolError(
                "Provide at least one item_id from QueueBrief.items[].item_id "
                "(use get_active_queue first)."
            )
        await confirm_or_raise(
            ctx,
            f"Remove {len(item_ids)} item(s) from queue {queue_id!r}?",
            enabled=require_confirmation,
        )
        for item_id in item_ids:
            try:
                mass.player_queues.delete_item(queue_id, item_id)
            except InvalidDataError as exc:
                raise ToolError(str(exc)) from exc

    @sub.tool(
        tags={Tag.EDIT_QUEUE},
        annotations=ToolAnnotations(
            title="Move queue item",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def move_item(queue_id: str, item_id: str, pos_shift: int = 1) -> None:
        """
        Move an existing queue row up, down, or to play next.

        Call ``get_active_queue`` first for ``item_id`` values. The currently
        playing or buffered item cannot be moved. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param item_id: ``item_id`` from ``QueueItemBrief`` returned by
            ``get_active_queue``.
        :param pos_shift: Relative move — ``-1`` up one slot, ``+1`` down one
            slot (default), ``0`` to insert after the currently playing item
            (play next).
        """
        try:
            mass.player_queues.move_item(queue_id, item_id, pos_shift)
        except (IndexError, InvalidDataError) as exc:
            raise ToolError(str(exc)) from exc

    @sub.tool(
        tags={Tag.EDIT_QUEUE},
        annotations=ToolAnnotations(
            title="Move queue item to end",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def move_item_to_end(queue_id: str, item_id: str) -> None:
        """
        Move an existing queue row to the back of the queue.

        Call ``get_active_queue`` first for ``item_id`` values. The currently
        playing or buffered item cannot be moved. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param item_id: ``item_id`` from ``QueueItemBrief`` returned by
            ``get_active_queue``.
        """
        try:
            mass.player_queues.move_item_end(queue_id, item_id)
        except (IndexError, InvalidDataError) as exc:
            raise ToolError(str(exc)) from exc

    return sub
