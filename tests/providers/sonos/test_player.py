"""Tests for the Sonos player connection/reconnect handling."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ConnectionTimeoutError
from aiosonos.api.models import ContainerType, MusicService, PlaybackError
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import PlaybackErrorEvent
from aiosonos.exceptions import CannotConnect, FailedCommand
from aiosonos.group import PlaybackActions
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ContentType,
    IdentifierType,
    MediaType,
    PlaybackState,
    ProtocolRole,
    RepeatMode,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import DeviceInfo, OutputProtocol, PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import EXTERNAL_PAUSE_IDLE_TIMEOUT, PlaybackQuality
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_protocol import AnnouncementFeature
from music_assistant.providers.sonos.const import (
    PLAYER_SOURCE_MAP,
    SOURCE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_SPOTIFY,
    SOURCE_TV,
    UPNP_PORT,
)
from music_assistant.providers.sonos.player import SonosPlayer, _has_device_feature

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo as SonosDiscoveryInfo
    from aiosonos.api.models import PlaybackActions as PlaybackActionsData


def _actions(flags: dict[str, bool]) -> PlaybackActions:
    """Build the playback actions a speaker reports, from the flags it sent."""
    return PlaybackActions(cast("PlaybackActionsData", flags))


def _bind_player(mass: MusicAssistant | MagicMock) -> tuple[SonosPlayer, MagicMock]:
    """Create a SonosPlayer bound to the given MusicAssistant, with a mocked client."""
    player = SonosPlayer.__new__(SonosPlayer)
    client = MagicMock(player_ip="192.168.1.20")
    client.disconnect = AsyncMock()
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.player")
    player._protocol_id = "sonos_player"
    player._player = None
    player._listen_task = None
    player.connected = False
    player._connect_lock = asyncio.Lock()
    player._sleeps = False
    player._wol_mac = None
    player._marked_asleep = False
    player._woken_from_sleep = False
    player.cloud_queue_id = None
    player.group_queue_id = None
    player._stepping_out = False
    player._taken_over_item_id = None
    player.cloud_queue_item_generation = 0
    player._window_item_ids = {}
    player._served_bindings = {}
    player._evicted_cloud_queue = None
    player._flow_queue_id = None
    player._flow_media = None
    player._queue_session_id = None
    player._hand_over_tasks = set()
    player._airplay_rejoin_members = set()
    player._own_airplay_session = False
    player._reported_bonded_ids = set()
    player._line_in_connected = None
    player.upnp = MagicMock(
        follow=AsyncMock(), unfollow=AsyncMock(), follows=False, player_ip="192.168.1.20"
    )
    player._upnp_port = UPNP_PORT
    player._attr_options = []
    player.discovery_info = cast(
        "SonosDiscoveryInfo",
        {"device": {"modelDisplayName": "Era 100", "capabilities": ["PLAYBACK"]}},
    )
    client.player.has_subwoofer = False
    client.player.has_surrounds = False
    client.player.is_stereo_pair = False
    client.player.bonded_player_ids = []
    player.client = client
    player._provider = MagicMock(get_protocol=lambda pid: player.mass.players.get_protocol(pid))
    player._on_detach_callbacks = []
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player, client


def _attach_to_player(protocol: SonosPlayer) -> Player:
    """Attach a SonosPlayer to a player of its own, with a minted id, and return that player."""
    player = Player(
        protocol.mass,
        "pl0123456789ab",
        PlayerConfig(values={}, provider="sonos", player_id="pl0123456789ab"),
    )
    player.add_protocol(protocol, ProtocolRole.NATIVE)
    protocol.set_player(player)
    return player


def _make_player() -> tuple[SonosPlayer, MagicMock]:
    """Create a SonosPlayer with mocked connection dependencies."""
    mass = MagicMock()
    mass.closing = False
    mass.players.get_protocol.return_value = MagicMock()
    player, _ = _bind_player(mass)
    return player, mass


def _make_named_player(name: str) -> SonosPlayer:
    """Create a SonosPlayer that reports the given name as its display name."""
    player, mass = _make_player()
    player._cache = {}
    player._attr_name = name
    # the display name prefers the name set in the player config, which has none here
    player._config = MagicMock()
    player._config.name = None
    mass.streams.base_url = "http://192.168.1.10:9097"
    return player


def _playback_error(**fields: object) -> PlaybackErrorEvent:
    """Build the event a speaker sends when it fails to play an item."""
    body = cast(
        "PlaybackError",
        {
            "_objectType": "playbackError",
            "errorCode": "ERROR_PLAYBACK_FAILED",
            "reason": "ERROR_NO_RESOURCE",
            "itemId": "abc@3",
            "trackName": "Long Run 11",
            **fields,
        },
    )
    return PlaybackErrorEvent(SonosEventType.PLAYBACK_ERROR, "group1", body)


def _scheduled_tasks(mass: MagicMock) -> list[Coroutine[Any, Any, None]]:
    """Collect the coroutines the player schedules on the given mass instead of running them."""
    scheduled: list[Coroutine[Any, Any, None]] = []
    mass.create_task.side_effect = lambda coro, **_kwargs: scheduled.append(coro)
    return scheduled


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
@pytest.mark.parametrize("retry_on_fail", [3590, 3600])
async def test_connect_timeout_caps_reconnect_delay(retry_on_fail: int) -> None:
    """Test repeated timeouts never increase the retry delay beyond one hour."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionTimeoutError("Connection timeout to host https://x:1443")
    )

    await player._connect(retry_on_fail=retry_on_fail)

    mass.call_later.assert_called_once()
    args, _ = mass.call_later.call_args
    assert args[0] == 3600
    assert args[1] == player._connect
    assert args[2] == 3600


@pytest.mark.asyncio
async def test_cancelled_connect_propagates_without_reconnecting() -> None:
    """Test cancellation during a connection attempt does not schedule a retry."""
    player, mass = _make_player()
    player.client.connect = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await player._connect(retry_on_fail=30)

    mass.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_connect_during_shutdown_does_not_contact_player() -> None:
    """Test shutdown prevents a pending connection attempt from contacting the speaker."""
    player, mass = _make_player()
    mass.closing = True
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionTimeoutError("Connection timeout to host https://x:1443")
    )

    await player._connect(retry_on_fail=30)

    player.client.connect.assert_not_awaited()
    mass.call_later.assert_not_called()


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
    upnp: Any = player.upnp
    upnp.unfollow.reset_mock()

    await player.on_detach()

    assert player.connected is False
    client.disconnect.assert_awaited_once()
    upnp.unfollow.assert_awaited_once()
    # let the aborted listener run its cleanup
    with pytest.raises(asyncio.CancelledError):
        await listener
    assert timer_mass._tracked_timers == {}


