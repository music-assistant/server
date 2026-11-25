"""Fixtures for script tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer Git configuration out of script tests and their subprocesses."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
