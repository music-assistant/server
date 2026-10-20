"""Shared fixtures for the Bandcamp provider tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "bandcamp"
    return manifest
