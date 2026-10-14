"""Fixtures for BookOrbit provider tests."""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())  # type: ignore[no-any-return]


@pytest.fixture
def book_audiobook() -> dict[str, Any]:
    """Return a single-file m4b BookDetail with embedded chapters."""
    return _load("book_audiobook.json")


@pytest.fixture
def book_multifile() -> dict[str, Any]:
    """Return a multi-file mp3 BookDetail (plus an epub) without embedded chapters."""
    return _load("book_multifile.json")
