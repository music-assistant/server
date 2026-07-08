"""
Always-on diagnostics capture and sanitization helpers.

This module is intentionally stdlib-only and free of any Music Assistant imports so the
capture handler can be installed at the earliest possible moment (before the server core
is even constructed) and adopted later by the diagnostics controller. The always-on cost
is limited to one logging handler doing bounded, in-memory ring buffer appends;
sanitization and report building only happen when a report is explicitly requested.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import time
import traceback
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import NamedTuple

# bounds for the captured data (everything in memory, so keep it small)
LOG_RING_MAXLEN = 300
MAX_EXCEPTION_FINGERPRINTS = 100
MAX_TRACEBACK_CHARS = 6000
MAX_MESSAGE_CHARS = 500
TRACEBACK_FRAME_LIMIT = 50

REDACTION_NOTICE = (
    "This report is automatically redacted: home directories, media file paths/names, "
    "URL credentials and query strings, passwords/tokens/secrets, e-mail addresses, "
    "non-local IP addresses and MAC addresses are replaced by placeholders. Absolute "
    "code paths are shortened to be relative to the application root. Media paths and "
    "MAC addresses are reduced to a stable hash so identical values can still be "
    "correlated within the report."
)

# file extensions that reveal (music) library content when they appear in a path
_MEDIA_FILE_EXTENSIONS = (
    "aac|aif|aiff|alac|ape|asx|avi|cue|dff|dsf|flac|gif|jpeg|jpg|m3u|m3u8|m4a|m4b|m4v|"
    "mka|mkv|mov|mp3|mp4|nfo|oga|ogg|opus|pls|png|wav|webm|webp|wma|wv|xspf"
)

# absolute code paths -> relative to site-packages / music_assistant root
_RE_SITE_PACKAGES_PATH = re.compile(r"(?:[A-Za-z]:)?[^\s\"']*[/\\]site-packages[/\\]")
_RE_MASS_ROOT_PATH = re.compile(r"(?:[A-Za-z]:)?[^\s\"']*[/\\]music_assistant[/\\]")
# URL userinfo (user:pass@host) and query strings (tokens/signatures live there)
_RE_URL_USERINFO = re.compile(r"(\w+://)[^/\s@\"']+@")
_RE_URL_QUERY = re.compile(r"(\w+://[^\s\"'<>]*?)\?[^\s\"'<>]*")
# query strings on path-style (relative) request URLs, e.g. OAuth callbacks
_RE_PATH_QUERY = re.compile(r"(?<!\w)(/[^\s\"'<>?]*)\?[^\s\"'<>]+")
# secrets: JWT's, Authorization header schemes, key=value assignments, long token blobs
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")
_RE_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|digest)\s+[A-Za-z0-9\-._~+/=]{16,}")
_RE_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|auth|"
    r"authorization|credentials?|access[_-]?key|private[_-]?key|cookie|csrf))\b"
    r"(\"?'?\s*[=:]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',;&]+)"
)
# long token blobs: 32+ chars of token-ish alphabet containing at least one digit
# (the digit requirement spares long snake_case identifiers and module paths)
_RE_LONG_TOKEN = re.compile(
    r"(?<![A-Za-z0-9+_-])(?=[A-Za-z0-9+_-]*\d)[A-Za-z0-9+_-]{32,}(?![A-Za-z0-9+_-])"
)
# media library paths: full paths (at least one path separator) and bare filenames;
# bare filenames may contain spaces, so unquoted/undelimited prose directly in front
# of a media filename is redacted along with it (privacy beats message fidelity here)
_RE_MEDIA_PATH = re.compile(
    rf"(?<!\w)(?:[A-Za-z]:)?(?:[/\\][^/\\\r\n]*?)+\.(?:{_MEDIA_FILE_EXTENSIONS})\b",
    re.IGNORECASE,
)
_RE_MEDIA_FILENAME = re.compile(
    rf"(?<![\w/\\.])[^\s/\\:*?\"'<>|(][^/\\:*?\"'<>|\r\n]*\.(?:{_MEDIA_FILE_EXTENSIONS})\b",
    re.IGNORECASE,
)
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# MAC addresses (also common as/inside player ids), colon or dash separated
_RE_MAC_ADDRESS = re.compile(
    r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}(?:([:-])[0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f:-])"
)
# IPv6 first (so IPv4-mapped addresses are redacted as a whole), candidates are
# verified with the ipaddress module to avoid eating timestamps and MAC addresses
_RE_IPV6_CANDIDATE = re.compile(
    r"(?<![\w.:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])"
    r"|(?<![\w.:])[0-9A-Fa-f:]*::[0-9A-Fa-f:.]*(?![\w:.])"
)
_RE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_RE_HOME_DIR = re.compile(r"(?:/(?:Users|home)/|[A-Za-z]:\\Users\\)[^/\\\s:;,'\"]+")


def sanitize_text(text: str) -> str:
    """
    Redact privacy-sensitive data from a text snippet.

    Applied to every string that ends up in a diagnostics report (log messages,
    tracebacks, error strings): redacts home directories, media file paths, URL
    credentials/query strings, secrets/tokens, e-mail addresses, IP addresses and
    MAC addresses, and shortens absolute code paths to be relative to the
    application root.

    :param text: The raw text to sanitize.
    """
    text = _RE_SITE_PACKAGES_PATH.sub("", text)
    text = _RE_MASS_ROOT_PATH.sub("music_assistant/", text)
    text = _RE_URL_QUERY.sub(r"\1?<redacted-query>", text)
    text = _RE_URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _RE_PATH_QUERY.sub(r"\1?<redacted-query>", text)
    text = _RE_JWT.sub("<redacted-token>", text)
    text = _RE_AUTH_SCHEME.sub(r"\1 <redacted>", text)
    text = _RE_SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    text = _RE_LONG_TOKEN.sub("<redacted-token>", text)
    text = _RE_MEDIA_PATH.sub(_redact_media_path, text)
    text = _RE_MEDIA_FILENAME.sub(_redact_media_path, text)
    text = _RE_EMAIL.sub("<redacted-email>", text)
    text = _RE_MAC_ADDRESS.sub(_redact_mac, text)
    text = _RE_IPV6_CANDIDATE.sub(_redact_ip, text)
    text = _RE_IPV4.sub(_redact_ip, text)
    return _RE_HOME_DIR.sub("~", text)


def sanitize_data[DataT](data: DataT) -> DataT:
    """
    Recursively sanitize all strings (including dict keys) inside a data structure.

    :param data: Plain data (dicts/lists/tuples/scalars) to sanitize in depth.
    """
    if isinstance(data, str):
        return sanitize_text(data)  # type: ignore[return-value]
    if isinstance(data, dict):
        return {sanitize_data(key): sanitize_data(value) for key, value in data.items()}  # type: ignore[return-value]
    if isinstance(data, tuple):
        return tuple(sanitize_data(value) for value in data)  # type: ignore[return-value]
    if isinstance(data, list):
        return [sanitize_data(value) for value in data]  # type: ignore[return-value]
    return data


class CapturedLogRecord(NamedTuple):
    """A single captured (WARNING or higher) log record."""

    created: float
    level: str
    logger_name: str
    message: str


@dataclass
class CapturedException:
    """Aggregated info for one unique exception fingerprint."""

    fingerprint: str
    exc_type: str
    origin: str
    logger_name: str
    level: str
    message: str
    traceback_exc: traceback.TracebackException
    first_seen: float
    last_seen: float
    count: int = 1

    def render_traceback(self) -> str:
        """Render the representative traceback as text (may read source files)."""
        try:
            return "".join(self.traceback_exc.format())[-MAX_TRACEBACK_CHARS:]
        except Exception as err:
            return f"<traceback rendering failed: {err!r}>"


class DiagnosticsLogHandler(logging.Handler):
    """
    Root-logger handler that captures warnings/errors for the diagnostics report.

    Keeps a bounded ring of recent WARNING+ records plus exception aggregates keyed by
    fingerprint (exception type + raise site + topmost music_assistant frame). All data
    stays in memory as-is; sanitization happens only when a report is built.
    """

    def __init__(self) -> None:
        """Initialize the capture handler."""
        super().__init__(level=logging.WARNING)
        self.installed_at = time.monotonic()
        self.log_ring: deque[CapturedLogRecord] = deque(maxlen=LOG_RING_MAXLEN)
        self.exceptions: OrderedDict[str, CapturedException] = OrderedDict()

    def emit(self, record: logging.LogRecord) -> None:
        """Capture a single log record (may never raise or block)."""
        try:
            message = record.getMessage()[:MAX_MESSAGE_CHARS]
            self.log_ring.append(
                CapturedLogRecord(record.created, record.levelname, record.name, message)
            )
            if record.exc_info and record.exc_info[0] is not None:
                self._capture_exception(record, message)
        except Exception:  # noqa: S110 - capturing diagnostics may never break logging itself
            pass

    def snapshot(self) -> tuple[list[CapturedLogRecord], list[CapturedException]]:
        """Return a point-in-time copy of the captured log ring and exception aggregates."""
        self.acquire()
        try:
            return list(self.log_ring), [replace(entry) for entry in self.exceptions.values()]
        finally:
            self.release()

    def clear(self) -> None:
        """Clear all captured data (intended for tests)."""
        self.acquire()
        try:
            self.log_ring.clear()
            self.exceptions.clear()
        finally:
            self.release()

    def _capture_exception(self, record: logging.LogRecord, message: str) -> None:
        """Aggregate an exception attached to a log record by fingerprint."""
        assert record.exc_info is not None  # guarded by caller
        exc_type, exc_value, exc_tb = record.exc_info
        if exc_type is None or exc_value is None:
            return
        # walk the raw traceback frames without touching linecache/source files,
        # keeping this always-on path cheap
        raise_site = ""
        raise_site_key = ""
        origin = ""
        origin_key = ""
        frame_tb = exc_tb
        while frame_tb is not None:
            code = frame_tb.tb_frame.f_code
            raise_site = f"{code.co_filename}:{frame_tb.tb_lineno} in {code.co_name}"
            raise_site_key = f"{code.co_filename}:{code.co_name}"
            if "music_assistant" in code.co_filename:
                origin = raise_site
                origin_key = raise_site_key
            frame_tb = frame_tb.tb_next
        exc_type_name = exc_type.__qualname__ if exc_type else "UnknownError"
        # fingerprint on function granularity (not line numbers) so retries/loops aggregate
        fingerprint_source = f"{exc_type_name}|{raise_site_key}|{origin_key}"
        fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()[:12]
        if entry := self.exceptions.get(fingerprint):
            entry.count += 1
            entry.last_seen = record.created
            self.exceptions.move_to_end(fingerprint)
            return
        # capture structured traceback metadata once per unique fingerprint;
        # lookup_lines=False defers all source file (linecache) reads to report time
        traceback_exc = traceback.TracebackException(
            exc_type,
            exc_value,
            exc_tb,
            limit=TRACEBACK_FRAME_LIMIT,
            lookup_lines=False,
            compact=True,
        )
        self.exceptions[fingerprint] = CapturedException(
            fingerprint=fingerprint,
            exc_type=exc_type_name,
            origin=origin or raise_site,
            logger_name=record.name,
            level=record.levelname,
            message=message,
            traceback_exc=traceback_exc,
            first_seen=record.created,
            last_seen=record.created,
        )
        while len(self.exceptions) > MAX_EXCEPTION_FINGERPRINTS:
            self.exceptions.popitem(last=False)


def install_diagnostics_log_handler() -> DiagnosticsLogHandler:
    """
    Install (or return the already installed) diagnostics capture handler.

    Idempotent: attaches a single handler instance to the root logger so warnings,
    errors and exceptions are captured from the earliest moment possible, independent
    of controller setup order (also covers embedded usage where __main__ is not used).
    """
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, DiagnosticsLogHandler):
            return handler
    handler = DiagnosticsLogHandler()
    root_logger.addHandler(handler)
    return handler


def _redact_media_path(match: re.Match[str]) -> str:
    """Replace a matched media file path with a stable hash placeholder plus extension."""
    path = match.group(0)
    extension = path.rsplit(".", 1)[-1].lower()
    digest = hashlib.sha256(path.encode()).hexdigest()[:8]
    return f"<path-{digest}>.{extension}"


def _redact_mac(match: re.Match[str]) -> str:
    """Replace a matched MAC address with a stable hash placeholder."""
    digest = hashlib.sha256(match.group(0).lower().encode()).hexdigest()[:8]
    return f"<mac-{digest}>"


def _redact_ip(match: re.Match[str]) -> str:
    """Replace a matched IP address candidate, keeping localhost and invalid candidates."""
    candidate = match.group(0)
    try:
        ip_address = ipaddress.ip_address(candidate)
    except ValueError:
        # not an actual IP address (e.g. a timestamp or MAC address lookalike)
        return candidate
    if ip_address.is_loopback or ip_address.is_unspecified:
        return candidate
    return "<redacted-ip>"
