"""Shared fixtures for the storage controller tests."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Callable, Iterator
from pathlib import Path

import pytest

from music_assistant.controllers.storage import (
    StorageController,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.storage.controller import _ProbeResult, _ProbeState
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
        self.text = ""

    def set(self, *lines: str) -> None:
        """
        Replace the mounts in the table.

        :param lines: The mountinfo lines of the table.
        """
        self.text = "\n".join(lines)


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


async def wait_until(condition: Callable[[], bool]) -> None:
    """
    Wait until a condition holds, failing after two seconds.

    :param condition: The condition to wait for.
    """
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.01)
