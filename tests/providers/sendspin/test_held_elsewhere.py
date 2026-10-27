"""Tests for Sendspin devices another server holds that this server has not approved."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Set as AbstractSet
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest
from aiosendspin.models.types import GoodbyeReason, PairAbortReason, PairMethod
from aiosendspin.noise.keys import generate_psk, psk_id_for
from aiosendspin.noise.pairing import RemotePairingAbortError
from aiosendspin.noise.trust_store import (
    InMemoryServerPairingStore,
    PskCategory,
    ServerPairingRecord,
    TrustedUnpairedClient,
)
from aiosendspin.server import ClientConnectedEvent, ClientDisconnectedEvent
from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import AbortFlow
from music_assistant.providers.sendspin.constants import (
    CONF_CONNECT_METHOD,
    CONF_PAIRING_PIN,
    CONNECT_METHOD_UNPAIRED,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

from .test_pin_session import _FakeMass, _offer
from .test_pin_session import _FakeServerApi as _PinServerApi
from .test_pin_session import _make_provider as _make_pin_provider
from .test_setup_flow import (
    _attach_mass,
    _desc,
    _FakeApi,
    _FakeProvider,
    _make_player,
    _make_session,
    _ok_finish,
    _wait_for,
    _wait_step,
)

if TYPE_CHECKING:
    from aiosendspin.server import SendspinServer

    from music_assistant.mass import MusicAssistant


class _HeldServerApi:
    """Server stand-in with a pairing store and an offline trust call."""

    def __init__(self, *, encrypted: bool = True) -> None:
        self.pairing_store = InMemoryServerPairingStore()
        security = SimpleNamespace(psk_category=PskCategory.SENTINEL) if encrypted else None
        self._client = SimpleNamespace(connection_security=security)

    def get_client(self, client_id: str) -> Any:
        return self._client

    async def trust_unpaired(
        self, client_id: str, *, approved_roles: AbstractSet[str] | None = None
    ) -> None:
        await self.pairing_store.add_trusted_unpaired(
            TrustedUnpairedClient(
                client_id=client_id, approved_roles=frozenset(approved_roles or ())
            )
        )


def _make_provider(api: _HeldServerApi) -> tuple[SendspinProvider, Mock]:
    player = Mock(spec=SendspinBasePlayer)
    player.initialized.is_set.return_value = True
    mass = _FakeMass(asyncio.get_running_loop())
    mass.players = SimpleNamespace(  # type: ignore[attr-defined]
        get_player=lambda _client_id: player, trigger_player_update=Mock()
    )
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", mass)
    provider.server_api = cast("SendspinServer", api)
    provider.logger = logging.getLogger("test.sendspin.held")
    provider._held_elsewhere = set()
    provider._held_unapproved = set()
    provider._held_paired = set()
    provider._unencrypted_clients = set()
    provider._hass_auto_pairer = Mock()
    provider._pending_pairing_evictions = set()
    return provider, player


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _refuse(provider: SendspinProvider) -> None:
    """Connect, then get refused because another server holds the device."""
    server = cast("SendspinServer", None)
    provider.event_cb(server, ClientConnectedEvent("c1"))
    provider.event_cb(server, ClientDisconnectedEvent("c1", GoodbyeReason.CONCURRENT_ATTEMPT))
    await _settle()


async def test_a_refused_device_never_approved_here_is_held_unapproved() -> None:
    """The player is recomputed so it shows as needing setup instead of looking playable."""
    provider, player = _make_provider(_HeldServerApi())

    await _refuse(provider)

    assert provider.is_held_unapproved("c1")
    player.update_state.assert_called()


@pytest.mark.parametrize("paired", [False, True])
async def test_a_held_device_approved_or_paired_here_stays_playable(paired: bool) -> None:
    """Playing it reclaims it, so it needs no setup, and only a guest can still be paired."""
    api = _HeldServerApi()
    if paired:
        psk = generate_psk()
        await api.pairing_store.store_record(
            ServerPairingRecord(
                psk_id=psk_id_for(psk), psk=psk, client_id="c1", pair_methods=[], owner=None
            )
        )
    else:
        await api.pairing_store.add_trusted_unpaired(TrustedUnpairedClient(client_id="c1"))
    provider, _player = _make_provider(api)

    await _refuse(provider)

    assert not provider.is_held_unapproved("c1")
    assert provider.is_held_unpaired("c1") is not paired


async def test_a_held_legacy_unencrypted_device_needs_no_approval() -> None:
    """A device connected without encryption plays without any approval."""
    provider, _player = _make_provider(_HeldServerApi(encrypted=False))

    await _refuse(provider)

    assert not provider.is_held_unapproved("c1")


async def test_approving_a_held_device_clears_its_setup_state() -> None:
    """The approval is stored while the device is away, so the reclaim dial may play."""
    api = _HeldServerApi()
    provider, player = _make_provider(api)
    await _refuse(provider)
    player.update_state.reset_mock()

    await provider.set_trusted_unpaired("c1", enabled=True, allow_input=True)

    assert not provider.is_held_unapproved("c1")
    player.update_state.assert_called()


async def test_a_device_that_reconnects_here_is_no_longer_held() -> None:
    """The live connection decides its setup state again."""
    provider, _player = _make_provider(_HeldServerApi())
    await _refuse(provider)

    provider.event_cb(cast("SendspinServer", None), ClientConnectedEvent("c1"))

    assert not provider.is_held_unapproved("c1")


def _held_player(
    *, guest: bool, roles: list[str], approved: bool = False
) -> tuple[SendspinBasePlayer, _FakeProvider]:
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PAIRING_CODE)], unpaired_access=guest)
    api.connection_security = None
    api.is_connected = False
    api.negotiated_role_ids = roles
    provider = _FakeProvider(api)
    provider.held_unapproved = not approved
    provider.held_unpaired = True
    player = _make_player(api, provider)
    _attach_mass(player)
    return player, provider


def test_a_held_unapproved_device_needs_setup_and_offers_it() -> None:
    """It is kept out of the playable players, and the setup flow can approve it."""
    player, _provider = _held_player(guest=True, roles=["player@v1"])

    assert player.needs_setup
    assert player.setup_flow_available


async def test_setup_approves_a_held_device() -> None:
    """Approving needs no connection to the device."""
    player, provider = _held_player(guest=True, roles=["player@v1"])
    session, _mass = _make_session(_ok_finish)

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="approve_device")
    session.handle_submit({CONF_CONNECT_METHOD: CONNECT_METHOD_UNPAIRED})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.trust_calls == [True]


async def test_a_held_device_approved_here_pairs_without_asking_again() -> None:
    """Pairing stays available as an upgrade, and the approval is not asked for twice."""
    player, provider = _held_player(guest=True, roles=["player@v1", "source@v1"], approved=True)
    session, _mass = _make_session(_ok_finish)
    assert not player.needs_setup
    assert player.setup_flow_available

    task = asyncio.create_task(player.run_setup_flow(session))
    await _wait_step(session, step_type=FlowStepType.FORM, step_id="enter_pin")
    session.handle_submit({CONF_PAIRING_PIN: "123456"})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.submitted_pins == ["123456"]
    assert provider.trust_calls == []


def test_a_held_device_paired_here_has_nothing_to_set_up() -> None:
    """Its pairing already lets the next play take it over."""
    player, provider = _held_player(guest=True, roles=["player@v1"], approved=True)
    provider.held_unpaired = False

    assert not player.setup_flow_available


async def test_a_busy_refusal_stops_pairing_instead_of_dialing_again() -> None:
    """A device connected here when setup began can be taken over while a form is open."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PAIRING_CODE)])
    provider = _FakeProvider(api)
    player = _make_player(api, provider)
    _attach_mass(player)
    start_pin_pairing = provider.start_pin_pairing

    async def _refused(client_id: str, *, static: bool = False) -> Any:
        pin_session = await start_pin_pairing(client_id, static=static)
        pin_session.awaiting_pin = False
        pin_session.can_retry = True
        pin_session.error = RemotePairingAbortError(PairAbortReason.CONCURRENT_ATTEMPT)
        return pin_session

    provider.start_pin_pairing = _refused  # type: ignore[method-assign]
    session, _mass = _make_session(_ok_finish)

    with pytest.raises(AbortFlow) as excinfo:
        await player.run_setup_flow(session)
    assert excinfo.value.reason == "pairing_error_concurrent"
    assert provider.start_calls == 1