@pytest.mark.asyncio
async def test_on_unload_of_an_offline_speaker_ends_no_subscription() -> None:
    """Test an unload does not wait for an unsubscribe a disconnected speaker cannot answer."""
    player, _ = _make_player()

    await player.on_detach()

    upnp: Any = player.upnp
    upnp.unfollow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_at_a_new_address_moves_upnp_along(timer_mass: MusicAssistant) -> None:
    """Test a speaker that reconnects at a new address gets its UPnP requests there."""
    player, client = _bind_player(timer_mass)
    old_upnp: Any = player.upnp
    client.player_ip = "192.168.1.21"

    await _connect_player(player, client)
    await asyncio.sleep(0)

    assert player.upnp.location == "http://192.168.1.21:1400/xml/device_description.xml"
    old_upnp.unfollow.assert_awaited_once()
    await player.on_detach()


@pytest.mark.asyncio
async def test_reconnect_at_a_new_address_keeps_the_upnp_port(timer_mass: MusicAssistant) -> None:
    """Test a zone of an Amp Multi keeps its own UPnP port at a new address."""
    player, client = _bind_player(timer_mass)
    player._upnp_port = 1500
    client.player_ip = "192.168.1.21"

    await _connect_player(player, client)

    assert player.upnp.location == "http://192.168.1.21:1500/xml/device_description.xml"
    await player.on_detach()


@pytest.mark.asyncio
async def test_reachability_is_probed_on_the_api_port_of_the_client() -> None:
    """Test a zone of an Amp Multi is probed on its own API port."""
    player, _ = _make_player()
    player.client.port = 1445
    player._attr_device_info = DeviceInfo()
    player._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, "192.168.1.20")
    writer = MagicMock(wait_closed=AsyncMock())
    open_connection = AsyncMock(return_value=(MagicMock(), writer))

    with patch("music_assistant.providers.sonos.player.asyncio.open_connection", open_connection):
        assert await player._is_reachable()

    open_connection.assert_awaited_once_with("192.168.1.20", 1445)


@pytest.mark.asyncio
async def test_on_unload_cancels_a_pending_reconnect(timer_mass: MusicAssistant) -> None:
    """Test a reconnect that is still waiting to fire does not connect after the unload."""
    player, _ = _bind_player(timer_mass)
    connect_attempts: list[int] = []

    async def _connect(retry_on_fail: int = 0) -> None:
        connect_attempts.append(retry_on_fail)

    player._connect = _connect  # type: ignore[method-assign]
    player.reconnect(0)
    handle = timer_mass._tracked_timers[f"sonos_reconnect_{player.protocol_id}"]

    await player.on_detach()
    await asyncio.sleep(0.05)

    assert handle.cancelled()
    assert connect_attempts == []


@pytest.mark.asyncio
async def test_on_detach_cancels_a_scheduled_options_read(timer_mass: MusicAssistant) -> None:
    """Test an options read that is still waiting to fire does not run after the detach."""
    player, _ = _bind_player(timer_mass)
    player._schedule_options_read()
    handle = timer_mass._tracked_timers[f"sonos_options_{player.protocol_id}"]

    with patch.object(player, "_update_options", AsyncMock()) as update_options:
        await player.on_detach()
        await asyncio.sleep(0.05)

    assert handle.cancelled()
    update_options.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_unload_cancels_a_running_airplay_group_restore(
    timer_mass: MusicAssistant,
) -> None:
    """Test the AirPlay group restore of a player stops listening for its members on unload."""
    player, client = _bind_player(timer_mass)
    player._attr_name = "Sonos Player"
    player.connected = True
    client.player.is_coordinator = True
    client.player.group_members = [player.protocol_id, "sonos_player_2"]
    client.api.groups.get_groups = AsyncMock()
    timer_mass.players = MagicMock()
    timer_mass.players.get_protocol.return_value = None
    # an AirPlay session that already renders: the members stay in the group
    airplay_player = MagicMock(playback_state=PlaybackState.PLAYING)
    player._airplay_output = lambda: airplay_player  # type: ignore[method-assign]

    async with player.prepare_play_media():
        pass
    task = timer_mass._tracked_tasks[f"restore_airplay_group_{player.protocol_id}"]

    await player.on_detach()

    with pytest.raises(asyncio.CancelledError):
        await task
    client.subscribe.return_value.assert_called_once_with()
    assert timer_mass._tracked_tasks == {}
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

    task_id = f"restore_airplay_group_{player.protocol_id}"
    timer_mass.call_later(0, _restore_airplay_group, task_id=task_id)
    await restoring.wait()
    task = timer_mass._tracked_tasks[task_id]

    await player.on_detach()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_on_unload_cancels_every_pending_hand_over_of_a_moved_group(
    timer_mass: MusicAssistant,
) -> None:
    """Test each hand-over waiting on a group's new coordinator is aborted on unload."""
    player, _ = _bind_player(timer_mass)
    waiting = [asyncio.Event(), asyncio.Event()]

    async def _hand_over(started: asyncio.Event) -> None:
        started.set()
        await asyncio.sleep(5)

    tasks = [
        timer_mass.create_task(_hand_over(started), task_id=f"hand_over_{index}")
        for index, started in enumerate(waiting)
    ]
    player._hand_over_tasks.update(tasks)
    for started in waiting:
        await started.wait()

    await player.on_detach()

    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (OutputProtocol(output_protocol_id="ap", name="AirPlay", protocol_domain="airplay"), "ap"),
        (
            OutputProtocol(
                output_protocol_id="bridge",
                name="Sendspin",
                protocol_domain="sendspin",
                derived_from="ap",
            ),
            "ap",
        ),
        (OutputProtocol(output_protocol_id="dlna", name="DLNA", protocol_domain="dlna"), None),
    ],
)
async def test_airplay_output_is_the_airplay_protocol_the_active_output_plays_over(
    timer_mass: MusicAssistant, output: OutputProtocol, expected: str | None
) -> None:
    """Test AirPlay is found as the active output and as the base of a derived transport."""
    player, _ = _bind_player(timer_mass)
    airplay_player = MagicMock()
    airplay_player.provider.domain = "airplay"
    player._player = MagicMock(active_output_protocol=output.output_protocol_id)
    player._player.get_linked_protocol.return_value = output
    timer_mass.players = MagicMock()
    timer_mass.players.get_protocol.side_effect = lambda pid: (
        airplay_player if pid == "ap" else None
    )

    assert player._airplay_output() is (airplay_player if expected else None)


@pytest.mark.asyncio
async def test_on_detach_unsubscribes_before_disconnecting(timer_mass: MusicAssistant) -> None:
    """Test the registered detach callbacks run before the client is disconnected."""
    player, client = _bind_player(timer_mass)
    calls: list[str] = []
    player._on_detach_callbacks.append(lambda: calls.append("unsubscribe"))
    client.disconnect = AsyncMock(side_effect=lambda: calls.append("disconnect"))

    await player.on_detach()

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
    await player.on_detach()

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

    await player.on_detach()

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
    _attach_to_player(player)
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
    player._provider = MagicMock(
        instance_id="sonos", get_protocol=lambda pid: player.mass.players.get_protocol(pid)
    )
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


def test_the_player_reports_its_announcement_features() -> None:
    """Test the clips honour the requested level and are fired together across members."""
    player, _ = _make_player()

    assert player.announcement_features == {
        AnnouncementFeature.SUPPORTS_VOLUME,
        AnnouncementFeature.COORDINATES_START,
    }


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
    mass.players.get_protocol.return_value = group_parent

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source.can_shuffle is True
    assert source.shuffle_enabled is True


