"""Shared fixtures for the streams controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.tasks import TasksController

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

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


@pytest.fixture
def streamserver_fallback(
    streams_controller: StreamsController,
) -> Iterator[AsyncMock]:
    """
    Make the streamserver report a successful fallback to all interfaces.

    :param streams_controller: StreamsController whose server should report the fallback.
    """
    server = streams_controller._server

    async def setup(*, bind_port: int, **_kwargs: object) -> None:
        server._bind_ip = None
        server._bind_port = bind_port

    with patch.object(server, "setup", AsyncMock(side_effect=setup)) as setup_mock:
        yield setup_mock
