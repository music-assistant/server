"""Tests for the always-on diagnostics facility."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from music_assistant.controllers.diagnostics import DiagnosticsController
from music_assistant.helpers.diagnostics import (
    LOG_RING_MAXLEN,
    MAX_EXCEPTION_FINGERPRINTS,
    DiagnosticsLogHandler,
    install_diagnostics_log_handler,
    sanitize_data,
    sanitize_text,
)
from music_assistant.helpers.json import json_dumps

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        # home directories
        ("error in /Users/johndoe/.musicassistant/file", ["johndoe"], ["~/.musicassistant"]),
        ("error in /home/johndoe/music-assistant/data", ["johndoe"], ["~"]),
        (r"error in C:\Users\johndoe\AppData", ["johndoe"], ["~"]),
        # media file paths reveal library content
        (
            "failed to open /media/Pink Floyd/The Wall/01 - In The Flesh.flac",
            ["Pink Floyd", "The Wall", "In The Flesh"],
            [".flac", "<path-"],
        ),
        ("cannot read Highway to Hell.mp3", ["Highway", "Hell"], [".mp3", "<path-"]),
        ("failed: '01 - In The Flesh.flac' not found", ["In The Flesh"], ["failed:", "not found"]),
        # URL credentials and query strings
        (
            "GET http://admin:hunter2@192.168.1.10/api?token=s3cr3t&x=1",
            ["admin:hunter2", "s3cr3t", "192.168.1.10"],
            ["<redacted>@", "<redacted-query>"],
        ),
        # query strings on relative request paths (e.g. OAuth callbacks)
        (
            "GET /callback?code=s3cr3tcode&state=abc failed",
            ["s3cr3tcode"],
            ["/callback?<redacted-query>", "failed"],
        ),
        # secret assignments in all common shapes
        ("password=hunter2", ["hunter2"], ["password=<redacted>"]),
        ('"api_key": "abc-def-123"', ["abc-def-123"], ["<redacted>"]),
        ("Authorization: Bearer SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV", ["SflKxwRJ"], ["<redacted>"]),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpM",
            ["eyJhbGci"],
            ["<redacted-token>"],
        ),
        # long token blobs
        ("blob A1b2C3d4E5f6A1b2C3d4E5f6A1b2C3d4E5f6", ["A1b2C3d4"], ["<redacted-token>"]),
        # e-mail addresses
        (
            "user john.doe+spam@example.com failed",
            ["john.doe", "example.com"],
            ["<redacted-email>"],
        ),
        # IP addresses (loopback stays)
        ("connect to 192.168.1.100 failed", ["192.168.1.100"], ["<redacted-ip>"]),
        ("connect to 2001:db8:85a3::8a2e:370:7334 failed", ["2001:db8"], ["<redacted-ip>"]),
        ("listening on 127.0.0.1 and ::1", [], ["127.0.0.1", "::1"]),
        ("connection.20:F8:3B:09:03:E2 closed", ["20:F8:3B"], ["<mac-"]),
        ("device 20-f8-3b-09-03-e2 offline", ["20-f8-3b"], ["<mac-"]),
        ("scan done at 01:06:02 (took 12:34:56)", [], ["01:06:02", "12:34:56"]),
        # timestamps and version numbers must survive
        ("at 12:34:56.789 version 1.2.3 happened", [], ["12:34:56.789", "1.2.3"]),
    ],
)
def test_sanitize_text(raw: str, must_not_contain: list[str], must_contain: list[str]) -> None:
    """
    Test the sanitizer against adversarial fixtures.

    :param raw: The raw input text.
    :param must_not_contain: Substrings that may not survive sanitization.
    :param must_contain: Substrings that must be present after sanitization.
    """
    result = sanitize_text(raw)
    for fragment in must_not_contain:
        assert fragment not in result, f"{fragment!r} leaked into {result!r}"
    for fragment in must_contain:
        assert fragment in result, f"{fragment!r} missing from {result!r}"


def test_sanitize_text_code_paths() -> None:
    """Test that absolute code paths are rewritten to be relative to the app root."""
    raw = 'File "/opt/venv/lib/python3.13/site-packages/aiohttp/web.py", line 12'
    assert "/opt/venv" not in sanitize_text(raw)
    assert 'File "aiohttp/web.py", line 12' in sanitize_text(raw)
    raw = 'File "/opt/app/music_assistant/controllers/music.py", line 5'
    assert "/opt/app" not in sanitize_text(raw)
    assert 'File "music_assistant/controllers/music.py", line 5' in sanitize_text(raw)


def test_sanitize_data_recurses() -> None:
    """Test that sanitize_data sanitizes string values in nested structures."""
    data: dict[str, Any] = {
        "outer": [{"msg": "password=hunter2"}, "mail me at a@b.com"],
        "count": 42,
        "flag": True,
        "point": (1, "b@c.com"),
        "/home/marcel/Music/secret song.mp3": "path as key",
    }
    result = sanitize_data(data)
    assert result["outer"][0]["msg"] == "password=<redacted>"
    assert "a@b.com" not in result["outer"][1]
    assert result["count"] == 42
    assert result["flag"] is True
    assert result["point"] == (1, "<redacted-email>")
    # dict keys must be sanitized too
    assert not any("secret song" in key for key in result)


def _emit_exception(handler: DiagnosticsLogHandler, message: str = "it broke") -> None:
    """Raise a ValueError and emit it as an exception log record to the given handler."""
    logger = logging.getLogger("test.diagnostics")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception(message)
    finally:
        logger.removeHandler(handler)


def test_exception_aggregation() -> None:
    """Test that repeated exceptions aggregate on one fingerprint with count."""
    handler = DiagnosticsLogHandler()
    _emit_exception(handler)
    _emit_exception(handler)
    _, exceptions = handler.snapshot()
    assert len(exceptions) == 1
    entry = exceptions[0]
    assert entry.count == 2
    assert entry.exc_type == "ValueError"
    assert entry.logger_name == "test.diagnostics"
    assert "ValueError: boom" in entry.render_traceback()
    assert entry.last_seen >= entry.first_seen


def test_exception_lru_bound() -> None:
    """Test that the exception aggregation stays bounded (LRU eviction)."""
    handler = DiagnosticsLogHandler()
    for index in range(MAX_EXCEPTION_FINGERPRINTS + 20):
        # unique exception type per iteration -> unique fingerprint
        exc_type = type(f"CustomError{index}", (Exception,), {})
        try:
            raise exc_type("boom")
        except Exception:
            record = logging.LogRecord(
                "test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )
            handler.emit(record)
    _, exceptions = handler.snapshot()
    assert len(exceptions) == MAX_EXCEPTION_FINGERPRINTS


def test_log_ring_bound_and_level() -> None:
    """Test that the log ring is bounded and only captures WARNING and above."""
    handler = DiagnosticsLogHandler()
    logger = logging.getLogger("test.diagnostics.ring")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info("not captured")
        for index in range(LOG_RING_MAXLEN + 50):
            logger.warning("warning %s", index)
    finally:
        logger.removeHandler(handler)
    records, _ = handler.snapshot()
    assert len(records) == LOG_RING_MAXLEN
    assert all(record.level == "WARNING" for record in records)
    assert records[-1].message == f"warning {LOG_RING_MAXLEN + 49}"


def test_emit_never_raises() -> None:
    """Test that a poisoned log record cannot break the capture handler."""
    handler = DiagnosticsLogHandler()
    # args/format mismatch makes record.getMessage() raise
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "%d", ("nan",), None)
    handler.emit(record)  # must not raise


def test_emit_does_no_sanitization_work() -> None:
    """Test that the always-on capture path never invokes the (expensive) sanitizer."""
    handler = DiagnosticsLogHandler()
    with patch("music_assistant.helpers.diagnostics.sanitize_text") as mock_sanitize:
        _emit_exception(handler)
        mock_sanitize.assert_not_called()


def test_emit_does_no_disk_io() -> None:
    """Test that capturing an exception never reads source files (linecache)."""
    handler = DiagnosticsLogHandler()
    with (
        patch("linecache.getline", side_effect=AssertionError("linecache hit on emit")),
        patch("linecache.updatecache", side_effect=AssertionError("linecache hit on emit")),
    ):
        _emit_exception(handler)
    _, exceptions = handler.snapshot()
    assert len(exceptions) == 1


def test_install_diagnostics_log_handler_idempotent() -> None:
    """Test that installing the capture handler twice returns the same instance."""
    handler = install_diagnostics_log_handler()
    try:
        assert install_diagnostics_log_handler() is handler
        assert logging.getLogger().handlers.count(handler) == 1
    finally:
        logging.getLogger().removeHandler(handler)


async def test_get_report(mass: MusicAssistant) -> None:
    """
    Test the full report: shape, bounded size, sanitization and JSON serializability.

    :param mass: Full Music Assistant test instance.
    """
    logging.getLogger("music_assistant.test").warning("test warning for the ring")
    # device identifiers embedded in logger names must be redacted too
    logging.getLogger("aiosendspin.server.connection.20:F8:3B:09:03:E2").warning("closed")
    try:
        raise RuntimeError("report traceback probe")
    except RuntimeError:
        logging.getLogger("music_assistant.test").exception("probe failed")
    report = await mass.diagnostics.get_report()
    assert report["schema_version"] == 1
    assert "redaction_notice" in report
    assert report["system"]["python_version"]
    assert report["system"]["counts"]["threads"] > 0
    assert isinstance(report["install"]["providers"], list)
    assert isinstance(report["install"]["library"]["tracks"], int)
    assert isinstance(report["exceptions"], list)
    assert any(
        entry["type"] == "RuntimeError" and "report traceback probe" in entry["traceback"]
        for entry in report["exceptions"]
    )
    # streams controller contributes its section through the get_diagnostics hook
    assert "active_output_streams" in report["sections"]["core.streams"]
    # log tail is opt-in
    assert "log_tail" not in report
    report_with_tail = await mass.diagnostics.get_report(include_log_tail=True)
    messages = [record["message"] for record in report_with_tail["log_tail"]]
    assert "test warning for the ring" in messages
    loggers = [record["logger"] for record in report_with_tail["log_tail"]]
    assert "aiosendspin.server.connection.<mac-7f85b52c>" in loggers
    # the whole report must be JSON serializable and stay small
    assert len(json_dumps(report_with_tail)) < 100_000


async def test_get_report_command_admin_only(mass: MusicAssistant) -> None:
    """
    Test that the diagnostics/get API command is registered with admin-only scope.

    :param mass: Full Music Assistant test instance.
    """
    handler = mass.command_handlers["diagnostics/get"]
    assert handler.required_role == "admin"


async def test_register_section(mass: MusicAssistant) -> None:
    """
    Test section registration: contribution, duplicates and unregistration.

    :param mass: Full Music Assistant test instance.
    """

    async def async_section() -> dict[str, Any]:
        return {"queued_jobs": 3}

    unregister = mass.diagnostics.register_section("profiler", async_section)
    unregister_sync = mass.diagnostics.register_section("sync_section", lambda: {"value": 1})
    with pytest.raises(ValueError, match="already registered"):
        mass.diagnostics.register_section("profiler", async_section)
    report = await mass.diagnostics.get_report()
    assert report["sections"]["profiler"] == {"queued_jobs": 3}
    assert report["sections"]["sync_section"] == {"value": 1}
    unregister()
    unregister_sync()
    report = await mass.diagnostics.get_report()
    assert "profiler" not in report["sections"]
    assert "sync_section" not in report["sections"]
    # a stale unregister handle must not remove a newer registration with the same name
    mass.diagnostics.register_section("profiler", lambda: {"value": 2})
    unregister()
    report = await mass.diagnostics.get_report()
    assert report["sections"]["profiler"] == {"value": 2}


async def test_section_failure_isolation(mass: MusicAssistant) -> None:
    """
    Test that a broken or slow section cannot break the report.

    :param mass: Full Music Assistant test instance.
    """

    def broken_section() -> dict[str, Any]:
        raise RuntimeError("contributor exploded with secret password=hunter2")

    async def slow_section() -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    unregister_broken = mass.diagnostics.register_section("broken", broken_section)
    unregister_slow = mass.diagnostics.register_section("slow", slow_section)
    try:
        with patch("music_assistant.controllers.diagnostics.SECTION_TIMEOUT", 0.1):
            report = await mass.diagnostics.get_report()
        assert "hunter2" not in report["sections"]["broken"]["error"]
        assert "RuntimeError" in report["sections"]["broken"]["error"]
        assert "error" in report["sections"]["slow"]
        # healthy sections are unaffected
        assert "core.streams" in report["sections"]
    finally:
        unregister_broken()
        unregister_slow()


async def test_section_sanitization(mass: MusicAssistant) -> None:
    """
    Test that section content is sanitized in depth.

    :param mass: Full Music Assistant test instance.
    """
    unregister = mass.diagnostics.register_section(
        "leaky", lambda: {"nested": ["mail a@b.com", {"file": "/music/Artist/song.flac"}]}
    )
    try:
        report = await mass.diagnostics.get_report()
        leaky = json_dumps(report["sections"]["leaky"])
        assert "a@b.com" not in leaky
        assert "Artist" not in leaky
    finally:
        unregister()


async def test_no_background_work(mass: MusicAssistant) -> None:
    """
    Test that the diagnostics facility schedules no background work of its own.

    :param mass: Full Music Assistant test instance.
    """
    assert isinstance(mass.diagnostics, DiagnosticsController)
    assert not [task for task in mass._tracked_tasks if "diagnostics" in task.lower()]
    assert not [timer for timer in mass._tracked_timers if "diagnostics" in timer.lower()]
