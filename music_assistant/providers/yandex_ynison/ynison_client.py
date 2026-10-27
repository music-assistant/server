"""Ynison WebSocket client for Yandex Music device synchronization."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)

if TYPE_CHECKING:
    from music_assistant_models.enums import RepeatMode
    from ya_passport_auth import SecretStr

from .constants import (
    DEFAULT_APP_NAME,
    DEFAULT_APP_VERSION,
    DEVICE_TYPE_WEB,
    RECONNECT_DELAYS,
    RECONNECT_IDLE_DELAY,
    STATUS_DEDUP_WINDOW_MS,
    STRICT_SEND_WAIT,
    WS_CONNECT_TIMEOUT,
    WS_HEARTBEAT,
    WS_MIN_HEARTBEAT,
    YNISON_DEVICE_SERVER_DISCONNECT,
    YNISON_ORIGIN,
    YNISON_RECONNECT_ERROR_CODES,
    YNISON_REDIRECT_URL,
    YNISON_STATE_PATH,
)
from .queue_model import (
    NavigationOutcome,
    make_version_block,
    plan_navigation,
    replace_items,
    set_index,
    set_repeat,
    set_shuffle,
)
from .reconnect_policy import ReconnectPolicy, parse_seconds


class YnisonSendError(ConnectionError):
    """
    Raised by `YnisonClient._send(strict=True)` when a send cannot reach Ynison.

    Indicates a transport-level failure (WebSocket not connected, write raised
    ``ConnectionError`` / ``aiohttp.ClientError`` / ``RuntimeError`` / ``OSError``).
    A reconnect is always scheduled before this is raised; callers should
    translate it to the appropriate user-facing error (e.g.
    ``PlayerCommandFailed``) or log-and-return for fire-and-forget paths.

    Inherits from ``ConnectionError`` so existing broad transport-error
    handlers continue to catch it.
    """


def _stringify_version(version: Any) -> None:
    """Coerce int `version.version`/`version.timestamp_ms` fields to str in-place."""
    if not isinstance(version, dict):
        return
    for key in ("version", "timestamp_ms"):
        val = version.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            version[key] = str(val)


def _local_time(epoch: float) -> str:
    """Format a wall-clock time for log messages in the server's local zone."""
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _same_playback(queue: dict[str, Any], current: dict[str, Any] | None) -> bool:
    """Return whether ``queue`` differs from ``current`` only in its playback options."""
    ignored = {"version", "options", "shuffle_optional"}
    if current is None:
        return False
    return {k: v for k, v in queue.items() if k not in ignored} == {
        k: v for k, v in current.items() if k not in ignored
    }


def _current_playable_id(queue: dict[str, Any]) -> Any:
    """Return the playable id at the current index of a queue, or ``None``."""
    playables = queue.get("playable_list") or []
    index = queue.get("current_playable_index", -1)
    if isinstance(index, int) and 0 <= index < len(playables):
        return playables[index].get("playable_id")
    return None


def normalize_player_state_timestamps(player_state: dict[str, Any]) -> None:
    """
    Coerce Ynison timestamp fields to strings in-place.

    Ynison rejects integer `status.progress_ms`/`duration_ms`/`version.*`
    (HTTP 500 + WS teardown), so we normalize inbound state at the ingestion
    boundary. This guarantees that every outbound echo — whether via
    `update_player_state` (from shallow-copied state) or `send_full_state`
    on reconnect — carries string-typed timestamps by construction.
    """
    status = player_state.get("status")
    if isinstance(status, dict):
        for key in ("progress_ms", "duration_ms", "player_action_timestamp_ms"):
            val = status.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                status[key] = str(val)
        _stringify_version(status.get("version"))
    queue = player_state.get("player_queue")
    if isinstance(queue, dict):
        _stringify_version(queue.get("version"))


@dataclass
class YnisonDeviceInfo:
    """Device identification for Ynison registration."""

    device_id: str
    title: str
    type: str = DEVICE_TYPE_WEB
    app_name: str = DEFAULT_APP_NAME
    app_version: str = DEFAULT_APP_VERSION


@dataclass(frozen=True)
class _StatusWatermark:
    """Record one successfully sent playing-status update."""

    track_id: str
    progress_ms: int
    duration_ms: int
    paused: bool
    sent_at: float


@dataclass(frozen=True, slots=True)
class StatusBaseline:
    """Newest playback status known to the server: received from it or sent by us."""

    progress_ms: int
    duration_ms: int
    paused: bool
    # Monotonic time at which ``progress_ms`` was current.
    anchor: float
    # ``None`` for a status we sent; its echo comes back without our version.
    version: dict[str, Any] | None

    @classmethod
    def from_status(cls, status: dict[str, Any], *, anchor: float) -> StatusBaseline | None:
        """
        Build a baseline from a server status, or ``None`` when it is unreadable.

        :param status: Ynison ``player_state.status`` object.
        :param anchor: Monotonic time at which the status was received.
        """
        try:
            progress = int(status.get("progress_ms", 0))
            duration = int(status.get("duration_ms", 0))
        except TypeError, ValueError:
            return None
        return cls(
            progress,
            duration,
            bool(status.get("paused", True)),
            anchor,
            status.get("version") or {},
        )

    def expected_progress_ms(self, now: float) -> int:
        """
        Return the position this status implies at ``now``.

        :param now: Monotonic time to extrapolate to.
        """
        if self.paused:
            return self.progress_ms
        return self.progress_ms + int((now - self.anchor) * 1000)

    def matches(self, progress_ms: int, duration_ms: int, paused: bool, now: float) -> bool:
        """
        Return whether a status adds nothing to this one within the dedup window.

        :param progress_ms: Position of the candidate status.
        :param duration_ms: Duration of the candidate status.
        :param paused: Pause flag of the candidate status.
        :param now: Monotonic time of the candidate status.
        """
        if self.paused != paused or self.duration_ms != duration_ms:
            return False
        return abs(self.expected_progress_ms(now) - progress_ms) <= STATUS_DEDUP_WINDOW_MS

    def is_repeated_by(self, status: dict[str, Any], device_id: str) -> bool:
        """
        Return whether an inbound status merely repeats this one.

        :param status: Inbound Ynison ``player_state.status`` object.
        :param device_id: This device, whose own sends come back unversioned.
        """
        try:
            progress = int(status.get("progress_ms", 0))
            duration = int(status.get("duration_ms", 0))
        except TypeError, ValueError:
            return False
        # A status authored by another device under a new version is a fresh
        # command, even when it lands on the position we already know.
        version = status.get("version") or {}
        author = version.get("device_id") or ""
        if author and author != device_id and version != self.version:
            return False
        return (progress, duration, bool(status.get("paused", True))) == (
            self.progress_ms,
            self.duration_ms,
            self.paused,
        )