def test_an_item_refused_by_our_stream_server_is_not_logged_as_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a 404 from our own stream server, which happens in bursts, only logs at debug."""
    player = _make_named_player("Kantoor")
    event = _playback_error(httpStatus=404, serviceName="192.168.1.10:9097")

    with caplog.at_level(logging.DEBUG, logger="test.sonos.player"):
        player._on_playback_error(event)

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert "was refused abc@3 by the stream server" in caplog.text


def test_a_404_from_another_service_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a 404 from a service other than our stream server is a real failure."""
    player = _make_named_player("Kantoor")
    event = _playback_error(httpStatus=404, serviceName="radio.example.com:80")

    with caplog.at_level(logging.DEBUG, logger="test.sonos.player"):
        player._on_playback_error(event)

    assert len([record for record in caplog.records if record.levelno >= logging.WARNING]) == 1
    assert "could not play Long Run 11 and reported ERROR_PLAYBACK_FAILED" in caplog.text


def test_a_playback_error_from_the_speaker_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failure that is not ours names the track and the error the speaker reported."""
    player = _make_named_player("Kantoor")
    event = _playback_error(reason="ERROR_DISALLOWED_BY_POLICY")

    with caplog.at_level(logging.DEBUG, logger="test.sonos.player"):
        player._on_playback_error(event)

    assert len([record for record in caplog.records if record.levelno >= logging.WARNING]) == 1
    assert (
        "could not play Long Run 11 and reported ERROR_PLAYBACK_FAILED (ERROR_DISALLOWED_BY_POLICY)"
        in caplog.text
    )


def test_a_group_member_leaves_reporting_playback_errors_to_the_coordinator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a speaker synced to another one stays silent, so a group failure is logged once."""
    player = _make_named_player("Kantoor")
    client = cast("MagicMock", player.client)
    client.player.is_coordinator = False
    client.player.group.coordinator_id = "coordinator"
    event = _playback_error(reason="ERROR_DISALLOWED_BY_POLICY")

    with caplog.at_level(logging.DEBUG, logger="test.sonos.player"):
        player._on_playback_error(event)

    assert not caplog.records


@pytest.mark.parametrize("item_id", ["abc@3", "abc"], ids=["current load", "legacy bare id"])
async def test_a_failed_track_hands_its_source_to_the_track_the_speaker_moves_on_to(
    item_id: str,
) -> None:
    """Test the queue is asked to release the failed track's source, by its bare item id."""
    player = _make_named_player("Kantoor")
    player.cloud_queue_id = "queue1"
    player.cloud_queue_item_generation = 3
    mass = cast("MagicMock", player.mass)
    mass.player_queues.release_failed_item_source = AsyncMock()
    scheduled = _scheduled_tasks(mass)

    player._on_playback_error(_playback_error(reason="ERROR_BUFFERING", itemId=item_id))

    assert len(scheduled) == 1
    await scheduled[0]
    mass.player_queues.release_failed_item_source.assert_awaited_once_with("queue1", "abc")


async def test_a_failed_track_releases_the_track_at_its_position() -> None:
    """Test the source released is that of the track the queue holds at the failed slot."""
    player = _make_named_player("Kantoor")
    player.cloud_queue_id = "queue1"
    player.cloud_queue_item_generation = 3
    player._window_item_ids = {4: "abc@3"}
    player.on_queue_position_served(4, "guest")
    mass = cast("MagicMock", player.mass)
    mass.player_queues.release_failed_item_source = AsyncMock()
    scheduled = _scheduled_tasks(mass)

    player._on_playback_error(_playback_error(reason="ERROR_BUFFERING", itemId="abc@3"))

    assert len(scheduled) == 1
    await scheduled[0]
    mass.player_queues.release_failed_item_source.assert_awaited_once_with("queue1", "guest")


def _report_playing_our_queue(group: MagicMock, wire_item_id: str) -> None:
    """Let the given group report it plays our cloud queue, on the given item."""
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PLAYING
    group.position = 12.0
    group.container_type = None
    group.active_service = MusicService.MUSIC_ASSISTANT
    group.playback_metadata = {
        "container": {
            "name": "Music Assistant",
            "id": {"objectId": "mass:queue1"},
            "service": {"name": "Music Assistant", "id": "mass"},
        },
        "currentItem": {"id": wire_item_id, "track": {"name": "Shout"}},
    }
    group.playback_actions.raw_data = {}
    group.play_modes.shuffle = False
    group.play_modes.repeat = False
    group.play_modes.repeat_one = False


