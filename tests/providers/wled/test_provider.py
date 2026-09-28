"""Tests for the WLED provider's duplicate-port guard and config-change handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PORT
from music_assistant.providers.wled.constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_SCALING_MODE,
    ScalingMode,
)
from music_assistant.providers.wled.provider import WledProvider


def _make_provider(
    own_port: int = 11988, sibling_ports: dict[str, int] | None = None
) -> WledProvider:
    mass = MagicMock()
    config = MagicMock(instance_id="wled_a")
    config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    provider = WledProvider(mass, MagicMock(domain="wled"), config, set())
    ports = {"wled_a": own_port, **(sibling_ports or {})}
    mass.config.get_provider_configs = AsyncMock(
        return_value=[MagicMock(instance_id=instance_id) for instance_id in ports]
    )
    mass.config.get_provider_setup_value = MagicMock(
        side_effect=lambda instance_id, _key: ports[instance_id]
    )
    mass.config.get = MagicMock(return_value={CONF_PORT: own_port})
    return provider


async def test_rejects_a_sibling_on_the_same_port() -> None:
    """A second instance on a used port fails with a translated error."""
    provider = _make_provider(11988, {"wled_b": 11988})
    with pytest.raises(SetupFailedError) as exc_info:
        await provider.handle_async_init()
    assert exc_info.value.translation_key == "port_in_use"


async def test_starts_bridge_on_a_free_port() -> None:
    """A unique port starts the bridge."""
    provider = _make_provider(11988, {"wled_b": 11989})
    with patch("music_assistant.providers.wled.provider.WledBridge") as bridge_cls:
        bridge_cls.return_value.start = AsyncMock()
        await provider.handle_async_init()
    bridge_cls.return_value.start.assert_awaited_once()
    assert bridge_cls.call_args.args[2] == 11988


async def test_immediate_apply_keys_update_in_place() -> None:
    """Playback settings change without a reload."""
    provider = _make_provider()
    provider._bridge = MagicMock()
    config = MagicMock()
    values: dict[str, Any] = {CONF_LATENCY_MS: 250, CONF_GAIN_DB: 0, CONF_SCALING_MODE: "linear"}
    config.get_value = MagicMock(side_effect=lambda key, _default=None: values[key])
    await provider.update_config(config, {f"values/{CONF_GAIN_DB}"})
    provider._bridge.update_settings.assert_called_once_with(250, 0.0, ScalingMode.LINEAR)


async def test_other_changes_reload() -> None:
    """Non-playback changes fall back to a reload."""
    provider = _make_provider()
    provider._bridge = MagicMock()
    with patch("music_assistant.models.plugin.PluginProvider.update_config", AsyncMock()) as sup:
        await provider.update_config(MagicMock(), {"values/other"})
    sup.assert_awaited_once()
