"""Unit tests for SafeLogTail (path allowlist, byte cap, redactor)."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.debug.log_reader import (
    _MAX_RESPONSE_BYTES,
    SafeLogTail,
)
from music_assistant.providers.fastmcp_server.models import LogTailResult

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def test_tail_returns_last_n_lines(tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """SafeLogTail.tail returns exactly N requested lines."""
    tail = SafeLogTail()
    result = tail.tail(lines=5)
    assert len(result.lines) == 5
    assert result.bytes_scanned > 0


def test_tail_parses_real_ma_log_line_format(tmp_path: Path) -> None:
    """
    Pin parser support for both MA-runtime and Python-default log shapes.

    Reproduces the regression caught live in the dev container: real MA writes
    ``<ts> <LEVEL> (<thread>) [<component>] <msg>``, the synthetic fixture used
    ``<ts> <LEVEL> <component>: <msg>``. Without this pin the parser silently
    drops every real MA line to ``timestamp/level/component=None`` and
    ``debug_tail_log(level="ERROR")`` returns empty.
    """
    log_path = tmp_path / "musicassistant.log"
    log_path.write_text(
        # MA runtime format with (MainThread) and [bracket] component.
        "2026-05-28 19:02:21.989 INFO (MainThread) [mcp.server.lowlevel.server] Processing request\n"
        # Python default format with colon-after-component.
        "2026-05-28 09:00:00,001 INFO music_assistant.mass: Starting Music Assistant\n",
        encoding="utf-8",
    )

    setattr(SafeLogTail, "ROOT", tmp_path)  # noqa: B010 -- redirect class-level log root for this test
    try:
        result = SafeLogTail().tail(lines=10)
    finally:
        SafeLogTail.ROOT = Path.home() / ".musicassistant"

    assert len(result.lines) == 2
    by_component = {ln.component: ln for ln in result.lines}
    assert "mcp.server.lowlevel.server" in by_component
    assert by_component["mcp.server.lowlevel.server"].level == "INFO"
    assert "music_assistant.mass" in by_component
    assert by_component["music_assistant.mass"].level == "INFO"


def test_tail_prefers_mass_storage_path_over_class_root(tmp_path: Path) -> None:
    """
    When constructed with ``mass``, SafeLogTail reads from ``mass.storage_path``.

    Pins the regression that surfaced live in the dev container: MA is started
    with ``--data-dir /data`` so the real log lives at ``/data/musicassistant.log``,
    not at ``Path.home() / ".musicassistant"``. The class-level ``ROOT`` default
    is wrong for any non-default deployment.
    """
    log_path = tmp_path / "musicassistant.log"
    log_path.write_text("2026-05-28 09:00:00,001 INFO music_assistant.mass: hello\n")
    mass = cast("MusicAssistant", SimpleNamespace(storage_path=str(tmp_path)))

    tail = SafeLogTail(mass)
    result = tail.tail(lines=5)
    assert result.log_path == str(log_path)
    assert len(result.lines) == 1
    assert result.lines[0].message == "hello"
    assert result.truncated is False


def test_tail_redacts_bearer_token(tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """SafeLogTail redacts Authorization: Bearer tokens."""
    tail = SafeLogTail()
    result = tail.tail(lines=200)
    joined = "\n".join(line.message for line in result.lines)
    assert "abc.def.ghi" not in joined
    assert "<redacted>" in joined


def test_tail_redacts_query_string_secrets(tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """SafeLogTail redacts token= and password= query string values."""
    tail = SafeLogTail()
    result = tail.tail(lines=200)
    joined = "\n".join(line.message for line in result.lines)
    assert "secret_token_42" not in joined
    assert "hunter2" not in joined


def test_tail_filters_by_level(tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """SafeLogTail filters by log level."""
    tail = SafeLogTail()
    result = tail.tail(lines=200, level="ERROR")
    assert all(line.level == "ERROR" for line in result.lines)
    assert any("lookup failed" in line.message for line in result.lines)


def test_tail_filters_by_component_regex(tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """SafeLogTail filters by component regex."""
    tail = SafeLogTail()
    result = tail.tail(lines=200, component_regex=r"providers\.yandex.*")
    assert all(
        line.component and line.component.startswith("music_assistant.providers.yandex")
        for line in result.lines
    )


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "/etc/passwd",
        "musicassistant.log\x00.txt",
        "..",
        ".",
        "",
        "musicassistant.log.99",
    ],
)
def test_path_traversal_rejected(tmp_log_dir: Path, name: str) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """SafeLogTail rejects path traversal attempts."""
    tail = SafeLogTail()
    with pytest.raises(ToolError):
        tail.tail(lines=1, name=name)


def test_symlink_escape_rejected(tmp_log_dir: Path) -> None:
    """SafeLogTail rejects symlinks pointing outside ROOT."""
    outside = tmp_log_dir.parent / "outside.log"
    outside.write_text("LEAK\n")
    symlink = tmp_log_dir / "musicassistant.log.1"  # in allowlist by basename
    symlink.symlink_to(outside)

    tail = SafeLogTail()
    with pytest.raises(ToolError):
        tail.tail(lines=1, name="musicassistant.log.1")


def test_scan_bytes_cap_marks_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The 10 MB scan cap fires when filters keep skipping records.

    With filter-then-tail semantics the scan exits early once the page fills,
    so the cap is observable only when the filter never matches — the reader
    must then stop at 10 MB, flag ``truncated`` and return an empty page.
    """
    from music_assistant.providers.fastmcp_server.debug import log_reader  # noqa: PLC0415

    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    huge = tmp_path / "musicassistant.log"
    line = b"2026-05-28 09:00:00,001 INFO music_assistant.mass: filler\n"
    with huge.open("wb") as fh:
        # 20 MB of repeated, parseable lines.
        while fh.tell() < 20 * 1024 * 1024:
            fh.write(line)

    tail = log_reader.SafeLogTail()
    result = tail.tail(lines=10, level="CRITICAL")
    assert result.truncated is True
    assert result.lines == []
    assert result.bytes_scanned <= 10 * 1024 * 1024 + len(line)
    assert result.next_call_hint is not None


