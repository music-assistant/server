"""Various (server-only) tools and helpers."""

from __future__ import annotations

import asyncio
import codecs
import functools
import html
import importlib
import inspect
import logging
import os
import platform
import re
import shutil
import signal
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from ipaddress import IPv4Address, IPv6Address, ip_address
from itertools import islice
from pathlib import Path
from types import ModuleType, TracebackType
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, Protocol, Self, TypeVar, cast
from urllib.parse import urlparse

import ifaddr
from markdownify import markdownify
from music_assistant_models.enums import AlbumType, IdentifierType
from music_assistant_models.errors import UnsupportedSystemError
from zeroconf import InterfaceChoice, IPVersion

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    LIVE_INDICATORS,
    SOUNDTRACK_INDICATORS,
    VERBOSE_LOG_LEVEL,
    WILDCARD_BIND_IPS,
)
from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.player import DeviceInfo
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderModuleType


LOGGER = logging.getLogger(__name__)

CALLBACK_TYPE = Callable[[], None]


async def warn_if_missing_x86_64_v2(logger: logging.Logger) -> None:
    """
    Log a deprecation warning if the CPU lacks x86-64-v2 support.

    :param logger: Logger instance to write the warning to.
    """
    if platform.machine() not in ("x86_64", "AMD64"):
        return

    def _check() -> bool | None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except FileNotFoundError, PermissionError:
            return None

        flags: set[str] = set()
        for line in cpuinfo.splitlines():
            if line.startswith("flags"):
                flags.update(line.split())
                break

        if not flags:
            return None

        # x86-64-v2 requires: CMPXCHG16B, LAHF/SAHF, POPCNT, SSE3, SSSE3, SSE4.1, SSE4.2
        # SSE3 may appear as "pni" (Prescott New Instructions) on older kernels
        required = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"}
        has_sse3 = bool({"sse3", "pni"} & flags)
        return required.issubset(flags) and has_sse3

    if await asyncio.to_thread(_check) is False:
        logger.warning(
            "\n\n"
            "########################################################"
            "########################\n"
            "###               CPU DEPRECATION WARNING"
            "                                    ###\n"
            "########################################################"
            "########################\n"
            "\n"
            "Your CPU does not support the x86-64-v2 instruction "
            "set, which will be\n"
            "required starting with Music Assistant 2.9.\n"
            "\n"
            "If you are running in a virtual machine (e.g. Proxmox),"
            " change the CPU type\n"
            "to 'host' or select a more modern CPU type preset "
            "(e.g. x86-64-v2 or newer).\n"
            "\n"
            "If your physical CPU predates 2009, you will likely "
            "need to upgrade\n"
            "your hardware before updating Music Assistant to 2.9.\n"
            "\n"
            "########################################################"
            "########################\n"
        )


def get_total_system_memory() -> float:
    """
    Return the memory available to this process in GB (0.0 when unknown).

    On Linux this is min(physical RAM, cgroup memory limit), so a container's
    --memory limit is honored when sizing buffers and gating heavy features.
    Returns 0.0 when the platform cannot report memory (e.g. Windows), which
    callers treat as "unknown" and fail open.
    """
    host_gb = _get_host_memory_gb()
    if host_gb <= 0.0:
        return 0.0
    if sys.platform != "linux":
        return host_gb
    cgroup_gb = _get_cgroup_memory_limit_gb()
    if cgroup_gb is None or cgroup_gb <= 0.0:
        return host_gb
    return min(host_gb, cgroup_gb)


def _get_host_memory_gb() -> float:
    """Return host physical RAM in GB via sysconf, or 0.0 when unavailable."""
    try:
        total_memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return total_memory_bytes / (1024**3)
    except AttributeError, ValueError, OSError:
        # sysconf is unavailable on some platforms (e.g. Windows); treat as unknown.
        return 0.0


def _get_cgroup_memory_limit_gb(
    cgroup_root: str = "/sys/fs/cgroup", proc_cgroup: str = "/proc/self/cgroup"
) -> float | None:
    """
    Return this process's cgroup memory limit in GB, or None if unlimited/unavailable.

    cgroup v2 (memory.max) is tried first, then v1 (memory/memory.limit_in_bytes).

    :param cgroup_root: Mount point of the cgroup filesystem (overridable for tests).
    :param proc_cgroup: Path to the process cgroup file (overridable for tests).
    """
    limit = _read_cgroup_v2_limit(cgroup_root, proc_cgroup)
    if limit is not None:
        return limit
    return _read_cgroup_v1_limit(cgroup_root, proc_cgroup)


def _read_cgroup_v2_limit(cgroup_root: str, proc_cgroup: str) -> float | None:
    """Read the effective cgroup v2 memory limit in GB, or None."""
    rel = _read_self_cgroup_path(proc_cgroup, controller=None)
    return _min_hierarchical_limit(cgroup_root, rel, "memory.max")


def _read_cgroup_v1_limit(cgroup_root: str, proc_cgroup: str) -> float | None:
    """Read the effective cgroup v1 memory limit in GB, or None."""
    # On v1 the memory controller is conventionally mounted at <root>/memory.
    rel = _read_self_cgroup_path(proc_cgroup, controller="memory")
    return _min_hierarchical_limit(
        os.path.join(cgroup_root, "memory"), rel, "memory.limit_in_bytes"
    )


def _min_hierarchical_limit(base: str, rel: str | None, filename: str) -> float | None:
    """
    Return the smallest bounded memory limit (GB) across the cgroup and its ancestors, or None.

    The effective limit is the minimum imposed anywhere from the process's own cgroup
    up to the mount root, since a parent slice can cap memory even when the leaf cgroup
    itself is unlimited (e.g. systemd slices or nested k8s cgroups).

    :param base: Base of the hierarchy (the cgroup mount, or <mount>/memory on v1).
    :param rel: The process's cgroup path relative to base (from /proc/self/cgroup).
    :param filename: Limit file to read at each level (memory.max or memory.limit_in_bytes).
    """
    parts = [p for p in (rel or "").split("/") if p]
    limits: list[float] = []
    # Walk from the process's own cgroup up to the mount root.
    while True:
        directory = os.path.join(base, *parts) if parts else base
        limit = _read_cgroup_limit_file(os.path.join(directory, filename))
        if limit is not None:
            limits.append(limit)
        if not parts:
            break
        parts.pop()
    return min(limits) if limits else None


def _read_cgroup_limit_file(path: str) -> float | None:
    """
    Parse a cgroup memory-limit file into GB, or None if missing/unlimited/invalid.

    :param path: Path to a cgroup memory.max (v2) or memory.limit_in_bytes (v1) file.
    """
    try:
        with open(path) as fh:
            raw = fh.read().strip()
    except OSError:
        # File absent or unreadable (covers FileNotFoundError/PermissionError/etc.).
        return None
    # "max" (v2) or a near-INT64_MAX sentinel (v1) both mean "no limit set".
    if not raw or raw == "max":
        return None
    try:
        limit_bytes = int(raw)
    except ValueError:
        return None
    if limit_bytes <= 0 or limit_bytes >= _CGROUP_UNLIMITED_THRESHOLD:
        return None
    return limit_bytes / (1024**3)


def _read_self_cgroup_path(proc_cgroup: str, *, controller: str | None) -> str | None:
    """
    Return the process's cgroup path from /proc/self/cgroup, or None.

    :param proc_cgroup: Path to the process cgroup file.
    :param controller: For cgroup v1, the controller name (e.g. "memory") whose path
        to return. None selects the cgroup v2 unified hierarchy line ("0::<path>").
    """
    try:
        with open(proc_cgroup) as fh:
            for line in fh:
                parts = line.strip().split(":", 2)
                if len(parts) != 3:
                    continue
                hierarchy_id, controllers, path = parts
                if controller is None:
                    if hierarchy_id == "0" and controllers == "":
                        return path or "/"
                elif controller in controllers.split(","):
                    return path or "/"
    except OSError:
        return None
    return None


# cgroup v1 writes a near-INT64_MAX value (PAGE_SIZE * LONG_MAX on most kernels) to
# memory.limit_in_bytes when no limit is set; treat anything this large as unlimited.
_CGROUP_UNLIMITED_THRESHOLD: int = 1 << 62


def is_arm() -> bool:
    """Return whether the host CPU is ARM-based (32- or 64-bit)."""
    return platform.machine().lower() in ("arm64", "aarch64", "armv8l", "armv7l")


