"""Tests for the lifecycle of a universal group on a running server: create, restart, remove."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import (
    ContentType,
    EventType,
    MediaType,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
    CONF_POWER_CONTROL,
)
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.universal_group.player import UniversalGroupPlayer
from music_assistant.providers.universal_group.ugp_stream import UGPStream
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

PIVOT_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2
)


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers and the real group providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


async def _members(scenario: Scenario) -> list[str]:
    """Load the provider, register two devices of different providers and return their player ids."""
    await scenario.mass.config._create_provider_instance("universal_group", {})
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    member_a = sonos.protocol("member_a", "Player A")
    member_b = airplay.protocol("member_b", "Player B")
    await scenario.register(member_a)
    await scenario.register(member_b)
    player_ids = [scenario.player_of(member_a), scenario.player_of(member_b)]
    assert None not in player_ids
    return [player_id for player_id in player_ids if player_id]


async def _create_group(
    scenario: Scenario, members: list[str], *, name: str = "Group Z", dynamic: bool = False
) -> Player:
    """Create a universal group the way a client does."""
    group = await scenario.mass.players.create_group_player(
        "universal_group", name, members, dynamic
    )
    await scenario.settle()
    return group


def _group_protocol(scenario: Scenario, player_id: str) -> UniversalGroupPlayer:
    """Return the attached group protocol of the player of a group."""
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    protocol = player.own_protocol
    assert isinstance(protocol, UniversalGroupPlayer)
    return protocol


def _routes(scenario: Scenario) -> set[str]:
    """Return the paths of the stream routes the universal groups registered."""
    routes = scenario.mass.streams._server._dynamic_routes or {}
    # the routes are keyed by "<method>.<path>"
    return {key.split(".", 1)[1] for key in routes if key.split(".", 1)[1].startswith("/ugp/")}


async def test_a_new_group_gets_a_player_of_its_own(scenario: Scenario) -> None:
    """A group created now is a player with a minted id; its members are its protocol's."""
    members = await _members(scenario)

    group = await _create_group(scenario, members)

    assert group.player_id.startswith(PLAYER_ID_PREFIX)
    assert group.type == PlayerType.GROUP
    assert group.name == "Group Z"
    protocol = _group_protocol(scenario, group.player_id)
    assert protocol.protocol_id != group.player_id
    assert scenario.mass.config.get_protocol_value(protocol.protocol_id, CONF_GROUP_MEMBERS) == (
        members
    )
    assert group.static_group_members == members
    # the stream of the group is served under the id of its protocol
    assert _routes(scenario) == {
        f"/ugp/{protocol.protocol_id}.flac",
        f"/ugp/{protocol.protocol_id}.mp3",
    }


async def test_a_new_group_is_announced_with_its_members(scenario: Scenario) -> None:
    """The player of a new group is announced with the members it was created with."""
    members = await _members(scenario)
    announced: list[tuple[list[str], bool]] = []
    signal_event = scenario.mass.signal_event

    def _record(event: EventType, object_id: str | None = None, data: Any = None) -> None:
        # the state as it is announced, not as it is by the time a subscriber runs
        if event == EventType.PLAYER_ADDED and isinstance(data, Player):
            announced.append(
                (
                    list(data.static_group_members),
                    PlayerFeature.SET_MEMBERS in data.supported_features,
                )
            )
        signal_event(event, object_id, data)

    with patch.object(scenario.mass, "signal_event", side_effect=_record):
        await _create_group(scenario, members, dynamic=True)

    assert announced == [(members, True)]


async def test_a_new_group_advertises_power_with_the_fake_power_control(
    scenario: Scenario,
) -> None:
    """The power control of the group's player decides whether the group protocol offers power."""
    group = await _create_group(scenario, await _members(scenario))
    protocol = _group_protocol(scenario, group.player_id)
    assert PlayerFeature.POWER not in protocol.supported_features

    await scenario.configure_player(group.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})

    assert PlayerFeature.POWER in protocol.supported_features
    assert PlayerFeature.POWER in group.supported_features


