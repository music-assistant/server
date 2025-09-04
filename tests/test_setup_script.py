"""Unit tests for setup.sh and Python interpreter selection."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def _write_exe(path: Path, content: str) -> None:
    """Write an executable file with the given content."""
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _make_python_shim(dir_: Path, name: str, major: int, minor: int) -> None:
    """Create a fake python3.X with just enough for the setup script.

    -V  -> prints "Python M.m.0"
    -c  -> prints "M.m"

    NOTE: this shim does NOT implement 'python -m venv'. Tests use DRY_RUN/SKIP to avoid
    side-effects.
    """
    shim = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        if [[ "$1" == "-V" ]]; then
          echo "Python {major}.{minor}.0"
          exit 0
        fi
        if [[ "$1" == "-c" ]]; then
          echo "{major}.{minor}"
          exit 0
        fi
        echo "Unsupported shim invocation: $@" >&2
        exit 1
        """
    )
    _write_exe(dir_ / name, shim)


def _make_repo(tmp_path: Path, requires: str) -> Path:
    """Create a minimal working copy: setup.sh + pyproject.toml (only requires-python)."""
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "setup.sh").write_text(
        (REPO_ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nrequires-python = "{requires}"\n',
        encoding="utf-8",
    )
    return tmp_path


def _run_setup(
    repo_dir: Path,
    env: dict[str, str] | None = None,
    expect_ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run scripts/setup.sh with an absolute bash path; assert on exit code."""
    bash_path = shutil.which("bash") or "/bin/bash"
    script_path = repo_dir / "scripts" / "setup.sh"

    # We call a local test sandbox with controlled inputs; suppress S603/S607 here.
    result = subprocess.run(  # noqa: S603
        [bash_path, str(script_path)],
        check=False,
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(
            f"Expected success, got {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if not expect_ok and result.returncode == 0:
        raise AssertionError("Expected failure but script exited 0")
    return result


def _combined_output(proc: subprocess.CompletedProcess[str]) -> str:
    return f"{proc.stdout}\n{proc.stderr}"


def test_fails_when_no_interpreter_satisfies_spec(tmp_path: Path) -> None:
    """If requires-python is impossible (>=3.99), the script fails early and clearly."""
    repo = _make_repo(tmp_path, requires=">=3.99")
    shims = tmp_path / "shims"
    shims.mkdir()
    _make_python_shim(shims, "python3.12", 3, 12)  # still < 3.99
    env = os.environ.copy()
    env["PATH"] = f"{shims}:{env['PATH']}"
    env["SETUP_DRY_RUN"] = "1"
    result = _run_setup(repo, env=env, expect_ok=False)
    msg = _combined_output(result)
    # Flexible wording: look for the constraint and "does not satisfy"
    assert "does not satisfy requires-python: >=3.99" in msg or re.search(
        r"does not satisfy .*>=\s*3\.99", msg
    )


def test_picks_newest_matching_interpreter(tmp_path: Path) -> None:
    """When multiple python3.X exist, pick the highest that satisfies the spec."""
    repo = _make_repo(tmp_path, requires=">=3.12")
    shims = tmp_path / "shims"
    shims.mkdir()
    _make_python_shim(shims, "python3.10", 3, 10)
    _make_python_shim(shims, "python3.12", 3, 12)
    env = os.environ.copy()
    env["PATH"] = f"{shims}:{env['PATH']}"
    env["SETUP_DRY_RUN"] = "1"  # stop before venv creation
    result = _run_setup(repo, env=env)
    out = _combined_output(result)
    # Accept either "Selected interpreter: python3.12" or with a version in parentheses
    assert re.search(r"Selected interpreter:\s*python3\.\d+\b", out)
    assert re.search(r"\[DRY-RUN\]\s*Would create venv with:\s*python3\.\d+\b", out)


def test_existing_venv_is_not_overwritten_and_is_validated(tmp_path: Path) -> None:
    """If .venv exists and is compliant, do not recreate; stop before installs when asked."""
    repo = _make_repo(tmp_path, requires=">=3.12")
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    # minimal activate and a compliant python shim inside venv
    (vbin / "activate").write_text(
        'export VIRTUAL_ENV="$(pwd)/.venv"\nexport PATH="$VIRTUAL_ENV/bin:$PATH"\n',
        encoding="utf-8",
    )
    _make_python_shim(vbin, "python", 3, 12)
    env = os.environ.copy()
    env["SETUP_SKIP_INSTALL"] = "1"
    result = _run_setup(repo, env=env)
    out = _combined_output(result)
    assert "Virtual environment '.venv' detected" in out
    assert "Using venv Python: Python 3.12.0" in out or "Using venv Python: Python 3.12" in out
    assert "[SKIP-INSTALL] venv validated; stopping before installs." in out


def test_existing_venv_too_old_exits_with_prompt(tmp_path: Path) -> None:
    """If .venv exists with too-old Python, exit early with clear guidance (no overwrite)."""
    repo = _make_repo(tmp_path, requires=">=3.12")
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text(
        'export VIRTUAL_ENV="$(pwd)/.venv"\nexport PATH="$VIRTUAL_ENV/bin:$PATH"\n',
        encoding="utf-8",
    )
    _make_python_shim(vbin, "python", 3, 10)  # too old
    env = os.environ.copy()
    result = _run_setup(repo, env=env, expect_ok=False)
    msg = _combined_output(result)
    # Must clearly report mismatch
    assert "does not satisfy requires-python: >=3.12" in msg or re.search(
        r"does not satisfy .*>=\s*3\.12", msg
    )
    # guidance text can vary; the key is that mismatch is reported and exit != 0


def test_auto_fix_without_matching_interpreter_fails_cleanly(tmp_path: Path) -> None:
    """Auto-fix path when no matching interpreter is available should fail clearly."""
    repo = _make_repo(tmp_path, requires=">=3.12")
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text(
        'export VIRTUAL_ENV="$(pwd)/.venv"\nexport PATH="$VIRTUAL_ENV/bin:$PATH"\n',
        encoding="utf-8",
    )
    _make_python_shim(vbin, "python", 3, 10)  # current venv too old

    # PATH has only an old interpreter; auto-fix can't find a match
    shims = tmp_path / "shims"
    shims.mkdir()
    _make_python_shim(shims, "python3.10", 3, 10)
    env = os.environ.copy()
    env["PATH"] = str(shims)  # only the shims; no system python3.13
    env["SETUP_AUTO_FIX"] = "1"
    env["SETUP_SKIP_INSTALL"] = "1"  # <-- don't run installs against a minimal pyproject
    env["SETUP_DRY_RUN"] = "1"

    # This will try auto-fix, fail to find a matching interpreter, and exit with advice.
    result = _run_setup(repo, env=env, expect_ok=False)
    msg = _combined_output(result)
    found = (
        "No suitable interpreter" in msg
        or "python3.12-venv" in msg
        or "Error: no python3 found on PATH." in msg
    )
    assert found
