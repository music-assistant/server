"""Generated converter tests using fixture test mappings.

This module provides automated converter testing for the Nicovideo provider.
The test system is type-safe with automatic fixture updates and parameterized
converter/type specification through common test functions.

Type System:
    - API Responses: Pydantic BaseModel (for JSON validation and fixture saving)
    - Converter Results: mashumaro DataClassDictMixin (for snapshot serialization)

Architecture Overview:
    1. Fixture Collection (fixtures/scripts/api_fixture_collector.py):
       - Collects API responses by calling Niconico APIs
       - Saves responses as JSON fixtures in generated/fixtures/

    2. Type Mapping (fixtures/fixture_type_mapping.py):
       - Maps fixture paths to their Pydantic types
       - Auto-generates generated/fixture_types.py

    3. Converter Mapping (fixtures/api_response_converter_mapping.py):
       - Defines which converter function to use for each API response type
       - Registry provides O(1) type -> converter lookup

    4. Test Execution (this file):
       - Loads fixtures using FixtureLoader
       - Applies converters via mapping registry
       - Validates results against snapshots


Adding New API Endpoints:
    See: tests/providers/nicovideo/fixtures/scripts/api_fixture_collector.py
    Add collection method and call from collect_all_fixtures()
    Note: API response types must inherit from Pydantic BaseModel


Adding New Converters:
    1. Implement converter: music_assistant/providers/nicovideo/converters/
       Note: Return types must inherit from mashumaro DataClassDictMixin
    2. Register: music_assistant/providers/nicovideo/converters/manager.py
    3. Add mapping: tests/providers/nicovideo/fixtures/api_response_converter_mapping.py

"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.providers.nicovideo.helpers import (
    to_dict_for_snapshot,
)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from syrupy.assertion import SnapshotAssertion

    from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager
    from tests.providers.nicovideo.fixtures.api_response_converter_mapping import (
        APIResponseConverterMappingRegistry,
        SnapshotableItem,
    )
    from tests.providers.nicovideo.fixtures.fixture_loader import FixtureLoader


from .constants import GENERATED_FIXTURES_DIR


class ConverterTestRunner:
    """Helper class to run converter tests with fixture files."""

    def __init__(
        self,
        mapping_registry: APIResponseConverterMappingRegistry,
        converter_manager: NicovideoConverterManager,
        fixture_loader: FixtureLoader,
        snapshot: SnapshotAssertion,
        fixtures_dir: Path,
    ) -> None:
        """Initialize the test runner."""
        self.mapping_registry = mapping_registry
        self.converter_manager = converter_manager
        self.fixture_loader = fixture_loader
        self.snapshot = snapshot
        self.fixtures_dir = fixtures_dir
        self.failed_tests: list[str] = []
        self.skipped_tests: list[str] = []

    def run_all_tests(self) -> None:
        """Execute converter tests for all fixture files."""
        # Recursively get all JSON files
        json_files = list(self.fixtures_dir.rglob("*.json"))

        if not json_files:
            pytest.skip("No fixture files found")

        for fixture_path in json_files:
            self._process_fixture_file(fixture_path)

        # Report results
        self._report_test_results()

    def _process_fixture_file(self, fixture_path: Path) -> None:
        """Process a single fixture file."""
        relative_path = fixture_path.relative_to(self.fixtures_dir)
        fixture_name = str(relative_path)

        try:
            # Load fixture data
            fixture_data = self.fixture_loader.load_fixture(relative_path)
            if fixture_data is None:
                self.failed_tests.append(f"{fixture_name}: Failed to load fixture")
                return

            fixture_list = fixture_data if isinstance(fixture_data, list) else [fixture_data]

            for fixture_index, fixture in enumerate(fixture_list):
                fixture_id = (
                    f"{fixture_name}[{fixture_index}]" if len(fixture_list) > 1 else fixture_name
                )
                # fixture is BaseModel type from FixtureLoader.load_fixture
                self._process_single_fixture(fixture_id, fixture)

        except Exception as e:
            self.failed_tests.append(f"{fixture_name}: {e}")

    def _process_single_fixture(self, fixture_id: str, fixture: BaseModel) -> None:
        """Process a single fixture within a fixture file."""
        try:
            # Get mapping directly by type
            mapping = self.mapping_registry.get_by_type(type(fixture))
            if mapping is None:
                # Skip if no mapping found
                self.skipped_tests.append(f"{fixture_id}: No mapping for {type(fixture).__name__}")
                return

            # Execute test
            converted_result = mapping.convert_func(fixture, self.converter_manager)
            if converted_result is None:
                self.skipped_tests.append(f"{fixture_id}: No conversion result")
                return

            # Process all converted items (handles both single and list results)
            self._process_all_converted_items(fixture_id, converted_result)

        except Exception as e:
            self.failed_tests.append(f"{fixture_id}: {e}")

    def _process_all_converted_items(
        self,
        base_fixture_id: str,
        converted_result: SnapshotableItem | list[SnapshotableItem],
    ) -> None:
        """Process all items in converted result (handles both single and list)."""
        # Convert to list for uniform processing
        items = converted_result if isinstance(converted_result, list) else [converted_result]

        for idx, item in enumerate(items):
            # Generate unique snapshot ID for each item
            snapshot_id = f"{base_fixture_id}_{idx}" if len(items) > 1 else base_fixture_id
            self._process_converted_result(snapshot_id, item)

    def _process_converted_result(
        self,
        snapshot_id: str,
        converted: SnapshotableItem,
    ) -> None:
        """Process a single converted result and compare with snapshot."""
        stable_dict = to_dict_for_snapshot(converted)

        # Compare with snapshot
        converted_snapshot = self.snapshot(name=snapshot_id)
        snapshot_matches = converted_snapshot == stable_dict

        if not snapshot_matches:
            # Get detailed diff information
            diff_lines = converted_snapshot.get_assert_diff()
            diff_summary = "\n".join(diff_lines[:10])  # Limit to first 10 lines
            if len(diff_lines) > 10:
                diff_summary += f"\n... ({len(diff_lines) - 10} more lines)"

            self.failed_tests.append(
                f"{snapshot_id}: Converted result doesn't match snapshot\nDiff:\n{diff_summary}"
            )

    def _report_test_results(self) -> None:
        """Report the final test results."""
        if self.failed_tests:
            error_msg = f"Failed tests ({len(self.failed_tests)}):\n" + "\n".join(
                f"  - {test}" for test in self.failed_tests
            )
            pytest.fail(error_msg)

        if self.skipped_tests:
            skip_msg = f"Skipped tests ({len(self.skipped_tests)}):\n" + "\n".join(
                f"  - {test}" for test in self.skipped_tests
            )
            warnings.warn(skip_msg, stacklevel=2)


def test_converter_with_fixture(
    mapping_registry: APIResponseConverterMappingRegistry,
    converter_manager: NicovideoConverterManager,
    fixture_loader: FixtureLoader,
    snapshot: SnapshotAssertion,
) -> None:
    """Execute converter tests for all fixture files."""
    runner = ConverterTestRunner(
        mapping_registry=mapping_registry,
        converter_manager=converter_manager,
        fixture_loader=fixture_loader,
        snapshot=snapshot,
        fixtures_dir=GENERATED_FIXTURES_DIR,
    )

    runner.run_all_tests()
