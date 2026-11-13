"""Tests for merging a player into another one: two players that turn out to be one device."""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage, ErrorResultMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
)
from music_assistant_models.event import MassEvent

from music_assistant.constants import (
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_GROUP_MEMBERS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROTOCOL_KEY_SPLITTER,
)
from music_assistant.controllers.config.player_split import (
    remove_player_records_of_older_versions,
)
from music_assistant.controllers.players.lifecycle import REMOVE_UNAVAILABLE_GRACE
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from tests.scenarios.harness import (
    DEFAULT_FEATURES,
    FAKE_SETTING,
    Event,
    FakeProtocol,
    Scenario,
    ScenarioError,
    VisiblePlayer,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# what the AirPlay and the Cast receiver of one TV report: nothing they share
TV_AIRPLAY = {IdentifierType.AIRPLAY_ID: "C0:FF:EE:00:00:01"}
TV_CAST = {IdentifierType.CAST_UUID: "6f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b"}
TV_IP = "192.168.1.60"
KITCHEN_MAC = "54:78:C9:E6:0D:A0"
KITCHEN = {IdentifierType.MAC_ADDRESS: KITCHEN_MAC}
HALL = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"}


class _TwoPlayers(NamedTuple):
    """The two players the receivers of one TV ended up as, the AirPlay one older."""

    airplay: FakeProtocol
    cast: FakeProtocol
    older_id: str
    younger_id: str


async def _two_players_for_one_tv(scenario: Scenario) -> _TwoPlayers:
    """Let the AirPlay and the Cast receiver of one TV end up as two players."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    tv_airplay = airplay.protocol("ap_tv", "Living TV", role="audio", identifiers=TV_AIRPLAY)
    tv_cast = chromecast.protocol("cast_tv", "Living TV", role="audio", identifiers=TV_CAST)
    await scenario.register(tv_airplay)
    await scenario.advance(15)
    await scenario.register(tv_cast)
    await scenario.advance(15)
    await scenario.idle()
    return _TwoPlayers(
        tv_airplay,
        tv_cast,
        scenario.single_player(tv_airplay).player_id,
        scenario.single_player(tv_cast).player_id,
    )


async def _merge(scenario: Scenario, keep_id: str, absorb_id: str) -> None:
    """Merge a player into another one and let the server settle."""
    await scenario.mass.players.merge_players(keep_id, absorb_id)
    await scenario.idle()


async def _call_as(scenario: Scenario, user: User, command: str, **args: Any) -> int | None:
    """
    Run an API command the way a client of the user does, and return its error code, if any.

    :param scenario: The scenario.
    :param user: The user the client signed in as.
    :param command: The API command.
    :param args: The arguments of the command.
    """
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client: Any = WebsocketClientHandler(scenario.mass.webserver, request)
    client._authenticated_user = user
    sent = client._send_message = AsyncMock()
    await client._handle_command(CommandMessage(message_id="1", command=command, args=args))
    await scenario.idle()
    for call in sent.await_args_list:
        if isinstance(message := call.args[0], ErrorResultMessage):
            return message.error_code
    return None


async def test_merged_player_carries_both_protocols_and_answers_to_the_absorbed_id(
    scenario: Scenario,
) -> None:
    """The kept player gets the protocols of the absorbed one, whose id becomes its legacy id."""
    tv = await _two_players_for_one_tv(scenario)
    scenario.clear_events()

    await scenario.mass.players.merge_players(tv.older_id, tv.younger_id)
    await scenario.settle()

    # the protocols are attached to the kept player at once
    assert scenario.player_of(tv.cast) == tv.older_id
    await scenario.idle()

    assert scenario.players_carrying(tv.airplay, tv.cast) == [
        VisiblePlayer(
            player_id=tv.older_id,
            name="Living TV",
            available=True,
            powered=None,
            provider="airplay",
            outputs=(("airplay", True), ("chromecast", True)),
        )
    ]
    kept = scenario.player(tv.older_id)
    assert kept is not None
    assert kept.legacy_ids == [tv.younger_id]
    resolved = scenario.player(tv.younger_id)
    assert resolved is not None
    assert resolved.player_id == tv.older_id
    assert scenario.queue(tv.younger_id) is scenario.queue(tv.older_id)
    assert scenario.events(EventType.PLAYER_REMOVED) == [
        Event(EventType.PLAYER_REMOVED, tv.younger_id)
    ]
    assert Event(EventType.PLAYER_UPDATED, tv.older_id) in scenario.events()
    assert await scenario.player_config(tv.younger_id) is None
    assert (scenario.mass.config.get_protocol_record("cast_tv") or {})["player_id"] == tv.older_id


async def test_merged_player_stays_one_player_after_a_restart(scenario: Scenario) -> None:
    """The protocols of a merged player attach to it again after a restart."""
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)

    await scenario.restart()
    await scenario.idle()

    airplay, cast = scenario.protocol("ap_tv"), scenario.protocol("cast_tv")
    assert [player.player_id for player in scenario.players_carrying(airplay, cast)] == [
        tv.older_id
    ]
    assert scenario.player_of(cast) == tv.older_id
    resolved = scenario.player(tv.younger_id)
    assert resolved is not None
    assert resolved.player_id == tv.older_id


@pytest.mark.parametrize("named_by_legacy_id", ["absorbed", "kept"])
async def test_merge_takes_legacy_ids(scenario: Scenario, named_by_legacy_id: str) -> None:
    """Either player, the one that goes or the one that stays, can be named by a legacy id."""
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    tv_dlna = dlna.protocol("uuid:tv", "Living TV", role="audio")
    await scenario.register(tv_dlna)
    await scenario.advance(15)
    dlna_id = scenario.single_player(tv_dlna).player_id
    # the Cast receiver's player goes into the DLNA one: its id is a legacy id from now on
    await _merge(scenario, dlna_id, tv.younger_id)

    if named_by_legacy_id == "absorbed":
        await _merge(scenario, tv.older_id, tv.younger_id)
        kept_id = tv.older_id
    else:
        await _merge(scenario, tv.younger_id, tv.older_id)
        kept_id = dlna_id

    assert [player.player_id for player in scenario.players_carrying(tv.airplay, tv_dlna)] == [
        kept_id
    ]


async def test_chain_of_merges_keeps_every_absorbed_id_resolving(scenario: Scenario) -> None:
    """A player merged into one that is merged on keeps resolving to the last one."""
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    tv_dlna = dlna.protocol("uuid:tv", "Living TV", role="audio")
    await scenario.register(tv_dlna)
    await scenario.advance(15)
    dlna_id = scenario.single_player(tv_dlna).player_id

    await _merge(scenario, tv.younger_id, tv.older_id)
    await _merge(scenario, dlna_id, tv.younger_id)

    kept = scenario.player(dlna_id)
    assert kept is not None
    assert kept.legacy_ids == [tv.younger_id, tv.older_id]
    for absorbed_id in (tv.older_id, tv.younger_id):
        resolved = scenario.player(absorbed_id)
        assert resolved is not None
        assert resolved.player_id == dlna_id
    assert [player.player_id for player in scenario.players_carrying(tv.airplay, tv.cast)] == [
        dlna_id
    ]


async def test_merge_keeps_the_name_and_settings_of_the_kept_player(scenario: Scenario) -> None:
    """Where both players have a name or setting, the kept player's stays."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.configure_player(tv.older_id, name="Living TV", **{CONF_MAX_VOLUME: 60})
    await scenario.configure_player(tv.younger_id, name="Kitchen TV", **{CONF_MAX_VOLUME: 80})

    await _merge(scenario, tv.older_id, tv.younger_id)

    config = await scenario.player_config(tv.older_id)
    assert config is not None
    assert config.name == "Living TV"
    assert config.get_value(CONF_MAX_VOLUME) == 60


async def test_merge_carries_over_what_only_the_absorbed_player_has(scenario: Scenario) -> None:
    """A name, a setting, DSP and queue settings only the absorbed player has are kept."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.configure_player(tv.younger_id, name="Kitchen TV", **{CONF_MIN_VOLUME: 5})
    await scenario.mass.config.save_dsp_config(tv.younger_id, DSPConfig(enabled=True))
    await scenario.mass.config.save_player_queue_config(
        tv.younger_id, {"volume_normalization": "disabled"}
    )
    # a setting of the protocol itself stays on its own config
    scenario.mass.config.set_protocol_value("cast_tv", FAKE_SETTING, "on the cast receiver")
    # what a player from before the split kept about its device
    scenario.mass.config.set(
        f"{CONF_PLAYERS}/{tv.younger_id}/values/{CONF_DEVICE_IDENTIFIERS}",
        {IdentifierType.MAC_ADDRESS.value: KITCHEN_MAC},
    )
    await scenario.settle()

    await _merge(scenario, tv.older_id, tv.younger_id)

    config = await scenario.player_config(tv.older_id)
    assert config is not None
    assert config.name == "Kitchen TV"
    assert config.get_value(CONF_MIN_VOLUME) == 5
    assert scenario.mass.config.get_player_dsp_config(tv.older_id).enabled
    assert (
        scenario.mass.config.get_raw_player_queue_config_value(tv.older_id, "volume_normalization")
        == "disabled"
    )
    assert (scenario.protocol_config("cast_tv") or {})[FAKE_SETTING] == "on the cast receiver"
    assert scenario.mass.config.get(
        f"{CONF_PLAYERS}/{tv.older_id}/values/{CONF_DEVICE_IDENTIFIERS}"
    ) == {IdentifierType.MAC_ADDRESS.value: KITCHEN_MAC}
    for tree in (CONF_PLAYERS, CONF_PLAYER_DSP, CONF_PLAYER_QUEUES):
        assert scenario.mass.config.get(f"{tree}/{tv.younger_id}") is None


async def test_merge_moves_group_memberships_and_user_filters(scenario: Scenario) -> None:
    """A group that lists the absorbed player and a user limited to it follow the kept player."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.mass.config._create_provider_instance("universal_group", {})
    group = await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", [tv.younger_id], False
    )
    group_protocol = scenario.mass.players.get_player(group.player_id)
    assert group_protocol is not None
    assert group_protocol.own_protocol is not None
    group_protocol_id = group_protocol.own_protocol.protocol_id
    scenario.mass.config.set_protocol_value(
        group_protocol_id, "allowed_members", [tv.younger_id, "pl_other"]
    )
    auth = scenario.mass.webserver.auth
    user = await auth.create_user(username="guest", player_filter=[tv.younger_id])
    await scenario.idle()

    await _merge(scenario, tv.older_id, tv.younger_id)

    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.group_members == [tv.older_id]
    stored_group = scenario.protocol_config(group_protocol_id) or {}
    assert stored_group[CONF_GROUP_MEMBERS] == [tv.older_id]
    assert stored_group["allowed_members"] == [tv.older_id, "pl_other"]
    stored_user = await auth.get_user(user.user_id)
    assert stored_user is not None
    assert stored_user.player_filter == [tv.older_id]


