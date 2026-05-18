"""Tests for the WLED Audio Sync provider's discovery filtering."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from zeroconf import ServiceStateChange

from music_assistant.providers.wled_audiosync.constants import (
    CONF_MANUAL_PLAYERS,
    CONF_REQUIRE_AUDIOREACTIVE,
    DEFAULT_REQUIRE_AUDIOREACTIVE,
)
from music_assistant.providers.wled_audiosync.provider import (
    info_has_audioreactive,
    probe_audioreactive,
)

if TYPE_CHECKING:
    from music_assistant.providers.wled_audiosync.provider import (
        WledAudioSyncProvider,
    )

# A reduced /json/info payload from a real WLED-MM device (only the bits we
# care about for detection; full payloads include dozens more fields).
_REAL_WLED_MM_INFO: dict[str, Any] = {
    "ver": "16.0.0",
    "vid": 2605030,
    "brand": "WLED",
    "product": "FOSS",
    "mac": "20e7c86ac540",
    "u": {
        "AudioReactive": ["<button …/>"],
        "GEQ Input Level": ["<div …/>"],
        "Audio Source": ["PDM digital"],
        "Sound Processing": ["running"],
        "AGC Gain": [5.21, "x"],
        "UDP Sound Sync": ["off"],
    },
}

# A vanilla WLED with no usermods loaded.
_VANILLA_WLED_INFO: dict[str, Any] = {
    "ver": "15.2",
    "vid": 2502080,
    "brand": "WLED",
    "product": "FOSS",
    "mac": "abcdef012345",
    "u": {},
}

# WLED with a usermod loaded, but not AudioReactive.
_OTHER_USERMOD_WLED_INFO: dict[str, Any] = {
    "ver": "15.2",
    "vid": 2502080,
    "brand": "WLED",
    "u": {"PIR sensor": ["motion"], "Temperature": ["20C"]},
}


# --- Pure-function tests for the detection rule ---


def test_info_has_audioreactive_accepts_real_mm_payload() -> None:
    """The real-device payload from wled-6ac540 must register as AudioReactive."""
    assert info_has_audioreactive(_REAL_WLED_MM_INFO) is True


def test_info_has_audioreactive_rejects_vanilla_wled() -> None:
    """Vanilla WLED with no usermods has an empty `u` dict."""
    assert info_has_audioreactive(_VANILLA_WLED_INFO) is False


def test_info_has_audioreactive_rejects_other_usermods() -> None:
    """A WLED running PIR/Temperature usermods but not AudioReactive is rejected."""
    assert info_has_audioreactive(_OTHER_USERMOD_WLED_INFO) is False


def test_info_has_audioreactive_handles_missing_usermods_key() -> None:
    """If the `u` field is absent entirely, we must treat it as no usermods."""
    assert info_has_audioreactive({"ver": "15.2", "brand": "WLED"}) is False


def test_info_has_audioreactive_handles_non_dict_usermods() -> None:
    """A malformed `u` field that isn't a dict should not crash the detector."""
    assert info_has_audioreactive({"u": "not-a-dict"}) is False
    assert info_has_audioreactive({"u": None}) is False
    assert info_has_audioreactive({"u": []}) is False


# --- HTTP-probe integration tests against a real aiohttp test server ---


FakeWledFactory = Callable[..., Awaitable["FakeWledHandle"]]


class FakeWledHandle:
    """Captured server URL and the per-test cleanup hook."""

    def __init__(self, server: TestServer) -> None:
        """Wrap the running TestServer so tests can pluck its host:port."""
        self.server = server

    @property
    def address(self) -> str:
        """Return ``host:port`` as probe_audioreactive expects."""
        return f"{self.server.host}:{self.server.port}"


