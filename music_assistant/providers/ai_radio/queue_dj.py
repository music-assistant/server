"""Sticky per-queue AI DJ for AI Radio."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiofiles
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import async_json_loads

from .models import DJQueueState

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant


class AIRadioQueueDJMixin:
    """Mixin managing sticky queue DJ state and clip injection."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _hosts: dict[str, dict[str, Any]]
        _dj_queues: dict[str, DJQueueState]
        _dj_file: Path
        _dj_lock: asyncio.Lock

    async def set_queue_dj(self, queue_id: str, host_id: str | None) -> dict[str, str]:
        """
        Enable, switch or disable the sticky AI DJ on a queue.

        :param queue_id: The queue to change.
        :param host_id: The host to enable, or None to disable.
        :return: The full queue-to-host mapping after the change.
        """
        queue_id = str(queue_id).strip()
        if not queue_id:
            raise InvalidDataError("queue_id is required")
        async with self._dj_lock:
            if host_id is None:
                state = self._dj_queues.pop(queue_id, None)
                await self._write_queue_dj()
            else:
                host_id = str(host_id).strip()
                if host_id not in self._hosts:
                    raise InvalidDataError(f"Unknown host id: {host_id}")
                state = self._arm_dj_state(queue_id, host_id)
                await self._write_queue_dj()
        if host_id is None:
            if state is not None:
                await self._remove_pending_dj_clips(queue_id)
        else:
            self._schedule_replan(queue_id)
        return await self.get_queue_dj_status()

    async def get_queue_dj_status(self) -> dict[str, str]:
        """Return the queue-to-host mapping of all active queue DJs."""
        return {queue_id: state.host_id for queue_id, state in self._dj_queues.items()}

    async def _load_queue_dj(self) -> None:
        """Load persisted queue DJ assignments and arm their states."""
        file_exists = await asyncio.to_thread(self._dj_file.exists)
        if not file_exists:
            self._dj_queues = {}
            return
        async with aiofiles.open(self._dj_file) as file_handle:
            content = await file_handle.read()
        try:
            payload = await async_json_loads(content)
        except ValueError as err:
            self.logger.error("Queue DJ file is corrupt, starting without queue DJs: %s", err)
            payload = {}
        queues = payload.get("queues", {}) if isinstance(payload, dict) else {}
        self._dj_queues = {}
        if isinstance(queues, dict):
            for queue_id, entry in queues.items():
                host_id = str(entry.get("host_id", "")).strip() if isinstance(entry, dict) else ""
                if host_id not in self._hosts:
                    self.logger.warning(
                        "Dropping queue DJ for %s: host %s no longer exists", queue_id, host_id
                    )
                    continue
                self._arm_dj_state(str(queue_id), host_id)

    async def _write_queue_dj(self) -> None:
        """Persist queue DJ assignments to disk."""
        payload = {
            "version": 1,
            "queues": {
                queue_id: {"host_id": state.host_id}
                for queue_id, state in sorted(self._dj_queues.items())
            },
        }
        await self._write_json_file(self._dj_file, payload)

    def _arm_dj_state(self, queue_id: str, host_id: str) -> DJQueueState:
        """Create fresh in-memory DJ state for a queue."""
        # a fresh session id per arm keeps clip ids from colliding with clips
        # persisted in the queue by a previous run of this provider
        state = DJQueueState(
            queue_id=queue_id,
            host_id=host_id,
            dj_session_id=f"dj{uuid4().hex[:12]}",
        )
        self._dj_queues[queue_id] = state
        return state

    def _schedule_replan(self, queue_id: str) -> None:
        """Schedule a replan pass for the given queue."""

    async def _remove_pending_dj_clips(self, queue_id: str) -> None:
        """Remove not-yet-played DJ clips from the given queue."""