async def test_merge_of_a_player_that_plays_stops_its_queue(scenario: Scenario) -> None:
    """The queue of the absorbed player is stopped and dropped; the kept player has its own."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.play(tv.younger_id)
    assert await scenario.saved_queue(tv.younger_id)
    tv.cast.commands.clear()

    await _merge(scenario, tv.older_id, tv.younger_id)

    assert ("stop",) in tv.cast.commands
    assert await scenario.saved_queue(tv.younger_id) is None
    kept = scenario.player(tv.older_id)
    assert kept is not None
    assert kept.playback_state == PlaybackState.IDLE


async def test_merge_leaves_alone_what_the_device_plays_by_itself(scenario: Scenario) -> None:
    """A source the device of the absorbed player plays without the server plays on."""
    airplay = await scenario.add_provider("airplay")
    musiccast = await scenario.add_provider("musiccast")
    # the AirPlay receiver of the AVR was first seen without its address
    avr_airplay = airplay.protocol("ap_avr", "AVR", role="audio")
    await scenario.register(avr_airplay)
    await scenario.advance(15)
    keep_id = scenario.single_player(avr_airplay).player_id
    receiver = musiccast.protocol(
        "avr_1",
        "AVR",
        identifiers=KITCHEN,
        playback_state=PlaybackState.PLAYING,
        active_source="hdmi1",
    )
    await scenario.register(receiver)
    receiver_id = scenario.single_player(receiver).player_id

    await _merge(scenario, keep_id, receiver_id)

    assert ("stop",) not in receiver.commands
    kept = scenario.player(keep_id)
    assert kept is not None
    assert kept.playback_state == PlaybackState.PLAYING


async def test_merge_of_a_group_member_keeps_it_in_its_group(scenario: Scenario) -> None:
    """
    A member of a group that plays is merged without leaving it: the kept player is a member.

    Its protocol is detached to move, and nothing starts it again by itself.
    """
    tv = await _two_players_for_one_tv(scenario)
    sonos = await scenario.add_provider("sonos")
    hall = sonos.protocol("RINCON_HALL", "Hall", identifiers=HALL)
    await scenario.register(hall)
    hall_id = scenario.single_player(hall).player_id
    await scenario.mass.config._create_provider_instance("universal_group", {})
    group = await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", [tv.younger_id, hall_id], False
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    tv.cast.commands.clear()
    detached: list[str] = []
    on_detach = tv.cast.on_detach

    async def _on_detach() -> None:
        detached.append(tv.cast.protocol_id)
        await on_detach()

    tv.cast.on_detach = _on_detach  # type: ignore[method-assign]

    await _merge(scenario, tv.older_id, tv.younger_id)

    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.group_members == [tv.older_id, hall_id]
    assert group_state.playback_state == PlaybackState.PLAYING
    assert ("stop",) not in tv.cast.commands
    assert detached == ["cast_tv"]
    assert "play_media" not in [command[0] for command in tv.cast.commands]


async def test_merge_of_a_sync_leader_keeps_its_members(scenario: Scenario) -> None:
    """A sync leader that is merged away leaves its members synced to the kept player."""
    airplay = await scenario.add_provider("airplay")
    sonos = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    # the AirPlay receiver of the speaker was first seen without its address
    kitchen_airplay = airplay.protocol("ap_kitchen", "Kitchen", role="audio")
    await scenario.register(kitchen_airplay)
    await scenario.advance(15)
    keep_id = scenario.single_player(kitchen_airplay).player_id
    leader = sonos.protocol(
        "RINCON_KITCHEN",
        "Kitchen",
        identifiers=KITCHEN,
        supported_features=features,
        can_group_with={"RINCON_HALL"},
    )
    member = sonos.protocol(
        "RINCON_HALL",
        "Hall",
        identifiers=HALL,
        supported_features=features,
        can_group_with={"RINCON_KITCHEN"},
    )
    await scenario.register(leader)
    await scenario.register(member)
    # the speakers learn who they can be grouped with
    await scenario.advance(3)
    leader_id = scenario.single_player(leader).player_id
    member_id = scenario.single_player(member).player_id
    await scenario.mass.players.cmd_set_members(leader_id, player_ids_to_add=[member_id])
    await scenario.idle()

    await _merge(scenario, keep_id, leader_id)

    member_state = scenario.player(member_id)
    assert member_state is not None
    assert member_state.synced_to == keep_id
    kept = scenario.player(keep_id)
    assert kept is not None
    assert kept.group_members == [keep_id, member_id]


async def test_merge_of_a_disabled_player_brings_its_device_to_the_kept_player(
    scenario: Scenario,
) -> None:
    """A disabled player that is merged goes, and its device attaches to the kept player."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.disable(tv.younger_id)

    await _merge(scenario, tv.older_id, tv.younger_id)

    assert scenario.player_of(tv.cast) == tv.older_id
    assert await scenario.player_config(tv.younger_id) is None


