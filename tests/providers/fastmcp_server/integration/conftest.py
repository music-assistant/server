"""Pytest configuration local to opt-in live MCP tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker when tests run under the upstream MA pytest root."""
    config.addinivalue_line(
        "markers", "integration: tests requiring a live authenticated Music Assistant endpoint"
    )
