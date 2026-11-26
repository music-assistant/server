"""Fixtures for the AirPlay tests that run the provider on a server."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.provider import AirPlayProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers; nothing reaches the network.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    with (
        patch(
            "music_assistant.controllers.discovery.controller.DiscoveryController"
            ".async_find_mdns_service",
            AsyncMock(return_value=None),
        ),
        patch(
            "music_assistant.providers.airplay.provider.probe_audio_formats",
            AsyncMock(return_value=0),
        ),
    ):
        try:
            await scenario.start()
            yield scenario
        finally:
            await scenario.close()


@pytest.fixture
async def provider(scenario: Scenario) -> AirPlayProvider:
    """
    Return a real AirPlay provider loaded in the server, without its network services.

    :param scenario: The running server.
    """
    fake = await scenario.add_provider("airplay")
    provider = AirPlayProvider(scenario.mass, fake.manifest, fake.config)
    provider._bridge_manager = MagicMock(
        evaluate_bridge=AsyncMock(),
        remove_bridge=AsyncMock(),
        close=AsyncMock(),
        get_transport_command_target=MagicMock(return_value=None),
        get_bridge=MagicMock(return_value=None),
        stop_streaming=MagicMock(return_value=False),
    )
    provider.dashboards = MagicMock(unload=AsyncMock())
    provider._companion_info_by_address = {}
    provider._mrp_info_by_address = {}
    provider._speakers = {}
    provider._set_carriers = {}
    provider._pair_records = {}
    provider._pair_carriers = {}
    provider._announce_plans = {}
    provider._dacp_server = MagicMock()
    provider._dacp_info = None  # type: ignore[assignment]
    scenario.mass._providers[fake.instance_id] = provider
    return provider
