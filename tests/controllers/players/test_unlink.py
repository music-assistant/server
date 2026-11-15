"""Tests for taking a protocol off its player: a protocol that ended up on the wrong player."""

from __future__ import annotations

import copy
import re
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage, ErrorResultMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
)

from music_assistant.constants import (
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_UNLINKED_FROM,
)
from music_assistant.controllers.config.player_split import (
    remove_player_records_of_older_versions,
)
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from tests.scenarios.harness import (
    DEFAULT_FEATURES,
    Event,
    FakeProtocol,
    Scenario,
    VisiblePlayer,
)

KITCHEN_IP = "192.168.1.20"
KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0", IdentifierType.IP_ADDRESS: KITCHEN_IP}


class _WrongMatch(NamedTuple):
    """A speaker, and the AirPlay receiver of another device on its host that joined it."""

    speaker: FakeProtocol
    receiver: FakeProtocol
    player_id: str


async def _wrong_match(scenario: Scenario) -> _WrongMatch:
    """Let an AirPlay receiver that only reports the address of its host join a speaker."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    receiver = airplay.protocol(
        "ap_studio", "Studio", role="audio", identifiers={IdentifierType.IP_ADDRESS: KITCHEN_IP}
    )
    await scenario.register(speaker)
    await scenario.register(receiver)
    await scenario.advance(15)
    await scenario.idle()
    player_id = scenario.single_player(speaker, receiver).player_id
    return _WrongMatch(speaker, receiver, player_id)


async def _unlink(scenario: Scenario, protocol_id: str) -> str:
    """Take a protocol off its player and let the server settle."""
    new_id = await scenario.mass.players.unlink_protocol(protocol_id)
    await scenario.idle()
    return new_id


async def test_unlinked_protocol_gets_a_player_of_its_own(scenario: Scenario) -> None:
    """The protocol leaves its player for a new one; the player keeps everything else."""
    match = await _wrong_match(scenario)
    await scenario.configure_player(
        match.player_id, **{CONF_PREFERRED_OUTPUT_PROTOCOL: "ap_studio"}
    )
    scenario.clear_events()

    new_id = await scenario.mass.players.unlink_protocol("ap_studio")
    await scenario.settle()

    # the player no longer lists the protocol, at once
    assert [player.outputs for player in scenario.players_carrying(match.speaker)] == [
        (("sonos", True),)
    ]
    await scenario.idle()
    assert re.fullmatch(r"pl[0-9a-f]{12}", new_id)
    assert scenario.player_of(match.receiver) == new_id
    assert scenario.players_carrying(match.receiver) == [
        VisiblePlayer(
            player_id=new_id,
            name="Studio",
            available=True,
            powered=None,
            provider="airplay",
            outputs=(("airplay", True),),
        )
    ]
    assert scenario.players_carrying(match.speaker) == [
        VisiblePlayer(
            player_id=match.player_id,
            name="Kitchen",
            available=True,
            powered=None,
            provider="sonos",
            outputs=(("sonos", True),),
        )
    ]
    assert scenario.events(EventType.PLAYER_ADDED, EventType.QUEUE_ADDED) == [
        Event(EventType.PLAYER_ADDED, new_id),
        Event(EventType.QUEUE_ADDED, new_id),
    ]
    # the choice that named the protocol falls back to its default
    assert (
        scenario.mass.config.get_player_value(match.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL)
        is None
    )


async def test_unlinked_protocol_stays_on_its_player(scenario: Scenario) -> None:
    """Found again, also after a restart, the protocol attaches to its new player."""
    match = await _wrong_match(scenario)
    new_id = await _unlink(scenario, "ap_studio")

    await scenario.unregister(match.receiver)
    await scenario.register(match.receiver)
    await scenario.idle()
    assert scenario.player_of(match.receiver) == new_id

    await scenario.restart()
    await scenario.idle()
    assert scenario.player_of(scenario.protocol("ap_studio")) == new_id
    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == match.player_id


async def test_unlink_then_merge_back_puts_the_protocol_back(scenario: Scenario) -> None:
    """A merge the user asks for puts the protocol back, and forgets it was taken off."""
    match = await _wrong_match(scenario)
    new_id = await _unlink(scenario, "ap_studio")

    await scenario.mass.players.merge_players(match.player_id, new_id)
    await scenario.idle()

    assert scenario.player_of(match.receiver) == match.player_id
    assert CONF_UNLINKED_FROM not in (scenario.protocol_config("ap_studio") or {})


async def test_merge_with_a_third_player_keeps_the_protocol_off_its_old_player(
    scenario: Scenario,
) -> None:
    """Only a merge with the player it was taken off makes a protocol forget that."""
    match = await _wrong_match(scenario)
    new_id = await _unlink(scenario, "ap_studio")
    dlna = await scenario.add_provider("dlna")
    studio_dlna = dlna.protocol("uuid:studio", "Studio", role="audio")
    await scenario.register(studio_dlna)
    await scenario.advance(15)
    await scenario.idle()

    await scenario.mass.players.merge_players(new_id, scenario.single_player(studio_dlna).player_id)
    await scenario.idle()

    assert (scenario.protocol_config("ap_studio") or {})[CONF_UNLINKED_FROM] == match.player_id


async def test_unlinked_native_protocol_leaves_a_player_of_the_others(scenario: Scenario) -> None:
    """
    A player whose native protocol is taken off keeps its id with its other protocols.

    Without a custom name it is named by its other protocols then.
    """
    match = await _wrong_match(scenario)

    new_id = await _unlink(scenario, "RINCON_KITCHEN")

    assert scenario.player_of(match.speaker) == new_id
    assert scenario.players_carrying(match.receiver) == [
        VisiblePlayer(
            player_id=match.player_id,
            name="Studio",
            available=True,
            powered=None,
            provider="airplay",
            outputs=(("airplay", True),),
        )
    ]


async def test_protocol_that_is_away_is_unlinked_to_a_player_that_waits_for_it(
    scenario: Scenario,
) -> None:
    """A protocol whose device is away gets a player that is unavailable until it returns."""
    match = await _wrong_match(scenario)
    await scenario.unregister(match.receiver)

    new_id = await _unlink(scenario, "ap_studio")

    new_player = scenario.player(new_id)
    assert new_player is not None
    assert not new_player.available
    await scenario.register(match.receiver)
    assert scenario.player_of(match.receiver) == new_id


async def test_protocols_riding_on_an_unlinked_one_move_with_it(scenario: Scenario) -> None:
    """A bridge that rides on the protocol follows it to its new player."""
    match = await _wrong_match(scenario)
    sendspin = await scenario.add_provider("sendspin")
    bridge = sendspin.protocol(
        "spb_studio", "Studio", role="audio", underlying_protocol_id="ap_studio"
    )
    await scenario.register(bridge)
    assert scenario.player_of(bridge) == match.player_id

    new_id = await _unlink(scenario, "ap_studio")

    assert scenario.player_of(bridge) == new_id

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("spb_studio")
    assert refused.value.translation_key == "unlink_derived"


async def test_only_protocol_of_a_player_is_not_unlinked(scenario: Scenario) -> None:
    """A protocol that is all of its player is a player of its own already."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("RINCON_KITCHEN")

    assert refused.value.translation_key == "unlink_only_protocol"


