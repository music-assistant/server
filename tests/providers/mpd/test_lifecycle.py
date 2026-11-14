"""Tests for the configured MPD servers on a running server: identity, removal, discovery."""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.mpd.provider import CONF_MANUAL_IPS

from .conftest import (
    FIRST,
    HOST,
    SECOND,
    FakeMPDClient,
    add_provider,
    load_provider,
    reconfigure,
)

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.providers.mpd.player import MPDPlayer
    from music_assistant.providers.mpd.provider import MPDPlayerProvider
    from tests.scenarios.harness import Scenario

BOTH = [HOST, f"{HOST}:6601"]


def _protocol(provider: MPDPlayerProvider, protocol_id: str) -> MPDPlayer:
    """Return the attached protocol of a server."""
    protocol = provider.get_protocol(protocol_id)
    assert protocol is not None
    return cast("MPDPlayer", protocol)


def _player(provider: MPDPlayerProvider, protocol_id: str) -> Player:
    """Return the player a server is attached to."""
    player = _protocol(provider, protocol_id).player
    assert player is not None
    return player


def _connected(protocol: MPDPlayer) -> bool:
    """Return whether the protocol holds a connection to its server."""
    return protocol._client is not None


def _hosts(scenario: Scenario, provider: MPDPlayerProvider) -> Any:
    """Return the hosts the provider is set up with, as stored."""
    return scenario.mass.config.get_raw_provider_config_value(provider.instance_id, CONF_MANUAL_IPS)


async def test_a_second_server_on_the_host_gets_a_player_of_its_own(scenario: Scenario) -> None:
    """A server added while its sibling on the host is offline does not take its player."""
    provider = await add_provider(scenario, [HOST])
    first_player = _player(provider, FIRST).player_id
    await scenario.configure_player(first_player, name="MPD one")

    # the new server comes first in the list, so it attaches while the first one is offline
    provider = await reconfigure(scenario, provider, [f"{HOST}:6601", HOST])

    second_player = _player(provider, SECOND).player_id
    assert second_player != first_player
    assert second_player.startswith(PLAYER_ID_PREFIX)
    assert _player(provider, FIRST).player_id == first_player
    assert _player(provider, FIRST).display_name == "MPD one"
    assert len(scenario.visible_players()) == 2


async def test_each_server_finds_its_own_player_after_a_restart(scenario: Scenario) -> None:
    """After a restart the servers of one host come back in any order, each on its own player."""
    provider = await add_provider(scenario, BOTH)
    first_player = _player(provider, FIRST).player_id
    second_player = _player(provider, SECOND).player_id
    assert first_player != second_player
    await scenario.configure_player(first_player, name="MPD one")

    await scenario.restart()
    scenario.mass.config.set_raw_provider_config_value(
        provider.instance_id, CONF_MANUAL_IPS, list(reversed(BOTH))
    )
    provider = await load_provider(scenario, provider.instance_id)

    assert _player(provider, FIRST).player_id == first_player
    assert _player(provider, SECOND).player_id == second_player
    assert _player(provider, FIRST).display_name == "MPD one"


async def test_a_server_disconnects_at_every_detach(scenario: Scenario) -> None:
    """The connection is closed when the player is disabled, also after it was enabled again."""
    provider = await add_provider(scenario, [HOST])
    protocol = _protocol(provider, FIRST)
    player_id = _player(provider, FIRST).player_id
    assert _connected(protocol)

    await scenario.disable(player_id)
    assert not _connected(protocol)

    await scenario.enable(player_id)
    await scenario.idle()
    assert provider.get_protocol(FIRST) is protocol
    assert _connected(protocol)

    await scenario.disable(player_id)
    assert not _connected(protocol)


