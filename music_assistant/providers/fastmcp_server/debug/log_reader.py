"""Stateless tail reader for ``mass.storage_path / musicassistant.log``.

The reader is constrained by three load-bearing invariants:

* **Path allowlist.** Only the canonical log file and its rotated
  siblings (``.log.1`` … ``.log.5``) are openable. Inputs containing
  ``..``, absolute paths, NUL bytes, or symlinks pointing outside the
  root raise :class:`fastmcp.exceptions.ToolError`.
* **Byte cap.** No call reads more than 10 MB from end-of-file even if
  the caller asks for thousands of lines. The cap protects the provider
  process from a self-DoS, not a privacy boundary.
* **Token redactor.** Common bearer-token / query-string / form-field
  secret patterns in the line text are replaced with ``<redacted>``
  before the line is returned. Best-effort; not a substitute for
  scrubbing in MA's own logger.
"""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from ..models import LogLine, LogTailResult

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# Accept both common formats:
#   - Music Assistant runtime: ``<ts> <LEVEL> (<thread>) [<component>] <msg>``
#   - Python logging default:  ``<ts> <LEVEL> <component>: <msg>``
# Component is surfaced from whichever group matched.
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"(?:"
    r"\([^)]+\)\s+\[(?P<component_brk>[^\]]+)\]\s+"
    r"|"
    r"(?P<component_col>[A-Za-z0-9_.]+)\s*:\s+"
    r")"
    r"(?P<msg>.*)$"
)

_REDACTORS = (
    (re.compile(r"(?i)(authorization:\s*bearer)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)\btoken=\S+"), "token=<redacted>"),
    (re.compile(r"(?i)\bpassword=\S+"), "password=<redacted>"),
    (re.compile(r"(?i)\bsecret=\S+"), "secret=<redacted>"),
)

_MAX_SCAN_BYTES = 10 * 1024 * 1024
_BLOCK_SIZE = 8 * 1024


