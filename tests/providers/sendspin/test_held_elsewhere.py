"""Tests for Sendspin devices another server holds that this server has not approved."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Set as AbstractSet
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest
from aiosendspin.models.types import GoodbyeReason, PairMethod
from aiosendspin.noise.keys import generate_psk, psk_id_for
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
    CONNECT_METHOD_UNPAIRED_WITH_INPUT,
    CONNECT_METHOD_UNPAIRED_WITHOUT_INPUT,
)
from music_assistant.providers.sendspin.player import SendspinBasePlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

from .test_pin_session import _FakeMass
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
    mass.players = SimpleNamespace(get_player=lambda _client_id: player)  # type: ignore[attr-defined]
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", mass)
    provider.server_api = cast("SendspinServer", api)
    provider.logger = logging.getLogger("test.sendspin.held")
    provider._held_elsewhere = set()
    provider._held_unapproved = set()
    provider._unencrypted_clients = set()
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
    """Playing it reclaims it, so it needs no setup."""
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


async def test_a_held_legacy_unencrypted_device_needs_no_approval() -> None:
    """A device connected without encryption plays without any approval."""
    provider, _player = _make_provider(_HeldServerApi(encrypted=False))

    await _refuse(provider)

    assert not provider.is_held_unapproved("c1")


async def test_approving_a_held_device_lets_the_next_play_take_it_over() -> None:
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


def _held_player(*, guest: bool, roles: list[str]) -> tuple[SendspinBasePlayer, _FakeProvider]:
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PAIRING_CODE)], unpaired_access=guest)
    api.connection_security = None
    api.is_connected = False
    api.negotiated_role_ids = roles
    provider = _FakeProvider(api)
    provider.held_unapproved = True
    player = _make_player(api, provider)
    _attach_mass(player)
    return player, provider


def test_a_held_unapproved_device_needs_setup_and_offers_it() -> None:
    """It is kept out of the playable players, and the setup flow can approve it."""
    player, _provider = _held_player(guest=True, roles=["player@v1"])

    assert player.needs_setup
    assert player.setup_flow_available


async def test_setup_approves_a_held_device_with_its_input_without_pairing() -> None:
    """Pairing needs a live connection, so only the guest choices are offered."""
    player, provider = _held_player(guest=True, roles=["player@v1", "source@v1"])
    session, _mass = _make_session(_ok_finish)

    task = asyncio.create_task(player.run_setup_flow(session))
    step = await _wait_step(
        session, step_type=FlowStepType.FORM, step_id="approve_held_device_source"
    )
    assert [option.value for option in step.entries[0].options] == [
        CONNECT_METHOD_UNPAIRED_WITH_INPUT,
        CONNECT_METHOD_UNPAIRED_WITHOUT_INPUT,
    ]
    session.handle_submit({CONF_CONNECT_METHOD: CONNECT_METHOD_UNPAIRED_WITH_INPUT})

    await _wait_for(lambda: session.finished)
    await task
    assert provider.trust_calls == [True]
    assert provider.allow_input_calls == [True]


async def test_setup_of_a_held_device_without_guest_access_explains_why_it_cannot_pair() -> None:
    """A device that only pairs cannot be set up while another server holds it."""
    player, provider = _held_player(guest=False, roles=["player@v1"])
    session, _mass = _make_session(_ok_finish)

    with pytest.raises(AbortFlow) as excinfo:
        await player.run_setup_flow(session)
    assert excinfo.value.reason == "held_elsewhere"
    assert provider.start_calls == 0
