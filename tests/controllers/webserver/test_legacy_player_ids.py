"""
Tests for reaching a player by a legacy id through the API.

A real server with two players, one of which holds a legacy id, takes its commands through
the websocket and the HTTP endpoint the way a client sends them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.constants import CONF_LEGACY_IDS, CONF_PLAYERS
from music_assistant.controllers.webserver import controller as webserver_controller
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.json import json_dumps
from music_assistant.providers.party import (
    CONF_ENABLE_GUEST_ACCESS,
    CONF_ENABLE_SKIP_SONG,
    CONF_PARTY_PLAYER,
    CONF_PARTY_SKIP_VOTES_REQUIRED,
)
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator, Coroutine

KITCHEN = "sonos_kitchen"
BEDROOM = "sonos_bedroom"
# an id the kitchen player was known under before
LEGACY_ID = "sonos_old_kitchen"

type Transport = Literal["websocket", "http"]

TRANSPORTS = pytest.mark.parametrize("transport", ["websocket", "http"])


@dataclass
class _Reply:
    """What the server answered to a command."""

    result: Any
    # whether the command was refused for a lack of permissions
    refused: bool


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with a kitchen and a bedroom player; the kitchen holds a legacy id.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        sonos = await scenario.add_provider("sonos")
        await scenario.attach(sonos.protocol(KITCHEN, "Kitchen"))
        await scenario.attach(sonos.protocol(BEDROOM, "Bedroom"))
        await scenario.seed_config(
            {CONF_PLAYERS: {KITCHEN: {"values": {CONF_LEGACY_IDS: [LEGACY_ID]}}}}
        )
        yield scenario
    finally:
        await scenario.close()


async def _user(scenario: Scenario, *player_filter: str, role: UserRole = UserRole.USER) -> User:
    """
    Create a user on the server of the scenario.

    :param scenario: The scenario to create the user in.
    :param player_filter: The players the user is limited to, none for every player.
    :param role: The role of the user.
    """
    return await scenario.mass.webserver.auth.create_user(
        username="_".join((role.value, *player_filter)),
        role=role,
        player_filter=list(player_filter),
    )


async def _run[T](scenario: Scenario, operation: Coroutine[Any, Any, T]) -> T:
    """
    Run an operation on the server of the scenario to its end.

    :param scenario: The scenario whose server runs the operation.
    :param operation: The operation, which must not wait for the clock.
    """
    task = await scenario.command(operation)
    assert task.done()
    return await task


async def _send(
    scenario: Scenario, transport: Transport, user: User, command: str, **args: Any
) -> _Reply:
    """
    Send a command to the server as the given user and return its answer.

    :param scenario: The scenario whose server takes the command.
    :param transport: The API transport to send the command through.
    :param user: The user the connection is signed in as.
    :param command: The command to send.
    :param args: The arguments of the command.
    """
    message = {"message_id": "1", "command": command, "args": args}
    webserver = scenario.mass.webserver
    if transport == "websocket":
        client = WebsocketClientHandler(
            webserver, make_mocked_request("GET", "/ws", app=web.Application())
        )
        client._authenticated_user = user
        await _run(scenario, client._handle_command(CommandMessage.from_dict(message)))
        answer = json.loads(client._to_write.get_nowait() or "")
        refused = answer.get("error_code") == InsufficientPermissions.error_code
        assert refused or "error_code" not in answer, answer
        return _Reply(answer.get("result"), refused)
    payload = StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(json_dumps(message).encode())
    payload.feed_eof()
    request = make_mocked_request("POST", "/api", payload=payload, app=web.Application())
    with patch.object(webserver_controller, "get_authenticated_user", AsyncMock(return_value=user)):
        response = await _run(scenario, webserver._handle_jsonrpc_api_command(request))
    assert response.status in (200, 403), response.text
    assert response.text is not None
    refused = response.status == 403
    return _Reply(None if refused else json.loads(response.text), refused)


def _queue_items(scenario: Scenario) -> dict[str, int]:
    """Return the number of items in the queue of each player."""
    return {
        queue_id: queue.items
        for queue_id in (KITCHEN, BEDROOM)
        if (queue := scenario.queue(queue_id)) is not None
    }


@TRANSPORTS
async def test_player_is_read_by_its_legacy_id(scenario: Scenario, transport: Transport) -> None:
    """A user limited to a player reads it by its legacy id."""
    user = await _user(scenario, KITCHEN)

    reply = await _send(scenario, transport, user, "players/get", player_id=LEGACY_ID)

    assert not reply.refused
    assert reply.result["player_id"] == KITCHEN
    assert reply.result["name"] == "Kitchen"


@TRANSPORTS
async def test_queue_is_transferred_from_a_legacy_id(
    scenario: Scenario, transport: Transport
) -> None:
    """A user limited to both players moves the queue of the one named by its legacy id."""
    user = await _user(scenario, KITCHEN, BEDROOM)
    await scenario.play(KITCHEN)

    reply = await _send(
        scenario,
        transport,
        user,
        "player_queues/transfer",
        source_queue_id=LEGACY_ID,
        target_queue_id=BEDROOM,
        auto_play=False,
    )

    assert not reply.refused
    assert _queue_items(scenario) == {KITCHEN: 0, BEDROOM: 1}


@TRANSPORTS
async def test_transfer_from_a_legacy_id_needs_access_to_its_player(
    scenario: Scenario, transport: Transport
) -> None:
    """A user without access to a player cannot reach it by its legacy id either."""
    user = await _user(scenario, BEDROOM)
    await scenario.play(KITCHEN)
    scenario.expect_log(f"does not have access to player {KITCHEN}")

    reply = await _send(
        scenario,
        transport,
        user,
        "player_queues/transfer",
        source_queue_id=LEGACY_ID,
        target_queue_id=BEDROOM,
        auto_play=False,
    )

    assert reply.refused
    assert _queue_items(scenario) == {KITCHEN: 1, BEDROOM: 0}


@TRANSPORTS
async def test_queue_selected_as_source_by_a_legacy_id_is_active_under_its_own_id(
    scenario: Scenario, transport: Transport
) -> None:
    """A player whose queue is selected as source by a legacy id plays from that queue."""
    user = await _user(scenario, KITCHEN)

    reply = await _send(
        scenario, transport, user, "players/cmd/select_source", player_id=KITCHEN, source=LEGACY_ID
    )

    assert not reply.refused
    kitchen = scenario.player(KITCHEN)
    assert kitchen is not None
    assert kitchen.active_source == KITCHEN


@TRANSPORTS
async def test_playing_queue_selected_again_by_a_legacy_id_keeps_playing(
    scenario: Scenario, transport: Transport
) -> None:
    """Selecting the queue a player already plays, by a legacy id, leaves the playback alone."""
    user = await _user(scenario, KITCHEN)
    await scenario.play(KITCHEN)

    reply = await _send(
        scenario, transport, user, "players/cmd/select_source", player_id=KITCHEN, source=LEGACY_ID
    )

    assert not reply.refused
    kitchen = scenario.player(KITCHEN)
    assert kitchen is not None
    assert kitchen.playback_state == PlaybackState.PLAYING


@TRANSPORTS
async def test_command_aimed_at_the_playing_queue_by_a_legacy_id_reaches_it(
    scenario: Scenario, transport: Transport
) -> None:
    """A command for the source a player plays takes the queue under a legacy id as well."""
    user = await _user(scenario, KITCHEN)
    await scenario.play(KITCHEN)

    reply = await _send(
        scenario,
        transport,
        user,
        "players/cmd/shuffle",
        player_id=KITCHEN,
        shuffle_enabled=True,
        source_id=LEGACY_ID,
    )

    assert not reply.refused
    queue = scenario.queue(KITCHEN)
    assert queue is not None
    assert queue.shuffle_enabled


@TRANSPORTS
async def test_user_is_limited_to_the_player_behind_a_legacy_id(
    scenario: Scenario, transport: Transport
) -> None:
    """A player filter given with a legacy id is stored with the id of the player itself."""
    admin = await _user(scenario, role=UserRole.ADMIN)
    user = await _user(scenario, BEDROOM)

    reply = await _send(
        scenario,
        transport,
        admin,
        "auth/user/update",
        user_id=user.user_id,
        player_filter=[LEGACY_ID, BEDROOM],
    )

    assert reply.result["player_filter"] == [KITCHEN, BEDROOM]
    stored = await scenario.mass.webserver.auth.get_user(user.user_id)
    assert stored is not None
    assert stored.player_filter == [KITCHEN, BEDROOM]


@TRANSPORTS
async def test_party_player_set_by_a_legacy_id_takes_votes_of_a_user_of_its_player(
    scenario: Scenario, transport: Transport
) -> None:
    """A user limited to a player votes on the party it hosts, set up with a legacy id."""
    user = await _user(scenario, KITCHEN)
    await scenario.play(KITCHEN)
    await scenario.mass.config._create_provider_instance(
        "party",
        {
            CONF_ENABLE_GUEST_ACCESS: True,
            CONF_ENABLE_SKIP_SONG: True,
            CONF_PARTY_PLAYER: LEGACY_ID,
            # a single vote is counted without skipping the track
            CONF_PARTY_SKIP_VOTES_REQUIRED: 2,
        },
    )
    await scenario.settle()

    reply = await _send(scenario, transport, user, "party/skip")

    assert not reply.refused
    assert reply.result["vote_counted"]