async def test_protocol_of_a_group_is_not_unlinked(scenario: Scenario) -> None:
    """The protocol of a group stands for the group itself."""
    chromecast = await scenario.add_provider("chromecast")
    group = chromecast.protocol("cast_group", "Group Z", role="virtual", type=PlayerType.GROUP)
    await scenario.register(group)

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("cast_group")

    assert refused.value.translation_key == "unlink_group"


async def test_protocol_that_only_exists_while_connected_is_not_unlinked(
    scenario: Scenario,
) -> None:
    """A browser player has no record to keep a player of its own in."""
    sendspin = await scenario.add_provider("sendspin")
    web_player = sendspin.protocol("sendspin_web_1", "Browser", role="audio", session_only=True)
    await scenario.register(web_player)

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("sendspin_web_1")

    assert refused.value.translation_key == "unlink_session_only"


async def test_unknown_protocol_is_not_unlinked(scenario: Scenario) -> None:
    """An id no protocol has is refused."""
    with pytest.raises(InvalidDataError):
        await scenario.mass.players.unlink_protocol("ap_unknown")


async def test_protocol_that_belongs_to_no_player_is_not_unlinked(scenario: Scenario) -> None:
    """A stored protocol that waits to be matched to a player has no player to leave."""
    scenario.mass.config.create_protocol_record(
        "ap_unbound", provider="airplay", player_id=None, role=ProtocolRole.AUDIO, default_name="X"
    )

    with pytest.raises(InvalidDataError, match="belongs to no player"):
        await scenario.mass.players.unlink_protocol("ap_unbound")


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


