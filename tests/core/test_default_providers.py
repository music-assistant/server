"""Tests for the automatic setup of the default providers."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.constants import CONF_DEFAULT_PROVIDERS_SETUP
from music_assistant.mass import MusicAssistant

# What the kernel reports for a nominal 4GB host (e.g. a Home Assistant Green).
HOST_4GB_REPORTED = 3.8
HOST_16GB_REPORTED = 15.6


def _mass(*, running_as_hass_addon: bool) -> MusicAssistant:
    """Return a minimal MusicAssistant that has not set up any default provider yet."""
    mass = object.__new__(MusicAssistant)
    mass.running_as_hass_addon = running_as_hass_addon
    mass._provider_manifests = {
        domain: MagicMock(domain=domain, mdns_discovery=None, builtin=False)
        for domain in ("smart_fades", "airplay")
    }
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.create_builtin_provider_config = AsyncMock()
    mass.config.get_player_configs = AsyncMock(return_value=[])
    mass.config.get_provider_configs = AsyncMock(return_value=[])
    return mass


async def _load_default_providers(mass: MusicAssistant, memory_gb: float) -> set[str]:
    """Run the default providers setup and return the domains it created a config for."""
    with (
        patch(
            "music_assistant.mass.DEFAULT_PROVIDERS",
            {("smart_fades", False), ("airplay", False)},
        ),
        patch("music_assistant.helpers.util.get_total_system_memory", return_value=memory_gb),
    ):
        await mass._load_providers()
    create = cast("AsyncMock", mass.config.create_builtin_provider_config)
    return {call.args[0] for call in create.await_args_list}


async def test_smart_fades_not_auto_enabled_on_small_hass_addon() -> None:
    """A 4GB add-on host skips Smart Fades, and remembers it so it is not offered again."""
    mass = _mass(running_as_hass_addon=True)

    created = await _load_default_providers(mass, HOST_4GB_REPORTED)

    assert created == {"airplay"}
    cast("MagicMock", mass.config.set).assert_called_once_with(
        CONF_DEFAULT_PROVIDERS_SETUP, {"smart_fades", "airplay"}
    )


@pytest.mark.parametrize(
    ("running_as_hass_addon", "memory_gb"),
    [
        (True, HOST_16GB_REPORTED),
        (False, HOST_4GB_REPORTED),
    ],
    ids=["hass_addon_16gb", "standalone_4gb"],
)
async def test_smart_fades_auto_enabled(running_as_hass_addon: bool, memory_gb: float) -> None:
    """A large add-on host and a standalone host still get Smart Fades set up by default."""
    mass = _mass(running_as_hass_addon=running_as_hass_addon)

    created = await _load_default_providers(mass, memory_gb)

    assert created == {"smart_fades", "airplay"}
