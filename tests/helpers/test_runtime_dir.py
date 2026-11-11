"""Tests for the runtime_dir helper."""

from __future__ import annotations

import os
import stat
import tempfile
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
    monkeypatch.setattr(os, "getuid", lambda: 0)
    return shm


@pytest.fixture(autouse=True)
def temp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Return a stand-in for the system temp dir, so no test touches the real one."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    return temp


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    """Return an existing cache dir."""
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


@pytest.fixture
def no_shared_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make /dev/shm missing for a system user."""
    monkeypatch.setattr(runtime_dir, "SHM_PATH", str(tmp_path / "missing_shm"))
    monkeypatch.setattr(os, "getuid", lambda: 0)


def test_prefers_shared_memory(shm_path: Path, temp_path: Path, cache_path: Path) -> None:
    """The dir is created on shared memory, private to the server's user."""
    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == shm_path
    assert result.name.startswith("music-assistant-")
    assert result.is_dir()
    assert not result.is_symlink()
    assert stat.S_IMODE(result.stat().st_mode) == 0o700
    assert list(temp_path.iterdir()) == []
    assert not (cache_path / "runtime").exists()


def test_regular_user_on_a_systemd_host_uses_the_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
    shm_path: Path,
    temp_path: Path,
    cache_path: Path,
    tmp_path: Path,
) -> None:
    """A regular user's files in /dev/shm can be wiped by logind, so they go to the temp dir."""
    monkeypatch.setattr(os, "getuid", lambda: runtime_dir.FIRST_REGULAR_UID)
    monkeypatch.setattr(runtime_dir, "SYSTEMD_RUN_PATH", str(tmp_path))

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == temp_path
    assert stat.S_IMODE(result.stat().st_mode) == 0o700
    assert list(shm_path.iterdir()) == []


def test_regular_user_without_systemd_uses_shared_memory(
    monkeypatch: pytest.MonkeyPatch, shm_path: Path, cache_path: Path, tmp_path: Path
) -> None:
    """Without logind, as in a container running as a regular user, /dev/shm is safe."""
    monkeypatch.setattr(os, "getuid", lambda: runtime_dir.FIRST_REGULAR_UID)
    monkeypatch.setattr(runtime_dir, "SYSTEMD_RUN_PATH", str(tmp_path / "no_systemd"))

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == shm_path


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


@pytest.mark.usefixtures("no_shared_memory")
def test_falls_back_to_the_temp_dir_without_shared_memory(
    temp_path: Path, cache_path: Path, tmp_path: Path
) -> None:
    """Without /dev/shm the dir is created in the system temp dir."""
    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == temp_path
    assert stat.S_IMODE(result.stat().st_mode) == 0o700
    assert not (tmp_path / "missing_shm").exists()
    assert not (cache_path / "runtime").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_falls_back_to_the_temp_dir_when_shared_memory_is_read_only(
    shm_path: Path, temp_path: Path, cache_path: Path
) -> None:
    """An unwritable /dev/shm falls back to the temp dir."""
    shm_path.chmod(0o500)
    try:
        result = Path(prepare_runtime_dir(str(cache_path)))
    finally:
        shm_path.chmod(0o700)

    assert result.parent == temp_path
    assert list(shm_path.iterdir()) == []


@pytest.mark.usefixtures("shm_path")
def test_name_held_by_someone_else_is_never_used(
    temp_path: Path, cache_path: Path, tmp_path: Path
) -> None:
    """A symlink planted under the server's name is left alone and not followed."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep").touch()
    planted = Path(prepare_runtime_dir(str(cache_path)))
    planted.rmdir()
    planted.symlink_to(target)

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == temp_path
    assert planted.is_symlink()
    assert (target / "keep").exists()


@pytest.mark.usefixtures("no_shared_memory")
def test_falls_back_to_the_cache_path_as_a_last_resort(
    monkeypatch: pytest.MonkeyPatch, cache_path: Path, tmp_path: Path
) -> None:
    """Without /dev/shm and a usable temp dir the dir is created under the cache path."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "missing_temp"))

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result == cache_path / "runtime"
    assert stat.S_IMODE(result.stat().st_mode) == 0o700


def _no_usable_temp_dir() -> str:
    raise FileNotFoundError("No usable temporary directory found")


def test_no_usable_temp_dir_still_uses_shared_memory(
    monkeypatch: pytest.MonkeyPatch, shm_path: Path, cache_path: Path
) -> None:
    """A host without a usable temp dir still gets /dev/shm."""
    monkeypatch.setattr(tempfile, "gettempdir", _no_usable_temp_dir)

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result.parent == shm_path


@pytest.mark.usefixtures("no_shared_memory")
def test_no_usable_temp_dir_falls_back_to_the_cache_path(
    monkeypatch: pytest.MonkeyPatch, cache_path: Path
) -> None:
    """Without /dev/shm and without a usable temp dir the cache path is used."""
    monkeypatch.setattr(tempfile, "gettempdir", _no_usable_temp_dir)

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert result == cache_path / "runtime"


@pytest.mark.usefixtures("no_shared_memory")
def test_last_resort_leftovers_are_removed(
    monkeypatch: pytest.MonkeyPatch, cache_path: Path, tmp_path: Path
) -> None:
    """The cache path fallback is emptied on a restart too."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "missing_temp"))
    stale_dir = Path(prepare_runtime_dir(str(cache_path)))
    os.mkfifo(stale_dir / "stale_pipe")

    result = Path(prepare_runtime_dir(str(cache_path)))

    assert list(result.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.usefixtures("no_shared_memory")
def test_last_resort_does_not_fail_on_a_leftover_it_cannot_remove(
    monkeypatch: pytest.MonkeyPatch, cache_path: Path, tmp_path: Path
) -> None:
    """A leftover the server cannot remove from its own cache dir does not stop a start."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "missing_temp"))
    locked = cache_path / "runtime" / "locked"
    locked.mkdir(parents=True)
    (locked / "held").touch()
    (cache_path / "runtime").chmod(0o755)
    locked.chmod(0o500)
    try:
        result = Path(prepare_runtime_dir(str(cache_path)))
    finally:
        locked.chmod(0o700)

    assert result == cache_path / "runtime"
    assert stat.S_IMODE(result.stat().st_mode) == 0o700
