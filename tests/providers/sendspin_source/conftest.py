"""Shared fakes for the Sendspin Source provider tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import PlaybackState

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
        self.negotiated_role_ids = ["source@v1"] if has_source_role else ["player@v1"]
        self.source_role = _FakeSourceRole() if has_source_role else None
        self.listeners: list[Callable[[Any, Any], None]] = []

    def roles_by_family(self, family: str) -> list[Any]:
        if family == "source" and self.source_role is not None:
            return [self.source_role]
        return []

    def detach_roles(self) -> _FakeSourceRole | None:
        """Drop the role instances, as a cold reconnect does before re-attaching them."""
        role, self.source_role = self.source_role, None
        return role

    def attach_roles(self, role: _FakeSourceRole | None) -> None:
        """Re-attach role instances, which the server does after signalling connected."""
        self.source_role = role

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
        self.stop_started: asyncio.Event | None = None
        self.release_stop: asyncio.Event | None = None
        # live source sessions, keyed on the player playing one
        self.source_sessions: dict[str, Any] = {}
        self.deselected: list[str] = []

    def get_player(self, player_id: str, *args: Any, **kwargs: Any) -> _FakePlayer | None:
        return self.players.get(player_id)

    def get_active_queue(self, player: _FakePlayer) -> _FakeQueue:
        return player.queue

    def get_audio_source_session(self, player_id: str) -> Any:
        return self.source_sessions.get(player_id)

    async def deselect_source(
        self,
        player_id: str,
        stop_playback: bool = True,
        provider_instance_id: str | None = None,
    ) -> None:
        self.deselected.append(player_id)
        if stop_playback:
            await self.cmd_stop(player_id)

    async def cmd_stop(self, player_id: str) -> None:
        self.stopped.append(player_id)
        if self.stop_started is not None:
            self.stop_started.set()
        if self.release_stop is not None:
            await self.release_stop.wait()


class _FakePlayerQueues:
    """Queue controller stand-in recording play_media calls."""

    def __init__(self) -> None:
        self.played: list[tuple[str, str, Any]] = []
        self.stopped: list[str] = []
        self.play_started: asyncio.Event | None = None
        self.release_play: asyncio.Event | None = None
        self.stop_started: asyncio.Event | None = None
        self.release_stop: asyncio.Event | None = None
        self._sessions: dict[str, str | None] = {}
        self._session_counter = 0

    async def play_media(self, queue_id: str, media: Any, option: Any = None, **_: Any) -> None:
        self._session_counter += 1
        self._sessions[queue_id] = f"session-{self._session_counter}"
        if self.play_started is not None:
            self.play_started.set()
        if self.release_play is not None:
            await self.release_play.wait()
        self.played.append((queue_id, media, option))

    async def stop(self, queue_id: str) -> None:
        self.stopped.append(queue_id)
        if self.stop_started is not None:
            self.stop_started.set()
        if self.release_stop is not None:
            await self.release_stop.wait()
        self._sessions[queue_id] = None

    def queue_data(self, queue_id: str) -> Any:
        return SimpleNamespace(session_id=self._sessions.get(queue_id))


class _FakeConfigController:
    """Config controller stand-in separating stored values from entry defaults."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}
        self.defaults: dict[tuple[str, str], Any] = {}

    def get_raw_player_config_value(self, player_id: str, key: str, default: Any = None) -> Any:
        return self.values.get((player_id, key), default)

    async def get_player_config_value(
        self, player_id: str, key: str, *, default: Any = None
    ) -> Any:
        if (stored := self.values.get((player_id, key))) is not None:
            return stored
        return self.defaults.get((player_id, key), default)


class _FakeMass:
    """MusicAssistant stand-in providing loop, task creation and provider lookup."""

    cache = None

    def __init__(self, sendspin_provider: Any) -> None:
        self.loop = asyncio.get_running_loop()
        self.players = _FakePlayers()
        self.player_queues = _FakePlayerQueues()
        self.config = _FakeConfigController()
        self._sendspin_provider = sendspin_provider
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def get_provider(self, domain: str) -> Any:
        if domain == "sendspin":
            return self._sendspin_provider
        return None

    def create_task(
        self,
        coro: Any,
        *,
        task_id: str | None = None,
        abort_existing: bool = False,
        eager_start: bool = True,
    ) -> asyncio.Task[Any]:
        # Mirror the real controller's eager default, which decides whether a task
        # body runs inside the event callback that created it.
        if task_id is not None and (existing := self._tasks.get(task_id)) is not None:
            if not abort_existing:
                coro.close()
                return existing
            existing.cancel()
        task = asyncio.Task(coro, loop=self.loop, eager_start=eager_start)
        if task_id is not None:
            self._tasks[task_id] = task
            task.add_done_callback(
                lambda completed: (
                    self._tasks.pop(task_id, None)
                    if self._tasks.get(task_id) is completed
                    else None
                )
            )
        return task

    def call_later(
        self,
        delay: float,
        target: Callable[..., Any],
        *args: Any,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> asyncio.TimerHandle:
        timer_id = task_id or str(id(target))
        self.cancel_timer(timer_id)

        def run() -> None:
            self._timers.pop(timer_id, None)
            self.create_task(target(*args, **kwargs), task_id=timer_id, abort_existing=True)

        handle = self.loop.call_later(delay, run)
        self._timers[timer_id] = handle
        return handle

    def cancel_timer(self, task_id: str) -> None:
        if (timer := self._timers.pop(task_id, None)) is not None:
            timer.cancel()

    def get_task(self, task_id: str) -> asyncio.Task[Any] | None:
        return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> None:
        if (task := self._tasks.pop(task_id, None)) is not None:
            task.cancel()


class _FakeConfig:
    """
    Provider config stand-in.

    Values default to unset, as they are at load time: a provider's own entries are
    only resolved once its instance is loaded, so nothing has been parsed yet.
    """

    instance_id = "sendspin_source"

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}

    def get_value(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


async def make_provider(
    clients: list[_FakeClient], config_values: dict[str, Any] | None = None
) -> SendspinSourceProvider:
    """Build a provider wired to fake mass/server_api around the given clients."""
    server_api = _FakeServerApi(clients)
    sendspin_provider = type("FakeSendspinProvider", (), {"server_api": server_api})()
    # Constructed rather than hand-populated, so a new instance attribute cannot go
    # missing here and take the fake out of step with the provider.
    provider = SendspinSourceProvider(
        cast("MusicAssistant", _FakeMass(sendspin_provider)),
        cast("Any", type("Manifest", (), {"domain": "sendspin_source"})()),
        cast("Any", _FakeConfig(config_values)),
    )
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
