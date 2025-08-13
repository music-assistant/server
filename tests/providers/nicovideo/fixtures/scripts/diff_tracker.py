"""Fixture difference tracking and display utilities."""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path

from tests.providers.nicovideo.constants import GENERATED_FIXTURES_DIR
from tests.providers.nicovideo.types import JsonContainer

logger = logging.getLogger(__name__)


class FixtureDiffTracker:
    """Tracks and displays differences between fixture versions."""

    def __init__(self) -> None:
        """Initialize the diff tracker."""
        self.changed_fixtures: list[str] = []  # Track changed fixtures
        self.new_fixtures: list[str] = []  # Track new fixtures

    def load_existing_fixture(self, fixture_path: Path) -> str | None:
        """Load existing fixture content if it exists."""
        if fixture_path.exists():
            try:
                with fixture_path.open("r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Could not read existing fixture {fixture_path}: {e}")
        return None

    def format_fixture_content(self, data: JsonContainer) -> str:
        """Format fixture data as JSON string for comparison."""
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    def log_fixture_diff(self, fixture_path: Path, old_content: str, new_content: str) -> None:
        """Log the diff between old and new fixture content."""
        if old_content == new_content:
            return

        logger.info(f"\n{'=' * 60}")
        logger.info(f"FIXTURE CHANGED: {fixture_path.relative_to(GENERATED_FIXTURES_DIR)}")
        logger.info(f"{'=' * 60}")

        # Generate unified diff
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        diff_lines = list(
            difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after", n=3)
        )

        if diff_lines:
            for line in diff_lines:
                # Color coding for terminal output
                if line.startswith(("---", "+++", "@@")):
                    logger.info(f"{line.rstrip()}")
                elif line.startswith(("-", "+")):
                    logger.info(f"E   {line.rstrip()}")
                else:
                    logger.info(f"    {line.rstrip()}")
        else:
            logger.info("(No differences found)")

        logger.info(f"{'=' * 60}\n")

    def track_fixture_changes(self, data: JsonContainer, fixture_path: Path) -> None:
        """Track fixture changes and log differences.

        This method only handles diff tracking and change detection.
        File saving should be done externally.
        """
        # Load existing content
        old_content = self.load_existing_fixture(fixture_path)

        # Format new content
        new_content = self.format_fixture_content(data)

        # Track changes and log diff if there was existing content
        relative_path = str(fixture_path.relative_to(GENERATED_FIXTURES_DIR))
        if old_content is not None:
            if old_content != new_content:
                self.log_fixture_diff(fixture_path, old_content, new_content)
                self.changed_fixtures.append(relative_path)
        else:
            logger.info(f"NEW FIXTURE: {relative_path}")
            self.new_fixtures.append(relative_path)

    def log_summary(self) -> None:
        """Log a summary of all fixture changes."""
        logger.info(f"\n{'=' * 60}")
        logger.info("FIXTURE GENERATION SUMMARY")
        logger.info(f"{'=' * 60}")

        if self.new_fixtures:
            logger.info(f"NEW FIXTURES ({len(self.new_fixtures)}):")
            for fixture in self.new_fixtures:
                logger.info(f"  + {fixture}")

        if self.changed_fixtures:
            logger.info(f"CHANGED FIXTURES ({len(self.changed_fixtures)}):")
            for fixture in self.changed_fixtures:
                logger.info(f"  ~ {fixture}")

        if not self.new_fixtures and not self.changed_fixtures:
            logger.info("No changes detected in any fixtures.")

        logger.info(f"{'=' * 60}\n")
