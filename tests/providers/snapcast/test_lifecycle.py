"""Tests for snapcast players on a running server: two clients of one host stay apart."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant.providers.snapcast.constants import DEFAULT_SNAPCAST_FORMAT
from music_assistant.providers.snapcast.player import SnapCastPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from tests.scenarios.harness import Scenario

HOST_IP = "192.168.1.30"
HOST_MAC = "b8:27:eb:11:22:33"
# snapclient ids: the host id, with the instance number from the second instance on
AMP_ONE = HOST_MAC
AMP_TWO = f"{HOST_MAC}#2"


async def _load(scenario: Scenario) -> None:
    """Load the snapcast provider; its snapserver is not needed to register a client."""
    provider = await scenario.add_provider("snapcast")
    provider.stream_audio_format = DEFAULT_SNAPCAST_FORMAT  # type: ignore[attr-defined]


async def _connect(scenario: Scenario, identifier: str, name: str) -> SnapCastPlayer:
    """Register the protocol of a connected snapclient of the host, as the provider does."""
    provider = scenario.provider("snapcast")
    provider.stream_audio_format = DEFAULT_SNAPCAST_FORMAT  # type: ignore[attr-defined]
    snap_client = MagicMock(identifier=identifier, friendly_name=name, connected=True, group=None)
    snap_client._client = {"host": {"ip": HOST_IP, "mac": HOST_MAC, "os": "Linux", "arch": "arm"}}
    protocol_id = "ma_" + identifier.replace(":", "").replace("#", "")
    protocol = SnapCastPlayer(provider, protocol_id, snap_client)  # type: ignore[arg-type]
    protocol.setup()
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    return protocol


async def _disconnect(scenario: Scenario, protocol: SnapCastPlayer) -> None:
    """Unregister the protocol of a snapclient, as the provider does."""
    await scenario.mass.players.unregister(protocol.protocol_id)
    await scenario.settle()


def _player(protocol: SnapCastPlayer) -> Player:
    """Return the player a protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


async def test_a_new_client_does_not_take_over_an_offline_sibling(scenario: Scenario) -> None:
    """A second client first seen while the first is away gets a player of its own."""
    await _load(scenario)
    amp_one = await _connect(scenario, AMP_ONE, "Pi amp 1")
    player_one = _player(amp_one).player_id
    await scenario.configure_player(player_one, name="Amp one")
    await _disconnect(scenario, amp_one)

    amp_two = await _connect(scenario, AMP_TWO, "Pi amp 2")

    assert _player(amp_two).player_id != player_one
    assert _player(amp_two).name == "Pi amp 2"

    amp_one = await _connect(scenario, AMP_ONE, "Pi amp 1")

    assert _player(amp_one).player_id == player_one
    assert [player.name for player in scenario.visible_players()] == ["Amp one", "Pi amp 2"]


async def test_each_client_finds_its_own_player_after_a_restart(scenario: Scenario) -> None:
    """After a restart the clients come back in any order, each on its own player."""
    await _load(scenario)
    amp_one = await _connect(scenario, AMP_ONE, "Pi amp 1")
    amp_two = await _connect(scenario, AMP_TWO, "Pi amp 2")
    player_one, player_two = _player(amp_one).player_id, _player(amp_two).player_id
    assert player_one != player_two
    await scenario.configure_player(player_one, name="Amp one")

    await scenario.restart()
    amp_two = await _connect(scenario, AMP_TWO, "Pi amp 2")
    amp_one = await _connect(scenario, AMP_ONE, "Pi amp 1")

    assert _player(amp_one).player_id == player_one
    assert _player(amp_two).player_id == player_two
    assert _player(amp_one).name == "Amp one"
