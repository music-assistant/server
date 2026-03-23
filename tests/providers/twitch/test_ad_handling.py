"""Test Twitch ad handling — silence injection and passthrough."""

# ruff: noqa: PLC0415
# Imports must be inside test functions because we inject fake
# streamlink modules into sys.modules via the autouse fixture.

from __future__ import annotations

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
    """Mock Streamlink TwitchHLSStreamWriter base class."""

    _prev_was_ad: bool = False

    def __init__(self) -> None:
        """Initialize with a mock reader/buffer."""
        self.reader = Mock()
        self.reader.buffer = Mock()

    def write(self, segment: Any, result: Any, *data: Any) -> None:
        """Store segment content for verification."""
        self.reader.buffer.write(result.content)

    def should_filter_segment(self, segment: Any) -> bool:
        """Return False — never filter segments."""
        return False


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


def test_silence_patch_applies_without_error() -> None:
    """Silence mode monkey-patch applies without import/attribute errors."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")
    # After patching, __writer__ should be a different class
    assert FakeTwitchHLSStreamReader.__writer__ is not FakeTwitchHLSStreamWriter


def test_passthrough_patch_applies_without_error() -> None:
    """Passthrough mode monkey-patch applies without errors."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("passthrough")
    assert FakeTwitchHLSStreamReader.__writer__ is not FakeTwitchHLSStreamWriter


def test_patch_does_not_affect_non_ad_segments() -> None:
    """Normal (non-ad) segments pass through unchanged in silence mode."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    segment = FakeTwitchHLSSegment(ad=False, num=1, duration=2.0)
    result = Mock()
    result.content = b"real_audio_data"

    # Call write — should delegate to super (FakeTwitchHLSStreamWriter.write)
    writer._prev_was_ad = False
    writer.write(segment, result)

    # Buffer should have received the real content (via super().write)
    writer.reader.buffer.write.assert_called()


# --- Silence Mode ---


def test_ad_segment_replaced_with_silence() -> None:
    """When segment.ad=True, output bytes are silence data, not ad bytes."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=1.0)
    result = Mock()
    result.raw = Mock()
    result.content = b"ad_audio_bytes"

    writer.write(segment, result)

    # Buffer should have received silence data, not ad bytes
    written = writer.reader.buffer.write.call_args[0][0]
    assert written != b"ad_audio_bytes"
    assert len(written) > 0


def test_silence_scaled_to_segment_duration() -> None:
    """2s ad segment gets 2 copies of 1s silence clip."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    # 1s segment
    seg_1s = FakeTwitchHLSSegment(ad=True, num=1, duration=1.0)
    result_1s = Mock()
    result_1s.raw = Mock()
    writer.write(seg_1s, result_1s)
    data_1s = writer.reader.buffer.write.call_args[0][0]

    writer.reader.buffer.reset_mock()

    # 2s segment — should be 2x the silence data
    seg_2s = FakeTwitchHLSSegment(ad=True, num=2, duration=2.0)
    result_2s = Mock()
    result_2s.raw = Mock()
    writer.write(seg_2s, result_2s)
    data_2s = writer.reader.buffer.write.call_args[0][0]

    assert len(data_2s) == 2 * len(data_1s)


def test_silence_fallback_for_zero_duration() -> None:
    """Ad segment with duration=0.0 gets 2 copies (2s fallback)."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    # Get 1-copy size for reference
    seg_1s = FakeTwitchHLSSegment(ad=True, num=1, duration=1.0)
    result_1s = Mock()
    result_1s.raw = Mock()
    writer.write(seg_1s, result_1s)
    one_copy_size = len(writer.reader.buffer.write.call_args[0][0])

    writer.reader.buffer.reset_mock()

    # 0s segment — should get 2 copies (2s fallback)
    seg_0s = FakeTwitchHLSSegment(ad=True, num=2, duration=0.0)
    result_0s = Mock()
    result_0s.raw = Mock()
    writer.write(seg_0s, result_0s)
    data_0s = writer.reader.buffer.write.call_args[0][0]

    assert len(data_0s) == 2 * one_copy_size


def test_silence_minimum_one_copy() -> None:
    """Very short ad segment (<0.5s) still gets at least 1 copy."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=0.1)
    result = Mock()
    result.raw = Mock()

    writer.write(segment, result)

    written = writer.reader.buffer.write.call_args[0][0]
    assert len(written) > 0  # at least one copy


def test_ad_bytes_discarded_via_drain() -> None:
    """Ad segment body is drained/discarded, not read into memory."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)
    result = Mock()
    result.raw = Mock()

    writer.write(segment, result)

    # drain_conn should have been called on the raw response
    result.raw.drain_conn.assert_called_once()


def test_ad_break_flag_set_on_ad() -> None:
    """ad_break_active set to True when ad segment processed."""
    import music_assistant.providers.twitch.ad_handling as ah

    ah.patch_ad_handling("silence")
    ah.ad_break_active = False

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)
    result = Mock()
    result.raw = Mock()

    writer.write(segment, result)
    assert ah.ad_break_active is True


def test_ad_break_flag_cleared_on_content() -> None:
    """ad_break_active set to False when non-ad segment follows."""
    import music_assistant.providers.twitch.ad_handling as ah

    ah.patch_ad_handling("silence")
    ah.ad_break_active = True

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)
    writer.reader = Mock()
    writer.reader.buffer = Mock()
    writer._prev_was_ad = True

    segment = FakeTwitchHLSSegment(ad=False, num=2, duration=2.0)
    result = Mock()
    result.content = b"content"

    writer.write(segment, result)
    assert ah.ad_break_active is False


# --- Passthrough Mode ---


def test_ad_segment_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Ad segment in passthrough mode is logged (debug level)."""
    import logging

    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("passthrough")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)

    with caplog.at_level(logging.DEBUG):
        writer.should_filter_segment(segment)

    assert any("ad segment" in r.message.lower() for r in caplog.records)


def test_ad_segment_passes_through() -> None:
    """In passthrough mode, should_filter_segment returns False for ad segments."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("passthrough")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=True, num=1, duration=2.0)
    assert writer.should_filter_segment(segment) is False


def test_passthrough_non_ad_also_passes() -> None:
    """In passthrough mode, non-ad segments also pass through."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("passthrough")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    writer = object.__new__(writer_cls)

    segment = FakeTwitchHLSSegment(ad=False, num=1, duration=2.0)
    assert writer.should_filter_segment(segment) is False


# --- Config → Mode Routing ---


def test_silence_mode_selected_when_config_is_silence() -> None:
    """When ad_handling="silence", silence monkey-patch is applied."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("silence")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    # Silence writer overrides `write`, not `should_filter_segment`
    assert hasattr(writer_cls, "write")
    # Verify it's not the base class
    assert writer_cls is not FakeTwitchHLSStreamWriter


def test_passthrough_mode_selected_when_config_is_passthrough() -> None:
    """When ad_handling="passthrough", passthrough monkey-patch is applied."""
    from music_assistant.providers.twitch.ad_handling import patch_ad_handling

    patch_ad_handling("passthrough")

    writer_cls = FakeTwitchHLSStreamReader.__writer__
    # Passthrough writer overrides `should_filter_segment`
    assert hasattr(writer_cls, "should_filter_segment")
    assert writer_cls is not FakeTwitchHLSStreamWriter