async def test_unlink_command_needs_the_player_config_scope(scenario: Scenario) -> None:
    """A user without the scope to configure players can not unlink a protocol."""
    match = await _wrong_match(scenario)
    user = await scenario.mass.webserver.auth.create_user(username="member", role=UserRole.USER)

    error = await _call_as(scenario, user, "players/unlink_protocol", protocol_id="ap_studio")

    assert error == InsufficientPermissions.error_code
    assert scenario.player_of(match.receiver) == match.player_id


async def test_unlink_command_follows_the_policy_of_removing_a_player(scenario: Scenario) -> None:
    """With the scope to configure players, a player filter does not limit an unlink."""
    match = await _wrong_match(scenario)
    user = await scenario.mass.webserver.auth.create_user(
        username="service", role=UserRole.SERVICE, player_filter=["pl_other"]
    )

    error = await _call_as(scenario, user, "players/unlink_protocol", protocol_id="ap_studio")

    assert error is None
    assert scenario.player_of(match.receiver) != match.player_id


async def test_protocol_taken_off_a_player_is_not_merged_back_through_an_older_player(
    scenario: Scenario,
) -> None:
    """
    A player that holds an unlinked protocol never merges with the player it was taken off.

    Also when the user merged it into a player that is older than that one, so that the
    player it was taken off is the younger of the two.
    """
    chromecast = await scenario.add_provider("chromecast")
    # an older player of the device the AirPlay receiver belongs to, first seen without identifiers
    studio_cast = chromecast.protocol("cast_studio", "Studio", role="audio")
    await scenario.register(studio_cast)
    await scenario.advance(15)
    studio_id = scenario.single_player(studio_cast).player_id
    match = await _wrong_match(scenario)
    new_id = await _unlink(scenario, "ap_studio")
    await scenario.mass.players.merge_players(studio_id, new_id)
    await scenario.idle()

    # either of the two may be the one whose protocol attaches again
    for protocol in (match.receiver, match.speaker):
        await scenario.unregister(protocol)
        await scenario.register(protocol)
        await scenario.idle()

        assert scenario.player_of(match.receiver) == studio_id
        assert scenario.player_of(match.speaker) == match.player_id


async def test_protocol_taken_off_a_player_is_not_merged_back_into_the_player_that_took_it_in(
    scenario: Scenario,
) -> None:
    """The player a protocol was taken off is followed into the player it is merged into."""
    chromecast = await scenario.add_provider("chromecast")
    dlna = await scenario.add_provider("dlna")
    studio_cast = chromecast.protocol("cast_studio", "Studio", role="audio")
    await scenario.register(studio_cast)
    await scenario.advance(15)
    studio_id = scenario.single_player(studio_cast).player_id
    # a renderer of the speaker, first seen without identifiers
    kitchen_renderer = dlna.protocol("uuid:kitchen", "Kitchen", role="audio")
    await scenario.register(kitchen_renderer)
    await scenario.advance(15)
    kitchen_id = scenario.single_player(kitchen_renderer).player_id
    match = await _wrong_match(scenario)
    new_id = await _unlink(scenario, "ap_studio")
    await scenario.mass.players.merge_players(studio_id, new_id)
    # the speaker goes into the player of its renderer, which answers to its id from then on
    await scenario.mass.players.merge_players(kitchen_id, match.player_id)
    await scenario.idle()

    await scenario.unregister(match.receiver)
    await scenario.register(match.receiver)
    await scenario.idle()

    assert scenario.player_of(match.receiver) == studio_id
    assert scenario.player_of(match.speaker) == kitchen_id