async def test_merge_of_an_unavailable_player_takes_its_device_when_it_returns(
    scenario: Scenario,
) -> None:
    """
    A player whose device is away is merged; its device attaches to the kept player later.

    Also after a restart, when only the stored config tells which player it belongs to.
    """
    tv = await _two_players_for_one_tv(scenario)
    await scenario.unregister(tv.cast)

    await scenario.mass.players.merge_players(tv.older_id, tv.younger_id)
    await scenario.settle()

    # the kept player answers to its new legacy id at once
    kept = scenario.player(tv.older_id)
    assert kept is not None
    assert kept.legacy_ids == [tv.younger_id]
    await scenario.idle()
    assert scenario.players_carrying(tv.cast) == [
        VisiblePlayer(
            player_id=tv.older_id,
            name="Living TV",
            available=True,
            powered=None,
            provider="airplay",
            outputs=(("airplay", True), ("chromecast", False)),
        )
    ]
    await scenario.restart()
    await scenario.register(scenario.protocol("cast_tv"))

    assert scenario.player_of(scenario.protocol("cast_tv")) == tv.older_id


async def test_two_players_that_each_have_a_native_protocol_are_not_merged(
    scenario: Scenario,
) -> None:
    """Two players that each stand for a device of their own stay as they are."""
    sonos = await scenario.add_provider("sonos")
    bluesound = await scenario.add_provider("bluesound")
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    hall = bluesound.protocol("bluesound_hall", "Hall", identifiers=HALL)
    await scenario.register(kitchen)
    await scenario.register(hall)
    kitchen_id = scenario.single_player(kitchen).player_id
    hall_id = scenario.single_player(hall).player_id

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.merge_players(kitchen_id, hall_id)

    assert refused.value.translation_key == "merge_two_own_protocols"
    assert scenario.player_of(hall) == hall_id
    kept = scenario.player(kitchen_id)
    assert kept is not None
    assert kept.legacy_ids == []


async def test_two_players_with_a_protocol_of_one_domain_are_not_merged(
    scenario: Scenario,
) -> None:
    """Two players that both have an AirPlay protocol stay as they are."""
    airplay = await scenario.add_provider("airplay")
    kitchen = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    hall = airplay.protocol("ap_hall", "Hall", role="audio", identifiers=HALL)
    await scenario.register(kitchen)
    await scenario.register(hall)
    kitchen_id = scenario.single_player(kitchen).player_id
    hall_id = scenario.single_player(hall).player_id

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.merge_players(kitchen_id, hall_id)

    assert refused.value.translation_key == "merge_shared_domain"
    assert scenario.player_of(hall) == hall_id


async def test_two_players_whose_identifiers_differ_are_not_merged(scenario: Scenario) -> None:
    """Two players whose devices report different AirPlay ids stay as they are."""
    airplay = await scenario.add_provider("airplay")
    bluesound = await scenario.add_provider("bluesound")
    receiver = airplay.protocol(
        "ap_kitchen", "Kitchen", role="audio", identifiers={IdentifierType.AIRPLAY_ID: "AA:01"}
    )
    speaker = bluesound.protocol(
        "bluesound_hall", "Hall", identifiers={IdentifierType.AIRPLAY_ID: "AA:02", **HALL}
    )
    await scenario.register(receiver)
    await scenario.advance(15)
    await scenario.register(speaker)
    receiver_id = scenario.single_player(receiver).player_id
    speaker_id = scenario.single_player(speaker).player_id

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.merge_players(speaker_id, receiver_id)

    assert refused.value.translation_key == "merge_identifiers_differ"
    assert scenario.player_of(receiver) == receiver_id


