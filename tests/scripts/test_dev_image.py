"""Tests for the development image: its repository references and its check."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
REPO_REF_SCRIPT = ROOT / "scripts" / "dev-image" / "repo_ref.sh"
DEV_IMAGE_WORKFLOW = ROOT / ".github" / "workflows" / "dev-image.yml"

# answers the two lookups of repo_ref.sh and logs every call
STUB_GIT = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_GIT_LOG"
case "$*" in
    *--symref*)
        if [ -n "$STUB_HEAD" ]; then
            printf 'ref: refs/heads/%s\\tHEAD\\n0123abcd\\tHEAD\\n' "$STUB_HEAD"
        fi
        ;;
    *--exit-code*)
        exit "$STUB_PROBE_RC"
        ;;
esac
"""
PROVIDER_FORMS = (
    "owner/repo          the default branch of the repository",
    "owner/repo@ref      a branch, a tag or a commit",
    "owner/repo@pr-12    pull request 12 of the repository",
)


class RepoRefRun:
    """The outcome of one function of repo_ref.sh, run by the system shell."""

    def __init__(self, result: subprocess.CompletedProcess[str], git_calls: list[str]) -> None:
        """
        Store the outcome.

        :param result: The finished shell process.
        :param git_calls: The arguments of every git call, one string per call.
        """
        self.returncode = result.returncode
        self.stdout = result.stdout.strip()
        self.stderr = result.stderr
        self.git_calls = git_calls


