"""Tests for a Sendspin client that can no longer use the pairing this server holds."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from aiosendspin.server import ClientCredentialMismatchEvent

from .test_pairing_eviction import _EvictionServerApi, _make_provider, _record

if TYPE_CHECKING:
    import pytest
    from aiosendspin.server import SendspinServer


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
