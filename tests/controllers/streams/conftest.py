"""Shared fixtures for the streams controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import unused_port

from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_PUBLISH_IP,
    CONF_VALUE_AUTO,
)
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.controllers.tasks import TasksController
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from music_assistant.mass import MusicAssistant

# the address the tests bind is deliberately absent here, so a publish IP that follows
# the bind address can never be mistaken for the one auto-detection would have picked
DEFAULT_PUBLISH_CANDIDATES = ("192.168.1.10", "10.0.0.5", "fd00::10")


async def setup_streams(
    controller: StreamsController,
    mass: MusicAssistant,
    *,
    bind_ip: str,
    publish_ip: str = CONF_VALUE_AUTO,
    all_ip_addresses: tuple[str, ...] = DEFAULT_PUBLISH_CANDIDATES,
    bind_port: int | None = None,
) -> int:
    """
    Run the streamserver's setup against the given bind and publish configuration.

    :param controller: The StreamsController to set up.
    :param mass: The MusicAssistant instance owning the controller.
    :param bind_ip: Address to configure as bind IP.
    :param publish_ip: Address to configure as publish IP ("auto" to have it resolved).
    :param all_ip_addresses: Host addresses the setup detects, in ranked order.
    :param bind_port: Port to configure as bind port (None picks a free one, 0 lets the OS assign one).
    :return: The port the streamserver bound to and publishes on.
    """
    config = await mass.config.get_core_config(controller.domain)
    config.update(
        {
            CONF_BIND_IP: bind_ip,
            CONF_PUBLISH_IP: publish_ip,
            CONF_BIND_PORT: unused_port() if bind_port is None else bind_port,
        }
    )
    with (
        patch(
            "music_assistant.controllers.streams.controller.get_publish_ip_candidates",
            AsyncMock(return_value=all_ip_addresses),
        ),
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            AsyncMock(),
        ),
    ):
        await controller.setup(config)
    port = controller.publish_port
    assert isinstance(port, int)
    assert port != 0
    return port


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
    # setup() overwrites the level of these process-global loggers with the controller
    # level, so snapshot them and restore afterwards to keep a level a test raised out
    # of unrelated tests
    saved_levels = [
        (logger, logger.level)
        for logger in (
            FFMPEG_LOGGER,
            streams.audio.logger,
            streams.logger.getChild("smart_fades_mixer"),
        )
    ]
    try:
        yield streams
    finally:
        # restore first: a failed close must not leave a leaked level behind
        for logger, level in saved_levels:
            logger.setLevel(level)
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