async def test_protocol_of_a_disabled_player_comes_back_on_its_new_player(
    scenario: Scenario,
) -> None:
    """A protocol taken off a disabled player attaches to its new player, without a restart."""
    match = await _wrong_match(scenario)
    await scenario.disable(match.player_id)

    new_id = await _unlink(scenario, "ap_studio")

    assert scenario.player_of(match.receiver) == new_id
    new_player = scenario.player(new_id)
    assert new_player is not None
    assert new_player.available


@pytest.mark.parametrize("domain", ["airplay", "sendspin"])
async def test_a_restart_after_an_unlink_keeps_every_record(
    scenario: Scenario, domain: str
) -> None:
    """The cleanup of older versions leaves both players of an unlink alone, a Sendspin one too."""
    sonos = await scenario.add_provider("sonos")
    other = await scenario.add_provider(domain)
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    receiver = other.protocol(
        f"{domain}_studio",
        "Studio",
        role="audio",
        identifiers={IdentifierType.IP_ADDRESS: KITCHEN_IP},
    )
    await scenario.register(speaker)
    await scenario.register(receiver)
    await scenario.advance(15)
    await scenario.idle()
    old_id = scenario.single_player(speaker, receiver).player_id

    new_id = await _unlink(scenario, receiver.protocol_id)

    assert (scenario.mass.config.get(f"{CONF_PLAYERS}/{new_id}") or {}).get("provider") == domain
    await _assert_a_restart_keeps_every_record(scenario)
    assert {old_id, new_id} <= set(scenario.mass.config.get(CONF_PLAYERS, {}))


