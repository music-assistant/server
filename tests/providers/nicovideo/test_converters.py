"""Generated converter tests using fixture test mappings."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.providers.nicovideo.fixtures.fixture_manager import FixtureManager
from tests.providers.nicovideo.helpers import (
    to_dict_for_snapshot,
)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from syrupy.assertion import SnapshotAssertion

    from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager
    from tests.providers.nicovideo.fixtures.type_to_converter_mapping import (
        SnapshotableItem,
        TypeToConverterMappingRegistry,
    )


from .constants import GENERATED_FIXTURES_DIR


class ConverterTestRunner:
    """Helper class to run converter tests with fixture files."""

    def __init__(
        self,
        mapping_registry: TypeToConverterMappingRegistry,
        converter_manager: NicovideoConverterManager,
        fixture_manager: FixtureManager,
        snapshot: SnapshotAssertion,
        fixtures_dir: Path,
    ) -> None:
        """Initialize the test runner."""
        self.mapping_registry = mapping_registry
        self.converter_manager = converter_manager
        self.fixture_manager = fixture_manager
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
            fixture_data = self.fixture_manager.load_fixture(relative_path)
            if fixture_data is None:
                self.failed_tests.append(f"{fixture_name}: Failed to load fixture")
                return

            fixture_list = fixture_data if isinstance(fixture_data, list) else [fixture_data]

            for fixture_index, fixture in enumerate(fixture_list):
                fixture_id = (
                    f"{fixture_name}[{fixture_index}]" if len(fixture_list) > 1 else fixture_name
                )
                # fixture is BaseModel type from FixtureManager.load_fixture
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

            # Convert to list for iteration
            if isinstance(converted_result, list):
                converted_list = converted_result
            else:
                converted_list = [converted_result]

            for converted_index, converted in enumerate(converted_list):
                snapshot_id = (
                    f"{fixture_id}_{converted_index}" if len(converted_list) > 1 else fixture_id
                )
                self._process_converted_result(snapshot_id, converted)

        except Exception as e:
            self.failed_tests.append(f"{fixture_id}: {e}")

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
    mapping_registry: TypeToConverterMappingRegistry,
    converter_manager: NicovideoConverterManager,
    fixture_manager: FixtureManager,
    snapshot: SnapshotAssertion,
) -> None:
    """Execute converter tests for all fixture files."""
    runner = ConverterTestRunner(
        mapping_registry=mapping_registry,
        converter_manager=converter_manager,
        fixture_manager=fixture_manager,
        snapshot=snapshot,
        fixtures_dir=GENERATED_FIXTURES_DIR,
    )

    runner.run_all_tests()
