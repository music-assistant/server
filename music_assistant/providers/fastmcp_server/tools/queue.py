"""Queue: read state and edit / delete queue items."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from music_assistant_models.enums import QueueOption, RepeatMode
from music_assistant_models.errors import InvalidDataError, MusicAssistantError

from music_assistant.controllers.player_queues.helpers import build_queue_item

from ..models import AddToQueueResult, QueueBrief, RemoveFromQueueResult
from ..tags import Tag
from ._common import (
    TIMEOUT_FAST,
    TIMEOUT_MUTATION,
    TIMEOUT_QUERY,
    confirm_or_raise,
    min_insert_index,
    queue_item_display_name,
    resolve_added_queue_item,
    to_brief_queue,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import PlayableMediaItemType

    from music_assistant.mass import MusicAssistant

# Matches MA's default queue page size (and the ``queue://`` resource cap).
MAX_QUEUE_ITEMS = 500


def _queue_items_window_offset(queue: object | None, queue_option: QueueOption) -> int:
    """Return the ``items()`` offset for locating newly added rows in long queues."""
    if queue is None:
        return 0
    total = int(getattr(queue, "items", 0) or 0)
    current_index = int(getattr(queue, "current_index", 0) or 0)
    if queue_option is QueueOption.ADD:
        return max(0, total - MAX_QUEUE_ITEMS)
    if queue_option is QueueOption.REPLACE:
        return 0
    return max(0, current_index)


def _items_window_offset_for_index(index: int) -> int:
    """Return the ``items()`` offset that centers a window on ``index``."""
    return max(0, index - MAX_QUEUE_ITEMS // 2)


def _require_queue(mass: MusicAssistant, queue_id: str) -> None:
    """Raise ``ToolError`` when ``queue_id`` does not resolve to a queue."""
    if mass.player_queues.get(queue_id) is None:
        raise ToolError(f"Queue {queue_id!r} not found.")


async def _add_to_queue_at_index(
    mass: MusicAssistant,
    queue_id: str,
    uri: str,
    option: str,
    index: int,
) -> AddToQueueResult:
    """
    Insert media at an absolute 0-based queue index without interrupting playback.

    :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
    :param uri: Music Assistant URI of the media to insert.
    :param option: Original placement option, echoed back in the result.
    :param index: Absolute 0-based insertion position.
    """
    queue = mass.player_queues.get(queue_id)
    if queue is None:
        raise ToolError(f"Queue {queue_id!r} not found.")
    min_insert = min_insert_index(queue)
    # The queue's own row count — items() pages are capped at MAX_QUEUE_ITEMS,
    # so len() of a page would wrongly reject valid inserts past that cap.
    item_count = int(getattr(queue, "items", 0) or 0)
    if index < min_insert:
        cur = getattr(queue, "current_index", None)
        buf = getattr(queue, "index_in_buffer", None)
        raise ToolError(
            f"Index {index} is before the next insertable position ({min_insert}). "
            f"current_index={cur!r}, index_in_buffer={buf!r}. "
            f"Re-call get_active_queue and use index >= next_insertable_index."
        )
    if index > item_count:
        raise ToolError(
            f"Index {index} is out of range for queue {queue_id!r} "
            f"(item_count={item_count}, valid range {min_insert}..{item_count})."
        )
    offset = _items_window_offset_for_index(index)
    before_items = mass.player_queues.items(queue_id, limit=MAX_QUEUE_ITEMS, offset=offset)
    before_item_ids = frozenset(str(getattr(it, "queue_item_id", "")) for it in before_items)
    try:
        media_item = await mass.music.get_item_by_uri(uri)
    except MusicAssistantError as err:
        raise ToolError(str(err)) from err
    try:
        # `_resolve_media_items` is a private MA method with no public equivalent.
        # It moved from PlayerQueuesController onto an internal MediaResolver
        # upstream, so resolve the host object across both MA layouts.
        resolver: Any = getattr(mass.player_queues, "_media_resolver", mass.player_queues)
        resolved = await resolver._resolve_media_items(media_item, queue_id=queue_id)
    except InvalidDataError as err:
        raise ToolError(str(err)) from err
    queue_items = [
        build_queue_item(queue_id, cast("PlayableMediaItemType", x))
        for x in resolved
        if x and getattr(x, "available", True)
    ]
    if not queue_items:
        raise ToolError("No playable items found")
    await mass.player_queues.load(
        queue_id,
        queue_items,
        insert_at_index=index,
        keep_remaining=True,
        keep_played=True,
        shuffle=False,
    )
    after_items = mass.player_queues.items(queue_id, limit=MAX_QUEUE_ITEMS, offset=offset)
    # A container URI (album / playlist) expands to per-track rows, so match on
    # the resolved track URIs as well as the requested URI.
    resolved_uris = frozenset(
        str(getattr(x, "uri", "")) for x in resolved if getattr(x, "uri", None)
    )
    added = resolve_added_queue_item(
        after_items, uris=frozenset({uri}) | resolved_uris, before_item_ids=before_item_ids
    )
    if added is None:
        raise ToolError(
            f"Added {uri!r} to queue {queue_id!r} at index {index} "
            "but could not locate the new queue row."
        )
    return AddToQueueResult(
        item_id=str(getattr(added, "queue_item_id", "")),
        uri=uri,
        name=queue_item_display_name(added),
        option=option,
        index=index,
    )


def build_queue_server(  # noqa: PLR0915 -- one sub-server registers all queue tools
    mass: MusicAssistant,
    *,
    require_confirmation: bool = True,
    delete_queue_enabled: bool = True,
) -> FastMCP:
    """Construct the ``queue/*`` sub-server."""
    sub: FastMCP = FastMCP(name="queue")

    def _queue_brief(queue_id: str, include_items: int) -> QueueBrief:
        queue = mass.player_queues.get(queue_id)
        if queue is None:
            raise ToolError(f"Queue {queue_id!r} not found after move.")
        limit = min(max(include_items, 0), MAX_QUEUE_ITEMS)
        items = mass.player_queues.items(queue.queue_id, limit=limit, offset=0) if limit > 0 else []
        return to_brief_queue(queue, items=list(items))

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
    async def get_active_queue(
        player_id: str = "",
        include_items: int = 25,
        items_from_current: bool = False,
        queue_id: str = "",
    ) -> QueueBrief | None:
        """
        Return the active queue for a player, or ``None`` if the player is idle.

        Returns ``QueueBrief`` with ``queue_id``, ``current_index``,
        ``index_in_buffer``, ``next_insertable_index``, ``item_count``,
        shuffle / repeat flags, ``available`` and up to ``include_items``
        queue ``items``. Each ``QueueItemBrief`` includes its absolute ``index``.

        Use ``next_insertable_index`` when choosing ``add_to_queue(index=…)`` —
        indices at or before ``index_in_buffer`` are already played or buffered
        and cannot receive new rows. ``current_index`` is the now-playing row.

        By default ``items`` are fetched from the start of the queue (offset 0),
        which suits bulk inspect / remove workflows. Set ``items_from_current=True``
        to fetch a lookahead window from the current playback position instead.

        ``QueueBrief.queue_id`` is the identifier the mutation tools
        (``set_shuffle``, ``set_repeat``, ``add_to_queue``, ``remove_item``,
        ``move_item``, ``move_item_to_end``, ``clear_queue``,
        ``transfer_queue``) expect; for a standard player-backed queue that
        value equals ``PlayerBrief.player_id``. For a queue fed by an external
        plugin source (Connect / AirPlay / Ynison), the current item's ``name``
        is the real track title rather than the source wrapper name.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        :param include_items: How many items to materialise. Clamped to the
            ``[0, 500]`` range — 500 matches MA's own queue page size and the
            ``queue://`` resource cap, preventing a hostile or sloppy client from
            forcing the server to load thousands of rows on every call.
        :param items_from_current: When ``True``, fetch ``items`` from
            ``current_index`` rather than the queue start. ``items_start_index``
            in the response reflects the offset used.
        :param queue_id: Convenience alias for ``player_id`` when an agent
            passes the queue identifier instead. Supply one of the two;
            ignored when ``player_id`` is given.
        """
        target = player_id or queue_id
        if not target:
            raise ToolError("Provide player_id (from PlayerBrief.player_id) or queue_id.")
        queue = mass.player_queues.get_active_queue(target)
        if queue is None:
            return None
        limit = min(max(include_items, 0), MAX_QUEUE_ITEMS)
        current_index = getattr(queue, "current_index", None)
        items_offset = max(0, int(current_index or 0)) if items_from_current else 0
        if limit > 0:
            items = mass.player_queues.items(queue.queue_id, limit=limit, offset=items_offset)
        else:
            items = []
        return to_brief_queue(queue, items=list(items), items_offset=items_offset)

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
        tags={Tag.EDIT_QUEUE},
        annotations=ToolAnnotations(
            title="Set queue repeat mode",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def set_repeat(queue_id: str, repeat_mode: str = "off") -> None:
        """
        Set the repeat mode for the given queue.

        Setting the current value again is a no-op. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id`` (distinct
            from ``PlayerBrief.player_id``).
        :param repeat_mode: Repeat mode:

            - ``off`` (default): No repeating.
            - ``one``: Repeat the current track.
            - ``all``: Repeat the entire queue.
        """
        # RepeatMode._missing_ silently falls back to UNKNOWN for invalid values
        # instead of raising ValueError, so we must validate explicitly.
        mode = RepeatMode(repeat_mode.lower())
        if mode is RepeatMode.UNKNOWN:
            valid = ", ".join(f"``{e.value}``" for e in RepeatMode if e is not RepeatMode.UNKNOWN)
            raise ToolError(f"Invalid repeat_mode {repeat_mode!r}. Valid options: {valid}")

        await mass.player_queues.set_repeat(queue_id, mode)

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
    ) -> RemoveFromQueueResult:
        """
        Remove one or more **up-next** items from a queue by ``item_id``.

        Call ``get_active_queue`` first to list items and their stable
        ``item_id`` values, then pass all ids in a single call rather than
        removing one at a time.

        Only rows after the current playback position are deleted. Every
        requested id is acknowledged in exactly one ``RemoveFromQueueResult``
        bucket: ``removed`` (verified deleted), ``skipped_played`` (at or
        before the now-playing row), ``skipped_buffered`` (already loaded in
        the player's audio buffer), or ``not_found`` (unknown or stale id).
        A stale id never aborts the batch, so rows deleted earlier in the
        call are always reported.

        When ``Confirm destructive operations`` is enabled the client is
        asked to confirm before any item is removed.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param item_ids: ``item_id`` values from ``QueueItemBrief`` returned
            by ``get_active_queue``. At least one id is required.
        """
        if not item_ids:
            raise ToolError(
                "Provide at least one item_id from QueueBrief.items[].item_id "
                "(use get_active_queue first)."
            )
        _require_queue(mass, queue_id)
        await confirm_or_raise(
            ctx,
            f"Remove {len(item_ids)} item(s) from queue {queue_id!r}? This cannot be undone.",
            enabled=require_confirmation,
        )
        queue = mass.player_queues.get(queue_id)
        current_index = getattr(queue, "current_index", None) if queue else None
        index_in_buffer = getattr(queue, "index_in_buffer", None) if queue else None
        result = RemoveFromQueueResult()
        for item_id in item_ids:
            item_index = mass.player_queues.index_by_id(queue_id, item_id)
            if item_index is None:
                result.not_found.append(item_id)
                continue
            # Played first: MA keeps index_in_buffer >= current_index, so the
            # buffer check would otherwise swallow every history row.
            if current_index is not None and item_index <= current_index:
                result.skipped_played.append(item_id)
                continue
            if index_in_buffer is not None and item_index <= index_in_buffer:
                result.skipped_buffered.append(item_id)
                continue
            try:
                mass.player_queues.delete_item(queue_id, item_id)
            except KeyError, InvalidDataError:
                # Raced with another client between resolve and delete.
                result.not_found.append(item_id)
                continue
            # MA silently ignores deletes of rows already in the player
            # buffer, so verify the row is gone before claiming "removed".
            if mass.player_queues.index_by_id(queue_id, item_id) is None:
                result.removed.append(item_id)
            else:
                result.skipped_buffered.append(item_id)
        return result

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
    async def move_item(
        queue_id: str, item_id: str, pos_shift: int = 1, include_items: int = 25
    ) -> QueueBrief:
        """
        Move an existing queue row up, down, or to play next.

        Call ``get_active_queue`` first for ``item_id`` values. The currently
        playing or buffered item cannot be moved. Returns the reordered
        ``QueueBrief`` so the new order can be confirmed without a separate
        ``get_active_queue`` call.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param item_id: ``item_id`` from ``QueueItemBrief`` returned by
            ``get_active_queue``.
        :param pos_shift: Relative move — ``-1`` up one slot, ``+1`` down one
            slot (default), ``0`` to insert after the currently playing item
            (play next).
        :param include_items: How many items to materialise in the returned
            brief. Clamped to the ``[0, 500]`` range.
        """
        _require_queue(mass, queue_id)
        try:
            mass.player_queues.move_item(queue_id, item_id, pos_shift)
        except (KeyError, IndexError, InvalidDataError) as exc:
            raise ToolError(str(exc)) from exc
        return _queue_brief(queue_id, include_items)

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
    async def move_item_to_end(queue_id: str, item_id: str, include_items: int = 25) -> QueueBrief:
        """
        Move an existing queue row to the back of the queue.

        Call ``get_active_queue`` first for ``item_id`` values. The currently
        playing or buffered item cannot be moved. Returns the reordered
        ``QueueBrief`` so the new order can be confirmed without a separate
        ``get_active_queue`` call.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param item_id: ``item_id`` from ``QueueItemBrief`` returned by
            ``get_active_queue``.
        :param include_items: How many items to materialise in the returned
            brief. Clamped to the ``[0, 500]`` range.
        """
        _require_queue(mass, queue_id)
        try:
            mass.player_queues.move_item_end(queue_id, item_id)
        except (KeyError, IndexError, InvalidDataError) as exc:
            raise ToolError(str(exc)) from exc
        return _queue_brief(queue_id, include_items)

    @sub.tool(
        tags={Tag.EDIT_QUEUE},
        annotations=ToolAnnotations(
            title="Add media to queue",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def add_to_queue(
        queue_id: str,
        uri: str,
        option: str = "add",
        index: int | None = None,
    ) -> AddToQueueResult:
        """
        Enqueue media on a queue with an explicit placement mode.

        Supports different enqueue modes to control where items are placed
        and whether playback is affected. When ``index`` is provided it
        overrides ``option`` placement and inserts at that absolute 0-based
        queue position without interrupting playback.

        Call ``get_active_queue(include_items=…)`` first to inspect row order,
        ``next_insertable_index``, and per-item ``index`` when choosing ``index``.
        For play-next placement only, ``option=next`` is simpler than computing
        an index. Read ``next_insertable_index`` from ``get_active_queue`` before
        setting ``index``; do not insert at or before ``index_in_buffer``.

        Returns ``AddToQueueResult`` with the new row's ``item_id``, ``uri``,
        ``name``, and ``option`` so callers can confirm the add succeeded
        before enqueueing the next item. When ``index`` was used, ``index``
        in the result echoes the insertion position.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id`` (distinct
            from ``PlayerBrief.player_id``).
        :param uri: Music Assistant URI of the media to add, of the form
            ``<provider>://<media_type>/<id>`` (e.g. as found on
            ``TrackBrief.uri`` / ``AlbumBrief.uri`` / ``PlaylistBrief.uri``).
        :param option: Enqueue mode controlling placement and playback when
            ``index`` is omitted:

            - ``add`` (default): Append to the end of the queue without
              interrupting the current item. Preferred for "add to queue"
              requests — unlike ``playback_play_media``, this keeps what is
              already playing.
            - ``next``: Insert after the currently playing item (plays next).
            - ``play``: Insert after current item and start playing immediately.
            - ``replace_next``: Replace all items after the current one.
            - ``replace``: Clear the queue and replace with the new media.
        :param index: Optional 0-based absolute queue index. When set, overrides
            ``option`` and inserts without starting playback. Must be at or after
            the next insertable position (after the current and buffered rows).
            Valid range is ``min_insert .. item_count`` inclusive.
        """
        # QueueOption._missing_ silently falls back to UNKNOWN for invalid values
        # instead of raising ValueError, so validate explicitly — for the index
        # path too, where an unvalidated option would otherwise be echoed back.
        queue_option = QueueOption(option)
        if queue_option is QueueOption.UNKNOWN:
            valid = ", ".join(f"``{e.value}``" for e in QueueOption if e is not QueueOption.UNKNOWN)
            raise ToolError(f"Invalid option {option!r}. Valid options: {valid}")

        if index is not None:
            if queue_option in {QueueOption.REPLACE, QueueOption.REPLACE_NEXT}:
                raise ToolError(
                    "``replace`` and ``replace_next`` cannot be combined with ``index``."
                )
            return await _add_to_queue_at_index(mass, queue_id, uri, option, index)

        if (
            queue_option in {QueueOption.REPLACE, QueueOption.REPLACE_NEXT}
            and not delete_queue_enabled
        ):
            raise ToolError(
                "Option requires delete:queue permission "
                "(``replace`` and ``replace_next`` clear queue items)."
            )

        queue = mass.player_queues.get(queue_id)
        offset = _queue_items_window_offset(queue, queue_option)
        before_items = mass.player_queues.items(queue_id, limit=MAX_QUEUE_ITEMS, offset=offset)
        before_item_ids = frozenset(str(getattr(it, "queue_item_id", "")) for it in before_items)
        await mass.player_queues.play_media(queue_id, uri, option=queue_option)
        # Re-read the queue after the add: an ``add`` onto a queue longer than
        # MAX_QUEUE_ITEMS appends rows beyond the pre-add window, so recompute
        # the offset from the updated total or the new tail is missed.
        updated = mass.player_queues.get(queue_id)
        after_offset = _queue_items_window_offset(updated, queue_option)
        after_items = mass.player_queues.items(queue_id, limit=MAX_QUEUE_ITEMS, offset=after_offset)
        added = resolve_added_queue_item(
            after_items, uris=frozenset({uri}), before_item_ids=before_item_ids
        )
        if added is None:
            raise ToolError(
                f"Added {uri!r} to queue {queue_id!r} but could not locate the new queue row."
            )
        return AddToQueueResult(
            item_id=str(getattr(added, "queue_item_id", "")),
            uri=uri,
            name=queue_item_display_name(added),
            option=option,
        )

    return sub