async def test_group_player_is_not_merged(scenario: Scenario) -> None:
    """A group player is no device, so it is not merged nor merged into."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.mass.config._create_provider_instance("universal_group", {})
    group = await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", [tv.older_id], False
    )
    await scenario.settle()

    for keep_id, absorb_id in ((group.player_id, tv.younger_id), (tv.younger_id, group.player_id)):
        with pytest.raises(InvalidCommand) as refused:
            await scenario.mass.players.merge_players(keep_id, absorb_id)
        assert refused.value.translation_key == "merge_group"


async def test_session_only_player_is_not_merged(scenario: Scenario) -> None:
    """A player that only exists while it is connected is not merged."""
    tv = await _two_players_for_one_tv(scenario)
    sendspin = await scenario.add_provider("sendspin")
    web_player = sendspin.protocol("sendspin_web_1", "Browser", role="audio", session_only=True)
    await scenario.register(web_player)
    web_player_id = scenario.single_player(web_player).player_id

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.merge_players(tv.older_id, web_player_id)

    assert refused.value.translation_key == "merge_session_only"
    assert scenario.player_of(web_player) == web_player_id


@pytest.mark.parametrize("absorb_id", ["itself", "pl_unknown"])
async def test_merge_needs_two_existing_players(scenario: Scenario, absorb_id: str) -> None:
    """A player is not merged into itself, nor is a player merged that does not exist."""
    tv = await _two_players_for_one_tv(scenario)

    with pytest.raises(InvalidDataError):
        await scenario.mass.players.merge_players(
            tv.older_id, tv.older_id if absorb_id == "itself" else absorb_id
        )


async def test_merge_command_needs_the_player_config_scope(scenario: Scenario) -> None:
    """A user without the scope to configure players can not merge them."""
    tv = await _two_players_for_one_tv(scenario)
    user = await scenario.mass.webserver.auth.create_user(username="member", role=UserRole.USER)

    error = await _call_as(
        scenario, user, "players/merge", player_id=tv.younger_id, target_player=tv.older_id
    )

    assert error == InsufficientPermissions.error_code
    assert scenario.player_of(tv.cast) == tv.younger_id


async def test_merge_command_follows_the_policy_of_removing_a_player(scenario: Scenario) -> None:
    """
    With the scope to configure players, a player filter does not limit a merge.

    Removing a player demands only that scope, also for a player outside the filter of the
    user, and merging demands the same.
    """
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    speaker = dlna.protocol("uuid:speaker", "Speaker", role="audio", identifiers=HALL)
    await scenario.register(speaker)
    speaker_id = scenario.single_player(speaker).player_id
    scenario.set_present("uuid:speaker", False)
    user = await scenario.mass.webserver.auth.create_user(
        username="service", role=UserRole.SERVICE, player_filter=[tv.older_id]
    )

    removed = await _call_as(scenario, user, "players/remove", player_id=speaker_id)
    merged = await _call_as(
        scenario, user, "players/merge", player_id=tv.younger_id, target_player=tv.older_id
    )

    assert (removed, merged) == (None, None)
    assert scenario.player_of(tv.cast) == tv.older_id


async def test_merge_command_takes_legacy_ids(scenario: Scenario) -> None:
    """The ids the command is given are resolved like those of every other command."""
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    tv_dlna = dlna.protocol("uuid:tv", "Living TV", role="audio")
    await scenario.register(tv_dlna)
    await scenario.advance(15)
    dlna_id = scenario.single_player(tv_dlna).player_id
    await _merge(scenario, dlna_id, tv.younger_id)
    admin = await scenario.mass.webserver.auth.create_user(username="admin", role=UserRole.ADMIN)

    error = await _call_as(
        scenario, admin, "players/merge", player_id=tv.older_id, target_player=tv.younger_id
    )

    assert error is None
    assert [player.player_id for player in scenario.players_carrying(tv.airplay)] == [dlna_id]


async def _duplicates(scenario: Scenario) -> _TwoPlayers:
    """
    Let the receivers of one TV end up as two players, which can tell they are one later.

    The Cast receiver reports the address of the TV; the AirPlay receiver does once it is
    found again (see _seen_again_with_its_address).
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    tv_airplay = airplay.protocol("ap_tv", "Living TV", role="audio", identifiers=TV_AIRPLAY)
    tv_cast = chromecast.protocol(
        "cast_tv",
        "Living TV",
        role="audio",
        identifiers={**TV_CAST, IdentifierType.IP_ADDRESS: TV_IP},
    )
    await scenario.register(tv_airplay)
    await scenario.advance(15)
    await scenario.register(tv_cast)
    await scenario.advance(15)
    await scenario.idle()
    older_id = scenario.single_player(tv_airplay).player_id
    younger_id = scenario.single_player(tv_cast).player_id
    if older_id == younger_id:
        msg = "the receivers of the TV were matched at first sight"
        raise ScenarioError(msg)
    return _TwoPlayers(tv_airplay, tv_cast, older_id, younger_id)


async def _seen_again_with_its_address(scenario: Scenario, protocol: FakeProtocol) -> None:
    """Let the AirPlay receiver report the address of the TV and be found again."""
    await scenario.report(protocol, identifiers={IdentifierType.IP_ADDRESS: TV_IP})
    await scenario.unregister(protocol)
    await scenario.register(protocol)
    await scenario.idle()


async def test_unused_duplicate_merges_into_the_older_player_by_itself(
    scenario: Scenario,
) -> None:
    """A younger duplicate nothing is set on goes into the older player once they match."""
    tv = await _duplicates(scenario)

    await _seen_again_with_its_address(scenario, tv.airplay)

    assert [player.player_id for player in scenario.players_carrying(tv.airplay, tv.cast)] == [
        tv.older_id
    ]
    kept = scenario.player(tv.older_id)
    assert kept is not None
    assert kept.legacy_ids == [tv.younger_id]


async def _renamed(scenario: Scenario, tv: _TwoPlayers) -> None:
    await scenario.configure_player(tv.younger_id, name="Kitchen TV")


async def _player_setting(scenario: Scenario, tv: _TwoPlayers) -> None:
    await scenario.configure_player(tv.younger_id, **{CONF_MAX_VOLUME: 80})


async def _protocol_setting(scenario: Scenario, tv: _TwoPlayers) -> None:
    key = f"cast_tv{CONF_PROTOCOL_KEY_SPLITTER}{FAKE_SETTING}"
    await scenario.configure_player(tv.younger_id, **{key: "on the cast receiver"})
    if (scenario.protocol_config("cast_tv") or {}).get(FAKE_SETTING) != "on the cast receiver":
        msg = "the setting of the Cast receiver was not stored"
        raise ScenarioError(msg)


async def _dsp(scenario: Scenario, tv: _TwoPlayers) -> None:
    await scenario.mass.config.save_dsp_config(tv.younger_id, DSPConfig(enabled=True))


async def _queue_settings(scenario: Scenario, tv: _TwoPlayers) -> None:
    await scenario.mass.config.save_player_queue_config(
        tv.younger_id, {"volume_normalization": "disabled"}
    )


async def _played(scenario: Scenario, tv: _TwoPlayers) -> None:
    await scenario.play(tv.younger_id)
    await scenario.mass.player_queues.stop(tv.younger_id)
    await scenario.idle()


