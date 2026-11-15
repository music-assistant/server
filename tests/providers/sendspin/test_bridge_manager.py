"""Tests for the shared Sendspin bridge manager lifecycle reconciliation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant.constants import CONF_UNDERLYING_PLAYER_ID
from music_assistant.providers.sendspin.bridge_manager import (
    SendspinBridgeManagerBase,
    evaluate_bridge_of,
)

BASE_ID = "base_1"
CLIENT_ID = f"spb_{BASE_ID}"
# the player of the bridged protocol has an id of its own
PLAYER_ID = "pl0123456789ab"


class FakeBridge:
    """Minimal Sendspin bridge implementation for testing."""

    def __init__(self, sendspin_server: Any, protocol: Any = None) -> None:
        """Initialize the fake bridge."""
        self.sendspin_server = sendspin_server
        self.protocol = protocol
        self.started = False
        self.stopped = False

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self.started and not self.stopped

    async def start(self) -> None:
        """Register the bridge as an external Sendspin client."""
        self.started = True

    async def stop(self) -> None:
        """Stop and unregister the bridge."""
        self.stopped = True


class FakeBridgeManager(SendspinBridgeManagerBase[FakeBridge]):
    """Concrete bridge manager with controllable policy for testing."""

    policy_result = True

    def _bridge_client_id(self, protocol: Any) -> str | None:
        """Return the Sendspin client_id used to bridge the given protocol."""
        return f"spb_{protocol.protocol_id}"

    def _create_bridge(self, protocol: Any) -> FakeBridge:
        """Create a (not yet started) bridge instance for the given protocol."""
        return FakeBridge(self.sendspin_server, protocol)

    def _should_have_bridge(self, protocol: Any) -> bool:
        """Return whether provider policy wants a bridge for this protocol."""
        return self.policy_result


class _Environment:
    """A bridge manager with a mocked server around it."""

    def __init__(self) -> None:
        """Build the manager and one attached protocol of its provider."""
        # the protocols the controller holds, the protocol records, the player records
        self.protocols: dict[str, Any] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.player_records: dict[str, Any] = {f"players/{PLAYER_ID}": {"enabled": True}}
        # the protocols the server does not want attached (switched off, player disabled)
        self.unwanted: set[str] = set()
        mass = MagicMock()
        mass.subscribe = MagicMock(return_value=MagicMock())
        mass.create_task = MagicMock(
            side_effect=lambda coro, **_kwargs: asyncio.get_running_loop().create_task(coro)
        )
        self.sendspin_provider = MagicMock()
        mass.get_provider = MagicMock(
            side_effect=lambda domain: self.sendspin_provider if domain == "sendspin" else None
        )
        mass.players.get_protocol = MagicMock(side_effect=self.protocols.get)
        mass.players.wants_protocol = MagicMock(
            side_effect=lambda protocol_id: protocol_id not in self.unwanted
        )
        mass.players.remove_protocol = AsyncMock()
        mass.config.get_protocol_record = MagicMock(side_effect=self.records.get)
        mass.config.get = MagicMock(
            side_effect=lambda key, default=None: self.player_records.get(key, default)
        )
        mass.config.save_protocol_config = AsyncMock()
        self.mass = mass
        provider = MagicMock()
        provider.mass = mass
        provider.get_protocol = lambda protocol_id: self.protocols.get(protocol_id)
        provider.logger = logging.getLogger("test.bridge_manager")
        self.provider = provider
        self.manager = FakeBridgeManager(provider)
        provider.bridge_manager = self.manager
        self.protocol = self.attach(BASE_ID)

    def attach(self, protocol_id: str) -> Any:
        """
        Attach a new protocol object of the provider to the player.

        :param protocol_id: Id of the protocol.
        """
        protocol = MagicMock()
        protocol.protocol_id = protocol_id
        protocol.display_name = "Test Player"
        protocol.provider = self.provider
        protocol.player = MagicMock(player_id=PLAYER_ID)
        protocol._on_detach_callbacks = []
        self.protocols[protocol_id] = protocol
        self.provider.protocols = list(self.protocols.values())
        return protocol

    def detach(self, protocol: Any) -> None:
        """
        Detach a protocol the way the controller does: it runs the detach callbacks.

        :param protocol: The protocol object to detach.
        """
        self.protocols.pop(protocol.protocol_id, None)
        protocol.player = None
        callbacks, protocol._on_detach_callbacks = protocol._on_detach_callbacks, []
        for callback in callbacks:
            callback()

    def store_client(self, *, underlying_id: str = BASE_ID, enabled: bool = True) -> None:
        """
        Store the protocol record of the bridge client, as its attach does.

        :param underlying_id: The protocol the client rides on.
        :param enabled: Whether its output is switched on.
        """
        self.records[CLIENT_ID] = {
            "protocol_id": CLIENT_ID,
            "player_id": PLAYER_ID,
            "enabled": enabled,
            "values": {CONF_UNDERLYING_PLAYER_ID: underlying_id},
        }


async def test_bridge_created_for_an_attached_protocol() -> None:
    """A protocol attached to its player gets a bridge while Sendspin is loaded."""
    env = _Environment()

    await env.manager.evaluate_bridge(env.protocol)

    bridge = env.manager.get_bridge(BASE_ID)
    assert bridge is not None
    assert bridge.is_registered


async def test_no_bridge_for_a_protocol_that_waits_for_its_player() -> None:
    """A bridge belongs to the player of the protocol it rides on, so it waits for that."""
    env = _Environment()
    env.protocol.player = None

    await env.manager.evaluate_bridge(env.protocol)

    assert env.manager.get_bridge(BASE_ID) is None


async def test_no_bridge_for_an_object_that_is_not_the_attached_one() -> None:
    """An object the controller does not hold under its id gets no bridge."""
    env = _Environment()
    del env.protocols[BASE_ID]

    await env.manager.evaluate_bridge(env.protocol)

    assert env.manager.get_bridge(BASE_ID) is None


async def test_no_bridge_without_sendspin() -> None:
    """Without the Sendspin provider there is no server to register a bridge with."""
    env = _Environment()
    env.mass.get_provider = MagicMock(return_value=None)

    await env.manager.evaluate_bridge(env.protocol)

    assert env.manager.get_bridge(BASE_ID) is None


async def test_bridge_stops_when_its_protocol_is_detached() -> None:
    """A device that is gone, a disabled player or an unloading provider takes the bridge along."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    bridge = env.manager.get_bridge(BASE_ID)
    assert bridge is not None

    env.detach(env.protocol)
    await asyncio.sleep(0)

    assert env.manager.get_bridge(BASE_ID) is None
    assert bridge.stopped
    env.mass.players.remove_protocol.assert_not_awaited()


