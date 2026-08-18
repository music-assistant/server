"""Fixtures for testing Music Assistant."""

import asyncio
import logging
import os
import pathlib
import tempfile
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock, patch

import pytest
from music_assistant_models import helpers as models_helpers
from zeroconf.asyncio import AsyncZeroconf

from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.discovery import DiscoveryController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.tasks import TasksController
from music_assistant.mass import MusicAssistant
from tests.common import (
    suppress_auto_loaded_providers,
    suppress_initial_library_sync,
    use_ephemeral_server_ports,
    utf8_safe,
    wait_for_boot_to_settle,
)

NUMBA_CACHE_DIR = pytest.StashKey[tempfile.TemporaryDirectory[str]]()


def pytest_configure(config: pytest.Config) -> None:
    """
    Give this test process its own numba kernel cache.

    librosa compiles its numba kernels with ``cache=True`` into a directory shared by
    every process on the machine. numba updates that cache's index non-atomically, so
    xdist workers filling a cold cache can leave an entry pointing at another
    signature's machine code — calling it then segfaults the worker.
    See https://github.com/numba/numba/issues/10128.
    """
    cache_dir = tempfile.TemporaryDirectory(prefix="ma-numba-cache-")
    config.stash[NUMBA_CACHE_DIR] = cache_dir
    # numba reads this once, when it is imported; nothing here imports it that early.
    os.environ["NUMBA_CACHE_DIR"] = cache_dir.name


def pytest_unconfigure(config: pytest.Config) -> None:
    """Drop this test process's numba kernel cache."""
    if (cache_dir := config.stash.get(NUMBA_CACHE_DIR, None)) is not None:
        cache_dir.cleanup()


@pytest.hookimpl(wrapper=True)
def pytest_report_to_serializable() -> Generator[None, object, object]:
    """
    Make serialized test reports strict-UTF-8 safe for pytest-xdist.

    Lone surrogates in a captured report (e.g. undecodable filesystem paths) kill
    the execnet worker channel. Covers test reports only; other xdist payloads
    (warnings, log-start nodeids) are serialized outside this hook.
    """
    data = yield
    return utf8_safe(data)


@pytest.fixture(autouse=True)
def isolate_models_global_cache() -> Generator[None]:
    """
    Reset the models package's process-global cache between tests.

    A full server boot populates module-level globals in music_assistant_models
    (e.g. ``available_providers``, which drives ``MediaItem.available``). Left in
    place, they leak into later tests in the same pytest process and change item
    availability depending on test ordering. Note that an empty cache falls back
    to permissive defaults, so tests sharing a broader-scoped server instance are
    unaffected by the per-test clear.
    """
    yield
    models_helpers._global_cache.clear()


@pytest.fixture(name="caplog")
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Set log level to debug for tests using the caplog fixture."""
    caplog.set_level(logging.DEBUG)
    return caplog


def _create_mock_zeroconf() -> MagicMock:
    """
    Create a mock AsyncZeroconf that prevents real network I/O.

    Uses spec=AsyncZeroconf to ensure the mock only has valid attributes,
    preventing it from being mistakenly registered as an API handler.
    """
    mock_zc = MagicMock(spec=AsyncZeroconf)
    # Set up nested zeroconf object with proper spec
    mock_inner_zc = NonCallableMagicMock()
    mock_inner_zc.cache = NonCallableMagicMock()
    mock_inner_zc.cache.cache = {}  # Empty cache - no discovered services
    mock_zc.zeroconf = mock_inner_zc
    # Set up async methods
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()
    return mock_zc


@pytest.fixture
async def mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """
    Start a Music Assistant in test mode.

    :param tmp_path: Temporary directory for test data.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))

    # Mock zeroconf to prevent real network I/O during tests
    mock_zc = _create_mock_zeroconf()
    mock_browser = NonCallableMagicMock()  # Use NonCallable to avoid api_cmd issues

    with (
        use_ephemeral_server_ports(),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=mock_zc,
        ),
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceBrowser",
            return_value=mock_browser,
        ),
        # Booting the server runs an ffmpeg presence check; mock it so tests can boot
        # without the binary. Tests that actually spawn ffmpeg still use the real one.
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            new=AsyncMock(),
        ),
        # keep the fixture isolated from the developer's machine: no auto-loaded device
        # providers and no local_audio bridging the host's sound devices as players
        suppress_auto_loaded_providers(),
        # keep the booted instance quiet: no library sync firing into a running test
        suppress_initial_library_sync(),
    ):
        try:
            await mass_instance.start()
            await wait_for_boot_to_settle(mass_instance)
            yield mass_instance
        finally:
            # also stop after a failed boot: pytest holds on to the setup traceback,
            # which keeps the half-started server (and the non-daemon threads of its
            # open database connections) alive until the interpreter exits, where
            # joining those threads then hangs the whole test process
            await mass_instance.stop()


@pytest.fixture
async def mass_minimal(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """
    Create a minimal Music Assistant instance without starting the full server.

    Only initializes the event loop and config controller.
    Useful for testing individual controllers without the overhead of the webserver.

    :param tmp_path: Temporary directory for test data.
    """
    async with _minimal_mass_context(tmp_path) as mass_instance:
        yield mass_instance


@pytest.fixture(scope="class")
async def music_mass_class(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[MusicAssistant]:
    """Create a class-scoped Music Assistant instance with only library storage."""
    async with _music_mass_context(tmp_path_factory.mktemp("music_class")) as mass_instance:
        yield mass_instance


@pytest.fixture(scope="module")
async def music_mass_module(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[MusicAssistant]:
    """Create a module-scoped Music Assistant instance with only library storage."""
    async with _music_mass_context(tmp_path_factory.mktemp("music_module")) as mass_instance:
        yield mass_instance


@asynccontextmanager
async def _minimal_mass_context(
    tmp_path: pathlib.Path,
) -> AsyncGenerator[MusicAssistant]:
    """Create a minimal Music Assistant instance for a fixture."""
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    mass_instance.loop = asyncio.get_running_loop()
    mass_instance.loop_thread_id = threading.get_ident()
    mass_instance.config = ConfigController(mass_instance)
    await mass_instance.config.setup()
    mass_instance.discovery = DiscoveryController(mass_instance)
    mass_instance.cache = CacheController(mass_instance)

    try:
        yield mass_instance
    finally:
        await mass_instance.cache.close()
        await mass_instance.config.close()


@asynccontextmanager
async def _music_mass_context(
    tmp_path: pathlib.Path,
) -> AsyncGenerator[MusicAssistant]:
    """Create a minimal Music Assistant instance with a real library database."""
    async with _minimal_mass_context(tmp_path) as mass_instance:
        mass_instance.tasks = TasksController(mass_instance)
        tasks_config = await mass_instance.config.get_core_config(mass_instance.tasks.domain)
        await mass_instance.tasks.setup(tasks_config)

        mass_instance.metadata = MagicMock()
        mass_instance.metadata.schedule_update_metadata = MagicMock()
        mass_instance.metadata.invalidate_image_cache = AsyncMock()
        mass_instance.webserver = MagicMock()
        mass_instance.webserver.auth.list_users = AsyncMock(return_value=[])

        mass_instance.music = MusicController(mass_instance)
        music_config = await mass_instance.config.get_core_config(mass_instance.music.domain)
        await mass_instance.music.setup(music_config)
        await mass_instance.music.post_setup()
        try:
            yield mass_instance
        finally:
            await mass_instance.tasks.close()
            await mass_instance.music.close()
