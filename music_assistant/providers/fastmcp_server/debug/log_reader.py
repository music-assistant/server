"""
Stateless tail reader for ``mass.storage_path / musicassistant.log``.

The reader is constrained by four load-bearing invariants:

* **Path allowlist.** Only the canonical log file and its rotated
  siblings (``.log.1`` … ``.log.5``) are openable. Inputs containing
  ``..``, absolute paths, NUL bytes, or symlinks pointing outside the
  root raise :class:`fastmcp.exceptions.ToolError`.
* **Scan byte cap.** No call reads more than 10 MB from end-of-file even
  if the caller asks for thousands of records. The cap protects the
  provider process from a self-DoS, not a privacy boundary.
* **Response byte budget.** Independently of the scan cap, the combined
  size of returned messages is bounded so a single tool result cannot
  blow past the MCP client's token limits; the result flags the cut and
  carries a ready-to-use follow-up hint.
* **Token redactor.** Common bearer-token / query-string / form-field
  secret patterns in the line text are replaced with ``<redacted>``
  before the line is returned. Best-effort; not a substitute for
  scrubbing in MA's own logger.

Lines that do not start a new log record (tracebacks, wrapped output)
are attached to the preceding record's message, and all filters count
*records*, applied while scanning backwards — ``lines=N`` means "the N
most recent matching records", not "matches among the last N raw lines".
"""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from ..models import ComponentCount, LogLine, LogStatsResult, LogTailResult

if TYPE_CHECKING:
    from collections.abc import Iterator

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

# Severity ranks for threshold filtering; aliases map to their canonical rank.
_LEVEL_ORDER = {
    "VERBOSE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
    "FATAL": 50,
}

_MAX_SCAN_BYTES = 10 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_BLOCK_SIZE = 8 * 1024
_TOP_COMPONENTS = 10


@dataclass
class _ScanState:
    """Mutable counters shared between the line iterator and its caller."""

    bytes_scanned: int = 0
    truncated: bool = False
    reached_start: bool = False


@dataclass
class _Record:
    """One log record: a parsed header line plus its continuation lines."""

    entry: LogLine
    offset: int
    approx_bytes: int = field(init=False)

    def __post_init__(self) -> None:
        self.approx_bytes = len(self.entry.message.encode("utf-8", errors="replace"))


