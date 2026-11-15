"""Tests for a Music Assistant session on a Sonos speaker that this server did not start."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.const import EventType as SonosEventType
from aiosonos.const import SessionErrorEvent
from aiosonos.group import SonosGroup
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import MediaType, PlaybackState, ProtocolRole
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players.controller import PlayerController
from music_assistant.models.player import Player
from music_assistant.providers.sonos.const import SOURCE_AIRPLAY, SOURCE_MUSIC_ASSISTANT
from music_assistant.providers.sonos.player import SonosPlayer
from tests.common import MockProvider, mock_player_mass

if TYPE_CHECKING:
    from aiosonos.api.models import (
        DiscoveryInfo,
        Group,
        MetadataStatus,
        PlaybackStatus,
        SessionError,
    )

# the speakers' own protocol ids, which their groups report
SPEAKER = "RINCON_F0F6C1CC8D0401400"
MEMBER = "RINCON_F0F6C1CC8D0501400"
# the players of the speakers, with ids of their own: the ids of their queues
SPEAKER_PLAYER = "pl_speaker"
MEMBER_PLAYER = "pl_member"
OUR_SESSION = "session-ours"


def _mass() -> MagicMock:
    """Return a mocked mass with the real players controller and a bare queues controller."""
    mass = mock_player_mass()
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.streams.get_announcement_duration = AsyncMock(return_value=3)
    mass.players = PlayerController(mass)
    queues = PlayerQueuesController.__new__(PlayerQueuesController)
    queues.mass = mass
    queues.logger = MagicMock()
    queues._queue_data = {}
    queues._save_queue_to_cache = AsyncMock()  # type: ignore[method-assign]
    mass.player_queues = queues
    return mass


def _protocol_config(name: str) -> MagicMock:
    """Return a mocked config of a protocol without a custom name or a power control."""
    config = MagicMock()
    config.name = None
    config.default_name = name
    config.get_value = MagicMock(return_value="none")
    return config


def _speaker(
    mass: MagicMock,
    protocol_id: str = SPEAKER,
    player_id: str = SPEAKER_PLAYER,
    leader: str | None = None,
) -> SonosPlayer:
    """
    Attach a connected speaker, with its queue, that coordinates its own group or joins one.

    :param mass: The mocked mass from _mass.
    :param protocol_id: The id of the speaker's protocol.
    :param player_id: The id of the speaker's player.
    :param leader: The protocol id of the speaker coordinating the group this one joins, if any.
    """
    provider = MockProvider("sonos", instance_id="sonos", mass=mass)
    mass.config.get_base_protocol_config.return_value = _protocol_config(protocol_id)
    discovery_info = cast("DiscoveryInfo", {"device": {}})
    player = SonosPlayer(cast("Any", provider), protocol_id, discovery_info)
    client = MagicMock()
    client.player_id = protocol_id
    coordinator = leader or protocol_id
    group_data = cast(
        "Group",
        {"id": f"{coordinator}:1", "name": "Group", "coordinatorId": coordinator, "playerIds": []},
    )
    # the library's own group: it knows the session this server created, and nothing else
    client.player.group = SonosGroup(client, group_data)
    client.player.is_coordinator = leader is None
    client.player.is_passive = False
    client.player.group_members = [protocol_id]
    client.player.has_fixed_volume = False
    client.player.volume_muted = False
    client.player.volume_level = 20
    client.api.playback_session.create_session = AsyncMock(return_value={"sessionId": "new"})
    client.api.playback_session.leave_session = AsyncMock()
    client.api.playback.play = AsyncMock()
    player.client = client
    player.connected = True
    player._attr_available = True
    player._attr_powered = True
    owner = Player(
        mass,
        player_id,
        PlayerConfig(values={}, provider=provider.instance_id, player_id=player_id),
    )
    mass.players._players[player_id] = owner
    mass.players._protocols[protocol_id] = player
    owner.add_protocol(player, ProtocolRole.NATIVE)
    player.set_player(owner)
    owner.set_initialized()
    queue = PlayerQueue(
        queue_id=player_id, active=False, display_name=player_id, available=True, items=1
    )
    item = QueueItem(queue_id=player_id, queue_item_id="abc", name="Shout", duration=200)
    mass.player_queues._queue_data[player_id] = PlayerQueueData(queue=queue, items=[item])
    return player


def _session_api(player: SonosPlayer) -> MagicMock:
    """Return the speaker's playback session commands, as the test speaker mocks them."""
    client = cast("MagicMock", player.client)
    return cast("MagicMock", client.api.playback_session)