def test_scan_bytes_cap_drops_partial_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When the byte cap fires mid-line, the partial leading fragment must be dropped.

    Without the guard, the backwards line iterator would emit a tail substring
    of a real line as a headerless record — which slips past level filtering
    and confuses callers.
    """
    from music_assistant.providers.fastmcp_server.debug import log_reader  # noqa: PLC0415

    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    huge = tmp_path / "musicassistant.log"
    line = b"2026-05-28 09:00:00,001 INFO music_assistant.mass: filler\n"
    with huge.open("wb") as fh:
        # 11 MB of identical, parseable lines — enough to trigger the 10 MB cap.
        while fh.tell() < 11 * 1024 * 1024:
            fh.write(line)

    tail = log_reader.SafeLogTail()
    state = log_reader._ScanState()
    records = list(tail._iter_records_backwards(huge, state))
    assert state.truncated is True
    # Every surfaced record must be fully parsed — the partial fragment at the
    # cap boundary is dropped, never yielded as a headerless record.
    assert all(rec.entry.timestamp is not None for rec in records), (
        "partial leading fragment leaked: "
        + str([r.entry for r in records if r.entry.timestamp is None][:3])
    )


# ---- E2E tests via MCP transport (debug_tail_log tool) ----


async def test_e2e_debug_tail_log(mounted_debug: Any, tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """debug_tail_log tool returns the last 5 lines via MCP."""
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_tail_log", {"lines": 5})
    assert len(result.data.lines) == 5
    assert result.data.truncated is False


async def test_e2e_debug_tail_log_invalid_name(mounted_debug: Any, tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """debug_tail_log rejects path traversal attempts via MCP."""
    async with Client(mounted_debug) as client:
        with pytest.raises(ToolError):
            await client.call_tool("debug_tail_log", {"name": "../etc/passwd"})


async def test_debug_tail_log_runs_off_event_loop_thread(
    mounted_debug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The blocking log read is offloaded to a worker thread, not the event loop.

    Synchronous file I/O up to the 10 MB scan cap must not run on MA's single
    event loop. This pins that ``tail_log`` dispatches the read via a worker
    thread (it would fail if the tool called ``SafeLogTail.tail`` directly).
    """
    main_thread = threading.current_thread()
    captured: dict[str, Any] = {}

    def fake_tail(_self: Any, **_kwargs: Any) -> LogTailResult:
        captured["thread"] = threading.current_thread()
        return LogTailResult(log_path="x", lines=[], bytes_scanned=0, truncated=False)

    monkeypatch.setattr(SafeLogTail, "tail", fake_tail)
    async with Client(mounted_debug) as client:
        await client.call_tool("debug_tail_log", {"lines": 5})
    assert captured["thread"] is not main_thread


# ---- Spec 0017: filter-then-tail, record grouping, search, budgets, stats ----


def _write_log(tmp_path: Path, text: str) -> None:
    (tmp_path / "musicassistant.log").write_text(text, encoding="utf-8")


