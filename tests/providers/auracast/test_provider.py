"""Tests for Auracast provider configuration."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.auracast import AuracastProvider


def create_provider(setup_values: dict[str, Any]) -> MagicMock:
    """Build a provider stub whose setup data holds the given values."""
    provider = MagicMock()
    provider.get_setup_value.side_effect = lambda key, default=None: setup_values.get(key, default)
    return provider


@pytest.mark.asyncio
async def test_setup_data_values_seed_provider_settings() -> None:
    """Expose submitted setup-flow answers as initial provider setting values."""
    setup_values = {
        "serial_port": "/dev/controller",
        "baudrate": 1_000_000,
        "broadcast_name": "Auracast test",
        "manufacturer_data": "87:abcd",
    }

    entries = await AuracastProvider.get_config_entries(cast("Any", create_provider(setup_values)))

    assert {entry.key: entry.default_value for entry in entries} == setup_values
    assert {entry.key: entry.type for entry in entries}["baudrate"] == ConfigEntryType.INTEGER


@pytest.mark.asyncio
async def test_serial_port_has_no_platform_specific_default() -> None:
    """Leave the serial port empty until the setup flow provides one."""
    entries = await AuracastProvider.get_config_entries(cast("Any", create_provider({})))

    assert {entry.key: entry.default_value for entry in entries}["serial_port"] == ""


@pytest.mark.asyncio
async def test_discover_players_starts_broadcast_after_registering_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start the empty broadcast as soon as provider discovery registers its player."""
    player = MagicMock()
    player.start_broadcast = AsyncMock()
    monkeypatch.setattr("music_assistant.providers.auracast.AuracastPlayer", lambda *_: player)
    provider = MagicMock()
    provider.instance_id = "auracast"
    provider.mass.players.register = AsyncMock()

    await AuracastProvider.discover_players(cast("Any", provider))

    provider.mass.players.register.assert_awaited_once_with(player)
    player.start_broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_editable_options_reject_invalid_values() -> None:
    """Reject malformed baud rates and manufacturer data before they are persisted."""
    entries = await AuracastProvider.get_config_entries(cast("Any", create_provider({})))
    validators = {entry.key: entry.validate for entry in entries}
    baudrate_validator = validators["baudrate"]
    manufacturer_validator = validators["manufacturer_data"]
    assert baudrate_validator is not None
    assert manufacturer_validator is not None

    assert baudrate_validator(1_000_000)
    assert not baudrate_validator("abc")
    assert not baudrate_validator(100)
    assert manufacturer_validator("87:abcd")
    assert manufacturer_validator("")
    assert not manufacturer_validator("87:xyz")


def test_provider_relies_on_default_config_reload() -> None:
    """Reload on config changes through the base class instead of a custom override."""
    assert "update_config" not in vars(AuracastProvider)