@pytest.fixture(name="repo_ref")
def repo_ref_fixture(tmp_path: Path) -> Any:
    """Return a runner for a function of repo_ref.sh with a stub git on the PATH."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "git"
    stub.write_text(STUB_GIT, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "git.log"

    def run(*args: str, probe_rc: int = 2, head: str = "") -> RepoRefRun:
        log.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_GIT_LOG": str(log),
            "STUB_PROBE_RC": str(probe_rc),
            "STUB_HEAD": head,
        }
        result = subprocess.run(  # noqa: S603
            ["sh", "-c", '. "$0"; "$@"', str(REPO_REF_SCRIPT), *args],  # noqa: S607
            capture_output=True,
            check=False,
            env=env,
            text=True,
        )
        return RepoRefRun(result, log.read_text(encoding="utf-8").splitlines())

    return run


@pytest.mark.parametrize(
    ("value", "default_repo", "expected"),
    [
        ("pr-123", "server", "music-assistant/server@refs/pull/123/head"),
        ("pr-45", "frontend", "music-assistant/frontend@refs/pull/45/head"),
        ("dev", "server", "music-assistant/server@dev"),
        (
            "0123456789abcdef0123456789abcdef01234567",
            "server",
            "music-assistant/server@0123456789abcdef0123456789abcdef01234567",
        ),
        ("someone/server@my-branch", "server", "someone/server@my-branch"),
        ("someone/frontend@feature/x", "frontend", "someone/frontend@feature/x"),
    ],
)
def test_repo_ref_without_lookup(
    repo_ref: Any, value: str, default_repo: str, expected: str
) -> None:
    """A PR, a branch, a commit and owner/repo@ref need no lookup."""
    run = repo_ref("parse_repo_ref", value, "music-assistant", default_repo)

    assert run.returncode == 0, run.stderr
    assert run.stdout == expected
    assert run.git_calls == []


def test_repo_ref_branch_with_slash(repo_ref: Any) -> None:
    """A name with a slash that the default repository has is a branch of it."""
    run = repo_ref("parse_repo_ref", "user/my-feature", "music-assistant", "server", probe_rc=0)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "music-assistant/server@user/my-feature"
    assert run.git_calls == [
        "ls-remote --exit-code https://github.com/music-assistant/server "
        "refs/heads/user/my-feature refs/tags/user/my-feature"
    ]


def test_repo_ref_fork(repo_ref: Any) -> None:
    """A name with a slash that the default repository lacks is a fork, on its default branch."""
    run = repo_ref(
        "parse_repo_ref", "someone/server", "music-assistant", "server", probe_rc=2, head="develop"
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "someone/server@develop"
    assert run.git_calls[-1] == "ls-remote --symref https://github.com/someone/server HEAD"


def test_repo_ref_fork_without_default_branch(repo_ref: Any) -> None:
    """A fork whose default branch cannot be looked up is fetched from main."""
    run = repo_ref("parse_repo_ref", "someone/server", "music-assistant", "server", probe_rc=2)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "someone/server@main"


def test_repo_ref_failed_lookup(repo_ref: Any) -> None:
    """A lookup that fails stops with a readable error instead of a guess."""
    run = repo_ref("parse_repo_ref", "someone/server", "music-assistant", "server", probe_rc=128)

    assert run.returncode == 1
    assert run.stdout == ""
    assert "ERROR: could not reach GitHub to look up 'someone/server'" in run.stderr


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("owner/repo@v1.2.0", "owner/repo@v1.2.0"),
        ("my-org/my_provider.repo@feature/x", "my-org/my_provider.repo@feature/x"),
        (
            "owner/repo@0123456789abcdef0123456789abcdef01234567",
            "owner/repo@0123456789abcdef0123456789abcdef01234567",
        ),
        ("owner/repo@pr-12", "owner/repo@refs/pull/12/head"),
        ("owner/repo@pr-12a", "owner/repo@pr-12a"),
    ],
)
def test_provider_ref_with_ref(repo_ref: Any, value: str, expected: str) -> None:
    """A provider reference with a ref names that branch, tag, commit or pull request."""
    run = repo_ref("parse_provider_ref", value)

    assert run.returncode == 0, run.stderr
    assert run.stdout == expected
    assert run.git_calls == []


def test_provider_ref_default_branch(repo_ref: Any) -> None:
    """A provider reference without a ref names the default branch of that repository."""
    run = repo_ref("parse_provider_ref", "owner/repo", head="trunk")

    assert run.returncode == 0, run.stderr
    assert run.stdout == "owner/repo@trunk"
    assert run.git_calls == ["ls-remote --symref https://github.com/owner/repo HEAD"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "pr-12",
        "my-branch",
        "owner/",
        "/repo",
        "owner/repo/extra",
        "owner/repo@",
        "owner/repo@--upload-pack=touch /tmp/x",
        "own er/repo",
        "owner/re$po",
        "owner/repo\nother/repo",
        "https://github.com/owner/repo",
    ],
)
def test_provider_ref_refused(repo_ref: Any, value: str) -> None:
    """Anything but the three forms is refused with a message that shows them."""
    run = repo_ref("parse_provider_ref", value)

    assert run.returncode == 1
    assert run.stdout == ""
    assert f"ERROR: '{value}' does not name a provider repository. Use one of:" in run.stderr
    for form in PROVIDER_FORMS:
        assert form in run.stderr
    assert run.git_calls == []


def test_dev_image_builds_on_the_base_image_argument() -> None:
    """The image builds on the base image the release passes in, by default the nightly."""
    dockerfile = (ROOT / "Dockerfile.dev").read_text(encoding="utf-8")

    assert "ARG BASE_IMAGE=ghcr.io/music-assistant/server:nightly" in dockerfile
    assert re.findall(r"^FROM .*$", dockerfile, re.MULTILINE) == ["FROM $BASE_IMAGE"]


def test_dev_image_check_needs_no_write_access() -> None:
    """The check of the image on pull requests reads the repository and nothing more."""
    workflow = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        assert job["timeout-minutes"] > 0
    triggers = workflow.get("on", workflow.get(True))
    for event in ("pull_request", "push"):
        assert set(triggers[event]["paths"]) == {
            "Dockerfile.dev",
            "scripts/dev-image/**",
            ".github/workflows/dev-image.yml",
        }
