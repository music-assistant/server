"""Fixtures for the Sonos provider tests."""

import asyncio
import threading
from collections.abc import AsyncGenerator

import pytest
from music_assistant_models.enums import CoreState

from music_assistant.mass import MusicAssistant


@pytest.fixture
async def timer_mass() -> AsyncGenerator[MusicAssistant]:
    """Create a bare MusicAssistant exposing the real task/timer machinery."""
    mass = object.__new__(MusicAssistant)
    mass.loop = asyncio.get_running_loop()
    mass.loop_thread_id = threading.get_ident()
    mass._tracked_timers = {}
    mass._tracked_tasks = {}
    mass._state = CoreState.RUNNING
    yield mass
    for handle in mass._tracked_timers.values():
        handle.cancel()
    for task in mass._tracked_tasks.values():
        task.cancel()
