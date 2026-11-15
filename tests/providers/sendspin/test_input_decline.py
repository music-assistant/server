"""Tests for the decline of the audio input of a client that only exists while it is connected."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from aiosendspin.models.core import ClientHelloPayload, UnpairedAccess
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.noise.trust_store import PskCategory
from music_assistant_models.enums import PlayerType, ProtocolRole

from music_assistant.constants import CONF_PLAYERS
from music_assistant.providers.sendspin.constants import (
    CONF_ACTION_SOURCE_INPUT_DISABLE,
    CONF_ACTION_SOURCE_INPUT_ENABLE,
    CONF_CONNECT_METHOD,
    CONF_SOURCE_INPUT_ACTION,
    CONF_SOURCE_INPUT_DECLINED,
    CONNECT_METHOD_UNPAIRED_WITH_INPUT,
    CONNECT_METHOD_UNPAIRED_WITHOUT_INPUT,
    SOURCE_INPUT_DISMISS,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinVisualizerPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.providers.sendspin.provider import SendspinProvider
    from tests.scenarios.harness import Scenario

CLIENT_ID = "display-key-id-0001"
NEGOTIATED_ROLE_IDS = ["metadata@v1", "source@v1"]

# the role families the server approved for the client: None while it is not approved
Approvals = dict[str, frozenset[str] | None]


@pytest.fixture(autouse=True)
def _no_security_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the pairing status out of the config entries of a client."""

    async def _entries(_self: SendspinBasePlayer) -> list[ConfigEntry]:
        return []

    monkeypatch.setattr(SendspinBasePlayer, "_get_security_config_entries", _entries)


def _sendspin(scenario: Scenario, approvals: Approvals) -> Any:
    """
    Return the loaded Sendspin provider, whose server approves its clients as they are told.

    :param scenario: The running server.
    :param approvals: What the server approved for each client, kept across restarts as
        the pairing store keeps it.
    """
    provider: Any = scenario.provider("sendspin")
    provider.clients = {}
    provider.server_api = SimpleNamespace(
        get_client=provider.clients.get,
        pairing_store=SimpleNamespace(record_by_client_id=AsyncMock(return_value=None)),
    )
    provider.is_virtual_player = lambda _client_id: False
    provider.is_held_unapproved = lambda _client_id: False
    provider.pairing_config_snapshot = lambda _client_id: None

    async def _set_trusted_unpaired(
        client_id: str, enabled: bool, *, allow_input: bool = False
    ) -> None:
        assert enabled
        approvals[client_id] = frozenset({"source"} if allow_input else ())
        if (protocol := provider.get_protocol(client_id)) is not None:
            protocol.update_state()

    provider.set_trusted_unpaired = _set_trusted_unpaired
    return provider


def _connect(sendspin: Any, approvals: Approvals, *, guest_access: bool) -> SendspinBasePlayer:
    """
    Connect the display, which has an audio input, and return the protocol the provider creates.

    :param sendspin: The Sendspin provider.
    :param approvals: What the server approved for each client.
    :param guest_access: Whether the display offers to play without a pairing.
    """
    hello = ClientHelloPayload(
        client_id=CLIENT_ID,
        name="Display",
        version=1,
        supported_roles=NEGOTIATED_ROLE_IDS,
        device_info=SendspinDeviceInfo(product_name="ESP32 Display", manufacturer="Espressif"),
        unpaired_access=UnpairedAccess(enabled=guest_access),
    )

    def _active_families() -> set[str]:
        approved = approvals.get(CLIENT_ID)
        if approved is None:
            return set()
        # the input needs an approval of its own
        return {"metadata", *(approved & {"source"})}

    client = MagicMock(
        client_id=CLIENT_ID,
        info=hello,
        info_or_none=hello,
        negotiated_role_ids=NEGOTIATED_ROLE_IDS,
        connection_security=SimpleNamespace(psk_category=PskCategory.SENTINEL),
        is_connected=True,
    )
    type(client).active_roles = PropertyMock(side_effect=lambda: sorted(_active_families()))
    client.roles_by_family.side_effect = lambda family: (
        [object()] if family in _active_families() else []
    )
    sendspin.clients[CLIENT_ID] = client
    protocol = SendspinVisualizerPlayer(cast("SendspinProvider", sendspin), CLIENT_ID, hello)
    protocol._attr_type = PlayerType.DISPLAY
    protocol._attr_role = ProtocolRole.NATIVE
    return protocol


async def _register(scenario: Scenario, protocol: SendspinBasePlayer) -> None:
    """Register a protocol, as the provider does for a connected client."""
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    assert protocol.player is not None


