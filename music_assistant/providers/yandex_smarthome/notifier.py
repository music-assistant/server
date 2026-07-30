"""
State notifier — reports MA player state changes to Yandex Smart Home.

Watches MA player events and pushes state updates to Yandex via the
callback/state API endpoint (cloud or direct). Uses a 1-second debounce
window to batch rapid state changes into a single callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

from music_assistant_models.enums import EventType

from .constants import (
    STATE_HEARTBEAT_INTERVAL,
    STATE_INITIAL_REPORT_DELAY,
    STATE_REPORT_DELAY,
)
from .device import get_device_state, is_player_exposable
from .handlers import _strip_none
from .schema import CallbackPayload, CallbackRequest, DeviceState

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)


class _CallbackErrorAlreadyLogged(RuntimeError):
    """
    Sentinel for state-callback errors that have already been logged.

    Raised by ``_send_state_callback`` after dedupe-aware logging so the
    outer exception handler can re-queue (via ``_flush_pending``) without
    emitting a second log line.
    """


class StateNotifier:
    """Watches MA player events and reports state changes to Yandex."""

    def __init__(
        self,
        mass: MusicAssistant,
        session: aiohttp.ClientSession,
        user_id: str,
        callback_url: str,
        auth_header: dict[str, str],
        logger: logging.Logger | None = None,
        exposed_ids: set[str] | None = None,
        playlist_uris: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Initialize state notifier."""
        self._mass = mass
        self._session = session
        self._user_id = user_id
        self._callback_url = callback_url
        self._auth_header = auth_header
        self._logger = logger or _LOGGER
        self._exposed_ids = exposed_ids
        self._playlist_uris = tuple(playlist_uris)

        self._dirty_player_ids: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._initial_report_handle: asyncio.TimerHandle | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._unsub: Callable[[], None] | None = None

        # Dedupe state-callback errors by fingerprint. Yandex's backend
        # returns transient HTTP 5xx for ~1-2 minutes after a freshly
        # created skill (CDN warmup), then 400 + UNKNOWN_USER until the
        # user links the skill in the mobile app. Without dedupe each
        # 1 s flush logs a full traceback — flooding the log. First
        # occurrence per fingerprint logs as follows:
        #   - UNKNOWN_USER, HTTP 5xx → WARNING (expected first-run state,
        #     no traceback — see _emit_callback_error)
        #   - transport / unexpected errors → ERROR + traceback (real
        #     bugs worth diagnostic detail — see outer except)
        # Repeats with the same fingerprint drop to DEBUG until a
        # different error class arrives or a successful callback resets
        # the fingerprint (which then logs an INFO recovery line).
        self._last_error_fingerprint: str | None = None

    async def start(self) -> None:
        """Subscribe to player events and start background tasks."""
        self._unsub = self._mass.subscribe(
            self._on_player_event,
            event_filter=(
                EventType.PLAYER_UPDATED,
                EventType.PLAYER_ADDED,
                EventType.PLAYER_REMOVED,
            ),
        )

        # Schedule initial full state report after startup delay
        self._initial_report_handle = self._mass.loop.call_later(
            STATE_INITIAL_REPORT_DELAY,
            lambda: self._mass.create_task(self._report_all_states()),
        )

        # Periodic heartbeat
        self._heartbeat_task = self._mass.create_task(
            self._heartbeat_loop(), task_id="yandex_smarthome_heartbeat"
        )

        self._logger.info("State notifier started (callback=%s)", self._callback_url)

    async def stop(self) -> None:
        """Unsubscribe from events and cancel background tasks."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._initial_report_handle:
            self._initial_report_handle.cancel()
            self._initial_report_handle = None
        if self._flush_handle:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._heartbeat_task is not None:
            if not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
            self._heartbeat_task = None
        self._dirty_player_ids.clear()
        self._logger.info("State notifier stopped")

    # -----------------------------------------------------------------------
    # Event handling
    # -----------------------------------------------------------------------

    def _on_player_event(self, event: MassEvent) -> None:
        """Handle player state change — mark player as dirty for batched reporting."""
        if event.event in (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED):
            self._schedule_discovery()
            return

        # PLAYER_UPDATED — event.data is the Player (state) dataclass
        player_state = event.data
        if player_state is None:
            return

        # If this player is synced to a group, propagate the event to the group
        # (child volume/mute changes should update the group's state in Yandex)
        synced_to = getattr(player_state, "synced_to", None)
        if synced_to:
            self._dirty_player_ids.add(synced_to)
            self._schedule_flush()
            return

        if not is_player_exposable(player_state, exposed_ids=self._exposed_ids):
            return

        self._dirty_player_ids.add(player_state.player_id)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        """Schedule a batched state flush after the debounce window."""
        if self._flush_handle is not None:
            return  # already scheduled
        self._flush_handle = self._mass.loop.call_later(
            STATE_REPORT_DELAY,
            lambda: self._mass.create_task(self._flush_pending()),
        )

    async def _flush_pending(self) -> None:
        """
        Send all pending state changes to Yandex.

        Reads the fresh player state at flush time (not at event time)
        so transient states during track transitions are not reported.
        """
        self._flush_handle = None
        if not self._dirty_player_ids:
            return
        dirty = self._dirty_player_ids
        self._dirty_player_ids = set()

        devices: list[DeviceState] = []
        for player_id in dirty:
            player = self._mass.players.get_player(player_id)
            if player is None:
                continue
            state = player.state
            if is_player_exposable(state, exposed_ids=self._exposed_ids):
                devices.append(get_device_state(state, playlist_uris=self._playlist_uris))

        if not devices:
            return
        try:
            await self._send_state_callback(devices)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Re-queue failed player IDs and reschedule. _send_state_callback
            # already deduplicated the log entry (WARNING for known classes,
            # ERROR-with-traceback for unexpected) so we swallow the exception
            # here to keep MA's task scheduler from re-logging it as
            # "Task exception was never retrieved" on every retry.
            self._dirty_player_ids |= dirty
            self._schedule_flush()

    # -----------------------------------------------------------------------
    # State reporting
    # -----------------------------------------------------------------------

    def _emit_callback_error(self, fingerprint: str, warn_message: str) -> None:
        """
        Log a state-callback error once per fingerprint, then DEBUG.

        Different fingerprint classes (UNKNOWN_USER, HTTP 5xx, transport
        failures) each emit a single WARNING the first time they occur,
        then drop to DEBUG until a different error class arrives or a
        successful callback resets the fingerprint.
        """
        if self._last_error_fingerprint == fingerprint:
            self._logger.debug("State callback still failing (%s)", fingerprint)
            return
        self._last_error_fingerprint = fingerprint
        self._logger.warning("%s", warn_message)

    async def _send_state_callback(self, devices: list[DeviceState]) -> None:
        """
        POST state callback to Yandex.

        Yandex's callback endpoint can fail three ways: HTTP 5xx while
        the skill propagates through their CDN, HTTP 400 + UNKNOWN_USER
        until the user links the skill in the mobile app, and
        transport-level errors during network issues. All three are
        deduped via ``_last_error_fingerprint`` so each class only logs
        once per "episode" — UNKNOWN_USER and 5xx at WARNING (expected
        first-run states), transport / unexpected errors at ERROR with
        traceback (real bugs worth diagnostic detail). Repeats drop to
        DEBUG until something changes.
        """
        payload = CallbackRequest(
            ts=time.time(),
            payload=CallbackPayload(user_id=self._user_id, devices=devices),
        )
        try:
            async with self._session.post(
                self._callback_url,
                json=_strip_none(asdict(payload)),
                headers=self._auth_header,
            ) as resp:
                if resp.status in (200, 202):
                    if self._last_error_fingerprint is not None:
                        self._logger.info(
                            "State callback recovered (was failing with %s)",
                            self._last_error_fingerprint,
                        )
                        self._last_error_fingerprint = None
                    self._logger.debug("State callback sent: %d device(s)", len(devices))
                    return

                body = await resp.text()
                if resp.status == 400 and "UNKNOWN_USER" in body:
                    self._emit_callback_error(
                        "unknown_user",
                        "Yandex returned UNKNOWN_USER for state callback — the skill is "
                        "not yet linked to a Yandex account. Open "
                        "https://yandex.ru/quasar/iot or the «Дом с Алисой» app, find "  # noqa: RUF001
                        "the skill in Devices → +, and tap «Связать аккаунт». Further "
                        "callback errors will be suppressed at debug level until linking "
                        "succeeds.",
                    )
                    return  # silent — not a real error, don't raise

                if 500 <= resp.status < 600:
                    # Transient Yandex backend issue — common for ~1-2 min
                    # after a freshly created skill while CDN propagates.
                    # Dedupe the WARNING but still raise so _flush_pending
                    # re-queues the dirty players for the next attempt.
                    self._emit_callback_error(
                        f"http_{resp.status}",
                        f"State callback failed with HTTP {resp.status} — Yandex backend "
                        "may be propagating a freshly created skill. Further callback "
                        "errors will be suppressed at debug level until the next "
                        f"successful callback. Body: {body[:200]}",
                    )
                    raise _CallbackErrorAlreadyLogged(
                        f"State callback failed with HTTP {resp.status}"
                    )

                raise RuntimeError(f"State callback failed with HTTP {resp.status}: {body[:200]}")
        except asyncio.CancelledError:
            # Cooperative cancellation must propagate untouched.
            raise
        except _CallbackErrorAlreadyLogged:
            # Already deduped via _emit_callback_error above — just propagate
            # so _flush_pending re-queues without a second log entry.
            raise
        except Exception as exc:
            # Transport-level errors (aiohttp.ClientError, DNS resolution
            # failures, connection resets, etc.) plus the catch-all RuntimeError
            # for non-5xx HTTP failures. Dedupe by exception class name so a
            # repeat of the same transport failure doesn't flood the log.
            fingerprint = type(exc).__name__
            if self._last_error_fingerprint == fingerprint:
                self._logger.debug("State callback still failing (%s)", fingerprint)
            else:
                self._last_error_fingerprint = fingerprint
                self._logger.exception("State callback error")
            raise

    async def _report_all_states(self) -> None:
        """Report states for all currently exposed players."""
        devices: list[DeviceState] = []
        for player in self._mass.players.all_players():
            state = player.state
            if is_player_exposable(state, exposed_ids=self._exposed_ids):
                devices.append(get_device_state(state, playlist_uris=self._playlist_uris))
        if devices:
            self._logger.info("Reporting all states: %d device(s)", len(devices))
            await self._send_state_callback(devices)

    async def _heartbeat_loop(self) -> None:
        """Periodically report all states as a heartbeat."""
        while True:
            try:
                await asyncio.sleep(STATE_HEARTBEAT_INTERVAL)
                await self._report_all_states()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Heartbeat state report failed, will retry next interval")

    # -----------------------------------------------------------------------
    # Discovery notification
    # -----------------------------------------------------------------------

    def _schedule_discovery(self) -> None:
        """Notify Yandex that the device list has changed."""
        self._mass.create_task(self._send_discovery())

    async def _send_discovery(self) -> None:
        """POST discovery notification to Yandex."""
        discovery_url = self._callback_url.removesuffix("/state") + "/discovery"
        payload = {
            "ts": time.time(),
            "payload": {"user_id": self._user_id},
        }
        try:
            async with self._session.post(
                discovery_url,
                json=payload,
                headers=self._auth_header,
            ) as resp:
                if resp.status not in (200, 202):
                    body = await resp.text()
                    self._logger.warning(
                        "Discovery callback failed (HTTP %d): %s", resp.status, body[:200]
                    )
                else:
                    self._logger.debug("Discovery notification sent")
        except Exception:
            self._logger.exception("Discovery callback error")