async def test_protocol_attached_again_gets_a_new_bridge() -> None:
    """The bridge of a protocol found again is built anew, after the old one went."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    old_bridge = env.manager.get_bridge(BASE_ID)
    assert old_bridge is not None

    env.detach(env.protocol)
    new_protocol = env.attach(BASE_ID)
    await env.manager.evaluate_bridge(new_protocol)

    new_bridge = env.manager.get_bridge(BASE_ID)
    assert old_bridge.stopped
    assert new_bridge is not None
    assert new_bridge.protocol is new_protocol


async def test_bridge_stops_when_its_output_is_switched_off() -> None:
    """The user switching the Sendspin output off takes the bridge away, its config stays."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    bridge = env.manager.get_bridge(BASE_ID)
    assert bridge is not None

    env.store_client(enabled=False)
    env.unwanted.add(CLIENT_ID)
    # what the Sendspin protocol of the bridge asks for when it is detached
    await evaluate_bridge_of(env.mass, BASE_ID)

    assert env.manager.get_bridge(BASE_ID) is None
    assert bridge.stopped
    env.mass.players.remove_protocol.assert_not_awaited()
    env.mass.config.save_protocol_config.assert_not_awaited()


async def test_bridge_comes_back_when_its_output_is_switched_on() -> None:
    """Switching the output on again sets the bridge up through the protocol it rides on."""
    env = _Environment()
    env.store_client(enabled=False)
    env.unwanted.add(CLIENT_ID)
    await env.manager.evaluate_bridge(env.protocol)
    assert env.manager.get_bridge(BASE_ID) is None

    env.records[CLIENT_ID]["enabled"] = True
    env.unwanted.clear()
    await evaluate_bridge_of(env.mass, BASE_ID)

    assert env.manager.get_bridge(BASE_ID) is not None


