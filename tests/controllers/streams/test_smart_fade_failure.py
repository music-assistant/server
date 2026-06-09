"""Tests for smart-crossfade FFmpeg failure detection and reporting."""

from __future__ import annotations

from music_assistant.controllers.streams.smart_fades.fades import SmartFade


def test_no_failure_on_clean_exit_with_output() -> None:
    """A clean exit (rc=0) that produced output is not a failure."""
    assert SmartFade._ffmpeg_failure_reason(0, got_output=True, stderr_lines=[]) is None


def test_no_failure_when_returncode_unknown_with_output() -> None:
    """A still-unknown return code with output is not a failure."""
    assert SmartFade._ffmpeg_failure_reason(None, got_output=True, stderr_lines=[]) is None


def test_nonzero_returncode_reports_rc_and_stderr() -> None:
    """A non-zero exit reports the return code and captured stderr."""
    reason = SmartFade._ffmpeg_failure_reason(1, got_output=True, stderr_lines=["boom", "kaput"])
    assert reason is not None
    assert "rc=1" in reason
    assert "boom; kaput" in reason


def test_no_output_with_clean_exit_is_not_reported_as_rc_failure() -> None:
    """
    A clean exit that produced no output is a distinct failure mode.

    Regression: previously this rendered as the misleading
    "Smart crossfade FFmpeg failed (rc=0): (no stderr)", which reads like a
    self-contradiction and hides the real cause (empty output).
    """
    reason = SmartFade._ffmpeg_failure_reason(0, got_output=False, stderr_lines=[])
    assert reason is not None
    assert "no output" in reason
    assert "rc=0" not in reason
