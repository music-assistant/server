"""Tests for automatically approving devices that offer guest access."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from aiosendspin.noise.keys import generate_psk, psk_id_for
from aiosendspin.noise.trust_store import (
    InMemoryServerPairingStore,
    PskCategory,
    ServerPairingRecord,
    TrustedUnpairedClient,
)

from music_assistant.providers.sendspin.provider import SendspinProvider

from .test_pairing_code_session import _FakeMass

if TYPE_CHECKING:
    from aiosendspin.server import SendspinServer
    from aiosendspin.server.client import SendspinClient

    from music_assistant.mass import MusicAssistant


class _ApprovalServerApi:
    """Server stand-in exposing just the pairing store and the trust call."""

    def __init__(self) -> None:
        self.pairing_store = InMemoryServerPairingStore()
        self.trusted: list[str] = []

    async def trust_unpaired(self, client_id: str) -> None:
        await self.pairing_store.add_trusted_unpaired(TrustedUnpairedClient(client_id=client_id))
        self.trusted.append(client_id)


def _client(
    *,
    guest: bool = True,
    roles: tuple[str, ...] = ("player@v1",),
    psk_category: PskCategory | None = PskCategory.SENTINEL,
) -> SendspinClient:
    security = None if psk_category is None else SimpleNamespace(psk_category=psk_category)
    return cast(
        "SendspinClient",
        SimpleNamespace(
            info_or_none=SimpleNamespace(unpaired_access=SimpleNamespace(enabled=guest)),
            negotiated_role_ids=list(roles),
            connection_security=security,
        ),
    )


def _make_provider(api: _ApprovalServerApi) -> SendspinProvider:
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", _FakeMass(asyncio.get_running_loop()))
    provider.server_api = cast("SendspinServer", api)
    provider.logger = logging.getLogger("test.sendspin.guest")
    provider._client_event_versions = {"c1": 1}
    return provider


async def test_a_guest_capable_player_is_approved_on_connect() -> None:
    """The device plays without any setup step, so nothing is left for the user to decide."""
    api = _ApprovalServerApi()
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(), 1)

    assert api.trusted == ["c1"]


async def test_a_combo_with_an_audio_input_is_approved_too() -> None:
    """Its playback roles still benefit, even though the input itself needs pairing."""
    api = _ApprovalServerApi()
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(roles=("player@v1", "source@v1")), 1)

    assert api.trusted == ["c1"]


async def test_a_capture_only_device_is_left_to_pair() -> None:
    """Guest access grants a device whose every role needs pairing precisely nothing."""
    api = _ApprovalServerApi()
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(roles=("source@v1",)), 1)

    assert api.trusted == []


async def test_a_device_not_offering_guest_access_is_left_alone() -> None:
    """Approving a device that never offered it would grant access it did not consent to."""
    api = _ApprovalServerApi()
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(guest=False), 1)

    assert api.trusted == []


async def test_an_existing_approval_is_not_rewritten() -> None:
    """A second connect must not churn the store for a device already approved."""
    api = _ApprovalServerApi()
    await api.pairing_store.add_trusted_unpaired(TrustedUnpairedClient(client_id="c1"))
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(), 1)

    assert api.trusted == []


async def test_a_live_pairing_skips_the_approval() -> None:
    """A paired device needs no guest access; its long-term handshake already carries it."""
    api = _ApprovalServerApi()
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(psk_category=PskCategory.LONG_TERM), 1)

    assert api.trusted == []


async def test_a_stale_record_does_not_block_a_guest_handshake() -> None:
    """
    A client that lost its half reconnects as a guest, and must still be approved.

    The server-side record outlives that loss, so keying the decision on the record
    would leave the device unusable with no way back.
    """
    api = _ApprovalServerApi()
    psk = generate_psk()
    await api.pairing_store.store_record(
        ServerPairingRecord(
            psk_id=psk_id_for(psk), psk=psk, client_id="c1", pair_methods=[], owner=None
        )
    )
    provider = _make_provider(api)

    await provider._auto_trust_guest_access("c1", _client(), 1)

    assert api.trusted == ["c1"]


async def test_a_superseded_event_grants_nothing() -> None:
    """The store reads suspend, so a reconnect in between invalidates the hello behind this."""
    api = _ApprovalServerApi()
    provider = _make_provider(api)
    provider._client_event_versions["c1"] = 2

    await provider._auto_trust_guest_access("c1", _client(), 1)

    assert api.trusted == []