def test_the_playing_item_follows_the_audio_served_for_the_reported_position(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a stale cached id the speaker reports reads as the track served for its slot, logged once."""
    player, _mass, client = _connected_player()
    player._cache = {}
    player._attr_name = "Kantoor"
    player._config = MagicMock()
    player._config.name = None
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    player.cloud_queue_id = "queue1"
    player.cloud_queue_item_generation = 3
    player._window_item_ids = {4: "abc@3"}
    player.on_queue_position_served(4, "guest")
    _report_playing_our_queue(client.player.group, "abc@3")

    with caplog.at_level(logging.DEBUG, logger="test.sonos.player"):
        player.on_player_event(None)
        player.on_player_event(None)

    assert player.current_media is not None
    assert player.current_media.queue_item_id == "guest"
    stale_reports = "Position 4 of queue queue1 now holds guest, speaker Kantoor expected abc"
    assert caplog.text.count(stale_reports) == 1


def _real_queues(queue_id: str, item_ids: list[str]) -> PlayerQueuesController:
    """Return the real queue controller holding a playing queue of the given items."""
    queue = PlayerQueue(queue_id=queue_id, active=True, display_name="Den", available=True, items=0)
    queue.state = PlaybackState.PLAYING
    data = PlayerQueueData(queue=queue)
    data.items = [
        QueueItem(queue_id=queue_id, queue_item_id=x, name=x, duration=180) for x in item_ids
    ]
    queue.items = len(data.items)
    queues = PlayerQueuesController.__new__(PlayerQueuesController)
    queues.mass = MagicMock()
    queues._queue_data = {queue_id: data}
    return queues


async def test_a_jump_is_not_undone_by_the_report_on_the_replaced_load() -> None:
    """
    Test the queue stays on the item a jump loads until the speaker reports on the new load.

    The speaker answers the load before its events of the new item are applied, so the queue
    reads what it reported on the replaced load first, and then the old track playing out.
    """
    player, mass, client = _connected_player()
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    client.player.is_passive = False
    group = client.player.group
    group.play_cloud_queue = AsyncMock()
    group.active_session_id = "session"
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/single/track2.wav")
    player._announcement_media = None
    player.cloud_queue_id = "queue1"
    queues = _real_queues("queue1", ["track0", "track1", "track2"])
    mass.player_queues = queues
    queue = queues._queue_data["queue1"].queue
    reporting = MagicMock(corrected_elapsed_time=0.0, fake_paused=False)
    reporting.resolve_output_protocol.return_value = player
    _report_playing_our_queue(group, cast("str", player.wire_item_id("track0")))
    player.on_player_event(None)
    queues._update_current_index_from_player(queue, reporting)
    assert queue.current_index == 0
    # the jump moves the queue before it loads the item on the speaker
    queue.current_index = 2

    with patch.object(SonosPlayer, "flow_mode", new=property(lambda _self: False)):
        await player.play_media(
            PlayerMedia(
                uri="library://track/3",
                media_type=MediaType.TRACK,
                source_id="queue1",
                queue_item_id="track2",
                queue_session_id="session-2",
            )
        )
    queues._update_current_index_from_player(queue, reporting)
    assert queue.current_index == 2
    player.on_player_event(None)
    queues._update_current_index_from_player(queue, reporting)
    assert queue.current_index == 2

    _report_playing_our_queue(group, cast("str", player.wire_item_id("track2")))
    player.on_player_event(None)
    assert queues._parse_player_current_item_id("queue1", reporting) == "track2"


@pytest.mark.parametrize("item_id", ["abc@7", "abc"], ids=["coordinator load", "bare id"])
def test_a_group_member_names_the_item_of_its_coordinators_load(item_id: str) -> None:
    """Test a member reads the reported item against the load of its coordinator."""
    player, mass, client = _connected_player()
    client.player.is_coordinator = False
    client.player.group.coordinator_id = "sonos_leader"
    player.cloud_queue_item_generation = 2
    leader, _, leader_client = _connected_player()
    leader.cloud_queue_id = "queue1"
    leader.cloud_queue_item_generation = 7
    _report_playing_our_queue(leader_client.player.group, item_id)
    mass.players.get_protocol.return_value = leader

    player.on_player_event(None)

    assert player.current_media is not None
    assert player.current_media.queue_item_id == "abc"


def test_a_failure_of_an_earlier_load_of_a_track_leaves_its_fresh_source_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Test a delayed report about the load a reload replaced is ignored.

    A seek or replay serves the same track under a new generation, and the source the queue
    item holds then belongs to that load; the old load's failure must not cancel it.
    """
    player = _make_named_player("Kantoor")
    player.cloud_queue_id = "queue1"
    player.cloud_queue_item_generation = 4

    with caplog.at_level(logging.DEBUG, logger="test.sonos.player"):
        player._on_playback_error(_playback_error(reason="ERROR_BUFFERING", itemId="abc@3"))

    cast("MagicMock", player.mass).create_task.assert_not_called()
    assert "Ignoring the failure of abc@3" in caplog.text


def test_a_track_refused_by_our_stream_server_has_no_source_to_release() -> None:
    """Test our own 404 never started a source for the track, so there is nothing to free."""
    player = _make_named_player("Kantoor")
    player.cloud_queue_id = "queue1"

    player._on_playback_error(_playback_error(httpStatus=404, serviceName="192.168.1.10:9097"))

    cast("MagicMock", player.mass).create_task.assert_not_called()


def test_a_group_member_leaves_releasing_a_failed_track_to_the_coordinator() -> None:
    """Test a speaker synced to another one does not act on the group's failure."""
    player = _make_named_player("Kantoor")
    player.cloud_queue_id = "queue1"
    client = cast("MagicMock", player.client)
    client.player.is_coordinator = False
    client.player.group.coordinator_id = "coordinator"

    player._on_playback_error(_playback_error(reason="ERROR_BUFFERING"))

    cast("MagicMock", player.mass).create_task.assert_not_called()


def test_a_failure_outside_a_cloud_queue_releases_nothing() -> None:
    """Test a failure while the speaker plays another source has no queue item behind it."""
    player = _make_named_player("Kantoor")

    player._on_playback_error(_playback_error(reason="ERROR_BUFFERING"))

    cast("MagicMock", player.mass).create_task.assert_not_called()


def _report_paused_qobuz(group: MagicMock) -> None:
    """Let the given group report a paused session of a service we did not map."""
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PAUSED
    group.position = 42.0
    group.container_type = "playlist"
    group.active_service = "31"
    group.playback_metadata = {
        "container": {"name": "Chill Mix", "service": {"name": "Qobuz"}},
        "currentItem": {"id": "1", "track": {"name": "Shout"}},
    }
    group.playback_actions = _actions(
        {
            "canShuffle": True,
            "canRepeat": True,
            "canRepeatOne": True,
            # paused, so the speaker offers play but not pause
            "canPlay": True,
            "canPause": False,
            "canSeek": True,
            "canSkip": True,
            "canSkipToPrevious": False,
        }
    )
    # a bare MagicMock attribute is truthy, so the play modes are spelled out
    group.play_modes.shuffle = True
    group.play_modes.repeat = True
    group.play_modes.repeat_one = False


def _speaker_reporting_paused_qobuz() -> tuple[SonosPlayer, MagicMock]:
    """Create a connected player whose speaker plays a service we did not map."""
    player, mass, client = _connected_player()
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    player._attr_source_list = [PLAYER_SOURCE_MAP[SOURCE_LINE_IN]]
    _report_paused_qobuz(client.player.group)
    return player, mass


def test_a_service_we_did_not_map_gets_a_source_entry_with_its_play_modes() -> None:
    """Test a service we did not map is reported with the play modes the speaker offers for it."""
    player, _ = _speaker_reporting_paused_qobuz()

    player.on_player_event(None)

    assert player._attr_active_source == "Qobuz"
    assert any(x.id == "Qobuz" for x in player.source_list)
    source = next(x for x in player._attr_source_list if x.id == "Qobuz")
    assert source.name == "Qobuz"
    assert source.passive is True
    assert source.can_shuffle is True
    assert source.can_repeat is True
    assert source.shuffle_enabled is True
    assert source.repeat_mode is RepeatMode.ALL
    # either play or pause being available means the source can be paused and resumed
    assert source.can_play_pause is True
    assert source.can_seek is True
    # one flag for both buttons: kept while either direction works
    assert source.can_next_previous is True
    # the entry only exists while the speaker plays it, there is no template for it
    assert "Qobuz" not in PLAYER_SOURCE_MAP


def test_the_entry_of_a_service_we_did_not_map_is_dropped_when_it_stops() -> None:
    """Test a service we did not map leaves the source list when it is no longer playing."""
    player, _ = _speaker_reporting_paused_qobuz()
    player.on_player_event(None)
    group = cast("MagicMock", player.client.player.group)
    group.active_service = MusicService.MUSIC_ASSISTANT
    group.container_type = None
    del group.playback_metadata["container"]["service"]

    player.on_player_event(None)

    assert not [x for x in player._attr_source_list if x.id == "Qobuz"]
    assert player._attr_active_source is None
    source = next(x for x in player._attr_source_list if x.id == SOURCE_LINE_IN)
    assert source is PLAYER_SOURCE_MAP[SOURCE_LINE_IN]


def test_a_service_we_did_not_map_is_replaced_when_another_source_takes_over() -> None:
    """Test only the source that plays now has an entry when another source takes over."""
    player, _ = _speaker_reporting_paused_qobuz()
    player.on_player_event(None)
    group = cast("MagicMock", player.client.player.group)
    _report_paused_spotify(group)

    player.on_player_event(None)

    assert not [x for x in player._attr_source_list if x.id == "Qobuz"]
    source = next(x for x in player._attr_source_list if x.id == SOURCE_SPOTIFY)
    assert source.can_shuffle is True
    assert [x for x in player._attr_source_list if x.id == SOURCE_LINE_IN]


def test_a_service_we_did_not_map_leaves_the_templates_untouched() -> None:
    """Test the templates shared between players keep their defaults."""
    player, _ = _speaker_reporting_paused_qobuz()
    templates = set(PLAYER_SOURCE_MAP)

    player.on_player_event(None)

    assert PLAYER_SOURCE_MAP[SOURCE_LINE_IN].shuffle_enabled is None
    assert PLAYER_SOURCE_MAP[SOURCE_SPOTIFY].can_shuffle is False
    assert set(PLAYER_SOURCE_MAP) == templates


def test_line_in_and_tv_are_the_inputs() -> None:
    """Line-in and TV are inputs of the speaker; AirPlay and the music services are not."""
    assert [x.id for x in PLAYER_SOURCE_MAP.values() if x.is_input] == [SOURCE_LINE_IN, SOURCE_TV]


def test_the_entry_of_a_service_we_did_not_map_follows_what_the_speaker_reports() -> None:
    """Test the entry of a service we did not map is rebuilt from the latest speaker state."""
    player, _ = _speaker_reporting_paused_qobuz()
    player.on_player_event(None)
    group = cast("MagicMock", player.client.player.group)
    group.playback_actions = _actions(
        {
            "canShuffle": False,
            "canRepeat": False,
            "canPause": False,
            "canSeek": False,
            "canSkip": True,
            "canSkipToPrevious": True,
        }
    )
    group.play_modes.shuffle = False

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == "Qobuz")
    assert source.can_shuffle is False
    assert source.shuffle_enabled is False
    assert source.can_play_pause is False
    assert source.can_next_previous is True
    assert len([x for x in player._attr_source_list if x.id == "Qobuz"]) == 1


