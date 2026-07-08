"""
Fixtures for the CodSpeed macro benchmarks: hermetic MusicAssistant instances.

Every instance boots fully isolated from the host and the network: webserver and
streamserver bind random free loopback ports, mDNS/SSDP discovery is mocked and only
local-only builtin providers are allowed to load. This keeps the measured instruction
counts deterministic (no network I/O, no real devices connecting) and mirrors the
hermetic setup of the deep-dive harness in scripts/perf/.
"""

from __future__ import annotations

import asyncio
import itertools
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock, patch

import pytest
import pytest_asyncio
from zeroconf.asyncio import AsyncZeroconf

from music_assistant.controllers.config.controller import ConfigController
from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

# Local-only builtin providers that are allowed to load. Everything else with
# builtin=true in its manifest is stripped before builtin load. Most importantly
# sendspin: it runs its own aiosendspin server with its own mDNS advertisement
# (bypassing the mocked discovery controller), so real devices on the LAN would
# discover and connect to the benchmark instance otherwise.
ALLOWED_BUILTIN_PROVIDERS = {
    "builtin",
    "loudness_analysis",
    "playlist_metadata",
    "radio_playlist",
    "sync_group",
    "universal_player",
}

# Library sizes for the fake `test` music provider (artists x albums x tracks).
SMALL_LIBRARY_SIZE = {"num_artists": 5, "num_albums": 4, "num_tracks": 5}
LIBRARY_SIZE = {"num_artists": 20, "num_albums": 5, "num_tracks": 10}

SYNC_TIMEOUT_SECONDS = 300.0

_ORIG_CONFIG_SETUP = ConfigController.setup
_ORIG_LOAD_BUILTIN = MusicAssistant._load_builtin_providers


def expected_track_count(library_size: dict[str, int]) -> int:
    """Return the number of tracks the test provider generates for a library size."""
    return library_size["num_artists"] * library_size["num_albums"] * library_size["num_tracks"]


async def sync_test_provider(mass: MusicAssistant, library_size: dict[str, int]) -> None:
    """
    Configure the fake `test` music provider and wait until its initial sync completes.

    :param mass: A started MusicAssistant instance without the test provider configured.
    :param library_size: num_artists/num_albums/num_tracks for the generated library.
    """
    await mass.config.save_provider_config(
        "test", {**library_size, "num_podcasts": 2, "num_audiobooks": 2}
    )
    await wait_for_sync_complete(mass, expected_track_count(library_size))


async def wait_for_sync_complete(mass: MusicAssistant, expected_tracks: int) -> None:
    """
    Wait until the library holds the expected tracks and no sync tasks are active.

    :param mass: The MusicAssistant instance that is syncing.
    :param expected_tracks: Minimum number of tracks the library must contain.
    """

    async def _synced() -> bool:
        if await mass.music.tracks.library_count() < expected_tracks:
            return False
        return not mass.music.active_sync_tasks

    await _wait_for(_synced, timeout=SYNC_TIMEOUT_SECONDS)


@pytest.fixture
def hermetic_server(
    tmp_path: Path,
) -> Callable[[], AbstractAsyncContextManager[MusicAssistant]]:
    """
    Return a factory producing async context managers for fresh hermetic instances.

    Every call yields a brand-new unstarted instance on its own data dir, so
    benchmark bodies that boot a server stay re-runnable when the measurement
    tool invokes them multiple times (warmup + measured rounds).
    """
    counter = itertools.count()

    def _factory() -> AbstractAsyncContextManager[MusicAssistant]:
        return _hermetic_instance(tmp_path / f"instance_{next(counter)}")

    return _factory


