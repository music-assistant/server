"""Fixtures for testing the Web Kiosk Provider."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.enums import PlayerType

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider

# Lock hierarchy from CLAUDE.local.md: a task holding a lock never takes an
# earlier one. Every test checks it through the lock_order fixture.
PLAYBACK_LOCK, PLAYER_LOCK, OUTBOUND_LOCK = 1, 2, 3
_LOCK_NAMES = {
    PLAYBACK_LOCK: "playback lock",
    PLAYER_LOCK: "player lock",
    OUTBOUND_LOCK: "outbound",
}


class LockOrder:
    """Record lock ranks held by each task and every out-of-order acquisition."""

    def __init__(self) -> None:
        """Start with no locks held."""
        self.held: dict[asyncio.Task[Any] | None, list[int]] = {}
        self.violations: list[str] = []

    @asynccontextmanager
    async def hold(self, rank: int, lock: AbstractAsyncContextManager[Any]) -> AsyncIterator[None]:
        """Acquire a lock of the given rank, recording an inversion."""
        stack = self.held.setdefault(asyncio.current_task(), [])
        # MA's playback lock is re-entrant per task: taking it again is no wait.
        if stack and max(stack) > rank and rank not in stack:
            self.violations.append(
                f"{_LOCK_NAMES[rank]} taken while holding {_LOCK_NAMES[max(stack)]}"
            )
        async with lock:
            stack.append(rank)
            try:
                yield
            finally:
                stack.remove(rank)


LOCK_ORDER = LockOrder()


class TrackedLock:
    """An asyncio.Lock whose acquisitions are checked against the hierarchy."""

    def __init__(self, lock: asyncio.Lock, rank: int) -> None:
        """Wrap a lock of the given rank."""
        self._lock = lock
        self._rank = rank
        self._held: list[AbstractAsyncContextManager[None]] = []

    def locked(self) -> bool:
        """Return whether the wrapped lock is held."""
        return self._lock.locked()

    async def __aenter__(self) -> None:
        """Acquire the lock, recording an inversion."""
        holder = LOCK_ORDER.hold(self._rank, self._lock)
        await holder.__aenter__()
        self._held.append(holder)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock."""
        await self._held.pop().__aexit__(exc_type, exc, traceback)


@pytest.fixture
def player_config_mock() -> Mock:
    """Return a mock PlayerConfig as returned by get_base_player_config()."""
    player_config = Mock()
    player_config.name = None
    player_config.default_name = None
    player_config.enabled = True
    player_config.player_type = PlayerType.PLAYER
    player_config.get_value = Mock(return_value=None)
    return player_config


@pytest.fixture
def mass_mock(player_config_mock: Mock) -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    mass.cache = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.closing = False
    mass.version = "2.17.0.dev0"
    mass.create_task = Mock(side_effect=lambda coro, *_args, **_kwargs: asyncio.create_task(coro))

    # Player.__init__ deps
    mass.config.create_default_player_config = Mock()
    mass.config.get_base_player_config = Mock(return_value=player_config_mock)
    mass.config.get_raw_player_config_value = Mock(return_value="stereo")
    mass.config.get_player_dsp_config = Mock()
    mass.config.get = Mock(return_value={})
    mass.verify_event_loop_thread = Mock()

    # Library API
    mass.music.albums.library_items = AsyncMock(return_value=[])
    mass.music.albums.tracks = AsyncMock(return_value=[])
    mass.music.artists.library_items = AsyncMock(return_value=[])
    mass.music.artists.albums = AsyncMock(return_value=[])
    mass.music.playlists.library_items = AsyncMock(return_value=[])
    mass.music.playlists.tracks = Mock(side_effect=lambda *_args, **_kwargs: _empty_async_gen())
    mass.music.tracks.library_items = AsyncMock(return_value=[])
    mass.music.search = AsyncMock(return_value=Mock(artists=[], albums=[], tracks=[], playlists=[]))

    # Playback control
    mass.player_queues.play_media = _playback_command()
    mass.player_queues.items = Mock(return_value=[])
    mass.player_queues.get = Mock(return_value=None)
    mass.player_queues.get_next_item = Mock(return_value=None)
    # MA playback commands take the playback lock; the mocks count as taking it.
    mass.player_queues.resume = _playback_command()
    mass.player_queues.play_index = _playback_command()
    mass.players.cmd_pause = _playback_command()
    mass.players.cmd_play = _playback_command()
    mass.players.cmd_stop = _playback_command()
    mass.players.cmd_seek = _playback_command()
    mass.players.get_group_and_player_lock = _playback_locks()
    mass.players.get = Mock(return_value=None)
    mass.players.get_player = Mock(return_value=None)
    mass.players.register = AsyncMock()
    mass.players.unregister = AsyncMock()
    mass.players.all = Mock(return_value=[])
    mass.players.all_players = Mock(return_value=[])
    mass.players.iter_players = Mock(return_value=[])

    # Stream URL resolution
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma.local:8095/stream/1")

    # Image URLs
    mass.metadata.get_image_url = Mock(return_value=None)

    # Other providers (Sendspin, Party) are absent by default
    mass.get_provider = Mock(return_value=None)
    mass.command_handlers = {}

    return mass


def _playback_command() -> AsyncMock:
    """Return a mocked MA playback command that takes the playback lock like the real one."""

    async def command(*_args: object, **_kwargs: object) -> None:
        async with LOCK_ORDER.hold(PLAYBACK_LOCK, contextlib.nullcontext()):
            return

    return AsyncMock(side_effect=command)


