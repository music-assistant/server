"""Shared fakes for the Sendspin Source provider tests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from music_assistant.providers.sendspin_source.constants import (
    CONF_TARGET_LATENCY,
    DEFAULT_TARGET_LATENCY_MS,
)
from music_assistant.providers.sendspin_source.provider import SendspinSourceProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant


class _FakeSourceRole:
    """Source role stand-in recording start/stop commands."""

    def __init__(self) -> None:
        self.start_requests = 0
        self.stop_requests = 0
        self.stream_active = False

    def request_start(self) -> None:
        self.start_requests += 1

    def request_stop(self) -> None:
        self.stop_requests += 1


class _FakeClient:
    """Server-side SendspinClient stand-in."""

    def __init__(
        self,
        client_id: str,
        *,
        name: str | None = None,
        has_source_role: bool = True,
        connected: bool = True,
    ) -> None:
        self.client_id = client_id
        self.info_or_none = None
        if name is not None:
            self.info_or_none = type("Info", (), {"name": name})()
        self.is_connected = connected
        self.source_role = _FakeSourceRole() if has_source_role else None
        self.listeners: list[Callable[[Any, Any], None]] = []

    def roles_by_family(self, family: str) -> list[Any]:
        if family == "source" and self.source_role is not None:
            return [self.source_role]
        return []

    def add_event_listener(self, callback: Callable[[Any, Any], None]) -> Callable[[], None]:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def emit(self, event: Any) -> None:
        for callback in list(self.listeners):
            callback(self, event)


class _FakeServerApi:
    """SendspinServer stand-in serving a fixed client set."""

    def __init__(self, clients: list[_FakeClient]) -> None:
        self._clients = {client.client_id: client for client in clients}
        self.listeners: list[Callable[[Any, Any], None]] = []

    @property
    def connected_clients(self) -> list[_FakeClient]:
        return [c for c in self._clients.values() if c.is_connected]

    def get_client(self, client_id: str) -> _FakeClient | None:
        return self._clients.get(client_id)

    def add_event_listener(self, callback: Callable[[Any, Any], None]) -> Callable[[], None]:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def emit(self, event: Any) -> None:
        for callback in list(self.listeners):
            callback(self, event)


class _FakePlayers:
    """Players controller stand-in recording stop commands."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def cmd_stop(self, player_id: str) -> None:
        self.stopped.append(player_id)


class _FakeMass:
    """MusicAssistant stand-in providing loop, task creation and provider lookup."""

    def __init__(self, sendspin_provider: Any) -> None:
        self.loop = asyncio.get_running_loop()
        self.players = _FakePlayers()
        self._sendspin_provider = sendspin_provider

    def get_provider(self, domain: str) -> Any:
        if domain == "sendspin":
            return self._sendspin_provider
        return None

    def create_task(self, coro: Any) -> asyncio.Task[Any]:
        return self.loop.create_task(coro)


class _FakeConfig:
    """Provider config stand-in with a fixed target latency."""

    instance_id = "sendspin_source"

    def get_value(self, key: str) -> Any:
        assert key == CONF_TARGET_LATENCY
        return DEFAULT_TARGET_LATENCY_MS


def make_provider(clients: list[_FakeClient]) -> SendspinSourceProvider:
    """Build a provider wired to fake mass/server_api around the given clients."""
    provider = SendspinSourceProvider.__new__(SendspinSourceProvider)
    server_api = _FakeServerApi(clients)
    sendspin_provider = type("FakeSendspinProvider", (), {"server_api": server_api})()
    provider.mass = cast("MusicAssistant", _FakeMass(sendspin_provider))
    provider.config = cast("Any", _FakeConfig())
    provider.manifest = cast("Any", type("Manifest", (), {"domain": "sendspin_source"})())
    provider.logger = logging.getLogger("test.sendspin_source")
    return provider


def get_server_api(provider: SendspinSourceProvider) -> _FakeServerApi:
    """Return the fake server api the given provider is wired to."""
    sendspin = cast("Any", provider.mass.get_provider("sendspin"))
    return cast("_FakeServerApi", sendspin.server_api)


def get_players(provider: SendspinSourceProvider) -> _FakePlayers:
    """Return the fake players controller the given provider is wired to."""
    return cast("_FakePlayers", provider.mass.players)


@pytest.fixture
def fake_client() -> _FakeClient:
    """Return a connected client with an active source role."""
    return _FakeClient("client-1", name="Turntable")
