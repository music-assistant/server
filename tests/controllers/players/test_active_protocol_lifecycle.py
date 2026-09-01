"""
Tests for the active output protocol lifecycle in play/stop command handling.

Covers two regressions from support#5771:
- a leftover ``active_output_protocol`` from a previous session must not be
  reused on a fresh playback start (it bypassed protocol selection entirely,
  e.g. a Sonos kept playing via AirPlay after leaving a sync group);
- a stop command arriving when the player already reports IDLE must still
  forward the stop to the pinned protocol player and schedule the protocol
  clear (previously it returned early, leaving a stale session on the device
  and the protocol pinned forever).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import LinkedOutputProtocol
from tests.common import MockPlayer, MockProvider


class PlayableMockPlayer(MockPlayer):
    """MockPlayer that records play_media calls."""

    def __init__(
        self,
        provider: MockProvider,
        player_id: str,
        name: str,
        player_type: PlayerType = PlayerType.PLAYER,
    ) -> None:
        """Initialize the mock player with play_media call recording."""
        super().__init__(provider, player_id, name, player_type=player_type)
        self.play_media_calls: list[PlayerMedia] = []

    async def play_media(self, media: PlayerMedia) -> None:
        """Record the play_media call."""
        self.play_media_calls.append(media)


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])

    def _get_raw_player_config_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_player_config_value)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.player_queues = MagicMock()
    mass.player_queues.get = MagicMock(return_value=None)
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    ctrl = PlayerController(mock_mass)
    mock_mass.players = ctrl
    return ctrl


def _make_player_with_protocol(
    mock_mass: MagicMock,
    controller: PlayerController,
    protocol_state: PlaybackState,
    native_play_media: bool = True,
) -> tuple[PlayableMockPlayer, PlayableMockPlayer]:
    """Create a native player with one linked sendspin protocol player."""
    native_provider = MockProvider("sonos", mass=mock_mass)
    player = PlayableMockPlayer(native_provider, "player_1", "Test Player")
    if native_play_media:
        player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

    protocol_provider = MockProvider("sendspin", mass=mock_mass)
    protocol_player = PlayableMockPlayer(
        protocol_provider, "proto_1", "Test Protocol", player_type=PlayerType.PROTOCOL
    )
    protocol_player._attr_playback_state = protocol_state

    controller._players = {"player_1": player, "proto_1": protocol_player}
    player.set_linked_output_protocols(
        [
            LinkedOutputProtocol(
                output_protocol_id="proto_1",
                protocol_domain="sendspin",
                priority=40,
            )
        ]
    )
    player.set_active_output_protocol("proto_1")
    protocol_player.update_state(signal_event=False)
    player.refresh_state(signal_event=False)
    return player, protocol_player


class TestPlayMediaProtocolSelection:
    """A fresh playback start must re-select instead of reusing a stale protocol."""

    async def test_idle_player_reselects_instead_of_stale_protocol(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """
        An idle player with a leftover active protocol re-runs selection.

        Regression: a Sonos grouped via AirPlay kept the AirPlay protocol pinned
        after leaving the group, so the next standalone playback used AirPlay
        instead of native playback.
        """
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("player_1", media)

        assert player.play_media_calls == [media]
        assert protocol_player.play_media_calls == []
        assert player.active_output_protocol == "native"

    async def test_playing_player_keeps_active_protocol(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """While a session is active the already-set protocol stays in use."""
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.PLAYING
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("player_1", media)

        assert protocol_player.play_media_calls == [media]
        assert player.play_media_calls == []
        assert player.active_output_protocol == "proto_1"

    async def test_unlinked_active_protocol_falls_back_to_selection(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """An active protocol that is no longer linked falls back to selection."""
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.PLAYING
        )
        # simulate the protocol link being removed while the id stays pinned
        player.set_linked_output_protocols([])
        player.refresh_state(signal_event=False)
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("player_1", media)

        assert player.play_media_calls == [media]
        assert protocol_player.play_media_calls == []
        assert player.active_output_protocol == "native"

    async def test_idle_player_reselects_best_protocol_without_native(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """Without native playback, re-selection picks the best protocol again."""
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE, native_play_media=False
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("player_1", media)

        assert protocol_player.play_media_calls == [media]
        assert player.play_media_calls == []
        assert player.active_output_protocol == "proto_1"


class TestCmdStopWithPinnedProtocol:
    """Stop on an already-idle player must still release the pinned protocol."""

    async def test_idle_stop_forwards_to_pinned_protocol(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """
        Stop on an idle player with a pinned protocol stops the protocol player.

        Regression: when the source stream ended on its own before the stop
        command arrived, the early idle-return skipped the protocol stop, so the
        device kept a stale session (reported "playing" while silent) and the
        protocol stayed pinned forever.
        """
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        protocol_player.stop = AsyncMock()  # type: ignore[method-assign]
        player.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop("player_1")

        protocol_player.stop.assert_awaited_once()
        player.stop.assert_not_awaited()
        controller.schedule_active_output_protocol_clear.assert_called_once_with(player)

    async def test_idle_stop_without_pinned_protocol_is_noop(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """Stop on an idle player without an active protocol does nothing."""
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        player.set_active_output_protocol(None)
        player.refresh_state(signal_event=False)
        protocol_player.stop = AsyncMock()  # type: ignore[method-assign]
        player.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop("player_1")

        protocol_player.stop.assert_not_awaited()
        player.stop.assert_not_awaited()
        controller.schedule_active_output_protocol_clear.assert_not_called()

    async def test_idle_stop_grouped_protocol_keeps_pin(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """A protocol player that still has group members keeps the pin."""
        _player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        protocol_player._attr_group_members = ["proto_1", "proto_2"]
        protocol_player.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop("player_1")

        protocol_player.stop.assert_awaited_once()
        controller.schedule_active_output_protocol_clear.assert_not_called()

    async def test_playing_stop_delegates_and_schedules_clear(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """The regular stop path (player playing) is unchanged."""
        player, protocol_player = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.PLAYING
        )
        protocol_player.stop = AsyncMock()  # type: ignore[method-assign]
        player.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop("player_1")

        protocol_player.stop.assert_awaited_once()
        player.stop.assert_not_awaited()
        controller.schedule_active_output_protocol_clear.assert_called_once_with(player)
