"""Fixtures for the Fully Kiosk provider tests on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fully_kiosk.provider import CONF_MANUAL_IPS, FullyKioskProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator, Iterator

KITCHEN_HOST = "10.0.0.5"
OFFICE_HOST = "10.0.0.6"
KITCHEN = f"fully_kiosk_{KITCHEN_HOST}_2323"
OFFICE = f"fully_kiosk_{OFFICE_HOST}_2323"


def mock_protocol_config(name: str) -> MagicMock:
    """
    Return a mocked config of a protocol without a custom name.

    :param name: The default name of the protocol.
    """
    config = MagicMock()
    config.name = None
    config.default_name = name
    config.get_value = MagicMock(return_value="none")
    return config


async def load_provider(scenario: Scenario, instance_id: str) -> FullyKioskProvider:
    """
    Load a real provider in place of the fake one of its instance, as the server does.

    :param scenario: The running server.
    :param instance_id: The instance id of the provider.
    """
    manifest = scenario.mass.get_provider_manifest("fully_kiosk")
    entry = ConfigEntry(key=CONF_MANUAL_IPS, type=ConfigEntryType.STRING, multi_value=True)
    raw = scenario.mass.config.get(f"providers/{instance_id}")
    config = cast("ProviderConfig", ProviderConfig.parse([entry], raw))
    provider = FullyKioskProvider(scenario.mass, manifest, config)
    await provider.handle_async_init()
    provider.available = True
    provider.initialized.set()
    scenario.mass._providers[instance_id] = provider
    await provider.loaded_in_mass()
    await scenario.mass.run_provider_discovery(instance_id)
    await scenario.settle()
    return provider


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


@pytest.fixture(autouse=True)
def device_client() -> Iterator[MagicMock]:
    """Replace the Fully Kiosk client: every device answers, nothing reaches the network."""
    client = MagicMock(deviceInfo={"deviceName": "Kiosk", "audioVolumes": []})
    client.getDeviceInfo = AsyncMock()
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        yield client


@pytest.fixture
async def provider(scenario: Scenario) -> FullyKioskProvider:
    """
    Return a real provider set up with the kitchen and office devices.

    :param scenario: The running server.
    """
    fake = await scenario.add_provider("fully_kiosk")
    scenario.mass.config.set_raw_provider_config_value(
        fake.instance_id, CONF_MANUAL_IPS, [KITCHEN_HOST, f"{OFFICE_HOST}:2323"]
    )
    return await load_provider(scenario, fake.instance_id)