@pytest_asyncio.fixture
async def http_session() -> AsyncIterator[aiohttp.ClientSession]:
    """Provide a fresh aiohttp session that's closed when the test exits."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest_asyncio.fixture
async def fake_wled() -> AsyncIterator[FakeWledFactory]:
    """Spin up a TestServer that returns a configurable /json/info payload."""
    spawned: list[TestServer] = []

    async def factory(
        *,
        info_json: Any = _REAL_WLED_MM_INFO,
        status: int = 200,
        delay: float = 0.0,
        return_html: bool = False,
        return_invalid_json: bool = False,
    ) -> FakeWledHandle:
        async def handler(_request: web.Request) -> web.Response:
            if delay:
                await asyncio.sleep(delay)
            if return_invalid_json:
                return web.Response(status=status, body=b"not-json")
            if return_html:
                return web.Response(
                    status=status,
                    body=b"<html>WLED</html>",
                    content_type="text/html",
                )
            return web.json_response(info_json, status=status)

        app = web.Application()
        app.router.add_get("/json/info", handler)
        server = TestServer(app)
        await server.start_server()
        spawned.append(server)
        return FakeWledHandle(server)

    try:
        yield factory
    finally:
        for s in spawned:
            await s.close()


async def test_probe_returns_true_for_real_payload(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """A real-device payload over real HTTP must return True."""
    handle = await fake_wled(info_json=_REAL_WLED_MM_INFO)
    assert await probe_audioreactive(http_session, handle.address) is True


async def test_probe_returns_false_for_vanilla_wled(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """A vanilla WLED payload over real HTTP must return False."""
    handle = await fake_wled(info_json=_VANILLA_WLED_INFO)
    assert await probe_audioreactive(http_session, handle.address) is False


async def test_probe_returns_false_on_404(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """Non-200 HTTP statuses are treated as 'not AudioReactive-capable'."""
    handle = await fake_wled(status=404)
    assert await probe_audioreactive(http_session, handle.address) is False


async def test_probe_returns_false_on_500(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """Server errors are also treated as 'not capable' rather than retried."""
    handle = await fake_wled(status=500)
    assert await probe_audioreactive(http_session, handle.address) is False


async def test_probe_returns_false_when_response_is_not_json(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """If the body isn't parseable JSON, treat as not capable."""
    handle = await fake_wled(return_invalid_json=True)
    assert await probe_audioreactive(http_session, handle.address) is False


