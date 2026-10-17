"""Checks that the test-suite guards from CLAUDE.local.md are active."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.player import playback_lock
from music_assistant.providers.web_kiosk.provider import WebKioskProvider

from .conftest import LockOrder


async def test_lock_order_reports_a_playback_lock_inside_the_player_lock(
    provider: WebKioskProvider, mass_mock: Mock, lock_order: LockOrder
) -> None:
    """Taking the MA playback lock while holding the provider lock is an inversion."""
    async with provider._player_lock, playback_lock(mass_mock, "wk_test"):
        pass

    assert lock_order.violations == ["playback lock taken while holding player lock"]
    lock_order.violations.clear()


async def test_lock_order_reports_any_lock_inside_the_outbound_channel(
    provider: WebKioskProvider, lock_order: LockOrder
) -> None:
    """The outbound channel is a leaf: no other lock inside it."""
    server = WebKioskHTTPServer(provider, 0)
    async with server._outbound_lock("wk_test"), provider._player_lock:
        pass

    assert lock_order.violations == ["player lock taken while holding outbound"]
    lock_order.violations.clear()


async def test_lock_order_accepts_the_documented_order(
    provider: WebKioskProvider, mass_mock: Mock, lock_order: LockOrder
) -> None:
    """Playback lock, then provider lock, then the outbound channel is allowed."""
    server = WebKioskHTTPServer(provider, 0)
    async with (
        playback_lock(mass_mock, "wk_test"),
        provider._player_lock,
        server._outbound_lock("wk_test"),
    ):
        pass

    assert lock_order.violations == []


async def test_lock_order_is_tracked_per_task(
    provider: WebKioskProvider, mass_mock: Mock, lock_order: LockOrder
) -> None:
    """A lock held by another task does not count for this one."""
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_provider_lock() -> None:
        async with provider._player_lock:
            held.set()
            await release.wait()

    holder = asyncio.create_task(hold_provider_lock())
    await held.wait()
    async with playback_lock(mass_mock, "wk_test"):
        pass
    release.set()
    await holder

    assert lock_order.violations == []


async def test_lock_order_counts_an_ma_command_as_the_playback_lock(
    provider: WebKioskProvider, mass_mock: Mock, lock_order: LockOrder
) -> None:
    """Calling an MA playback command under the provider lock is an inversion."""
    async with provider._player_lock:
        await mass_mock.players.cmd_stop("wk_test")

    assert lock_order.violations == ["playback lock taken while holding player lock"]
    lock_order.violations.clear()