async def test_a_held_device_is_dialed_for_pairing_without_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held device is reached by the pairing dial, never over management."""
    api = _PinServerApi(
        _offer(PairMethod.STATIC_PAIRING_CODE),
        await_pin=False,
        management_capable=True,
        legacy_wire=True,
        connected=False,
    )
    cast("Any", api.get_client("c")).connection = None
    cast("Any", api).get_client_url = lambda _client_id: "ws://device/sendspin"
    provider, _refreshed = _make_pin_provider(api, monkeypatch)
    provider._held_elsewhere.add("c")

    pin_session = await provider.start_pin_pairing("c", static=True)
    assert pin_session.task is not None
    await pin_session.task

    assert api.calls == ["pair"]


async def test_cancelling_a_pairing_dial_before_it_attaches_withdraws_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """end_pairing cannot reach a dial that has no connection yet, so cancel stops it."""
    api = _PinServerApi(
        _offer(PairMethod.STATIC_PAIRING_CODE), gesture=asyncio.Event(), connected=False
    )
    cast("Any", api.get_client("c")).connection = None
    cast("Any", api).get_client_url = lambda _client_id: "ws://device/sendspin"
    provider, _refreshed = _make_pin_provider(api, monkeypatch)
    provider._held_elsewhere.add("c")
    pin_session = await provider.start_pin_pairing("c", static=True)

    async with asyncio.timeout(1):
        await provider.cancel_pin_pairing("c")

    assert pin_session.task is not None
    assert pin_session.task.cancelled()
