"""Shared fixtures for the Local files provider tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest

from music_assistant.controllers.storage import StorageController

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.fixture
async def storage(mass_minimal: MusicAssistant) -> AsyncGenerator[StorageController]:
    """
    Provide a storage controller on a minimal server, not set up (no background refresh).

    Hold locations with the helpers of tests/controllers/storage/conftest.py.

    :param mass_minimal: The minimal server to attach the controller to.
    """
    controller = StorageController(mass_minimal)
    mass_minimal.storage = controller
    try:
        yield controller
    finally:
        await controller.close()
