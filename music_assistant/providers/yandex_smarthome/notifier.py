"""State notifier — reports MA player state changes to Yandex Smart Home.

Watches MA player events and pushes state updates to Yandex via the
callback/state API endpoint (cloud or direct). Uses a 1-second debounce
window to batch rapid state changes into a single callback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

from .constants import (
    STATE_HEARTBEAT_INTERVAL,
    STATE_INITIAL_REPORT_DELAY,
    STATE_REPORT_DELAY,
)
from .device import get_device_state, is_player_exposable
from .schema import CallbackPayload, CallbackRequest, DeviceState

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self._mass = mass
        self._session = session
        self._user_id = user_id
        self._callback_url = callback_url
        self._auth_header = auth_header
        self._logger = logger or _LOGGER
        self._exposed_ids = exposed_ids

        self._pending: dict[str, DeviceState] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._unsub: Callable[[], None] | None = None

    async def start(self) -> None:
        """Subscribe to player events and start background tasks."""
        from music_assistant_models.enums import EventType

        self._unsub = self._mass.subscribe(
            self._on_player_event,
            event_filter=(
                EventType.PLAYER_UPDATED,
                EventType.PLAYER_ADDED,
                EventType.PLAYER_REMOVED,
            ),
        )

        # Schedule initial full state report after startup delay
        self._mass.loop.call_later(
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
        if self._flush_handle:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._pending.clear()
        self._logger.info("State notifier stopped")

    # -----------------------------------------------------------------------
    # Event handling
    # -----------------------------------------------------------------------

    def _on_player_event(self, event: MassEvent) -> None:
        """Handle player state change — queue for batched reporting."""
        from music_assistant_models.enums import EventType

        if event.event in (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED):
            self._schedule_discovery()
            return

        # PLAYER_UPDATED — event.data is the Player (state) dataclass
        player_state = event.data
        if player_state is None:
            return

        if not is_player_exposable(player_state, exposed_ids=self._exposed_ids):
            return

        device_state = get_device_state(player_state)
        self._pending[device_state.id] = device_state
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
        """Send all pending state changes to Yandex."""
        self._flush_handle = None
        if not self._pending:
            return
        devices = list(self._pending.values())
        self._pending.clear()
        await self._send_state_callback(devices)

    # -----------------------------------------------------------------------
    # State reporting
    # -----------------------------------------------------------------------

    async def _send_state_callback(self, devices: list[DeviceState]) -> None:
        """POST state callback to Yandex."""
        payload = CallbackRequest(
            ts=time.time(),
            payload=CallbackPayload(user_id=self._user_id, devices=devices),
        )
        try:
            async with self._session.post(
                self._callback_url,
                json=asdict(payload),
                headers=self._auth_header,
            ) as resp:
                if resp.status not in (200, 202):
                    body = await resp.text()
                    self._logger.warning(
                        "State callback failed (HTTP %d): %s", resp.status, body[:200]
                    )
                else:
                    self._logger.debug("State callback sent: %d device(s)", len(devices))
        except Exception:
            self._logger.exception("State callback error")

    async def _report_all_states(self) -> None:
        """Report states for all currently exposed players."""
        devices: list[DeviceState] = []
        for player in self._mass.players:
            player_state = player.state if hasattr(player, "state") else player
            if is_player_exposable(player_state, exposed_ids=self._exposed_ids):  # type: ignore[arg-type]
                devices.append(get_device_state(player_state))  # type: ignore[arg-type]
        if devices:
            self._logger.info("Reporting all states: %d device(s)", len(devices))
            await self._send_state_callback(devices)

    async def _heartbeat_loop(self) -> None:
        """Periodically report all states as a heartbeat."""
        while True:
            await asyncio.sleep(STATE_HEARTBEAT_INTERVAL)
            await self._report_all_states()

    # -----------------------------------------------------------------------
    # Discovery notification
    # -----------------------------------------------------------------------

    def _schedule_discovery(self) -> None:
        """Notify Yandex that the device list has changed."""
        self._mass.create_task(self._send_discovery())

    async def _send_discovery(self) -> None:
        """POST discovery notification to Yandex."""
        discovery_url = self._callback_url.replace("/state", "/discovery")
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
