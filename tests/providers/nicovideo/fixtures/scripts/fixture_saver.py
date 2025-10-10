"""Fixture data saving with integrated diff tracking."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from tests.providers.nicovideo.fixtures.scripts.diff_tracker import (
    FixtureDiffTracker,
)
from tests.providers.nicovideo.types import JsonContainer

logger = logging.getLogger(__name__)


class FixtureSaver:
    """Saves fixture data with integrated diff tracking."""

    def __init__(self) -> None:
        """Initialize the fixture data saver with diff tracking."""
        self.diff_tracker = FixtureDiffTracker()

    def save_fixture_data(self, data: JsonContainer, fixture_path: Path) -> None:
        """Save fixture data to file with diff tracking.

        Args:
            data: JSON serializable data to save
            fixture_path: Path where to save the fixture file
        """
        # Track changes before saving (loads existing file if it exists)
        self.diff_tracker.track_fixture_changes(data, fixture_path)

        # Save the file
        self._save_file(data, fixture_path)

    def _save_file(self, data: JsonContainer, fixture_path: Path) -> None:
        """Save fixture data to file."""
        # Create directory
        fixture_path.parent.mkdir(parents=True, exist_ok=True)

        # Format and save file
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with fixture_path.open("w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Saved fixture: {fixture_path}")

    def log_summary(self) -> None:
        """Log diff summary."""
        self.diff_tracker.log_summary()