async def test_evaluating_a_protocol_that_is_not_attached_does_nothing() -> None:
    """A protocol that is not attached, or of a provider without bridges, is left alone."""
    env = _Environment()
    env.protocol.player = None

    await evaluate_bridge_of(env.mass, BASE_ID)
    await evaluate_bridge_of(env.mass, "unknown")

    assert env.manager.get_bridge(BASE_ID) is None


async def test_policy_denial_removes_the_bridge_and_its_client() -> None:
    """A protocol that must not have a bridge loses it, with the client protocol and config."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    assert env.manager.get_bridge(BASE_ID) is not None
    env.store_client()

    env.manager.policy_result = False
    await env.manager.evaluate_bridge(env.protocol)

    assert env.manager.get_bridge(BASE_ID) is None
    env.mass.players.remove_protocol.assert_awaited_once_with(CLIENT_ID)


async def test_permanent_removal_leaves_a_client_riding_on_another_protocol_alone() -> None:
    """Another manager can compute the same client id for the device: its client stays."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    # the client with this id rides on the Cast protocol of the same speaker
    env.store_client(underlying_id="cast_protocol")

    await env.manager.remove_bridge(BASE_ID, permanent=True)

    assert env.manager.get_bridge(BASE_ID) is None
    env.mass.players.remove_protocol.assert_not_awaited()


async def test_permanent_removal_of_an_attached_client_checks_what_it_rides_on() -> None:
    """An attached client is judged by the protocol it reports to ride on."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    client = env.attach(CLIENT_ID)
    client.underlying_protocol_id = "cast_protocol"

    await env.manager.remove_bridge(BASE_ID, permanent=True)
    env.mass.players.remove_protocol.assert_not_awaited()

    client.underlying_protocol_id = BASE_ID
    await env.manager.remove_bridge(BASE_ID, permanent=True)
    env.mass.players.remove_protocol.assert_awaited_once_with(CLIENT_ID)


async def test_stale_disabled_client_is_enabled_again_when_its_player_is_gone() -> None:
    """A client disabled under a player that no longer exists is switched on again."""
    env = _Environment()
    env.store_client(enabled=False)
    env.records[CLIENT_ID]["player_id"] = "pl_gone"

    await env.manager.evaluate_bridge(env.protocol)

    env.mass.config.save_protocol_config.assert_awaited_once_with(CLIENT_ID, {"enabled": True})


async def test_stale_disabled_client_without_a_player_is_enabled_again() -> None:
    """A disabled client that never belonged to a player is switched on again."""
    env = _Environment()
    env.store_client(enabled=False)
    env.records[CLIENT_ID]["player_id"] = None

    await env.manager.evaluate_bridge(env.protocol)

    env.mass.config.save_protocol_config.assert_awaited_once_with(CLIENT_ID, {"enabled": True})


async def test_disabled_client_of_an_existing_player_is_respected() -> None:
    """A client the user switched off on a player that exists stays off."""
    env = _Environment()
    env.store_client(enabled=False)
    env.unwanted.add(CLIENT_ID)

    await env.manager.evaluate_bridge(env.protocol)

    env.mass.config.save_protocol_config.assert_not_awaited()
    assert env.manager.get_bridge(BASE_ID) is None


async def test_stale_bridge_rebuilt_after_sendspin_reload() -> None:
    """A bridge bound to a replaced Sendspin server is rebuilt."""
    env = _Environment()
    await env.manager.evaluate_bridge(env.protocol)
    old_bridge = env.manager.get_bridge(BASE_ID)
    assert old_bridge is not None

    # Simulate a sendspin provider reload (new server instance)
    new_provider = MagicMock()
    env.mass.get_provider = MagicMock(
        side_effect=lambda domain: new_provider if domain == "sendspin" else None
    )
    await env.manager.evaluate_bridge(env.protocol)

    new_bridge = env.manager.get_bridge(BASE_ID)
    assert old_bridge.stopped
    assert new_bridge is not None
    assert new_bridge is not old_bridge
    assert new_bridge.sendspin_server is new_provider.server_api


async def test_provider_change_evaluates_the_attached_protocols_only() -> None:
    """A provider (un)loading re-evaluates the protocols that are attached to a player."""
    env = _Environment()
    waiting = env.attach("base_2")
    waiting.player = None

    await env.manager._on_providers_updated(MagicMock())

    assert env.manager.get_bridge(BASE_ID) is not None
    assert env.manager.get_bridge("base_2") is None