class SafeLogTail:
    """Tail the MA log file with path allowlist + byte cap + redactor."""

    # Fallback root used only when no ``mass`` instance is provided. The class-level
    # attribute remains so existing unit tests that monkeypatch ``SafeLogTail.ROOT``
    # against a ``tmp_path`` keep working without plumbing the fixture through a mass
    # mock. Production callers pass ``mass`` and the tailer reads ``mass.storage_path``,
    # which is the directory MA itself writes ``musicassistant.log`` into
    # (see MA ``mass.py``: ``logfile = os.path.join(self.storage_path, ...)``).
    ROOT: Path = Path.home() / ".musicassistant"
    ALLOWED = frozenset(["musicassistant.log"] + [f"musicassistant.log.{i}" for i in range(1, 6)])

    def __init__(self, mass: MusicAssistant | None = None) -> None:
        """Build a tail reader optionally bound to a live MA instance.

        :param mass: When provided, the log root is taken from
            ``mass.storage_path`` — honouring custom ``--data-dir`` deployments
            (Docker, HA add-on, etc.). When ``None``, falls back to the class
            attribute ``ROOT`` (default ``$HOME/.musicassistant``) so unit
            tests that monkeypatch ``SafeLogTail.ROOT`` keep working.
        """
        self._mass = mass

    @property
    def _root(self) -> Path:
        """Effective log root — prefer ``mass.storage_path`` when bound."""
        if self._mass is not None:
            return Path(self._mass.storage_path)
        return self.ROOT

    def tail(
        self,
        *,
        lines: int = 200,
        level: str | None = None,
        component_regex: str | None = None,
        since_seconds: int | None = None,
        name: str = "musicassistant.log",
    ) -> LogTailResult:
        """Return the last ``lines`` parsed entries from the named log file.

        :param lines: Maximum number of lines to return (clamped to 2000).
        :param level: Filter by log level (e.g., "ERROR", "INFO").
        :param component_regex: Filter by component name regex.
        :param since_seconds: Filter to entries from the last N seconds.
        :param name: Log file name (must be in ALLOWED set).
        """
        lines = max(1, min(int(lines), 2000))
        path = self._safe_path(name)
        if not path.exists():
            raise ToolError(f"log file {name!r} not found")

        component_filter = re.compile(component_regex) if component_regex else None
        now = datetime.now().astimezone()

        raw_lines, bytes_scanned, truncated = self._read_last_lines(path, lines)

        out: list[LogLine] = []
        for raw in raw_lines:
            parsed = self._parse(raw)
            if level and parsed.level != level:
                continue
            if component_filter and not (
                parsed.component and component_filter.search(parsed.component)
            ):
                continue
            if since_seconds is not None and parsed.timestamp:
                try:
                    when = datetime.fromisoformat(parsed.timestamp)
                except ValueError:
                    continue
                if (now - when).total_seconds() > since_seconds:
                    continue
            out.append(parsed)

        return LogTailResult(
            log_path=str(path),
            lines=out,
            bytes_scanned=bytes_scanned,
            truncated=truncated,
        )

    def count_errors_last_5min(self, *, name: str = "musicassistant.log") -> int:
        """Count ERROR-level entries in the last 5 minutes (used by health_summary).

        :param name: Log file name (must be in ALLOWED set).
        """
        result = self.tail(lines=2000, level="ERROR", since_seconds=300, name=name)
        return len(result.lines)

    # --- internal helpers ---

    def _safe_path(self, name: str) -> Path:
        """Validate and resolve a log file path.

        :param name: The requested log file name.
        :raises ToolError: If name contains traversal characters, is not in ALLOWED, or symlink escapes.
        """
        if not name or "\x00" in name or name in {".", ".."}:
            raise ToolError(f"log file {name!r} not allowed")
        if name not in self.ALLOWED:
            raise ToolError(f"log file {name!r} not allowed")

        root_resolved = self._root.resolve()
        candidate = self._root / name
        # resolve(strict=False) follows symlinks; the is_relative_to check below
        # catches both directly-malformed paths and symlink-escape attempts in one pass.
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root_resolved):
            raise ToolError(f"log file {name!r} not allowed")
        return resolved

    def _read_last_lines(self, path: Path, lines: int) -> tuple[list[str], int, bool]:
        """Read up to ``lines`` final lines or 10 MB, whichever is smaller.

        :param path: Resolved path to log file.
        :param lines: Maximum lines to read.
        :return: Tuple of (line list, bytes scanned, truncated flag).
        """
        bytes_scanned = 0
        truncated = False
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            buffer = b""
            position = end
            # Read backwards from EOF until we hit byte cap or EOF.
            while position > 0 and bytes_scanned < _MAX_SCAN_BYTES:
                read_size = min(_BLOCK_SIZE, position, _MAX_SCAN_BYTES - bytes_scanned)
                position -= read_size
                fh.seek(position)
                chunk = fh.read(read_size)
                bytes_scanned += read_size
                buffer = chunk + buffer
            # Mark truncated if we hit byte cap and there's still data before position.
            if bytes_scanned >= _MAX_SCAN_BYTES and position > 0:
                truncated = True

        text = buffer.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        # When the byte cap fires mid-line, the buffer's first line is a partial fragment.
        # Drop it so callers never see a half-parsed entry.
        if truncated and all_lines:
            all_lines = all_lines[1:]
        return all_lines[-lines:], bytes_scanned, truncated

    def _parse(self, raw: str) -> LogLine:
        """Parse a log line into timestamp, level, component, message.

        :param raw: Raw log line text.
        :return: LogLine with parsed fields or message-only if unparsable.
        """
        match = _LOG_LINE_RE.match(raw)
        if not match:
            return LogLine(timestamp=None, level=None, component=None, message=self._redact(raw))
        ts_raw = match["ts"].replace(",", ".").replace(" ", "T")
        try:
            # Stamp as local time — MA's logger has no TZ info in the line.
            parsed_ts = datetime.fromisoformat(ts_raw).astimezone().isoformat()
        except ValueError:
            parsed_ts = None
        component = match.group("component_brk") or match.group("component_col")
        return LogLine(
            timestamp=parsed_ts,
            level=match["level"],
            component=component,
            message=self._redact(match["msg"]),
        )

    @staticmethod
    def _redact(text: str) -> str:
        """Redact common secret patterns (bearer tokens, credentials, passwords).

        :param text: The input string.
        :return: The text with secrets replaced by <redacted>.
        """
        for pattern, repl in _REDACTORS:
            text = pattern.sub(repl, text)
        return text
