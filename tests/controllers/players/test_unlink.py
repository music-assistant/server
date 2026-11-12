"""Tests for taking a protocol off its player: a protocol that ended up on the wrong player."""

from __future__ import annotations

import re
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage, ErrorResultMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import EventType, IdentifierType, PlayerType
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
)

from music_assistant.constants import CONF_PREFERRED_OUTPUT_PROTOCOL, CONF_UNLINKED_FROM
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from tests.scenarios.harness import Event, FakeProtocol, Scenario, VisiblePlayer

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
    await scenario.attach(speaker)
    await scenario.attach(receiver)
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

    await scenario.detach(match.receiver)
    await scenario.attach(match.receiver)
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
    await scenario.attach(studio_dlna)
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
    await scenario.detach(match.receiver)

    new_id = await _unlink(scenario, "ap_studio")

    new_player = scenario.player(new_id)
    assert new_player is not None
    assert not new_player.available
    await scenario.attach(match.receiver)
    assert scenario.player_of(match.receiver) == new_id


async def test_protocols_riding_on_an_unlinked_one_move_with_it(scenario: Scenario) -> None:
    """A bridge that rides on the protocol follows it to its new player."""
    match = await _wrong_match(scenario)
    sendspin = await scenario.add_provider("sendspin")
    bridge = sendspin.protocol(
        "spb_studio", "Studio", role="audio", underlying_protocol_id="ap_studio"
    )
    await scenario.attach(bridge)
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
    await scenario.attach(speaker)

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("RINCON_KITCHEN")

    assert refused.value.translation_key == "unlink_only_protocol"


async def test_protocol_of_a_group_is_not_unlinked(scenario: Scenario) -> None:
    """The protocol of a group stands for the group itself."""
    chromecast = await scenario.add_provider("chromecast")
    group = chromecast.protocol("cast_group", "Group Z", role="virtual", type=PlayerType.GROUP)
    await scenario.attach(group)

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("cast_group")

    assert refused.value.translation_key == "unlink_group"


async def test_protocol_that_only_exists_while_connected_is_not_unlinked(
    scenario: Scenario,
) -> None:
    """A browser player has no record to keep a player of its own in."""
    sendspin = await scenario.add_provider("sendspin")
    web_player = sendspin.protocol("sendspin_web_1", "Browser", role="audio", session_only=True)
    await scenario.attach(web_player)

    with pytest.raises(InvalidCommand) as refused:
        await scenario.mass.players.unlink_protocol("sendspin_web_1")

    assert refused.value.translation_key == "unlink_session_only"


async def test_unknown_protocol_is_not_unlinked(scenario: Scenario) -> None:
    """An id no protocol has is refused."""
    with pytest.raises(InvalidDataError):
        await scenario.mass.players.unlink_protocol("ap_unknown")


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