@pytest.fixture
def log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty sandboxed log root; tests write their own log content."""
    from music_assistant.providers.fastmcp_server.debug import log_reader  # noqa: PLC0415

    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    return tmp_path


def _line(ts_ms: int, level: str, component: str, msg: str) -> str:
    return f"2026-05-28 09:00:{ts_ms // 1000:02d},{ts_ms % 1000:03d} {level} {component}: {msg}\n"


def test_tail_filter_then_tail_finds_buried_errors(log_root: Path) -> None:
    """lines=N with level counts N *matching* records, not N raw lines."""
    body = _line(1000, "ERROR", "music_assistant.streams", "err one")
    body += _line(2000, "ERROR", "music_assistant.streams", "err two")
    for i in range(300):
        body += _line(3000 + i * 10, "INFO", "music_assistant.mass", f"chatter {i}")
    _write_log(log_root, body)

    result = SafeLogTail().tail(lines=2, level="ERROR")
    assert [ln.message for ln in result.lines] == ["err one", "err two"]


def test_tail_groups_traceback_into_error_record(log_root: Path) -> None:
    """Continuation lines (tracebacks) attach to the preceding record."""
    body = _line(1000, "INFO", "music_assistant.mass", "fine")
    body += _line(2000, "ERROR", "music_assistant.streams", "Unhandled exception")
    body += "Traceback (most recent call last):\n"
    body += '  File "stream.py", line 10, in pump\n'
    body += "ValueError: bad frame\n"
    body += _line(3000, "INFO", "music_assistant.mass", "recovered")
    _write_log(log_root, body)

    result = SafeLogTail().tail(lines=10, level="ERROR")
    assert len(result.lines) == 1
    record = result.lines[0]
    assert record.level == "ERROR"
    assert "Unhandled exception" in record.message
    assert "Traceback (most recent call last):" in record.message
    assert "ValueError: bad frame" in record.message


def test_tail_level_threshold_case_insensitive(log_root: Path) -> None:
    """Level is a case-insensitive minimum-severity threshold."""
    body = _line(1000, "DEBUG", "c.a", "dbg")
    body += _line(2000, "INFO", "c.b", "inf")
    body += _line(3000, "WARNING", "c.c", "warn")
    body += _line(4000, "ERROR", "c.d", "err")
    body += _line(5000, "CRITICAL", "c.e", "crit")
    _write_log(log_root, body)

    result = SafeLogTail().tail(lines=10, level="warning")
    assert [ln.level for ln in result.lines] == ["WARNING", "ERROR", "CRITICAL"]


def test_tail_invalid_level_raises(log_root: Path) -> None:
    """An unknown level name raises ToolError naming valid levels."""
    _write_log(log_root, _line(1000, "INFO", "c.a", "x"))
    with pytest.raises(ToolError, match=r"(?i)level"):
        SafeLogTail().tail(lines=1, level="LOUD")


def test_tail_search_matches_message_and_traceback(log_root: Path) -> None:
    """Search greps the full record text, including continuation lines."""
    body = _line(1000, "INFO", "c.a", "playback started")
    body += _line(2000, "ERROR", "c.b", "boom")
    body += "ValueError: playback pipeline broke\n"
    body += _line(3000, "INFO", "c.c", "unrelated")
    _write_log(log_root, body)

    result = SafeLogTail().tail(lines=10, search="PLAYBACK")
    messages = [ln.message for ln in result.lines]
    assert len(messages) == 2
    assert any("playback started" in m for m in messages)
    assert any("pipeline broke" in m for m in messages)


def test_tail_invalid_search_regex_raises(log_root: Path) -> None:
    """A malformed search regex raises ToolError naming the parameter."""
    _write_log(log_root, _line(1000, "INFO", "c.a", "x"))
    with pytest.raises(ToolError, match="search"):
        SafeLogTail().tail(lines=1, search="[unclosed")


def test_tail_has_more_and_next_call_hint(log_root: Path) -> None:
    """When matches remain beyond the page, has_more is set and the hint pages by timestamp."""
    body = ""
    for i in range(10):
        body += _line(1000 * (i + 1), "ERROR", "c.a", f"err {i}")
    _write_log(log_root, body)

    result = SafeLogTail().tail(lines=3, level="ERROR")
    assert [ln.message for ln in result.lines] == ["err 7", "err 8", "err 9"]
    assert result.has_more is True
    assert result.next_call_hint is not None
    assert "before=" in result.next_call_hint


def test_tail_no_hint_when_page_complete(log_root: Path) -> None:
    """A complete page reports has_more=False and no hint."""
    _write_log(log_root, _line(1000, "ERROR", "c.a", "only"))
    result = SafeLogTail().tail(lines=5, level="ERROR")
    assert result.has_more is False
    assert result.response_truncated is False
    assert result.next_call_hint is None


def test_tail_response_budget_truncates_with_hint(log_root: Path) -> None:
    """The response byte budget cuts the page short with an explicit flag + hint."""
    big = "x" * 8000
    body = ""
    for i in range(20):
        body += _line(1000 * (i + 1), "INFO", "c.a", f"{i} {big}")
    _write_log(log_root, body)

    result = SafeLogTail().tail(lines=20)
    assert result.response_truncated is True
    assert 0 < len(result.lines) < 20
    assert result.next_call_hint is not None


def test_tail_before_cursor_pages_older_records(log_root: Path) -> None:
    """before=<ISO ts> returns only records strictly older than the cursor."""
    body = ""
    for i in range(5):
        body += _line(1000 * (i + 1), "INFO", "c.a", f"msg {i}")
    _write_log(log_root, body)

    first = SafeLogTail().tail(lines=2)
    assert [ln.message for ln in first.lines] == ["msg 3", "msg 4"]
    oldest_ts = first.lines[0].timestamp
    assert oldest_ts is not None

    second = SafeLogTail().tail(lines=2, before=oldest_ts)
    assert [ln.message for ln in second.lines] == ["msg 1", "msg 2"]


def test_stats_counts_levels_components_and_range(log_root: Path) -> None:
    """stats() aggregates per-level counts, top components and the time range."""
    body = _line(1000, "INFO", "c.alpha", "one")
    body += _line(2000, "ERROR", "c.beta", "two")
    body += "Traceback (most recent call last):\n"
    body += _line(3000, "ERROR", "c.beta", "three")
    body += _line(4000, "WARNING", "c.alpha", "four")
    _write_log(log_root, body)

    stats = SafeLogTail().stats()
    assert stats.total_records == 4
    assert stats.level_counts["ERROR"] == 2
    assert stats.level_counts["INFO"] == 1
    assert stats.level_counts["WARNING"] == 1
    top = {c.component: c.count for c in stats.top_components}
    assert top == {"c.alpha": 2, "c.beta": 2}
    assert stats.first_timestamp is not None
    assert stats.last_timestamp is not None
    assert stats.first_timestamp < stats.last_timestamp


def test_count_errors_includes_critical(log_root: Path) -> None:
    """count_errors_last_5min counts ERROR and above within the window."""
    from datetime import datetime  # noqa: PLC0415

    # Log timestamps carry no TZ and are parsed as local time — write local.
    recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S,000")  # noqa: DTZ005
    body = "2020-01-01 00:00:00,000 ERROR c.c: ancient err\n"
    body += f"{recent} ERROR c.a: recent err\n"
    body += f"{recent} CRITICAL c.b: recent crit\n"
    _write_log(log_root, body)

    assert SafeLogTail().count_errors_last_5min() == 2


async def test_e2e_debug_log_stats(mounted_debug: Any, tmp_log_dir: Path) -> None:  # noqa: ARG001 -- fixture activates SafeLogTail.ROOT patch via monkeypatch
    """debug_log_stats tool aggregates the sample log via MCP."""
    async with Client(mounted_debug) as client:
        result = await client.call_tool("debug_log_stats", {})
    assert result.data.total_records > 0
    assert result.data.level_counts["ERROR"] >= 1
    assert len(result.data.top_components) > 0


def test_tail_offset_cursor_survives_same_timestamp_burst(log_root: Path) -> None:
    """
    Paging via the hint's offset cursor never loses same-timestamp records.

    A timestamp-only cursor drops tied records (ms resolution + error bursts);
    the hint must page by file offset so every record is reachable.
    """
    body = "".join(_line(1000, "ERROR", "c.a", f"burst {i}") for i in range(5))
    _write_log(log_root, body)

    page1 = SafeLogTail().tail(lines=2, level="ERROR")
    assert [ln.message for ln in page1.lines] == ["burst 3", "burst 4"]
    assert page1.has_more is True
    assert page1.next_call_hint is not None
    match = re.search(r"before='(offset:\d+)'", page1.next_call_hint)
    assert match, f"hint lacks offset cursor: {page1.next_call_hint!r}"

    page2 = SafeLogTail().tail(lines=2, level="ERROR", before=match.group(1))
    assert [ln.message for ln in page2.lines] == ["burst 1", "burst 2"]
    match2 = re.search(r"before='(offset:\d+)'", page2.next_call_hint or "")
    assert match2

    page3 = SafeLogTail().tail(lines=2, level="ERROR", before=match2.group(1))
    assert [ln.message for ln in page3.lines] == ["burst 0"]
    assert page3.has_more is False


def test_tail_oversized_record_truncated_by_bytes(log_root: Path) -> None:
    """
    A single oversized record is cut against the byte budget, not characters.

    Multi-byte UTF-8 (Cyrillic/CJK) must not blow the response budget when the
    character count is under it but the encoded size is several times larger.
    """
    big = "я" * 100_000  # 200k bytes in UTF-8
    _write_log(log_root, _line(1000, "ERROR", "c.a", big))

    result = SafeLogTail().tail(lines=5)
    assert result.response_truncated is True
    assert len(result.lines) == 1
    encoded = result.lines[0].message.encode("utf-8", errors="replace")
    assert len(encoded) <= _MAX_RESPONSE_BYTES + 100  # small slack for the marker
    assert "…[message truncated]" in result.lines[0].message