async def test_a_detach_during_the_connect_leaves_no_connection(scenario: Scenario) -> None:
    """A server detached while its attach still connects holds no connection or idle loop."""
    provider = await add_provider(scenario, [HOST])
    protocol = _protocol(provider, FIRST)
    await scenario.mass.players.unregister(FIRST)
    await scenario.settle()
    connecting = asyncio.Event()
    release = asyncio.Event()

    async def _held_connect(_client: FakeMPDClient, _host: str, _port: int) -> None:
        connecting.set()
        await release.wait()

    with patch.object(FakeMPDClient, "connect", _held_connect):
        task = await scenario.command(provider.discover_players())
        assert connecting.is_set()
        await scenario.mass.players.unregister(FIRST)
        release.set()
        await scenario.settle()
        await task

    assert protocol.player is None
    assert not _connected(protocol)
    assert protocol._idle_task is None
    assert protocol._on_detach_callbacks == []


async def test_discovery_leaves_an_attached_server_alone(scenario: Scenario) -> None:
    """Discovering again attaches only the server without an attached protocol."""
    provider = await add_provider(scenario, BOTH)
    protocol = provider.get_protocol(FIRST)
    await scenario.disable(_player(provider, SECOND).player_id)
    attach = scenario.mass.players.register

    with patch.object(scenario.mass.players, "register", wraps=attach) as attached:
        await provider.discover_players()
        await scenario.settle()

    assert [call.args[0].protocol_id for call in attached.call_args_list] == [SECOND]
    assert provider.get_protocol(FIRST) is protocol


async def test_a_removed_server_is_forgotten(scenario: Scenario) -> None:
    """A removed player leaves the configured hosts and does not come back."""
    provider = await add_provider(scenario, BOTH)
    player_id = _player(provider, FIRST).player_id

    await scenario.remove(player_id)
    await scenario.idle()

    assert _hosts(scenario, provider) == [f"{HOST}:6601"]
    assert provider.get_protocol(FIRST) is None
    assert scenario.player(player_id) is None


async def test_a_removed_disabled_server_is_forgotten(scenario: Scenario) -> None:
    """The removal of a player that is disabled also reaches its server."""
    provider = await add_provider(scenario, BOTH)
    player_id = _player(provider, SECOND).player_id
    await scenario.disable(player_id)
    gc.collect()

    await scenario.remove(player_id)
    await scenario.idle()

    assert _hosts(scenario, provider) == [HOST]
    assert scenario.mass.config.get_protocol_record(SECOND) is None


async def test_a_removed_server_that_never_attached_is_forgotten(scenario: Scenario) -> None:
    """A server disabled before the server started is found by the removal of its player."""
    provider = await add_provider(scenario, BOTH)
    player_id = _player(provider, SECOND).player_id
    await scenario.disable(player_id)
    await scenario.restart()
    provider = await load_provider(scenario, provider.instance_id)
    assert provider.get_protocol(SECOND) is None
    gc.collect()

    await scenario.remove(player_id)
    await scenario.idle()

    assert _hosts(scenario, provider) == [HOST]
    assert scenario.mass.config.get_protocol_record(SECOND) is None


async def test_a_host_taken_out_of_the_config_is_removed(scenario: Scenario) -> None:
    """At the next load a server that is no longer configured loses its player."""
    provider = await add_provider(scenario, BOTH)
    player_id = _player(provider, FIRST).player_id

    await reconfigure(scenario, provider, [f"{HOST}:6601"])

    assert scenario.player(player_id) is None
    assert scenario.mass.config.get_protocol_record(FIRST) is None


async def test_a_setting_of_a_new_player_is_its_server_s(scenario: Scenario) -> None:
    """The codec of a player with a minted id is stored for its server and read back."""
    provider = await add_provider(scenario, [HOST])
    player_id = _player(provider, FIRST).player_id

    await scenario.configure_player(player_id, **{CONF_OUTPUT_CODEC: "wav"})

    assert _protocol(provider, FIRST).get_config_value(CONF_OUTPUT_CODEC) == "wav"
    assert CONF_OUTPUT_CODEC not in (scenario.mass.config.get(f"players/{player_id}/values") or {})
