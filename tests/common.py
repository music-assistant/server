"""Common test helpers for Music Assistant tests."""

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import pathlib
import weakref
from collections.abc import AsyncGenerator, Iterator, Mapping
from functools import partial
from types import CoroutineType, MethodType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiofiles.os
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.player import DeviceInfo
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.controllers.webserver import controller as webserver_controller
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    sendspin_player_id,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.player_state import (
    GroupSnapshot,
    PeerSnapshot,
    ProtocolSnapshot,
    StateInputs,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderAccess
    from music_assistant_models.event import MassEvent


# every value of a published state: its fields and its calculated properties
_PLAYER_STATE_NAMES = [field.name for field in dataclasses.fields(PlayerState)] + [
    name for name, value in vars(PlayerState).items() if isinstance(value, property)
]


def utf8_safe(value: object) -> object:
    """
    Return ``value`` with any non-UTF-8-encodable strings made encodable.

    Lone surrogates (e.g. from undecodable filesystem paths) are replaced with
    their backslash escapes so the value survives strict-UTF-8 serialization.
    """
    if isinstance(value, str):
        try:
            value.encode()
        except UnicodeEncodeError:
            return value.encode("utf-8", "backslashreplace").decode()
        return value
    if isinstance(value, list):
        return [utf8_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(utf8_safe(item) for item in value)
    if isinstance(value, dict):
        return {utf8_safe(key): utf8_safe(item) for key, item in value.items()}
    return value


def _get_fixture_folder(provider: str | None = None) -> pathlib.Path:
    tests_base = pathlib.Path(__file__).parent
    if provider:
        return tests_base / "providers" / provider / "fixtures"
    return tests_base / "fixtures"


async def get_fixtures_dir(
    subdir: str, provider: str | None = None
) -> AsyncGenerator[tuple[str, bytes]]:
    """Yield the contents of every fixture in a fixtures folder."""
    dir_path = _get_fixture_folder(provider) / subdir
    for file in await aiofiles.os.listdir(dir_path):
        async with aiofiles.open(dir_path / file, "rb") as fp:
            yield (file, await fp.read())


@contextlib.contextmanager
def collect_loop_errors() -> Iterator[list[dict[str, Any]]]:
    """
    Capture everything the running loop reports to its exception handler.

    Yields the (initially empty) list the captured contexts are appended to; the loop's
    own handler is restored on exit. Use it to assert that an operation does not surface
    an error the server itself already handles, which would otherwise reach the user as
    an ERROR log entry with a traceback.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        yield reported
    finally:
        loop.set_exception_handler(previous)


class _RecordCollector(logging.Handler):
    """Log handler that keeps every record it gets."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        """
        Initialize the handler at the lowest level.

        :param records: The list the records are appended to.
        """
        super().__init__(level=1)
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        """Keep a record."""
        self.records.append(record)


@contextlib.contextmanager
def capture_log_records(logger: logging.Logger) -> Iterator[list[logging.LogRecord]]:
    """
    Capture every record of a logger, whatever other tests did to the logging setup.

    Yields the (initially empty) list of records. The logger gets a handler of its own and
    the lowest level, so neither the level an ancestor was given, nor its propagation or the
    handlers of the root logger, decide what is captured; all of it is restored on exit.

    :param logger: The logger the code under test writes to.
    """
    records: list[logging.LogRecord] = []
    handler = _RecordCollector(records)
    level, disabled, disabled_below = logger.level, logger.disabled, logging.root.manager.disable
    logger.addHandler(handler)
    logger.setLevel(1)
    logger.disabled = False
    logging.disable(logging.NOTSET)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.disabled = disabled
        logging.disable(disabled_below)


@contextlib.asynccontextmanager
async def wait_for_sync_completion(mass: MusicAssistant) -> AsyncGenerator[None]:
    """Wait for a sync to finish."""
    flag = asyncio.Event()

    def _event(_event: MassEvent) -> None:
        flag.set()

    release_cb = mass.subscribe(_event, EventType.MUSIC_SYNC_COMPLETED)

    try:
        yield
    finally:
        try:
            if mass.music.active_sync_tasks:
                await flag.wait()
        finally:
            release_cb()


# the address a fixture's web and stream servers bind to, so a test run never listens
# on the host's real interfaces
LOOPBACK_IP = "127.0.0.1"


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
    # aiosendspin keeps its aiohttp runner to itself; the runner's addresses are
    # the bound sockets' names, which is the only place the kernel-picked port lives
    runner = getattr(getattr(provider, "server_api", None), "_app_runner", None)
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


def scheduled_call(coro: CoroutineType[Any, Any, Any]) -> tuple[str, dict[str, Any]]:
    """
    Return the name and bound arguments of a coroutine handed to mass.create_task.

    Callers build the coroutine before handing it over, so a test asserting on what was
    scheduled reads it back off the coroutine. It is closed here, since a test that only
    asserts on the scheduling never awaits it.

    :param coro: The coroutine the caller passed to create_task.
    """
    name = coro.cr_code.co_qualname
    # cr_frame is None only once a coroutine has finished; one handed to create_task has
    # not been started yet, so its frame still holds the arguments it was built with
    assert coro.cr_frame is not None
    arguments = dict(coro.cr_frame.f_locals)
    coro.close()
    return name, arguments


# Mock classes for testing


def use_real_create_task(mass: MagicMock | MusicAssistant) -> None:
    """
    Give a mocked MusicAssistant the real create_task implementation.

    Needed for any test that lets a `@use_cache` decorated method run, since the
    decorator awaits the task it gets back to share one fetch between callers.

    :param mass: The mock standing in for the MusicAssistant instance.
    """
    mass._tracked_tasks = {}
    # on an AsyncMock this call would hand back a coroutine that nobody awaits
    mass.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    real_create_task = MethodType(MusicAssistant.create_task, mass)

    def _create_task(target: Any, *args: Any, **kwargs: Any) -> Any:
        if not (inspect.iscoroutine(target) or inspect.iscoroutinefunction(target)):
            # tests hand this mocked methods too, which the real one refuses
            return MagicMock()
        # resolved per call so this also works from a synchronous fixture
        mass.loop = asyncio.get_running_loop()
        return real_create_task(target, *args, **kwargs)

    # kept a mock so tests can still assert on the calls it received
    mass.create_task = MagicMock(side_effect=_create_task)  # type: ignore[method-assign]


def bare_player_controller() -> PlayerController:
    """Return a bare players controller, carrying just enough state for its real player locks."""
    players = PlayerController.__new__(PlayerController)
    players._player_command_locks = {}
    players._players = {}
    players._protocols = {}
    players._legacy_ids = {}
    players._task_held_locks = weakref.WeakKeyDictionary()
    players._lock_holders = weakref.WeakValueDictionary()
    players.logger = MagicMock()
    return players


def mock_player_mass() -> MagicMock:
    """
    Return a mocked MusicAssistant for tests of players and protocols without a server.

    Nothing is stored in its config, no queue or player control exists, and its players
    controller looks players and protocols up for real, in what
    ``attach_mock()`` registers for a provider of it.
    """
    mass = MagicMock()
    mass.closing = False
    config = mass.config
    config.get = MagicMock(side_effect=lambda _key, default=None: default)
    config.get_player_value = MagicMock(side_effect=lambda _player_id, _key, default=None: default)
    config.get_protocol_value = MagicMock(
        side_effect=lambda _protocol_id, _key, default=None: default
    )
    config.get_protocol_record = MagicMock(return_value=None)
    config.get_protocol_ids_of_player = MagicMock(return_value=[])
    config.get_primary_protocol_id = MagicMock(return_value=None)
    mass.player_queues.get = MagicMock(return_value=None)
    use_player_registry(mass)
    return mass


def use_player_registry(mass: MagicMock | MusicAssistant) -> MagicMock:
    """
    Give a mass a mocked players controller that looks players and protocols up for real.

    Its registries are empty: ``attach_mock()`` fills them. No player control or live
    audio source exists.

    :param mass: The (mocked) MusicAssistant instance.
    """
    players = MagicMock()
    players.mass = mass
    players._players = {}
    players._protocols = {}
    players._legacy_ids = {}
    for lookup in (
        "get_player",
        "resolve_player_id",
        "get_protocol",
        "get_player_by_protocol",
        "get_player_or_protocol_player",
        "get_output_protocol",
        "iter_players",
        "iter_protocols",
    ):
        setattr(players, lookup, MethodType(getattr(PlayerController, lookup), players))
    players.get_audio_source_session = MagicMock(return_value=None)
    players.is_live_audio_source = MagicMock(return_value=False)
    players.get_player_control = MagicMock(return_value=None)
    players.player_controls = MagicMock(return_value=[])
    players.scale_volume_from_device = MagicMock(side_effect=lambda _player_id, volume: volume)
    mass.players = players
    return players


def use_api_command_registry(mass: MagicMock, *command_owners: object) -> None:
    """
    Give a mocked mass the server's API command registry, with the commands of the given objects.

    Commands that a provider registers with ``mass.register_api_command`` land in it too, so a
    test reaches a command through ``mass.command_handlers[<command>].target`` like a client.

    :param mass: The mocked MusicAssistant instance.
    :param command_owners: Controllers whose ``api_command`` methods to register.
    """
    mass.command_handlers = {}
    mass.register_api_command = partial(MusicAssistant.register_api_command, mass)
    for owner in command_owners:
        for attr_name in dir(owner):
            if attr_name.startswith("__") or isinstance(
                getattr(type(owner), attr_name, None), property
            ):
                continue
            if hasattr(handler := getattr(owner, attr_name), "api_cmd"):
                mass.register_api_command(
                    handler.api_cmd, handler, required_scope=handler.api_required_scope
                )


def grouping_player_controller(*player_ids: str) -> PlayerController:
    """
    Return a players controller over ungrouped players that can all group with each other.

    The players are named "Player <ID>". The protocol of each has an id of its own,
    "proto_<player id>", and reports what it can group with in those protocol ids. The
    controller's commands are in the command registry of its mocked mass (see
    use_api_command_registry). The member change itself is stubbed
    (``_handle_set_members``), so a test can see whether one was made, and how.

    :param player_ids: The ids of the players to register.
    """
    mass = MagicMock()
    mass.closing = False
    mass.config.get_player_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    controller = PlayerController(mass)
    mass.players = controller
    provider = MockProvider("test", instance_id="test", mass=mass)
    for player_id in player_ids:
        _, protocol = attach_mock(
            provider, f"proto_{player_id}", f"Player {player_id.upper()}", player_id=player_id
        )
        protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol._attr_can_group_with = {f"proto_{other}" for other in player_ids} - {
            protocol.protocol_id
        }
    for player in controller._players.values():
        player.refresh_state(signal_event=False)
    controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]
    use_api_command_registry(mass, controller)
    return controller


def record_lock_waits(players: PlayerController) -> list[str]:
    """
    Record each player lock a command has to wait for from now on, by its key.

    A lock that is free, or that the task holds already, is taken at once and not recorded,
    so an empty record proves that no command waited, however fast the machine is.

    :param players: The players controller whose locks to watch.
    :return: The keys of the locks waited for (e.g. ``playback_<player id>``), in order.
    """
    waits: list[str] = []
    acquire = players._acquire_player_lock

    async def _acquire(
        lock: asyncio.Lock, lock_key: str, purpose: PlayerLockPurpose, player_id: str
    ) -> None:
        if lock.locked():
            waits.append(lock_key)
        await acquire(lock, lock_key, purpose, player_id)

    players._acquire_player_lock = _acquire  # type: ignore[method-assign]
    return waits


@contextlib.contextmanager
def virtual_loop_clock() -> Iterator[None]:
    """
    Run the running loop on a clock that moves only while the loop has nothing else to do.

    The clock (loop.time()) stands still while anything is runnable; once the loop would wait,
    it jumps to the next timer. Sleeps, timers and timeouts then fire in the order they fall due,
    however fast or busy the machine is. For tests without thread or socket work, as the loop
    no longer waits for that.
    """
    loop = asyncio.get_running_loop()
    now = loop.time()
    real_select = loop._selector.select  # type: ignore[attr-defined]

    def _time() -> float:
        return now

    def _select(timeout: float | None = None) -> Any:
        nonlocal now
        if timeout:
            # nothing is runnable before the next timer: let the clock jump to it
            now += timeout
            timeout = 0
        return real_select(timeout)

    with (
        patch.object(loop, "time", _time),
        patch.object(loop._selector, "select", _select),  # type: ignore[attr-defined]
    ):
        yield


@contextlib.contextmanager
def as_user(
    player_filter: list[str],
    role: UserRole = UserRole.USER,
    client_player_id: str | None = None,
) -> Iterator[None]:
    """
    Run the block as a user limited to the given players.

    :param player_filter: The players the user may use, none means all of them.
    :param role: The role of the user.
    :param client_player_id: The (private) client player the user is connected on.
    """
    token = current_user.set(
        User(user_id="user_1", username="limited", role=role, player_filter=player_filter)
    )
    client_token = sendspin_player_id.set(client_player_id)
    try:
        yield
    finally:
        sendspin_player_id.reset(client_token)
        current_user.reset(token)


def set_music_source_access(
    mass: MusicAssistant | MagicMock,
    access_by_instance: Mapping[str, ProviderAccess | None],
) -> None:
    """
    Give the server the given music sources, each carrying the given access record.

    A user's set of music sources is derived from the access records on the raw provider
    configs, so this is all a test needs to make a source visible to (or hidden from) a
    user. Works both on a real server and on a mocked one.

    :param mass: The (real or mocked) MusicAssistant instance.
    :param access_by_instance: Access record per music source instance id; None means a
        household source, visible to everyone.
    """
    raw_configs = {
        instance_id: {
            "type": ProviderType.MUSIC.value,
            "domain": instance_id.split("--")[0],
            "instance_id": instance_id,
            "access": access.to_dict() if access else None,
        }
        for instance_id, access in access_by_instance.items()
    }
    if isinstance(mass.config, ConfigController):
        for instance_id, raw_config in raw_configs.items():
            mass.config.set(f"{CONF_PROVIDERS}/{instance_id}", raw_config)
        return
    mass.config.get = MagicMock(
        side_effect=lambda key, default=None: (
            raw_configs
            if key == CONF_PROVIDERS
            else raw_configs.get(key.removeprefix(f"{CONF_PROVIDERS}/"), default)
        )
    )


class MockProvider:
    """Mock player provider for testing."""

    def __init__(
        self, domain: str, instance_id: str = "test_instance", mass: MagicMock | None = None
    ) -> None:
        """Initialize the mock provider."""
        self.domain = domain
        self.instance_id = instance_id
        self.name = f"Mock {domain.title()}"
        self.manifest = MagicMock()
        self.manifest.name = f"Mock {domain} Provider"
        self.mass = mass or MagicMock()
        self.dashboards = MagicMock()
        self.logger = logging.getLogger(f"test.{domain}")
        self.unloading = False
        self.translation_owner = f"provider.{domain}"
        # what a server needs of a provider that is registered on it
        self.type = ProviderType.PLAYER
        self.available = True
        self.supported_features: set[ProviderFeature] = set()

    # the lookups of a real player provider, over what attach_mock() registered
    protocols = PlayerProvider.protocols
    get_protocol = PlayerProvider.get_protocol


class MockProtocol(PlayerProtocol):
    """Mock protocol object of a player provider for testing."""

    def __init__(
        self,
        provider: MockProvider,
        protocol_id: str,
        name: str,
        role: ProtocolRole = ProtocolRole.NATIVE,
        identifiers: dict[IdentifierType, str] | None = None,
        player_type: PlayerType = PlayerType.PLAYER,
    ) -> None:
        """
        Initialize the mock protocol; use attach_mock() to get one attached to a player.

        :param provider: The (mock) provider the protocol belongs to.
        :param protocol_id: ID of the protocol object.
        :param name: The name its device reports.
        :param role: The role it reports for its device.
        :param identifiers: The device identifiers it reports.
        :param player_type: The type of player it reports.
        """
        if isinstance(provider.mass.config, MagicMock):
            # the base class asks the config controller for its config
            provider.mass.config.get_base_protocol_config.return_value = PlayerConfig(
                values={}, provider=provider.instance_id, player_id=protocol_id, default_name=name
            )
        super().__init__(provider, protocol_id)  # type: ignore[arg-type]
        self._attr_name = name
        self._attr_role = role
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET}
        self._attr_device_info = DeviceInfo(
            model="Test Model",
            manufacturer="Test Manufacturer",
        )
        if identifiers:
            for conn_type, value in identifiers.items():
                self._attr_device_info.add_identifier(conn_type, value)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Mock implementation of set_members."""
        current_members = set(self._attr_group_members)

        if player_ids_to_add:
            current_members.update(player_ids_to_add)

        if player_ids_to_remove:
            current_members.difference_update(player_ids_to_remove)

        # Always include self as first member if there are members
        if current_members:
            self._attr_group_members = [self.protocol_id] + [
                pid for pid in current_members if pid != self.protocol_id
            ]
        else:
            self._attr_group_members = []

        # Clear cache to reflect changes
        self._cache.clear()

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


def attach_mock(
    provider: MockProvider,
    protocol_id: str,
    name: str | None = None,
    *,
    role: ProtocolRole = ProtocolRole.NATIVE,
    player: Player | None = None,
    player_id: str | None = None,
    identifiers: dict[IdentifierType, str] | None = None,
    player_type: PlayerType = PlayerType.PLAYER,
    protocol_class: type[MockProtocol] = MockProtocol,
) -> tuple[Player, MockProtocol]:
    """
    Return a player and a mock protocol attached to it, without what register() awaits.

    Both are in the registries of the players controller of the provider's mass
    (``provider.mass.players``). The player is created (initialized, with a config of
    its own) unless one is given.

    This only wires the objects together. It applies none of the attach rules of the
    players controller (one protocol per domain, one that stands for the device, the
    identifier vetoes), stores no config, calls no hook of the protocol and publishes
    nothing: the final values of the player (``player.<field>``) stay those it was
    created with until the test calls ``update_state()``. A test of a sequence the
    controller may refuse, or of what an attach stores or announces, attaches to a real
    controller instead.

    :param provider: The (mock) provider the protocol belongs to.
    :param protocol_id: ID of the protocol object.
    :param name: The name its device reports, the protocol id by default.
    :param role: The role the protocol attaches in.
    :param player: The player to attach it to, for a second protocol of a device.
    :param player_id: Id of the player to create: by default one of its own, as a device
        gets that is new. Pass the protocol id for a player from before the player
        split, whose own protocol has the id of the player.
    :param identifiers: The device identifiers the protocol reports.
    :param player_type: The type of player the protocol reports.
    :param protocol_class: The class of the protocol, for a test that overrides what the
        provider implements.
    """
    controller = provider.mass.players
    protocol = protocol_class(
        provider, protocol_id, name or protocol_id, role, identifiers, player_type
    )
    if player is None:
        player_id = player_id or f"pl_{protocol_id}"
        player = Player(
            provider.mass,
            player_id,
            PlayerConfig(
                values={},
                provider=provider.instance_id,
                player_id=player_id,
                default_name=name or protocol_id,
                player_type=player_type,
            ),
        )
        controller._players[player_id] = player
        player.set_initialized()
    controller._protocols[protocol_id] = protocol
    player.add_protocol(protocol, role)
    protocol.set_player(player)
    return player, protocol


def make_player_state(player_id: str = "player_1", **values: Any) -> PlayerState:
    """
    Return the published state of an idle, available player.

    :param player_id: Id of the player.
    :param values: The values that differ from that state.
    """
    defaults: dict[str, Any] = {
        "provider": "test_prov",
        "type": PlayerType.PLAYER,
        "name": player_id,
        "available": True,
        "device_info": DeviceInfo(),
    }
    return PlayerState(player_id=player_id, **(defaults | values))


def mock_player(player_id: str = "player_1", **values: Any) -> MagicMock:
    """
    Return a mocked player whose final values are those of its published state.

    For code that reads both ``player.<field>`` and the snapshot ``player.state``. The
    mock has the spec of ``Player``; set what else the code reads on it.

    :param player_id: Id of the player.
    :param values: The values of its state that differ from ``make_player_state()``.
    """
    state = make_player_state(player_id, **values)
    player = MagicMock(spec=Player)
    player.state = state
    for name in _PLAYER_STATE_NAMES:
        setattr(player, name, getattr(state, name))
    return player


def make_protocol_snapshot(protocol_id: str = "player_1", **values: Any) -> ProtocolSnapshot:
    """
    Return what the native protocol of an idle, available device reports.

    :param protocol_id: Id of the protocol.
    :param values: The values that differ from that report.
    """
    defaults: dict[str, Any] = {
        "role": ProtocolRole.NATIVE,
        "available": True,
        "available_for_playback": True,
        "needs_setup": False,
        "setup_reason": None,
        "supported_features": set(),
        "playback_state": PlaybackState.IDLE,
        "elapsed_time": None,
        "elapsed_time_last_updated": None,
        "active_source": None,
        "source_list": [],
        "current_media": None,
        "group_members": [],
        "synced_to": None,
    }
    return ProtocolSnapshot(protocol_id=protocol_id, **(defaults | values))


def make_peer_snapshot(
    player_id: str, state: PlayerState | None = None, **values: Any
) -> PeerSnapshot:
    """
    Return an idle, available player the way the state of another player depends on it.

    :param player_id: Id of the player.
    :param state: The state the player publishes.
    :param values: The values that differ from that player.
    """
    defaults: dict[str, Any] = {
        "type": PlayerType.PLAYER,
        "available": True,
        "available_for_playback": True,
        "grouping_locked": False,
    }
    return PeerSnapshot(
        player_id=player_id,
        state=state or make_player_state(player_id),
        **(defaults | values),
    )


def make_group_snapshot(
    player_id: str, state: PlayerState | None = None, **values: Any
) -> GroupSnapshot:
    """
    Return a dormant group player the way the state of a player it can hold depends on it.

    :param player_id: Id of the group player.
    :param state: The state the group player publishes.
    :param values: The values that differ from that group player.
    """
    defaults: dict[str, Any] = {"powered": None, "is_active_session": False}
    return GroupSnapshot(
        player_id=player_id,
        state=state or make_player_state(player_id),
        **(defaults | values),
    )


def make_state_inputs(**values: Any) -> StateInputs:
    """
    Return what the state of an idle, available player without any controls is derived from.

    :param values: The values that differ from those inputs.
    """
    player_id = values.get("player_id", "player_1")
    defaults: dict[str, Any] = {
        "player_id": player_id,
        "provider": "test_prov",
        "player_type": PlayerType.PLAYER,
        "now": 1000.0,
        "previous_state": make_player_state(player_id),
        "enabled": True,
        "name": player_id,
        "device_name": None,
        "default_name": None,
        "hide_in_ui": False,
        "expose_to_ha": True,
        "icon": "speaker",
        "legacy_ids": [],
        "last_seen": None,
        "unavailable_since": None,
        "power_control": PLAYER_CONTROL_NONE,
        "volume_control": PLAYER_CONTROL_NONE,
        "mute_control": PLAYER_CONTROL_NONE,
        "powered": None,
        "volume_level": None,
        "volume_muted": None,
        "offline_by_power_control": False,
        "own": make_protocol_snapshot(player_id),
        "device_info": DeviceInfo(),
        "private": False,
        "trusts_reported_source": False,
        "grouping_locked": False,
        "active_sound_mode": None,
        "sound_mode_list": UniqueList(),
        "options": UniqueList(),
        "static_group_members": [],
        "can_group_with": set(),
        "protocol_can_group_with": set(),
        "has_setup_flow": False,
        "linked": (),
        "active_protocol": None,
        "foreign_app": None,
        "output_protocols": [],
        "protocols": [],
        "active_output_protocol": None,
        "active_mass_source": None,
        "sleep_timer_expires_at": None,
        "sleep_timer_mode": None,
        "extra_attributes": {},
        "palette_url": None,
        "palette": None,
        "peers": {},
        "groups": (),
        "queues": {},
        "mass_sources": frozenset(),
        "session": None,
        "session_image_url": None,
        "standing_sources": (),
    }
    return StateInputs(**(defaults | values))


class MockMass:
    """Type hint for mocked MusicAssistant instance."""
