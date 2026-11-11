"""
Tests for the active output protocol lifecycle in play/stop command handling.

Covers two regressions from support#5771:
- a leftover ``active_output_protocol`` from a previous session must not be
  reused on a fresh playback start (it bypassed protocol selection entirely,
  e.g. a Sonos kept playing via AirPlay after leaving a sync group);
- a stop command arriving when the player already reports IDLE must still
  forward the stop to the pinned protocol and schedule the protocol clear
  (previously it returned early, leaving a stale session on the device and
  the protocol pinned forever).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, ProtocolRole
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.players import PlayerController
from tests.common import MockProtocol, MockProvider, attach_mock

if TYPE_CHECKING:
    from music_assistant.models.player import Player


class PlayableMockProtocol(MockProtocol):
    """MockProtocol that records play_media calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the mock protocol with play_media call recording."""
        super().__init__(*args, **kwargs)
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

    def _get_player_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_player_value = MagicMock(side_effect=_get_player_value)
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
) -> tuple[Player, PlayableMockProtocol, PlayableMockProtocol]:
    """
    Create a player of a native protocol, with a sendspin audio protocol as its active output.

    Returns the player, its native protocol and its audio protocol.

    :param mock_mass: The mocked server.
    :param controller: The players controller the player is registered in.
    :param protocol_state: The playback state the audio protocol reports.
    :param native_play_media: Whether the native protocol can play media itself.
    """
    native_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
    player, native = attach_mock(
        native_provider, "native_1", "Test Player", protocol_class=PlayableMockProtocol
    )
    if native_play_media:
        native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

    protocol_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
    _, audio = attach_mock(
        protocol_provider,
        "proto_1",
        "Test Protocol",
        role=ProtocolRole.AUDIO,
        player=player,
        protocol_class=PlayableMockProtocol,
    )
    audio._attr_playback_state = protocol_state
    player.set_active_output_protocol("proto_1")
    player.refresh_state(signal_event=False)
    assert controller.get_player(player.player_id) is player
    return player, cast("PlayableMockProtocol", native), cast("PlayableMockProtocol", audio)


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
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(player.player_id, media)

        assert native.play_media_calls == [media]
        assert audio.play_media_calls == []
        assert player.active_output_protocol == "native"

    async def test_playing_player_keeps_active_protocol(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """While a session is active the already-set protocol stays in use."""
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.PLAYING
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(player.player_id, media)

        assert audio.play_media_calls == [media]
        assert native.play_media_calls == []
        assert player.active_output_protocol == "proto_1"

    async def test_detached_active_protocol_falls_back_to_selection(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """A playing player whose active protocol detached plays through selection again."""
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.PLAYING
        )
        # the native protocol keeps the session going, so this is no fresh start
        native._attr_playback_state = PlaybackState.PLAYING
        mock_mass.config.get_player_object_config = AsyncMock(return_value=player.config)
        await controller.detach("proto_1")
        assert player.state.playback_state == PlaybackState.PLAYING
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(player.player_id, media)

        assert native.play_media_calls == [media]
        assert audio.play_media_calls == []
        assert player.active_output_protocol == "native"

    async def test_idle_player_reselects_best_protocol_without_native(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """Without native playback, re-selection picks the best protocol again."""
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE, native_play_media=False
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media(player.player_id, media)

        assert audio.play_media_calls == [media]
        assert native.play_media_calls == []
        assert player.active_output_protocol == "proto_1"


class TestCmdStopWithPinnedProtocol:
    """Stop on an already-idle player must still release the pinned protocol."""

    async def test_idle_stop_forwards_to_pinned_protocol(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """
        Stop on an idle player with a pinned protocol stops the protocol.

        Regression: when the source stream ended on its own before the stop
        command arrived, the early idle-return skipped the protocol stop, so the
        device kept a stale session (reported "playing" while silent) and the
        protocol stayed pinned forever.
        """
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        audio.stop = AsyncMock()  # type: ignore[method-assign]
        native.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop(player.player_id)

        audio.stop.assert_awaited_once()
        native.stop.assert_not_awaited()
        controller.schedule_active_output_protocol_clear.assert_called_once_with(player)

    async def test_idle_stop_without_pinned_protocol_is_noop(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """Stop on an idle player without an active protocol does nothing."""
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        player.set_active_output_protocol(None)
        player.refresh_state(signal_event=False)
        audio.stop = AsyncMock()  # type: ignore[method-assign]
        native.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop(player.player_id)

        audio.stop.assert_not_awaited()
        native.stop.assert_not_awaited()
        controller.schedule_active_output_protocol_clear.assert_not_called()

    async def test_idle_stop_grouped_protocol_keeps_pin(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """A protocol that still has group members keeps the pin."""
        player, _native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.IDLE
        )
        audio._attr_group_members = ["proto_1", "proto_2"]
        audio.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop(player.player_id)

        audio.stop.assert_awaited_once()
        controller.schedule_active_output_protocol_clear.assert_not_called()

    async def test_playing_stop_delegates_and_schedules_clear(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """The regular stop path (player playing) is unchanged."""
        player, native, audio = _make_player_with_protocol(
            mock_mass, controller, PlaybackState.PLAYING
        )
        audio.stop = AsyncMock()  # type: ignore[method-assign]
        native.stop = AsyncMock()  # type: ignore[method-assign]
        controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]

        await controller._handle_cmd_stop(player.player_id)

        audio.stop.assert_awaited_once()
        native.stop.assert_not_awaited()
        controller.schedule_active_output_protocol_clear.assert_called_once_with(player)
