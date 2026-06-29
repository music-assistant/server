"""Shared helpers for tool sub-servers."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar, cast

from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_REQUEST, METHOD_NOT_FOUND
from music_assistant_models.enums import MediaType

from ..models import (
    AlbumBrief,
    AlbumTracksResult,
    ArtistAlbumsResult,
    ArtistBrief,
    PlayerBrief,
    PlaylistBrief,
    QueueBrief,
    QueueItemBrief,
    RadioBrief,
    TrackBrief,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastmcp import Context, FastMCP

    from music_assistant.mass import MusicAssistant

T = TypeVar("T")

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


class _LeanToolView:
    """
    A pass-through view of a FastMCP sub-server with a lean tool decorator.

    Forwards every attribute to the wrapped server, except ``tool``: its
    decorator defaults ``output_schema=None`` so tools registered through this
    view omit the auto-generated ``outputSchema``. Tools still register on the
    wrapped server; this object is a thin facade, not a separate registry.
    """

    def __init__(self, sub: FastMCP) -> None:
        self._sub = sub

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        # An explicit output_schema still wins; we only supply the default.
        kwargs.setdefault("output_schema", None)
        return self._sub.tool(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sub, name)


def lean_schema_view(sub: FastMCP) -> FastMCP:
    """
    Return a view of ``sub`` whose tools omit their output schema.

    FastMCP otherwise auto-generates an ``outputSchema`` from each tool's
    return dataclass; those schemas dominate the gated config/debug namespaces'
    context footprint. Register a namespace's tools through this view to shrink
    that footprint for MCP hosts without tool-search deferred loading. The typed
    return value is unaffected — FastMCP still serializes it into the tool
    result's text content.

    Unlike mutating ``sub.tool`` in place, this leaves the FastMCP instance
    untouched, so it does not depend on ``tool`` being a writable attribute.

    :param sub: The FastMCP sub-server to wrap.
    """
    # The view duck-types the subset of FastMCP that the tool builders use
    # (the ``tool`` decorator); typed as FastMCP so call sites stay clean.
    return cast("FastMCP", _LeanToolView(sub))


async def confirm_or_raise(ctx: Context | None, prompt: str, *, enabled: bool) -> None:
    """
    Ask the MCP client to confirm a destructive operation.

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


async def resolve_uri(mass: MusicAssistant, uri: str) -> Any:
    """
    Look up a MediaItem by MA URI, raising ToolError when missing.

    MA's MusicController APIs that mutate library / favorites / play history
    expect a resolved (media_type, library_item_id) pair or a typed media
    object — not a raw URI string. This helper centralises the lookup and
    surfaces a distinct ToolError message per failure class so the LLM
    caller can distinguish "URI typo" from "provider offline".
    """
    from music_assistant_models.errors import (  # noqa: PLC0415
        InvalidProviderURI,
        MediaNotFoundError,
        ProviderUnavailableError,
    )

    try:
        return await mass.music.get_item_by_uri(uri)
    except MediaNotFoundError as exc:
        raise ToolError(f"Item not found for URI: {uri!r}") from exc
    except InvalidProviderURI as exc:
        raise ToolError(f"Malformed Music Assistant URI: {uri!r}") from exc
    except ProviderUnavailableError as exc:
        raise ToolError(f"Provider for URI {uri!r} is offline or unreachable") from exc


async def resolve_typed_uri(
    mass: MusicAssistant,
    uri: str,
    expected: MediaType,
    *,
    type_label: str,
    hint: str | None = None,
) -> Any:
    """
    Resolve a URI and enforce an expected ``MediaType``.

    :param mass: Music Assistant instance.
    :param uri: Music Assistant media URI.
    :param expected: Required ``MediaType`` for the caller.
    :param type_label: Human-readable type name for error messages (e.g. ``track``).
    :param hint: Optional recovery hint; defaults to a tool name derived from the
        resolved ``media_type``.
    """
    item = await resolve_uri(mass, uri)
    media_type = getattr(item, "media_type", None)
    if media_type != expected:
        recovery = hint if hint is not None else _wrong_type_hint(media_type)
        article = "an" if type_label[:1].lower() in "aeiou" else "a"
        raise ToolError(
            f"URI {uri!r} is not {article} {type_label} (got media_type={media_type!r}); {recovery}"
        )
    return item


