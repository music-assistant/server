"""Helpers to read the process's memory footprint the way diagnostics need it."""

from __future__ import annotations

from pathlib import Path

from music_assistant.helpers.util import get_self_cgroup_path

# /proc/self/status rows that split resident memory, mapped to report keys
_PROC_STATUS_RSS_KEYS = {
    "RssAnon": "rss_anon_mb",
    "RssFile": "rss_file_mb",
    "RssShmem": "rss_shmem_mb",
}
_CGROUP_KEYS = ("cgroup_usage_mb", "cgroup_anon_mb", "cgroup_file_mb", "cgroup_reported_mb")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = "/proc/self/cgroup"


def collect_resident_memory_split() -> dict[str, float | None]:
    """
    Return the process's resident memory split into anonymous, file-backed and shared MB.

    Anonymous memory is heap, stacks and private mappings; file-backed memory is resident
    pages of mapped files such as database pages and model weights, which the kernel can
    reclaim. Values are None where /proc is absent.
    """
    try:
        return parse_proc_status_rss(Path("/proc/self/status").read_text(encoding="utf-8"))
    except OSError, ValueError, IndexError:
        return dict.fromkeys(_PROC_STATUS_RSS_KEYS.values())


def parse_proc_status_rss(text: str) -> dict[str, float | None]:
    """
    Extract the resident memory split from the contents of /proc/<pid>/status.

    :param text: The file contents, one ``Key: value kB`` row per line.
    """
    result: dict[str, float | None] = dict.fromkeys(_PROC_STATUS_RSS_KEYS.values())
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key in _PROC_STATUS_RSS_KEYS:
            result[_PROC_STATUS_RSS_KEYS[key]] = round(int(value.split()[0]) / 1024, 1)
    return result


def collect_cgroup_memory(
    cgroup_root: Path = _CGROUP_ROOT, proc_cgroup: str = _PROC_SELF_CGROUP
) -> dict[str, float | None]:
    """
    Return the memory accounting of the cgroup the process runs in, in MB.

    ``cgroup_reported_mb`` is usage minus inactive file cache, which is the figure the Home
    Assistant Supervisor shows as add-on memory. Values are None when no cgroup memory files
    are visible to the process, for example outside a container.

    :param cgroup_root: Mount point of the cgroup filesystem (overridable for tests).
    :param proc_cgroup: Path to the process cgroup file (overridable for tests).
    """
    try:
        for base, controller, usage_file in (
            (cgroup_root, None, "memory.current"),
            (cgroup_root / "memory", "memory", "memory.usage_in_bytes"),
        ):
            rel = get_self_cgroup_path(proc_cgroup, controller=controller)
            directory = _nearest_cgroup_dir(base, rel, usage_file)
            if directory is not None:
                stat_text = (directory / "memory.stat").read_text(encoding="utf-8")
                usage = int((directory / usage_file).read_text(encoding="utf-8"))
                return parse_cgroup_memory(stat_text, usage)
    except OSError, ValueError:
        pass
    return dict.fromkeys(_CGROUP_KEYS)


def parse_cgroup_memory(stat_text: str, usage_bytes: int) -> dict[str, float | None]:
    """
    Derive the cgroup memory view from a memory.stat file and the cgroup's usage.

    Handles both cgroup v2 (``anon``/``file``) and v1 (``rss``/``cache``) key names.

    :param stat_text: Contents of memory.stat, one ``key value`` row per line.
    :param usage_bytes: The cgroup's current usage in bytes.
    """
    stats: dict[str, int] = {}
    for line in stat_text.splitlines():
        key, sep, value = line.partition(" ")
        if sep and value.strip().isdigit():
            stats[key] = int(value)

    def _first(*keys: str) -> int | None:
        return next((stats[key] for key in keys if key in stats), None)

    anon = _first("anon", "total_rss", "rss")
    file_cache = _first("file", "total_cache", "cache")
    inactive_file = _first("total_inactive_file", "inactive_file")
    return {
        "cgroup_usage_mb": round(usage_bytes / 1024**2, 1),
        "cgroup_anon_mb": None if anon is None else round(anon / 1024**2, 1),
        "cgroup_file_mb": None if file_cache is None else round(file_cache / 1024**2, 1),
        "cgroup_reported_mb": (
            None if inactive_file is None else round((usage_bytes - inactive_file) / 1024**2, 1)
        ),
    }


def _nearest_cgroup_dir(base: Path, rel: str | None, usage_file: str) -> Path | None:
    """
    Return the deepest directory from the process's cgroup up to the mount root with memory files.

    Inside a container the process's path from /proc/self/cgroup often does not exist under
    the mount, because the container's cgroup is mounted at the root itself.
    """
    parts = [part for part in (rel or "").split("/") if part]
    while True:
        directory = base.joinpath(*parts)
        if (directory / usage_file).is_file() and (directory / "memory.stat").is_file():
            return directory
        if not parts:
            return None
        parts.pop()
