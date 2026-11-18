"""Tests for the memory footprint helpers."""

from __future__ import annotations

from pathlib import Path

from music_assistant.helpers.memory import (
    collect_cgroup_memory,
    parse_cgroup_memory,
    parse_proc_status_rss,
)


def test_parse_proc_status_rss() -> None:
    """Test that the resident memory split is read from /proc status text in MB."""
    text = "Name:\tpython\nVmRSS:\t 1900544 kB\nRssAnon:\t 1228800 kB\nRssFile:\t  665600 kB\nRssShmem:\t    6144 kB\n"
    assert parse_proc_status_rss(text) == {
        "rss_anon_mb": 1200.0,
        "rss_file_mb": 650.0,
        "rss_shmem_mb": 6.0,
    }
    assert parse_proc_status_rss("Name:\tpython\n") == {
        "rss_anon_mb": None,
        "rss_file_mb": None,
        "rss_shmem_mb": None,
    }


def test_parse_cgroup_memory() -> None:
    """Test the cgroup memory view for both the v2 and the v1 stat file layout."""
    mib = 1024**2
    v2 = (
        f"anon {1200 * mib}\nfile {900 * mib}\nfile_mapped {500 * mib}\ninactive_file {300 * mib}\n"
    )
    assert parse_cgroup_memory(v2, 2200 * mib) == {
        "cgroup_usage_mb": 2200.0,
        "cgroup_anon_mb": 1200.0,
        "cgroup_file_mb": 900.0,
        "cgroup_reported_mb": 1900.0,
    }
    v1 = (
        f"cache {900 * mib}\nrss {1200 * mib}\ninactive_file {300 * mib}\n"
        f"total_cache {950 * mib}\ntotal_rss {1250 * mib}\ntotal_inactive_file {320 * mib}\n"
    )
    assert parse_cgroup_memory(v1, 2300 * mib) == {
        "cgroup_usage_mb": 2300.0,
        "cgroup_anon_mb": 1250.0,
        "cgroup_file_mb": 950.0,
        "cgroup_reported_mb": 1980.0,
    }
    assert parse_cgroup_memory("", 10 * mib)["cgroup_reported_mb"] is None


def test_collect_cgroup_memory_uses_own_cgroup(tmp_path: Path) -> None:
    """Test that the process's own cgroup is read, falling back to the nearest parent."""
    mib = 1024**2

    def _write(directory: Path, anon: int, usage: int) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "memory.stat").write_text(f"anon {anon * mib}\nfile 0\ninactive_file 0\n")
        (directory / "memory.current").write_text(str(usage * mib))

    root = tmp_path / "cgroup"
    _write(root, anon=9000, usage=9500)
    _write(root / "docker" / "abc", anon=1200, usage=1300)
    proc_cgroup = tmp_path / "proc_cgroup"

    proc_cgroup.write_text("0::/docker/abc\n")
    assert collect_cgroup_memory(root, str(proc_cgroup))["cgroup_anon_mb"] == 1200.0
    # a path that is not mounted inside the container resolves to the nearest parent
    proc_cgroup.write_text("0::/docker/missing\n")
    assert collect_cgroup_memory(root, str(proc_cgroup))["cgroup_anon_mb"] == 9000.0
    # no cgroup files at all
    assert collect_cgroup_memory(tmp_path / "nowhere", str(proc_cgroup)) == {
        "cgroup_usage_mb": None,
        "cgroup_anon_mb": None,
        "cgroup_file_mb": None,
        "cgroup_reported_mb": None,
    }
