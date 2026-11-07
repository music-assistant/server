"""Tests for the AriaCast Receiver provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.ariacast_receiver import (
    CONF_MASS_PLAYER_ID,
    DEFAULT_ARIACAST_NAME,
    AriaCastReceiver,
)


def _make_provider(
    player_id: str = "player1", registered_player_ids: frozenset[str] = frozenset()
) -> AriaCastReceiver:
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
    mass.players.get_player.side_effect = lambda pid: (
        MagicMock() if pid in registered_player_ids else None
    )
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
    provider.mass.players.get_player.side_effect = None  # type: ignore[attr-defined]
    provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    assert provider._ariacast_name == "Living Room"


def test_advertised_name_falls_back_while_player_unregistered() -> None:
    """The default name applies while the connected player is not registered."""
    provider = _make_provider()

    assert provider._ariacast_name == DEFAULT_ARIACAST_NAME


def test_target_player_is_the_configured_player() -> None:
    """Without an active player the configured (registered) player is the target."""
    provider = _make_provider(registered_player_ids=frozenset({"player1"}))

    assert provider._get_target_player_id() == "player1"


def test_target_player_is_none_while_configured_player_unavailable() -> None:
    """An unregistered configured player yields no target instead of a dead queue."""
    provider = _make_provider()

    assert provider._get_target_player_id() is None


def _register_players(provider: AriaCastReceiver, players: dict[str, MagicMock]) -> None:
    """Make the given players the only registered ones."""
    provider.mass.players.get_player.side_effect = players.get  # type: ignore[attr-defined]


def _protocol_player(parent_id: str | None) -> MagicMock:
    """Create a protocol player linked to the given parent player."""
    player = MagicMock()
    player.type = PlayerType.PROTOCOL
    player.protocol_parent_id = parent_id
    return player


def test_target_player_is_the_parent_of_a_linked_protocol_player() -> None:
    """A protocol player has no queue of its own, so its parent player is the target."""
    provider = _make_provider()
    _register_players(provider, {"player1": _protocol_player("parent1"), "parent1": MagicMock()})

    assert provider._get_target_player_id() == "parent1"


def test_target_player_is_none_while_the_protocol_parent_is_unavailable() -> None:
    """A protocol player whose parent is not registered yields no target."""
    provider = _make_provider()
    _register_players(provider, {"player1": _protocol_player("parent1")})

    assert provider._get_target_player_id() is None


def test_unlinked_protocol_player_is_its_own_target() -> None:
    """A protocol player without a parent keeps its queue and stays the target."""
    provider = _make_provider()
    _register_players(provider, {"player1": _protocol_player(None)})

    assert provider._get_target_player_id() == "player1"


def test_stored_protocol_player_is_replaced_by_its_parent() -> None:
    """A remembered protocol player is redirected and the parent is remembered instead."""
    provider = _make_provider()
    provider._active_player_id = "proto1"
    _register_players(provider, {"proto1": _protocol_player("parent1"), "parent1": MagicMock()})

    assert provider._get_target_player_id() == "parent1"
    assert provider._active_player_id == "parent1"
