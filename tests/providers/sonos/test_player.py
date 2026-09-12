"""Tests for the Sonos player connection/reconnect handling."""

import asyncio
import logging
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ConnectionTimeoutError
from aiosonos.api.models import MusicService
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.exceptions import CannotConnect, FailedCommand
from music_assistant_models.enums import PlaybackState, RepeatMode
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import EXTERNAL_PAUSE_IDLE_TIMEOUT
from music_assistant.mass import MusicAssistant
from music_assistant.providers.sonos.const import PLAYER_SOURCE_MAP, SOURCE_SPOTIFY
from music_assistant.providers.sonos.player import SonosPlayer


def _bind_player(mass: MusicAssistant | MagicMock) -> tuple[SonosPlayer, MagicMock]:
    """Create a SonosPlayer bound to the given MusicAssistant, with a mocked client."""
    player = SonosPlayer.__new__(SonosPlayer)
    client = MagicMock()
    client.disconnect = AsyncMock()
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.player")
    player._player_id = "sonos_player"
    player._listen_task = None
    player.connected = False
    player.client = client
    player._on_unload_callbacks = []
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player, client


def _make_player() -> tuple[SonosPlayer, MagicMock]:
    """Create a SonosPlayer with mocked connection dependencies."""
    mass = MagicMock()
    mass.closing = False
    mass.players.get_player.return_value = MagicMock()
    player, _ = _bind_player(mass)
    return player, mass


async def _connect_player(player: SonosPlayer, client: MagicMock) -> None:
    """Connect the player to a listener that stays alive until it is cancelled."""

    async def _start_listening(init_ready: asyncio.Event) -> None:
        init_ready.set()
        await asyncio.sleep(3600)

    client.connect = AsyncMock()
    client.start_listening = _start_listening
    await player._connect()