@dataclass(frozen=True)
class NavigationBaseline:
    """Playback that a queue navigation decision was made for."""

    # Queue lineage: changes only when a queue from another device is adopted.
    lineage: int
    playable_id: Any
    active_device_id: str | None
    paused: bool
    navigation_version: int | None


@dataclass
class YnisonState:
    """Parsed Ynison state from the server."""

    player_state: dict[str, Any] = field(default_factory=dict)
    active_device_id: str | None = None
    devices: list[dict[str, Any]] = field(default_factory=list)
    # True iff the most recent state update carried a version block
    # (on player_queue or status) authored by our own device_id — i.e.
    # it is Ynison echoing back an update we originated. Consumers can
    # inspect this to suppress feedback loops. False when no authored
    # version block is present (e.g. status-only update from a peer
    # that did not round-trip via our device).
    last_update_is_echo: bool = False
    # True iff the most recent update carried no status or repeated the newest
    # status already known to the server (received from it or sent by us).
    # Status is published only on change, so such a status carries a
    # deliberately stale position.
    status_is_relay: bool = False
    # True iff the most recent update was the echo of our own queue edit that
    # kept the current item (an append, window trim or option change), so a
    # shifted index is the same occurrence, not navigation.
    current_item_kept: bool = False
    # True iff the most recent update was the echo of our own navigation, so
    # the current item is a new occurrence even with the same track and index.
    current_item_navigated: bool = False

    @property
    def current_track_id(self) -> str | None:
        """Extract current track_id from player queue."""
        queue = self.player_state.get("player_queue", {})
        playable_list = queue.get("playable_list", [])
        index = queue.get("current_playable_index", 0)
        if playable_list and 0 <= index < len(playable_list):
            playable_id = playable_list[index].get("playable_id")
            if playable_id:
                return str(playable_id)
        return None

    @property
    def current_index(self) -> int:
        """Return the original-list index of the current item, or -1."""
        index = self.player_state.get("player_queue", {}).get("current_playable_index", -1)
        return index if isinstance(index, int) and not isinstance(index, bool) else -1

    @property
    def is_paused(self) -> bool:
        """Return True if playback is paused."""
        return bool(self.player_state.get("status", {}).get("paused", True))

    @property
    def progress_ms(self) -> int:
        """Return current playback progress in milliseconds."""
        return int(self.player_state.get("status", {}).get("progress_ms", 0))

    @property
    def duration_ms(self) -> int:
        """Return current track duration in milliseconds."""
        return int(self.player_state.get("status", {}).get("duration_ms", 0))


# Type alias for the state update callback
StateUpdateCallback = Callable[[YnisonState], Awaitable[None]]
# Callback invoked on auth failure; should return a fresh token (or raise).
AuthRefreshCallback = Callable[[], Awaitable["SecretStr"]]


