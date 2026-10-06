"""Tests for a Sendspin client that can no longer use the pairing this server holds."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from aiosendspin.models.types import PairMethod
from aiosendspin.server import ClientCredentialMismatchEvent

from music_assistant.providers.sendspin.constants import CONF_ACTION_UNPAIR

from .test_pairing_eviction import _EvictionServerApi, _make_provider, _record
from .test_setup_flow import _desc, _FakeApi, _FakeProvider, _make_player

if TYPE_CHECKING:
    import pytest
    from aiosendspin.server import SendspinServer

    from music_assistant.providers.sendspin.provider import SendspinProvider


async def test_credential_mismatch_refreshes_the_player_and_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The player is re-evaluated for re-pairing, and the record stays for it to replace."""
    api = _EvictionServerApi()
    record = _record("c1")
    await api.pairing_store.store_record(record)
    provider, refreshed = _make_provider(api, monkeypatch)

    with caplog.at_level(logging.INFO):
        provider.event_cb(cast("SendspinServer", api), ClientCredentialMismatchEvent("c1"))
        await asyncio.sleep(0)

    assert refreshed == ["c1"]
    assert await api.pairing_store.record_by_client_id("c1") == record
    assert not [entry for entry in caplog.records if entry.levelno >= logging.WARNING]


async def test_settings_name_the_lost_pairing_and_offer_unpair() -> None:
    """A device that lost its pairing says so and can drop it, instead of claiming guest access."""
    api = _FakeApi([_desc(PairMethod.DYNAMIC_PAIRING_CODE)], unpaired_access=True)
    provider = _FakeProvider(api, record=object(), trusted=object())
    player = _make_player(api, provider)

    status, actions = await player._security_state_entries(cast("SendspinProvider", provider))

    assert status is not None
    assert status.key == "security_status_pairing_lost"
    assert [entry.key for entry in actions] == [CONF_ACTION_UNPAIR]
