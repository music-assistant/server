"""Fixtures for testing the MPD player provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.mpd.player import MPDPlayer
from music_assistant.providers.mpd.provider import CONF_MANUAL_IPS, MPDPlayerProvider
from tests.common import MockProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib

HOST = "10.0.0.6"
# two MPD servers on one host
FIRST = f"mpd_{HOST}_6600"
SECOND = f"mpd_{HOST}_6601"


class FakeMPDClient:
    """An MPD client whose server answers on every host and port, and never reports a change."""

    mpd_version = "0.23.5"

    async def connect(self, host: str, port: int) -> None:
        """Connect to the server."""

    async def password(self, password: str) -> None:
        """Authenticate at the server."""

    async def status(self) -> dict[str, Any]:
        """Return the status of the server."""
        return {"state": "stop", "volume": "40"}

    async def idle(self) -> AsyncIterator[str]:
        """Wait for changes of the server, which never come."""
        await asyncio.Event().wait()
        yield "player"

    def disconnect(self) -> None:
        """Disconnect from the server."""


@pytest.fixture
def make_mpd_player() -> Callable[..., MPDPlayer]:
    """Return a factory that builds an MPDPlayer with a decoupled (mocked) provider/mass."""

    def _make_player(protocol_id: str = "mpd_test") -> MPDPlayer:
        provider = MockProvider("mpd", instance_id="mpd--1")
        config = MagicMock()
        config.name = None
        config.default_name = "MPD Test"
        config.setup_data = {}
        provider.mass.config.get_base_protocol_config.return_value = config
        return MPDPlayer(provider=provider, protocol_id=protocol_id, host=HOST)  # type: ignore[arg-type]

    return _make_player


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers; no MPD server is contacted.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    with patch("music_assistant.providers.mpd.player.MPDClient", FakeMPDClient):
        try:
            await scenario.start()
            yield scenario
        finally:
            await scenario.close()


async def load_provider(scenario: Scenario, instance_id: str) -> MPDPlayerProvider:
    """
    Load a real provider in place of the fake one of its instance, as the server does.

    :param scenario: The running server.
    :param instance_id: The instance id of the provider.
    """
    manifest = scenario.mass.get_provider_manifest("mpd")
    entry = ConfigEntry(key=CONF_MANUAL_IPS, type=ConfigEntryType.STRING, multi_value=True)
    raw = scenario.mass.config.get(f"providers/{instance_id}")
    config = cast("ProviderConfig", ProviderConfig.parse([entry], raw))
    provider = MPDPlayerProvider(scenario.mass, manifest, config)
    await provider.handle_async_init()
    provider.available = True
    provider.initialized.set()
    scenario.mass._providers[instance_id] = provider
    await provider.loaded_in_mass()
    await scenario.mass.run_provider_discovery(instance_id)
    await scenario.settle()
    return provider


async def add_provider(scenario: Scenario, hosts: list[str]) -> MPDPlayerProvider:
    """
    Add a real provider set up with the given hosts.

    :param scenario: The running server.
    :param hosts: The host entries of the provider config.
    """
    fake = await scenario.add_provider("mpd")
    scenario.mass.config.set_raw_provider_config_value(fake.instance_id, CONF_MANUAL_IPS, hosts)
    return await load_provider(scenario, fake.instance_id)


async def reconfigure(
    scenario: Scenario, provider: MPDPlayerProvider, hosts: list[str]
) -> MPDPlayerProvider:
    """
    Change the hosts of a provider, which reloads it.

    :param scenario: The running server.
    :param provider: The provider to reconfigure.
    :param hosts: The new host entries.
    """
    scenario.mass.config.set_raw_provider_config_value(provider.instance_id, CONF_MANUAL_IPS, hosts)
    await scenario.mass.unload_provider(provider.instance_id)
    await scenario.settle()
    return await load_provider(scenario, provider.instance_id)
