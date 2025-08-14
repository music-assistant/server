"""Common constants for nicovideo tests."""

from __future__ import annotations

import pathlib

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
DUMMY_DESCRIPTION = "This is a dummy description for testing purposes."
DUMMY_COUNT = 1
DUMMY_SEARCH_ID = "dummy-search-id-for-testing"
DUMMY_DATETIME = "2025-01-01T00:00:00+09:00"
DUMMY_NICOSID = "dummy_nicosid_for_testing"
DUMMY_TRACK_ID = "dummy_track_id_for_testing"
DUMMY_JWT_TOKEN = "dummy.jwt.token.for.testing"
DUMMY_THUMBNAIL_URL = "https://resource.video.nimg.jp/web/img/series/no_thumbnail.png"
DUMMY_IS_PEAK_TIME = False
DUMMY_PLAYBACK_POSITION = 0.0
