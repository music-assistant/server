"""
Tests for ``HueEntertainmentProvider.update_config`` routing.

Brightness, colour mode and latency are applied to the running bridges in
place; anything else falls through to the base implementation, which reloads
the provider and therefore drops the entertainment session and the virtual
players. The changed keys are derived from the real ``Config.update()`` here
rather than hardcoded, so the test keeps tracking the ``values/`` namespacing
the models package produces.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderType

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.models.provider import Provider
from music_assistant.providers.hue_entertainment.constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
)
from music_assistant.providers.hue_entertainment.provider import HueEntertainmentProvider


async def _make_config() -> ProviderConfig:
    """Build a ProviderConfig holding the provider's own entries plus the log level."""
    provider = HueEntertainmentProvider.__new__(HueEntertainmentProvider)
    entries = list(await provider.get_config_entries())
    entries.append(ConfigEntry(key=CONF_LOG_LEVEL, type=ConfigEntryType.STRING, default_value=None))
    values = {entry.key: entry for entry in entries}
    for entry in values.values():
        entry.value = entry.default_value
    return ProviderConfig(
        type=ProviderType.PLUGIN,
        domain="hue_entertainment",
        instance_id="hue_entertainment--test",
        values=values,
    )


def _make_provider(config: ProviderConfig) -> tuple[HueEntertainmentProvider, MagicMock]:
    """Build a provider with a mocked bridge manager, skipping the framework init."""
    provider = HueEntertainmentProvider.__new__(HueEntertainmentProvider)
    provider.mass = MagicMock()
    provider.config = config
    provider.logger = logging.getLogger("test")
    bridge_manager = MagicMock()
    provider._bridge_manager = bridge_manager
    return provider, bridge_manager


async def test_settings_change_applies_in_place() -> None:
    """A brightness change updates the bridges without reloading the provider."""
    config = await _make_config()
    provider, bridge_manager = _make_provider(config)
    changed_keys = config.update({CONF_BRIGHTNESS: 50})

    # Guards the premise of the routing: the models package namespaces value keys.
    assert changed_keys == {f"values/{CONF_BRIGHTNESS}"}

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    bridge_manager.update_settings.assert_called_once()
    assert bridge_manager.update_settings.call_args.kwargs["brightness"] == 50
    base_update.assert_not_awaited()
    assert provider.config is config


@pytest.mark.parametrize(
    "update",
    [
        {CONF_COLOR_MODE: "ambient"},
        {CONF_HUE_LATENCY_MS: 120},
        {CONF_BRIGHTNESS: 50, CONF_COLOR_MODE: "ambient", CONF_HUE_LATENCY_MS: 120},
    ],
    ids=["color_mode", "latency", "all_settings"],
)
async def test_every_in_place_setting_skips_the_reload(update: dict[str, ConfigValueType]) -> None:
    """Each hot-applyable setting - and any combination of them - avoids the reload."""
    config = await _make_config()
    provider, bridge_manager = _make_provider(config)
    changed_keys = config.update(update)

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    bridge_manager.update_settings.assert_called_once()
    base_update.assert_not_awaited()


async def test_other_change_falls_through_to_reload() -> None:
    """A log level change is left to the base implementation."""
    config = await _make_config()
    provider, bridge_manager = _make_provider(config)
    changed_keys = config.update({CONF_LOG_LEVEL: "DEBUG"})

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    bridge_manager.update_settings.assert_not_called()
    base_update.assert_awaited_once_with(config, changed_keys)


async def test_mixed_change_falls_through_to_reload() -> None:
    """A setting changed alongside another key must not skip the base implementation."""
    config = await _make_config()
    provider, bridge_manager = _make_provider(config)
    changed_keys = config.update({CONF_BRIGHTNESS: 50, CONF_LOG_LEVEL: "DEBUG"})

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    bridge_manager.update_settings.assert_not_called()
    base_update.assert_awaited_once_with(config, changed_keys)


async def test_settings_change_without_bridge_manager_falls_through() -> None:
    """Without running bridges there is nothing to update in place."""
    config = await _make_config()
    provider, _ = _make_provider(config)
    provider._bridge_manager = None
    changed_keys = config.update({CONF_BRIGHTNESS: 50})

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    base_update.assert_awaited_once_with(config, changed_keys)
