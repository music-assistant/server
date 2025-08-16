"""Common constants for nicovideo tests."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

# Test fixtures directories
_BASE_DIR = pathlib.Path(__file__).parent
GENERATED_DIR = _BASE_DIR / "generated"
GENERATED_FIXTURES_DIR = GENERATED_DIR / "fixtures"
GENERATED_SNAPSHOTS_DIR = GENERATED_DIR / "snapshots"

# Sample test data IDs
SAMPLE_VIDEO_ID = "sm45285955"
SAMPLE_USER_ID = "68461151"
SAMPLE_MYLIST_ID = "78597499"
SAMPLE_SERIES_ID = "527007"

# Stabilization constants
DUMMY_COUNT = 1
DUMMY_DESCRIPTION = "This is a dummy description for testing purposes."


@dataclass(frozen=True)
class StabilizationInfo:
    """Information about how to stabilize a field."""

    pattern: str
    replacement_value: str | int | float | bool
    is_partial_match: bool = False

    def matches(self, field_name: str) -> bool:
        """Check if this stabilization info matches the given field name."""
        if self.is_partial_match:
            return self.pattern.lower() in field_name.lower()
        return self.pattern == field_name


# Centralized field stabilization rules
STABILIZATION_RULES: list[StabilizationInfo] = [
    # Exact matches
    StabilizationInfo("searchId", "dummy-search-id-for-testing"),
    StabilizationInfo("lastViewedAt", "2025-01-01T00:00:00+09:00"),
    StabilizationInfo("serverTime", "2025-01-01T00:00:00+09:00"),
    StabilizationInfo("registeredAt", "2025-01-01T00:00:00+09:00"),
    StabilizationInfo("nicosid", "dummy_nicosid_for_testing"),
    StabilizationInfo("watchTrackId", "dummy_track_id_for_testing"),
    StabilizationInfo("isPeakTime", False),
    StabilizationInfo(
        "thumbnailUrl", "https://resource.video.nimg.jp/web/img/series/no_thumbnail.png"
    ),
    StabilizationInfo("playbackPosition", 0.0),
    StabilizationInfo("hls_url", "https://dummy.hls.url/for/testing"),
    StabilizationInfo("domand_bid", "dummy_domand_bid_for_testing"),
    StabilizationInfo("threadKey", "dummy.jwt.token.for.testing"),
    StabilizationInfo("accessRightKey", "dummy.jwt.token.for.testing"),
    StabilizationInfo("editKey", "dummy.jwt.token.for.testing"),
    StabilizationInfo("views", DUMMY_COUNT),
    # Partial matches
    StabilizationInfo("description", DUMMY_DESCRIPTION, is_partial_match=True),
]
