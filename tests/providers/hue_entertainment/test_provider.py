"""Tests for loading a Hue Lights Sync instance that streams to ZHA lamps."""

from __future__ import annotations

import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.hue_entertainment import provider as provider_module
from music_assistant.providers.hue_entertainment.constants import (
    BACKEND_ZHA,
    CONF_BACKEND,
    CONF_ZIGBEE_LIGHTS,
    CONF_ZIGBEE_NAME,
)
from music_assistant.providers.hue_entertainment.provider import HueEntertainmentProvider
from music_assistant.providers.hue_entertainment.targets import ZigbeeTarget

_LAMP = "00:17:88:01:00:00:00:02"
_SIGNE = "00:17:88:01:00:00:00:03"


def _provider(setup_data: dict[str, Any]) -> HueEntertainmentProvider:
    """Build a provider with the given setup data, skipping the framework init."""
    provider = HueEntertainmentProvider.__new__(HueEntertainmentProvider)
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id="hue_entertainment--AbCd1234")
    provider.config.get_value.return_value = None
    provider.logger = logging.getLogger("test")
    provider._hue_api = None
    provider._bridge_manager = None
    provider.get_setup_value = lambda key, default=None: setup_data.get(key, default)  # type: ignore[method-assign]
    return provider


async def test_zha_instance_loads_one_target_without_home_assistant() -> None:
    """A ZHA instance gets one player for its lamps; Home Assistant is not asked at load."""
    provider = _provider(
        {
            CONF_BACKEND: BACKEND_ZHA,
            CONF_ZIGBEE_NAME: "Living Room",
            CONF_ZIGBEE_LIGHTS: [f"{_LAMP}/1", f"{_SIGNE}/3"],
        }
    )
    manager = MagicMock(setup_bridges=AsyncMock())

    with patch.object(provider_module, "HueEntertainmentBridgeManager", return_value=manager):
        await provider.loaded_in_mass()

    (targets,) = manager.setup_bridges.await_args.args
    (target,) = targets
    assert isinstance(target, ZigbeeTarget)
    assert target.name == "Living Room"
    assert target.client_id == "hue-zb-AbCd1234"
    assert [channel.service_id for channel in target.channels] == [_LAMP, _SIGNE, _SIGNE, _SIGNE]
    assert provider.available
    cast("MagicMock", provider.mass).get_provider.assert_not_called()


async def test_zha_instance_without_lamps_is_unavailable() -> None:
    """A ZHA instance without stored lamps has no player to offer."""
    provider = _provider({CONF_BACKEND: BACKEND_ZHA})

    with patch.object(provider_module, "HueEntertainmentBridgeManager") as manager_class:
        await provider.loaded_in_mass()

    manager_class.assert_not_called()
    assert not provider.available