@pytest.mark.parametrize(
    ("flags", "offered"),
    [
        ({"canSkip": True, "canSkipToPrevious": False}, True),
        ({"canSkip": False, "canSkipToPrevious": True}, True),
        ({"canSkip": False, "canSkipToPrevious": False}, False),
    ],
    ids=["first_track", "last_track", "neither"],
)
def test_skipping_is_offered_while_either_direction_works(
    flags: dict[str, bool], offered: bool
) -> None:
    """Test the speaker's first and last track keep the buttons; a source with neither has none."""
    player, _ = _speaker_reporting_paused_qobuz()
    group = cast("MagicMock", player.client.player.group)
    group.playback_actions = _actions(flags)

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == "Qobuz")
    assert source.can_next_previous is offered


@pytest.mark.parametrize("available", [True, False], ids=["offered", "not_offered"])
@pytest.mark.parametrize(
    ("command", "flag", "speaker_call"),
    [
        ("next_track", "canSkip", "skip_to_next_track"),
        ("previous_track", "canSkipToPrevious", "skip_to_previous_track"),
    ],
    ids=["next", "previous"],
)
async def test_a_skip_is_sent_only_in_a_direction_the_speaker_offers(
    command: str, flag: str, speaker_call: str, available: bool
) -> None:
    """Test a skip the speaker does not offer is not sent: it refuses it (DISALLOWED_BY_POLICY)."""
    player, _ = _speaker_reporting_paused_qobuz()
    # the debug line names the player
    player._cache = {}
    player._attr_name = "Office"
    player._config = MagicMock()
    player._config.name = None
    group = cast("MagicMock", player.client.player.group)
    group.playback_actions = _actions({flag: available})
    setattr(group, speaker_call, AsyncMock())

    await getattr(player, command)()

    assert getattr(group, speaker_call).await_count == (1 if available else 0)


def test_skipping_back_follows_the_deprecated_key_on_older_firmware() -> None:
    """Test a speaker that only reports canSkipBack (before API 1.36.0) still offers skipping."""
    player, _ = _speaker_reporting_paused_qobuz()
    group = cast("MagicMock", player.client.player.group)
    # only the old key is there to offer skipping at all
    group.playback_actions = _actions({"canSkip": False, "canSkipBack": True})

    player.on_player_event(None)

    source = next(x for x in player._attr_source_list if x.id == "Qobuz")
    assert source.can_next_previous is True


def test_a_group_child_does_not_take_over_the_line_in_of_its_coordinator() -> None:
    """Test a source the coordinator offers but this player lacks never enters its source list."""
    player, mass, client = _connected_player()
    client.player.is_coordinator = False
    client.player.group.coordinator_id = "sonos_leader"
    group_parent = MagicMock()
    group_parent.client.player.group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PLAYING
    group_parent.client.player.group.position = 0.0
    group_parent.client.player.group.container_type = ContainerType.LINEIN
    group_parent.client.player.group.playback_metadata = {
        "container": {"type": ContainerType.LINEIN, "lineInSource": "sonos_leader"}
    }
    mass.players.get_protocol.return_value = group_parent

    # twice: a transient entry would only be promoted to its template on the next update
    player.on_player_event(None)
    player.on_player_event(None)

    # the member reports the line-in its coordinator plays
    assert player._attr_active_source == "line_in@sonos_leader"
    assert player._attr_source_list == []


def test_a_service_we_did_not_map_leaves_the_source_list_when_it_is_given_up_on() -> None:
    """Test the entry goes when a paused session is declared over, before the speaker reports."""
    player, _ = _speaker_reporting_paused_qobuz()
    player.on_player_event(None)

    _attach_to_player(player).mark_external_source_ended()

    assert player._attr_active_source is None
    assert player.source_list == [PLAYER_SOURCE_MAP[SOURCE_LINE_IN]]


def _report_airplay(
    player: SonosPlayer, client: MagicMock, state: SonosPlayBackState, *, our_output: bool
) -> None:
    """Let the speaker report an AirPlay session, with or without our AirPlay output on it."""
    group = client.player.group
    group.playback_state = state
    group.position = 0.0
    group.container_type = ContainerType.AIRPLAY
    group.active_service = None
    group.playback_metadata = {
        "container": {"name": "AirPlay", "type": "linein.airplay", "service": {"name": "Airplay"}},
        "currentItem": {
            "track": {
                "name": "Shout",
                "images": [{"url": "http://192.168.1.20:1400/getaa?u=1"}],
            }
        },
    }
    group.playback_actions = _actions({})
    player._airplay_output = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock() if our_output else None
    )
    player.on_player_event(None)


def _speaker_on_airplay() -> tuple[SonosPlayer, MagicMock]:
    """Create a connected player that coordinates a group of its own."""
    player, _, client = _connected_player()
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    return player, client


def test_our_stopped_airplay_session_is_reported_idle_without_a_source() -> None:
    """Test the paused AirPlay session the speaker keeps after our stream stopped is not a source."""
    player, client = _speaker_on_airplay()
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=True)

    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PAUSED, our_output=False)

    assert player._attr_playback_state is PlaybackState.IDLE
    assert player._attr_active_source is None


