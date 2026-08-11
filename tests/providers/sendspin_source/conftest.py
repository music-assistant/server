"""Shared fakes for the Sendspin Source provider tests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import PlaybackState

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


class _FakeQueue:
    """PlayerQueue stand-in."""

    def __init__(self, queue_id: str, state: PlaybackState = PlaybackState.IDLE) -> None:
        self.queue_id = queue_id
        self.state = state


class _FakePlayer:
    """Player stand-in owning a queue."""

    def __init__(self, player_id: str, state: PlaybackState = PlaybackState.IDLE) -> None:
        self.player_id = player_id
        self.queue = _FakeQueue(player_id, state)


class _FakePlayers:
    """Players controller stand-in recording stop commands."""

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.players: dict[str, _FakePlayer] = {}

    def get_player(self, player_id: str, *args: Any, **kwargs: Any) -> _FakePlayer | None:
        return self.players.get(player_id)

    def get_active_queue(self, player: _FakePlayer) -> _FakeQueue:
        return player.queue

    async def cmd_stop(self, player_id: str) -> None:
        self.stopped.append(player_id)


class _FakePlayerQueues:
    """Queue controller stand-in recording play_media calls."""

    def __init__(self) -> None:
        self.played: list[tuple[str, str, Any]] = []
        self.stopped: list[str] = []

    async def play_media(self, queue_id: str, media: Any, option: Any = None, **_: Any) -> None:
        self.played.append((queue_id, media, option))

    async def stop(self, queue_id: str) -> None:
        self.stopped.append(queue_id)


class _FakeConfigController:
    """Config controller stand-in serving raw player config values."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}

    def get_raw_player_config_value(self, player_id: str, key: str, default: Any = None) -> Any:
        return self.values.get((player_id, key), default)


class _FakeMass:
    """MusicAssistant stand-in providing loop, task creation and provider lookup."""

    def __init__(self, sendspin_provider: Any) -> None:
        self.loop = asyncio.get_running_loop()
        self.players = _FakePlayers()
        self.player_queues = _FakePlayerQueues()
        self.config = _FakeConfigController()
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


async def make_provider(clients: list[_FakeClient]) -> SendspinSourceProvider:
    """Build a provider wired to fake mass/server_api around the given clients."""
    provider = SendspinSourceProvider.__new__(SendspinSourceProvider)
    server_api = _FakeServerApi(clients)
    sendspin_provider = type("FakeSendspinProvider", (), {"server_api": server_api})()
    provider.mass = cast("MusicAssistant", _FakeMass(sendspin_provider))
    provider.config = cast("Any", _FakeConfig())
    provider.manifest = cast("Any", type("Manifest", (), {"domain": "sendspin_source"})())
    provider.logger = logging.getLogger("test.sendspin_source")
    provider._sessions = {}
    provider._watchers = {}
    provider._signals = {}
    provider._pending_autostart = {}
    provider._server_unsubscribe = None
    for client in clients:
        get_players(provider).players[client.client_id] = _FakePlayer(client.client_id)
    await provider.loaded_in_mass()
    return provider


def get_config(provider: SendspinSourceProvider) -> _FakeConfigController:
    """Return the fake config controller the given provider is wired to."""
    return cast("_FakeConfigController", provider.mass.config)


def get_queues(provider: SendspinSourceProvider) -> _FakePlayerQueues:
    """Return the fake queue controller the given provider is wired to."""
    return cast("_FakePlayerQueues", provider.mass.player_queues)


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