async def _group_member(scenario: Scenario, tv: _TwoPlayers) -> None:
    await scenario.mass.config._create_provider_instance("universal_group", {})
    await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", [tv.younger_id], False
    )
    await scenario.idle()


async def _in_active_group(scenario: Scenario, tv: _TwoPlayers) -> None:
    group = scenario.provider("chromecast").protocol(
        "cast_group",
        "Cast Group",
        role="virtual",
        type=PlayerType.GROUP,
        group_members=["cast_tv"],
        powered=True,
    )
    await scenario.register(group)
    await scenario.idle()
    if (state := scenario.player(tv.younger_id)) is None or not state.active_group:
        msg = f"{tv.younger_id} is in no active group"
        raise ScenarioError(msg)


@pytest.mark.parametrize(
    "use",
    [
        _renamed,
        _player_setting,
        _protocol_setting,
        _dsp,
        _queue_settings,
        _played,
        _group_member,
        _in_active_group,
    ],
    ids=[
        "renamed",
        "player_setting",
        "protocol_setting",
        "dsp",
        "queue_settings",
        "queue_items",
        "group_member",
        "active_group",
    ],
)
async def test_used_duplicate_stays_a_player_of_its_own(
    scenario: Scenario, use: Callable[[Scenario, _TwoPlayers], Awaitable[None]]
) -> None:
    """A younger duplicate the user set something on, that played or is grouped, stays."""
    tv = await _duplicates(scenario)
    await use(scenario, tv)

    await _seen_again_with_its_address(scenario, tv.airplay)

    assert [player.player_id for player in scenario.players_carrying(tv.cast)] == [tv.younger_id]
    assert [player.player_id for player in scenario.players_carrying(tv.airplay)] == [tv.older_id]


async def test_player_without_a_creation_moment_counts_as_the_older(scenario: Scenario) -> None:
    """Of two duplicates, one whose record holds no creation moment is kept."""
    tv = await _duplicates(scenario)
    scenario.mass.config.remove(f"{CONF_PLAYERS}/{tv.younger_id}/values/{CONF_CREATED_AT}")

    await _seen_again_with_its_address(scenario, tv.airplay)

    assert [player.player_id for player in scenario.players_carrying(tv.airplay, tv.cast)] == [
        tv.younger_id
    ]


async def test_player_from_before_the_split_is_kept_of_two_equally_old_ones(
    scenario: Scenario,
) -> None:
    """Of two players without a creation moment, the one with an id from before the split is kept."""
    config = scenario.mass.config
    # an id from before the split that sorts after a minted one
    for player_id in ("pl0123456789ab", "up0123abcd"):
        config.create_player_record(
            player_id, provider="sonos", player_type=PlayerType.PLAYER, default_name="Kitchen"
        )

    ranked = sorted(("pl0123456789ab", "up0123abcd"), key=scenario.mass.players._merge_rank)

    assert ranked == ["up0123abcd", "pl0123456789ab"]


async def test_duplicates_with_a_protocol_of_one_domain_stay_apart(scenario: Scenario) -> None:
    """Two players of one host that each have a DLNA protocol are not merged by their address."""
    dlna = await scenario.add_provider("dlna")
    host = {IdentifierType.IP_ADDRESS: "192.168.1.40"}
    first = dlna.protocol("uuid:renderer-1", "Renderer 1", role="audio", identifiers=host)
    second = dlna.protocol("uuid:renderer-2", "Renderer 2", role="audio", identifiers=host)
    await scenario.register(first)
    await scenario.advance(15)
    await scenario.register(second)
    await scenario.advance(15)
    await scenario.idle()

    first_id, second_id = scenario.player_of(first), scenario.player_of(second)
    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id


async def test_disabled_duplicate_is_not_merged_by_itself(scenario: Scenario) -> None:
    """A disabled player is the user's choice: it stays, also once its device turns out known."""
    tv = await _duplicates(scenario)
    await scenario.disable(tv.younger_id)

    await _seen_again_with_its_address(scenario, tv.airplay)

    assert await scenario.player_config(tv.younger_id) is not None
    assert (scenario.mass.config.get_protocol_record("cast_tv") or {})["player_id"] == tv.younger_id


@pytest.mark.parametrize("grouping", ["none", "member", "leader"])
async def test_synced_duplicate_stays_a_player_of_its_own(
    scenario: Scenario, grouping: str
) -> None:
    """
    A younger duplicate that is synced to a leader, or leads members, stays.

    Without a sync it is merged, as the control.
    """
    airplay = await scenario.add_provider("airplay")
    sonos = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    # the AirPlay receiver of the speaker was first seen without its address
    kitchen_airplay = airplay.protocol("ap_kitchen", "Kitchen", role="audio")
    await scenario.register(kitchen_airplay)
    await scenario.advance(15)
    older_id = scenario.single_player(kitchen_airplay).player_id
    kitchen = sonos.protocol(
        "RINCON_KITCHEN",
        "Kitchen",
        identifiers=KITCHEN,
        supported_features=features,
        can_group_with={"RINCON_HALL"},
    )
    hall = sonos.protocol(
        "RINCON_HALL",
        "Hall",
        identifiers=HALL,
        supported_features=features,
        can_group_with={"RINCON_KITCHEN"},
    )
    await scenario.register(kitchen)
    await scenario.register(hall)
    # the speakers learn who they can be grouped with
    await scenario.advance(3)
    younger_id = scenario.single_player(kitchen).player_id
    hall_id = scenario.single_player(hall).player_id
    if grouping == "member":
        await scenario.mass.players.cmd_set_members(hall_id, player_ids_to_add=[younger_id])
    elif grouping == "leader":
        await scenario.mass.players.cmd_set_members(younger_id, player_ids_to_add=[hall_id])
    await scenario.idle()

    await scenario.report(kitchen_airplay, identifiers=KITCHEN)
    await scenario.unregister(kitchen_airplay)
    await scenario.register(kitchen_airplay)
    await scenario.idle()

    expected = [older_id] if grouping == "none" else [younger_id]
    assert [player.player_id for player in scenario.players_carrying(kitchen)] == expected


async def test_duplicate_is_merged_when_its_attached_protocol_is_attached_again(
    scenario: Scenario,
) -> None:
    """A provider that announces an attached device again with new identifiers merges it."""
    tv = await _duplicates(scenario)
    await scenario.report(tv.airplay, identifiers={IdentifierType.IP_ADDRESS: TV_IP})

    await scenario.register(tv.airplay)
    await scenario.idle()

    assert [player.player_id for player in scenario.players_carrying(tv.cast)] == [tv.older_id]