class YnisonClient:
    """
    WebSocket client for the Yandex Ynison protocol.

    Manages the two-step connection (redirector → state service) and
    provides methods to send state updates back to Ynison.
    """

    def __init__(
        self,
        token: SecretStr,
        device_info: YnisonDeviceInfo,
        on_state_update: StateUpdateCallback,
        logger: logging.Logger,
        http_session: aiohttp.ClientSession | None = None,
        on_auth_failure: AuthRefreshCallback | None = None,
    ) -> None:
        """
        Initialize Ynison client.

        :param token: Yandex Music OAuth token (wrapped in SecretStr).
        :param device_info: Device identification for Ynison.
        :param on_state_update: Callback for state updates from Ynison.
        :param logger: Logger instance.
        :param http_session: Optional shared aiohttp session.
        :param on_auth_failure: Optional callback invoked on auth failure during
            reconnect. Should return a fresh SecretStr token. If not provided or
            if the callback raises, reconnect proceeds with the current token.
        """
        self._token = token
        self._device_info = device_info
        self._on_state_update = on_state_update
        self._logger = logger
        self._external_session = http_session
        self._on_auth_failure = on_auth_failure

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._send_lock = asyncio.Lock()
        self._pending_queue: dict[str, Any] | None = None
        self._pending_queue_until = 0.0
        self._last_queue_version: int | None = None
        self._queue_epoch = 0
        self._queue_lineage = 0
        self._navigation_version: int | None = None
        self._unadopted_navigation: int | None = None
        self._navigation_status: dict[str, Any] | None = None
        self._confirmed_queue_version: int | None = None
        self._message_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected = False
        self._has_connected_once = False
        self._policy = ReconnectPolicy(RECONNECT_DELAYS, RECONNECT_IDLE_DELAY)
        self._heartbeat = WS_HEARTBEAT
        self._status_watermarks: deque[_StatusWatermark] = deque(maxlen=8)
        self._status_baseline: StatusBaseline | None = None

        # Latest state from server
        self.state = YnisonState()

        # Reconnect settle window — first inbound state after reconnect can
        # be our own stale broadcast (server retains state across reconnects
        # and re-sends it). Provider-level handlers consult this watermark
        # to discard the first ≤2s of post-reconnect state changes.
        self._post_reconnect_settle_until: float = 0.0

    @property
    def connected(self) -> bool:
        """Return True if connected to Ynison state service."""
        return self._connected

    @property
    def in_post_reconnect_settle(self) -> bool:
        """
        True iff we're inside the 2 s post-reconnect settle window.

        Provider handlers consult this to skip the first inbound state right
        after a reconnect — that state can be a stale broadcast of our own
        last-known view (server retained it across the WS hop) and acting on
        it would re-fire pause/play commands the user never issued.
        """
        return time.monotonic() < self._post_reconnect_settle_until

    @property
    def device_id(self) -> str:
        """Return our Ynison device_id (used when authoring outgoing state)."""
        return self._device_info.device_id

    @property
    def queue_generation(self) -> int:
        """Return the generation invalidated by peer replacement or reconnect."""
        return self._queue_epoch

    def queue_snapshot(self) -> dict[str, Any]:
        """Return the latest queue base for local navigation and mutations."""
        queue = self.state.player_state.get("player_queue", {})
        if self._pending_queue is not None and time.monotonic() < self._pending_queue_until:
            queue = self._pending_queue
        return deepcopy(queue)

    async def connect(self) -> None:
        """
        Connect to Ynison (redirector → state service).

        Recover authentication through the refresh callback when available.
        Raise authentication failures without a callback; retry transient errors.
        """
        self._stop_event.clear()
        if self._external_session and self._external_session.closed:
            raise ResourceTemporarilyUnavailable("Provided http_session is closed")
        # ma-allow: MA001 standalone fallback only; the provider always passes mass.http_session
        self._session = self._external_session or aiohttp.ClientSession()

        try:
            # Step 1: Get redirect ticket
            host, ticket, session_id = await self._get_redirect_ticket()

            # Step 2: Connect to state service
            await self._connect_state(host, ticket, session_id)
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except (LoginFailed, aiohttp.ClientError, OSError, TimeoutError) as exc:
            if isinstance(exc, LoginFailed) and self._on_auth_failure is None:
                await self.disconnect()
                raise
            self._logger.warning("Initial connection failed, scheduling reconnect", exc_info=True)
            self._connected = False
            await self._close_socket()
            await self._close_session()
            self._schedule_reconnect()
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Gracefully disconnect from Ynison."""
        self._stop_event.set()
        self._connected = False
        self._reset_pending_queue()

        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._message_task

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task

        await self._close_socket()
        await self._close_session()

    def update_token(self, token: SecretStr) -> None:
        """Replace the stored OAuth token (e.g. after a refresh)."""
        self._token = token

    async def wait_connected(self, timeout: float) -> bool:
        """
        Wait until the state channel is usable.

        :param timeout: Maximum seconds to wait.
        :return: ``True`` when connected, ``False`` on timeout or disconnect().
        """
        deadline = time.monotonic() + timeout
        while not self._channel_ready():
            if self._stop_event.is_set() or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def report_status(
        self,
        progress_ms: int,
        duration_ms: int,
        paused: bool,
        *,
        strict: bool = False,
    ) -> bool:
        """
        Publish this device's playback position when Ynison needs to know it.

        Progress is clamped to the duration, which falls back to the server's
        duration when unknown locally. Best-effort reports (``strict=False``)
        are skipped while another device is active or when they match the
        newest server-known status within two seconds; strict reports are
        always sent.

        :param progress_ms: Playback position in milliseconds.
        :param duration_ms: Track duration in milliseconds, or 0 when unknown.
        :param paused: Whether playback is paused.
        :param strict: Raise :class:`YnisonSendError` instead of dropping the
            report when no duration is known or the channel stays unavailable
            for :data:`STRICT_SEND_WAIT` seconds.
        :return: ``True`` when the status was sent.
        """
        duration_ms = duration_ms if duration_ms > 0 else self.state.duration_ms
        if duration_ms <= 0:
            if strict:
                raise YnisonSendError("Ynison track duration is unknown")
            return False
        progress_ms = max(0, min(progress_ms, duration_ms))
        if not strict and (
            self.state.active_device_id != self.device_id
            or self._matches_status_baseline(progress_ms, duration_ms, paused)
        ):
            return False
        return await self.update_playing_status(progress_ms, duration_ms, paused, strict=strict)

    # ------------------------------------------------------------------
    # Send methods
    # ------------------------------------------------------------------

    async def update_playing_status(
        self,
        progress_ms: int,
        duration_ms: int,
        paused: bool,
        *,
        strict: bool = False,
    ) -> bool:
        """
        Send exactly this playback status to Ynison.

        Prefer :meth:`report_status`, which decides whether a status is needed.

        :param progress_ms: Current playback position in milliseconds.
        :param duration_ms: Current track duration in milliseconds.
        :param paused: Whether playback is paused.
        :param strict: When ``True``, raise :class:`YnisonSendError` on
            transport failure instead of silently scheduling a reconnect.
            Delivery-critical callers (user commands, end-of-track signal)
            opt in; heartbeat callers leave the default.
        :return: ``True`` when the status was sent.
        """
        self._logger.debug(
            "→ update_playing_status: progress=%dms duration=%dms paused=%s",
            progress_ms,
            duration_ms,
            paused,
        )
        msg = {
            "update_playing_status": {
                "playing_status": {
                    "progress_ms": str(progress_ms),
                    "duration_ms": str(duration_ms),
                    "paused": paused,
                    "playback_speed": 1.0,
                },
            },
            **self._message_meta(
                "DO_NOT_INTERCEPT_BY_DEFAULT" if paused else "INTERCEPT_IF_NO_ONE_ACTIVE"
            ),
        }
        sent = await self._send(msg, strict=strict)
        if sent:
            self._status_baseline = StatusBaseline(
                progress_ms, duration_ms, paused, time.monotonic(), None
            )
            if self._unadopted_navigation is not None and self._navigation_status is not None:
                # Later queue edits republish this status, not the navigation's.
                self._navigation_status = {
                    **self._navigation_status,
                    "progress_ms": str(progress_ms),
                    "duration_ms": str(duration_ms),
                    "paused": paused,
                }
            track_id = self.state.current_track_id
            if track_id is not None:
                self._status_watermarks.append(
                    _StatusWatermark(
                        track_id=track_id,
                        progress_ms=progress_ms,
                        duration_ms=duration_ms,
                        paused=paused,
                        sent_at=time.monotonic(),
                    )
                )
        return sent

    async def update_active_device(self, device_id: str) -> None:
        """Request playback transfer to this device."""
        msg = {
            "update_active_device": {
                "device_id_optional": device_id,
            },
            **self._message_meta(),
        }
        await self._send(msg)

    async def update_session_params(self, mute_events_if_passive: bool = True) -> None:
        """
        Configure session params on the Ynison server.

        `mute_events_if_passive=True` tells Ynison not to forward peer
        state updates while we're not the active device. Reduces inbound
        WS noise (and CPU) when running in `borrow` mode alongside other
        active subscribers, and removes a class of false positives in
        echo detection — fewer messages means fewer chances to misclassify.
        """
        msg = {
            "update_session_params": {
                "mute_events_if_passive": mute_events_if_passive,
            },
            **self._message_meta(),
        }
        self._logger.info(
            "→ update_session_params: mute_events_if_passive=%s", mute_events_if_passive
        )
        await self._send(msg)

    async def sync_state_from_eov(self, actual_queue_id: str = "") -> None:
        """
        Request queue sync from the EOV (Unified Playback Queue) backend.

        Asks the Ynison server to refresh the queue from the central EOV service.
        Only works when this device is the active player. If the EOV queue
        differs from actual_queue_id, the server broadcasts the updated state.

        :param actual_queue_id: Current queue ID (empty string forces refresh).
        """
        msg = {
            "sync_state_from_eov": {
                "actual_queue_id": actual_queue_id,
            },
            **self._message_meta(),
        }
        self._logger.info("→ sync_state_from_eov: queue_id=%r", actual_queue_id)
        await self._send(msg)

    async def update_player_state(
        self,
        player_state: dict[str, Any],
        *,
        strict: bool = False,
    ) -> None:
        """
        Send player state update (queue changes, track skip).

        Unlike send_full_state, this does NOT reset active device status.
        Use this for track advances, queue modifications, repeat/shuffle changes.

        :param player_state: Complete `player_state` dict to broadcast.
        :param strict: When ``True``, raise :class:`YnisonSendError` on
            transport failure instead of silently scheduling a reconnect.
            Delivery-critical callers (queue advance after track end)
            opt in; queue-list-replenish heartbeats leave the default.
        """
        await self._send(self._player_state_message(player_state), strict=strict)

    async def mutate_player_state(
        self,
        mutation: Callable[[dict[str, Any]], bool | None],
        *,
        expected_generation: int | None = None,
        navigation: bool | Callable[[], bool] = False,
    ) -> int | None:
        """
        Serialize a queue change against the latest successfully sent queue.

        :param mutation: Synchronous edit of a private complete player-state copy.
            Return ``False`` to leave the queue unchanged without publishing.
        :param expected_generation: Queue generation that an asynchronous result belongs to.
        :param navigation: Whether the edit moves the current item, or a callable that
            decides it after ``mutation`` ran; it is registered before sending so that
            an early echo is recognised as navigation.
        :return: Version of the published queue, or ``None`` when nothing was sent.
        """
        requested_epoch = self._queue_epoch if expected_generation is None else expected_generation
        async with self._send_lock:
            if requested_epoch != self._queue_epoch:
                raise ResourceTemporarilyUnavailable("Ynison queue changed before command was sent")
            player_state = deepcopy(self.state.player_state)
            player_state["player_queue"] = self.queue_snapshot()
            if self._unadopted_navigation is not None and self._navigation_status is not None:
                # Until our navigation is adopted, its status is the newest one we sent.
                player_state["status"] = deepcopy(self._navigation_status)
            if mutation(player_state) is False:
                return None
            queue = player_state.get("player_queue", {})
            queue["version"] = make_version_block(self.device_id)
            previous = (
                self._navigation_version,
                self._unadopted_navigation,
                self._navigation_status,
            )
            registered = navigation() if callable(navigation) else navigation
            if registered:
                self._navigation_version = int(queue["version"]["version"])
                self._unadopted_navigation = self._navigation_version
                self._navigation_status = deepcopy(player_state.get("status"))
            epoch = self._queue_epoch
            try:
                await self._send_locked(self._player_state_message(player_state), strict=True)
            except YnisonSendError:
                if registered:
                    (
                        self._navigation_version,
                        self._unadopted_navigation,
                        self._navigation_status,
                    ) = previous
                raise
            if epoch != self._queue_epoch:
                raise ResourceTemporarilyUnavailable("Ynison queue changed while command was sent")
            self._pending_queue = deepcopy(queue)
            self._pending_queue_until = time.monotonic() + 30
            self._last_queue_version = int(queue["version"]["version"])
            return self._last_queue_version

    def estimated_progress_ms(self) -> int:
        """Return the server-known position of the current item, extrapolated to now."""
        baseline = self._status_baseline
        if baseline is None:
            return self.state.progress_ms
        progress = baseline.expected_progress_ms(time.monotonic())
        return min(progress, baseline.duration_ms) if baseline.duration_ms > 0 else progress

    def navigation_baseline(self) -> NavigationBaseline:
        """Capture the playback that a later :meth:`advance_queue` must still find."""
        return NavigationBaseline(
            lineage=self._queue_lineage,
            playable_id=_current_playable_id(self.queue_snapshot()),
            active_device_id=self.state.active_device_id,
            paused=self.state.is_paused,
            navigation_version=self._navigation_version,
        )

    def is_current_playback(self, baseline: NavigationBaseline) -> bool:
        """
        Return whether navigation decided for ``baseline`` still applies.

        Our own appends, window trims and option edits keep it valid; a queue
        from another device, an active-device change or a pause change do not.

        :param baseline: Playback captured with :meth:`navigation_baseline`.
        """
        return self._matches_baseline(baseline, self.queue_snapshot())

    def navigation_confirmed(self, baseline: NavigationBaseline) -> bool:
        """
        Return whether navigation published after ``baseline`` has settled.

        It settles once Ynison echoes that navigation, or when another device
        replaces the queue or becomes active, which supersedes it.

        :param baseline: Playback captured before the navigation was published.
        """
        if (
            self._queue_lineage != baseline.lineage
            or self.state.active_device_id != baseline.active_device_id
        ):
            return True
        version = self._navigation_version
        return (
            version is not None
            and version != baseline.navigation_version
            and self._confirmed_queue_version is not None
            and self._confirmed_queue_version >= version
        )

    async def advance_queue(
        self,
        *,
        natural: bool,
        duration_ms: int,
        baseline: NavigationBaseline | None = None,
    ) -> NavigationOutcome:
        """
        Publish the next logical item of the current queue, honouring repeat.

        :param natural: Whether the current item finished rather than being skipped.
        :param duration_ms: Duration of the finished item, published on a terminal stop.
        :param baseline: Playback captured when the decision was requested; defaults
            to the current playback.
        :return: ``radio`` without publishing when a RADIO queue needs more items,
            otherwise the published ``change``, ``restart`` or ``stop``.
        :raises ResourceTemporarilyUnavailable: When the queue, active device or
            pause state changed before the edit was sent.
        :raises YnisonSendError: When the edit could not be sent.
        """
        expected = baseline or self.navigation_baseline()
        outcome = NavigationOutcome.FAILED

        def mutate(player_state: dict[str, Any]) -> bool | None:
            nonlocal outcome
            if not self._matches_baseline(expected, player_state["player_queue"]):
                raise ResourceTemporarilyUnavailable("Ynison playback changed during navigation")
            outcome = plan_navigation(
                player_state, natural=natural, duration_ms=duration_ms, device_id=self.device_id
            )
            return False if outcome == NavigationOutcome.RADIO else None

        # Repeating the same item restarts it locally; only a move is navigation.
        await self.mutate_player_state(
            mutate, navigation=lambda: outcome == NavigationOutcome.CHANGE
        )
        return outcome

    async def go_to_index(
        self,
        index: int,
        *,
        items: list[dict[str, Any]] | None = None,
        expected_generation: int | None = None,
    ) -> None:
        """
        Publish ``index`` as the current item, optionally appending items first.

        :param index: Original-list index of the new current item, counted in ``items``.
        :param items: Current playables followed by appended ones.
        :param expected_generation: Queue generation the edit was computed for.
        :raises ResourceTemporarilyUnavailable: When the queue was replaced meanwhile.
        :raises YnisonSendError: When the edit could not be sent.
        """

        def mutate(player_state: dict[str, Any]) -> None:
            set_index(player_state, index, device_id=self.device_id, items=items)

        await self.mutate_player_state(
            mutate, expected_generation=expected_generation, navigation=True
        )

    async def publish_items(
        self,
        items: list[dict[str, Any]],
        *,
        expected_generation: int | None = None,
    ) -> int | None:
        """
        Publish an appended playable list without moving, while this device is active.

        :param items: Current playables followed by appended ones.
        :param expected_generation: Queue generation the items were fetched for.
        :return: Leading items dropped by the window (the current index moved down
            by that much), or ``None`` when another device is active and nothing
            was sent.
        :raises ResourceTemporarilyUnavailable: When the queue was replaced meanwhile.
        :raises YnisonSendError: When the edit could not be sent.
        """
        if self.state.active_device_id != self.device_id:
            return None
        dropped = 0

        def mutate(player_state: dict[str, Any]) -> None:
            nonlocal dropped
            dropped = replace_items(player_state, items)

        await self.mutate_player_state(mutate, expected_generation=expected_generation)
        return dropped

    async def set_repeat_mode(self, mode: RepeatMode) -> None:
        """
        Publish a repeat mode for the current queue.

        :param mode: Repeat mode other than ``UNKNOWN``.
        :raises ResourceTemporarilyUnavailable: When the queue was replaced meanwhile.
        :raises YnisonSendError: When the edit could not be sent.
        """

        def mutate(player_state: dict[str, Any]) -> None:
            set_repeat(player_state, mode)

        await self.mutate_player_state(mutate)

    async def set_shuffle(self, *, enabled: bool) -> None:
        """
        Publish a shuffle permutation starting at the current item, or remove it.

        :param enabled: Whether shuffle is on.
        :raises ResourceTemporarilyUnavailable: When the queue was replaced meanwhile.
        :raises YnisonSendError: When the edit could not be sent.
        """

        def mutate(player_state: dict[str, Any]) -> None:
            set_shuffle(player_state, enabled=enabled)

        await self.mutate_player_state(mutate)

    async def send_full_state(
        self,
        player_state: dict[str, Any] | None = None,
    ) -> None:
        """Send full state update (cold start, reconnect after offline)."""
        state = player_state or self._build_initial_state()
        msg = {
            "update_full_state": {
                "player_state": state,
                "device": self._build_device_dict(),
                "is_currently_active": False,
            },
            **self._message_meta(),
        }
        self._logger.debug("Sending full state: %s", json.dumps(msg)[:500])
        await self._send(msg)

    @staticmethod
    def _message_meta(interception: str = "DO_NOT_INTERCEPT_BY_DEFAULT") -> dict[str, Any]:
        """
        Return the request envelope every outbound message carries.

        Ynison expects string-typed timestamps; integers cause 500 responses.

        :param interception: ``activity_interception_type`` for this request.
        """
        return {
            "rid": str(uuid.uuid4()),
            "player_action_timestamp_ms": str(int(time.time() * 1000)),
            "activity_interception_type": interception,
        }

    def _is_known_status(self, status: Any) -> bool:
        """Return whether a status repeats the newest server-known one."""
        if self._status_baseline is None or not isinstance(status, dict):
            return False
        return self._status_baseline.is_repeated_by(status, self.device_id)

    def _remember_server_status(self, status: Any) -> None:
        """Record a server-authoritative status as the dedup baseline."""
        if isinstance(status, dict):
            self._status_baseline = StatusBaseline.from_status(status, anchor=time.monotonic())

    def _matches_baseline(self, baseline: NavigationBaseline, queue: dict[str, Any]) -> bool:
        """Return whether ``queue`` and the current playback are still those of ``baseline``."""
        return (
            self._queue_lineage == baseline.lineage
            and self.state.active_device_id == baseline.active_device_id
            and self.state.is_paused == baseline.paused
            and _current_playable_id(queue) == baseline.playable_id
            and self._navigation_version == baseline.navigation_version
        )

    def _channel_ready(self) -> bool:
        """Return whether the state WebSocket can accept a message."""
        return self._connected and self._ws is not None and not self._ws.closed

    def _matches_status_baseline(self, progress_ms: int, duration_ms: int, paused: bool) -> bool:
        """Return whether a status equals the newest server-known one within the dedup window."""
        if self._status_baseline is None:
            return False
        return self._status_baseline.matches(progress_ms, duration_ms, paused, time.monotonic())

    def _classify_state_as_echo(self, incoming_ps: dict[str, Any]) -> tuple[bool, bool]:
        """
        Classify an inbound state as an echo and whether its status is stale.

        Full state broadcasts require both queue and status authorship. Ynison
        normalizes playing-status heartbeats to an empty/zero version, so a
        status-only response may instead match one recent successful send.
        """
        own_id = self._device_info.device_id
        queue_block = (incoming_ps.get("player_queue") or {}).get("version") or {}
        status_block = (incoming_ps.get("status") or {}).get("version") or {}
        queue_is_ours = queue_block.get("device_id") == own_id
        status_is_ours = status_block.get("device_id") == own_id
        if queue_is_ours and status_is_ours:
            return True, False
        incoming_queue = incoming_ps.get("player_queue")
        queue_changed = (
            incoming_queue is not None
            and incoming_queue != self.state.player_state.get("player_queue")
        )
        if queue_changed or status_block != {
            "device_id": "",
            "version": "0",
            "timestamp_ms": "0",
        }:
            return False, False

        status = incoming_ps.get("status") or {}
        progress = status.get("progress_ms")
        duration = status.get("duration_ms")
        if (
            not isinstance(progress, (str, int))
            or isinstance(progress, bool)
            or not isinstance(duration, (str, int))
            or isinstance(duration, bool)
        ):
            return False, False
        try:
            progress_ms = int(progress)
            duration_ms = int(duration)
        except ValueError:
            return False, False
        current_track = self.state.current_track_id
        now = time.monotonic()
        for watermark in reversed(self._status_watermarks):
            if (
                now - watermark.sent_at <= 2.0
                and current_track == watermark.track_id
                and abs(progress_ms - watermark.progress_ms) <= 1
                and duration_ms == watermark.duration_ms
                and status.get("paused") is watermark.paused
            ):
                return True, True
        return False, False

    # ------------------------------------------------------------------
    # Connection internals
    # ------------------------------------------------------------------

    def _build_ws_protocol_header(
        self,
        redirect_ticket: str | None = None,
        session_id: int | None = None,
    ) -> str:
        """Build Sec-WebSocket-Protocol header value."""
        proto: dict[str, Any] = {
            "Ynison-Device-Id": self._device_info.device_id,
            "Ynison-Device-Info": json.dumps(
                {
                    "app_name": self._device_info.app_name,
                    "app_version": self._device_info.app_version,
                    "type": 1,
                }
            ),
        }
        if redirect_ticket is not None:
            proto["Ynison-Redirect-Ticket"] = redirect_ticket
        if session_id is not None:
            proto["Ynison-Session-Id"] = str(session_id)
        return f"Bearer, v2, {json.dumps(proto)}"

    def _build_headers(
        self,
        redirect_ticket: str | None = None,
        session_id: int | None = None,
    ) -> dict[str, str]:
        """Build common WebSocket headers."""
        return {
            "Authorization": f"OAuth {self._token.get_secret()}",
            "Origin": YNISON_ORIGIN,
            "Sec-WebSocket-Protocol": self._build_ws_protocol_header(redirect_ticket, session_id),
        }

    def _build_device_dict(self) -> dict[str, Any]:
        """Build device info dict for Ynison messages."""
        info = asdict(self._device_info)
        return {
            "info": info,
            "capabilities": {
                "can_be_player": True,
                "can_be_remote_controller": False,
            },
        }

    def _build_initial_state(self) -> dict[str, Any]:
        """Build initial player state (paused, empty queue)."""
        device_id = self._device_info.device_id
        return {
            "status": {
                "paused": True,
                "duration_ms": "0",
                "progress_ms": "0",
                "playback_speed": 1,
                "version": make_version_block(device_id),
            },
            "player_queue": {
                "current_playable_index": -1,
                "entity_id": "",
                "entity_type": "VARIOUS",
                "playable_list": [],
                "options": {"repeat_mode": "NONE"},
                "entity_context": "BASED_ON_ENTITY_BY_DEFAULT",
                "version": make_version_block(device_id),
                "from_optional": "",
            },
        }

    async def _get_redirect_ticket(self) -> tuple[str, str, int]:
        """
        Connect to redirector and obtain redirect ticket.

        :return: (host, redirect_ticket, session_id)
        :raises LoginFailed: If authentication fails.
        """
        if self._session is None:
            raise RuntimeError("HTTP session not initialized — call connect() first")
        headers = self._build_headers()

        ws_timeout = aiohttp.ClientWSTimeout(ws_close=WS_CONNECT_TIMEOUT)
        try:
            ws = await self._session.ws_connect(
                YNISON_REDIRECT_URL,
                headers=headers,
                timeout=ws_timeout,
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise LoginFailed("Ynison authentication failed — invalid token") from err
            raise

        try:
            msg = await ws.receive(timeout=WS_CONNECT_TIMEOUT)
            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                data = json.loads(msg.data)
            else:
                raise ConnectionError(f"Unexpected message type from redirector: {msg.type}")
        finally:
            await ws.close()

        host = data.get("host", "")
        ticket = data.get("redirect_ticket", "")
        session_id = int(data.get("session_id", 0))
        keepalive = data.get("keep_alive_params")
        interval = (
            parse_seconds(keepalive.get("keep_alive_time_seconds"))
            if isinstance(keepalive, dict)
            else 0.0
        )
        # A tiny server value would make aiohttp ping and time out aggressively.
        self._heartbeat = max(interval, WS_MIN_HEARTBEAT) if interval else WS_HEARTBEAT

        if not host or not ticket:
            raise LoginFailed("Redirector response missing host or ticket")

        self._logger.debug("Ynison redirect: host=%s, session_id=%d", host, session_id)
        return host, ticket, session_id

    async def _connect_state(self, host: str, ticket: str, session_id: int) -> None:
        """Connect to Ynison state service and start message loop."""
        if self._session is None:
            raise RuntimeError("HTTP session not initialized — call connect() first")
        self._reset_pending_queue()
        url = f"wss://{host}{YNISON_STATE_PATH}"
        headers = self._build_headers(redirect_ticket=ticket, session_id=session_id)

        ws_timeout = aiohttp.ClientWSTimeout(ws_close=WS_CONNECT_TIMEOUT)
        try:
            self._ws = await self._session.ws_connect(
                url, headers=headers, timeout=ws_timeout, heartbeat=self._heartbeat
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise LoginFailed("Ynison authentication failed — invalid token") from err
            raise
        self._connected = True
        self._logger.info("Connected to Ynison state service at %s", host)

        # Always send a fresh initial state (empty/paused) — both on cold
        # start and reconnect (v2.0). The previous behaviour replayed
        # `self.state.player_state`, which after a heartbeat could carry
        # `paused=True` and trigger an unintended pause on the still-running
        # player when Ynison broadcast it back to us.
        # If a player is already active (handoff in progress), the provider
        # will reclaim ownership via `update_active_device` after the
        # post-reconnect settle window expires.
        if self._has_connected_once:
            self._logger.info("Reconnect: sending fresh initial state (no stale replay)")
            self._post_reconnect_settle_until = time.monotonic() + 2.0
        await self.send_full_state()
        # Best-effort: ask the server not to forward peer events while we
        # are passive. Failure is non-fatal — we just receive more events.
        try:
            await self.update_session_params(mute_events_if_passive=True)
        except aiohttp.ClientError, OSError, TimeoutError:
            self._logger.debug("update_session_params failed", exc_info=True)

        self._has_connected_once = True

        # Start message loop
        self._message_task = asyncio.create_task(self._message_loop(), name="ynison_message_loop")
        self._message_task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        """Report unexpected background failures with their original traceback."""
        if task.cancelled():
            return
        if (error := task.exception()) is not None:
            self._logger.error(
                "Unexpected error in Ynison task %s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _message_loop(self) -> None:  # noqa: PLR0915
        """Read messages from state service and dispatch callbacks."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected — call connect() first")
        socket = self._ws
        cancelled = False
        server_directive = False
        try:
            async for msg in socket:
                if self._stop_event.is_set():
                    break

                if msg.type == aiohttp.WSMsgType.ERROR:
                    msg_data_preview = str(socket.exception())
                elif not msg.data:
                    msg_data_preview = "<empty>"
                elif isinstance(msg.data, str):
                    msg_data_preview = msg.data[:500]
                elif isinstance(msg.data, bytes):
                    msg_data_preview = msg.data[:500].decode(errors="replace")
                else:
                    msg_data_preview = str(msg.data)

                self._logger.debug(
                    "Ynison msg type=%s, data=%s",
                    msg.type,
                    msg_data_preview,
                )

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        data = None
                    if not isinstance(data, dict):
                        self._logger.warning(
                            "Failed to parse Ynison message: %s",
                            msg.data[:200] if msg.data else "<empty>",
                        )
                        continue

                    if "error" in data:
                        server_directive = True
                        if self._apply_error_frame(data["error"]):
                            break
                        continue

                    await self._dispatch_state(data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self._logger.debug(
                        "Ynison binary message (%d bytes)", len(msg.data) if msg.data else 0
                    )
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self._logger.warning("Ynison WebSocket error: %s", socket.exception())
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    self._logger.debug(
                        "Ynison WS close: type=%s, close_code=%s, extra=%s",
                        msg.type,
                        socket.close_code,
                        msg.extra,
                    )
                    break
        except asyncio.CancelledError:
            cancelled = True
            return
        except aiohttp.ClientError, OSError, TimeoutError:
            self._logger.exception("Unexpected error in Ynison message loop")
        finally:
            if self._ws is socket:
                self._connected = False
            if not socket.closed:
                with suppress(aiohttp.ClientError, OSError, TimeoutError):
                    await socket.close()
            if self._ws is socket and not self._stop_event.is_set() and not cancelled:
                if not server_directive:
                    self._policy.on_transport_failure()
                self._schedule_reconnect()
        self._logger.debug("Ynison message loop exited")

    def _apply_error_frame(self, error_info: dict[str, Any]) -> bool:
        """
        Hand a Ynison error response to the reconnect policy.

        :param error_info: The ``error`` object of the response.
        :return: ``True`` when the session must end now.
        """
        details = error_info.get("details", {})
        error_code = details.get("ynison-error-code", "")
        self._logger.warning("Ynison error response: %s", json.dumps(error_info)[:300])
        go_away = parse_seconds(details.get("ynison-go-away-for-seconds"))
        grpc_code = error_info.get("grpc_code")
        self._policy.on_server_error(
            error_code,
            grpc_code=grpc_code if isinstance(grpc_code, int) else None,
            go_away=go_away,
            ladder=details.get("ynison-backoff-millis"),
        )
        if go_away:
            self._logger.warning(
                "Yandex rejected the Ynison connection (error %s) and asked to wait %.0f s. "
                "This device will not appear in the Yandex Music app until the next attempt "
                "at %s; reconnecting earlier would extend the wait",
                error_code,
                go_away,
                _local_time(time.time() + go_away),
            )
            return True
        if error_code in YNISON_RECONNECT_ERROR_CODES:
            self._logger.info(
                "Ynison re-balance error %s — breaking for immediate reconnect", error_code
            )
            return True
        return False

    async def _dispatch_state(self, data: dict[str, Any]) -> None:
        """Apply one state frame and notify the state callback."""
        was_active = self.state.active_device_id == self.device_id
        self._parse_state(data)
        self._policy.on_healthy()
        if was_active and self.state.active_device_id != self.device_id:
            # The server clears this flag when the device activates.
            await self.update_session_params(mute_events_if_passive=True)
        try:
            await self._on_state_update(self.state)
        except MusicAssistantError:
            self._logger.exception("Error in Ynison state update callback")

    def _reset_pending_queue(self) -> None:
        """Discard outbound queue edits when a peer or a new connection takes over."""
        self._pending_queue = None
        self._pending_queue_until = 0.0
        self._last_queue_version = None
        self._unadopted_navigation = None
        self._queue_epoch += 1

    def _accept_queue(self, queue: dict[str, Any], current: dict[str, Any] | None) -> bool:
        """
        Track the lineage and our confirmed versions for an inbound queue.

        :return: ``False`` for an echo of our own queue older than our newest send.
        """
        version = queue.get("version", {})
        number = str(version.get("version", ""))
        if version.get("device_id") != self.device_id:
            if queue != current and _same_playback(queue, current):
                # A peer option edit keeps the playback; it only replaces our queue base.
                self._pending_queue = None
            elif queue != current:
                self._queue_lineage += 1
                self._reset_pending_queue()
            return True
        if number.isdigit():
            self._confirmed_queue_version = max(self._confirmed_queue_version or 0, int(number))
        if self._last_queue_version is not None:
            if number.isdigit() and int(number) < self._last_queue_version:
                return False
            self._pending_queue = None
        # A later own edit carries a navigation whose own echo was skipped.
        navigated = self._unadopted_navigation is not None and (
            not number.isdigit() or int(number) >= self._unadopted_navigation
        )
        if navigated:
            self._unadopted_navigation = None
        self.state.current_item_navigated = navigated
        self.state.current_item_kept = (
            not navigated
            and current is not None
            and _current_playable_id(queue) == _current_playable_id(current)
        )
        return True

    def _parse_state(self, data: dict[str, Any]) -> None:
        """Parse PutYnisonStateResponse into YnisonState."""
        old_track = self.state.current_track_id
        old_active_device = self.state.active_device_id
        old_index = self.state.player_state.get("player_queue", {}).get(
            "current_playable_index", -1
        )

        # Replace each incoming player_state sub-object at the top level:
        # Ynison sends entries like "player_queue" and "status" as complete
        # objects, so merging nested dicts would retain stale keys that are
        # absent from the update.
        incoming_ps = data.get("player_state")
        self.state.current_item_kept = False
        self.state.current_item_navigated = False
        # A frame without a status keeps the retained one, which is already known.
        self.state.status_is_relay = not (isinstance(incoming_ps, dict) and "status" in incoming_ps)
        if incoming_ps is not None:
            # Normalize timestamp fields before storing: Ynison rejects int
            # `status.progress_ms`/`duration_ms`/`version.*` on outbound
            # messages, and stored state is round-tripped via send_full_state
            # (on reconnect) and update_player_state (on queue edits).
            normalize_player_state_timestamps(incoming_ps)
            is_echo, suppress_status = self._classify_state_as_echo(incoming_ps)
            existing_ps = self.state.player_state
            for key, value in incoming_ps.items():
                if key == "player_queue" and not self._accept_queue(
                    value, existing_ps.get("player_queue")
                ):
                    continue
                if key == "status":
                    self.state.status_is_relay = self._is_known_status(value)
                if suppress_status and key == "status":
                    continue
                if key == "status" and not self.state.status_is_relay:
                    # A relay keeps the anchor its position is extrapolated from.
                    self._remember_server_status(value)
                    # A newer status supersedes the one our pending navigation sent.
                    self._navigation_status = None
                existing_ps[key] = value
            if (
                self.state.current_item_navigated
                and "status" not in incoming_ps
                and self._navigation_status is not None
            ):
                # A queue-only echo of our navigation starts where that navigation said.
                existing_ps["status"] = deepcopy(self._navigation_status)
            self.state.last_update_is_echo = is_echo
        else:
            self.state.last_update_is_echo = False
        incoming_status_author = (
            incoming_ps.get("status", {}).get("version", {}).get("device_id")
            if isinstance(incoming_ps, dict)
            else None
        )
        if (
            "active_device_id_optional" not in data
            and incoming_status_author == YNISON_DEVICE_SERVER_DISCONNECT
        ):
            self.state.active_device_id = None
        else:
            self.state.active_device_id = data.get(
                "active_device_id_optional", self.state.active_device_id
            )
        if self.state.active_device_id != old_active_device:
            self._reset_pending_queue()
        self.state.devices = data.get("devices", self.state.devices)

        new_track = self.state.current_track_id
        queue = self.state.player_state.get("player_queue", {})
        new_index = queue.get("current_playable_index", -1)
        queue_len = len(queue.get("playable_list", []))
        entity_type = queue.get("entity_type", "")

        item_changed = self.state.current_item_navigated or (
            (old_track != new_track or old_index != new_index) and not self.state.current_item_kept
        )
        if item_changed and not (incoming_ps and "status" in incoming_ps):
            # A new item without its own status must not inherit the old baseline.
            self._status_baseline = None
        if old_track != new_track or old_index != new_index:
            self._logger.info(
                "Ynison queue change: track %s→%s index %d→%d queue_len=%d entity_type=%s",
                old_track,
                new_track,
                old_index,
                new_index,
                queue_len,
                entity_type,
            )
        else:
            self._logger.debug(
                "Ynison state update (no queue change): track=%s index=%d progress=%dms paused=%s",
                new_track,
                new_index,
                self.state.progress_ms,
                self.state.is_paused,
            )

    async def _reconnect(self) -> None:
        """
        Reconnect on the reconnect policy's schedule, retrying indefinitely.

        On authentication failure (LoginFailed), attempts to refresh the token
        via the on_auth_failure callback before the next retry. The loop only
        exits when `_stop_event` is set (via disconnect()) or on successful
        reconnection; a reliable long-running plugin never permanently gives up.
        """
        attempt = 0
        refresh_attempted = False
        while not self._stop_event.is_set():
            delay = self._policy.next_delay()
            self._logger.info("Ynison reconnect attempt %d in %.1fs", attempt + 1, delay)
            await asyncio.sleep(delay)

            if self._stop_event.is_set():
                return
            if self._policy.go_away_pending:
                # A go-away arrived during this wait; dialing now would land in its window.
                continue

            attempt += 1
            try:
                await self._close_socket()

                # Re-create session if needed
                if self._session is None or self._session.closed:
                    if self._external_session is not None:
                        if self._external_session.closed:
                            msg = "External HTTP session is closed"
                            raise ResourceTemporarilyUnavailable(msg)  # noqa: TRY301 - handled by except below
                        self._session = self._external_session
                    else:
                        # ma-allow: MA001 standalone fallback without mass.http_session
                        self._session = aiohttp.ClientSession()

                host, ticket, session_id = await self._get_redirect_ticket()
                await self._connect_state(host, ticket, session_id)
                self._logger.info("Ynison reconnected successfully")
                return
            except LoginFailed:
                self._policy.on_transport_failure()
                self._logger.warning("Ynison reconnect attempt %d failed: auth error", attempt)
                if self._on_auth_failure and not refresh_attempted:
                    try:
                        new_token = await self._on_auth_failure()
                        self._token = new_token
                        refresh_attempted = True
                        self._logger.info("Token refreshed, will retry with new token")
                    except ResourceTemporarilyUnavailable:
                        self._logger.warning(
                            "Token refresh temporarily unavailable, will retry after backoff",
                            exc_info=True,
                        )
                    except LoginFailed:
                        refresh_attempted = True
                        self._logger.warning("Token refresh permanently failed", exc_info=True)
            except asyncio.CancelledError:
                return
            except aiohttp.ClientError, OSError, TimeoutError, ResourceTemporarilyUnavailable:
                self._policy.on_transport_failure()
                self._logger.warning("Ynison reconnect attempt %d failed", attempt, exc_info=True)

    async def _close_socket(self) -> None:
        """Close and forget the state WebSocket."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _close_session(self) -> None:
        """Close an HTTP session this client created and forget it."""
        if self._session and not self._external_session:
            await self._session.close()
        self._session = None

    def _player_state_message(self, player_state: dict[str, Any]) -> dict[str, Any]:
        """Build a complete queue update envelope."""
        queue = player_state.get("player_queue", {})
        self._logger.info(
            "→ update_player_state: index=%s queue_len=%d entity_type=%s",
            queue.get("current_playable_index"),
            len(queue.get("playable_list", [])),
            queue.get("entity_type", ""),
        )
        msg = {
            "update_player_state": {
                "player_state": player_state,
            },
            **self._message_meta(),
        }
        self._logger.debug("Sending player state: %s", json.dumps(msg)[:500])
        return msg

    async def _send(self, msg: dict[str, Any], *, strict: bool = False) -> bool:
        """
        Send a JSON message to the state service (thread-safe).

        :param msg: JSON-serialisable Ynison envelope.
        :param strict: When ``True``, transport failures (disconnected socket
            or write error) raise :class:`YnisonSendError` after scheduling a
            reconnect. Default is the legacy fire-and-forget behaviour:
            log + schedule reconnect + return.
        """
        if strict and not self._channel_ready():
            await self.wait_connected(STRICT_SEND_WAIT)
        async with self._send_lock:
            return await self._send_locked(msg, strict=strict)

    async def _send_locked(self, msg: dict[str, Any], *, strict: bool) -> bool:
        """Send one envelope while the caller holds the transport lock."""
        if self._ws is None or self._ws.closed:
            self._logger.debug("Cannot send to Ynison — not connected")
            if strict:
                raise YnisonSendError("Ynison WebSocket not connected")
            return False
        try:
            await self._ws.send_str(json.dumps(msg))
            return True
        except (ConnectionError, aiohttp.ClientError, RuntimeError, OSError) as exc:
            self._logger.warning("Failed to send message to Ynison, scheduling reconnect")
            self._connected = False
            self._schedule_reconnect()
            if strict:
                raise YnisonSendError("Ynison send failed") from exc
            return False

    def _schedule_reconnect(self) -> None:
        """
        Schedule a background reconnect attempt if none is already in flight.

        Idempotent: a single reconnect task is in flight at any time. Becomes
        a no-op once :meth:`disconnect` has set ``_stop_event``.
        """
        if self._stop_event.is_set():
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect(), name="ynison_reconnect")
        self._reconnect_task.add_done_callback(self._on_background_task_done)


def generate_device_id() -> str:
    """Generate a 16-character hex device ID for Ynison registration."""
    return secrets.token_hex(8)