def inference_thread_budget() -> int:
    """
    Return the native thread budget for on-device inference.

    Defaults to ~25% of the available cores, or to an operator-supplied OMP_NUM_THREADS
    when that is set, so the torch and native pool budgets stay in agreement.
    """
    override = os.environ.get("OMP_NUM_THREADS", "")
    if override.isdigit() and (threads := int(override)) > 0:
        return threads
    return max(1, (os.process_cpu_count() or os.cpu_count() or 4) // 4)


def cap_native_thread_pools() -> int:
    """
    Cap the native BLAS/OpenMP thread pools process-wide and return the applied budget.

    Left uncapped, every one of these pools sizes itself to the full core count per worker
    and, across concurrent analysis sessions, saturates the box and starves playback.

    Must be called before any native math library is loaded, because these pools read their
    size from the environment once, at library load time. Capping them after the fact means
    walking the dynamic linker's loaded-library list (what threadpoolctl does), which
    deadlocks against a concurrent import: the walk holds the loader lock while it needs the
    GIL back for each callback, and an importing thread holds the GIL while it waits in
    dlopen() for that same loader lock.
    """
    budget = inference_thread_budget()
    for env_var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        # setdefault so an operator-supplied value always wins
        os.environ.setdefault(env_var, str(budget))
    return budget


async def verify_system_meets_requirements(
    *,
    feature_name: str,
    min_memory_gb: float = 0.0,
    min_cpu_cores: int = 0,
    require_ml_inference: bool = False,
) -> None:
    """
    Verify the host meets the minimum CPU/RAM requirements for a heavy provider.

    :param feature_name: Human-readable provider name used in the error message.
    :param min_memory_gb: Minimum total system RAM in GB (0 disables the check).
    :param min_cpu_cores: Minimum CPU core count (0 disables the check).
    :param require_ml_inference: When True, also verify the CPU can run on-device
        torch inference. Checked last, as it spawns a probe subprocess.
    :raises UnsupportedSystemError: If the system does not meet the requirements.
    """
    if shortfall := _resource_shortfall(min_memory_gb=min_memory_gb, min_cpu_cores=min_cpu_cores):
        message, translation_key, translation_args = shortfall
        raise UnsupportedSystemError(
            f"This system does not meet the minimal requirements for {feature_name}: {message}",
            translation_key=translation_key,
            translation_args=[feature_name, *translation_args],
        )
    if require_ml_inference:
        await verify_cpu_supports_ml_inference()


def system_meets_requirements(
    *,
    min_memory_gb: float = 0.0,
    min_cpu_cores: int = 0,
) -> bool:
    """
    Return whether the host meets the given RAM/CPU thresholds.

    A non-raising companion to verify_system_meets_requirements for soft UI hints
    (e.g. hiding a recommended-hardware notice) rather than gating setup. The
    ML-inference capability is not considered here.

    :param min_memory_gb: Minimum total system RAM in GB (0 disables the check).
    :param min_cpu_cores: Minimum CPU core count (0 disables the check).
    """
    return _resource_shortfall(min_memory_gb=min_memory_gb, min_cpu_cores=min_cpu_cores) is None


# The kernel reports MemTotal — installed RAM minus firmware/reserved pages — so a host
# always shows a little under its nominal size (a "4GB" box reports ~3.8GB). Allow this
# fraction of slack when checking a RAM target, in one place rather than per call site, so
# nominal requirements (4, 8 GB) match the hardware they describe without ad-hoc thresholds.
MEMORY_REPORTING_TOLERANCE: float = 0.08


def meets_memory_target(total_memory_gb: float, target_gb: float) -> bool:
    """
    Return whether reported RAM satisfies a nominal target within the reporting tolerance.

    Fails open (True) when the target is 0 (no requirement) or memory is unknown
    (0.0, e.g. Windows), so callers never block on a guess.

    :param total_memory_gb: RAM reported by get_total_system_memory() in GB.
    :param target_gb: Nominal RAM target in GB (e.g. 4 or 8).
    """
    if not target_gb or not total_memory_gb:
        return True
    return total_memory_gb >= target_gb * (1.0 - MEMORY_REPORTING_TOLERANCE)


def _resource_shortfall(
    *, min_memory_gb: float, min_cpu_cores: int
) -> tuple[str, str, list[Any]] | None:
    """
    Return an unmet RAM/CPU threshold as (message, translation_key, translation_args), or None.

    translation_args exclude the feature name, which the caller prepends.

    :param min_memory_gb: Minimum total system RAM in GB (0 disables the check).
    :param min_cpu_cores: Minimum CPU core count (0 disables the check).
    """
    cpu_cores = os.process_cpu_count() or os.cpu_count() or 1
    if min_cpu_cores and cpu_cores < min_cpu_cores:
        return (
            f"at least {min_cpu_cores} CPU cores are required ({cpu_cores} detected).",
            "unsupported_system_cpu_cores",
            [min_cpu_cores, cpu_cores],
        )
    total_memory_gb = get_total_system_memory()
    # meets_memory_target() fails open on unknown memory (0.0, e.g. Windows) and absorbs
    # the kernel's MemTotal under-report, so min_memory_gb stays a clean nominal figure.
    if min_memory_gb and not meets_memory_target(total_memory_gb, min_memory_gb):
        return (
            f"at least {min_memory_gb:.0f}GB of RAM is required ({total_memory_gb:.1f}GB detected).",
            "unsupported_system_memory",
            [f"{min_memory_gb:.0f}", f"{total_memory_gb:.1f}"],
        )
    return None


# How long to wait for the out-of-process inference probe before treating it as
# inconclusive. The probe only imports torch and runs a few tiny tensors, but a cold,
# heavily loaded VM can be slow to start the interpreter, so keep this generous.
_ML_INFERENCE_PROBE_TIMEOUT = 60.0
# POSIX signals that mean the CPU could not execute the inference (the probe exits with the
# negated signal number). Any of these disables the feature; other exits fail open.
_ML_INFERENCE_FAULT_SIGNALS = frozenset(
    {signal.SIGILL, signal.SIGSEGV, signal.SIGABRT, signal.SIGFPE}
)


async def verify_cpu_supports_ml_inference() -> None:
    """
    Verify the CPU can actually execute on-device ML (torch) inference.

    Runs a representative inference in a throwaway subprocess, so a CPU that reports a
    capability it cannot actually execute (common on virtual machines without host CPU
    passthrough) crashes the probe instead of the server. Inconclusive probe results fail
    open, so a probe malfunction never blocks a capable host.

    :raises UnsupportedSystemError: If the CPU lacks AVX2, or reports it but cannot execute
        the required instructions.
    """
    if platform.machine().lower() not in ("x86_64", "amd64", "i386", "i686", "x86"):
        # non-x86 (ARM) machines run quantized inference via QNNPACK instead of FBGEMM
        return
    from music_assistant.helpers import _ml_inference_probe  # noqa: PLC0415

    returncode = await _run_ml_inference_probe()
    if returncode == _ml_inference_probe.PROBE_CAPABLE:
        return
    if returncode == _ml_inference_probe.PROBE_NO_AVX2:
        raise UnsupportedSystemError(
            "On-device audio analysis requires a CPU with AVX2 support "
            "(Intel Haswell / AMD Zen or newer). This CPU does not support AVX2. "
            "If you are running in a virtual machine (e.g. Proxmox), changing the "
            "CPU type to 'host' may expose AVX2 to the guest.",
            translation_key="unsupported_system_avx2",
        )
    if returncode is not None and returncode < 0 and -returncode in _ML_INFERENCE_FAULT_SIGNALS:
        raise UnsupportedSystemError(
            "On-device audio analysis cannot run on this CPU: it reports AVX2 support but "
            "fails to execute the required instructions. This is common on virtual machines "
            "without host CPU passthrough -- if you are running in a VM (e.g. Proxmox or "
            "TrueNAS), set the CPU type to 'host'.",
            translation_key="unsupported_system_ml_inference_failed",
        )
    # Inconclusive: the probe could not be spawned, timed out, was OOM-killed, or exited for
    # an unexpected reason. Assume the host is capable rather than block a working setup.
    LOGGER.warning(
        "On-device ML inference capability probe was inconclusive (exit code %s); "
        "assuming this CPU is capable",
        returncode,
    )


async def _run_ml_inference_probe() -> int | None:
    """
    Run the inference probe subprocess and return its exit code.

    Returns None when the probe could not be started or did not finish in time; otherwise
    the process return code (negative if a signal killed it).
    """
    from music_assistant.helpers import _ml_inference_probe  # noqa: PLC0415

    try:
        # Run with -m, not by file path: a path run puts the probe's own directory on
        # sys.path, which would shadow the stdlib (e.g. helpers/logging.py over logging).
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _ml_inference_probe.__name__,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as err:
        LOGGER.warning("Could not start the ML inference capability probe: %s", err)
        return None
    try:
        await asyncio.wait_for(proc.wait(), timeout=_ML_INFERENCE_PROBE_TIMEOUT)
    except TimeoutError:
        proc.kill()
        with suppress(ProcessLookupError):
            await proc.wait()
        LOGGER.warning("The ML inference capability probe timed out")
        return None
    return proc.returncode


keyword_pattern = re.compile("title=|artist=")
title_pattern = re.compile(r"title=\"(?P<title>.*?)\"")
artist_pattern = re.compile(r"artist=\"(?P<artist>.*?)\"")
dot_com_pattern = re.compile(r"(?P<netloc>\(?\w+\.(?:\w+\.)?(\w{2,3})\)?)")
ad_pattern = re.compile(r"((ad|advertisement)_)|^AD\s\d+$|ADBREAK", flags=re.IGNORECASE)
title_artist_order_pattern = re.compile(r"(?P<title>.+)\sBy:\s(?P<artist>.+)", flags=re.IGNORECASE)
# German format used by some stations: "Track" von Artist
german_von_pattern = re.compile(r'^"(?P<title>[^"]+)"\s+von\s+(?P<artist>.+)$', flags=re.IGNORECASE)
# English format used by some stations: "Track" by Artist from "Album" (album optional).
# Title and album are quote-delimited, so the non-greedy artist plus the anchored,
# quoted album group keep "by"/"from" inside the artist name from being mis-split.
english_by_pattern = re.compile(
    r'^"(?P<title>[^"]+)"\s+by\s+(?P<artist>.+?)(?:\s+from\s+"(?P<album>[^"]*)")?$',
    flags=re.IGNORECASE,
)
multi_space_pattern = re.compile(r"\s{2,}")
end_junk_pattern = re.compile(r"(.+?)(\s\W+)$")

# HTML tags worth preserving as markdown; any other tag is stripped (text kept)
MARKDOWN_SAFE_TAGS = [
    "a",
    "b",
    "blockquote",
    "br",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "i",
    "li",
    "ol",
    "p",
    "strong",
    "ul",
]

VERSION_PARTS = (
    # list of common version strings
    "version",
    "live",
    "edit",
    "remix",
    "mix",
    "acoustic",
    "instrumental",
    "karaoke",
    "remaster",
    "remastered",
    "versie",
    "unplugged",
    "disco",
    "akoestisch",
    "deluxe",
    "video",
    "radio",
    "extended",
    "single",
    "edition",
    "anniversary",
    "stereo",
    "album",
    "bonus",
    "release",
)
IGNORE_TITLE_PARTS = (
    # strings that may be stripped off a title part
    # (most important the featuring parts)
    "feat.",
    "featuring",
    "ft.",
    "with ",
    "explicit",
)
WITH_TITLE_WORDS = (
    # words that, when following "with", indicate this is part of the song title
    # not a featuring credit.
    "someone",
    "the",
    "u",
    "you",
    "no",
)

# Keywords for aggressive search cleaning (includes featuring).
_VERSION_PATTERN = "|".join(re.escape(v) for v in VERSION_PARTS)
_FEAT_PATTERN = r"feat(?:uring)?|ft"
_SEARCH_PATTERN = rf"{_VERSION_PATTERN}|{_FEAT_PATTERN}"

_SEARCH_PAREN_PATTERN = re.compile(
    rf"[\(\[][^\)\]]*\b({_SEARCH_PATTERN})\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_SEARCH_HYPHEN_PATTERN = re.compile(
    rf"(\s*-\s*(\d{{4}}|{_SEARCH_PATTERN}).*)$",
    re.IGNORECASE,
)

# Superfluous suffixes to strip for display (video/audio markers, etc.)
_DISPLAY_STRIP_PATTERN = re.compile(
    r"\s*[\(\[]"
    r"(official\s+)?(lyric\s+|music\s+)?(video|audio|visualizer|clip)"
    r"[\)\]]$",
    re.IGNORECASE,
)

# Featuring patterns for stripping from titles (not in parentheses).
_FEATURING_PATTERNS = (
    " featuring ",
    " feat. ",
    " feat ",
    " ft. ",
    " ft ",
)


def filename_from_string(string: str) -> str:
    """Create filename from unsafe string."""
    keepcharacters = (" ", ".", "_")
    return "".join(c for c in string if c.isalnum() or c in keepcharacters).rstrip()


# aiohttp rejects the full C0 control character range plus DEL in response headers
# to prevent header injection attacks (see aiohttp http_writer._FORBIDDEN_HEADER_CHARS_RE)
_FORBIDDEN_HEADER_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_http_header_value(value: str) -> str:
    """Replace control characters that are not allowed in HTTP header values."""
    return _FORBIDDEN_HEADER_CHARS_RE.sub(" ", value).strip()


def try_parse_int(possible_int: Any, default: int | None = 0) -> int | None:
    """Try to parse an int."""
    try:
        return int(float(possible_int))
    except TypeError, ValueError:
        return default


def try_parse_float(possible_float: Any, default: float | None = 0.0) -> float | None:
    """Try to parse a float."""
    try:
        return float(possible_float)
    except TypeError, ValueError:
        return default


def try_parse_bool(possible_bool: Any) -> bool:
    """Try to parse a bool."""
    if isinstance(possible_bool, bool):
        return possible_bool
    return possible_bool in ["true", "True", "1", "on", "ON", 1]


def try_parse_duration(duration_str: str) -> float:
    """Try to parse a duration in seconds from a duration (HH:MM:SS) string."""
    milliseconds = (
        float("0." + duration_str.rsplit(".", maxsplit=1)[-1]) if "." in duration_str else 0.0
    )
    duration_parts = duration_str.split(".", maxsplit=1)[0].split(",", maxsplit=1)[0].split(":")
    if len(duration_parts) == 3:
        seconds = sum(x * int(t) for x, t in zip([3600, 60, 1], duration_parts, strict=False))
    elif len(duration_parts) == 2:
        seconds = sum(x * int(t) for x, t in zip([60, 1], duration_parts, strict=False))
    else:
        seconds = int(duration_parts[0])
    return seconds + milliseconds


def normalize_unicode(value: str | None) -> str | None:
    """
    Normalize Unicode strings to NFC form for consistent handling.

    This ensures that Unicode characters like "é" are stored as single
    codepoints rather than "e" + combining accent mark, which prevents
    issues with string comparisons and memory bloat.

    :param value: String to normalize, or None.
    """
    if value is None:
        return None
    return unicodedata.normalize("NFC", value)


@functools.lru_cache(maxsize=2048)
def parse_title_and_version(
    title: str,
    track_version: str | None = None,
    strip_for_search: bool = False,
    strip_for_display: bool = False,
) -> tuple[str, str]:
    """
    Parse version from the title and optionally clean for search or display.

    :param title: The title to parse.
    :param track_version: Optional existing version string.
    :param strip_for_search: Aggressively strip for search matching.
    :param strip_for_display: Strip superfluous suffixes for display.
    """
    version_parts = [track_version] if track_version else []
    version_keys = {track_version.casefold()} if track_version else set()

    # Strip featuring, bracketed version info, and hyphen suffixes (e.g. "- Remastered 2019")
    if strip_for_search:
        title = _SEARCH_PAREN_PATTERN.sub("", title)
        title = _SEARCH_HYPHEN_PATTERN.sub("", title)
        # Strip bare featuring credits (not in parentheses)
        title_lower = title.lower()
        for pattern in _FEATURING_PATTERNS:
            if pattern in title_lower:
                idx = title_lower.find(pattern)
                title = title[:idx]
                break
        # Clean up dangling hyphens and extra spaces
        title = re.sub(r"\s*-\s*$", "", title)
        title = re.sub(r"\s+", " ", title).strip()
        return title, track_version or ""

    # Strip video/audio suffixes like "(Official Video)"
    if strip_for_display:
        title = _DISPLAY_STRIP_PATTERN.sub("", title).strip()
        return title, track_version or ""

    # Standard version parsing
    # each pass extracts from the current title so removals from
    # earlier passes are taken into account
    for extract_parts in (
        lambda t: _balanced_bracket_groups(t, "(", ")"),
        lambda t: _balanced_bracket_groups(t, "[", "]"),
        lambda t: re.findall(r" - .*", t),
    ):
        for title_part in extract_parts(title):
            # skip parts already consumed by an earlier removal in this pass
            if title_part not in title:
                continue
            # Extract the content without brackets/dashes for checking
            clean_part = title_part.translate(str.maketrans("", "", "()[]-")).strip().lower()

            # Check if this should be ignored (featuring/explicit parts)
            should_ignore = False
            for ignore_str in IGNORE_TITLE_PARTS:
                if clean_part.startswith(ignore_str):
                    # Special handling for "with " - check if followed by title words
                    if ignore_str == "with ":
                        # Extract the word after "with "
                        after_with = (
                            clean_part[len("with ") :].split()[0]
                            if len(clean_part) > len("with ")
                            else ""
                        )
                        if after_with in WITH_TITLE_WORDS:
                            # This is part of the title (e.g., "with you"), don't ignore
                            break
                    # Remove this part from the title
                    title = title.replace(title_part, "").strip()
                    should_ignore = True
                    break

            if should_ignore:
                continue

            # Check if this part is a version
            for version_str in VERSION_PARTS:
                if version_str in clean_part:
                    # Preserve original casing (and any nested brackets) for output
                    version_part = _strip_outer_markers(title_part)
                    if version_part.casefold() not in version_keys:
                        version_parts.append(version_part)
                        version_keys.add(version_part.casefold())
                    title = title.replace(title_part, "").strip()
                    break
    title = re.sub(r"\s{2,}", " ", title).strip()
    return title, " ".join(version_parts)


def _balanced_bracket_groups(text: str, open_char: str, close_char: str) -> list[str]:
    """
    Return the top-level balanced bracketed substrings, including the outer brackets.

    :param text: The text to scan.
    :param open_char: The opening bracket character.
    :param close_char: The closing bracket character.
    """
    groups: list[str] = []
    depth = 0
    start = -1
    for idx, char in enumerate(text):
        if char == open_char:
            if depth == 0:
                start = idx
            depth += 1
        elif char == close_char and depth > 0:
            depth -= 1
            if depth == 0:
                groups.append(text[start : idx + 1])
    return groups


def _strip_outer_markers(part: str) -> str:
    """
    Strip the outer brackets or leading hyphen from a parsed title part.

    :param part: The raw title part as matched from the title.
    """
    part = part.strip()
    # only strip a single outer bracket pair so nested brackets stay intact
    if part[:1] in "([" and part[-1:] in ")]":
        return part[1:-1].strip()
    return part.lstrip("- ").strip()


def infer_album_type(title: str, version: str) -> AlbumType:
    """Infer album type by looking for live or soundtrack indicators."""
    combined = f"{title} {version}".lower()
    for pat in LIVE_INDICATORS:
        if re.search(pat, combined):
            return AlbumType.LIVE
    for pat in SOUNDTRACK_INDICATORS:
        if re.search(pat, combined):
            return AlbumType.SOUNDTRACK
    return AlbumType.UNKNOWN


def strip_ads(line: str) -> str:
    """Strip Ads from line."""
    if ad_pattern.search(line):
        return "Advert"
    return line


def strip_url(line: str) -> str:
    """Strip URL from line."""
    return (
        " ".join([p for p in line.split() if (not urlparse(p).scheme or not urlparse(p).netloc)])
    ).rstrip()


def strip_dotcom(line: str) -> str:
    """Strip scheme-less netloc from line."""
    return dot_com_pattern.sub("", line)


def strip_end_junk(line: str) -> str:
    """Strip non-word info from end of line."""
    return end_junk_pattern.sub(r"\1", line)


def swap_title_artist_order(line: str) -> str:
    """Swap title/artist order in line."""
    return title_artist_order_pattern.sub(r"\g<artist> - \g<title>", line)


def strip_multi_space(line: str) -> str:
    """Strip multi-whitespace from line."""
    return multi_space_pattern.sub(" ", line)


def html_to_markdown(line: str) -> str:
    """Convert the safe subset of HTML in a string to markdown, stripping other tags."""
    # unescape first so entity-encoded markup (e.g. "&lt;p&gt;") is handled too
    return markdownify(
        html.unescape(line),
        convert=MARKDOWN_SAFE_TAGS,
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
    ).strip()


def multi_strip(line: str) -> str:
    """Strip assorted junk from line."""
    return strip_multi_space(
        swap_title_artist_order(strip_end_junk(strip_dotcom(strip_url(strip_ads(line)))))
    ).rstrip()


def parse_quoted_stream_title(line: str) -> tuple[str, str, str | None] | None:
    """
    Parse stream titles that name the track in natural language with a quoted title.

    Recognises '"Track" by Artist from "Album"' (album optional) and the German
    '"Track" von Artist'.

    :param line: Raw (uncleaned) stream title.
    :returns: Tuple of (title, artist, album), or None when the line is not in one of
        these formats. ``album`` is None when the station omits it.
    """
    stripped = line.strip()
    if match := english_by_pattern.match(stripped):
        title = multi_strip(match.group("title"))
        artist = multi_strip(match.group("artist")).strip('"')
        album_raw = match.group("album")
        album = multi_strip(album_raw).strip('"') if album_raw else None
        if title and artist:
            return title, artist, album or None
    if match := german_von_pattern.match(stripped):
        title = multi_strip(match.group("title"))
        artist = multi_strip(match.group("artist")).strip('"')
        if title and artist:
            return title, artist, None
    return None


def clean_stream_title(line: str) -> str:
    """Strip junk text from radio streamtitle."""
    title: str = ""
    artist: str = ""

    if not keyword_pattern.search(line):
        if parsed := parse_quoted_stream_title(line):
            track_name, artist_name, _ = parsed
            return f"{artist_name} - {track_name}"
        return multi_strip(line)

    if match := title_pattern.search(line):
        title = multi_strip(match.group("title"))

    if match := artist_pattern.search(line):
        possible_artist = multi_strip(match.group("artist"))
        if possible_artist and possible_artist != title:
            artist = possible_artist

    if not title and not artist:
        return ""

    if title:
        if re.search(" - ", title) or not artist:
            return title
        if artist:
            return f"{artist} - {title}"

    if artist:
        return artist

    return line


# cache for get_ip_addresses: enumerating the network adapters involves a thread hop,
# socket probes and a full adapter walk, while the result rarely (if ever) changes
IP_ADDRESSES_CACHE_TTL = 30
_ip_addresses_cache: dict[tuple[bool, bool], tuple[float, tuple[str, ...]]] = {}
_ip_addresses_pending: dict[tuple[bool, bool], asyncio.Task[tuple[str, ...]]] = {}

# Interfaces that only ever carry container, VM or VPN traffic, so a device on the local
# network can never reach us on their addresses.
_VIRTUAL_INTERFACE_PREFIXES = (
    "cali",
    "cni",
    "docker",
    "flannel",
    "hassio",
    "incusbr",
    "lxcbr",
    "lxdbr",
    "nordlynx",
    "podman",
    "ppp",
    "tailscale",
    "tap",
    "tun",
    "utun",
    "vboxnet",
    "veth",
    "virbr",
    "vmnet",
    "wg",
    "zt",
)
# Docker names its user-defined bridges br-<12 hex> and the macOS host-only bridges of
# Docker Desktop, Parallels and VMware start at bridge100. Both are matched in full, so a
# hand-named LAN bridge (br-lan on OpenWrt, a second macOS bridge1) is left alone - as are
# the regular LAN bridge names br0, vmbr0 and bond0.
_VIRTUAL_INTERFACE_NAMES = re.compile(r"br-[0-9a-f]{12}|bridge\d{3}")


async def get_ip_addresses(include_ipv6: bool = False) -> tuple[str, ...]:
    """
    Return all IP addresses of all network interfaces.

    Always returns at least one address: when no routable address is found
    (e.g. offline host), the loopback address is returned as fallback.
    Results are cached for a short while, so an IP/interface change may take up to
    IP_ADDRESSES_CACHE_TTL seconds to be reflected.

    :param include_ipv6: Whether to include IPv6 addresses in the result.
    """
    return await _get_ip_addresses(include_ipv6, publish_candidates_only=False)


async def get_publish_ip_candidates(include_ipv6: bool = False) -> tuple[str, ...]:
    """
    Return the IP addresses a device on the local network may reach this host on.

    Same as get_ip_addresses, minus the addresses of container, VM and VPN interfaces -
    unless the host holds no other address at all.

    :param include_ipv6: Whether to include IPv6 addresses in the result.
    """
    return await _get_ip_addresses(include_ipv6, publish_candidates_only=True)


async def _get_ip_addresses(include_ipv6: bool, publish_candidates_only: bool) -> tuple[str, ...]:
    """Return the host's IP addresses, enumerating the adapters at most once per TTL."""
    cache_key = (include_ipv6, publish_candidates_only)
    if cached := _ip_addresses_cache.get(cache_key):
        cached_at, addresses = cached
        if (time.monotonic() - cached_at) < IP_ADDRESSES_CACHE_TTL:
            return addresses

    async def _probe() -> tuple[str, ...]:
        try:
            addresses = await asyncio.to_thread(
                _enumerate_ip_addresses, include_ipv6, publish_candidates_only
            )
            _ip_addresses_cache[cache_key] = (time.monotonic(), addresses)
            return addresses
        finally:
            _ip_addresses_pending.pop(cache_key, None)

    # single-flight: no await between the pending-check and storing the task,
    # so concurrent callers always end up awaiting the same probe
    if not (pending := _ip_addresses_pending.get(cache_key)):
        pending = asyncio.create_task(_probe())
        pending.add_done_callback(_log_ip_probe_failure)
        _ip_addresses_pending[cache_key] = pending
    return await join_task(pending)


def _log_ip_probe_failure(probe: asyncio.Task[tuple[str, ...]]) -> None:
    """Log (and thereby retrieve) the exception of a finished address probe, if any."""
    if probe.cancelled():
        return
    # every waiter that is still around reports the failure itself, so a debug line is
    # enough here; retrieving the exception is what keeps asyncio from reporting it as
    # "Task exception was never retrieved" once the probe is garbage collected
    if (err := probe.exception()) is not None:
        LOGGER.debug("Enumerating IP addresses failed: %s", err)


def _enumerate_ip_addresses(include_ipv6: bool, publish_candidates_only: bool) -> tuple[str, ...]:
    """Enumerate all IP addresses of all network interfaces (blocking)."""
    result: list[tuple[int, str]] = []
    # the same addresses, without the ones no device on the local network can reach
    lan_result: list[tuple[int, str]] = []
    # try to get the primary IP address
    # this is the IP address of the default route
    primary_ip = ""
    # try IPv4 first
    _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _sock.settimeout(0)
    try:
        # doesn't even have to be reachable
        _sock.connect(("10.254.254.254", 1))
        primary_ip = _sock.getsockname()[0]
    except Exception:
        primary_ip = ""
    finally:
        _sock.close()
    # fall back to IPv6 if no IPv4 primary found (e.g. IPv6-only networks)
    if not primary_ip:
        _sock6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        _sock6.settimeout(0)
        try:
            _sock6.connect(("2001:db8::1", 1))
            primary_ip = _sock6.getsockname()[0]
        except Exception:
            primary_ip = ""
        finally:
            _sock6.close()
    # get all IP addresses of all network interfaces
    adapters = ifaddr.get_adapters()
    for adapter in adapters:
        adapter_is_virtual = _is_virtual_interface(adapter.name) or _is_virtual_interface(
            adapter.nice_name
        )
        for ip in adapter.ips:
            if ip.is_IPv6 and not include_ipv6:
                continue
            # ifaddr returns IPv6 addresses as (address, flowinfo, scope_id) tuples
            ip_str = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
            if ip_str.startswith(("127", "169.254")):
                # filter out IPv4 loopback/APIPA address
                continue
            if ip_str.startswith(("::1", "::ffff:", "fe80")):
                # filter out IPv6 loopback/link-local address
                continue
            if ip_str == primary_ip:
                score = 10
            elif ip_str.startswith(("192.168.",)):
                # we rank the 192.168 range a bit higher as its most
                # often used as the private network subnet
                score = 2
            elif ip_str.startswith(("172.", "10.", "192.")):
                # we rank the 172 range a bit lower as its most
                # often used as the private docker network
                score = 1
            else:
                score = 0
            result.append((score, ip_str))
            if not adapter_is_virtual:
                lan_result.append((score, ip_str))
    # a host that is only reachable over a tunnel or bridge still has to publish something
    selected = (lan_result or result) if publish_candidates_only else result
    selected.sort(key=lambda x: x[0], reverse=True)
    if not selected:
        # no routable addresses found (e.g. offline host with only loopback/link-local):
        # fall back to loopback so callers that rely on at least one address keep working
        return ("127.0.0.1",)
    return tuple(ip[1] for ip in selected)


def _is_virtual_interface(name: str) -> bool:
    """Return whether the named interface belongs to a container, VM or VPN network."""
    name = name.lower()
    return name.startswith(_VIRTUAL_INTERFACE_PREFIXES) or bool(
        _VIRTUAL_INTERFACE_NAMES.fullmatch(name)
    )


def interface_name_for_ip(ip: str) -> str | None:
    """
    Return the name of the network interface that holds the given IP, or None.

    Used to map a bind/publish IP to its interface name for components that select
    their mDNS/zeroconf advertisement interface by name (e.g. shairport-sync and
    go-librespot), so the advertisement stays on the intended network.

    :param ip: The IPv4/IPv6 address to look up.
    """
    for adapter in ifaddr.get_adapters():
        for ip_config in adapter.ips:
            addr = ip_config.ip if isinstance(ip_config.ip, str) else ip_config.ip[0]
            if addr == ip:
                return adapter.name
    return None


async def is_port_in_use(port: int, host: str | None = None) -> bool:
    """
    Check if a port is in use.

    :param port: Port number to check.
    :param host: Optional bind address to probe. When omitted, both IPv4 and IPv6
        wildcard addresses are checked.
    """

    def _is_port_in_use() -> bool:
        candidates: tuple[tuple[socket.AddressFamily, str], ...]
        if host is not None:
            candidates = ((socket.AF_INET6 if ":" in host else socket.AF_INET, host),)
        else:
            # Try both IPv4 and IPv6 to support single-stack and dual-stack systems.
            # A port is considered free if it can be bound on at least one address family.
            candidates = ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::"))
        for family, addr in candidates:
            try:
                with socket.socket(family, socket.SOCK_STREAM) as _sock:
                    # Set SO_REUSEADDR to match asyncio.start_server behavior
                    # This allows binding to ports in TIME_WAIT state
                    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    _sock.bind((addr, port))
                    return False
            except OSError:
                continue
        return True

    return await asyncio.to_thread(_is_port_in_use)


# In-process reservations for ports handed out by select_free_port. Provider
# instances (and reloads) frequently call select_free_port at nearly the same
# moment and only bind the returned port asynchronously afterwards, so a port
# that was just handed out is not yet detectable as "in use". Keeping a
# short-lived reservation per returned port stops concurrent/successive callers
# from picking the same one. Reservations expire automatically after the grace
# period so the range is never permanently exhausted across reloads.
_PORT_RESERVATION_TTL = 60.0
_reserved_ports: dict[int, float] = {}
_select_free_port_lock = asyncio.Lock()


async def select_free_port(range_start: int, range_end: int, host: str | None = None) -> int:
    """
    Find and reserve a free port within the given range.

    The returned port is reserved so concurrent or successive callers are not
    handed the same port.

    :param range_start: First port (inclusive) of the range to search.
    :param range_end: Port to stop before (exclusive) when searching the range.
    :param host: Optional bind address to probe for availability.
    """
    async with _select_free_port_lock:
        now = time.monotonic()
        # drop expired reservations so their ports become reusable again
        for reserved_port, deadline in list(_reserved_ports.items()):
            if deadline <= now:
                del _reserved_ports[reserved_port]
        for port in range(range_start, range_end):
            if port in _reserved_ports:
                continue
            if not await is_port_in_use(port, host=host):
                _reserved_ports[port] = now + _PORT_RESERVATION_TTL
                return port
    msg = f"No free port available in range {range_start}-{range_end - 1}"
    raise OSError(msg)


async def get_ip_from_host(dns_name: str) -> str | None:
    """Resolve (first) IP-address for given dns name."""

    def _resolve() -> str | None:
        try:
            # use getaddrinfo to support both IPv4 and IPv6 resolution
            results = socket.getaddrinfo(dns_name, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            if results:
                return str(results[0][4][0])
        except Exception:
            # fail gracefully!
            return None
        return None

    return await asyncio.to_thread(_resolve)


async def get_source_ip_for_target(target_ip: str) -> str:
    """
    Return the local interface address the routing table would egress to ``target_ip`` from.

    Empty when no route to the target can be determined.

    :param target_ip: IP address of the device the traffic is meant for.
    """

    def _routing_lookup() -> str:
        try:
            is_ipv6_target = ip_address(target_ip).version == 6
        except ValueError:
            is_ipv6_target = False
        route_family = socket.AF_INET6 if is_ipv6_target else socket.AF_INET
        route_target: tuple[str, int] | tuple[str, int, int, int] = (
            (target_ip, 80, 0, 0) if is_ipv6_target else (target_ip, 80)
        )
        with socket.socket(route_family, socket.SOCK_DGRAM) as _sock:
            try:
                _sock.settimeout(1.0)
                _sock.connect(route_target)
                routed_ip = str(_sock.getsockname()[0])
                if routed_ip and routed_ip not in WILDCARD_BIND_IPS:
                    return routed_ip
            except OSError:
                pass
        return ""

    return await asyncio.to_thread(_routing_lookup)


async def get_ip_pton(ip_string: str) -> bytes:
    """Return socket pton for a local ip."""
    try:
        return await asyncio.to_thread(socket.inet_pton, socket.AF_INET, ip_string)
    except OSError:
        return await asyncio.to_thread(socket.inet_pton, socket.AF_INET6, ip_string)


def format_ip_for_url(ip_address: str) -> str:
    """Wrap IPv6 addresses in brackets for use in URLs (RFC 2732)."""
    if ":" in ip_address:
        return f"[{ip_address}]"
    return ip_address


async def get_folder_size(folderpath: str) -> float:
    """Return folder size in gb."""

    def _get_folder_size(folderpath: str) -> float:
        total_size = 0
        for dirpath, _dirnames, filenames in os.walk(folderpath):
            for _file in filenames:
                _fp = os.path.join(dirpath, _file)
                total_size += Path(_fp).stat().st_size
        return total_size / float(1 << 30)

    return await asyncio.to_thread(_get_folder_size, folderpath)


def get_changed_keys(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    recursive: bool = False,
) -> set[str]:
    """Compare 2 dicts and return set of changed keys."""
    # TODO: Check with Marcel whether we should calculate new dicts based on ignore_keys
    return set(get_changed_dict_values(dict1, dict2, recursive).keys())
    # return set(get_changed_dict_values(dict1, dict2, ignore_keys, recursive).keys())


def get_changed_dict_values(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    recursive: bool = False,
) -> dict[str, tuple[Any, Any]]:
    """
    Compare 2 dicts and return dict of changed values.

    dict key is the changed key, value is tuple of old and new values.
    """
    if not dict1 and not dict2:
        return {}
    if not dict1:
        return {key: (None, value) for key, value in dict2.items()}
    if not dict2:
        return {key: (None, value) for key, value in dict1.items()}
    changed_values = {}
    for key, value in dict2.items():
        if isinstance(value, dict) and isinstance(dict1[key], dict) and recursive:
            changed_subvalues = get_changed_dict_values(dict1[key], value, recursive)
            for subkey, subvalue in changed_subvalues.items():
                changed_values[f"{key}.{subkey}"] = subvalue
            continue
        if key not in dict1:
            changed_values[key] = (None, value)
            continue
        if dict1[key] != value:
            changed_values[key] = (dict1[key], value)
    return changed_values


def empty_queue[T](q: asyncio.Queue[T]) -> None:
    """Empty an asyncio Queue."""
    for _ in range(q.qsize()):
        try:
            q.get_nowait()
            q.task_done()
        except asyncio.QueueEmpty, ValueError:
            pass


async def install_package(package: str) -> None:
    """Install package with pip, raise when install failed."""
    LOGGER.debug("Installing python package %s", package)
    args = ["uv", "pip", "install", "--no-cache", package]
    return_code, output = await check_output(*args)
    if return_code != 0:
        msg = f"Failed to install package {package}\n{output.decode()}"
        raise RuntimeError(msg)


async def get_package_version(pkg_name: str) -> str | None:
    """
    Return the version of an installed (python) package.

    Will return None if the package is not found.
    """
    try:
        return await asyncio.to_thread(pkg_version, pkg_name)
    except PackageNotFoundError:
        return None


async def is_hass_supervisor() -> bool:
    """Return if we're running inside the HA Supervisor (e.g. HAOS)."""
    # Fast path: check for HA supervisor token environment variable
    # This is always set when running inside the HA supervisor
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return False

    # Token exists, verify the supervisor is actually reachable
    def _check() -> bool:
        try:
            urllib.request.urlopen("http://supervisor/core", timeout=1)
        except urllib.error.URLError as err:
            # this should return a 401 unauthorized if it exists
            return getattr(err, "code", 999) == 401
        except Exception:
            return False
        return False

    return await asyncio.to_thread(_check)


# CPython holds a lock per module while importing it, so two threads importing modules with
# overlapping dependency graphs (e.g. two providers that both pull in `requests`) can end up
# waiting on each other's module locks. The import machinery then bails out at one of them with
# a _DeadlockError ("deadlock detected by _ModuleLock(...)") instead of hanging, which surfaces
# as a provider that failed to load and stays broken until it is reloaded by hand.
# A single-worker executor keeps imports serialized without parking a thread from the default
# pool while waiting; only the import itself is serialized, providers still load concurrently.
_IMPORT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="module_import")

# requirements verified this session, so repeated (config) loads skip the version check
_checked_requirements: set[str] = set()


async def import_module_in_thread(name: str, package: str | None = None) -> ModuleType:
    """
    Import a module in a thread, serialized against all other imports done this way.

    :param name: Name of the module to import, may be relative to the given package.
    :param package: Package to resolve the name against, required for a relative name.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_IMPORT_EXECUTOR, importlib.import_module, name, package)
    except RuntimeError as err:
        # threads we do not control (a library importing lazily in its own thread) can still
        # cross a module lock with ours; the import machinery reports that as a deadlock at
        # whoever detects it. The other import has finished by now, so a single retry sticks.
        if "deadlock detected" not in str(err):
            raise
        LOGGER.warning("Retrying import of %s after a module lock collision: %s", name, err)
        return await loop.run_in_executor(_IMPORT_EXECUTOR, importlib.import_module, name, package)


async def load_provider_module(domain: str, requirements: list[str]) -> ProviderModuleType:
    """Return module for given provider domain and make sure the requirements are met."""

    async def _get_provider_module() -> ProviderModuleType:
        module = await import_module_in_thread(f".{domain}", "music_assistant.providers")
        return cast("ProviderModuleType", module)

    # ensure module requirements are met
    for requirement in requirements:
        if requirement in _checked_requirements:
            continue
        if "==" not in requirement:
            # we should really get rid of unpinned requirements
            continue
        package_name, version = requirement.split("==", 1)
        # importlib.metadata can't resolve extras (e.g. aiosendspin[server]), so strip them
        package_name = package_name.split("[", 1)[0]
        installed_version = await get_package_version(package_name)
        if installed_version == "0.0.0":
            # ignore editable installs
            _checked_requirements.add(requirement)
            continue
        if installed_version != version:
            await install_package(requirement)
        _checked_requirements.add(requirement)

    # try to load the module
    try:
        return await _get_provider_module()
    except ImportError:
        # (re)install ALL requirements
        for requirement in requirements:
            await install_package(requirement)
    # try loading the provider again to be safe
    # this will fail if something else is wrong (as it should)
    return await _get_provider_module()


async def has_tmpfs_mount() -> bool:
    """Check if we have a tmpfs mount."""

    def _has_tmpfs_mount() -> bool:
        """Check if we have a tmpfs mount."""
        try:
            with open("/proc/mounts") as file:
                for line in file:
                    if "tmpfs /tmp tmpfs rw" in line:
                        return True
        except FileNotFoundError, OSError, PermissionError:
            pass
        return False

    return await asyncio.to_thread(_has_tmpfs_mount)


async def get_free_space(folder: str) -> float:
    """Return free space on given folderpath in GB."""

    def _get_free_space(folder: str) -> float:
        """Return free space on given folderpath in GB."""
        try:
            res = shutil.disk_usage(folder)
            return res.free / float(1 << 30)
        except FileNotFoundError, OSError, PermissionError:
            return 0.0

    return await asyncio.to_thread(_get_free_space, folder)


async def get_free_space_percentage(folder: str) -> float:
    """Return free space on given folderpath in percentage."""

    def _get_free_space(folder: str) -> float:
        """Return free space on given folderpath in GB."""
        try:
            res = shutil.disk_usage(folder)
            return res.free / res.total * 100
        except FileNotFoundError, OSError, PermissionError:
            return 0.0

    return await asyncio.to_thread(_get_free_space, folder)


async def has_enough_space(folder: str, size: int) -> bool:
    """Check if folder has enough free space."""
    return await get_free_space(folder) > size


def divide_chunks(data: bytes, chunk_size: int) -> Iterator[bytes]:
    """Chunk bytes data into smaller chunks."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


async def remove_file(file_path: str) -> None:
    """Remove file path (if it exists)."""
    if not await asyncio.to_thread(os.path.exists, file_path):
        return
    await asyncio.to_thread(os.remove, file_path)
    LOGGER.log(VERBOSE_LOG_LEVEL, "Removed file: %s", file_path)


def get_primary_ip_address_from_zeroconf(
    discovery_info: AsyncServiceInfo,
    prefer_ipv6: bool = False,
) -> str | None:
    """
    Get primary IP address from zeroconf discovery info.

    :param discovery_info: The zeroconf service info to extract the address from.
    :param prefer_ipv6: If True, prefer IPv6 addresses over IPv4.
    """
    if prefer_ipv6:
        order = [IPVersion.V6Only, IPVersion.V4Only]
    else:
        order = [IPVersion.V4Only, IPVersion.V6Only]
    for version in order:
        for addr in discovery_info.ip_addresses_by_version(version):
            if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
                continue
            return str(addr)
    return None


def get_port_from_zeroconf(discovery_info: AsyncServiceInfo) -> int | None:
    """Get port from zeroconf discovery info."""
    return discovery_info.port


def get_zeroconf_args(
    use_all_interfaces: bool = False,
) -> dict[str, Any]:
    """
    Determine optimal zeroconf IPVersion and interfaces from system adapters.

    Inspects available network adapters to determine the correct IP version
    and interface configuration, similar to Home Assistant's approach.

    :param use_all_interfaces: If True, use all interfaces (user override).
    """
    adapters = ifaddr.get_adapters()
    has_ipv4 = False
    has_ipv6 = False
    interface_ips: list[str] = []
    for adapter in adapters:
        for ip_config in adapter.ips:
            if ip_config.is_IPv6:
                ip_tuple = cast("tuple[str, int, int]", ip_config.ip)
                addr = ip_address(ip_tuple[0])
                if (
                    isinstance(addr, IPv6Address)
                    and not addr.is_loopback
                    and not addr.is_link_local
                ):
                    has_ipv6 = True
                    if not addr.is_global:
                        interface_ips.append(f"{ip_tuple[0]}%{ip_tuple[2]}")
            else:
                ip_str = cast("str", ip_config.ip)
                addr = ip_address(ip_str)
                if isinstance(addr, IPv4Address) and not addr.is_loopback:
                    has_ipv4 = True
                    interface_ips.append(ip_str)

    # Determine IP version based on available addresses.
    # On macOS/FreeBSD, zeroconf's IPVersion.All creates an AF_INET6 listen socket
    # that cannot join IPv4 multicast groups, silently breaking discovery of
    # IPv4-only devices. Fall back to V4Only on those platforms.
    has_functional_dual_stack = not sys.platform.startswith(("freebsd", "darwin"))
    if has_ipv4 and has_ipv6 and has_functional_dual_stack:
        ip_version = IPVersion.All
    elif has_ipv4:
        ip_version = IPVersion.V4Only
    elif has_ipv6:
        ip_version = IPVersion.V6Only
    else:
        ip_version = IPVersion.V4Only

    if use_all_interfaces:
        # User explicitly requested all interfaces — pass explicit IP list
        # to avoid issues with InterfaceChoice.Default on multi-interface hosts.
        if interface_ips:
            return {"ip_version": ip_version, "interfaces": interface_ips}
        return {"ip_version": ip_version, "interfaces": InterfaceChoice.All}

    # Default mode: use InterfaceChoice.Default for IPv4-only single-interface,
    # otherwise pass explicit interface list for reliability.
    if ip_version == IPVersion.V4Only:
        return {"ip_version": ip_version, "interfaces": InterfaceChoice.Default}
    if interface_ips:
        return {"ip_version": ip_version, "interfaces": interface_ips}
    return {"ip_version": ip_version, "interfaces": InterfaceChoice.All}


async def close_async_generator(agen: AsyncGenerator[Any]) -> None:
    """Force close an async generator."""
    task = asyncio.create_task(agen.__anext__())
    task.cancel()
    with suppress(asyncio.CancelledError, StopAsyncIteration):
        await task
    await agen.aclose()


async def detect_charset(data: bytes, fallback: str = "utf-8", preferred: str | None = None) -> str:
    """
    Detect the charset to decode the given raw text with.

    :param data: The raw text bytes to inspect.
    :param fallback: Charset to return when the charset can not be determined.
    :param preferred: Charset declared by the source, taken over detection when usable.
    """
    # a BOM outranks the declared charset: it names the very same UTF-8 but, unlike
    # the declared name, also gets the marker itself stripped off the decoded text
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"

    if preferred:
        # a declared charset is only worth anything if Python can actually decode text with
        # it: servers do send misspelled or plain made-up names in their Content-Type, and a
        # handful of names that do resolve to a codec still cannot decode text (base64, idna)
        try:
            data[:16].decode(preferred, errors="replace")
        except (LookupError, ValueError) as err:
            LOGGER.debug("Ignoring unusable charset %s: %s", preferred, err)
        else:
            return preferred

    try:
        data.decode()
    except UnicodeDecodeError:
        pass
    else:
        # valid UTF-8 is never a legacy charset by accident, so skip detection
        return "utf-8"

    # imported here to keep the detector out of the idle import footprint:
    # it is only needed for the rare text that is not UTF-8
    import chardet  # noqa: PLC0415
    from chardet.enums import EncodingEra  # noqa: PLC0415

    # the reported confidence is deliberately not gated on: CUE sheets and playlists
    # are nearly all ASCII keywords, which holds the score far below any usable
    # threshold even though the charset itself is named correctly (support #6093).
    # With no score to weigh them against, DOS and mainframe codepages are dropped from
    # the candidates so a stray weak match cannot outrank the Windows codepage these
    # files are really written in. Only a superset is guaranteed to decode the bytes
    # past the window the detector samples, so it wins ties over its subsets.
    try:
        detected = await asyncio.to_thread(
            chardet.detect,
            data,
            encoding_era=EncodingEra.ALL & ~(EncodingEra.DOS | EncodingEra.MAINFRAME),
            prefer_superset=True,
            no_match_encoding=fallback,
        )
    except Exception as err:
        LOGGER.debug("Failed to detect charset: %s", err)
        return fallback
    if not (encoding := detected["encoding"]):
        return fallback
    LOGGER.debug("Detected charset %s (confidence %.2f)", encoding, detected["confidence"])
    return encoding


def parse_optional_bool(value: Any) -> bool | None:
    """Parse an optional boolean value from various input types."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value_lower = value.strip().lower()
        if value_lower in ("true", "1", "yes", "on"):
            return True
        if value_lower in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def merge_dict(
    base_dict: dict[Any, Any],
    new_dict: dict[Any, Any],
    allow_overwite: bool = False,
) -> dict[Any, Any]:
    """Merge dict without overwriting existing values."""
    final_dict = base_dict.copy()
    for key, value in new_dict.items():
        if final_dict.get(key) and isinstance(value, dict):
            final_dict[key] = merge_dict(final_dict[key], value)
        if final_dict.get(key) and isinstance(value, tuple):
            final_dict[key] = merge_tuples(final_dict[key], value)
        if final_dict.get(key) and isinstance(value, list):
            final_dict[key] = merge_lists(final_dict[key], value)
        elif not final_dict.get(key) or allow_overwite:
            final_dict[key] = value
    return final_dict


def merge_tuples(base: tuple[Any, ...], new: tuple[Any, ...]) -> tuple[Any, ...]:
    """Merge 2 tuples."""
    return tuple(x for x in base if x not in new) + tuple(new)


def merge_lists(base: list[Any], new: list[Any]) -> list[Any]:
    """Merge 2 lists."""
    return [x for x in base if x not in new] + list(new)


def percentage(part: float, whole: float) -> int:
    """Calculate percentage."""
    return int(100 * float(part) / float(whole))


def validate_announcement_chime_url(url: str) -> bool:
    """Validate announcement chime URL format."""
    if not url or not url.strip():
        return True  # Empty URL is valid

    if url == ANNOUNCE_ALERT_FILE:
        return True  # Built-in chime file is valid

    try:
        parsed = urlparse(url.strip())

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.netloc:
            return False

        path_lower = parsed.path.lower()
        audio_extensions = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac")

        return any(path_lower.endswith(ext) for ext in audio_extensions)

    except Exception:
        return False


async def get_mac_address(ip_address: str) -> str | None:
    """Get MAC address for given IP address via ARP lookup."""
    try:
        from getmac import get_mac_address as getmac_lookup  # noqa: PLC0415

        return await asyncio.to_thread(getmac_lookup, ip=ip_address)
    except ImportError:
        LOGGER.debug("getmac module not available, cannot resolve MAC from IP")
        return None
    except Exception as err:
        LOGGER.debug("Failed to resolve MAC address for %s: %s", ip_address, err)
        return None


def is_locally_administered_mac(mac_address: str) -> bool:
    """
    Check if a MAC address is locally administered (virtual/randomized).

    Locally administered addresses have bit 1 of the first octet set to 1.
    These are often used by devices for virtual interfaces or protocol-specific
    addresses (e.g., AirPlay, DLNA may use different virtual MACs than the real hardware MAC).

    :param mac_address: MAC address in any common format (with :, -, or no separator).
    :return: True if locally administered, False if globally unique (real hardware MAC).
    """
    # Normalize MAC address
    mac_clean = mac_address.upper().replace(":", "").replace("-", "")
    if len(mac_clean) < 2:
        return False

    # Get first octet and check bit 1 (second bit from right)
    try:
        first_octet = int(mac_clean[:2], 16)
        return bool(first_octet & 0x02)
    except ValueError:
        return False


def normalize_mac_for_matching(mac_address: str) -> str:
    """
    Normalize a MAC address for device matching by masking out the locally-administered bit.

    Some protocols (like AirPlay) report a locally-administered MAC address variant where
    bit 1 of the first octet is set. For example:
    - Real hardware MAC: 54:78:C9:E6:0D:A0 (first byte 0x54 = 01010100)
    - AirPlay reports:   56:78:C9:E6:0D:A0 (first byte 0x56 = 01010110)

    These represent the same device but differ only in the locally-administered bit.
    This function normalizes the MAC by clearing bit 1 of the first octet, allowing
    both variants to match the same device.

    :param mac_address: MAC address in any common format (with :, -, or no separator).
    :return: Normalized MAC address in lowercase without separators, with the
             locally-administered bit cleared.
    """
    # Normalize MAC address (remove separators, lowercase)
    mac_clean = mac_address.lower().replace(":", "").replace("-", "")
    if len(mac_clean) != 12:
        # Invalid MAC length, return as-is
        return mac_clean

    try:
        # Parse first octet and clear bit 1 (the locally-administered bit)
        first_octet = int(mac_clean[:2], 16)
        first_octet_normalized = first_octet & ~0x02  # Clear bit 1
        # Reconstruct the MAC with the normalized first octet
        return f"{first_octet_normalized:02x}{mac_clean[2:]}"
    except ValueError:
        # Invalid hex, return as-is
        return mac_clean


def is_valid_mac_address(mac_address: str | None) -> bool:
    """
    Check if a MAC address is valid and usable for device identification.

    Invalid MAC addresses include:
    - None or empty strings
    - Null MAC: 00:00:00:00:00:00
    - Broadcast MAC: ff:ff:ff:ff:ff:ff
    - Any MAC that doesn't follow the expected pattern

    :param mac_address: MAC address to validate.
    :return: True if valid and usable, False otherwise.
    """
    if not mac_address:
        return False

    # Normalize MAC address (remove separators and convert to lowercase)
    normalized = mac_address.lower().replace(":", "").replace("-", "")

    # Check for invalid/reserved MAC addresses
    if normalized in ("000000000000", "ffffffffffff"):
        return False

    # Check length and hex validity
    if len(normalized) != 12:
        return False

    try:
        int(normalized, 16)
        return True
    except ValueError:
        return False


def normalize_ip_address(ip_address: str | None) -> str | None:
    """
    Normalize IP address for comparison.

    Handles IPv6-mapped IPv4 addresses (e.g., ::ffff:192.168.1.64 -> 192.168.1.64).

    :param ip_address: IP address to normalize.
    :return: Normalized IP address or None if invalid.
    """
    if not ip_address:
        return None

    # Handle IPv6-mapped IPv4 addresses
    if ip_address.startswith("::ffff:"):
        # Extract the IPv4 part
        return ip_address[7:]

    return ip_address


async def resolve_real_mac_address(reported_mac: str | None, ip_address: str | None) -> str | None:
    """
    Resolve the real MAC address for a device.

    Some devices report different virtual MAC addresses per protocol (AirPlay, DLNA,
    Chromecast). This function tries to resolve the actual hardware MAC via ARP
    when the reported MAC appears to be locally administered (virtual).

    :param reported_mac: The MAC address reported by the protocol.
    :param ip_address: The IP address of the device (for ARP lookup).
    :return: The real MAC address if found, or None if it couldn't be resolved.
    """
    if not ip_address:
        return None

    # If no MAC reported or it's a locally administered one, try ARP lookup
    if not reported_mac or is_locally_administered_mac(reported_mac):
        real_mac = await get_mac_address(ip_address)
        if real_mac and is_valid_mac_address(real_mac):
            return real_mac.upper()

    return None


async def enrich_device_mac_address(
    device_info: DeviceInfo,
    logger: logging.Logger | None = None,
) -> None:
    """
    Enrich a player's device_info with a real MAC address via ARP.

    Called automatically during player registration. It validates the existing MAC,
    normalizes IPv6-mapped IPv4 addresses, and always performs an ARP lookup when
    an IP is available. The ARP result replaces the reported MAC because it reflects
    the true hardware address and reliably unifies protocols on the same device -
    even when different protocols report different valid MACs (e.g., Yamaha devices
    where DLNA and AirPlay MACs differ by 1 in the last octet).

    :param device_info: The player's DeviceInfo to enrich in-place.
    :param logger: Optional logger for debug messages.
    """
    identifiers = device_info.identifiers
    reported_mac = identifiers.get(IdentifierType.MAC_ADDRESS)
    ip_address = identifiers.get(IdentifierType.IP_ADDRESS)

    # Blank out invalid MAC addresses (00:00:00:00:00:00, ff:ff:ff:ff:ff:ff, etc.)
    # so they can't cause false matches in protocol linking.
    if reported_mac and not is_valid_mac_address(reported_mac):
        if logger:
            logger.debug("Removing invalid MAC address: %s", reported_mac)
        device_info.add_identifier(IdentifierType.MAC_ADDRESS, None)
        reported_mac = None

    # Normalize IP address (handle IPv6-mapped IPv4 like ::ffff:192.168.1.64)
    if ip_address:
        normalized_ip = normalize_ip_address(ip_address)
        if normalized_ip and normalized_ip != ip_address:
            device_info.add_identifier(IdentifierType.IP_ADDRESS, normalized_ip)
            if logger:
                logger.debug(
                    "Normalized IP address: %s -> %s",
                    ip_address,
                    normalized_ip,
                )
            ip_address = normalized_ip

    # Skip ARP enrichment if no IP available (can't do ARP lookup)
    if not ip_address:
        return

    # Always attempt ARP lookup when we have an IP address.
    # Some devices (e.g., Yamaha MusicCast) report different valid globally-unique
    # MACs per protocol (DLNA vs AirPlay differ by 1 in the last octet).
    # ARP resolves the true hardware MAC which reliably unifies all protocols.
    # The result is cached in player config so subsequent restarts are fast.
    real_mac = await resolve_real_mac_address(reported_mac, ip_address)
    if real_mac and real_mac.upper() != (reported_mac or "").upper():
        device_info.add_identifier(IdentifierType.MAC_ADDRESS, real_mac)
        if logger:
            logger.debug(
                "Resolved MAC via ARP: %s -> %s",
                reported_mac or "none",
                real_mac,
            )
    elif not reported_mac:
        # ARP failed and no reported MAC - nothing we can do
        if logger:
            logger.debug("ARP lookup failed for %s and no reported MAC", ip_address)


class TaskManager:
    """
    Helper class to run many tasks at once.

    This is basically an alternative to asyncio.TaskGroup but this will not
    cancel all operations when one of the tasks fails.
    Logging of exceptions is done by the mass.create_task helper.
    """

    def __init__(self, mass: MusicAssistant, limit: int = 0):
        """Initialize the TaskManager."""
        self.mass = mass
        self._tasks: list[asyncio.Task[None]] = []
        self._semaphore = asyncio.Semaphore(limit) if limit else None

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[None]:
        """Create a new task and add it to the manager."""
        task = self.mass.create_task(coro)
        self._tasks.append(task)
        return task

    async def create_task_with_limit(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Create a new task with semaphore limit."""
        assert self._semaphore is not None

        def task_done_callback(_task: asyncio.Task[None]) -> None:
            assert self._semaphore is not None  # for type checking
            self._tasks.remove(task)
            self._semaphore.release()

        await self._semaphore.acquire()
        task: asyncio.Task[None] = self.create_task(coro)
        task.add_done_callback(task_done_callback)

    async def __aenter__(self) -> Self:
        """Enter context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        if len(self._tasks) > 0:
            await asyncio.wait(self._tasks)
            self._tasks.clear()
        return None


_R = TypeVar("_R")
_P = ParamSpec("_P")


def lock[**P, R](  # type: ignore[valid-type]
    func: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """
    Call async function using a per-instance Lock.

    Each instance gets its own lock so that e.g. SyncGroupPlayer A
    does not block SyncGroupPlayer B when both call set_members().
    """
    # Per-instance lock storage (weak refs so locks are GC'd with their instance)
    instance_locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
    # Fallback lock for non-method (no self) usage
    fallback_lock: asyncio.Lock | None = None

    @functools.wraps(func)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Call async function using a per-instance Lock."""
        nonlocal fallback_lock
        instance = args[0] if args else None
        if instance is not None:
            try:
                func_lock = instance_locks.setdefault(instance, asyncio.Lock())
            except TypeError:
                # instance is not weakly referenceable, use fallback
                if fallback_lock is None:
                    fallback_lock = asyncio.Lock()
                func_lock = fallback_lock
        else:
            if fallback_lock is None:
                fallback_lock = asyncio.Lock()
            func_lock = fallback_lock
        async with func_lock:
            return await func(*args, **kwargs)

    return wrapper


class TimedAsyncGenerator:
    """
    Async iterable that times out after a given time.

    Source: https://medium.com/@dmitry8912/implementing-timeouts-in-pythons-asynchronous-generators-f7cbaa6dc1e9
    """

    def __init__(self, iterable: AsyncIterator[Any], timeout: int = 0):
        """
        Initialize the AsyncTimedIterable.

        Args:
            iterable: The async iterable to wrap.
            timeout: The timeout in seconds for each iteration.
        """

        class AsyncTimedIterator:
            def __init__(self) -> None:
                self._iterator = iterable.__aiter__()

            async def __anext__(self) -> Any:
                result = await asyncio.wait_for(self._iterator.__anext__(), int(timeout))
                if not result:
                    raise StopAsyncIteration
                return result

        self._factory = AsyncTimedIterator

    def __aiter__(self):  # type: ignore[no-untyped-def]
        """Return the async iterator."""
        return self._factory()


async def join_task[T](task: asyncio.Future[T], timeout: float | None = None) -> T:
    """
    Wait for a task started elsewhere and return its result.

    Cancelling the waiter leaves the task running, so work that is shared between callers -
    or that must outlive a caller's deadline - keeps going and still reaches every other
    waiter. A task that can lose all its waiters needs a done callback that retrieves its
    exception (as mass.create_task installs) to keep asyncio quiet about it.

    :param task: The task (or future) to wait for.
    :param timeout: Optional number of seconds to wait before giving up.
    :raises TimeoutError: If the task did not complete within the timeout.
    :raises asyncio.CancelledError: If the task itself was cancelled.
    :return: The task's result.
    """
    if not task.done():
        # awaiting the task directly would hold it as this coroutine's fut_waiter, so
        # cancelling the waiter would cancel the task itself. asyncio.shield achieves the
        # same isolation, but as of Python 3.14 a cancelled waiter makes it report the task's
        # exception through loop.call_exception_handler, even when another waiter already
        # handled it.
        await asyncio.wait((task,), timeout=timeout)
    if not task.done():
        raise TimeoutError
    return task.result()


# Bound for guard_single_request: it only needs ``.mass``, so a structural protocol
# lets it decorate providers, core controllers and media controllers alike without
# coupling to their concrete base classes.
class _SupportsMass(Protocol):
    """Structural type for objects exposing a MusicAssistant reference."""

    mass: MusicAssistant


def guard_single_request[SelfT: _SupportsMass, **P, R](
    func: Callable[Concatenate[SelfT, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[SelfT, P], Coroutine[Any, Any, R]]:
    """
    Ensure concurrent calls with identical arguments result in a single request.

    Callers arriving while an identical call is already in flight await that same call and
    receive its result. Cancelling one caller leaves both the request and the other callers
    unaffected. Calls count as identical when they are made on the same object with equal
    arguments, no matter whether those were passed positionally or by keyword; the request
    runs with the arguments of the caller that started it.

    Every argument must be a scalar or an object identified by its ``uri``, so that equal
    arguments are guaranteed to produce an equal key.

    :param func: The coroutine method to guard.
    """
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(self: SelfT, *args: P.args, **kwargs: P.kwargs) -> R:
        mass = self.mass
        # create a task_id dynamically based on the bound method and args/kwargs.
        # the instance is part of the key because a decorated method may be inherited by
        # multiple subclasses (all media controllers share
        # MediaControllerBase.get_provider_item) and a class may have multiple instances
        # (e.g. a provider set up twice), which must never join each other's flight.
        # id(self) is stable while a flight is live because the task references self;
        # the class name only serves to keep the task_id readable while debugging.
        # binding the arguments to their parameter names and filling in the defaults keys a
        # call the same however it was spelled; repr of the resulting tuple keeps the parts
        # apart, so an id that itself contains punctuation cannot run into the next one.
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        task_id = repr(
            (
                type(self).__name__,
                id(self),
                func.__qualname__,
                # skip the instance: it is the first parameter and is keyed by id() above
                *(
                    (name, _canonical_key_part(value))
                    for name, value in islice(bound.arguments.items(), 1, None)
                ),
            )
        )
        task: asyncio.Task[R] = mass.create_task(
            func,
            self,
            *args,
            task_id=task_id,
            abort_existing=False,
            eager_start=True,
            # every caller awaits the flight below and so sees the failure itself; the
            # task's own exception log would report a handled error as an unhandled one
            log_exceptions=False,
            **kwargs,
        )
        return await join_task(task)

    return wrapper


def _canonical_key_part(value: Any) -> Any:
    """Return a stable stand-in for a single argument of a guarded request."""
    if (uri := getattr(value, "uri", None)) is not None:
        # a media item renders as a multi-kilobyte dataclass repr in which the set-typed
        # fields (provider_mappings, external_ids) can iterate in different orders for two
        # equal items. the uri identifies the item, and the type travels with it because a
        # full item and an ItemMapping for that same item are not handled the same.
        return (type(value).__name__, uri)
    return value
