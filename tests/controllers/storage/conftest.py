"""Shared fixtures for the storage controller tests."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from music_assistant.controllers.storage import (
    StorageController,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.backends import supervisor as supervisor_module
from music_assistant.controllers.storage.backends.base import (
    BackendUnavailable,
    ShareMounter,
    ShareState,
)
from music_assistant.controllers.storage.backends.local_mount import MOUNT_ROOT
from music_assistant.controllers.storage.controller import _ProbeResult, _ProbeState
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from music_assistant.helpers import hassio
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def storage(mass_minimal: MusicAssistant) -> AsyncGenerator[StorageController]:
    """
    Provide a storage controller on a minimal server, not set up (no background refresh).

    :param mass_minimal: The minimal server to attach the controller to.
    """
    controller = StorageController(mass_minimal)
    mass_minimal.storage = controller
    try:
        yield controller
    finally:
        await controller.close()


def make_location(
    path: Path | str,
    kind: StorageKind = StorageKind.CONTAINER_VOLUME,
    usage: StorageUsage = StorageUsage.MEDIA,
    mountpoint: str | None = None,
    managed: bool | None = None,
    available: bool = True,
) -> StorageLocation:
    """
    Return a storage location on a path.

    :param path: The path of the location.
    :param kind: Where the location comes from.
    :param usage: What the location is used for.
    :param mountpoint: The mount backing the location.
    :param managed: Whether Music Assistant created the location, by default for a folder only.
    :param available: Whether the location can be used right now.
    """
    return StorageLocation(
        path=str(path),
        name=Path(path).name,
        usage=usage,
        kind=kind,
        available=available,
        managed=kind == StorageKind.MANUAL if managed is None else managed,
        mountpoint=mountpoint,
    )


def set_locations(storage: StorageController, *locations: StorageLocation) -> None:
    """
    Give the storage controller these locations, each with a fresh probe answer.

    With fresh answers a caller probes nothing, so the list stays as given.

    :param storage: The storage controller.
    :param locations: The locations to hold.
    """
    storage._locations = list(locations)
    for location in locations:
        storage._probes[location.path] = _ProbeState(
            answer=FOLDER if location.available else None, answered_at=time.monotonic()
        )


def mount_line(mountpoint: Path | str, fstype: str = "cifs", optional: str = "") -> str:
    """
    Return a mountinfo line for a mount, escaped the way the kernel writes it.

    :param mountpoint: Where the filesystem is mounted.
    :param fstype: The filesystem type.
    :param optional: The optional fields of the line (e.g. ``shared:5 master:1``).
    """
    escaped = str(mountpoint).replace(" ", "\\040")
    return " ".join(
        (
            "100",
            "1",
            "0:50",
            "/",
            escaped,
            "rw,relatime",
            *optional.split(),
            "-",
            fstype,
            "src",
            "rw",
        )
    )


class MountTable:
    """Stand-in for the mount table of the server process."""

    def __init__(self) -> None:
        """Initialize an empty mount table."""
        self.lines: list[str] = []

    @property
    def text(self) -> str:
        """Return the table as the kernel writes it."""
        return "\n".join(self.lines)

    def set(self, *lines: str) -> None:
        """
        Replace the mounts in the table.

        :param lines: The mountinfo lines of the table.
        """
        self.lines = list(lines)

    def mount(self, mountpoint: str, fstype: str = "cifs") -> None:
        """
        Add a mount on top of the table.

        :param mountpoint: Where the filesystem is mounted.
        :param fstype: The filesystem type.
        """
        self.lines.append(mount_line(mountpoint, fstype))

    def unmount(self, mountpoint: str) -> None:
        """
        Remove every mount on a mountpoint.

        :param mountpoint: Where the filesystems are mounted.
        """
        self.lines = [line for line in self.lines if line.split()[4] != mountpoint]


class FakeProbes:
    """Stand-in for the filesystem probes, answering a folder with space unless told otherwise."""

    def __init__(self) -> None:
        """Initialize the fake probes."""
        self.results: dict[str, _ProbeResult | None] = {}
        self.blocked: dict[str, threading.Event] = {}
        self.calls: list[str] = []
        # called before a probe answers, like the wake-up of an automount trigger
        self.side_effects: dict[str, Callable[[], None]] = {}

    def __call__(self, path: str) -> _ProbeResult | None:
        """
        Probe a path, blocking while the path is blocked.

        :param path: The probed path.
        """
        self.calls.append(path)
        if (gate := self.blocked.get(path)) is not None:
            gate.wait(10)
        if (side_effect := self.side_effects.get(path)) is not None:
            side_effect()
        return self.results.get(path, FOLDER)

    def block(self, path: str) -> None:
        """
        Make the probes of a path block until released.

        :param path: The path whose probes block.
        """
        self.blocked[path] = threading.Event()

    def release(self) -> None:
        """Let every blocked probe answer."""
        for gate in self.blocked.values():
            gate.set()


FOLDER = _ProbeResult(is_dir=True, free_space_gb=75.0, total_space_gb=100.0)
FILE = _ProbeResult(is_dir=False)


@pytest.fixture
def mount_table(monkeypatch: pytest.MonkeyPatch) -> MountTable:
    """
    Provide the mount table the storage controller reads, empty at first.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    table = MountTable()
    monkeypatch.setattr(controller_module, "read_mountinfo", lambda: table.text)
    return table


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProbes]:
    """
    Provide the filesystem probes of the storage controller.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    fake = FakeProbes()
    monkeypatch.setattr(controller_module, "_probe_path", fake)
    try:
        yield fake
    finally:
        fake.release()


class FakeMounter(ShareMounter):
    """Stand-in for a mount backend, which mounts a share by adding it to the mount table."""

    def __init__(
        self, mount_table: MountTable, backend: MountBackend = MountBackend.LOCAL_MOUNT
    ) -> None:
        """
        Initialize the fake mounter.

        :param mount_table: The mount table the mounts land in.
        :param backend: The backend the mounter stands in for.
        """
        super().__init__({ShareType.CIFS: ["2.0", "3.0"], ShareType.NFS: []})
        self.backend = backend
        self.mount_table = mount_table
        # (operation, share name, decrypted password) of every call
        self.calls: list[tuple[str, str, str | None]] = []
        # the error a share on one of these servers fails to mount with
        self.failing: dict[str, Exception] = {}
        self.mounted: dict[str, NetworkShareSpec] = {}

    def get_path(self, name: str) -> str:
        """
        Return where a share with this name is mounted.

        :param name: The name of the share.
        """
        return f"{MOUNT_ROOT}/{name}"

    async def add(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share that is not mounted.

        :param spec: The share.
        :param password: The decrypted password of the share.
        """
        self.calls.append(("add", spec.name, password))
        self._mount(spec)

    async def update(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again with changed settings.

        :param spec: The share.
        :param password: The decrypted password of the share.
        """
        self.calls.append(("update", spec.name, password))
        self._unmount(spec)
        self._mount(spec)

    async def reload(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again.

        :param spec: The share.
        :param password: The decrypted password of the share.
        """
        self.calls.append(("reload", spec.name, password))
        self._unmount(spec)
        self._mount(spec)

    async def remove(self, spec: NetworkShareSpec) -> None:
        """
        Unmount a share.

        :param spec: The share.
        """
        self.calls.append(("remove", spec.name, None))
        self._unmount(spec)

    async def get_states(self, specs: list[NetworkShareSpec]) -> dict[str, ShareState]:
        """
        Return for each share, by name, whether it is mounted.

        :param specs: The shares of this backend.
        """
        return {
            spec.name: ShareState.PRESENT if spec.name in self.mounted else ShareState.MISSING
            for spec in specs
        }

    def _mount(self, spec: NetworkShareSpec) -> None:
        """Mount a share, unless its server is failing."""
        if (error := self.failing.get(spec.server)) is not None:
            raise error
        self.mounted[spec.name] = spec
        self.mount_table.mount(spec.path, "cifs" if spec.share_type == ShareType.CIFS else "nfs4")

    def _unmount(self, spec: NetworkShareSpec) -> None:
        """Unmount a share."""
        self.mounted.pop(spec.name, None)
        self.mount_table.unmount(spec.path)


class FakeBackends:
    """Stand-in for the probes of the mount backends: finds the mounters it was given."""

    def __init__(self) -> None:
        """Initialize without any available backend."""
        self.available: dict[MountBackend, ShareMounter] = {}
        self.probes = 0

    def factory(self, backend: MountBackend) -> Callable[..., Awaitable[ShareMounter]]:
        """
        Return the stand-in for the function that creates the mounter of a backend.

        :param backend: The backend.
        """

        async def create_mounter(*_args: object) -> ShareMounter:
            self.probes += 1
            if (mounter := self.available.get(backend)) is None:
                msg = f"{backend} is not available in this test"
                raise BackendUnavailable(msg)
            return mounter

        return create_mounter


@pytest.fixture(autouse=True)
def backends(monkeypatch: pytest.MonkeyPatch) -> FakeBackends:
    """
    Make probing the mount backends find only the mounters a test hands out, none by default.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    fake = FakeBackends()
    monkeypatch.setattr(
        controller_module, "create_supervisor_mounter", fake.factory(MountBackend.SUPERVISOR)
    )
    monkeypatch.setattr(
        controller_module, "create_local_mounter", fake.factory(MountBackend.LOCAL_MOUNT)
    )
    return fake


@pytest.fixture
def mounter(
    storage: StorageController, mount_table: MountTable, backends: FakeBackends
) -> FakeMounter:
    """
    Provide the storage controller with a mount backend that mounts in the mount table.

    :param storage: The storage controller.
    :param mount_table: The mount table the mounts land in.
    :param backends: The mount backends probing finds.
    """
    # the root filesystem: a Linux mount table is never empty
    mount_table.set(mount_line("/", "ext4"))
    fake = FakeMounter(mount_table)
    backends.available[fake.backend] = fake
    storage._mounters = {fake.backend: fake}
    return fake


@pytest.fixture(autouse=True)
def resolvable_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Resolve every host name except those in the .invalid domain, without a lookup.

    :param monkeypatch: Pytest monkeypatch fixture.
    """

    async def get_ip_from_host(host: str) -> str | None:
        return None if host.endswith(".invalid") else "192.0.2.10"

    monkeypatch.setattr(controller_module, "get_ip_from_host", get_ip_from_host)


SUPERVISOR_TOKEN = "app-token"


class FakeSupervisor:
    """
    A Supervisor that keeps its media mounts in memory and mounts them in the mount table.

    It answers the way the real one does: a mount whose share does not answer fails to be
    created and is not kept, a failed update leaves the previous mount unmounted, a missing
    mount is a 404, and listed mounts never carry their credentials. It takes a user and a
    password together or neither, creates the folder of a mount in the media folder and leaves it
    behind when the mount fails or goes, and refuses a mount on a folder that holds files. A
    mounted folder holds a file of the share, so it can not be removed.
    """

    def __init__(self, mount_table: MountTable, media: Path) -> None:
        """
        Initialize the fake Supervisor without any mount.

        :param mount_table: The mount table the mounts land in.
        :param media: The media folder the mounts show up in.
        """
        self.mount_table = mount_table
        self.media = media
        self.mounts: dict[str, dict[str, Any]] = {}
        # (method, path, body) of every request
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        # the app has no manager role
        self.refuse_access = False
        # servers that do not answer
        self.unreachable: set[str] = set()
        # mounts that stay behind their automount trigger although the Supervisor mounted them
        self.dormant: set[str] = set()
        # mounts the Supervisor fails to remove
        self.stuck: set[str] = set()
        self.app = web.Application(middlewares=[self._security])
        self.app.router.add_get("/mounts", self._list)
        self.app.router.add_post("/mounts", self._create)
        self.app.router.add_put("/mounts/{name}", self._update)
        self.app.router.add_delete("/mounts/{name}", self._delete)
        self.app.router.add_post("/mounts/{name}/reload", self._reload)

    def path(self, name: str) -> str:
        """
        Return where a mount with this name shows up.

        :param name: The name of the mount.
        """
        return str(self.media / name)

    def add_mount(self, name: str, **settings: Any) -> None:
        """
        Add a mount as if it was added in Home Assistant.

        :param name: The name of the mount.
        :param settings: The settings of the mount as the Supervisor takes them.
        """
        self.mounts[name] = {"name": name, "usage": "media", "read_only": False, **settings}
        self._activate(name)

    @web.middleware
    async def _security(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        """Refuse a request without the app's token, or any when the app has no manager role."""
        if request.headers.get("Authorization") != f"Bearer {SUPERVISOR_TOKEN}":
            raise web.HTTPUnauthorized
        if self.refuse_access:
            raise web.HTTPForbidden
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, body))
        return await handler(request)

    async def _list(self, _request: web.Request) -> web.Response:
        """List the mounts without their credentials."""
        mounts = [
            {key: value for key, value in mount.items() if key not in ("username", "password")}
            | {"state": "active", "user_path": f"/media/{name}"}
            for name, mount in self.mounts.items()
        ]
        return _ok({"default_backup_mount": None, "mounts": mounts})

    async def _create(self, request: web.Request) -> web.Response:
        """Create a mount, which is only kept when its share answers."""
        body = await request.json()
        if (invalid := _invalid(body)) is not None:
            return invalid
        name = body["name"]
        if name in self.mounts:
            return _error(400, f"A mount already exists with name {name}")
        folder = self.media / name
        if folder.is_dir() and any(folder.iterdir()):
            return _error(
                400,
                f"Cannot mount {name} because there is existing data at {folder}. "
                "Move it away first, then retry",
            )
        folder.mkdir(exist_ok=True)
        if body["server"] in self.unreachable:
            return _not_reachable(name)
        self.mounts[name] = body
        self._activate(name)
        return _ok({})

    async def _update(self, request: web.Request) -> web.Response:
        """Mount a mount again with new settings; a failure leaves it unmounted."""
        name = request.match_info["name"]
        if name not in self.mounts:
            return _error(404, f"No mount exists with name {name}")
        body = await request.json()
        if (invalid := _invalid(body)) is not None:
            return invalid
        self._deactivate(name)
        if body["server"] in self.unreachable:
            return _not_reachable(name)
        self.mounts[name] = body
        self._activate(name)
        return _ok({})

    async def _delete(self, request: web.Request) -> web.Response:
        """Remove a mount, leaving its folder behind."""
        name = request.match_info["name"]
        if name not in self.mounts:
            return _error(404, f"No mount exists with name {name}")
        if name in self.stuck:
            return _error(400, f"Could not unmount {name}. Check the Supervisor logs for details")
        self._deactivate(name)
        del self.mounts[name]
        return _ok({})

    async def _reload(self, request: web.Request) -> web.Response:
        """Mount a mount again."""
        name = request.match_info["name"]
        if name not in self.mounts:
            return _error(404, f"No mount exists with name {name}")
        self._deactivate(name)
        if self.mounts[name]["server"] in self.unreachable:
            return _not_reachable(name)
        self._activate(name)
        return _ok({})

    def _activate(self, name: str) -> None:
        """Put a mount in the mount table, the share on top of its automount trigger."""
        folder = self.media / name
        folder.mkdir(exist_ok=True)
        (folder / "share.txt").touch()
        self.mount_table.mount(str(folder), "autofs")
        if name not in self.dormant:
            self.mount_table.mount(str(folder), str(self.mounts[name]["type"]))

    def _deactivate(self, name: str) -> None:
        """Remove a mount from the mount table."""
        folder = self.media / name
        (folder / "share.txt").unlink(missing_ok=True)
        self.mount_table.unmount(str(folder))


