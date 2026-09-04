"""Ynison WebSocket client for Yandex Music device synchronization."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable

if TYPE_CHECKING:
    from ya_passport_auth import SecretStr

from .constants import (
    DEFAULT_APP_NAME,
    DEFAULT_APP_VERSION,
    DEVICE_TYPE_WEB,
    RECONNECT_DELAYS,
    WS_CONNECT_TIMEOUT,
    WS_HEARTBEAT,
    YNISON_DEVICE_SERVER_DISCONNECT,
    YNISON_ORIGIN,
    YNISON_RECONNECT_ERROR_CODES,
    YNISON_REDIRECT_URL,
    YNISON_STATE_PATH,
)
from .queue_model import (
    YnisonQueueView,
    insert_shuffle_indices,
    move_shuffle_index,
    remove_shuffle_index,
)


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


class YnisonEmptyRedirectError(ConnectionError):
    """Raised when the redirector accepts a token but returns no connection ticket."""


def make_version_block(device_id: str) -> dict[str, Any]:
    """
    Build a version sub-object authored by the given device.

    Ynison expects string types for version and timestamp fields;
    passing integers triggers 500 responses that terminate the WebSocket.
    """
    return {
        "device_id": device_id,
        "version": str(time.time_ns()),
        "timestamp_ms": "0",
    }


def _stringify_version(version: Any) -> None:
    """Coerce int `version.version`/`version.timestamp_ms` fields to str in-place."""
    if not isinstance(version, dict):
        return
    for key in ("version", "timestamp_ms"):
        val = version.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            version[key] = str(val)


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
        self._message_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected = False
        self._has_connected_once = False
        self._status_watermarks: deque[_StatusWatermark] = deque(maxlen=8)

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

    async def connect(self) -> None:
        """
        Connect to Ynison (redirector → state service).

        Raises on auth failure; auto-reconnects on transient errors.
        """
        self._stop_event.clear()
        if self._external_session and self._external_session.closed:
            raise RuntimeError("Provided http_session is closed")
        self._session = self._external_session or aiohttp.ClientSession()

        try:
            # Step 1: Get redirect ticket
            host, ticket, session_id = await self._get_redirect_ticket()

            # Step 2: Connect to state service
            await self._connect_state(host, ticket, session_id)
        except LoginFailed:
            await self.disconnect()
            raise
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except Exception:
            # Transient error — schedule reconnect instead of dying
            self._logger.warning("Initial connection failed, scheduling reconnect", exc_info=True)
            self._connected = False
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._ws = None
            if self._session and not self._external_session:
                await self._session.close()
            self._session = None
            self._schedule_reconnect()

    async def disconnect(self) -> None:
        """Gracefully disconnect from Ynison."""
        self._stop_event.set()
        self._connected = False

        if self._message_task and not self._message_task.done():
            self._message_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._message_task

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._external_session:
            await self._session.close()
        self._session = None

    def update_token(self, token: SecretStr) -> None:
        """Replace the stored OAuth token (e.g. after a refresh)."""
        self._token = token

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
    ) -> None:
        """
        Send playback status update to Ynison.

        :param progress_ms: Current playback position in milliseconds.
        :param duration_ms: Current track duration in milliseconds.
        :param paused: Whether playback is paused.
        :param strict: When ``True``, raise :class:`YnisonSendError` on
            transport failure instead of silently scheduling a reconnect.
            Delivery-critical callers (user commands, end-of-track signal)
            opt in; heartbeat callers leave the default.
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
        }
        if await self._send(msg, strict=strict):
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

    async def update_active_device(self, device_id: str) -> None:
        """Request playback transfer to this device."""
        msg = {
            "update_active_device": {
                "device_id_optional": device_id,
            },
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
        await self._send(msg, strict=strict)

    async def add_playables_next(self, playables: list[dict[str, Any]]) -> None:
        """Insert playables immediately after the current queue position."""
        player_state = deepcopy(self.state.player_state)
        queue = player_state.get("player_queue")
        if not isinstance(queue, dict):
            raise ValueError("Current player state has no queue")  # noqa: TRY004
        playable_list = queue.get("playable_list")
        if not isinstance(playable_list, list):
            raise ValueError("Current player queue has no playable list")  # noqa: TRY004
        current_index = queue.get("current_playable_index")
        if not isinstance(current_index, int) or not 0 <= current_index < len(playable_list):
            raise ValueError("Current playable index is out of range")

        shuffle = queue.get("shuffle_optional")
        order = list(YnisonQueueView(queue).order)
        playable_list[current_index + 1 : current_index + 1] = deepcopy(playables)
        if isinstance(shuffle, dict) and isinstance(shuffle.get("playable_indices"), list):
            shuffle["playable_indices"] = insert_shuffle_indices(
                order,
                current_index + 1,
                len(playables),
                after_current=current_index,
            )
        queue["version"] = make_version_block(self.device_id)
        await self.update_player_state(player_state, strict=True)

    async def add_playables_last(self, playables: list[dict[str, Any]]) -> None:
        """Append playables to the current queue."""
        player_state = deepcopy(self.state.player_state)
        queue = player_state.get("player_queue")
        if not isinstance(queue, dict):
            raise ValueError("Current player state has no queue")  # noqa: TRY004
        playable_list = queue.get("playable_list")
        if not isinstance(playable_list, list):
            raise ValueError("Current player queue has no playable list")  # noqa: TRY004
        current_index = queue.get("current_playable_index")
        if not isinstance(current_index, int) or not (
            (current_index == -1 and not playable_list) or 0 <= current_index < len(playable_list)
        ):
            raise ValueError("Current playable index is out of range")

        shuffle = queue.get("shuffle_optional")
        order = list(YnisonQueueView(queue).order)
        playable_list.extend(deepcopy(playables))
        if isinstance(shuffle, dict) and isinstance(shuffle.get("playable_indices"), list):
            shuffle["playable_indices"] = insert_shuffle_indices(
                order, len(playable_list) - len(playables), len(playables)
            )
        queue["version"] = make_version_block(self.device_id)
        await self.update_player_state(player_state, strict=True)

    async def remove_queue_position(self, position: int) -> None:
        """Remove an original playable-list position from the current queue."""
        player_state = deepcopy(self.state.player_state)
        queue = player_state.get("player_queue")
        if not isinstance(queue, dict):
            raise ValueError("Current player state has no queue")  # noqa: TRY004
        playable_list = queue.get("playable_list")
        if not isinstance(playable_list, list):
            raise ValueError("Current player queue has no playable list")  # noqa: TRY004
        if not isinstance(position, int) or not 0 <= position < len(playable_list):
            raise ValueError("Queue position is out of range")
        current_index = queue.get("current_playable_index")
        if not isinstance(current_index, int) or not 0 <= current_index < len(playable_list):
            raise ValueError("Current playable index is out of range")

        shuffle = queue.get("shuffle_optional")
        order = list(YnisonQueueView(queue).order)
        logical_successor = None
        if position == current_index:
            logical_position = order.index(current_index)
            if logical_position + 1 < len(order):
                logical_successor = order[logical_position + 1]
        playable_list.pop(position)
        if position < current_index:
            queue["current_playable_index"] = current_index - 1
        elif position == current_index:
            if logical_successor is not None:
                queue["current_playable_index"] = (
                    logical_successor - 1 if logical_successor > position else logical_successor
                )
            else:
                queue["current_playable_index"] = min(current_index, len(playable_list) - 1)
            status = player_state.get("status")
            if isinstance(status, dict):
                status["progress_ms"] = "0"
                status["duration_ms"] = "0"
                status["version"] = make_version_block(self.device_id)
        if isinstance(shuffle, dict) and isinstance(shuffle.get("playable_indices"), list):
            shuffle["playable_indices"] = remove_shuffle_index(order, position)
        queue["version"] = make_version_block(self.device_id)
        await self.update_player_state(player_state, strict=True)

    async def move_queue_position(self, from_position: int, to_position: int) -> None:
        """Move a playable between original playable-list positions."""
        player_state = deepcopy(self.state.player_state)
        queue = player_state.get("player_queue")
        if not isinstance(queue, dict):
            raise ValueError("Current player state has no queue")  # noqa: TRY004
        playable_list = queue.get("playable_list")
        if not isinstance(playable_list, list):
            raise ValueError("Current player queue has no playable list")  # noqa: TRY004
        if (
            not isinstance(from_position, int)
            or not isinstance(to_position, int)
            or not 0 <= from_position < len(playable_list)
            or not 0 <= to_position < len(playable_list)
        ):
            raise ValueError("Queue position is out of range")
        current_index = queue.get("current_playable_index")
        if not isinstance(current_index, int) or not 0 <= current_index < len(playable_list):
            raise ValueError("Current playable index is out of range")

        shuffle = queue.get("shuffle_optional")
        order = list(YnisonQueueView(queue).order)
        playable = playable_list.pop(from_position)
        playable_list.insert(to_position, playable)
        if current_index == from_position:
            queue["current_playable_index"] = to_position
        elif from_position < current_index <= to_position:
            queue["current_playable_index"] = current_index - 1
        elif to_position <= current_index < from_position:
            queue["current_playable_index"] = current_index + 1
        if isinstance(shuffle, dict) and isinstance(shuffle.get("playable_indices"), list):
            shuffle["playable_indices"] = move_shuffle_index(order, from_position, to_position)
        queue["version"] = make_version_block(self.device_id)
        await self.update_player_state(player_state, strict=True)

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
    def _message_meta() -> dict[str, Any]:
        """
        Return common envelope fields for state-mutating messages.

        Ynison expects string-typed timestamps; integers cause 500 responses.
        """
        return {
            "rid": str(uuid.uuid4()),
            "player_action_timestamp_ms": str(int(time.time() * 1000)),
            "activity_interception_type": "DO_NOT_INTERCEPT_BY_DEFAULT",
        }

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
        current_track = self.state.current_track_id
        now = time.monotonic()
        for watermark in reversed(self._status_watermarks):
            if (
                now - watermark.sent_at <= 2.0
                and current_track == watermark.track_id
                and abs(int(status.get("progress_ms", -2)) - watermark.progress_ms) <= 1
                and int(status.get("duration_ms", -1)) == watermark.duration_ms
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
            "Ynison-Device-Info": json.dumps({"app_name": self._device_info.app_name, "type": 1}),
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
            "is_shadow": False,
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

        if not host or not ticket:
            raise YnisonEmptyRedirectError("Redirector response missing host or ticket")

        self._logger.debug("Ynison redirect: host=%s, session_id=%d", host, session_id)
        return host, ticket, session_id

    async def _connect_state(self, host: str, ticket: str, session_id: int) -> None:
        """Connect to Ynison state service and start message loop."""
        if self._session is None:
            raise RuntimeError("HTTP session not initialized — call connect() first")
        url = f"wss://{host}{YNISON_STATE_PATH}"
        headers = self._build_headers(redirect_ticket=ticket, session_id=session_id)

        ws_timeout = aiohttp.ClientWSTimeout(ws_close=WS_CONNECT_TIMEOUT)
        try:
            self._ws = await self._session.ws_connect(
                url, headers=headers, timeout=ws_timeout, heartbeat=WS_HEARTBEAT
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
        except Exception:
            self._logger.debug("update_session_params failed", exc_info=True)

        self._has_connected_once = True

        # Start message loop
        self._message_task = asyncio.create_task(self._message_loop())

    async def _message_loop(self) -> None:  # noqa: PLR0915
        """Read messages from state service and dispatch callbacks."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected — call connect() first")
        try:
            async for msg in self._ws:
                if self._stop_event.is_set():
                    break

                if msg.type == aiohttp.WSMsgType.ERROR:
                    msg_data_preview = str(self._ws.exception())
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
                        self._logger.warning(
                            "Failed to parse Ynison message: %s",
                            msg.data[:200] if msg.data else "<empty>",
                        )
                        continue

                    if "error" in data:
                        error_info = data["error"]
                        error_code = error_info.get("details", {}).get("ynison-error-code", "")
                        self._logger.warning(
                            "Ynison error response: %s",
                            json.dumps(error_info)[:300],
                        )
                        if error_code in YNISON_RECONNECT_ERROR_CODES:
                            self._logger.info(
                                "Ynison re-balance error %s — breaking for immediate reconnect",
                                error_code,
                            )
                            break
                        continue

                    self._parse_state(data)
                    try:
                        await self._on_state_update(self.state)
                    except Exception:
                        self._logger.exception("Error in Ynison state update callback")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self._logger.debug(
                        "Ynison binary message (%d bytes)", len(msg.data) if msg.data else 0
                    )
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self._logger.warning("Ynison WebSocket error: %s", self._ws.exception())
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    self._logger.debug(
                        "Ynison WS close: type=%s, close_code=%s, extra=%s",
                        msg.type,
                        self._ws.close_code,
                        msg.extra,
                    )
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            self._logger.exception("Unexpected error in Ynison message loop")
        self._logger.debug("Ynison message loop exited")

        self._connected = False

        if not self._stop_event.is_set() and (
            self._reconnect_task is None or self._reconnect_task.done()
        ):
            self._logger.warning("Ynison connection lost, scheduling reconnect")
            self._schedule_reconnect()

    def _parse_state(self, data: dict[str, Any]) -> None:
        """Parse PutYnisonStateResponse into YnisonState."""
        old_track = self.state.current_track_id
        old_index = self.state.player_state.get("player_queue", {}).get(
            "current_playable_index", -1
        )

        # Replace each incoming player_state sub-object at the top level:
        # Ynison sends entries like "player_queue" and "status" as complete
        # objects, so merging nested dicts would retain stale keys that are
        # absent from the update.
        incoming_ps = data.get("player_state")
        if incoming_ps is not None:
            # Normalize timestamp fields before storing: Ynison rejects int
            # `status.progress_ms`/`duration_ms`/`version.*` on outbound
            # messages, and stored state is round-tripped via send_full_state
            # (on reconnect) and update_player_state (on queue edits).
            normalize_player_state_timestamps(incoming_ps)
            is_echo, suppress_status = self._classify_state_as_echo(incoming_ps)
            existing_ps = self.state.player_state
            for key, value in incoming_ps.items():
                if suppress_status and key == "status":
                    continue
                existing_ps[key] = value
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
        self.state.devices = data.get("devices", self.state.devices)

        new_track = self.state.current_track_id
        queue = self.state.player_state.get("player_queue", {})
        new_index = queue.get("current_playable_index", -1)
        queue_len = len(queue.get("playable_list", []))
        entity_type = queue.get("entity_type", "")

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
        Reconnect with exponential backoff, retrying indefinitely.

        On authentication failure (LoginFailed), attempts to refresh the token
        via the on_auth_failure callback before the next retry. The loop only
        exits when `_stop_event` is set (via disconnect()) or on successful
        reconnection; a reliable long-running plugin never permanently gives up.
        """
        attempt = 0
        refresh_attempted = False
        while not self._stop_event.is_set():
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            # Add ±20% jitter to prevent thundering-herd reconnects
            jitter = delay * 0.2 * (2 * random.random() - 1)
            delay = max(0.5, delay + jitter)
            self._logger.info("Ynison reconnect attempt %d in %.1fs", attempt + 1, delay)
            await asyncio.sleep(delay)

            if self._stop_event.is_set():
                return

            attempt += 1
            try:
                # Close stale WebSocket
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                self._ws = None

                # Re-create session if needed
                if self._session is None or self._session.closed:
                    if self._external_session is not None:
                        if self._external_session.closed:
                            msg = "External HTTP session is closed"
                            raise RuntimeError(msg)
                        self._session = self._external_session
                    else:
                        self._session = aiohttp.ClientSession()

                host, ticket, session_id = await self._get_redirect_ticket()
                await self._connect_state(host, ticket, session_id)
                self._logger.info("Ynison reconnected successfully")
                return
            except LoginFailed, YnisonEmptyRedirectError:
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
            except Exception:
                self._logger.warning("Ynison reconnect attempt %d failed", attempt, exc_info=True)

    async def _send(self, msg: dict[str, Any], *, strict: bool = False) -> bool:
        """
        Send a JSON message to the state service (thread-safe).

        :param msg: JSON-serialisable Ynison envelope.
        :param strict: When ``True``, transport failures (disconnected socket
            or write error) raise :class:`YnisonSendError` after scheduling a
            reconnect. Default is the legacy fire-and-forget behaviour:
            log + schedule reconnect + return.
        """
        async with self._send_lock:
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
        self._reconnect_task = asyncio.create_task(self._reconnect())


def generate_device_id() -> str:
    """Generate a 16-character hex device ID for Ynison registration."""
    return secrets.token_hex(8)
