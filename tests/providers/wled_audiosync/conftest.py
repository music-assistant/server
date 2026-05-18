"""Shared fixtures + helpers for WLED Audio Sync plugin-provider tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from music_assistant.providers.wled_audiosync.constants import (
    CONF_DUPLICATE_TRANSMIT,
    CONF_MANUAL_PLAYERS,
    CONF_MULTICAST_TTL,
    CONF_REQUIRE_AUDIOREACTIVE,
    DEFAULT_DUPLICATE_TRANSMIT,
    DEFAULT_REQUIRE_AUDIOREACTIVE,
)
from music_assistant.providers.wled_audiosync.provider import WledAudioSyncProvider
from music_assistant.providers.wled_audiosync.wled_audiosync_bridge import DEFAULT_MULTICAST_TTL

# --- Shared loopback UDP listener ---


class LoopbackUdpListener:
    """Loopback UDP listener that records every received datagram.

    Used by ``test_transport.py`` and ``test_integration.py`` for
    asserting on what reaches the wire. Exposes ``wait_for(count)``
    when tests want to block until a specific number of packets has
    arrived (rather than racing on ``asyncio.sleep``).
    """

    def __init__(self) -> None:
        """Start with an empty receive log and no live transport."""
        self.received: list[bytes] = []
        self._event = asyncio.Event()
        self._transport: asyncio.DatagramTransport | None = None
        self.host: str = ""
        self.port: int = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        """Bind to an ephemeral loopback port and return ``(host, port)``."""
        loop = asyncio.get_running_loop()
        transport, _proto = await loop.create_datagram_endpoint(
            lambda: self._Protocol(self),
            local_addr=(host, port),
        )
        self._transport = transport
        sock = transport.get_extra_info("socket")
        bound_host, bound_port = sock.getsockname()[:2]
        self.host = bound_host
        self.port = bound_port
        return bound_host, bound_port

    async def wait_for(self, count: int, timeout: float = 1.0) -> None:
        """Wait until at least ``count`` packets have been received.

        :raises AssertionError: If the timeout elapses before ``count``
            packets arrive.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.received) < count:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                msg = f"only received {len(self.received)}/{count} packets in {timeout}s"
                raise AssertionError(msg)
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except TimeoutError:
                continue

    def close(self) -> None:
        """Close the underlying datagram endpoint."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    class _Protocol(asyncio.DatagramProtocol):
        """Append every received datagram to the owner's buffer."""

        def __init__(self, owner: LoopbackUdpListener) -> None:
            """Wire the protocol to its owner so it can record packets."""
            self._owner = owner

        def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
            """Record one inbound datagram and signal any wait_for()."""
            self._owner.received.append(data)
            self._owner._event.set()


@pytest_asyncio.fixture
async def listener() -> AsyncIterator[LoopbackUdpListener]:
    """Provide a started loopback UDP listener that's torn down after the test."""
    lis = LoopbackUdpListener()
    await lis.start()
    try:
        yield lis
    finally:
        lis.close()


@pytest.fixture
def provider_config_mock() -> Mock:
    """Return a mock provider config with sensible WLED defaults."""
    cfg = Mock()
    cfg.name = "WLED Audio Sync"
    cfg.instance_id = "wled_audiosync_test"
    cfg.enabled = True
    cfg.get_value = Mock(
        side_effect=lambda key, default=None: {
            CONF_MANUAL_PLAYERS: [],
            CONF_REQUIRE_AUDIOREACTIVE: DEFAULT_REQUIRE_AUDIOREACTIVE,
            CONF_DUPLICATE_TRANSMIT: DEFAULT_DUPLICATE_TRANSMIT,
            CONF_MULTICAST_TTL: DEFAULT_MULTICAST_TTL,
            "log_level": "GLOBAL",  # CONF_LOG_LEVEL — Provider.__init__ reads this
        }.get(key, default)
    )
    return cfg


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a minimal provider manifest mock."""
    m = Mock()
    m.domain = "wled_audiosync"
    m.name = "WLED Audio Sync"
    return m


@pytest.fixture
def sendspin_provider_mock() -> Mock:
    """Return a stub Sendspin provider exposing the bridge-player-type hook."""
    sp = Mock()
    sp.register_bridge_player_type = Mock()
    sp.unregister_bridge_player_type = Mock()
    return sp


@pytest.fixture
def mass_mock(sendspin_provider_mock: Mock) -> Mock:
    """Return a MagicMock posing as MusicAssistant for Provider + bridge tests."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.config.get = Mock(return_value={})
    mass.verify_event_loop_thread = Mock()

    # Sendspin lookup — bridges call mass.get_provider("sendspin").
    def _get_provider(domain: str) -> object | None:
        if domain == "sendspin":
            return sendspin_provider_mock
        return None

    mass.get_provider = Mock(side_effect=_get_provider)

    mass.create_task = Mock(side_effect=lambda coro: asyncio.create_task(coro))
    mass.streams.bind_ip = "127.0.0.1"

    return mass


@pytest.fixture
def provider(
    mass_mock: Mock, manifest_mock: Mock, provider_config_mock: Mock
) -> WledAudioSyncProvider:
    """Return a real WledAudioSyncProvider wired to mocks."""
    prov = WledAudioSyncProvider(mass_mock, manifest_mock, provider_config_mock, set())
    # Provider.__init__ has already wired up self.logger via _set_log_level_from_config,
    # so we just point it at a test logger for noise-free output.
    prov.logger = logging.getLogger("wled_audiosync.test")
    # handle_async_init populates _bridges; do the same here so individual
    # tests can inspect it without first awaiting init.
    prov._bridges = {}
    return prov
