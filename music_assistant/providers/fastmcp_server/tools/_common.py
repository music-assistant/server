"""Shared helpers for tool sub-servers."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import dataclasses
import json
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
    # MA's :class:`Player` exposes ``playback_state`` (an enum); ``state`` is
    # only a serialisation alias and is not present on the Python object.
    # Read both so test stubs and any older shim still resolve.
    state_obj = getattr(player, "playback_state", None) or getattr(player, "state", None)
    state_value = (
        str(getattr(state_obj, "value", state_obj)) if state_obj is not None else "unknown"
    )

    # ``Player.state`` (the :class:`PlayerState` dataclass) holds the canonical
    # final values that MA serialises in its REST API — ``__final_power_state``
    # and ``__final_current_media``. The raw ``Player.powered`` /
    # ``Player.current_media`` properties read internal ``_attr_*`` caches that
    # lag (powered stays False on virtual players, current_media isn't cleared
    # on stop). Detect a PlayerState dataclass by the presence of ``powered``
    # on it; fall back to the legacy direct attributes otherwise.
    player_state = getattr(player, "state", None)
    if player_state is not None and hasattr(player_state, "powered"):
        powered_val = bool(player_state.powered) if player_state.powered is not None else True
        current_media = getattr(player_state, "current_media", None)
    else:
        powered_val = bool(getattr(player, "powered", True))
        current_media = getattr(player, "current_media", None)

    current_item: str | None = None
    if current_media is not None:
        # Prefer the human-readable title; fall back to the URI (always
        # present on ``PlayerMedia``). Avoids stringifying the whole dataclass
        # which produces noisy ``PlayerMedia(uri=…, media_type=…, …)`` blobs.
        current_item = _str_or_none(getattr(current_media, "title", None)) or _str_or_none(
            getattr(current_media, "uri", None)
        )

    # Default the new MA-side fields to "not blocked" so legacy fixtures (and
    # any partial stub built before these fields existed) keep working. MA's
    # real :class:`Player` always sets all of them.
    available_val = bool(getattr(player, "available", True))
    enabled_val = bool(getattr(player, "enabled", True))
    needs_setup_val = bool(getattr(player, "needs_setup", False))
    active_group_val = _str_or_none(getattr(player, "active_group", None))
    synced_to_val = _str_or_none(getattr(player, "synced_to", None))

    # The cached ``playback_state`` of an unusable device is whatever MA last
    # saw (usually ``"idle"`` or ``"playing"`` for a sync follower), which is
    # indistinguishable from a quiet idle speaker. The ladder below surfaces
    # the most-blocking signal as ``state`` so a caller that only reads that
    # one field still makes a safe routing decision. Priority: unavailable
    # beats disabled beats needs-setup beats sync membership.
    if not available_val:
        state_value = "unavailable"
    elif not enabled_val:
        state_value = "disabled"
    elif needs_setup_val:
        state_value = "needs_setup"
    elif synced_to_val is not None or active_group_val is not None:
        state_value = "synced"

    return PlayerBrief(
        player_id=str(getattr(player, "player_id", "")),
        name=str(getattr(player, "display_name", None) or getattr(player, "name", "")),
        state=state_value,
        volume_level=_int(getattr(player, "volume_level", None)),
        powered=powered_val,
        current_item=current_item,
        available=available_val,
        enabled=enabled_val,
        needs_setup=needs_setup_val,
        active_group=active_group_val,
        synced_to=synced_to_val,
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
    # length), not a list. Fall back to alternate field names for older builds.
    # If none of those resolve, return ``None`` instead of len(brief_items) —
    # the latter would under-report the real length, since ``brief_items`` is
    # only the truncated lookahead from get_active_queue, not the full queue.
    raw_total = getattr(queue, "items", None)
    explicit_count = _int(raw_total) if isinstance(raw_total, int) else None
    if explicit_count is None:
        explicit_count = _int(
            getattr(queue, "items_count", None) or getattr(queue, "items_total", None)
        )
    return QueueBrief(
        queue_id=str(getattr(queue, "queue_id", "")),
        current_index=_int(getattr(queue, "current_index", None)),
        item_count=explicit_count,
        shuffle=bool(getattr(queue, "shuffle_enabled", False)),
        repeat=repeat_value,
        items=brief_items,
        available=bool(getattr(queue, "available", True)),
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


def to_resource_text(value: Any) -> str | None:
    """Serialize a resource handler's return value as JSON text.

    FastMCP's resource read API requires handlers to return
    ``str | bytes | list[ResourceContents]``. MA domain objects expose
    ``to_dict()``; our brief dataclasses are converted via
    :func:`dataclasses.asdict`. ``None`` is returned unchanged so FastMCP
    serialises it as a ``"null"`` ``TextResourceContents`` block.

    :param value: handler return value (None, MA domain object, or Brief).
    """
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return json.dumps(value.to_dict(), ensure_ascii=False, default=str)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json.dumps(dataclasses.asdict(value), ensure_ascii=False, default=str)
    return json.dumps(value, ensure_ascii=False, default=str)
