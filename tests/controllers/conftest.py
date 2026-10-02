"""Shared fixtures for the controller tests."""

from __future__ import annotations

import pytest

from music_assistant.controllers.storage.backends import mountinfo


@pytest.fixture
def discoverable_tmp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Let storage discovery find a mount in the temporary folder of a test.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    # the temporary folder of the tests may lie below a system path, which discovery leaves out
    monkeypatch.setattr(mountinfo, "SYSTEM_PATHS", ())
