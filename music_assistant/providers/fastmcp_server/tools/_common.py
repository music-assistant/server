"""Shared helpers for tool sub-servers."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any, NamedTuple

from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_REQUEST, METHOD_NOT_FOUND
from music_assistant_models.enums import MediaType

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
# Confirmation-gated writes block on an interactive elicitation round-trip
# (a human reads the prompt and answers) plus the subsequent save+reload.
# 10s (TIMEOUT_FAST) times out mid-confirmation; allow a generous human-scale
# window.
TIMEOUT_INTERACTIVE = 120.0


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
        # Client SDK has no elicitation support at all — pass through to the
        # permission flag (the primary defense).
        return
    except McpError as exc:
        # "Elicitation not supported" arrives as McpError(INVALID_REQUEST) /
        # METHOD_NOT_FOUND from MCP's default elicitation callback. Treat ONLY
        # the missing-capability codes as pass-through; any other wire error
        # (e.g. a throwing client handler → INTERNAL_ERROR) must fail CLOSED so
        # a destructive op is never silently confirmed.
        if exc.error.code in (INVALID_REQUEST, METHOD_NOT_FOUND):
            return
        raise
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


def to_brief_player(player: Any, active_queue: Any = None) -> PlayerBrief:
    """Convert a Player-like object to ``PlayerBrief``.

    :param player: a Player-like object.
    :param active_queue: the player's active ``PlayerQueue`` (or ``None``).
        When present, its ``state`` is the authoritative play/pause signal —
        it is what MA's own UI reads — and an external plugin source surfaces
        through it.
    """
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

    # ``active_group`` / ``synced_to`` follow the same state-first /
    # raw-fallback pattern as ``powered`` / ``current_media`` above. MA
    # populates ``state.active_group`` via ``__final_active_group``, which
    # walks every GROUP-type player and resolves membership / protocol-id
    # translation; the raw ``Player.active_group`` dataclass attr stays
    # ``None`` for SyncGroupPlayer followers even while they are streaming
    # the group's audio. Reading the canonical value is what makes
    # ``state="synced"`` fire correctly on a live sync follower.
    if player_state is not None and hasattr(player_state, "active_group"):
        active_group_val = _str_or_none(player_state.active_group)
        synced_to_val = _str_or_none(player_state.synced_to)
    else:
        active_group_val = _str_or_none(getattr(player, "active_group", None))
        synced_to_val = _str_or_none(getattr(player, "synced_to", None))

    volume_muted_val, group_volume_val, group_volume_muted_val = _volume_fields(
        player, player_state
    )

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
    elif active_queue is not None:
        queue_state = getattr(active_queue, "state", None)
        state_value = (
            str(getattr(queue_state, "value", queue_state))
            if queue_state is not None
            else state_value
        )

    external_source: str | None = None
    if active_queue is not None:
        now_playing = _external_now_playing(getattr(active_queue, "current_item", None))
        if now_playing is not None:
            external_source = now_playing.instance_id
            if now_playing.title:
                current_item = now_playing.title

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
        volume_muted=volume_muted_val,
        group_volume=group_volume_val,
        group_volume_muted=group_volume_muted_val,
        external_source=external_source,
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
            now_playing = _external_now_playing(it)
            item_name = (
                now_playing.title
                if now_playing and now_playing.title
                else str(getattr(it, "name", ""))
            )
            brief_items.append(
                QueueItemBrief(
                    item_id=str(getattr(it, "queue_item_id", "")),
                    name=item_name,
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


def _volume_fields(player: Any, player_state: Any) -> tuple[bool | None, int | None, bool | None]:
    """Extract ``(volume_muted, group_volume, group_volume_muted)`` from a player object.

    Volume/mute fields live canonically on ``Player.state`` — the raw dataclass
    attrs are caches that lag. ``group_volume`` is only ever populated on the
    state for SyncGroupPlayer. Reading state-first means the SyncGroupPlayer's
    brief reports a real ``group_volume`` instead of the cached ``None``.

    :param player: a Player-like object.
    :param player_state: the resolved ``Player.state`` object (may be ``None``).
    """
    if player_state is not None and hasattr(player_state, "volume_muted"):
        raw_vm = player_state.volume_muted
        volume_muted_val: bool | None = bool(raw_vm) if raw_vm is not None else None
    else:
        raw_vm = getattr(player, "volume_muted", None)
        volume_muted_val = bool(raw_vm) if raw_vm is not None else None

    if player_state is not None and hasattr(player_state, "group_volume"):
        group_volume_val = _int(player_state.group_volume)
        raw_gm = getattr(player_state, "group_volume_muted", None)
        group_volume_muted_val: bool | None = bool(raw_gm) if raw_gm is not None else None
    else:
        group_volume_val = _int(getattr(player, "group_volume", None))
        raw_gm = getattr(player, "group_volume_muted", None)
        group_volume_muted_val = bool(raw_gm) if raw_gm is not None else None

    return volume_muted_val, group_volume_val, group_volume_muted_val


def safe_active_queue(mass: Any, player_id: str) -> Any:
    """Resolve a player's active queue, degrading to ``None`` on any error.

    MA's queue resolver walks ``player.state`` and recurses through sync
    leaders / group players, so a single partially-populated player could
    otherwise raise and take down the whole ``list_players`` response. On any
    failure this degrades to "no active queue" — the brief still renders, just
    without external-source surfacing.

    :param mass: the Music Assistant instance.
    :param player_id: the player whose active queue to resolve.
    """
    try:
        return mass.player_queues.get_active_queue(player_id)
    except Exception:
        return None


class ExternalNowPlaying(NamedTuple):
    """An external (Connect-style) source's controlling provider and track title."""

    instance_id: str
    title: str | None


def _external_now_playing(queue_item: Any) -> ExternalNowPlaying | None:
    """Return the controlling provider and track title for a plugin source item.

    Detects a "Connect"-style external source (Spotify Connect, AirPlay,
    Yandex Ynison) — these surface as a single queue item whose stream is a
    :attr:`MediaType.AUDIO_SOURCE`. Returns ``None`` for normal tracks, for
    items without stream details, and for ``None``.

    :param queue_item: a queue item to inspect (may be ``None``).
    """
    sd = getattr(queue_item, "streamdetails", None)
    if sd is None:
        return None
    media_type = getattr(sd, "media_type", None)
    media_type_val = (
        str(getattr(media_type, "value", media_type)) if media_type is not None else None
    )
    # PLUGIN_SOURCE is the deprecated alias kept for one-release back-compat.
    if media_type_val not in {MediaType.AUDIO_SOURCE.value, MediaType.PLUGIN_SOURCE.value}:
        return None
    provider = _str_or_none(getattr(sd, "provider", None))
    if provider is None:
        return None
    metadata = getattr(sd, "stream_metadata", None)
    title = _str_or_none(getattr(metadata, "title", None)) if metadata is not None else None
    return ExternalNowPlaying(provider, title)


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
