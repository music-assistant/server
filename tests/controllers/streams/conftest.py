"""Shared fixtures for the streams controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.tasks import TasksController

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def streams_controller(mass_minimal: MusicAssistant) -> AsyncGenerator[StreamsController]:
    """
    Yield a StreamsController attached to a minimal server, closed afterwards.

    :param mass_minimal: Minimal MusicAssistant instance.
    """
    mass_minimal.tasks = TasksController(mass_minimal)
    await mass_minimal.tasks.setup(await mass_minimal.config.get_core_config("tasks"))
    streams = StreamsController(mass_minimal)
    mass_minimal.streams = streams
    try:
        yield streams
    finally:
        # close unconditionally: a failed assertion must not leave the socket bound
        await streams.close()
        await mass_minimal.tasks.close()