def _playback_locks() -> Callable[..., AbstractAsyncContextManager[None]]:
    """Return per-player playback locks, re-entrant per task like the player controller's."""
    playback_locks: dict[str, asyncio.Lock] = {}
    lock_owners: dict[str, asyncio.Task[Any] | None] = {}

    @asynccontextmanager
    async def playback_lock(player_id: str, **_kwargs: object) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if lock_owners.get(player_id) is task:
            yield
            return
        async with LOCK_ORDER.hold(
            PLAYBACK_LOCK, playback_locks.setdefault(player_id, asyncio.Lock())
        ):
            lock_owners[player_id] = task
            try:
                yield
            finally:
                lock_owners[player_id] = None

    return playback_lock


async def _empty_async_gen() -> AsyncGenerator[Any]:
    """Empty async generator for mocking AsyncGenerator return types."""
    return
    yield  # type: ignore[unreachable]  # pragma: no cover — makes it a generator


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "web_kiosk"
    manifest.name = "Web Kiosk"
    manifest.type = Mock()
    manifest.stage = Mock()
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Web Kiosk"
    config.instance_id = "web_kiosk_test"
    config.enabled = True
    config.get_value = Mock(
        side_effect=lambda key, default=None: {
            "http_port": 8098,
            "output_format": "mp3",
            "log_level": "GLOBAL",
            "enable_sendspin_bridge": True,
        }.get(key, default)
    )
    return config


@pytest.fixture
def provider(mass_mock: Mock, manifest_mock: Mock, config_mock: Mock) -> WebKioskProvider:
    """Return a WebKioskProvider instance without a real HTTP server."""
    prov = WebKioskProvider(mass_mock, manifest_mock, config_mock, set())
    prov.http_server = None
    return prov


@pytest.fixture
def player(provider: WebKioskProvider) -> WebKioskPlayer:
    """Return a WebKioskPlayer with update_state mocked."""
    p = WebKioskPlayer(provider, "wk_test", name="Test Kiosk", output_format="mp3")
    p.update_state = Mock()  # type: ignore[misc,method-assign]
    return p


@pytest.fixture
async def http_client(
    provider: WebKioskProvider,
) -> AsyncGenerator[TestClient[Any, Any]]:
    """Return an aiohttp TestClient for the Web Kiosk HTTP server."""
    server = WebKioskHTTPServer(provider, 0)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    yield client
    await client.close()


@pytest.fixture
def queue_order(mass_mock: Mock) -> Callable[..., None]:
    """Return a setter that makes the mocked queue name successors in the given order."""

    def set_order(*queue_item_ids: str) -> None:
        def get_next_item(_queue_id: str, current: object) -> Mock | None:
            if not isinstance(current, str) or current not in queue_item_ids:
                return None
            index = queue_item_ids.index(current) + 1
            if index == len(queue_item_ids):
                return None
            return Mock(queue_item_id=queue_item_ids[index])

        mass_mock.player_queues.get_next_item = Mock(side_effect=get_next_item)

    return set_order


@pytest.fixture(autouse=True)
async def task_errors_are_retrieved() -> AsyncIterator[None]:
    """Fail a test that leaves a task failure nobody retrieved (see CLAUDE.local.md)."""
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    tasks: list[asyncio.Task[Any]] = []

    def factory(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> asyncio.Future[Any]:
        task = asyncio.Task(coro, loop=loop, **kwargs)
        tasks.append(task)
        return task

    loop.set_task_factory(factory)
    try:
        yield
        await asyncio.sleep(0)
    finally:
        loop.set_task_factory(previous)
    unretrieved = [
        repr(task.exception())
        for task in tasks
        if task.done() and not task.cancelled() and task._log_traceback
    ]
    assert unretrieved == []


@pytest.fixture(autouse=True)
def lock_order(monkeypatch: pytest.MonkeyPatch) -> Iterator[LockOrder]:
    """Fail a test that takes the provider's locks out of order (see CLAUDE.local.md)."""
    LOCK_ORDER.held.clear()
    LOCK_ORDER.violations.clear()
    provider_init = WebKioskProvider.__init__

    def init(self: WebKioskProvider, *args: Any, **kwargs: Any) -> None:
        provider_init(self, *args, **kwargs)
        self._player_lock = TrackedLock(self._player_lock, PLAYER_LOCK)  # type: ignore[assignment]

    outbound_lock = WebKioskHTTPServer._outbound_lock

    def tracked_outbound(self: WebKioskHTTPServer, player_id: str) -> TrackedLock:
        return TrackedLock(outbound_lock(self, player_id), OUTBOUND_LOCK)

    core_lock = PlayerController.get_player_lock

    def tracked_core(
        self: PlayerController,
        player_id: str,
        purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
        strict: bool = False,
    ) -> AbstractAsyncContextManager[None]:
        lock = core_lock(self, player_id, purpose, strict=strict)
        return (
            LOCK_ORDER.hold(PLAYBACK_LOCK, lock) if purpose == PlayerLockPurpose.PLAYBACK else lock
        )

    monkeypatch.setattr(WebKioskProvider, "__init__", init)
    monkeypatch.setattr(WebKioskHTTPServer, "_outbound_lock", tracked_outbound)
    # MA's commands and get_group_and_player_lock() all take the lock through here.
    monkeypatch.setattr(PlayerController, "get_player_lock", tracked_core)
    yield LOCK_ORDER
    assert LOCK_ORDER.violations == []
