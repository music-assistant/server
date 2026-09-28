"""Shared fixtures for the storage controller tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from music_assistant.controllers.storage import (
    StorageController,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
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
) -> StorageLocation:
    """
    Return an available storage location on a path.

    :param path: The path of the location.
    :param kind: Where the location comes from.
    :param usage: What the location is used for.
    :param mountpoint: The mount backing the location.
    """
    return StorageLocation(
        path=str(path),
        name=Path(path).name,
        usage=usage,
        kind=kind,
        available=True,
        managed=kind == StorageKind.MANUAL,
        mountpoint=mountpoint,
    )
