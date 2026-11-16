"""Tests for showing and controlling an app on an audio protocol MA did not start."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, ProtocolRole
from music_assistant_models.player import PlayerMedia, PlayerSource

from music_assistant.controllers.players import PlayerController
from tests.common import MockProtocol, MockProvider, attach_mock

if TYPE_CHECKING:
    from music_assistant.models.player import Player


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_player_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
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


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("mock_provider", instance_id="mock_instance", mass=mock_mass)


def _linked(
    provider: MockProvider,
    controller: PlayerController,
    app_state: PlaybackState = PlaybackState.PLAYING,
    app: str | None = "tunein",
) -> tuple[Player, MockProtocol, MockProtocol]:
    """
    Build an idle player with an audio protocol that plays an app.

    Returns the player, its native protocol and its audio protocol.

    :param provider: The provider of the native protocol.
    :param controller: The controller that knows the player.
    :param app_state: The playback state the audio protocol reports.
    :param app: The source the audio protocol reports, or None for MA's own playback.
    """
    player, native = attach_mock(provider, "native_1", "Room")
    native._attr_playback_state = PlaybackState.IDLE
    native._attr_supported_features = set()
    cast_provider = MockProvider("chromecast", instance_id="chromecast", mass=provider.mass)
    _, cast = attach_mock(cast_provider, "cast_1", "Cast", role=ProtocolRole.AUDIO, player=player)
    cast._attr_playback_state = app_state
    cast._attr_active_source = app
    cast._attr_source_list = [
        PlayerSource(
            id="tunein",
            name="TuneIn",
            passive=True,
            can_play_pause=True,
            can_seek=True,
            can_next_previous=True,
        )
    ]
    cast._attr_current_media = PlayerMedia(uri="https://radio.example", title="Radio 538")
    cast._attr_elapsed_time = 12.0
    cast._attr_elapsed_time_last_updated = 1000.0
    cast._attr_supported_features = {
        PlayerFeature.PAUSE,
        PlayerFeature.SEEK,
        PlayerFeature.NEXT_PREVIOUS,
        PlayerFeature.ENQUEUE,
    }
    player.refresh_state(signal_event=False)
    assert controller.get_player(player.player_id) is player
    return player, native, cast


def test_the_app_shows_on_an_idle_player(
    provider: MockProvider, controller: PlayerController
) -> None:
    """Source, state, position, media and controls come from the audio protocol."""
    player, _, _ = _linked(provider, controller)

    state = player.state
    assert state.active_source == "tunein"
    assert state.playback_state == PlaybackState.PLAYING
    assert state.elapsed_time == 12.0
    assert state.current_media is not None
    assert state.current_media.title == "Radio 538"
    source = next(x for x in state.source_list if x.id == "tunein")
    assert source.can_seek
    assert PlayerFeature.SEEK in state.supported_features
    # what follows the audio MA plays is not taken over
    assert PlayerFeature.ENQUEUE not in state.supported_features


def test_the_app_replaces_the_same_service_among_own_sources(
    provider: MockProvider, controller: PlayerController
) -> None:
    """The player's own TuneIn does not offer what the app does."""
    player, native, _ = _linked(provider, controller)
    native._attr_source_list = [PlayerSource(id="tunein", name="Own TuneIn", passive=True)]

    player.refresh_state(signal_event=False)

    source = next(x for x in player.source_list if x.id == "tunein")
    assert source.name == "TuneIn"


def test_a_paused_app_shows_on_an_idle_player(
    provider: MockProvider, controller: PlayerController
) -> None:
    """An app paused from here stays on the player, to be resumed."""
    player, _, _ = _linked(provider, controller, PlaybackState.PAUSED)

    assert player.active_source == "tunein"
    assert player.playback_state == PlaybackState.PAUSED