def _cloud_queue_container(queue_id: str = SPEAKER_PLAYER) -> dict[str, Any]:
    """Return the container a speaker reports for a Music Assistant cloud queue."""
    return {
        "type": "trackList",
        "name": "Music Assistant",
        "service": {"name": "Music Assistant", "id": "mass"},
        "id": {"serviceId": "mass", "objectId": f"mass:{queue_id}", "accountId": ""},
    }


def _stream_container() -> dict[str, Any]:
    """Return the container a speaker reports for a Music Assistant queue played as one stream."""
    return {
        "type": "track",
        "name": "Shout",
        "service": {"name": "Music Assistant", "id": "mass"},
        "id": {"objectId": f"mass:{SPEAKER_PLAYER}:abc"},
    }


def _airplay_container() -> dict[str, Any]:
    """Return the container a speaker reports while it plays its AirPlay input."""
    return {"type": "linein.airplay", "name": "AirPlay", "service": {"name": "AirPlay"}}


def _report(
    group: SonosGroup,
    container: dict[str, Any],
    state: SonosPlayBackState = SonosPlayBackState.PLAYBACK_STATE_PLAYING,
) -> None:
    """Let the speaker report what its group plays, through the library's event handlers."""
    group._handle_playback_status_update(
        cast(
            "PlaybackStatus",
            {
                "playbackState": state,
                "positionMillis": 12000,
                "availablePlaybackActions": {},
                "playModes": {},
            },
        )
    )
    group._handle_metadata_status_update(
        cast(
            "MetadataStatus",
            {"container": container, "currentItem": {"id": "abc@1", "track": {"name": "Shout"}}},
        )
    )


def _update(mass: MagicMock, *players: SonosPlayer) -> PlayerQueue:
    """Process what the speakers reported, as their events and the queue updates would."""
    for player in players:
        player.on_player_event(None)
    for player in players:
        mass.player_queues.on_player_update(_player(player), {})
    return cast("PlayerQueue", mass.player_queues.get(_player(players[0]).player_id))


def _player(protocol: SonosPlayer) -> Player:
    """Return the player a speaker's protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


def _queue_media() -> PlayerMedia:
    """Return the media of the first item of a queue, as the queue controller hands it out."""
    return PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id=SPEAKER_PLAYER,
        queue_item_id="abc",
        queue_session_id="queue-session",
    )


@contextmanager
def _flow_mode(enabled: bool) -> Iterator[None]:
    """Let the speakers play a queue as one stream, or as a cloud queue."""
    with patch.object(SonosPlayer, "flow_mode", property(lambda _self: enabled)):
        yield


async def _play(player: SonosPlayer, media: PlayerMedia, on_load: Callable[[], None]) -> None:
    """Play media on the speaker, `on_load` running while the speaker loads it."""
    group = player.client.player.group
    assert group is not None

    async def _load(*_args: object, **_kwargs: object) -> None:
        # the library has created our session by the time the load is sent
        group.active_session_id = OUR_SESSION
        on_load()

    group.play_cloud_queue = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    group.play_stream_url = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    stream_url = "http://192.168.1.20:8097/flow/queue-session/RINCON/abc/RINCON.flac"
    cast("MagicMock", player.mass).streams.resolve_stream_url = AsyncMock(return_value=stream_url)
    await player.play_media(media)


def _evicted(player: SonosPlayer) -> None:
    """Let another app take the speaker over, evicting the session of our queue."""
    group = player.client.player.group
    assert group is not None
    # the library forgets the session, then signals the eviction
    group.active_session_id = None
    body = cast(
        "SessionError",
        {
            "_objectType": "sessionError",
            "errorCode": "ERROR_SESSION_EVICTED",
            "sessionId": OUR_SESSION,
        },
    )
    player._on_session_error(SessionErrorEvent(SonosEventType.SESSION_ERROR, group.id, body))


@pytest.mark.parametrize("container", [_cloud_queue_container(), _stream_container()])
async def test_a_shutdown_leaves_a_session_we_did_not_start_playing(
    container: dict[str, Any],
) -> None:
    """
    Test a speaker playing a session we did not start reads as a source of its own.

    The session is another server's, or one from before this server restarted: nothing is
    sent to the speaker at shutdown, so it keeps playing.
    """
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    _report(group, container)

    queue = _update(mass, player)
    await mass.player_queues.close()

    # a stop without a session of ours makes one, which ends the session that plays
    _session_api(player).create_session.assert_not_awaited()
    _session_api(player).leave_session.assert_not_awaited()
    assert _player(player).active_source == SOURCE_MUSIC_ASSISTANT
    assert _player(player).playback_state == PlaybackState.PLAYING
    assert queue.active is False
    assert queue.state == PlaybackState.IDLE


@pytest.mark.parametrize("flow_mode", [False, True])
async def test_a_shutdown_still_stops_our_own_queue(flow_mode: bool) -> None:
    """Test the queue we play on a speaker is stopped at shutdown, by leaving our session."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    container = _stream_container() if flow_mode else _cloud_queue_container()
    with _flow_mode(flow_mode):
        await _play(player, _queue_media(), lambda: None)
    # the stream session the queue controller opens when it starts the queue
    mass.player_queues._queue_data[SPEAKER_PLAYER].session_id = "queue-session"
    _report(group, container)

    queue = _update(mass, player)
    await mass.player_queues.close()

    assert _player(player).active_source == SPEAKER_PLAYER
    assert queue.active is True
    _session_api(player).leave_session.assert_awaited_once_with(OUR_SESSION)
    _session_api(player).create_session.assert_not_awaited()