async def test_every_unused_duplicate_of_a_device_is_merged_at_once(scenario: Scenario) -> None:
    """A player that turns out to stand for the devices of two others takes in both."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    dlna = await scenario.add_provider("dlna")
    serial = {IdentifierType.SERIAL_NUMBER: "TV-0001"}
    tv_airplay = airplay.protocol("ap_tv", "Living TV", role="audio", identifiers=TV_AIRPLAY)
    tv_cast = chromecast.protocol(
        "cast_tv", "Living TV", role="audio", identifiers={**TV_CAST, **KITCHEN}
    )
    tv_dlna = dlna.protocol("uuid:tv", "Living TV", role="audio", identifiers=serial)
    for protocol in (tv_airplay, tv_cast, tv_dlna):
        await scenario.register(protocol)
        await scenario.advance(15)
    await scenario.idle()
    older_id = scenario.single_player(tv_airplay).player_id
    if len({scenario.player_of(p) for p in (tv_airplay, tv_cast, tv_dlna)}) != 3:
        msg = "the receivers of the TV were matched at first sight"
        raise ScenarioError(msg)

    # the AirPlay receiver learns what ties it to both others
    await scenario.report(tv_airplay, identifiers={**KITCHEN, **serial})
    await scenario.unregister(tv_airplay)
    await scenario.register(tv_airplay)
    await scenario.idle()

    assert [
        player.player_id for player in scenario.players_carrying(tv_airplay, tv_cast, tv_dlna)
    ] == [older_id]


def _queue_state(scenario: Scenario, queue_id: str) -> PlaybackState | None:
    """Return the playback state of a queue, None for a queue that does not exist."""
    queue = scenario.queue(queue_id)
    return queue.state if queue else None


async def test_queue_commands_take_the_id_of_a_merged_player(scenario: Scenario) -> None:
    """
    A queue command called inside the server with a legacy id works on the queue it names.

    A plugin setting can still name a player that was merged into another one.
    """
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)
    queues = scenario.mass.player_queues
    legacy_id = tv.younger_id
    queue = scenario.queue(tv.older_id)
    assert queue is not None

    await scenario.play(legacy_id)
    assert _queue_state(scenario, tv.older_id) == PlaybackState.PLAYING
    await queues.set_shuffle(legacy_id, True)
    queues.set_autoplay(legacy_id, not queue.autoplay_enabled)
    queues.set_crossfade(legacy_id, not queue.crossfade_enabled)
    await queues.set_repeat(legacy_id, RepeatMode.ALL)
    await queues.set_overlay(legacy_id, enabled=False)
    await scenario.command(queues.skip(legacy_id, 10))
    await queues.pause(legacy_id)
    await scenario.idle()
    assert _queue_state(scenario, tv.older_id) == PlaybackState.PAUSED
    await queues.stop(legacy_id)
    await scenario.idle()
    assert _queue_state(scenario, tv.older_id) == PlaybackState.IDLE
    await scenario.command(queues.resume(legacy_id))
    await scenario.idle()
    assert _queue_state(scenario, tv.older_id) == PlaybackState.PLAYING
    await queues.stop(legacy_id)
    await scenario.idle()
    for track in ("0_0_1", "0_0_2"):
        await queues.play_media(legacy_id, f"test://track/{track}", option=QueueOption.ADD)
    current, second, third = (item.queue_item_id for item in queues.items(tv.older_id))
    queues.move_item_end(legacy_id, second)
    queues.move_item(legacy_id, second, -1)
    queues.delete_item(legacy_id, third)
    assert [item.queue_item_id for item in queues.items(tv.older_id)] == [current, second]
    queues.clear(legacy_id)
    assert queue.items == 0


async def test_pause_by_the_id_of_a_merged_player_calls_off_a_pending_skip(
    scenario: Scenario,
) -> None:
    """A pause right after a skip to the next track keeps the queue paused."""
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)
    for receiver in (tv.airplay, tv.cast):
        await scenario.report(
            receiver, supported_features={*DEFAULT_FEATURES, PlayerFeature.ENQUEUE}
        )
    queues = scenario.mass.player_queues
    await scenario.play(tv.younger_id)
    await queues.play_media(tv.younger_id, "test://track/0_0_1", option=QueueOption.ADD)
    await scenario.idle()

    await queues.next(tv.younger_id)
    await queues.pause(tv.younger_id)
    await scenario.advance(2)
    await scenario.idle()

    queue = scenario.queue(tv.older_id)
    assert queue is not None
    assert queue.state == PlaybackState.PAUSED


async def test_queue_is_transferred_by_the_id_of_a_merged_player(scenario: Scenario) -> None:
    """A transfer takes legacy ids for both of its queues."""
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    speaker = dlna.protocol("uuid:speaker", "Speaker", role="audio", identifiers=HALL)
    await scenario.register(speaker)
    speaker_id = scenario.single_player(speaker).player_id
    await _merge(scenario, tv.older_id, tv.younger_id)
    await scenario.play(tv.older_id)

    await scenario.mass.player_queues.transfer_queue(tv.younger_id, speaker_id)
    await scenario.idle()

    speaker_queue = scenario.queue(speaker_id)
    assert speaker_queue is not None
    assert speaker_queue.state == PlaybackState.PLAYING
    assert speaker_queue.items == 1

    await scenario.mass.player_queues.transfer_queue(speaker_id, tv.younger_id)
    await scenario.idle()

    kept_queue = scenario.queue(tv.older_id)
    assert kept_queue is not None
    assert kept_queue.state == PlaybackState.PLAYING


async def test_merge_and_a_removal_of_the_kept_player_leave_no_partial_player(
    scenario: Scenario,
) -> None:
    """A removal of the kept player that runs while the merge saves removes the merged player."""
    tv = await _two_players_for_one_tv(scenario)
    players = scenario.mass.players

    merge = asyncio.create_task(players.merge_players(tv.older_id, tv.younger_id))
    removal = asyncio.create_task(players.remove(tv.older_id))
    await scenario.settle()
    await merge
    await removal
    await scenario.advance(20)
    await scenario.idle()

    stored = scenario.mass.config.get(CONF_PLAYERS, {})
    assert tv.older_id not in stored
    assert tv.younger_id not in stored
    # every stored player is a whole one: the devices that are still there came back as new
    assert all(CONF_CREATED_AT in record["values"] for record in stored.values())
    assert {scenario.player_of(tv.airplay), scenario.player_of(tv.cast)}.isdisjoint(
        {tv.older_id, tv.younger_id, None}
    )


async def test_a_protocol_its_provider_detaches_during_the_merge_is_not_attached_again(
    scenario: Scenario,
) -> None:
    """A device that goes while the merge saves stays detached, on the kept player."""
    tv = await _two_players_for_one_tv(scenario)
    players = scenario.mass.players
    scenario.set_present("cast_tv", False)

    merge = asyncio.create_task(players.merge_players(tv.older_id, tv.younger_id))
    detach = asyncio.create_task(players.unregister("cast_tv"))
    await scenario.settle()
    await merge
    await detach
    await scenario.idle()

    assert scenario.player_of(tv.cast) is None
    assert [player.outputs for player in scenario.players_carrying(tv.cast)] == [
        (("airplay", True), ("chromecast", False))
    ]


async def test_a_protocol_detached_while_it_moves_is_not_attached_again(
    scenario: Scenario,
) -> None:
    """A device that goes after it left the absorbed player is not put on the kept one."""
    tv = await _two_players_for_one_tv(scenario)
    auth = scenario.mass.webserver.auth
    rewrite_filters = auth.replace_player_in_user_filters
    scenario.set_present("cast_tv", False)

    async def detach_then_rewrite(*args: Any, **kwargs: Any) -> None:
        await scenario.mass.players.unregister("cast_tv")
        await rewrite_filters(*args, **kwargs)

    with patch.object(auth, "replace_player_in_user_filters", new=detach_then_rewrite):
        await _merge(scenario, tv.older_id, tv.younger_id)

    assert scenario.player_of(tv.cast) is None
    await scenario.register(tv.cast)
    assert scenario.player_of(tv.cast) == tv.older_id


async def test_merge_is_stored_before_the_queue_of_the_absorbed_player_is_deleted(
    scenario: Scenario,
) -> None:
    """When the saved queue of the absorbed player goes, the settings file holds the merge."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.play(tv.younger_id)
    assert await scenario.saved_queue(tv.younger_id)
    await scenario.mass.config.async_save()
    cache = scenario.mass.cache
    delete = cache.delete
    # whether the settings file still held the absorbed player, at each delete of its queue
    stored_at_delete: list[bool] = []

    async def record_and_delete(key: str, *args: Any, **kwargs: Any) -> None:
        if key == tv.younger_id:
            on_disk = json.loads(pathlib.Path(scenario.mass.config.filename).read_text())
            stored_at_delete.append(tv.younger_id in on_disk.get(CONF_PLAYERS, {}))
        await delete(key, *args, **kwargs)

    with patch.object(cache, "delete", new=record_and_delete):
        await _merge(scenario, tv.older_id, tv.younger_id)

    assert stored_at_delete
    assert not any(stored_at_delete)
    assert await scenario.saved_queue(tv.younger_id) is None


