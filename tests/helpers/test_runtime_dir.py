"""Tests for the runtime_dir helper."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from music_assistant.helpers import runtime_dir
from music_assistant.helpers.runtime_dir import prepare_runtime_dir


@pytest.fixture
def shm_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Return a stand-in for /dev/shm that the helper uses instead, as a system user would."""
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(runtime_dir, "SHM_PATH", str(shm))
    monkeypatch.setattr(runtime_dir.os, "getuid", lambda: 0)
    return shm


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    """Return an existing cache dir."""
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


def test_prefers_shared_memory(shm_path: Path, cache_path: Path) -> None:
    """The dir is created on shared memory, private to the server's user."""
    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == shm_path
    assert result.name.startswith("music-assistant-")
    assert result.is_dir()
    assert not result.is_symlink()
    assert stat.S_IMODE(result.stat().st_mode) == 0o700
    assert not (cache_path / "runtime").exists()


def test_regular_user_keeps_it_under_the_cache_path(
    monkeypatch: pytest.MonkeyPatch, shm_path: Path, cache_path: Path
) -> None:
    """A regular user's files in /dev/shm can be wiped by logind, so they stay on disk."""
    monkeypatch.setattr(runtime_dir.os, "getuid", lambda: runtime_dir.FIRST_REGULAR_UID)

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result == cache_path / "runtime"
    assert list(shm_path.iterdir()) == []


def test_name_is_stable_and_unique_per_cache_path(shm_path: Path, tmp_path: Path) -> None:
    """A restart reuses the server's dir, and a second server on the host gets its own."""
    first = prepare_runtime_dir(str(tmp_path / "server_a"))
    again = prepare_runtime_dir(str(tmp_path / "server_a"))
    other = prepare_runtime_dir(str(tmp_path / "server_b"))

    assert first == again
    assert other != first
    assert {Path(first).parent, Path(other).parent} == {shm_path}


@pytest.mark.usefixtures("shm_path")
def test_name_length_does_not_grow_with_the_cache_path(tmp_path: Path) -> None:
    """The name stays short, so socket paths inside fit the 108-byte unix socket limit."""
    result = Path(prepare_runtime_dir(str(tmp_path / ("deep" * 50))))

    assert len(result.name) == len("music-assistant-") + 12


@pytest.mark.usefixtures("shm_path")
def test_leftovers_from_a_previous_run_are_removed(cache_path: Path) -> None:
    """A FIFO or socket left behind by a hard shutdown does not survive a restart."""
    stale_dir = Path(prepare_runtime_dir(str(cache_path)))
    os.mkfifo(stale_dir / "stale_pipe")
    (stale_dir / "pulse_capture").mkdir()
    (stale_dir / "pulse_capture" / "native").touch()

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result == stale_dir
    assert list(result.iterdir()) == []


def test_falls_back_to_cache_path_without_shared_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_path: Path
) -> None:
    """Without /dev/shm the dir is created under the cache path."""
    monkeypatch.setattr(runtime_dir, "SHM_PATH", str(tmp_path / "missing"))

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result == cache_path / "runtime"
    assert stat.S_IMODE(result.stat().st_mode) == 0o700
    assert not (tmp_path / "missing").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_falls_back_to_cache_path_when_shared_memory_is_read_only(
    shm_path: Path, cache_path: Path
) -> None:
    """An unwritable /dev/shm falls back to the cache path."""
    shm_path.chmod(0o500)
    try:
        result = Path(prepare_runtime_dir(str(cache_path)))
    finally:
        shm_path.chmod(0o700)

    assert result == cache_path / "runtime"
    assert list(shm_path.iterdir()) == []


@pytest.mark.usefixtures("shm_path")
def test_name_held_by_someone_else_is_never_used(cache_path: Path, tmp_path: Path) -> None:
    """A symlink planted under the server's name is left alone and not followed."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep").touch()
    planted = Path(prepare_runtime_dir(str(cache_path)))
    planted.rmdir()
    planted.symlink_to(target)

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result == cache_path / "runtime"
    assert planted.is_symlink()
    assert (target / "keep").exists()


def test_fallback_leftovers_are_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_path: Path
) -> None:
    """The fallback dir is emptied on a restart too."""
    monkeypatch.setattr(runtime_dir, "SHM_PATH", str(tmp_path / "missing"))
    stale_dir = Path(prepare_runtime_dir(str(cache_path)))
    os.mkfifo(stale_dir / "stale_pipe")

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert list(result.iterdir()) == []