@pytest.mark.asyncio
async def test_connect_timeout_reschedules_reconnect() -> None:
    """Test a blackholed connection (timeout, not refused) still schedules a retry."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionTimeoutError("Connection timeout to host https://x:1443")
    )

    await player._connect(retry_on_fail=30)

    assert player._attr_available is False
    mass.call_later.assert_called_once()
    args, _ = mass.call_later.call_args
    assert args[0] == min(30 + 30, 3600)
    assert args[1] == player._connect


@pytest.mark.asyncio
async def test_connect_timeout_without_retry_raises() -> None:
    """Test a connection failure without retry_on_fail still propagates."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionTimeoutError("Connection timeout to host https://x:1443")
    )

    with pytest.raises(ConnectionTimeoutError):
        await player._connect(retry_on_fail=0)

    mass.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_connect_websocket_handshake_failure_reschedules_reconnect() -> None:
    """Test a websocket handshake failure also reschedules a retry."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=CannotConnect(OSError("handshake failed"))
    )

    await player._connect(retry_on_fail=30)

    assert player._attr_available is False
    mass.call_later.assert_called_once()


@pytest.mark.asyncio
async def test_on_unload_disconnects_without_reconnecting(timer_mass: MusicAssistant) -> None:
    """Test an unloaded player disconnects and its aborted listener does not reconnect."""
    player, client = _bind_player(timer_mass)
    await _connect_player(player, client)
    listener = player._listen_task
    assert listener is not None

    await player.on_unload()

    assert player.connected is False
    client.disconnect.assert_awaited_once()
    # let the aborted listener run its cleanup
    with pytest.raises(asyncio.CancelledError):
        await listener
    assert timer_mass._tracked_timers == {}


@pytest.mark.asyncio
async def test_on_unload_cancels_a_pending_reconnect(timer_mass: MusicAssistant) -> None:
    """Test a reconnect that is still waiting to fire does not connect after the unload."""
    player, _ = _bind_player(timer_mass)
    connect_attempts: list[int] = []

    async def _connect(retry_on_fail: int = 0) -> None:
        connect_attempts.append(retry_on_fail)

    player._connect = _connect  # type: ignore[method-assign]
    player.reconnect(0)
    handle = timer_mass._tracked_timers[f"sonos_reconnect_{player.player_id}"]

    await player.on_unload()
    await asyncio.sleep(0.05)

    assert handle.cancelled()
    assert connect_attempts == []


@pytest.mark.asyncio
async def test_on_unload_cancels_a_scheduled_airplay_group_restore(
    timer_mass: MusicAssistant,
) -> None:
    """Test the AirPlay group restore scheduled for a player does not run after the unload."""
    player, client = _bind_player(timer_mass)
    player._attr_name = "Sonos Player"
    client.player.is_coordinator = True
    client.player.group_members = [player.player_id, "sonos_player_2"]
    output_protocol = MagicMock()
    output_protocol.protocol_domain = "airplay"

    await player.on_protocol_playback(output_protocol)
    handle = timer_mass._tracked_timers[f"restore_airplay_group_{player.player_id}"]

    await player.on_unload()

    assert handle.cancelled()
    assert timer_mass._tracked_timers == {}


@pytest.mark.asyncio
async def test_on_unload_cancels_an_airplay_group_restore_that_already_started(
    timer_mass: MusicAssistant,
) -> None:
    """Test a group restore that already started is aborted when the player is unloaded."""
    player, _ = _bind_player(timer_mass)
    restoring = asyncio.Event()

    async def _restore_airplay_group() -> None:
        restoring.set()
        await asyncio.sleep(5)

    task_id = f"restore_airplay_group_{player.player_id}"
    timer_mass.call_later(0, _restore_airplay_group, task_id=task_id)
    await restoring.wait()
    task = timer_mass._tracked_tasks[task_id]

    await player.on_unload()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_on_unload_unsubscribes_before_disconnecting(timer_mass: MusicAssistant) -> None:
    """Test the registered unload callbacks run before the client is disconnected."""
    player, client = _bind_player(timer_mass)
    calls: list[str] = []
    player._on_unload_callbacks.append(lambda: calls.append("unsubscribe"))
    client.disconnect = AsyncMock(side_effect=lambda: calls.append("disconnect"))

    await player.on_unload()

    assert calls == ["unsubscribe", "disconnect"]


@pytest.mark.asyncio
async def test_a_listener_ending_during_the_unload_cannot_rearm_a_reconnect(
    timer_mass: MusicAssistant,
) -> None:
    """Test a listener that ends while the player is unloading does not schedule a reconnect."""
    player, client = _bind_player(timer_mass)
    socket_drops = asyncio.Event()

    async def _start_listening(init_ready: asyncio.Event) -> None:
        init_ready.set()
        await socket_drops.wait()
        raise ConnectionResetError("socket dropped")

    client.connect = AsyncMock()
    client.start_listening = _start_listening
    await player._connect()
    listener = player._listen_task
    assert listener is not None

    # release the listener so it is queued to run its finally, then unload without
    # yielding in between: the cancellations and connected=False must be indivisible
    socket_drops.set()
    await player.on_unload()

    with pytest.raises(asyncio.CancelledError):
        await listener
    assert timer_mass._tracked_timers == {}
    assert timer_mass._tracked_tasks == {}


@pytest.mark.asyncio
async def test_on_unload_survives_a_failing_disconnect(timer_mass: MusicAssistant) -> None:
    """Test a speaker that cannot be disconnected does not abort the unload."""
    player, client = _bind_player(timer_mass)
    client.disconnect = AsyncMock(side_effect=OSError("speaker unreachable"))
    player.reconnect(0)

    await player.on_unload()

    assert player.connected is False
    assert timer_mass._tracked_timers == {}


def _make_externally_paused_player() -> tuple[SonosPlayer, MagicMock, MagicMock]:
    """Create a player reporting a paused external source, as Sonos does for Spotify Connect."""
    mass = MagicMock()
    mass.closing = False
    player, client = _bind_player(mass)
    player._attr_playback_state = PlaybackState.PAUSED
    player._attr_active_source = SOURCE_SPOTIFY
    player._attr_current_media = PlayerMedia(uri="spotify:track:1", title="Shout")
    return player, mass, client


def _refuse_to_resume(client: MagicMock) -> None:
    """Let the speaker reject the play command, as it does for a session it no longer has."""
    client.player.is_passive = False
    client.player.group.play = AsyncMock(side_effect=FailedCommand("ERROR_PLAYBACK_FAILED"))


@pytest.mark.asyncio
async def test_a_source_that_refuses_to_resume_is_ended_right_away() -> None:
    """Test a dead session does not have to sit out the grace period to be given up on."""
    player, _, client = _make_externally_paused_player()
    _refuse_to_resume(client)

    await player.play()

    assert player._attr_playback_state is PlaybackState.IDLE
    assert player._attr_active_source is None
    assert player._attr_current_media is None


@pytest.mark.asyncio
async def test_a_failing_play_on_our_own_queue_still_raises() -> None:
    """Test a failure that is not about a stale external source is not swallowed."""
    player, _, client = _make_externally_paused_player()
    player._attr_active_source = None
    _refuse_to_resume(client)

    with pytest.raises(FailedCommand):
        await player.play()


@pytest.mark.asyncio
async def test_a_coordinator_change_does_not_end_a_live_source() -> None:
    """Test the race the speaker reports while regrouping is not read as a source that is gone."""
    player, _, client = _make_externally_paused_player()
    client.player.is_passive = False
    client.player.group.play = AsyncMock(
        side_effect=FailedCommand("ERROR_PLAYBACK_FAILED groupCoordinatorChanged")
    )

    with pytest.raises(FailedCommand):
        await player.play()

    assert player._attr_playback_state is PlaybackState.PAUSED
    assert player._attr_active_source == SOURCE_SPOTIFY


def _connected_player() -> tuple[SonosPlayer, MagicMock, MagicMock]:
    """Create a connected player with the attributes the state calculation reads."""
    mass = MagicMock()
    mass.closing = False
    player, client = _bind_player(mass)
    player.connected = True
    player._attr_source_list = []
    player._attr_group_members = []
    player._attr_can_group_with = set()
    player._provider = MagicMock(instance_id="sonos")
    return player, mass, client


def _report_paused_spotify(group: MagicMock) -> None:
    """Let the given group report the paused Spotify Connect state we captured."""
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PAUSED
    group.position = 42.0
    group.container_type = "spotify.connect"
    group.active_service = MusicService.SPOTIFY
    group.playback_metadata = {
        "container": {"name": "Spotify", "service": {"name": "Spotify"}},
        "currentItem": {"id": "1", "track": {"name": "Shout"}},
    }
    group.playback_actions.raw_data = {"canShuffle": True, "canRepeat": True, "canRepeatOne": True}
    # a bare MagicMock attribute is truthy, so the play modes are spelled out
    group.play_modes.shuffle = True
    group.play_modes.repeat = False
    group.play_modes.repeat_one = True


def _speaker_reporting_paused_spotify() -> tuple[SonosPlayer, MagicMock]:
    """Create a connected player whose speaker reports the Spotify Connect state we captured."""
    player, mass, client = _connected_player()
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    _report_paused_spotify(client.player.group)
    return player, mass


def test_a_paused_connect_session_is_handed_to_the_stale_source_check() -> None:
    """Test what the speaker reports for Spotify Connect reaches the shared grace period."""
    player, _ = _speaker_reporting_paused_spotify()

    player.on_player_event(None)

    assert player._attr_playback_state is PlaybackState.PAUSED
    assert player._attr_active_source == SOURCE_SPOTIFY
    # giving up on such a source is handled for every player alike, from update_state,
    # so the speaker only has to opt in and let the state calculation see it
    assert player._attr_external_pause_idle_timeout == EXTERNAL_PAUSE_IDLE_TIMEOUT
    player.update_state.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_set_shuffle_is_forwarded_to_the_speaker() -> None:
    """Test the shuffle command reaches the source the speaker runs itself."""
    player, client = _bind_player(MagicMock())
    client.player.group.set_play_modes = AsyncMock()

    await player.set_shuffle(True)

    client.player.group.set_play_modes.assert_awaited_once_with(shuffle=True)


@pytest.mark.parametrize(
    ("repeat_mode", "repeat", "repeat_one"),
    [
        (RepeatMode.OFF, False, False),
        (RepeatMode.ALL, True, False),
        (RepeatMode.ONE, False, True),
    ],
)
@pytest.mark.asyncio
async def test_set_repeat_is_forwarded_to_the_speaker(
    repeat_mode: RepeatMode, repeat: bool, repeat_one: bool
) -> None:
    """Test each repeat mode reaches the speaker as the two flags Sonos knows."""
    player, client = _bind_player(MagicMock())
    client.player.group.set_play_modes = AsyncMock()

    await player.set_repeat(repeat_mode)

    client.player.group.set_play_modes.assert_awaited_once_with(
        repeat=repeat, repeat_one=repeat_one
    )


def test_a_connect_session_reports_its_play_modes() -> None:
    """Test the play modes of a source the speaker runs itself land on its source list entry."""
    player, _ = _speaker_reporting_paused_spotify()

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source.can_shuffle is True
    assert source.can_repeat is True
    assert source.shuffle_enabled is True
    # the template is shared with every other Sonos player, so it may not be touched
    assert PLAYER_SOURCE_MAP[SOURCE_SPOTIFY].can_shuffle is False
    assert PLAYER_SOURCE_MAP[SOURCE_SPOTIFY].shuffle_enabled is None


def test_the_play_modes_of_a_source_that_stopped_are_dropped() -> None:
    """Test a source that is no longer playing stops reporting its last play modes."""
    player, _ = _speaker_reporting_paused_spotify()
    player.on_player_event(None)
    group = cast("MagicMock", player.client.player.group)
    group.active_service = MusicService.MUSIC_ASSISTANT
    group.container_type = None
    del group.playback_metadata["container"]["service"]

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source is PLAYER_SOURCE_MAP[SOURCE_SPOTIFY]
    assert source.can_shuffle is False
    assert source.shuffle_enabled is None
    assert source.repeat_mode is None


@pytest.mark.parametrize(
    ("repeat", "repeat_one", "repeat_mode"),
    [
        (False, False, RepeatMode.OFF),
        (True, False, RepeatMode.ALL),
        (True, True, RepeatMode.ONE),
        (None, None, None),
    ],
)
def test_a_connect_session_reports_its_repeat_mode(
    repeat: bool | None, repeat_one: bool | None, repeat_mode: RepeatMode | None
) -> None:
    """Test the two repeat flags Sonos reports are mapped to the repeat mode they mean."""
    player, _ = _speaker_reporting_paused_spotify()
    group = cast("MagicMock", player.client.player.group)
    group.play_modes.repeat = repeat
    group.play_modes.repeat_one = repeat_one

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source.repeat_mode is repeat_mode


def test_a_source_that_only_offers_repeat_one_does_not_advertise_repeat() -> None:
    """Test repeat is only offered when the content can repeat all, the first step of the cycle."""
    player, _ = _speaker_reporting_paused_spotify()
    group = cast("MagicMock", player.client.player.group)
    group.playback_actions.raw_data = {
        "canShuffle": False,
        "canRepeat": False,
        "canRepeatOne": True,
    }

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source.can_repeat is False
    assert source.can_shuffle is False


def test_a_group_child_reports_the_play_modes_of_its_coordinator() -> None:
    """Test a player synced to another one reads the play modes of the group it plays in."""
    player, mass, client = _connected_player()
    client.player.is_coordinator = False
    client.player.group.coordinator_id = "sonos_leader"
    client.player.group.playback_actions.raw_data = {"canShuffle": False}
    client.player.group.play_modes.shuffle = None
    client.player.group.play_modes.repeat = None
    client.player.group.play_modes.repeat_one = None
    group_parent = MagicMock()
    _report_paused_spotify(group_parent.client.player.group)
    mass.players.get_player.return_value = group_parent

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source.can_shuffle is True
    assert source.shuffle_enabled is True
