"""Shared helpers for tool sub-servers."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError

from ..models import (
    AlbumBrief,
    ArtistBrief,
    PlayerBrief,
    PlaylistBrief,
    QueueBrief,
    QueueItemBrief,
    RadioBrief,
    TrackBrief,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastmcp import Context

MAX_PAGE = 200
DEFAULT_PAGE = 50

# Per-tool execution timeouts (seconds), used in @sub.tool(timeout=…). Long
# searches and recommendation fetches reach external music providers; transport
# controls are local-RPC-fast; bulk playlist edits are explicitly larger.
TIMEOUT_FAST = 10.0
TIMEOUT_MUTATION = 15.0
TIMEOUT_QUERY = 30.0
TIMEOUT_BULK = 60.0


async def confirm_or_raise(ctx: Context | None, prompt: str, *, enabled: bool) -> None:
    """Ask the MCP client to confirm a destructive operation.

    If ``enabled`` is False, or there is no Context (direct unit-test
    invocation), or the client returns ``NotImplementedError`` (no elicit
    support), the call passes through silently — the permission flag is
    still in effect as the primary defense.

    On user decline / cancel, raises ``ToolError`` so the SDK reports it as
    a tool-execution error (``isError: true``) rather than a protocol error.
    """
    if not enabled or ctx is None:
        return
    try:
        # ctx.elicit's overloads in older mypy stubs don't recognize ``bool``
        # as a valid scalar response_type — runtime behaviour is fine. Newer
        # upstream mypy resolves the overload correctly, so the unused-ignore
        # is also suppressed.
        result = await ctx.elicit(prompt, response_type=bool)  # type: ignore[arg-type, unused-ignore]
    except NotImplementedError:
        return
    action = getattr(result, "action", None)
    data = getattr(result, "data", None)
    if action != "accept" or not data:
        msg = "Operation cancelled by user"
        raise ToolError(msg)


def page_args(offset: int = 0, limit: int = DEFAULT_PAGE) -> tuple[int, int]:
    """Clamp paging arguments to safe bounds."""
    safe_limit = max(1, min(int(limit), MAX_PAGE))
    safe_offset = max(0, int(offset))
    return safe_offset, safe_limit


def to_brief_track(track: Any) -> TrackBrief:
    """Convert a ``music_assistant_models.Track`` (or compatible) to ``TrackBrief``."""
    artists = _names(getattr(track, "artists", None))
    album = _name(getattr(track, "album", None))
    return TrackBrief(
        uri=str(getattr(track, "uri", "")),
        name=str(getattr(track, "name", "")),
        artists=artists,
        album=album,
        duration=_int(getattr(track, "duration", None)),
    )


def to_brief_album(album: Any) -> AlbumBrief:
    """Convert an Album-like object to ``AlbumBrief``."""
    artist = _name(getattr(album, "artist", None))
    if artist is None:
        artists = _names(getattr(album, "artists", None))
        artist = artists[0] if artists else None
    return AlbumBrief(
        uri=str(getattr(album, "uri", "")),
        name=str(getattr(album, "name", "")),
        artist=artist,
        year=_int(getattr(album, "year", None)),
    )


def to_brief_artist(artist: Any) -> ArtistBrief:
    """Convert an Artist-like object to ``ArtistBrief``."""
    return ArtistBrief(
        uri=str(getattr(artist, "uri", "")),
        name=str(getattr(artist, "name", "")),
    )


def to_brief_playlist(playlist: Any) -> PlaylistBrief:
    """Convert a Playlist-like object to ``PlaylistBrief``."""
    return PlaylistBrief(
        uri=str(getattr(playlist, "uri", "")),
        name=str(getattr(playlist, "name", "")),
        track_count=_int(getattr(playlist, "track_count", None)),
        owner=_name(getattr(playlist, "owner", None)),
    )


def to_brief_radio(radio: Any) -> RadioBrief:
    """Convert a Radio-like object to ``RadioBrief``."""
    return RadioBrief(
        uri=str(getattr(radio, "uri", "")),
        name=str(getattr(radio, "name", "")),
        description=_str_or_none(getattr(radio, "description", None)),
    )


def to_brief_player(player: Any) -> PlayerBrief:
    """Convert a Player-like object to ``PlayerBrief``."""
    state = getattr(player, "state", None)
    state_value = str(getattr(state, "value", state)) if state is not None else "unknown"
    return PlayerBrief(
        player_id=str(getattr(player, "player_id", "")),
        name=str(getattr(player, "display_name", None) or getattr(player, "name", "")),
        state=state_value,
        volume_level=_int(getattr(player, "volume_level", None)),
        powered=bool(getattr(player, "powered", True)),
        current_item=_str_or_none(getattr(player, "current_media", None)),
    )


def to_brief_queue(queue: Any, items: Sequence[Any] | None = None) -> QueueBrief:
    """Convert a PlayerQueue-like object to ``QueueBrief``.

    :param queue: queue-like object with ``queue_id``, ``current_index``, etc.
    :param items: optional iterable of queue items to include.
    """
    repeat_mode = getattr(queue, "repeat_mode", None)
    repeat_value = str(getattr(repeat_mode, "value", repeat_mode)) if repeat_mode else "off"
    brief_items: list[QueueItemBrief] = []
    if items:
        for it in items:
            brief_items.append(
                QueueItemBrief(
                    item_id=str(getattr(it, "queue_item_id", "")),
                    name=str(getattr(it, "name", "")),
                    duration=_int(getattr(it, "duration", None)),
                    artists=_names(getattr(getattr(it, "media_item", None), "artists", None)),
                )
            )
    # In the canonical MA model PlayerQueue.items is an int (total queue
    # length), not a list. Fall back to alternate field names for older builds,
    # and only as a last resort to len(brief_items) — which would under-report
    # the real length, since `brief_items` is the truncated lookahead from
    # get_active_queue, not the full queue.
    raw_total = getattr(queue, "items", None)
    explicit_count = _int(raw_total) if isinstance(raw_total, int) else None
    if explicit_count is None:
        explicit_count = _int(
            getattr(queue, "items_count", None) or getattr(queue, "items_total", None)
        )
    return QueueBrief(
        queue_id=str(getattr(queue, "queue_id", "")),
        current_index=_int(getattr(queue, "current_index", None)),
        item_count=explicit_count if explicit_count is not None else len(brief_items),
        shuffle=bool(getattr(queue, "shuffle_enabled", False)),
        repeat=repeat_value,
        items=brief_items,
    )


# ── private helpers ──────────────────────────────────────────────────────────


def _names(items: Any) -> list[str]:
    if not items:
        return []
    return [str(getattr(i, "name", i)) for i in items]


def _name(item: Any) -> str | None:
    if item is None:
        return None
    return str(getattr(item, "name", item))


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
