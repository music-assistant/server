"""Tests for the AriaCast Receiver provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.ariacast_receiver import (
    CONF_MASS_PLAYER_ID,
    DEFAULT_ARIACAST_NAME,
    AriaCastReceiver,
)


def _make_provider(player_id: str = "player1") -> AriaCastReceiver:
    """Create an AriaCastReceiver with mock dependencies."""
    values: dict[str, Any] = {CONF_MASS_PLAYER_ID: player_id, "log_level": "GLOBAL"}
    config = MagicMock()
    config.get_value.side_effect = values.get
    config.instance_id = "ariacast_receiver"
    config.name = "AriaCast Receiver"
    manifest = MagicMock()
    manifest.domain = "ariacast_receiver"
    mass = MagicMock()
    # setup values resolve through the (empty) stored setup_data to config.get_value
    mass.config.get.return_value = {}
    mass.config.get_raw_provider_config_value.return_value = None
    mass.players.get_player.return_value = None
    return AriaCastReceiver(mass, manifest, config)


async def test_handle_async_init_requires_connected_player() -> None:
    """Loading without a connected player fails with a translated setup error."""
    provider = _make_provider(player_id="")

    with pytest.raises(SetupFailedError) as excinfo:
        await provider.handle_async_init()
    assert excinfo.value.translation_key == "no_connected_player"
    assert excinfo.value.translation_owner == "provider.ariacast_receiver"


def test_advertised_name_follows_connected_player() -> None:
    """The advertised receiver name is the connected player's display name."""
    provider = _make_provider()
    player = MagicMock()
    player.display_name = "Living Room"
    provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    assert provider._ariacast_name == "Living Room"


def test_advertised_name_falls_back_while_player_unregistered() -> None:
    """The default name applies while the connected player is not registered."""
    provider = _make_provider()

    assert provider._ariacast_name == DEFAULT_ARIACAST_NAME


def test_target_player_is_the_configured_player() -> None:
    """Without an active player the configured player is the playback target."""
    provider = _make_provider()

    assert provider._get_target_player_id() == "player1"