def test_a_session_we_did_not_start_is_listed_as_a_source() -> None:
    """Test another server's session gets a passive entry and no item of our queue."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    _report(group, _cloud_queue_container())

    player.on_player_event(None)

    sources = [x for x in player.source_list if x.id == SOURCE_MUSIC_ASSISTANT]
    assert [(x.name, x.passive) for x in sources] == [("Music Assistant", True)]
    assert player.current_media is not None
    assert player.current_media.title == "Shout"
    assert player.current_media.source_id is None
    assert player.current_media.queue_item_id is None


@pytest.mark.parametrize("flow_mode", [False, True])
def test_our_own_queue_stays_our_queue(flow_mode: bool) -> None:
    """Test a speaker playing the queue we loaded, cloud queue or stream, plays our queue."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    group.active_session_id = OUR_SESSION
    if flow_mode:
        player._flow_queue_id = SPEAKER_PLAYER
    else:
        player.cloud_queue_id = SPEAKER_PLAYER
    _report(group, _stream_container() if flow_mode else _cloud_queue_container())

    queue = _update(mass, player)

    assert player._attr_active_source is None
    assert player.current_media is not None
    assert player.current_media.queue_item_id == "abc"
    assert queue.active is True
    assert queue.state == PlaybackState.PLAYING


def test_our_queue_stays_ours_when_the_library_lost_track_of_its_session() -> None:
    """Test a reconnect, which drops the library's session, does not hand our queue away."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    player.cloud_queue_id = SPEAKER_PLAYER
    _report(group, _cloud_queue_container())

    queue = _update(mass, player)

    assert player._attr_active_source is None
    assert queue.active is True


def test_a_paused_session_of_ours_stays_ours() -> None:
    """Test our paused queue is still ours, reported idle like a session we stopped."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    group.active_session_id = OUR_SESSION
    player.cloud_queue_id = SPEAKER_PLAYER
    _report(group, _cloud_queue_container(), SonosPlayBackState.PLAYBACK_STATE_PAUSED)

    queue = _update(mass, player)

    assert player._attr_active_source is None
    assert player._attr_playback_state == PlaybackState.IDLE
    assert queue.active is True


