"""
Boot a real Music Assistant server for a test.

Also a pytest plugin for the repository of a provider: with
``pytest_plugins = ["music_assistant.testing"]`` in its ``tests/conftest.py``, the ``mass``
fixture gives a started server that loads the providers of that repository.
"""

import asyncio
import contextlib
import logging
import pathlib
from collections.abc import AsyncGenerator, Iterator
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock, patch

import pytest
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.controllers.webserver import controller as webserver_controller
from music_assistant.mass import MusicAssistant
from music_assistant.providers.sendspin.provider import SendspinProvider

# the address a fixture's web and stream servers bind to, so a test run never listens
# on the host's real interfaces
LOOPBACK_IP = "127.0.0.1"


@pytest.fixture
async def mass(
    tmp_path: pathlib.Path, pytestconfig: pytest.Config
) -> AsyncGenerator[MusicAssistant]:
    """
    Start a Music Assistant in dev mode that loads the providers in the root folder of the test run.

    :param tmp_path: Temporary directory for test data.
    :param pytestconfig: The configuration of the test run.
    """
    async with full_mass_context(tmp_path, dev_providers_path=pytestconfig.rootpath) as server:
        yield server


@contextlib.asynccontextmanager
async def full_mass_context(
    tmp_path: pathlib.Path,
    *,
    safe_mode: bool = False,
    dev_providers_path: pathlib.Path | None = None,
) -> AsyncGenerator[MusicAssistant]:
    """
    Boot a full server on the given temporary directory.

    Exposed next to the ``mass`` fixture so a test that needs to seed ``data/settings.json``
    (or the cache) before the boot can prepare the directory itself and boot it here.

    :param tmp_path: Temporary directory for test data.
    :param safe_mode: Boot the server in safe mode.
    :param dev_providers_path: Folder of providers to load as community providers. The server
        then runs in dev mode and is handed over once these are loaded.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True, exist_ok=True)
    cache_path.mkdir(parents=True, exist_ok=True)

    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(
        str(storage_path),
        str(cache_path),
        safe_mode,
        str(dev_providers_path) if dev_providers_path else None,
    )
    if dev_providers_path:
        # the server only loads a local provider folder in dev mode
        mass_instance.dev_mode = True

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
        # the Sendspin server runs its own zeroconf instance: keep it off the network,
        # its mDNS probing alone also holds up every boot for over a second
        patch("aiosendspin.server.server.AsyncZeroconf", return_value=_create_mock_zeroconf()),
        patch(
            "aiosendspin.server.server.AsyncServiceBrowser",
            return_value=MagicMock(spec=AsyncServiceBrowser),
        ),
        # Booting the server runs an ffmpeg presence check; mock it so tests can boot
        # without the binary. Tests that actually spawn ffmpeg still use the real one.
        patch(
            "music_assistant.controllers.streams.controller.check_ffmpeg_version",
            new=AsyncMock(),
        ),
        # keep the fixture isolated from the developer's machine: no auto-loaded
        # device providers
        suppress_auto_loaded_providers(),
        # keep the booted instance quiet: no library sync firing into a running test
        suppress_initial_library_sync(),
    ):
        try:
            await mass_instance.start()
            if dev_providers_path:
                # the server loads the providers of the local folder in a task of its own
                for task in list(mass_instance._tracked_tasks.values()):
                    if task.get_name() == "community_catalog_start":
                        await task
            await wait_for_boot_to_settle(mass_instance)
            adopt_bound_sendspin_port(mass_instance)
            yield mass_instance
        finally:
            # also stop after a failed boot: pytest holds on to the setup traceback,
            # which keeps the half-started server (and the non-daemon threads of its
            # open database connections) alive until the interpreter exits, where
            # joining those threads then hangs the whole test process
            await mass_instance.stop()


@contextlib.contextmanager
def use_ephemeral_server_ports() -> Iterator[None]:
    """
    Bind a full-server test fixture's web, stream and Sendspin servers to a free loopback port.

    Port 0 has the kernel pick the port during the bind itself, so nothing else can
    claim it in the meantime. The server also hands the Sendspin port out in the URLs it
    builds for its own Sendspin clients, so once the provider has bound its listener the
    fixture must call ``adopt_bound_sendspin_port`` to point those URLs at that port.

    Binding loopback keeps a test run off the host's other interfaces and gives each
    server a single socket, so it has one assigned port: asyncio binds a wildcard
    address once per address family, each with its own port.
    """
    with (
        patch("music_assistant.controllers.webserver.controller.DEFAULT_SERVER_PORT", 0),
        patch("music_assistant.controllers.streams.controller.DEFAULT_PORT", 0),
        # the Sendspin provider binds its own listener; on a fixed port, parallel test
        # workers collide and the provider unloads itself mid-test
        patch("music_assistant.providers.sendspin.provider.SENDSPIN_SERVER_PORT", 0),
        patch("music_assistant.controllers.webserver.controller.SENDSPIN_SERVER_PORT", 0),
        patch("music_assistant.controllers.webserver.controller.DEFAULT_HOST", LOOPBACK_IP),
        patch("music_assistant.controllers.streams.controller.DEFAULT_HOST", LOOPBACK_IP),
        # keep address detection off the host's real interfaces
        patch(
            "music_assistant.controllers.streams.controller.get_ip_addresses",
            AsyncMock(return_value=(LOOPBACK_IP,)),
        ),
        patch(
            "music_assistant.controllers.streams.controller.get_publish_ip_candidates",
            AsyncMock(return_value=(LOOPBACK_IP,)),
        ),
        patch(
            "music_assistant.controllers.webserver.controller.get_ip_addresses",
            AsyncMock(return_value=(LOOPBACK_IP,)),
        ),
        patch(
            "music_assistant.controllers.webserver.controller.get_publish_ip_candidates",
            AsyncMock(return_value=(LOOPBACK_IP,)),
        ),
    ):
        yield


def bound_sendspin_port(mass: MusicAssistant) -> int | None:
    """
    Return the port the Sendspin provider's listener is bound to, if it is listening.

    :param mass: The booted server to inspect.
    """
    provider = mass.get_provider("sendspin")
    if not isinstance(provider, SendspinProvider):
        return None
    # aiosendspin keeps its aiohttp runner to itself; the runner's addresses are
    # the bound sockets' names, which is the only place the kernel-picked port lives
    runner = provider.server_api._app_runner
    if runner is None:
        return None
    return next((address[1] for address in runner.addresses if address), None)


def adopt_bound_sendspin_port(mass: MusicAssistant) -> None:
    """
    Point the server's own Sendspin clients at the port its listener actually bound.

    Only meaningful inside ``use_ephemeral_server_ports``, whose patch of the URL
    builder's port this replaces for the rest of the fixture's lifetime.

    :param mass: The booted server whose Sendspin listener is up.
    """
    if (port := bound_sendspin_port(mass)) is not None:
        setattr(webserver_controller, "SENDSPIN_SERVER_PORT", port)  # noqa: B010


@contextlib.contextmanager
def suppress_auto_loaded_providers() -> Iterator[None]:
    """
    Stop a fixture boot from auto-setting-up providers that reach into the host.

    Keeps a booted test instance isolated from the developer's machine: the default
    device providers (airplay/chromecast/dlna/...) are not auto-configured.
    """
    with patch("music_assistant.mass.DEFAULT_PROVIDERS", ()):
        yield


async def wait_for_boot_to_settle(mass: MusicAssistant) -> None:
    """
    Wait out the events a fixture boot leaves in flight.

    A provider finishes loading in a detached task that registers background tasks, and
    registering one emits a debounced task list, so without this a test can start watching
    for events in time to catch the tail of its own fixture's boot.

    :param mass: The started instance to settle.
    """
    for provider in mass.providers:
        await provider.initialized.wait()
        # the post-load task keeps working after it marks the provider initialized
        if post_load := mass.get_task(f"post_load_provider_{provider.instance_id}"):
            await asyncio.wait({post_load})
    # emit the debounced task list now rather than waiting out its timer
    if mass.tasks._scheduled_task_update_at is not None:
        mass.cancel_timer(TASK_UPDATE_TIMER_ID)
        mass.tasks._signal_task_update()


@contextlib.contextmanager
def suppress_initial_library_sync() -> Iterator[None]:
    """
    Hold a fixture boot's music providers to their recurring library sync only.

    The first sync of a freshly loaded provider otherwise runs seconds into the boot, so on
    a loaded machine it lands in the middle of whatever test is running by then, rewriting
    the library under it. Tests that want a sync call ``start_sync()`` themselves.
    """
    with patch("music_assistant.controllers.music.controller.INITIAL_SYNC_DELAY", None):
        yield


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