def _session(answer: dict[str, str]) -> MagicMock:
    """
    Return a setup flow session in which the user gives one answer.

    :param answer: The values the user submits on the step.
    """
    session = MagicMock(context=SimpleNamespace(values={}))
    session.form = AsyncMock(return_value=answer)
    session.finish = AsyncMock()
    return session


async def _decline(scenario: Scenario, protocol: SendspinBasePlayer, writer: str) -> None:
    """
    Decline the audio input the way the user does it in one of the places that offer it.

    :param scenario: The running server.
    :param protocol: The protocol of the display.
    :param writer: ``consent``, ``dismiss`` or ``settings``.
    """
    if writer == "consent":
        # approve the display without its input
        answer = {CONF_CONNECT_METHOD: CONNECT_METHOD_UNPAIRED_WITHOUT_INPUT}
        await protocol.run_setup_flow(_session(answer))
    elif writer == "dismiss":
        # the display plays, without guest access on offer only its input is asked about
        await protocol.run_setup_flow(_session({CONF_SOURCE_INPUT_ACTION: SOURCE_INPUT_DISMISS}))
    else:
        await protocol.handle_config_action(CONF_ACTION_SOURCE_INPUT_DISABLE)
    await scenario.settle()


def _assert_declined(scenario: Scenario, protocol: SendspinBasePlayer) -> None:
    """Assert that the display plays without its input and asks nothing about it."""
    assert protocol._source_input_pending is False
    assert protocol.needs_setup is False
    state = scenario.player(CLIENT_ID)
    assert state is not None
    assert state.available is True


@pytest.mark.parametrize(
    ("writer", "approved_before"),
    [
        ("consent", None),
        ("dismiss", frozenset()),
        ("settings", frozenset({"source"})),
    ],
)
async def test_a_declined_input_stays_declined(
    scenario: Scenario, writer: str, approved_before: frozenset[str] | None
) -> None:
    """
    The decline of the input of a display that only exists while connected is kept.

    The display stays listed, unavailable, while it is disconnected; when it reconnects, also
    after a restart, it plays without asking about its input, until the user approves the input.
    """
    approvals: Approvals = {CLIENT_ID: approved_before}
    await scenario.add_provider("sendspin")
    sendspin = _sendspin(scenario, approvals)
    guest_access = writer != "dismiss"
    first = _connect(sendspin, approvals, guest_access=guest_access)
    await _register(scenario, first)

    await _decline(scenario, first, writer)

    _assert_declined(scenario, first)
    # the decline stores the config of the display, as a save of its settings does
    player_record = scenario.mass.config.get(f"{CONF_PLAYERS}/{CLIENT_ID}")
    assert player_record is not None
    assert player_record["values"]["session_only"] is True
    assert scenario.mass.config.get_protocol_value(CLIENT_ID, CONF_SOURCE_INPUT_DECLINED) is True
    await scenario.mass.players.unregister(CLIENT_ID)
    await scenario.settle()
    state = scenario.player(CLIENT_ID)
    assert state is not None
    assert state.available is False

    again = _connect(sendspin, approvals, guest_access=guest_access)
    await _register(scenario, again)
    _assert_declined(scenario, again)
    await scenario.restart()
    sendspin = _sendspin(scenario, approvals)
    after_restart = _connect(sendspin, approvals, guest_access=guest_access)
    await _register(scenario, after_restart)
    _assert_declined(scenario, after_restart)

    await after_restart.handle_config_action(CONF_ACTION_SOURCE_INPUT_ENABLE)
    await scenario.settle()
    await scenario.mass.players.unregister(CLIENT_ID)
    await scenario.settle()
    approved = _connect(sendspin, approvals, guest_access=guest_access)
    await _register(scenario, approved)

    assert approved.api.roles_by_family("source")
    assert approved._source_input_pending is False
    assert not scenario.mass.config.get_protocol_value(CLIENT_ID, CONF_SOURCE_INPUT_DECLINED)


async def test_approving_the_input_of_a_new_client_stores_nothing(scenario: Scenario) -> None:
    """A display approved with its input keeps nothing stored: that is no choice to keep."""
    approvals: Approvals = {CLIENT_ID: None}
    await scenario.add_provider("sendspin")
    sendspin = _sendspin(scenario, approvals)
    display = _connect(sendspin, approvals, guest_access=True)
    await _register(scenario, display)

    await display.run_setup_flow(
        _session({CONF_CONNECT_METHOD: CONNECT_METHOD_UNPAIRED_WITH_INPUT})
    )
    await scenario.settle()

    assert display.api.roles_by_family("source")
    assert display.needs_setup is False
    assert await scenario.stored_config_mentioning(CLIENT_ID) == []
