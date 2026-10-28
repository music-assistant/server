"""
Tests for playing a paused queue whose player renders through an output protocol.

The queue pause goes to the protocol player that renders the audio (for example the DLNA
player behind a LinkPlay speaker), so the play that ends the pause has to reach that same
protocol player. A protocol-backed player does not forward a play to its output protocol
itself, so sending it there would leave the speaker paused.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from music_assistant.models.protocol_backed_player import ProtocolBackedPlayer
from tests.common import MockPlayer, MockProvider, create_mock_config

QUEUE_ID = "linkplay_1"
PROTOCOL_ID = "dlna_1"


class _ShellPlayer(ProtocolBackedPlayer):
    """A protocol-backed player that plays through one DLNA protocol player."""

    def __init__(self, provider: MockProvider, player_id: str) -> None:
        """Initialize the shell player."""
        provider.mass.config.get_base_player_config.return_value = create_mock_config("Shell")
        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = "Shell"
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = set()
        self._cache.clear()

    def _backing_protocol_player_ids(self) -> list[str]:
        """Return the protocol player backing this player."""
        return [PROTOCOL_ID]


def _setup() -> tuple[PlayerQueuesController, PlayerController, MagicMock]:
    """Build a queue controller with a real player controller and a paused queue."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")

    def _player_config(_player_id: str, _key: str, default: Any = None) -> Any:
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_player_config)
    mass.get_providers = MagicMock(return_value=[])

    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = mass
    ctrl.logger = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q", available=True, items=1)
    queue.state = PlaybackState.PAUSED
    queue_data = PlayerQueueData(queue=queue)
    queue_data.session_id = "sess-1"
    ctrl._queue_data = {QUEUE_ID: queue_data}
    mass.player_queues = ctrl

    players = PlayerController(mass)
    players.wait_for_player_update = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_a, **_kw: contextlib.nullcontext()
    )
    mass.players = players
    ctrl.resume = AsyncMock()  # type: ignore[method-assign]
    return ctrl, players, mass


def _mock_player(mass: MagicMock, player_id: str, domain: str) -> MockPlayer:
    """Return a paused player of the given provider domain that supports pause."""
    player = MockPlayer(MockProvider(domain, instance_id=domain, mass=mass), player_id, domain)
    player._attr_supported_features = {PlayerFeature.PAUSE}
    player._attr_playback_state = PlaybackState.PAUSED
    player._cache.clear()
    player.play = AsyncMock()  # type: ignore[method-assign]
    return player


def _register(players: PlayerController, *registered: Player) -> None:
    """Register the players with the player controller and mark them initialized."""
    players._players = {player.player_id: player for player in registered}
    for player in registered:
        player.set_initialized()
        player.update_state(signal_event=False)


async def test_play_reaches_the_output_protocol_of_a_protocol_backed_player() -> None:
    """Play on a paused queue unpauses the protocol player the audio goes through."""
    ctrl, players, mass = _setup()
    protocol_player = _mock_player(mass, PROTOCOL_ID, "dlna")
    shell = _ShellPlayer(MockProvider("wiim", instance_id="wiim", mass=mass), QUEUE_ID)
    _register(players, shell, protocol_player)
    shell.set_active_output_protocol(PROTOCOL_ID)

    await ctrl._handle_play(QUEUE_ID)

    protocol_player.play.assert_awaited_once()  # type: ignore[attr-defined]
    ctrl.resume.assert_not_awaited()  # type: ignore[attr-defined]
    # the wait for playback is on the protocol player, whose state the device reports
    assert players.wait_for_player_update.call_args.args[0] == PROTOCOL_ID  # type: ignore[attr-defined]


async def test_play_unpauses_a_player_that_plays_natively() -> None:
    """Play on a paused queue unpauses a player that renders the audio itself."""
    ctrl, players, mass = _setup()
    player = _mock_player(mass, QUEUE_ID, "sonos")
    _register(players, player)
    player.set_active_output_protocol("native")

    await ctrl._handle_play(QUEUE_ID)

    player.play.assert_awaited_once()  # type: ignore[attr-defined]
    ctrl.resume.assert_not_awaited()  # type: ignore[attr-defined]
    assert players.wait_for_player_update.call_args.args[0] == QUEUE_ID  # type: ignore[attr-defined]


async def test_play_unpauses_the_player_when_no_output_is_active() -> None:
    """Without an active output (a group player), play still goes to the queue's player."""
    ctrl, players, mass = _setup()
    player = _mock_player(mass, QUEUE_ID, "sync_group")
    _register(players, player)

    await ctrl._handle_play(QUEUE_ID)

    player.play.assert_awaited_once()  # type: ignore[attr-defined]
    ctrl.resume.assert_not_awaited()  # type: ignore[attr-defined]
    assert players.wait_for_player_update.call_args.args[0] == QUEUE_ID  # type: ignore[attr-defined]
