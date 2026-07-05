"""Fixtures for DACP replay tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.airplay.provider import AirPlayProvider

_PLAYER_CMDS = (
    "cmd_play",
    "cmd_pause",
    "cmd_play_pause",
    "cmd_stop",
    "cmd_next_track",
    "cmd_previous_track",
    "cmd_volume_up",
    "cmd_volume_down",
    "cmd_ungroup",
)


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance for the AirPlay provider."""
    mass = MagicMock()
    mass.players = MagicMock()
    for name in _PLAYER_CMDS:
        setattr(mass.players, name, AsyncMock())
    mass.players.get_active_queue = MagicMock(
        return_value=MagicMock(queue_id="q1", shuffle_enabled=False)
    )
    mass.players.all_players = MagicMock(return_value=[])
    mass.player_queues = MagicMock()
    mass.player_queues.set_shuffle = AsyncMock()
    mass.call_later = MagicMock()
    mass.cancel_timer = MagicMock()
    mass.config.get_raw_player_config_value = MagicMock(return_value=False)
    return mass


@pytest.fixture
def manifest_mock() -> MagicMock:
    """Return a mock provider manifest."""
    manifest = MagicMock()
    manifest.domain = "airplay"
    manifest.name = "AirPlay"
    return manifest


@pytest.fixture
def config_mock() -> MagicMock:
    """Return a mock provider config (GLOBAL log level so construction doesn't fail)."""
    config = MagicMock()
    config.instance_id = "airplay_test"
    config.get_value = MagicMock(return_value="GLOBAL")
    return config


@pytest.fixture
def airplay_provider(
    mock_mass: MagicMock, manifest_mock: MagicMock, config_mock: MagicMock
) -> AirPlayProvider:
    """
    Build a real AirPlayProvider backed by the mock mass.

    handle_async_init (DACP server / zeroconf) is intentionally not called; only the
    request handler is exercised. The default logger level keeps VERBOSE capture off.
    """
    return AirPlayProvider(mock_mass, manifest_mock, config_mock)


def make_player(  # noqa: PLR0913
    mass: MagicMock,
    *,
    active_remote: str = "123",
    player_id: str = "ap123",
    playing: bool = True,
    manufacturer: str = "Test",
    synced_to: str | None = None,
    transitioning: bool = False,
    stream_connected: bool = True,
    prevent_playback: bool = False,
    has_session: bool = True,
    has_stream: bool = True,
) -> MagicMock:
    """
    Make the mock mass return a single mock AirPlay player, and return it.

    Each call replaces the mass player list, so calling it twice does not yield two
    players.

    :param mass: The mock_mass fixture the provider looks players up through.
    :param active_remote: Active-Remote id the player's stream answers to.
    :param player_id: Player id used in dispatched commands.
    :param playing: Whether the player reports PLAYING (else IDLE).
    :param manufacturer: Device manufacturer ("Apple" triggers ignore-volume).
    :param synced_to: Parent player id when synced, else None.
    :param transitioning: Value of player._transitioning.
    :param stream_connected: Whether the stream reports connected.
    :param prevent_playback: Initial stream.prevent_playback flag.
    :param has_session: Whether the stream has an active session.
    :param has_stream: Whether the player has a stream at all.
    """
    player = MagicMock()
    player.player_id = player_id
    player.name = "Test Player"
    player.protocol_parent_id = None
    player.synced_to = synced_to
    player._transitioning = transitioning
    state = PlaybackState.PLAYING if playing else PlaybackState.IDLE
    player.playback_state = state
    player.state.playback_state = state
    player.state.active_group = None
    player.device_info.manufacturer = manufacturer
    player.volume_muted = False
    player.volume_level = 50
    if has_stream:
        stream = MagicMock()
        stream.active_remote_id = active_remote
        stream.running = True
        stream.connected = stream_connected
        stream.prevent_playback = prevent_playback
        stream.session = MagicMock() if has_session else None
        stream.stop = AsyncMock()
        stream.send_cli_command = AsyncMock()
        player.stream = stream
    else:
        player.stream = None
    mass.players.all_players.return_value = [player]
    return player
