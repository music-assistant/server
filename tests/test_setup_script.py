import os
import stat
import subprocess
from pathlib import Path
import sys
import textwrap

REPO_ROOT = Path(__file__).resolve().parents[1]

def _write_exe(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)

def _make_python_shim(dir_: Path, name: str, major: int, minor: int) -> None:
    """
    Create a fake python3.X that supports:
      -V -> prints "Python M.m.0"
      -c '<any>' -> prints "M.m"
    This is enough for setup.sh's version checks.
    """
    shim = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [[ "$1" == "-V" ]]; then
          echo "Python {major}.{minor}.0"
          exit 0
        fi
        if [[ "$1" == "-c" ]]; then
          # setup.sh uses this to read major.minor
          echo "{major}.{minor}"
          exit 0
        fi
        echo "Unsupported shim invocation: $@" >&2
        exit 1
    """)
    _write_exe(dir_ / name, shim)

def _make_repo(tmp_path: Path, requires: str) -> Path:
    """Minimal working copy: setup.sh + pyproject.toml."""
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

def _run_setup(repo_dir: Path, env=None, expect_ok=True):
    result = subprocess.run(
        ["bash", "scripts/setup.sh"],
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

def test_fails_when_no_interpreter_satisfies_spec(tmp_path: Path):
    repo = _make_repo(tmp_path, requires=">=3.99")  # impossible locally
    shims = tmp_path / "shims"; shims.mkdir()
    _make_python_shim(shims, "python3.12", 3, 12)   # still < 3.99
    env = os.environ.copy()
    env["PATH"] = f"{shims}:{env['PATH']}"
    env["SETUP_DRY_RUN"] = "1"
    result = _run_setup(repo, env=env, expect_ok=False)
    msg = result.stdout + result.stderr
    assert "does not satisfy requires-python: >=3.99" in msg

def test_picks_newest_matching_interpreter(tmp_path: Path):
    repo = _make_repo(tmp_path, requires=">=3.12")
    shims = tmp_path / "shims"; shims.mkdir()
    _make_python_shim(shims, "python3.10", 3, 10)
    _make_python_shim(shims, "python3.12", 3, 12)
    env = os.environ.copy()
    env["PATH"] = f"{shims}:{env['PATH']}"
    env["SETUP_DRY_RUN"] = "1"  # stop before venv creation
    result = _run_setup(repo, env=env)
    out = result.stdout + result.stderr
    assert "Selected interpreter: python3.12 (Python 3.12.0)" in out
    assert "[DRY-RUN] Would create venv with: python3.12" in out

def test_existing_venv_is_not_overwritten_and_is_validated(tmp_path: Path):
    repo = _make_repo(tmp_path, requires=">=3.12")
    vbin = repo / ".venv" / "bin"
    vbin.mkdir(parents=True)
    # minimal activate that works in tests
    (vbin / "activate").write_text(
        'export VIRTUAL_ENV="$(pwd)/.venv"\nexport PATH="$VIRTUAL_ENV/bin:$PATH"\n',
        encoding="utf-8",
    )
    _make_python_shim(vbin, "python", 3, 12)
    env = os.environ.copy()
    env["SETUP_SKIP_INSTALL"] = "1"  # validate then stop; no installs
    result = _run_setup(repo, env=env)
    out = result.stdout + result.stderr
    assert "Virtual environment '.venv' detected — not modifying it." in out
    assert "Using venv Python: Python 3.12.0" in out
    assert "[SKIP-INSTALL] venv validated; stopping before installs." in out

def test_existing_venv_too_old_exits_cleanly(tmp_path: Path):
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
    msg = result.stdout + result.stderr
    assert "does not satisfy requires-python: >=3.12" in msg
    assert "Creating virtual environment" not in msg  # proves no overwrite