class SafeLogTail:
    """Tail the MA log with path allowlist, scan/response caps and a redactor."""

    # Fallback root used only when no ``mass`` instance is provided. The class-level
    # attribute remains so existing unit tests that monkeypatch ``SafeLogTail.ROOT``
    # against a ``tmp_path`` keep working without plumbing the fixture through a mass
    # mock. Production callers pass ``mass`` and the tailer reads ``mass.storage_path``,
    # which is the directory MA itself writes ``musicassistant.log`` into
    # (see MA ``mass.py``: ``logfile = os.path.join(self.storage_path, ...)``).
    ROOT: Path = Path.home() / ".musicassistant"
    ALLOWED = frozenset(["musicassistant.log"] + [f"musicassistant.log.{i}" for i in range(1, 6)])

    def __init__(self, mass: MusicAssistant | None = None) -> None:
        """
        Build a tail reader optionally bound to a live MA instance.

        :param mass: When provided, the log root is taken from
            ``mass.storage_path`` — honouring custom ``--data-dir`` deployments
            (Docker, HA add-on, etc.). When ``None``, falls back to the class
            attribute ``ROOT`` (default ``$HOME/.musicassistant``) so unit
            tests that monkeypatch ``SafeLogTail.ROOT`` keep working.
        """
        self._mass = mass

    def tail(
        self,
        *,
        lines: int = 200,
        level: str | None = None,
        component_regex: str | None = None,
        search: str | None = None,
        since_seconds: int | None = None,
        before: str | None = None,
        name: str = "musicassistant.log",
    ) -> LogTailResult:
        """
        Return the last ``lines`` matching records from the named log file.

        :param lines: Maximum number of matching records to return (clamped to 2000).
        :param level: Minimum severity threshold, case-insensitive (e.g. "warning").
        :param component_regex: Filter by component name regex.
        :param search: Case-insensitive regex over the full record text.
        :param since_seconds: Filter to records from the last N seconds.
        :param before: Paging cursor — an ISO timestamp, or the exact
            ``offset:<n>`` value from a previous result's ``next_call_hint``
            (offset paging is lossless for records sharing one timestamp).
        :param name: Log file name (must be in ALLOWED set).
        """
        lines = max(1, min(int(lines), 2000))
        path = self._safe_path(name)
        if not path.exists():
            raise ToolError(f"log file {name!r} not found")

        min_level = self._parse_level(level)
        component_filter = self._compile(component_regex, "component_regex", 0)
        search_filter = self._compile(search, "search", re.IGNORECASE)
        before_cutoff, before_offset = self._parse_before(before)
        from music_assistant.helpers.datetime import now  # noqa: PLC0415

        current_time = now()

        state = _ScanState()
        collected: list[LogLine] = []
        oldest_offset: int | None = None
        budget = _MAX_RESPONSE_BYTES
        has_more = False
        response_truncated = False

        for record in self._iter_records_backwards(path, state):
            entry = record.entry
            when = self._entry_datetime(entry)
            if before_offset is not None and record.offset >= before_offset:
                continue
            if before_cutoff is not None and (when is None or when >= before_cutoff):
                continue
            # Records are scanned newest-first; once one falls out of the
            # since window every older record does too (logs are monotonic).
            if (
                since_seconds is not None
                and when is not None
                and (current_time - when).total_seconds() > since_seconds
            ):
                break
            if min_level is not None:
                rank = _LEVEL_ORDER.get(entry.level or "")
                if rank is None or rank < min_level:
                    continue
            if component_filter and not (
                entry.component and component_filter.search(entry.component)
            ):
                continue
            if search_filter and not search_filter.search(entry.message):
                continue

            if len(collected) >= lines:
                has_more = True
                break
            if collected and record.approx_bytes > budget:
                has_more = True
                response_truncated = True
                break
            if not collected and record.approx_bytes > budget:
                # A single oversized record (giant traceback) is cut, not
                # dropped — sliced on encoded bytes so multi-byte UTF-8
                # content cannot blow past the response budget.
                cut = entry.message.encode("utf-8", errors="replace")[:budget]
                entry = LogLine(
                    timestamp=entry.timestamp,
                    level=entry.level,
                    component=entry.component,
                    message=cut.decode("utf-8", errors="ignore") + "\n…[message truncated]",
                )
                response_truncated = True
            budget -= record.approx_bytes
            collected.append(entry)
            oldest_offset = record.offset

        collected.reverse()
        hint = self._build_hint(
            oldest_offset,
            name=name,
            has_more=has_more,
            response_truncated=response_truncated,
            state=state,
        )
        return LogTailResult(
            log_path=str(path),
            lines=collected,
            bytes_scanned=state.bytes_scanned,
            truncated=state.truncated,
            has_more=has_more,
            response_truncated=response_truncated,
            next_call_hint=hint,
        )

    def stats(
        self,
        *,
        since_seconds: int | None = None,
        name: str = "musicassistant.log",
    ) -> LogStatsResult:
        """
        Aggregate the log window: per-level counts, top components, time range.

        :param since_seconds: Restrict to records from the last N seconds.
        :param name: Log file name (must be in ALLOWED set).
        """
        path = self._safe_path(name)
        if not path.exists():
            raise ToolError(f"log file {name!r} not found")
        from music_assistant.helpers.datetime import now  # noqa: PLC0415

        current_time = now()

        state = _ScanState()
        level_counts: Counter[str] = Counter()
        component_counts: Counter[str] = Counter()
        total = 0
        newest: str | None = None
        oldest: str | None = None

        for record in self._iter_records_backwards(path, state):
            entry = record.entry
            when = self._entry_datetime(entry)
            if (
                since_seconds is not None
                and when is not None
                and (current_time - when).total_seconds() > since_seconds
            ):
                break
            total += 1
            level_counts[entry.level or "UNPARSED"] += 1
            if entry.component:
                component_counts[entry.component] += 1
            if newest is None:
                newest = entry.timestamp
            if entry.timestamp is not None:
                oldest = entry.timestamp

        top = [
            ComponentCount(component=component, count=count)
            for component, count in component_counts.most_common(_TOP_COMPONENTS)
        ]
        return LogStatsResult(
            log_path=str(path),
            window_seconds=since_seconds,
            total_records=total,
            level_counts=dict(level_counts),
            top_components=top,
            first_timestamp=oldest,
            last_timestamp=newest,
            bytes_scanned=state.bytes_scanned,
            truncated=state.truncated,
        )

    def count_errors_last_5min(self, *, name: str = "musicassistant.log") -> int:
        """
        Count ERROR-and-above entries in the last 5 minutes (used by health_summary).

        :param name: Log file name (must be in ALLOWED set).
        """
        stats = self.stats(since_seconds=300, name=name)
        return sum(
            count
            for level, count in stats.level_counts.items()
            if _LEVEL_ORDER.get(level, 0) >= _LEVEL_ORDER["ERROR"]
        )

    # --- internal helpers ---

    @property
    def _root(self) -> Path:
        """Effective log root — prefer ``mass.storage_path`` when bound."""
        if self._mass is not None:
            return Path(self._mass.storage_path)
        return self.ROOT

    def _safe_path(self, name: str) -> Path:
        """
        Validate and resolve a log file path.

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

    @staticmethod
    def _parse_level(level: str | None) -> int | None:
        """Map a level name to its severity rank, or raise a ToolError."""
        if level is None:
            return None
        rank = _LEVEL_ORDER.get(level.strip().upper())
        if rank is None:
            valid = "VERBOSE, DEBUG, INFO, WARNING, ERROR, CRITICAL"
            raise ToolError(f"unknown level {level!r}; valid levels: {valid}")
        return rank

    @staticmethod
    def _compile(pattern: str | None, param: str, flags: int) -> re.Pattern[str] | None:
        """Compile an optional user-supplied regex, naming the parameter on failure."""
        if pattern is None:
            return None
        try:
            return re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(f"invalid regex in {param!r}: {exc}") from exc

    @staticmethod
    def _parse_before(before: str | None) -> tuple[datetime | None, int | None]:
        """Parse the ``before`` cursor: an ISO timestamp or an ``offset:<n>`` marker."""
        if before is None:
            return None, None
        if before.startswith("offset:"):
            try:
                offset = int(before.removeprefix("offset:"))
            except ValueError as exc:
                raise ToolError(f"invalid offset cursor in 'before': {before!r}") from exc
            if offset < 0:
                raise ToolError(f"invalid offset cursor in 'before': {before!r}")
            return None, offset
        try:
            cutoff = datetime.fromisoformat(before)
        except ValueError as exc:
            raise ToolError(
                f"invalid 'before' cursor {before!r}: expected an ISO timestamp "
                "or an 'offset:<n>' value from next_call_hint"
            ) from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.astimezone()
        return cutoff, None

    @staticmethod
    def _entry_datetime(entry: LogLine) -> datetime | None:
        """Return the record's timestamp as an aware datetime, if parseable."""
        if entry.timestamp is None:
            return None
        try:
            return datetime.fromisoformat(entry.timestamp)
        except ValueError:
            return None

    def _next_rotation(self, name: str) -> str | None:
        """Name of the next-older rotated log, when it exists and is allowed."""
        if name == "musicassistant.log":
            candidate = "musicassistant.log.1"
        else:
            try:
                candidate = f"musicassistant.log.{int(name.rsplit('.', 1)[1]) + 1}"
            except ValueError:
                return None
        if candidate in self.ALLOWED and (self._root / candidate).is_file():
            return candidate
        return None

    def _build_hint(
        self,
        oldest_offset: int | None,
        *,
        name: str,
        has_more: bool,
        response_truncated: bool,
        state: _ScanState,
    ) -> str | None:
        """Compose the ready-to-use follow-up instruction for an incomplete page."""
        # Page by file offset, not timestamp — offsets are lossless when many
        # records share one millisecond timestamp (error bursts).
        page = (
            f"before='offset:{oldest_offset}'"
            if oldest_offset is not None
            else "a narrower since_seconds="
        )
        if response_truncated:
            return (
                "Response size budget reached; narrow with level=/search=/"
                f"component_regex=/since_seconds=, or page older records with {page}."
            )
        if has_more:
            return f"More matching records exist; fetch the next page with {page} (same filters)."
        if state.truncated:
            return (
                f"Scan stopped at the 10 MB cap before the page filled; page deeper with {page} "
                "or narrow the filters."
            )
        next_rotation = self._next_rotation(name)
        if state.reached_start and next_rotation is not None:
            return (
                f"Reached the start of {name!r}; older entries continue in name={next_rotation!r}."
            )
        return None

    def _iter_lines_backwards(self, path: Path, state: _ScanState) -> Iterator[tuple[int, str]]:
        """Yield ``(byte_offset, line)`` pairs newest-first, up to the 10 MB scan cap."""
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            position = fh.tell()
            buffer = b""
            while position > 0 and state.bytes_scanned < _MAX_SCAN_BYTES:
                read_size = min(_BLOCK_SIZE, position, _MAX_SCAN_BYTES - state.bytes_scanned)
                position -= read_size
                fh.seek(position)
                buffer = fh.read(read_size) + buffer
                state.bytes_scanned += read_size
                parts = buffer.split(b"\n")
                offsets = []
                acc = position
                for part in parts:
                    offsets.append(acc)
                    acc += len(part) + 1
                # parts[0] may be a partial line continuing into the unread
                # region — keep it buffered until more data arrives.
                buffer = parts[0]
                for idx in range(len(parts) - 1, 0, -1):
                    if parts[idx]:
                        yield offsets[idx], parts[idx].decode("utf-8", errors="replace")
            if position == 0:
                state.reached_start = True
                if buffer:
                    yield 0, buffer.decode("utf-8", errors="replace")
            elif state.bytes_scanned >= _MAX_SCAN_BYTES:
                # The remaining buffer is a partial fragment of an unread line — drop it.
                state.truncated = True

    def _iter_records_backwards(self, path: Path, state: _ScanState) -> Iterator[_Record]:
        """Group lines into records (header + continuations), newest-first."""
        pending: list[str] = []
        for offset, raw in self._iter_lines_backwards(path, state):
            match = _LOG_LINE_RE.match(raw)
            if match is None:
                pending.append(raw)
                continue
            yield self._build_record(match, list(reversed(pending)), offset)
            pending.clear()
        if pending and not state.truncated:
            # Headerless lines at the very start of the file — surface them
            # as message-only records rather than dropping them silently.
            for raw in pending:
                yield _Record(
                    entry=LogLine(
                        timestamp=None, level=None, component=None, message=self._redact(raw)
                    ),
                    offset=0,
                )

    def _build_record(self, match: re.Match[str], continuation: list[str], offset: int) -> _Record:
        """Assemble a parsed record from a header match and its continuation lines."""
        ts_raw = match["ts"].replace(",", ".").replace(" ", "T")
        try:
            # Stamp as local time — MA's logger has no TZ info in the line.
            parsed_ts = datetime.fromisoformat(ts_raw).astimezone().isoformat()
        except ValueError:
            parsed_ts = None
        component = match.group("component_brk") or match.group("component_col")
        raw_message = "\n".join([match["msg"], *continuation])
        message = self._redact(raw_message)
        return _Record(
            entry=LogLine(
                timestamp=parsed_ts,
                level=match["level"],
                component=component,
                message=message,
            ),
            offset=offset,
        )

    @staticmethod
    def _redact(text: str) -> str:
        """
        Redact common secret patterns (bearer tokens, credentials, passwords).

        :param text: The input string.
        :return: The text with secrets replaced by <redacted>.
        """
        for pattern, repl in _REDACTORS:
            text = pattern.sub(repl, text)
        return text
