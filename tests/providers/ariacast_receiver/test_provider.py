"""Tests for the AriaCast Receiver provider."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.player import Player
from music_assistant.providers.ariacast_receiver import (
    CONF_MASS_PLAYER_ID,
    DEFAULT_ARIACAST_NAME,
    AriaCastReceiver,
)
from tests.common import MockProvider, attach_mock, mock_player_mass


def _make_provider(player_id: str = "player1") -> AriaCastReceiver:
    """Create an AriaCastReceiver on a players controller that looks players up for real."""
    values: dict[str, Any] = {CONF_MASS_PLAYER_ID: player_id, "log_level": "GLOBAL"}
    config = MagicMock()
    config.get_value.side_effect = values.get
    config.instance_id = "ariacast_receiver"
    config.name = "AriaCast Receiver"
    manifest = MagicMock()
    manifest.domain = "ariacast_receiver"
    mass = mock_player_mass()
    mass.config.get_raw_provider_config_value.return_value = None
    return AriaCastReceiver(mass, manifest, config)


def _attach(
    provider: AriaCastReceiver,
    protocol_id: str,
    *,
    player_id: str | None = None,
    available: bool = True,
) -> Player:
    """Attach a protocol to a player of the provider's server and publish its state."""
    player, protocol = attach_mock(
        MockProvider("airplay", mass=cast("MagicMock", provider.mass)),
        protocol_id,
        "Living Room",
        player_id=player_id,
    )
    protocol._attr_available = available
    player.update_state()
    return player


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
    _attach(provider, "player1", player_id="player1")

    assert provider._ariacast_name == "Living Room"


def test_advertised_name_follows_the_player_of_a_connected_protocol() -> None:
    """A connected id that names a protocol advertises the name of its player."""
    provider = _make_provider(player_id="ap_1")
    _attach(provider, "ap_1")

    assert provider._ariacast_name == "Living Room"


def test_connected_player_wins_over_a_protocol_of_another_player_with_its_id() -> None:
    """
    A connected id names the player that has it, not another player with a protocol of that id.

    That is the case of a player from before the split once its own protocol, which shares
    its id, was taken off it.
    """
    provider = _make_provider(player_id="wiim_living")
    connected = _attach(provider, "airplay_living", player_id="wiim_living")
    other, _protocol = attach_mock(
        MockProvider("wiim", mass=cast("MagicMock", provider.mass)), "wiim_living", "Other"
    )
    other.update_state()

    assert provider._ariacast_name == "Living Room"
    assert provider._get_target_player_id() == connected.player_id


def test_advertised_name_falls_back_while_player_unregistered() -> None:
    """The default name applies while the connected player is not registered."""
    provider = _make_provider()

    assert provider._ariacast_name == DEFAULT_ARIACAST_NAME


def test_target_player_is_the_configured_player() -> None:
    """Without an active player the configured (registered) player is the target."""
    provider = _make_provider()
    _attach(provider, "player1", player_id="player1")

    assert provider._get_target_player_id() == "player1"


def test_target_player_is_none_while_configured_player_unavailable() -> None:
    """An unregistered configured player yields no target instead of a dead queue."""
    provider = _make_provider()

    assert provider._get_target_player_id() is None


def test_target_player_is_none_while_configured_player_is_offline() -> None:
    """A registered but unavailable configured player yields no target."""
    provider = _make_provider()
    _attach(provider, "player1", player_id="player1", available=False)

    assert provider._get_target_player_id() is None


def test_target_player_is_the_player_of_a_configured_protocol() -> None:
    """A configured id that names a protocol plays on the player of that protocol."""
    provider = _make_provider(player_id="ap_1")
    player = _attach(provider, "ap_1")

    assert provider._get_target_player_id() == player.player_id


def test_target_player_is_none_while_the_player_of_a_configured_protocol_is_offline() -> None:
    """A configured protocol whose player is unavailable yields no target."""
    provider = _make_provider(player_id="ap_1")
    _attach(provider, "ap_1", available=False)

    assert provider._get_target_player_id() is None


def test_remembered_protocol_is_replaced_by_its_player() -> None:
    """A remembered protocol id is redirected and the player id is remembered instead."""
    provider = _make_provider()
    provider._active_player_id = "ap_1"
    player = _attach(provider, "ap_1")

    assert provider._get_target_player_id() == player.player_id
    assert provider._active_player_id == player.player_id