async def test_a_new_group_comes_back_with_its_members_after_a_restart(
    scenario: Scenario,
) -> None:
    """After a restart the group is the same player again, with the same members and power."""
    members = await _members(scenario)
    group = await _create_group(scenario, members)
    protocol_id = _group_protocol(scenario, group.player_id).protocol_id
    await scenario.configure_player(group.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})

    await scenario.restart()
    await scenario.idle()

    state = scenario.player(group.player_id)
    assert state is not None
    assert state.available
    assert state.name == "Group Z"
    assert state.static_group_members == members
    protocol = _group_protocol(scenario, group.player_id)
    assert protocol.protocol_id == protocol_id
    assert PlayerFeature.POWER in protocol.supported_features


async def test_a_group_stored_after_another_one_comes_back_when_enabled(
    scenario: Scenario,
) -> None:
    """Enabling a group attaches it again, also while a group stored before it is attached."""
    members = await _members(scenario)
    first = await _create_group(scenario, members, name="Group Y")
    second = await _create_group(scenario, members, name="Group Z")
    first_protocol = _group_protocol(scenario, first.player_id)
    await scenario.disable(second.player_id)
    assert scenario.player(second.player_id) is None

    await scenario.enable(second.player_id)
    await scenario.advance(15)
    await scenario.idle()

    state = scenario.player(second.player_id)
    assert state is not None
    assert state.available
    assert _group_protocol(scenario, first.player_id) is first_protocol
    second_protocol = _group_protocol(scenario, second.player_id)
    assert _routes(scenario) == {
        f"/ugp/{protocol_id}.{extension}"
        for protocol_id in (first_protocol.protocol_id, second_protocol.protocol_id)
        for extension in ("flac", "mp3")
    }


async def test_discovery_leaves_an_attached_group_alone(scenario: Scenario) -> None:
    """Discovering again keeps the attached group object and attaches nothing else."""
    group = await _create_group(scenario, await _members(scenario))
    protocol = _group_protocol(scenario, group.player_id)
    provider = scenario.mass.get_provider("universal_group")
    assert isinstance(provider, PlayerProvider)

    with patch.object(
        scenario.mass.players, "register", wraps=scenario.mass.players.register
    ) as attach:
        await provider.discover_players()

    attach.assert_not_called()
    assert _group_protocol(scenario, group.player_id) is protocol


async def test_discovery_attaches_a_group_that_is_stored_without_a_player(
    scenario: Scenario,
) -> None:
    """A group whose settings were stored before it got a player comes up when discovered."""
    members = await _members(scenario)
    config = scenario.mass.config
    config.create_protocol_record(
        "ugp_stored",
        provider="universal_group",
        player_id=None,
        role=ProtocolRole.VIRTUAL,
        default_name="Group Y",
    )
    config.set_protocol_value("ugp_stored", CONF_GROUP_MEMBERS, members)
    config.set_protocol_value("ugp_stored", CONF_DYNAMIC_GROUP_MEMBERS, False)
    provider = scenario.mass.get_provider("universal_group")
    assert isinstance(provider, PlayerProvider)

    await provider.discover_players()
    await scenario.idle()

    protocol = scenario.mass.players.get_protocol("ugp_stored")
    assert protocol is not None
    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.name == "Group Y"
    assert protocol.player.static_group_members == members


def _member_media(scenario: Scenario, group: Player, member_id: str) -> PlayerMedia:
    """Return the media a playing group hands a member, as play_media builds it."""
    protocol = _group_protocol(scenario, group.player_id)
    base_url = f"{scenario.mass.streams.base_url}/ugp/{protocol.protocol_id}.mp3"
    return PlayerMedia(
        uri=f"{base_url}?player_id={member_id}",
        media_type=MediaType.FLOW_STREAM,
        title=group.name,
        source_id=group.player_id,
        queue_session_id="session_a",
        custom_data={"ugp_player_id": group.player_id},
    )


