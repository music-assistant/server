"""Shared fixtures for the controller tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.storage.backends import mountinfo
from tests.controllers.streams.stream_slot_rig import SingleStreamProvider, SlotRig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
def discoverable_tmp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Let storage discovery find a mount in the temporary folder of a test.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    # the temporary folder of the tests may lie below a system path, which discovery leaves out
    monkeypatch.setattr(mountinfo, "SYSTEM_PATHS", ())


@pytest.fixture
async def rig(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[SlotRig]:
    """
    Build the stream slot rig with every music source open to the queue's playback.

    A test can pass another provider class through indirect parametrization.

    :param request: Pytest request, with the provider class as its param, if any.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(audio_mod, "playback_sources", AsyncMock(return_value=(None, [])))
    rig = SlotRig(getattr(request, "param", SingleStreamProvider))
    yield rig
    await rig.close()