async def test_a_user_limited_to_the_absorbed_player_learns_that_it_went(
    scenario: Scenario,
) -> None:
    """A client of a user limited to the absorbed player gets its removal, then the kept player."""
    tv = await _two_players_for_one_tv(scenario)
    user = await scenario.mass.webserver.auth.create_user(
        username="guest", player_filter=[tv.younger_id]
    )
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client: Any = WebsocketClientHandler(scenario.mass.webserver, request)
    client._authenticated_user = user
    client._send_message_sync = MagicMock()
    scenario.mass.webserver.register_websocket_client(client)
    client._subscribe_to_events()

    try:
        await _merge(scenario, tv.older_id, tv.younger_id)
    finally:
        client._events_unsub_callback()
        scenario.mass.webserver.unregister_websocket_client(client)

    sent = [
        (message.event, message.object_id)
        for call in client._send_message_sync.call_args_list
        if isinstance(message := call.args[0], MassEvent)
    ]
    removal = sent.index((EventType.PLAYER_REMOVED, tv.younger_id))
    assert (EventType.PLAYER_UPDATED, tv.older_id) in sent[removal:]
    assert client._authenticated_user.player_filter == [tv.older_id]


async def test_merge_is_checked_again_once_the_playback_stopped(scenario: Scenario) -> None:
    """A kept player removed while the absorbed one stops ends the merge before it changes anything."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.play(tv.younger_id)
    queues = scenario.mass.player_queues
    stop = queues.stop

    async def stop_and_remove_the_kept_player(queue_id: str) -> None:
        await stop(queue_id)
        await scenario.mass.players.remove(tv.older_id)

    with (
        patch.object(queues, "stop", new=stop_and_remove_the_kept_player),
        pytest.raises(InvalidDataError),
    ):
        await scenario.mass.players.merge_players(tv.older_id, tv.younger_id)
    await scenario.advance(20)
    await scenario.idle()

    assert await scenario.player_config(tv.younger_id) is not None
    assert scenario.player_of(tv.cast) == tv.younger_id


async def test_merge_and_a_removal_of_the_absorbed_player_announce_it_gone_once(
    scenario: Scenario,
) -> None:
    """A removal of the absorbed player while the merge saves leaves one removal event."""
    tv = await _two_players_for_one_tv(scenario)
    players = scenario.mass.players
    scenario.clear_events()

    merge = asyncio.create_task(players.merge_players(tv.older_id, tv.younger_id))
    removal = asyncio.create_task(players.remove(tv.younger_id))
    await scenario.settle()
    await merge
    await removal
    await scenario.advance(20)
    await scenario.idle()

    assert scenario.events(EventType.PLAYER_REMOVED, object_id=tv.younger_id) == [
        Event(EventType.PLAYER_REMOVED, tv.younger_id)
    ]


async def test_duplicate_that_plays_is_merged_only_once_it_stopped(scenario: Scenario) -> None:
    """A younger duplicate that plays (from a plugin: no queue items) merges at a later attach."""
    tv = await _duplicates(scenario)
    younger = scenario.mass.players.get_player(tv.younger_id)
    assert younger is not None
    younger.set_active_output_protocol("cast_tv")
    await scenario.report(tv.cast, playback_state=PlaybackState.PLAYING)
    if (state := scenario.player(tv.younger_id)) is None or (
        state.playback_state != PlaybackState.PLAYING
    ):
        msg = f"{tv.younger_id} does not play"
        raise ScenarioError(msg)

    await _seen_again_with_its_address(scenario, tv.airplay)
    assert [player.player_id for player in scenario.players_carrying(tv.cast)] == [tv.younger_id]

    await scenario.report(tv.cast, playback_state=PlaybackState.IDLE)
    await scenario.unregister(tv.airplay)
    await scenario.register(tv.airplay)
    await scenario.idle()
    assert [player.player_id for player in scenario.players_carrying(tv.cast)] == [tv.older_id]


async def test_player_commands_take_the_id_of_a_merged_player(scenario: Scenario) -> None:
    """
    A player command called inside the server with a legacy id reaches the player it names.

    Providers that store a target player (the intercept target of a Yandex Station, the
    autostart target of a Sendspin source) call them with the stored id.
    """
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)
    players = scenario.mass.players
    await scenario.play(tv.younger_id)

    await players.cmd_volume_set(tv.younger_id, 30)
    await players.cmd_pause(tv.younger_id)
    await scenario.idle()

    kept = scenario.player(tv.older_id)
    assert kept is not None
    assert (kept.volume_level, kept.playback_state) == (30, PlaybackState.PAUSED)


async def test_playback_commands_by_the_id_of_a_merged_player_check_the_kept_player(
    scenario: Scenario,
) -> None:
    """A playback command by an absorbed id needs access to the kept player, not to that id."""
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)
    await scenario.play(tv.older_id)
    auth = scenario.mass.webserver.auth
    allowed = await auth.create_user(username="allowed", player_filter=[tv.older_id])
    by_old_id = await auth.create_user(username="by_old_id", player_filter=[tv.younger_id])
    players = scenario.mass.players

    try:
        set_current_user(by_old_id)
        with pytest.raises(InsufficientPermissions):
            await players.cmd_pause(tv.younger_id)
        set_current_user(allowed)
        await players.cmd_pause(tv.younger_id)
    finally:
        set_current_user(None)
    await scenario.idle()

    kept = scenario.player(tv.older_id)
    assert kept is not None
    assert kept.playback_state == PlaybackState.PAUSED


async def test_playback_commands_by_the_id_of_a_merged_group_member_check_its_group(
    scenario: Scenario,
) -> None:
    """For a merged member of a group that plays, the command also needs access to the group."""
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)
    sonos = await scenario.add_provider("sonos")
    hall = sonos.protocol("RINCON_HALL", "Hall", identifiers=HALL)
    await scenario.register(hall)
    hall_id = scenario.single_player(hall).player_id
    await scenario.mass.config._create_provider_instance("universal_group", {})
    group = await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", [tv.older_id, hall_id], False
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    auth = scenario.mass.webserver.auth
    member_only = await auth.create_user(username="member_only", player_filter=[tv.older_id])
    with_group = await auth.create_user(
        username="with_group", player_filter=[tv.older_id, group.player_id]
    )
    players = scenario.mass.players

    try:
        set_current_user(member_only)
        with pytest.raises(InsufficientPermissions):
            await players.cmd_pause(tv.younger_id)
        set_current_user(with_group)
        await players.cmd_pause(tv.younger_id)
    finally:
        set_current_user(None)
    await scenario.advance(1)

    # the group played on until the user allowed the group paused it
    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.playback_state != PlaybackState.PLAYING


async def test_remove_unavailable_right_after_a_merge_lists_the_kept_player_once(
    scenario: Scenario,
) -> None:
    """A merged player whose devices are both gone is one leftover; the absorbed one is none."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    await scenario.report(tv.airplay, available=False)
    await scenario.report(tv.cast, available=False)
    assert sorted(await scenario.mass.players.remove_unavailable(dry_run=True)) == sorted(
        [tv.older_id, tv.younger_id]
    )
    await _merge(scenario, tv.older_id, tv.younger_id)

    assert await scenario.mass.players.remove_unavailable(dry_run=True) == [tv.older_id]
    assert await scenario.mass.players.remove_unavailable() == [tv.older_id]
    await scenario.idle()

    stored = scenario.mass.config.get(CONF_PLAYERS, {})
    assert tv.older_id not in stored
    assert tv.younger_id not in stored
    assert await scenario.mass.players.remove_unavailable() == []


