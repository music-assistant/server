"""Tests for merging a player into another one: two players that turn out to be one device."""

from __future__ import annotations

from typing import NamedTuple

import pytest
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
)
from music_assistant_models.errors import InvalidCommand, InvalidDataError

from music_assistant.constants import (
    CONF_DEVICE_IDENTIFIERS,
    CONF_GROUP_MEMBERS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
)
from tests.scenarios.harness import (
    DEFAULT_FEATURES,
    FAKE_SETTING,
    Event,
    FakeProtocol,
    Scenario,
    VisiblePlayer,
)

# what the AirPlay and the Cast receiver of one TV report: nothing they share
TV_AIRPLAY = {IdentifierType.AIRPLAY_ID: "C0:FF:EE:00:00:01"}
TV_CAST = {IdentifierType.CAST_UUID: "6f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b"}
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
    await scenario.attach(tv_airplay)
    await scenario.advance(15)
    await scenario.attach(tv_cast)
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


async def test_merged_player_carries_both_protocols_and_answers_to_the_absorbed_id(
    scenario: Scenario,
) -> None:
    """The kept player gets the protocols of the absorbed one, whose id becomes its legacy id."""
    tv = await _two_players_for_one_tv(scenario)
    scenario.clear_events()

    await _merge(scenario, tv.older_id, tv.younger_id)

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


async def test_merge_takes_legacy_ids(scenario: Scenario) -> None:
    """Either player can be named by one of its legacy ids."""
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    tv_dlna = dlna.protocol("uuid:tv", "Living TV", role="audio")
    await scenario.attach(tv_dlna)
    await scenario.advance(15)
    dlna_id = scenario.single_player(tv_dlna).player_id
    await _merge(scenario, dlna_id, tv.younger_id)

    await _merge(scenario, tv.older_id, tv.younger_id)

    assert [player.player_id for player in scenario.players_carrying(tv_dlna)] == [tv.older_id]


async def test_chain_of_merges_keeps_every_absorbed_id_resolving(scenario: Scenario) -> None:
    """A player merged into one that is merged on keeps resolving to the last one."""
    tv = await _two_players_for_one_tv(scenario)
    dlna = await scenario.add_provider("dlna")
    tv_dlna = dlna.protocol("uuid:tv", "Living TV", role="audio")
    await scenario.attach(tv_dlna)
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
    auth = scenario.mass.webserver.auth
    user = await auth.create_user(username="guest", player_filter=[tv.younger_id])
    await scenario.idle()

    await _merge(scenario, tv.older_id, tv.younger_id)

    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.group_members == [tv.older_id]
    group_protocol = scenario.mass.players.get_player(group.player_id)
    assert group_protocol is not None
    assert group_protocol.own_protocol is not None
    stored_group = scenario.protocol_config(group_protocol.own_protocol.protocol_id) or {}
    assert stored_group[CONF_GROUP_MEMBERS] == [tv.older_id]
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
    await scenario.attach(avr_airplay)
    await scenario.advance(15)
    keep_id = scenario.single_player(avr_airplay).player_id
    receiver = musiccast.protocol(
        "avr_1",
        "AVR",
        identifiers=KITCHEN,
        playback_state=PlaybackState.PLAYING,
        active_source="hdmi1",
    )
    await scenario.attach(receiver)
    receiver_id = scenario.single_player(receiver).player_id

    await _merge(scenario, keep_id, receiver_id)

    assert ("stop",) not in receiver.commands
    kept = scenario.player(keep_id)
    assert kept is not None
    assert kept.playback_state == PlaybackState.PLAYING


async def test_merge_of_a_group_member_keeps_it_in_its_group(scenario: Scenario) -> None:
    """A member of a group that plays is merged without leaving it: the kept player is a member."""
    tv = await _two_players_for_one_tv(scenario)
    sonos = await scenario.add_provider("sonos")
    hall = sonos.protocol("RINCON_HALL", "Hall", identifiers=HALL)
    await scenario.attach(hall)
    hall_id = scenario.single_player(hall).player_id
    await scenario.mass.config._create_provider_instance("universal_group", {})
    group = await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", [tv.younger_id, hall_id], False
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    tv.cast.commands.clear()

    await _merge(scenario, tv.older_id, tv.younger_id)

    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.group_members == [tv.older_id, hall_id]
    assert group_state.playback_state == PlaybackState.PLAYING
    assert ("stop",) not in tv.cast.commands


async def test_merge_of_a_sync_leader_keeps_its_members(scenario: Scenario) -> None:
    """A sync leader that is merged away leaves its members synced to the kept player."""
    airplay = await scenario.add_provider("airplay")
    sonos = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    # the AirPlay receiver of the speaker was first seen without its address
    kitchen_airplay = airplay.protocol("ap_kitchen", "Kitchen", role="audio")
    await scenario.attach(kitchen_airplay)
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
    await scenario.attach(leader)
    await scenario.attach(member)
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
    await scenario.detach(tv.cast)

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
    await scenario.attach(scenario.protocol("cast_tv"))

    assert scenario.player_of(scenario.protocol("cast_tv")) == tv.older_id


async def test_two_players_that_each_have_a_native_protocol_are_not_merged(
    scenario: Scenario,
) -> None:
    """Two players that each stand for a device of their own stay as they are."""
    sonos = await scenario.add_provider("sonos")
    bluesound = await scenario.add_provider("bluesound")
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    hall = bluesound.protocol("bluesound_hall", "Hall", identifiers=HALL)
    await scenario.attach(kitchen)
    await scenario.attach(hall)
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
    await scenario.attach(kitchen)
    await scenario.attach(hall)
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
    await scenario.attach(receiver)
    await scenario.advance(15)
    await scenario.attach(speaker)
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
    await scenario.attach(web_player)
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