@pytest.mark.parametrize("flow_mode", [False, True])
async def test_our_queue_is_ours_while_the_speaker_loads_it(flow_mode: bool) -> None:
    """Test the speaker reporting a Music Assistant session while our play is still loading."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    _report(group, _cloud_queue_container())
    player.on_player_event(None)
    assert player._attr_active_source == SOURCE_MUSIC_ASSISTANT
    sources_while_loading: list[str | None] = []

    def _report_while_loading() -> None:
        _report(group, _stream_container() if flow_mode else _cloud_queue_container())
        player.on_player_event(None)
        sources_while_loading.append(player._attr_active_source)

    with _flow_mode(flow_mode):
        await _play(player, _queue_media(), _report_while_loading)

    assert sources_while_loading == [None]


async def test_an_announcement_we_play_is_ours() -> None:
    """Test an announcement played as a cloud queue is ours, the library holding its session."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    announcement = PlayerMedia(
        uri="http://192.168.1.20:8097/announcement/abc.mp3",
        media_type=MediaType.ANNOUNCEMENT,
    )
    sources_while_playing: list[str | None] = []

    def _report_while_playing() -> None:
        _report(group, _cloud_queue_container("unknown"))
        player.on_player_event(None)
        sources_while_playing.append(player._attr_active_source)

    await _play(player, announcement, _report_while_playing)

    assert player._attr_active_source is None
    assert sources_while_playing == [None]


async def test_a_session_that_took_ours_over_is_not_our_queue() -> None:
    """Test another server taking the speaker over ends our queue there."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    with _flow_mode(False):
        await _play(player, _queue_media(), lambda: None)
    mass.player_queues._queue_data[SPEAKER_PLAYER].session_id = "queue-session"
    _report(group, _cloud_queue_container())
    assert _update(mass, player).state == PlaybackState.PLAYING

    _evicted(player)
    # the other server's queue for this speaker carries the same queue id
    _report(group, _cloud_queue_container())
    queue = _update(mass, player)

    assert _player(player).active_source == SOURCE_MUSIC_ASSISTANT
    assert queue.active is False
    assert queue.state == PlaybackState.IDLE


@pytest.mark.parametrize(
    ("session_id", "queue_id", "expected"),
    [
        (OUR_SESSION, SPEAKER_PLAYER, None),
        # the coordinator's connection dropped and came back
        (None, SPEAKER_PLAYER, None),
        (None, None, SOURCE_MUSIC_ASSISTANT),
    ],
)
def test_a_group_member_follows_whose_session_its_coordinator_plays(
    session_id: str | None, queue_id: str | None, expected: str | None
) -> None:
    """Test the members of a group read the session as their coordinator does."""
    mass = _mass()
    leader = _speaker(mass)
    member = _speaker(mass, MEMBER, MEMBER_PLAYER, leader=SPEAKER)
    cast("MagicMock", leader.client).player.group_members = [SPEAKER, MEMBER]
    group = leader.client.player.group
    assert group is not None
    group.active_session_id = session_id
    leader.cloud_queue_id = queue_id
    _report(group, _cloud_queue_container())

    _update(mass, leader, member)

    assert leader._attr_active_source == expected
    assert member._attr_active_source == expected


def test_our_queue_over_airplay_is_still_our_queue() -> None:
    """Test our queue streamed to the speaker's AirPlay input, as by a Sendspin bridge too."""
    mass = _mass()
    player = _speaker(mass)
    group = player.client.player.group
    assert group is not None
    _player(player).set_active_mass_source(SPEAKER_PLAYER)
    _player(player).set_active_output_protocol("airplay_protocol_player")
    _report(group, _airplay_container())

    queue = _update(mass, player)

    assert player._attr_active_source == SOURCE_AIRPLAY
    assert _player(player).active_source == SPEAKER_PLAYER
    assert queue.active is True
    assert queue.state == PlaybackState.PLAYING


async def _play_our_queue(mass: MagicMock, player: SonosPlayer) -> SonosGroup:
    """Start our queue on the speaker as the controllers would, and let the speaker report it."""
    group = player.client.player.group
    assert group is not None
    _player(player).set_active_mass_source(_player(player).player_id)
    with _flow_mode(False):
        await _play(player, _queue_media(), lambda: None)
    mass.player_queues._queue_data[_player(player).player_id].session_id = "queue-session"
    _report(group, _cloud_queue_container())
    return group