def test_an_airplay_sender_of_its_own_is_reported_as_the_airplay_source() -> None:
    """Test a phone that starts AirPlay on the speaker shows as source AirPlay."""
    player, client = _speaker_on_airplay()
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=True)
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PAUSED, our_output=False)

    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=False)

    assert (player._attr_playback_state, player._attr_active_source) == (
        PlaybackState.PLAYING,
        SOURCE_AIRPLAY,
    )

    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PAUSED, our_output=False)

    assert (player._attr_playback_state, player._attr_active_source) == (
        PlaybackState.PAUSED,
        SOURCE_AIRPLAY,
    )


def test_our_airplay_session_reports_no_artwork() -> None:
    """Test the artwork the speaker names for our AirPlay session is not reported."""
    player, client = _speaker_on_airplay()
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=True)

    # our stream ends before the speaker reports its pause
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=False)

    assert player._attr_current_media is not None
    assert player._attr_current_media.title == "Shout"
    assert player._attr_current_media.image_url is None


def test_an_airplay_sender_of_its_own_reports_its_artwork() -> None:
    """Test the artwork of a phone that plays over AirPlay is reported."""
    player, client = _speaker_on_airplay()

    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=False)

    assert player._attr_current_media is not None
    assert player._attr_current_media.image_url == "http://192.168.1.20:1400/getaa?u=1"


def test_a_sender_that_takes_over_while_we_stream_keeps_the_session_ours() -> None:
    """Test a phone that takes the speaker over mid-stream is not told apart from our stream."""
    player, client = _speaker_on_airplay()
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=True)
    # the speaker keeps playing through the takeover and our output stays the active one
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=True)

    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PAUSED, our_output=True)

    assert player._attr_playback_state is PlaybackState.IDLE
    assert player._attr_active_source is None


def test_other_speaker_events_keep_our_airplay_session() -> None:
    """Test an event that leaves the playback alone, such as a volume change, keeps our session."""
    player, client = _speaker_on_airplay()
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=True)
    # our stream ends before the speaker reports its pause
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PLAYING, our_output=False)
    _report_airplay(player, client, SonosPlayBackState.PLAYBACK_STATE_PAUSED, our_output=False)

    client.player.volume_level = 12
    player.on_player_event(None)

    assert player._attr_playback_state is PlaybackState.IDLE
    assert player._attr_active_source is None


def _wakeable_player(*, portable: bool = True) -> tuple[SonosPlayer, MagicMock]:
    """
    Create a bound player that advertises Wake-on-LAN support.

    :param portable: Whether it is a portable, as opposed to a speaker on mains power.
    """
    player, mass = _make_player()
    player._sleeps = portable
    player._wol_mac = "C4:38:75:0D:18:9C"
    player._cache = {}
    player._attr_name = "Portable"
    player._config = MagicMock()
    player._config.name = None
    player._provider = MagicMock(
        instance_id="sonos", get_protocol=lambda pid: player.mass.players.get_protocol(pid)
    )
    return player, mass


@pytest.mark.asyncio
@pytest.mark.parametrize(("powered", "connected"), [(False, True), (True, True), (False, False)])
async def test_power_without_a_wake_only_updates_local_state(
    powered: bool, connected: bool
) -> None:
    """Test power off, and power on while connected, send no packet."""
    player, _ = _wakeable_player()
    player.connected = connected

    with patch(
        "music_assistant.providers.sonos.player.send_magic_packet", new_callable=AsyncMock
    ) as wol:
        await player.power(powered)

    wol.assert_not_awaited()
    assert player._attr_powered is powered
    player.update_state.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_power_on_wakes_a_sleeping_speaker() -> None:
    """Test power on sends the magic packet, waits for the radio and connects."""
    player, _ = _wakeable_player()
    player._is_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _connect(_retry_on_fail: int = 0) -> None:
        player.connected = True

    player._connect = AsyncMock(side_effect=_connect)  # type: ignore[method-assign]
    with patch(
        "music_assistant.providers.sonos.player.send_magic_packet", new_callable=AsyncMock
    ) as wol:
        await player.power(True)

    wol.assert_awaited_once_with("C4:38:75:0D:18:9C")
    player._connect.assert_awaited_once()
    assert player._attr_powered is True
    assert player._woken_from_sleep is True


@pytest.mark.asyncio
async def test_power_on_gives_up_when_the_radio_never_answers() -> None:
    """Test a speaker that never comes back is reported unavailable without a connect attempt."""
    player, _ = _wakeable_player()
    player._is_reachable = AsyncMock(return_value=False)  # type: ignore[method-assign]
    player._connect = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("music_assistant.providers.sonos.player.send_magic_packet", new_callable=AsyncMock),
        patch("music_assistant.providers.sonos.player.WAKE_TIMEOUT", 0.05),
        pytest.raises(PlayerUnavailableError),
    ):
        await player.power(True)

    player._connect.assert_not_awaited()
    assert player._attr_powered is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("power_control", [PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_NONE])
async def test_a_goodbye_reads_as_sleep_only_when_we_control_power(power_control: str) -> None:
    """Test a withdrawn announcement puts a natively controlled portable to sleep."""
    player, _ = _wakeable_player()
    player.connected = True
    player._attr_powered = True
    player._attr_playback_state = PlaybackState.PAUSED
    player._disconnect = AsyncMock()  # type: ignore[method-assign]

    with patch.object(player, "_player", MagicMock(spec=Player, power_control=power_control)):
        await player.on_mdns_goodbye()

    if power_control == PLAYER_CONTROL_NATIVE:
        player._disconnect.assert_awaited_once()
        assert player._attr_powered is False
        assert player._attr_available is True
        assert player._attr_playback_state == PlaybackState.IDLE
        assert player._marked_asleep is True
    else:
        player._disconnect.assert_not_awaited()
        assert player._attr_powered is True


@pytest.mark.asyncio
async def test_a_goodbye_leaves_a_speaker_on_mains_power_connected() -> None:
    """Test a speaker that does not sleep is left as it is when its announcement is gone."""
    player, _ = _wakeable_player(portable=False)
    player.connected = True
    player._attr_powered = True
    player._disconnect = AsyncMock()  # type: ignore[method-assign]

    with patch.object(
        player, "_player", MagicMock(spec=Player, power_control=PLAYER_CONTROL_NATIVE)
    ):
        await player.on_mdns_goodbye()

    player._disconnect.assert_not_awaited()
    assert player._attr_powered is True
    assert player._marked_asleep is False


@pytest.mark.asyncio
@pytest.mark.parametrize("marked_asleep", [True, False])
async def test_reconnecting_only_powers_on_after_a_confirmed_sleep(
    marked_asleep: bool, timer_mass: MusicAssistant
) -> None:
    """Test a reconnect wakes a sleeping player but leaves a local power off alone."""
    player, client = _bind_player(timer_mass)
    player._sleeps = True
    player._attr_powered = False
    player._marked_asleep = marked_asleep

    with patch.object(
        player, "_player", MagicMock(spec=Player, power_control=PLAYER_CONTROL_NATIVE)
    ):
        await _connect_player(player, client)

    assert player._attr_powered is marked_asleep
    assert player._woken_from_sleep is marked_asleep
    assert player._marked_asleep is False
    assert player._listen_task is not None
    player._listen_task.cancel()