async def brief_from_uri(
    mass: MusicAssistant,
    uri: str,
    expected: MediaType,
    *,
    to_brief: Callable[[Any], T],
    type_label: str,
) -> T:
    """
    Resolve a URI and return a typed Brief, rejecting media-type mismatches.

    :param mass: Music Assistant instance.
    :param uri: Music Assistant media URI.
    :param expected: Required ``MediaType`` for the caller's tool.
    :param to_brief: Converter for the resolved item.
    :param type_label: Human-readable type name for error messages (e.g. ``track``).
    """
    item = await resolve_typed_uri(mass, uri, expected, type_label=type_label)
    return to_brief(item)


async def album_tracks_from_uri(mass: MusicAssistant, uri: str) -> AlbumTracksResult:
    """
    Resolve an album URI and return its track listing as Briefs.

    :param mass: Music Assistant instance.
    :param uri: Music Assistant album URI.
    """
    item = await resolve_typed_uri(
        mass,
        uri,
        MediaType.ALBUM,
        type_label="album",
        hint="use library_search_albums or library_get_album_by_uri first.",
    )
    raw_tracks = await mass.music.albums.tracks(item.item_id, item.provider)
    tracks = [t for t in raw_tracks if getattr(t, "available", True)]
    tracks.sort(
        key=lambda t: (
            _int(getattr(t, "disc_number", None)) or 0,
            _int(getattr(t, "track_number", None)) or 0,
            str(getattr(t, "name", "")).casefold(),
        )
    )
    return AlbumTracksResult(
        album=to_brief_album(item),
        tracks=[to_brief_track(t) for t in tracks],
    )