@pytest.mark.parametrize(
    ("command", "reported"),
    [
        ("cmd_stop", SonosPlayBackState.PLAYBACK_STATE_PAUSED),
        ("cmd_stop", SonosPlayBackState.PLAYBACK_STATE_IDLE),
        # pausing our queue stops the speaker
        ("cmd_pause", SonosPlayBackState.PLAYBACK_STATE_PAUSED),
    ],
)
async def test_play_after_we_stopped_our_queue_resumes_it(
    command: str, reported: SonosPlayBackState
) -> None:
    """Test the session we left on the speaker hands play back to our queue."""
    mass = _mass()
    player = _speaker(mass)
    group = await _play_our_queue(mass, player)
    _update(mass, player)
    await getattr(mass.players, command)(SPEAKER_PLAYER)
    _session_api(player).leave_session.assert_awaited_once_with(OUR_SESSION)
    # the speaker keeps showing what our session played
    _report(group, _cloud_queue_container(), reported)
    queue = _update(mass, player)
    resume = AsyncMock()
    mass.player_queues.resume = resume

    await mass.players.cmd_play(SPEAKER_PLAYER)

    assert _player(player).playback_state == PlaybackState.IDLE
    assert _player(player).active_source == SPEAKER_PLAYER
    assert queue.active is True
    # the pause, next, previous and seek commands go to the same queue
    assert mass.players.get_active_queue(_player(player)) is queue
    resume.assert_awaited_once_with(SPEAKER_PLAYER)
    cast("MagicMock", player.client).api.playback.play.assert_not_awaited()


async def test_play_on_a_member_after_its_group_stopped_resumes_the_queue() -> None:
    """Test a member of a group we stopped hands play to the coordinator's queue."""
    mass = _mass()
    leader = _speaker(mass)
    member = _speaker(mass, MEMBER, MEMBER_PLAYER, leader=SPEAKER)
    cast("MagicMock", leader.client).player.group_members = [SPEAKER, MEMBER]
    group = await _play_our_queue(mass, leader)
    _update(mass, leader, member)
    await mass.players.cmd_stop(MEMBER_PLAYER)
    _session_api(leader).leave_session.assert_awaited_once_with(OUR_SESSION)
    _report(group, _cloud_queue_container(), SonosPlayBackState.PLAYBACK_STATE_PAUSED)
    _update(mass, leader, member)
    resume = AsyncMock()
    mass.player_queues.resume = resume

    await mass.players.cmd_play(MEMBER_PLAYER)

    resume.assert_awaited_once_with(SPEAKER_PLAYER)
    cast("MagicMock", leader.client).api.playback.play.assert_not_awaited()


async def test_play_after_our_queue_ran_out_resumes_it() -> None:
    """Test a queue that ended by itself is still ours to start again."""
    mass = _mass()
    player = _speaker(mass)
    group = await _play_our_queue(mass, player)
    _update(mass, player)
    _report(group, _cloud_queue_container(), SonosPlayBackState.PLAYBACK_STATE_IDLE)
    queue = _update(mass, player)
    resume = AsyncMock()
    mass.player_queues.resume = resume

    await mass.players.cmd_play(SPEAKER_PLAYER)

    assert player._attr_active_source is None
    assert queue.active is True
    resume.assert_awaited_once_with(SPEAKER_PLAYER)


async def test_a_session_started_after_we_stopped_ours_is_not_ours() -> None:
    """Test another server playing after we stopped our queue is not taken for our queue."""
    mass = _mass()
    player = _speaker(mass)
    group = await _play_our_queue(mass, player)
    _update(mass, player)
    await mass.players.cmd_stop(SPEAKER_PLAYER)
    _report(group, _cloud_queue_container(), SonosPlayBackState.PLAYBACK_STATE_PAUSED)
    _update(mass, player)

    _report(group, _cloud_queue_container())
    queue = _update(mass, player)
    await mass.player_queues.close()

    assert _player(player).active_source == SOURCE_MUSIC_ASSISTANT
    assert queue.active is False
    assert queue.state == PlaybackState.IDLE
    # the leave of our own stop is all that went to the speaker
    _session_api(player).leave_session.assert_awaited_once_with(OUR_SESSION)
    _session_api(player).create_session.assert_not_awaited()
