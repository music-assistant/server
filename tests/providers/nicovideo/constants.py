"""Common constants for nicovideo tests."""

from __future__ import annotations

import pathlib

# Test fixtures directories
_BASE_DIR = pathlib.Path(__file__).parent
FIXTURE_DATA_DIR = _BASE_DIR / "fixture_data"
GENERATED_FIXTURES_DIR = FIXTURE_DATA_DIR / "fixtures"