@pytest.mark.parametrize(
    ("own_state", "app_state", "shown"),
    [
        (PlaybackState.PLAYING, PlaybackState.PLAYING, False),
        (PlaybackState.PAUSED, PlaybackState.PAUSED, False),
        (PlaybackState.PAUSED, PlaybackState.PLAYING, True),
        (PlaybackState.IDLE, PlaybackState.IDLE, False),
    ],
    ids=["player_plays", "both_paused", "app_plays_over_paused", "app_idle"],
)
def test_the_app_shows_only_when_the_player_plays_nothing_itself(
    provider: MockProvider,
    controller: PlayerController,
    own_state: PlaybackState,
    app_state: PlaybackState,
    shown: bool,
) -> None:
    """The player's own playback outranks an app, unless an app plays over a paused player."""
    player, native, cast = _linked(provider, controller, app_state)
    native._attr_playback_state = own_state

    player.refresh_state(signal_event=False)

    assert (player.foreign_protocol is cast) is shown


def test_ma_playback_through_the_protocol_is_no_app(
    provider: MockProvider, controller: PlayerController
) -> None:
    """MA's own session reports no source, and an active protocol is handled as ever."""
    player, _, _ = _linked(provider, controller, app=None)
    assert player.foreign_protocol is None

    player, _, cast = _linked(provider, controller)
    player.set_active_output_protocol("cast_1")
    player.refresh_state(signal_event=False)
    assert cast.playback_state == PlaybackState.PLAYING
    assert player.foreign_protocol is None


async def test_commands_go_to_the_protocol_player(
    provider: MockProvider, controller: PlayerController
) -> None:
    """Stop, pause and play reach the app, not the native protocol that plays nothing."""
    player, native, cast = _linked(provider, controller)
    controller.schedule_active_output_protocol_clear = MagicMock()  # type: ignore[method-assign]
    with (
        patch.object(native, "pause", AsyncMock()) as own_pause,
        patch.object(native, "stop", AsyncMock()) as own_stop,
        patch.object(native, "play", AsyncMock()) as own_play,
        patch.object(cast, "pause", AsyncMock()) as app_pause,
        patch.object(cast, "stop", AsyncMock()) as app_stop,
        patch.object(cast, "play", AsyncMock()) as app_play,
    ):
        await controller._handle_cmd_pause(player.player_id)
        await controller._handle_cmd_stop(player.player_id)
        cast._attr_playback_state = PlaybackState.PAUSED
        player.refresh_state(signal_event=False)
        await controller._handle_cmd_play(player.player_id)

    app_pause.assert_awaited_once()
    app_stop.assert_awaited_once()
    app_play.assert_awaited_once()
    own_pause.assert_not_awaited()
    own_stop.assert_not_awaited()
    own_play.assert_not_awaited()


async def test_seek_and_skip_go_to_the_protocol_player(
    provider: MockProvider, controller: PlayerController
) -> None:
    """Seeking and skipping are sent to the protocol that plays the app."""
    player, native, cast = _linked(provider, controller)
    with (
        patch.object(native, "seek", AsyncMock()) as own_seek,
        patch.object(cast, "seek", AsyncMock()) as app_seek,
        patch.object(cast, "next_track", AsyncMock()) as app_next,
        patch.object(cast, "previous_track", AsyncMock()) as app_previous,
    ):
        await controller.cmd_seek(player.player_id, 30)
        await controller.cmd_next_track(player.player_id)
        await controller.cmd_previous_track(player.player_id)

    app_seek.assert_awaited_once_with(30)
    app_next.assert_awaited_once()
    app_previous.assert_awaited_once()
    own_seek.assert_not_awaited()


def test_the_update_marks_the_protocol_of_the_app_while_it_plays(
    provider: MockProvider, controller: PlayerController
) -> None:
    """The update for an app that starts or stops changes which protocol is marked active."""
    player, _, cast = _linked(provider, controller, app=None)
    signal = MagicMock()
    controller.signal_player_state_update = signal  # type: ignore[method-assign]

    cast._attr_active_source = "tunein"
    player.update_state()
    cast._attr_playback_state = PlaybackState.IDLE
    player.update_state()

    marked = [
        [
            [info.protocol_id for info in protocols if info.active]
            for protocols in call.args[1]["protocols"]
        ]
        for call in signal.call_args_list
    ]
    assert marked == [[[], ["cast_1"]], [["cast_1"], []]]