def _live_stream() -> MagicMock:
    """Return a stand-in for the stream of a playing group, in the pivot format."""
    stream = MagicMock(spec=UGPStream)
    stream.done = False
    stream.base_pcm_format = PIVOT_FORMAT
    return stream


async def test_a_member_gets_the_stream_of_a_group_with_a_minted_id(scenario: Scenario) -> None:
    """A member that takes raw audio gets the one stream of its group, in the format it asks."""
    members = await _members(scenario)
    group = await _create_group(scenario, members)
    assert group.player_id.startswith(PLAYER_ID_PREFIX)
    stream = _group_protocol(scenario, group.player_id).stream = _live_stream()
    media = _member_media(scenario, group, members[0])
    other_format = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16)

    raw = scenario.mass.streams.get_stream(media, PIVOT_FORMAT, player_id="member_a")
    converted = scenario.mass.streams.get_stream(media, other_format, player_id="member_a")

    assert raw is stream.subscribe_raw.return_value
    assert converted is stream.get_stream.return_value
    stream.get_stream.assert_called_once_with(output_format=other_format)


async def test_the_queue_of_a_group_is_no_stream_it_shares(scenario: Scenario) -> None:
    """The group fetching the stream of its own queue does not get its own shared stream."""
    group = await _create_group(scenario, await _members(scenario))
    stream = _group_protocol(scenario, group.player_id).stream = _live_stream()
    media = PlayerMedia(uri="http://radio.local/stream.mp3", source_id=group.player_id)

    with patch(
        "music_assistant.controllers.streams.controller.get_ffmpeg_stream",
        return_value=MagicMock(),
    ) as get_ffmpeg_stream:
        audio = scenario.mass.streams.get_stream(media, PIVOT_FORMAT)

    assert audio is get_ffmpeg_stream.return_value
    stream.subscribe_raw.assert_not_called()


async def test_a_member_asking_after_the_stream_ended_gets_an_error(scenario: Scenario) -> None:
    """A member that asks for the stream of a group that stopped playing is refused."""
    members = await _members(scenario)
    group = await _create_group(scenario, members)
    protocol = _group_protocol(scenario, group.player_id)
    assert protocol.stream is None

    with pytest.raises(AudioError):
        scenario.mass.streams.get_stream(
            _member_media(scenario, group, members[0]), PIVOT_FORMAT, player_id="member_a"
        )


async def test_a_playing_group_that_is_disabled_ends_its_stream(scenario: Scenario) -> None:
    """A group taken off its player while it plays ends the stream its members play."""
    group = await _create_group(scenario, await _members(scenario))
    protocol = _group_protocol(scenario, group.player_id)
    stream = protocol.stream = _live_stream()

    await scenario.disable(group.player_id)
    await scenario.idle()

    stream.stop.assert_awaited_once_with()
    assert not protocol.is_active_session
    assert protocol.stream is None


async def test_removing_a_group_forgets_it(scenario: Scenario) -> None:
    """A removed group is gone with its records and routes, and does not come back."""
    group = await _create_group(scenario, await _members(scenario))
    protocol = _group_protocol(scenario, group.player_id)

    with (
        patch.object(UniversalGroupPlayer, "on_remove", autospec=True) as on_remove,
        patch.object(PlayerProvider, "schedule_discovery", autospec=True) as schedule_discovery,
    ):
        await scenario.remove(group.player_id)
    await scenario.idle()

    on_remove.assert_awaited_once_with(protocol)
    schedule_discovery.assert_not_called()
    assert scenario.player(group.player_id) is None
    assert scenario.mass.config.get_protocol_record(protocol.protocol_id) is None
    assert await scenario.player_config(group.player_id) is None
    assert _routes(scenario) == set()
    provider = scenario.mass.get_provider("universal_group")
    assert isinstance(provider, PlayerProvider)
    await provider.discover_players()
    await scenario.idle()
    assert scenario.player(group.player_id) is None
    assert scenario.mass.players.get_protocol(protocol.protocol_id) is None
