"""Fixture management utilities for nicovideo tests."""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from pydantic import BaseModel

from tests.providers.nicovideo.types import JsonContainer


class FixtureLoader:
    """Loads and validates test fixtures with type validation."""

    def __init__(self, fixtures_dir: pathlib.Path) -> None:
        """Initialize the fixture manager with the directory containing fixtures."""
        self.fixtures_dir = fixtures_dir

    def load_fixture(self, relative_path: pathlib.Path) -> BaseModel | list[BaseModel] | None:
        """Load and validate a JSON fixture against its expected type."""
        data = self._load_json_fixture(relative_path)

        fixture_type = self._get_fixture_type_from_path(relative_path)
        if fixture_type is None:
            pytest.fail(f"Unknown fixture type for {relative_path}")

        try:
            if isinstance(data, list):
                return [fixture_type.model_validate(item) for item in data]
            else:
                # Single object case
                return fixture_type.model_validate(data)
        except Exception as e:
            pytest.fail(f"Failed to validate fixture {relative_path}: {e}")

    def _get_fixture_type_from_path(self, relative_path: pathlib.Path) -> type[BaseModel] | None:
        from tests.providers.nicovideo.fixture_data.fixture_type_mappings import (  # noqa: PLC0415 - Because it does not exist before generation
            FIXTURE_TYPE_MAPPINGS,
        )

        for key, fixture_type in FIXTURE_TYPE_MAPPINGS.items():
            if relative_path == pathlib.Path(key):
                return fixture_type
        return None

    def _load_json_fixture(self, relative_path: pathlib.Path) -> JsonContainer:
        """Load a JSON fixture file."""
        fixture_path = self.fixtures_dir / relative_path
        if not fixture_path.exists():
            pytest.skip(f"Fixture {fixture_path} not found")

        with fixture_path.open("r", encoding="utf-8") as f:
            return cast("JsonContainer", json.load(f))