def _ok(data: dict[str, Any]) -> web.Response:
    """Return a successful answer of the Supervisor."""
    return web.json_response({"result": "ok", "data": data})


def _error(status: int, message: str) -> web.Response:
    """Return an error answer of the Supervisor."""
    return web.json_response({"result": "error", "message": message}, status=status)


def _invalid(body: dict[str, Any]) -> web.Response | None:
    """Return the answer of the Supervisor to a mount with a user without password or back."""
    if ("username" in body) != ("password" in body):
        return _error(
            400,
            "some but not all values in the same group of inclusion 'basic_auth' @ "
            "data[<basic_auth>]",
        )
    return None


def _not_reachable(name: str) -> web.Response:
    """Return the answer of the Supervisor for a share that did not answer."""
    return web.json_response(
        {
            "result": "error",
            "message": f"Mount {name} is not reachable. Check the Supervisor logs for details",
            "error_key": "mount_activation_error",
            "extra_fields": {"name": name},
        },
        status=400,
    )


@pytest.fixture
async def supervisor(
    storage: StorageController,
    mount_table: MountTable,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[FakeSupervisor]:
    """
    Run the storage controller under a fake Supervisor, which is its only mount backend.

    :param storage: The storage controller.
    :param mount_table: The mount table the mounts land in.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the media folder.
    """
    # the root filesystem: a Linux mount table is never empty
    mount_table.set(mount_line("/", "ext4"))
    media = tmp_path / "media"
    media.mkdir()
    fake = FakeSupervisor(mount_table, media)
    server = TestServer(fake.app)
    await server.start_server()
    session = ClientSession()
    storage.mass._http_session_no_ssl = session
    storage.mass.running_as_hass_addon = True
    monkeypatch.setattr(hassio, "SUPERVISOR_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", SUPERVISOR_TOKEN)
    monkeypatch.setattr(supervisor_module, "SUPERVISOR_MEDIA_PATH", str(media))
    monkeypatch.setattr(
        controller_module, "create_supervisor_mounter", supervisor_module.create_supervisor_mounter
    )
    try:
        yield fake
    finally:
        await session.close()
        await server.close()


async def wait_until(condition: Callable[[], bool]) -> None:
    """
    Wait until a condition holds, failing after two seconds.

    :param condition: The condition to wait for.
    """
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.01)