@pytest.mark.asyncio
async def test_a_failed_connect_puts_a_portable_to_sleep_without_retrying() -> None:
    """Test a portable that stops answering reads as asleep instead of being retried."""
    player, mass = _wakeable_player()
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=CannotConnect(OSError("no route to host"))
    )

    with patch.object(
        player, "_player", MagicMock(spec=Player, power_control=PLAYER_CONTROL_NATIVE)
    ):
        await player._connect(retry_on_fail=5)

    assert player._attr_powered is False
    assert player._attr_available is True
    assert player._marked_asleep is True
    mass.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_connect_to_a_speaker_on_mains_power_is_retried() -> None:
    """Test a speaker that does not sleep reads as unavailable when it stops answering."""
    player, mass = _wakeable_player(portable=False)
    player._attr_powered = True
    player.client.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=CannotConnect(OSError("no route to host"))
    )

    with patch.object(
        player, "_player", MagicMock(spec=Player, power_control=PLAYER_CONTROL_NATIVE)
    ):
        await player._connect(retry_on_fail=5)

    assert player._attr_available is False
    assert player._attr_powered is True
    assert player._marked_asleep is False
    mass.call_later.assert_called_once()


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ([{"name": "WAKEABLE"}, {"name": "BLE"}], True),
        ([{"name": "BLE"}], False),
        (None, False),
        (["WAKEABLE"], False),
    ],
)
def test_the_advertised_device_features_are_read_defensively(
    features: list[Any] | None, expected: bool
) -> None:
    """Test a device feature is found in the discovery info, whatever that holds."""
    device: dict[str, Any] = {} if features is None else {"deviceFeatures": features}
    assert _has_device_feature(cast("Any", {"device": device}), "WAKEABLE") is expected


@pytest.mark.asyncio
async def test_set_members_returns_once_the_sonos_group_reports_the_new_member(
    timer_mass: MusicAssistant,
) -> None:
    """Test set_members waits for the Sonos group event before returning."""
    player, client = _bind_player(timer_mass)
    timer_mass.players = MagicMock(get_protocol=MagicMock(return_value=None))
    client.player.group_members = [player.protocol_id]

    def _apply_new_member(**_kwargs: Any) -> None:
        asyncio.get_running_loop().call_later(
            0.3,
            lambda: setattr(client.player, "group_members", [player.protocol_id, "sonos_player_2"]),
        )

    client.player.group.modify_group_members = AsyncMock(side_effect=_apply_new_member)

    await player.set_members(player_ids_to_add=["sonos_player_2"])

    assert "sonos_player_2" in client.player.group_members


