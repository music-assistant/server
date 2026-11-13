"""Tests for the volume and mute a Sendspin device reports before its protocol is attached."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.models.core import ClientHelloPayload, ClientStatePayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    PlayerStatePayload,
    SupportedAudioFormat,
)
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.noise.trust_store import PskCategory
from aiosendspin.server.roles.player.v1 import PlayerPersistentState, PlayerV1Role

from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from tests.scenarios.harness import Scenario

CLIENT_ID = "voice-den"

# the code of the Sendspin provider that turns a connected client into its protocol
_CLIENT_CODE = (
    "_handle_client_added",
    "_create_player",
    "_device_role",
    "_begin_client_event",
    "_finish_client_event",
    "_is_current_client_event",
    "_await_client_hello",
)


@pytest.fixture(autouse=True)
def _no_sendspin_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the config entries of a client out of the pairing state."""

    async def _entries(_self: SendspinPlayer) -> list[ConfigEntry]:
        return []

    monkeypatch.setattr(SendspinPlayer, "get_config_entries", _entries)


def _client_state(volume: int, *, muted: bool) -> ClientStatePayload:
    """
    Return a client/state of the device with its level and mute.

    :param volume: The level the device reports.
    :param muted: Whether the device reports itself muted.
    """
    return ClientStatePayload(
        available=True,
        player=PlayerStatePayload(
            volume=volume,
            muted=muted,
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        ),
    )


def _client(hello_commands: list[PlayerCommand] | None) -> MagicMock:
    """
    Return a connected client that passes the events of its roles to its listeners.

    :param hello_commands: The commands an older client lists in its hello, None for a
        current one, which declares them in client/state only.
    """
    hello = ClientHelloPayload(
        client_id=CLIENT_ID,
        name="Den",
        version=1,
        supported_roles=["player@v1"],
        device_info=SendspinDeviceInfo(product_name="Voice", manufacturer="Acme"),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16
                )
            ],
            buffer_capacity=1024,
            supported_commands=hello_commands,
        ),
    )
    client = MagicMock(
        client_id=CLIENT_ID,
        info=hello,
        info_or_none=hello,
        negotiated_role_ids=["player@v1"],
        connection_security=SimpleNamespace(psk_category=PskCategory.LONG_TERM),
        is_connected=True,
    )
    listeners: list[Callable[[Any, Any], None]] = []

    def _add_listener(listener: Callable[[Any, Any], None]) -> Callable[[], None]:
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    client.add_event_listener.side_effect = _add_listener
    client._signal_event.side_effect = lambda event: [
        listener(client, event) for listener in list(listeners)
    ]
    role = PlayerV1Role(client)
    role._cached_state = PlayerPersistentState()
    client.active_roles = [role]
    client.roles_by_family.side_effect = lambda family: [role] if family == "player" else []
    return client


async def _connect(scenario: Scenario, client: MagicMock, *, replaced: bool = False) -> None:
    """
    Let a voice device connect that sends its state while the provider sets it up.

    :param scenario: The running server.
    :param client: The client of the device.
    :param replaced: Whether a second connection of the device replaces the first after that
        state, and sends no state of its own.
    """
    await scenario.add_provider("sendspin")
    provider: Any = scenario.provider("sendspin")
    for name in _CLIENT_CODE:
        setattr(provider, name, getattr(SendspinProvider, name).__get__(provider))
    provider.server_api = SimpleNamespace(get_client={CLIENT_ID: client}.get)
    provider.is_virtual_player = lambda _client_id: False
    provider.is_held_unapproved = lambda _client_id: False
    provider.is_held_elsewhere = lambda _client_id: False
    provider._auto_trust_guest_access = AsyncMock()
    provider._unloading = False
    provider._pending_unregisters = {}
    provider._client_event_versions = {}
    provider._client_event_task_counts = {}
    provider._bridge_identifiers = {}
    provider._bridge_underlying_protocols = {}
    provider._bridge_static_delay_defaults = {}
    provider._bridge_player_types = {}

    async def _enrich(_players: list[SendspinPlayer]) -> None:
        # the client/state arrives between the hello and the attach
        role = client.active_roles[0]
        role.on_client_state(_client_state(28, muted=False))
        if replaced:
            # what the client does with its role when another connection replaces the first
            role.on_disconnect()
            role.on_connect()

    provider._apply_hass_esphome_enrichment = _enrich
    await provider._handle_client_added(CLIENT_ID, provider._begin_client_event(CLIENT_ID))
    await scenario.settle()


@pytest.mark.parametrize(
    "hello_commands",
    [None, [PlayerCommand.VOLUME, PlayerCommand.MUTE]],
    ids=["current_client", "older_client"],
)
async def test_a_device_shows_the_volume_it_reported_before_its_protocol_was_attached(
    scenario: Scenario, hello_commands: list[PlayerCommand] | None
) -> None:
    """A Sendspin device shows its level and mute from its attach on, and takes volume commands."""
    client = _client(hello_commands)
    await _connect(scenario, client)

    protocol = scenario.mass.players.get_protocol(CLIENT_ID)
    assert protocol is not None
    assert protocol.player is not None
    player_id = protocol.player.player_id
    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_level, state.volume_muted) == (28, False)

    await scenario.mass.players.cmd_volume_set(player_id, 40)
    sent = [call.args[0].payload.player for call in client.send_message.call_args_list]
    assert [(command.command, command.volume) for command in sent] == [(PlayerCommand.VOLUME, 40)]
    # the device reports the level it was set to
    client.active_roles[0].on_client_state(_client_state(40, muted=False))
    await scenario.settle()
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 40


async def test_a_device_keeps_its_reported_volume_when_a_second_connection_replaces_the_first(
    scenario: Scenario,
) -> None:
    """
    The level and mute a device reported stay when a second connection of it replaces the first.

    The second connection sends no state until the device plays; until then the library takes
    no volume command for it, and the player keeps its controls.
    """
    client = _client([PlayerCommand.VOLUME, PlayerCommand.MUTE])
    await _connect(scenario, client, replaced=True)

    protocol = scenario.mass.players.get_protocol(CLIENT_ID)
    assert protocol is not None
    assert protocol.player is not None
    player_id = protocol.player.player_id
    state = scenario.player(player_id)
    assert state is not None
    assert (state.volume_level, state.volume_muted, state.volume_control) == (28, False, "native")

    # the device reports its state on the new connection, from then on it takes commands
    client.active_roles[0].on_client_state(_client_state(28, muted=False))
    await scenario.settle()
    await scenario.mass.players.cmd_volume_set(player_id, 40)
    sent = [call.args[0].payload.player for call in client.send_message.call_args_list]
    assert [(command.command, command.volume) for command in sent] == [(PlayerCommand.VOLUME, 40)]