async def test_a_restart_after_an_unlink_keeps_a_player_from_before_the_split(
    scenario: Scenario,
) -> None:
    """The own protocol of a player from before the split is taken off it: both players stay."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    airplay.protocol(
        "ap_studio", "Studio", role="audio", identifiers={IdentifierType.IP_ADDRESS: KITCHEN_IP}
    )
    scenario.set_present("RINCON_KITCHEN", True)
    scenario.set_present("ap_studio", True)
    await scenario.seed_pre_split_config(
        {
            "RINCON_KITCHEN": {
                "player_id": "RINCON_KITCHEN",
                "provider": "sonos",
                "player_type": "player",
                "enabled": True,
                "default_name": "Kitchen",
                "values": {},
            },
            "ap_studio": {
                "player_id": "ap_studio",
                "provider": "airplay",
                "player_type": "protocol",
                "enabled": True,
                "default_name": "Studio",
                "values": {"protocol_parent_id": "RINCON_KITCHEN"},
            },
        }
    )
    await scenario.idle()
    assert scenario.player_of(scenario.protocol("ap_studio")) == "RINCON_KITCHEN"

    new_id = await _unlink(scenario, "RINCON_KITCHEN")

    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == new_id
    await _assert_a_restart_keeps_every_record(scenario)
    assert {"RINCON_KITCHEN", new_id} <= set(scenario.mass.config.get(CONF_PLAYERS, {}))


async def test_a_restart_after_an_unlink_and_a_merge_into_a_third_player_keeps_every_record(
    scenario: Scenario,
) -> None:
    """The player an unlink gave a protocol is merged into a third one: what stays, stays."""
    match = await _wrong_match(scenario)
    new_id = await _unlink(scenario, "ap_studio")
    dlna = await scenario.add_provider("dlna")
    studio_dlna = dlna.protocol("uuid:studio", "Studio", role="audio")
    await scenario.register(studio_dlna)
    await scenario.advance(15)
    await scenario.idle()
    third_id = scenario.single_player(studio_dlna).player_id

    await scenario.mass.players.merge_players(third_id, new_id)
    await scenario.idle()

    assert (scenario.mass.config.get_protocol_record("ap_studio") or {}).get(
        "player_id"
    ) == third_id
    await _assert_a_restart_keeps_every_record(scenario)
    assert {match.player_id, third_id} <= set(scenario.mass.config.get(CONF_PLAYERS, {}))


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


@pytest.mark.parametrize("group_type", ["universal_group", "sync_group"])
async def test_group_keeps_the_player_that_shares_its_id_with_a_protocol_taken_off_it(
    scenario: Scenario, group_type: str
) -> None:
    """
    A group keeps naming the player from before the split once its own protocol was taken off.

    That protocol keeps the id it shares with its old player, and is on a player of its own
    from then on: the group still names the old player, also after a restart.
    """
    if group_type == "universal_group":
        await scenario.mass.config._create_provider_instance(group_type, {})
    hall_id = await _pre_split_living_room_and_hall(scenario)
    group = await scenario.mass.players.create_group_player(
        group_type, "Group Z", ["wiim_living", hall_id], False
    )
    await scenario.idle()

    new_id = await _unlink(scenario, "wiim_living")

    players = scenario.mass.players
    living = players.get_player_or_protocol_player("wiim_living")
    assert living is not None
    assert living.player_id == "wiim_living"
    new_player = players.get_player_by_protocol("wiim_living")
    assert new_player is not None
    assert new_player.player_id == new_id
    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.group_members == ["wiim_living", hall_id]
    await scenario.restart()
    await scenario.idle()
    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.group_members == ["wiim_living", hall_id]


@pytest.mark.parametrize("group_type", ["universal_group", "sync_group"])
async def test_group_does_not_take_another_player_for_its_disabled_member(
    scenario: Scenario, group_type: str
) -> None:
    """
    A disabled member is not swapped for the player of a protocol that has its id.

    The living room player from before the split has the id of its own protocol, which was taken
    off it. Disabled, the player has no live object but keeps its stored record, so its id still
    names it and never the new player of that protocol. An id that only a protocol has still
    names the player of that protocol.
    """
    if group_type == "universal_group":
        await scenario.mass.config._create_provider_instance(group_type, {})
    hall_id = await _pre_split_living_room_and_hall(scenario)
    group = await scenario.mass.players.create_group_player(
        group_type, "Group Z", ["wiim_living", hall_id], False
    )
    await scenario.idle()
    new_id = await _unlink(scenario, "wiim_living")

    await scenario.disable("wiim_living")
    await scenario.idle()

    players = scenario.mass.players
    assert players.get_player_or_protocol_player("wiim_living") is None
    hall = players.get_player_or_protocol_player("RINCON_HALL")
    assert hall is not None
    assert hall.player_id == hall_id
    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert new_id not in group_state.group_members


async def _pre_split_living_room_and_hall(scenario: Scenario) -> str:
    """
    Return the id of the hall player, next to the living room player from before the split.

    The living room player has the id of its own protocol (`wiim_living`) and an AirPlay
    output; the two can be grouped.

    :param scenario: The running scenario.
    """
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    sonos = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    living = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A5"}
    wiim.protocol(
        "wiim_living",
        "Living",
        identifiers=living,
        supported_features=features,
        can_group_with={"RINCON_HALL"},
    )
    airplay.protocol("airplay_living", "Living", role="audio", identifiers=living)
    sonos.protocol(
        "RINCON_HALL",
        "Hall",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B5"},
        supported_features=features,
        can_group_with={"wiim_living"},
    )
    for protocol_id in ("wiim_living", "airplay_living", "RINCON_HALL"):
        scenario.set_present(protocol_id, True)
    await scenario.seed_pre_split_config(
        {
            "wiim_living": {
                "player_id": "wiim_living",
                "provider": "wiim",
                "player_type": "player",
                "enabled": True,
                "name": "Living Room",
                "default_name": "Living",
                "values": {"linked_protocol_ids": ["airplay_living"]},
            },
            "airplay_living": {
                "player_id": "airplay_living",
                "provider": "airplay",
                "player_type": "protocol",
                "enabled": True,
                "default_name": "Living",
                "values": {"protocol_parent_id": "wiim_living"},
            },
        }
    )
    await scenario.idle()
    return scenario.single_player(scenario.protocol("RINCON_HALL")).player_id