async def test_remove_unavailable_counts_the_protocol_a_merge_moved(scenario: Scenario) -> None:
    """A merged player whose own device is gone stays while the moved protocol reaches its device."""
    tv = await _two_players_for_one_tv(scenario)
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    await scenario.report(tv.airplay, available=False)
    assert await scenario.mass.players.remove_unavailable(dry_run=True) == [tv.older_id]
    await _merge(scenario, tv.older_id, tv.younger_id)

    assert await scenario.mass.players.remove_unavailable(dry_run=True) == []
    assert await scenario.mass.players.remove_unavailable() == []
    assert scenario.player_of(tv.cast) == tv.older_id


async def test_duplicate_merged_while_the_native_is_unreachable_plays_at_the_held_level(
    scenario: Scenario,
) -> None:
    """
    The native of the older player can not be reached when its AirPlay duplicate merges in.

    The player keeps the level it had: the moved protocol is never shown louder, and is given
    that level before it plays.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=30)
    audio = airplay.protocol(
        "ap_kitchen", "Kitchen", role="audio", identifiers=TV_AIRPLAY, volume_level=80
    )
    await scenario.register(native)
    await scenario.advance(15)
    await scenario.register(audio)
    await scenario.advance(15)
    await scenario.idle()
    older_id = scenario.single_player(native).player_id
    assert scenario.single_player(audio).player_id != older_id
    await scenario.report(native, available=False)

    await scenario.report(audio, identifiers=KITCHEN)
    await scenario.unregister(audio)
    await scenario.register(audio)
    await scenario.idle()

    assert [player.player_id for player in scenario.players_carrying(native, audio)] == [older_id]
    state = scenario.player(older_id)
    assert state is not None
    assert (state.volume_control, state.volume_level) == ("ap_kitchen", 30)
    audio.commands.clear()

    await scenario.play(older_id)

    assert audio.commands[0] == ("volume_set", 30)
    assert "play_media" in [command[0] for command in audio.commands]


async def test_a_restart_after_a_merge_keeps_every_record(scenario: Scenario) -> None:
    """The cleanup of records an older version stored leaves what a merge stored alone."""
    tv = await _two_players_for_one_tv(scenario)
    await _merge(scenario, tv.older_id, tv.younger_id)

    await _assert_a_restart_keeps_every_record(scenario)

    assert (scenario.mass.config.get_protocol_record("cast_tv") or {}).get(
        "player_id"
    ) == tv.older_id


async def _assert_a_restart_keeps_every_record(scenario: Scenario) -> None:
    """
    Assert that the cleanup of records an older version stored finds nothing to do.

    Run on the stored settings as they are now, and through the restart that runs it.
    """
    config = scenario.mass.config
    stored = copy.deepcopy(config._data)
    checked = copy.deepcopy(stored)
    assert remove_player_records_of_older_versions(checked) is False
    assert checked == stored

    await scenario.restart()
    await scenario.idle()

    assert sorted(config.get(CONF_PLAYERS, {})) == sorted(stored[CONF_PLAYERS])
    assert _edges(config.get(CONF_PLAYER_PROTOCOLS, {})) == _edges(stored[CONF_PLAYER_PROTOCOLS])


def _edges(protocols: dict[str, Any]) -> dict[str, Any]:
    """Return the player each stored protocol names, by protocol id."""
    return {protocol_id: record.get("player_id") for protocol_id, record in protocols.items()}
