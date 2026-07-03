"""Unit tests for SafeLogTail (path allowlist, byte cap, redactor)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from music_assistant.providers.fastmcp_server.debug.log_reader import SafeLogTail
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
    """Pin parser support for both MA-runtime and Python-default log shapes.

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
    """When constructed with ``mass``, SafeLogTail reads from ``mass.storage_path``.

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
    """SafeLogTail marks result truncated when 10MB cap is reached."""
    from music_assistant.providers.fastmcp_server.debug import log_reader  # noqa: PLC0415

    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    huge = tmp_path / "musicassistant.log"
    with huge.open("wb") as fh:
        # 20 MB of repeated, parseable lines.
        line = b"2026-05-28 09:00:00,001 INFO music_assistant.mass: filler\n"
        while fh.tell() < 20 * 1024 * 1024:
            fh.write(line)

    tail = log_reader.SafeLogTail()
    result = tail.tail(lines=10_000)
    assert result.truncated is True
    assert result.bytes_scanned <= 10 * 1024 * 1024 + len(line)


def test_scan_bytes_cap_drops_partial_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the byte cap fires mid-line, the partial leading fragment must be dropped.

    Without the fix, `_read_last_lines` would return a half-parsed entry with
    no timestamp / level / component (just a tail substring of a real line) —
    which slips past since_seconds filtering and confuses callers.
    """
    from music_assistant.providers.fastmcp_server.debug import log_reader  # noqa: PLC0415

    monkeypatch.setattr(log_reader.SafeLogTail, "ROOT", tmp_path, raising=True)
    huge = tmp_path / "musicassistant.log"
    with huge.open("wb") as fh:
        line = b"2026-05-28 09:00:00,001 INFO music_assistant.mass: filler\n"
        # 11 MB of identical, parseable lines — enough to trigger the 10 MB cap.
        while fh.tell() < 11 * 1024 * 1024:
            fh.write(line)

    tail = log_reader.SafeLogTail()
    result = tail.tail(lines=2000)
    assert result.truncated is True
    # Every returned line must be fully parsed — no None timestamps.
    assert all(entry.timestamp is not None for entry in result.lines), (
        "partial leading fragment leaked: "
        + str([e for e in result.lines if e.timestamp is None][:3])
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
    """The blocking log read is offloaded to a worker thread, not the event loop.

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
