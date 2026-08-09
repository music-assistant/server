"""Unit tests for the AI Radio sticky queue DJ state."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.ai_radio.queue_dj import AIRadioQueueDJMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class DummyQueueDJ(AIRadioQueueDJMixin, AIRadioStorageMixin):
    """Minimal harness for queue DJ state tests."""

    instance_id = "ai_radio_test"

    def __init__(self, tmp_path: Path) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._hosts: dict[str, dict[str, Any]] = {
            "rick": {"id": "rick", "name": "Rick", "instructions": "x", "tts_engine": ""},
        }
        self._dj_queues: dict[str, Any] = {}
        self._dj_file = tmp_path / "queue_dj.json"
        self._dj_lock = asyncio.Lock()

    def _schedule_replan(self, queue_id: str) -> None:
        """Record replan requests instead of running them."""
        self.replanned = getattr(self, "replanned", [])
        self.replanned.append(queue_id)


async def test_set_queue_dj_enables_and_persists(tmp_path: Path) -> None:
    """Arm a queue DJ, persist it, and reload it into a fresh instance."""
    dummy = DummyQueueDJ(tmp_path)
    mapping = await dummy.set_queue_dj("queue-1", "rick")
    assert mapping == {"queue-1": "rick"}
    assert dummy._dj_queues["queue-1"].host_id == "rick"
    assert dummy._dj_queues["queue-1"].dj_session_id
    assert dummy.replanned == ["queue-1"]
    assert dummy._dj_file.exists()

    fresh = DummyQueueDJ(tmp_path)
    await fresh._load_queue_dj()
    assert fresh._dj_queues["queue-1"].host_id == "rick"


async def test_set_queue_dj_rejects_unknown_host(tmp_path: Path) -> None:
    """Reject arming a queue DJ with an unknown host id."""
    dummy = DummyQueueDJ(tmp_path)
    with pytest.raises(InvalidDataError):
        await dummy.set_queue_dj("queue-1", "nobody")


async def test_set_queue_dj_none_disables(tmp_path: Path) -> None:
    """Disable an armed queue DJ by passing host_id=None."""
    dummy = DummyQueueDJ(tmp_path)
    await dummy.set_queue_dj("queue-1", "rick")
    mapping = await dummy.set_queue_dj("queue-1", None)
    assert mapping == {}
    assert dummy._dj_queues == {}


async def test_status_returns_mapping(tmp_path: Path) -> None:
    """Return the queue-to-host mapping for an armed queue DJ."""
    dummy = DummyQueueDJ(tmp_path)
    await dummy.set_queue_dj("queue-1", "rick")
    assert await dummy.get_queue_dj_status() == {"queue-1": "rick"}