async def test_probe_returns_false_when_response_is_html(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """An HTML response (wrong content type) must not be mistaken for a match."""
    handle = await fake_wled(return_html=True)
    assert await probe_audioreactive(http_session, handle.address) is False


async def test_probe_returns_false_on_timeout(
    http_session: aiohttp.ClientSession, fake_wled: FakeWledFactory
) -> None:
    """If the device doesn't respond within the timeout, return False."""
    handle = await fake_wled(delay=1.5)
    assert await probe_audioreactive(http_session, handle.address, timeout=0.1) is False


async def test_probe_returns_false_when_unreachable(
    http_session: aiohttp.ClientSession,
) -> None:
    """Probing an address with no listener should return False, not raise."""
    # 127.0.0.1:1 is reserved/unused — connection refused immediately.
    assert await probe_audioreactive(http_session, "127.0.0.1:1", timeout=2.0) is False


# --- Bridge registration via handle_async_init + mDNS ---
#
# The provider doesn't register MA Players directly any more — it manages
# WledAudioSyncBridge instances that connect to MA's local Sendspin server
# as VISUALIZER clients. For unit tests we patch out bridge.start() so we
# don't try to open a real Sendspin WebSocket; we just observe which
# bridges the provider would have created.


def _set_manual_players(provider_config_mock: Mock, entries: list[str]) -> None:
    """Patch the provider config mock to return the given manual_players entries."""
    config_values = {
        CONF_MANUAL_PLAYERS: entries,
        CONF_REQUIRE_AUDIOREACTIVE: DEFAULT_REQUIRE_AUDIOREACTIVE,
        "log_level": "GLOBAL",
    }
    provider_config_mock.get_value = Mock(
        side_effect=lambda key, default=None: config_values.get(key, default)
    )


def _patch_bridge_start() -> AbstractContextManager[AsyncMock]:
    """Patch WledAudioSyncBridge.start so the unit tests never open a Sendspin client."""
    return patch(
        "music_assistant.providers.wled_audiosync.bridge.WledAudioSyncBridge.start",
        new=AsyncMock(),
    )


async def test_handle_async_init_creates_a_bridge_per_manual_entry(
    provider: WledAudioSyncProvider,
    provider_config_mock: Mock,
) -> None:
    """Every well-formed 'name=address' entry produces one bridge."""
    _set_manual_players(
        provider_config_mock,
        ["Living Room=192.168.1.50", "Multicast=239.0.0.1"],
    )
    with _patch_bridge_start():
        await provider.handle_async_init()
    assert set(provider.bridges) == {"wled_manual_living_room", "wled_manual_multicast"}
    living_room = provider.bridges["wled_manual_living_room"]
    multicast = provider.bridges["wled_manual_multicast"]
    assert living_room.destination_address == "192.168.1.50"
    assert multicast.destination_address == "239.0.0.1"


async def test_handle_async_init_skips_malformed_manual_entries(
    provider: WledAudioSyncProvider,
    provider_config_mock: Mock,
) -> None:
    """Entries lacking '=' or with empty name/address are logged + skipped."""
    _set_manual_players(
        provider_config_mock,
        [
            "no-equals-sign",
            "=onlyaddress",
            "onlyname=",
            "   = leading whitespace",
        ],
    )
    with _patch_bridge_start():
        await provider.handle_async_init()
    assert provider.bridges == {}


async def test_duplicate_manual_entries_collapse_to_one_bridge(
    provider: WledAudioSyncProvider,
    provider_config_mock: Mock,
) -> None:
    """Two manual entries with the same name collapse to one bridge (last-write wins)."""
    _set_manual_players(
        provider_config_mock,
        ["TestRoom=10.0.0.5", "TestRoom=10.0.0.99"],
    )
    with _patch_bridge_start():
        await provider.handle_async_init()
    # Same slugified client_id → single registry entry.
    assert list(provider.bridges) == ["wled_manual_testroom"]
    # The second entry's address replaces the first via set_destination.
    assert provider.bridges["wled_manual_testroom"].destination_address == "10.0.0.99"


# --- on_mdns_service_state_change ---


def _make_service_info(name: str, ipv4: str | None) -> Mock:
    """Build a minimal AsyncServiceInfo-like mock matching what the provider reads."""
    info = Mock()
    info.name = name
    if ipv4 is not None:
        info.ip_addresses_by_version = Mock(
            side_effect=lambda _version: [ipaddress.IPv4Address(ipv4)]
        )
    else:
        info.ip_addresses_by_version = Mock(return_value=[])
    return info


async def test_on_mdns_removed_stops_existing_bridge(
    provider: WledAudioSyncProvider,
) -> None:
    """A ServiceStateChange.Removed event stops + drops the matching bridge."""
    bridge = Mock()
    bridge.stop = AsyncMock()
    provider._bridges["wled_wled_bedroom"] = bridge
    await provider.on_mdns_service_state_change(
        name="wled-bedroom._wled._tcp.local.",
        state_change=ServiceStateChange.Removed,
        info=None,
    )
    bridge.stop.assert_awaited_once()
    assert "wled_wled_bedroom" not in provider.bridges


async def test_on_mdns_added_creates_bridge_after_successful_probe(
    provider: WledAudioSyncProvider,
) -> None:
    """A new device with a passing /json/info probe gets bridged."""
    info = _make_service_info("wled-livingroom._wled._tcp.local.", "192.168.1.50")
    with (
        patch(
            "music_assistant.providers.wled_audiosync.provider.probe_audioreactive",
            new=AsyncMock(return_value=True),
        ),
        _patch_bridge_start(),
    ):
        await provider.on_mdns_service_state_change(
            name="wled-livingroom._wled._tcp.local.",
            state_change=ServiceStateChange.Added,
            info=info,
        )
    assert "wled_wled_livingroom" in provider.bridges
    assert provider.bridges["wled_wled_livingroom"].destination_address == "192.168.1.50"


async def test_on_mdns_added_skips_when_probe_fails(
    provider: WledAudioSyncProvider,
) -> None:
    """A device whose /json/info probe fails is not bridged."""
    info = _make_service_info("wled-bedroom._wled._tcp.local.", "192.168.1.99")
    with (
        patch(
            "music_assistant.providers.wled_audiosync.provider.probe_audioreactive",
            new=AsyncMock(return_value=False),
        ),
        _patch_bridge_start(),
    ):
        await provider.on_mdns_service_state_change(
            name="wled-bedroom._wled._tcp.local.",
            state_change=ServiceStateChange.Added,
            info=info,
        )
    assert provider.bridges == {}


async def test_on_mdns_added_skips_probe_when_require_audioreactive_disabled(
    provider: WledAudioSyncProvider,
    provider_config_mock: Mock,
) -> None:
    """With require_audioreactive=False, vanilla WLEDs are bridged too."""
    provider_config_mock.get_value = Mock(
        side_effect=lambda key, default=None: {
            CONF_MANUAL_PLAYERS: [],
            CONF_REQUIRE_AUDIOREACTIVE: False,
            "log_level": "GLOBAL",
        }.get(key, default)
    )
    info = _make_service_info("wled-kitchen._wled._tcp.local.", "192.168.1.10")
    probe_mock = AsyncMock(return_value=False)
    with (
        patch(
            "music_assistant.providers.wled_audiosync.provider.probe_audioreactive",
            new=probe_mock,
        ),
        _patch_bridge_start(),
    ):
        await provider.on_mdns_service_state_change(
            name="wled-kitchen._wled._tcp.local.",
            state_change=ServiceStateChange.Added,
            info=info,
        )
    probe_mock.assert_not_awaited()
    assert "wled_wled_kitchen" in provider.bridges


async def test_on_mdns_skips_when_info_missing_ipv4(
    provider: WledAudioSyncProvider,
) -> None:
    """If we can't extract an IPv4 from the discovery info, skip silently."""
    info = _make_service_info("wled-noaddr._wled._tcp.local.", ipv4=None)
    with (
        patch(
            "music_assistant.providers.wled_audiosync.provider.probe_audioreactive",
            new=AsyncMock(return_value=True),
        ),
        _patch_bridge_start(),
    ):
        await provider.on_mdns_service_state_change(
            name="wled-noaddr._wled._tcp.local.",
            state_change=ServiceStateChange.Added,
            info=info,
        )
    assert provider.bridges == {}


async def test_on_mdns_updated_refreshes_existing_bridge_destination(
    provider: WledAudioSyncProvider,
) -> None:
    """A re-announcement for an already-registered bridge updates its destination."""
    existing = Mock()
    existing.set_destination = Mock()
    provider._bridges["wled_wled_bedroom"] = existing
    info = _make_service_info("wled-bedroom._wled._tcp.local.", "192.168.1.77")
    probe_mock = AsyncMock(return_value=True)
    with (
        patch(
            "music_assistant.providers.wled_audiosync.provider.probe_audioreactive",
            new=probe_mock,
        ),
        _patch_bridge_start(),
    ):
        await provider.on_mdns_service_state_change(
            name="wled-bedroom._wled._tcp.local.",
            state_change=ServiceStateChange.Updated,
            info=info,
        )
    existing.set_destination.assert_called_once_with("192.168.1.77", 11988)
    probe_mock.assert_not_awaited()


async def test_on_mdns_ignores_empty_hostname(
    provider: WledAudioSyncProvider,
) -> None:
    """A service name with no leading hostname is dropped cleanly."""
    with _patch_bridge_start():
        await provider.on_mdns_service_state_change(
            name=".",  # split(".", maxsplit=1)[0] == ""
            state_change=ServiceStateChange.Added,
            info=_make_service_info(".", "192.168.1.50"),
        )
    assert provider.bridges == {}
