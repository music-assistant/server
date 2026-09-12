"""Tests for the standalone development setup script."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if not (PROJECT_ROOT / "scripts" / "setup.sh").is_file():
    pytest.skip("standalone setup script is not synced upstream", allow_module_level=True)


def _prepare_fake_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "setup.sh", scripts / "setup.sh")
    (repo / "provider").mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$1" = venv ]; then\n  mkdir -p "$2/bin"\n  : > "$2/bin/activate"\nfi\n',
        encoding="utf-8",
    )
    git = fake_bin / "git"
    git.write_text(
        "#!/bin/sh\n"
        "for destination do :; done\n"
        'mkdir -p "$destination/.git" "$destination/music_assistant/providers"\n'
        ': > "$destination/requirements_all.txt"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    git.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"}
    return repo, env


def test_setup_replaces_empty_invalid_checkout(tmp_path: Path) -> None:
    """An empty ma-server directory is safely replaced by a clone."""
    repo, env = _prepare_fake_repo(tmp_path)
    (repo / "ma-server").mkdir()

    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(repo / "scripts" / "setup.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "ma-server" / ".git").is_dir()
    assert (repo / "ma-server" / "music_assistant" / "providers" / "yandex_station").is_symlink()


def test_setup_preserves_nonempty_invalid_checkout(tmp_path: Path) -> None:
    """A non-empty invalid ma-server directory is rejected without data loss."""
    repo, env = _prepare_fake_repo(tmp_path)
    checkout = repo / "ma-server"
    checkout.mkdir()
    marker = checkout / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(repo / "scripts" / "setup.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "No files were removed" in result.stdout
    assert marker.read_text(encoding="utf-8") == "user data"