@pytest.mark.asyncio
async def test_set_members_gives_up_when_the_sonos_group_never_reports_the_member(
    timer_mass: MusicAssistant,
) -> None:
    """Test set_members returns without raising if the group never reports the new member."""
    player, client = _bind_player(timer_mass)
    timer_mass.players = MagicMock(get_protocol=MagicMock(return_value=None))
    client.player.group_members = [player.protocol_id]
    client.player.group.modify_group_members = AsyncMock()

    with patch(
        "music_assistant.providers.sonos.player.asyncio.timeout",
        return_value=asyncio.timeout(0.2),
    ):
        await player.set_members(player_ids_to_add=["sonos_player_2"])

    assert client.player.group_members == [player.protocol_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("coordinator_state", ["connected", "disconnected", "unknown"])
async def test_ungroup_leaves_a_group_whose_coordinator_is_offline_by_creating_its_own(
    coordinator_state: str,
) -> None:
    """A member can only leave through a reachable coordinator, else it forms its own group."""
    mass = MagicMock()
    player, client = _bind_player(mass)
    client.player.is_coordinator = False
    client.player.group.coordinator_id = "coordinator"
    client.player.leave_group = AsyncMock()
    client.create_group = AsyncMock()
    coordinator, _ = _bind_player(mass)
    coordinator._attr_available = coordinator.connected = coordinator_state == "connected"
    mass.players.get_protocol.return_value = None if coordinator_state == "unknown" else coordinator

    await player.ungroup()

    if coordinator_state == "connected":
        client.player.leave_group.assert_awaited_once()
        client.create_group.assert_not_awaited()
    else:
        client.create_group.assert_awaited_once_with(["sonos_player"])
        client.player.leave_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("situation", ["coordinator", "no_group"])
async def test_ungroup_without_a_coordinator_to_reach_leaves_the_group(situation: str) -> None:
    """A coordinator, or a speaker without a group, simply leaves its group."""
    mass = MagicMock()
    player, client = _bind_player(mass)
    client.player.is_coordinator = situation == "coordinator"
    if situation == "no_group":
        client.player.group = None
    client.player.leave_group = AsyncMock()
    client.create_group = AsyncMock()

    await player.ungroup()

    client.player.leave_group.assert_awaited_once()
    client.create_group.assert_not_awaited()
    mass.players.get_protocol.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_playing", [True, False])
async def test_pause_stops_a_speaker_playing_the_queue_of_its_player(queue_playing: bool) -> None:
    """Test a speaker playing a Music Assistant queue is stopped instead of paused."""
    player, mass, client = _connected_player()
    client.player.is_passive = False
    group = client.player.group
    group.stop = AsyncMock()
    group.pause = AsyncMock()
    group.playback_actions.can_pause = True
    player._player = MagicMock(spec=Player)
    player._player.active_source = "pl0123456789ab"
    mass.player_queues.get.return_value = MagicMock() if queue_playing else None

    await player.pause()

    mass.player_queues.get.assert_called_once_with("pl0123456789ab")
    if queue_playing:
        group.stop.assert_awaited_once()
        group.pause.assert_not_awaited()
    else:
        group.pause.assert_awaited_once()


@pytest.mark.parametrize(
    ("queue_playing", "served_as_file", "can_pause", "passive", "stops"),
    [
        pytest.param(True, False, True, False, True, id="queue item not served as a file"),
        pytest.param(True, True, True, False, False, id="queue item served as a file"),
        pytest.param(False, False, True, False, False, id="another source"),
        pytest.param(False, False, False, False, True, id="no pause offered"),
        pytest.param(True, False, True, True, False, id="synced to another speaker"),
    ],
)
def test_a_speaker_stops_on_a_pause_it_could_not_resume(
    queue_playing: bool, served_as_file: bool, can_pause: bool, passive: bool, stops: bool
) -> None:
    """
    Test a speaker stops on a pause in a queue item it does not get as a file, or without a pause.

    :param queue_playing: Whether the speaker plays a Music Assistant queue.
    :param served_as_file: Whether the item it plays is served to it as a file.
    :param can_pause: Whether the speaker offers a pause of what it plays.
    :param passive: Whether the speaker follows another one, which handles the pause.
    :param stops: Whether a pause stops the speaker.
    """
    player, mass, client = _connected_player()
    client.player.is_passive = passive
    client.player.group.playback_actions.can_pause = can_pause
    player._player = MagicMock(spec=Player)
    player._player.active_source = "pl0123456789ab"
    mass.player_queues.get.return_value = MagicMock() if queue_playing else None

    with patch.object(SonosPlayer, "_plays_file", return_value=served_as_file):
        assert player.stops_on_pause is stops


@pytest.mark.asyncio
@pytest.mark.parametrize("served_as_file", [True, False])
async def test_pause_pauses_a_speaker_only_in_a_track_it_gets_as_a_file(
    served_as_file: bool,
) -> None:
    """Test a speaker playing a queue is paused in a file, and stopped in any other stream."""
    player, mass, client = _connected_player()
    client.player.is_passive = False
    group = client.player.group
    group.stop = AsyncMock()
    group.pause = AsyncMock()
    group.playback_actions.can_pause = True
    group.playback_metadata = {"currentItem": {"id": "abc@0", "track": {"name": "Shout"}}}
    player._player = MagicMock(spec=Player)
    player._player.active_source = "pl0123456789ab"
    player.cloud_queue_id = "pl0123456789ab"
    # the speaker reports the item it cached for the position, and plays what it fetched there
    player._served_bindings = {"abc@0": (0, "xyz")}
    player._config = MagicMock()
    player._config.get_value.return_value = "lossless_uncompressed"
    queue_item = mass.player_queues.get_item.return_value
    mass.streams.serves_as_file.return_value = served_as_file

    await player.pause()

    mass.player_queues.get_item.assert_called_once_with("pl0123456789ab", "xyz")
    mass.streams.serves_as_file.assert_called_once_with(player, queue_item, ContentType.WAV)
    if served_as_file:
        group.pause.assert_awaited_once()
        group.stop.assert_not_awaited()
    else:
        group.stop.assert_awaited_once()
        group.pause.assert_not_awaited()


def _speaker_to_pause(
    status_item_id: str | None, metadata_item_id: str | None, file_item_ids: set[str]
) -> tuple[SonosPlayer, MagicMock]:
    """
    Return a speaker playing our cloud queue of load generation 7, and its group.

    :param status_item_id: The item the group's playback status names.
    :param metadata_item_id: The item the group's playback metadata names.
    :param file_item_ids: The queue items served to the speaker as a file.
    """
    player, mass, client = _connected_player()
    client.player.is_passive = False
    group = client.player.group
    group.stop = AsyncMock()
    group.pause = AsyncMock()
    group.playback_actions.can_pause = True
    group.playback_item_id = status_item_id
    group.playback_metadata = (
        {"currentItem": {"id": metadata_item_id, "track": {"name": "Shout"}}}
        if metadata_item_id
        else {}
    )
    player._player = MagicMock(spec=Player)
    player._player.active_source = "pl0123456789ab"
    player._player.stop_called = False
    player.cloud_queue_id = "pl0123456789ab"
    player.cloud_queue_item_generation = 7
    player._config = MagicMock()
    player._config.get_value.return_value = "lossless_uncompressed"
    mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: MagicMock(
        queue_item_id=item_id
    )
    mass.streams.serves_as_file.side_effect = lambda _player, queue_item, _codec: (
        queue_item.queue_item_id in file_item_ids
    )
    return player, group


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_item_id", "metadata_item_id", "file_item_ids", "pauses"),
    [
        pytest.param("abc@7", None, {"abc"}, True, id="right after a load"),
        pytest.param("def@7", "abc@7", {"def"}, True, id="right after a change of track"),
        pytest.param(None, "abc@7", {"abc"}, True, id="status names no item"),
        pytest.param("abc@7", None, set(), False, id="item not served as a file"),
        pytest.param("abc@3", None, {"abc"}, False, id="status item of another load"),
        pytest.param("abc", None, {"abc"}, False, id="status item without a generation"),
        pytest.param("abc@6", "abc@6", {"abc"}, False, id="load in flight"),
    ],
)
async def test_pause_follows_the_item_the_playback_status_names(
    status_item_id: str | None, metadata_item_id: str | None, file_item_ids: set[str], pauses: bool
) -> None:
    """
    Test a pause reads the item the speaker plays from its status, before the metadata names it.

    :param status_item_id: The item the group's playback status names.
    :param metadata_item_id: The item the group's playback metadata names.
    :param file_item_ids: The queue items served to the speaker as a file.
    :param pauses: Whether the speaker is paused rather than stopped.
    """
    player, group = _speaker_to_pause(status_item_id, metadata_item_id, file_item_ids)

    await player.pause()

    if pauses:
        group.pause.assert_awaited_once()
        group.stop.assert_not_awaited()
    else:
        group.stop.assert_awaited_once()
        group.pause.assert_not_awaited()


def test_a_pause_reported_right_after_a_change_of_track_reads_as_paused() -> None:
    """Test a pause in a new track served as a file is a pause while the metadata names the old one."""
    player, group = _speaker_to_pause("def@7", "abc@7", {"def"})
    client = cast("MagicMock", player.client)
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    _report_playing_our_queue(group, "abc@7")
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PAUSED

    player.update_attributes()

    assert player._attr_playback_state is PlaybackState.PAUSED


def test_a_speaker_paused_in_a_flow_stream_reads_idle_until_it_is_loaded_anew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test a speaker that plays again by itself after a pause waits, idle, for the queue's resume."""
    player, group = _speaker_to_pause("flow@7", "flow@7", set())
    client = cast("MagicMock", player.client)
    client.player.is_coordinator = True
    client.player.group_members = ["sonos_player"]
    player._flow_media = PlayerMedia(
        uri="http://ma/flow/session/queue/track0/sonos_player.wav",
        queue_item_id="flow",
        queue_session_id="session",
    )
    player._flow_paused = player._flow_resume_asked = False
    resume = MagicMock()
    monkeypatch.setattr(player, "_resume_paused_flow", resume)
    _report_playing_our_queue(group, "flow@7")
    reported: list[PlaybackState] = []

    for state in ("PLAYING", "PAUSED", "BUFFERING", "PLAYING"):
        group.playback_state = SonosPlayBackState(f"PLAYBACK_STATE_{state}")
        player.update_attributes()
        reported.append(player._attr_playback_state)

    assert reported == [PlaybackState.PLAYING, *[PlaybackState.IDLE] * 3]
    resume.assert_called_once_with("pl0123456789ab", "session")


def test_a_speaker_fetches_files() -> None:
    """Test a speaker asks for a track as a file, where the stream server has one."""
    player, _ = _make_player()

    assert player.fetches_files is True


def test_a_speaker_shows_the_titles_in_a_stream() -> None:
    """Test a radio station a speaker plays as one stream carries its titles by default."""
    player, _ = _make_player()

    assert player.shows_stream_titles is True


def test_a_speaker_defaults_to_lossless_uncompressed() -> None:
    """Test a speaker the user picked no playback quality for is streamed lossless, uncompressed."""
    player, _ = _make_player()

    assert player.default_playback_quality is PlaybackQuality.LOSSLESS_UNCOMPRESSED


def test_a_speaker_needs_no_content_length() -> None:
    """Test a stream that is not a file reaches a speaker without a made-up length."""
    player, _ = _make_player()

    assert player.requires_content_length is False