async def artist_albums_from_uri(mass: MusicAssistant, uri: str) -> ArtistAlbumsResult:
    """
    Resolve an artist URI and return their album discography as Briefs.

    :param mass: Music Assistant instance.
    :param uri: Music Assistant artist URI.
    """
    item = await resolve_typed_uri(
        mass,
        uri,
        MediaType.ARTIST,
        type_label="artist",
        hint="use library_search_artists or library_get_artist_by_uri first.",
    )
    raw_albums = await mass.music.artists.albums(item.item_id, item.provider)
    albums = [a for a in raw_albums if getattr(a, "available", True)]
    albums.sort(
        key=lambda a: (
            -(_int(getattr(a, "year", None)) or 0),
            str(getattr(a, "name", "")).casefold(),
        )
    )
    return ArtistAlbumsResult(
        artist=to_brief_artist(item),
        albums=[to_brief_album(a) for a in albums],
    )


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
        disc_number=_int(getattr(track, "disc_number", None)),
        track_number=_int(getattr(track, "track_number", None)),
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
    """
    Convert a Player-like object to ``PlayerBrief``.

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


def min_insert_index(queue: object) -> int:
    """Return the first queue index where new rows may be inserted."""
    floor = getattr(queue, "current_index", None)
    floor_val = floor if isinstance(floor, int) else -1
    buf = getattr(queue, "index_in_buffer", None)
    if isinstance(buf, int):
        floor_val = max(floor_val, buf)
    return floor_val + 1


def to_brief_queue(
    queue: Any, items: Sequence[Any] | None = None, *, items_offset: int = 0
) -> QueueBrief:
    """
    Convert a PlayerQueue-like object to ``QueueBrief``.

    :param queue: queue-like object with ``queue_id``, ``current_index``, etc.
    :param items: optional iterable of queue items to include.
    :param items_offset: absolute queue index of ``items[0]`` when materialised.
    """
    repeat_mode = getattr(queue, "repeat_mode", None)
    repeat_value = str(getattr(repeat_mode, "value", repeat_mode)) if repeat_mode else "off"
    brief_items: list[QueueItemBrief] = []
    if items:
        for row_index, it in enumerate(items):
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
                    index=items_offset + row_index,
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
        index_in_buffer=_int(getattr(queue, "index_in_buffer", None)),
        next_insertable_index=min_insert_index(queue),
        items_start_index=items_offset,
    )


def queue_item_uri(item: Any) -> str:
    """
    Return the media URI for a queue row, if present.

    :param item: queue item object from ``player_queues.items``.
    """
    uri = getattr(item, "uri", None)
    if uri:
        return str(uri)
    media_item = getattr(item, "media_item", None)
    if media_item is not None:
        media_uri = getattr(media_item, "uri", None)
        if media_uri:
            return str(media_uri)
    return ""


def queue_item_display_name(item: Any) -> str:
    """
    Return the display title for a queue row.

    :param item: queue item object from ``player_queues.items``.
    """
    now_playing = _external_now_playing(item)
    if now_playing and now_playing.title:
        return now_playing.title
    return str(getattr(item, "name", ""))


def resolve_added_queue_item(
    items: Sequence[Any],
    *,
    uris: frozenset[str],
    before_item_ids: frozenset[str],
) -> Any | None:
    """
    Locate the queue row created by the most recent ``add_to_queue`` call.

    Prefers rows whose ``queue_item_id`` was not present before the add.
    Falls back to the last row whose URI is in ``uris`` when ids cannot be
    distinguished (e.g. after ``replace``).

    :param items: queue items after the add.
    :param uris: candidate media URIs — the requested URI plus, for a container
        add (album / playlist), the resolved per-track URIs.
    :param before_item_ids: ``queue_item_id`` values present before the add.
    """
    new_items = [it for it in items if str(getattr(it, "queue_item_id", "")) not in before_item_ids]
    if new_items:
        return new_items[0]
    matches = [it for it in items if queue_item_uri(it) in uris]
    if matches:
        return matches[-1]
    return None


# ── private helpers ──────────────────────────────────────────────────────────

_GET_BY_URI_TOOL: dict[MediaType, str] = {
    MediaType.TRACK: "library_get_track_by_uri",
    MediaType.ALBUM: "library_get_album_by_uri",
    MediaType.ARTIST: "library_get_artist_by_uri",
    MediaType.PLAYLIST: "library_get_playlist_by_uri",
    MediaType.RADIO: "library_get_radio_by_uri",
}

_LIST_OR_SEARCH_TOOL: dict[MediaType, str] = {
    MediaType.TRACK: "library_search_tracks",
    MediaType.ALBUM: "library_search_albums",
    MediaType.ARTIST: "library_search_artists",
    MediaType.PLAYLIST: "library_list_library_playlists",
    MediaType.RADIO: "library_list_library_radio",
}


def _wrong_type_hint(got: Any) -> str:
    """Build a recovery hint naming the tool that matches the resolved media type."""
    if isinstance(got, MediaType) and got in _GET_BY_URI_TOOL:
        return f"use {_GET_BY_URI_TOOL[got]} or {_LIST_OR_SEARCH_TOOL[got]}"
    label = getattr(got, "value", got)
    return f"use the matching library_get_*_by_uri tool for {label!r}"


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
    except TypeError, ValueError:
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _volume_fields(player: Any, player_state: Any) -> tuple[bool | None, int | None, bool | None]:
    """
    Extract ``(volume_muted, group_volume, group_volume_muted)`` from a player object.

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
    """
    Resolve a player's active queue, degrading to ``None`` on any error.

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
    """
    Return the controlling provider and track title for a plugin source item.

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
    """
    Serialize a resource handler's return value as JSON text.

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
