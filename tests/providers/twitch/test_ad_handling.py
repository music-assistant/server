"""Test Twitch ad handling — passthrough with ad break tracking."""

# ruff: noqa: PLC0415
# Imports must be inside test functions because we inject fake
# streamlink modules into sys.modules via the autouse fixture.

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

# --- Mock Streamlink Classes ---
# Streamlink is not installed in the test environment (runtime dep),
# so we create mock base classes and inject them into sys.modules
# before importing ad_handling.


class FakeTwitchHLSSegment:
    """Mock Streamlink TwitchHLSSegment."""

    def __init__(self, *, ad: bool = False, num: int = 0, duration: float = 2.0) -> None:
        """Initialize fake segment."""
        self.ad = ad
        self.num = num
        self.duration = duration


class FakeTwitchHLSStreamWriter:
    """Mock Streamlink TwitchHLSStreamWriter base class.

    Mirrors real TwitchHLSStreamWriter behavior:
    - should_filter_segment returns segment.ad (filters ads by default)
    """

    def __init__(self) -> None:
        """Initialize with a mock reader/buffer."""
        self.reader = Mock()
        self.reader.buffer = Mock()

    def write(self, segment: Any, result: Any, *data: Any) -> None:
        """Store segment content for verification."""
        self.reader.buffer.write(result.content)

    def should_filter_segment(self, segment: Any) -> bool:
        """Return segment.ad — matches real TwitchHLSStreamWriter."""
        return bool(segment.ad)


class FakeTwitchHLSStreamReader:
    """Mock Streamlink TwitchHLSStreamReader."""

    __writer__ = FakeTwitchHLSStreamWriter


@pytest.fixture(autouse=True)
def _mock_streamlink_modules() -> Any:
    """Inject fake streamlink modules so ad_handling can import them."""
    twitch_module = ModuleType("streamlink.plugins.twitch")
    twitch_module.TwitchHLSSegment = FakeTwitchHLSSegment  # type: ignore[attr-defined]
    twitch_module.TwitchHLSStreamWriter = FakeTwitchHLSStreamWriter  # type: ignore[attr-defined]
    twitch_module.TwitchHLSStreamReader = FakeTwitchHLSStreamReader  # type: ignore[attr-defined]

    streamlink_module = ModuleType("streamlink")
    plugins_module = ModuleType("streamlink.plugins")

    saved = {}
    for key in ("streamlink", "streamlink.plugins", "streamlink.plugins.twitch"):
        saved[key] = sys.modules.get(key)
    sys.modules["streamlink"] = streamlink_module
    sys.modules["streamlink.plugins"] = plugins_module
    sys.modules["streamlink.plugins.twitch"] = twitch_module

    # Reset __writer__ before each test
    FakeTwitchHLSStreamReader.__writer__ = FakeTwitchHLSStreamWriter

    yield

    # Restore
    for key, val in saved.items():
        if val is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = val

    # Also clear ad_handling module cache so it reimports cleanly
    sys.modules.pop("music_assistant.providers.twitch.ad_handling", None)


# --- Monkey-Patch Application ---


def test_patch_targets_exist_in_streamlink() -> None:
    """TwitchHLSSegment, TwitchHLSStreamReader, TwitchHLSStreamWriter are importable."""
    from streamlink.plugins.twitch import (
        TwitchHLSSegment,
        TwitchHLSStreamReader,
        TwitchHLSStreamWriter,
    )

    assert TwitchHLSSegment is FakeTwitchHLSSegment  # type: ignore[comparison-overlap]
    assert TwitchHLSStreamReader is FakeTwitchHLSStreamReader  # type: ignore[comparison-overlap]
    assert TwitchHLSStreamWriter is FakeTwitchHLSStreamWriter  # type: ignore[comparison-overlap]


def test_passthrough_patch_applies_without_error() -> None:
    """Passthrough monkey-patch applies without errors."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling()
    assert FakeTwitchHLSStreamReader.__writer__ is not FakeTwitchHLSStreamWriter


def test_patch_does_not_affect_non_ad_segments() -> None:
    """Normal (non-ad) segments pass through unchanged."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling()

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=False, num=1, duration=2.0)
    assert writer.should_filter_segment(segment) is False


# --- Ad Break Flag Tracking ---


def test_ad_break_flag_set_on_ad() -> None:
    """ad_break_active set to True when ad segment processed."""
    import music_assistant.providers.twitch.ad_handling as ah

    ah.patch_ad_handling()
    ah.ad_break_active = False

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)
    writer.should_filter_segment(segment)
    assert ah.ad_break_active is True


def test_ad_break_flag_cleared_on_content() -> None:
    """ad_break_active set to False when non-ad segment follows."""
    import music_assistant.providers.twitch.ad_handling as ah

    ah.patch_ad_handling()
    ah.ad_break_active = True

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=False, num=2, duration=2.0)
    writer.should_filter_segment(segment)
    assert ah.ad_break_active is False


# --- Passthrough Behavior ---


def test_ad_segment_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Ad segment is logged at debug level."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling()

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)

    with caplog.at_level(logging.DEBUG):
        writer.should_filter_segment(segment)

    assert any("ad segment" in r.message.lower() for r in caplog.records)


def test_ad_segment_passes_through() -> None:
    """should_filter_segment returns False for ad segments."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling()

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)
    assert writer.should_filter_segment(segment) is False


def test_passthrough_non_ad_also_passes() -> None:
    """Non-ad segments also pass through."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling()

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=False, num=1, duration=2.0)
    assert writer.should_filter_segment(segment) is False


def test_ad_end_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Transition from ad to content is logged."""
    import music_assistant.providers.twitch.ad_handling as ah

    ah.patch_ad_handling()
    ah.ad_break_active = True

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=False, num=2, duration=2.0)

    with caplog.at_level(logging.DEBUG):
        writer.should_filter_segment(segment)

    assert any("ad block ended" in r.message.lower() for r in caplog.records)