@pytest_asyncio.fixture
async def synced_mass(tmp_path: Path) -> AsyncGenerator[MusicAssistant]:
    """Yield a started hermetic MusicAssistant instance with a small synced library."""
    async with _hermetic_instance(tmp_path) as mass:
        await mass.start()
        await sync_test_provider(mass, SMALL_LIBRARY_SIZE)
        yield mass


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def library_mass(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[MusicAssistant]:
    """
    Yield a started hermetic MusicAssistant instance with a 1000-track synced library.

    Module-scoped: the library is synced once and shared by all read-only
    benchmarks in a module (which must run on the module event loop via
    ``pytest.mark.asyncio(loop_scope="module")``).
    """
    async with _hermetic_instance(tmp_path_factory.mktemp("library_mass")) as mass:
        await mass.start()
        await sync_test_provider(mass, LIBRARY_SIZE)
        yield mass


@asynccontextmanager
async def _hermetic_instance(base_path: Path) -> AsyncGenerator[MusicAssistant]:
    """
    Yield an unstarted, fully isolated MusicAssistant instance.

    The caller is responsible for calling ``start()``; the instance is stopped on
    exit when it was started. Only one instance may be booted at a time (the port
    override patch is process-global).
    """
    storage_path = base_path / "data"
    cache_path = base_path / "cache"
    storage_path.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)

    async with AsyncExitStack() as stack:
        for patcher in (
            patch.object(ConfigController, "setup", _make_loopback_config_setup()),
            patch.object(MusicAssistant, "_load_builtin_providers", _load_allowed_builtins),
            patch(
                "music_assistant.controllers.discovery.controller.AsyncZeroconf",
                return_value=_create_mock_zeroconf(),
            ),
            patch(
                "music_assistant.controllers.discovery.controller.AsyncServiceBrowser",
                return_value=NonCallableMagicMock(),
            ),
            # hermetic: no real SSDP search
            patch(
                "music_assistant.controllers.discovery.controller.async_upnp_search",
                new=AsyncMock(),
            ),
            # the benchmark environment has no ffmpeg and never streams audio
            patch(
                "music_assistant.controllers.streams.controller.check_ffmpeg_version",
                new=AsyncMock(),
            ),
            # hermetic: no auto-loaded device providers (dlna/sonos/...)
            patch("music_assistant.mass.DEFAULT_PROVIDERS", ()),
        ):
            stack.enter_context(patcher)
        mass = MusicAssistant(str(storage_path), str(cache_path))
        try:
            yield mass
        finally:
            if getattr(mass, "loop", None) is not None:
                await mass.stop()


def _make_loopback_config_setup() -> Callable[[ConfigController], Coroutine[Any, Any, None]]:
    """Build a ConfigController.setup override that binds free loopback ports."""
    web_port = _free_port()
    stream_port = _free_port()

    async def _setup(self: ConfigController) -> None:
        await _ORIG_CONFIG_SETUP(self)
        for core_module, key, value in (
            ("webserver", "bind_ip", "127.0.0.1"),
            ("webserver", "bind_port", web_port),
            ("webserver", "base_url", f"http://127.0.0.1:{web_port}"),
            ("streams", "bind_ip", "127.0.0.1"),
            ("streams", "publish_ip", "127.0.0.1"),
            ("streams", "bind_port", stream_port),
        ):
            self.set_raw_core_config_value(core_module, key, value)

    return _setup


async def _load_allowed_builtins(self: MusicAssistant) -> None:
    """Load builtin providers, stripping everything not on the local-only allowlist."""
    for domain, manifest in list(self._provider_manifests.items()):
        if manifest.builtin and domain not in ALLOWED_BUILTIN_PROVIDERS:
            self._provider_manifests.pop(domain)
    await _ORIG_LOAD_BUILTIN(self)


def _create_mock_zeroconf() -> MagicMock:
    """Create a mock AsyncZeroconf that prevents real mDNS network I/O."""
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_inner_zc = NonCallableMagicMock()
    mock_inner_zc.cache = NonCallableMagicMock()
    mock_inner_zc.cache.cache = {}  # empty cache - no discovered services
    mock_zc.zeroconf = mock_inner_zc
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()
    return mock_zc


def _free_port() -> int:
    """Pick a currently free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for(predicate: Callable[[], Awaitable[bool]], timeout: float) -> None:
    """Poll an async predicate until it is truthy or raise after ``timeout`` seconds."""
    elapsed = 0.0
    while elapsed < timeout:
        if await predicate():
            return
        await asyncio.sleep(0.1)
        elapsed += 0.1
    raise TimeoutError(f"condition not met within {timeout}s")
