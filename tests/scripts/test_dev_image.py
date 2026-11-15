"""Tests for the development image: its repository references and its publication."""

from __future__ import annotations

import json
import os
import re
import shutil
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
IMAGE_REPOSITORY = "ghcr.io/music-assistant/server"
NIGHTLY = "sha256:" + "1" * 64
NIGHTLY_AMD64 = "sha256:" + "2" * 64
DEV = "sha256:" + "3" * 64
DEV_AMD64 = "sha256:" + "4" * 64
DEV_ARM64 = "sha256:" + "5" * 64
# the workflow's steps use jq, which the runner has and the container of the tests lacks
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
# answers `docker buildx imagetools inspect <ref> --format '{{json .Manifest|.Image}}'` from a
# JSON file of {ref: {"Manifest": ..., "Image": ...}}, as the registry would
STUB_DOCKER = """#!/usr/bin/env python3
import json, os, sys
ref, fmt = sys.argv[4], sys.argv[6]
with open(os.environ["STUB_REGISTRY"], encoding="utf-8") as registry_file:
    entry = json.load(registry_file).get(ref)
if entry is None:
    sys.exit(f"ERROR: {ref}: not found")
if "error" in entry:
    sys.exit(entry["error"])
print(json.dumps(entry["Manifest" if "Manifest" in fmt else "Image"]))
"""


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
    """The image builds on the base image the publishing job passes in, by default the nightly."""
    dockerfile = (ROOT / "Dockerfile.dev").read_text(encoding="utf-8")

    assert "ARG BASE_IMAGE=ghcr.io/music-assistant/server:nightly" in dockerfile
    assert re.findall(r"^FROM .*$", dockerfile, re.MULTILINE) == ["FROM $BASE_IMAGE"]


def test_dev_image_resolve_and_check_need_no_write_access() -> None:
    """Resolving the nightly and checking the image only read, and need no login."""
    workflow = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))
    resolve = workflow["jobs"]["resolve"]
    check = workflow["jobs"]["check"]

    assert workflow["permissions"] == {"contents": "read"}
    for job in (resolve, check):
        assert "permissions" not in job
        assert job["timeout-minutes"] > 0
        # a pull request from a fork has no token to log in with
        assert not any("login-action" in step.get("uses", "") for step in job["steps"])
    assert "if" not in resolve
    triggers = _triggers(workflow)
    for event in ("pull_request", "push"):
        assert set(triggers[event]["paths"]) == {
            "Dockerfile.dev",
            "scripts/dev-image/**",
            ".github/workflows/dev-image.yml",
        }


def test_dev_image_check_starts_the_image_on_the_resolved_nightly() -> None:
    """The check builds and starts the image on the digest the resolve job found."""
    jobs = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    check = jobs["check"]
    build = _step(check, "Build development image")["with"]

    assert check["needs"] == "resolve"
    assert check["if"] == "needs.resolve.outputs.build == 'true'"
    assert build["platforms"] == "linux/amd64"
    assert build["push"] is False
    assert build["build-args"].splitlines() == [
        "BASE_IMAGE=${{ env.IMAGE_REPOSITORY }}@${{ needs.resolve.outputs.digest }}"
    ]
    assert jobs["resolve"]["outputs"]["build"] == "${{ steps.nightly.outputs.build }}"
    assert jobs["resolve"]["outputs"]["digest"] == "${{ steps.nightly.outputs.digest }}"


def test_dev_image_publication_follows_a_passed_check_on_dev() -> None:
    """Only an image the check passed is published, from dev of this repository, never for a PR."""
    workflow = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))
    triggers = _triggers(workflow)
    job = workflow["jobs"]["publish"]

    assert job["needs"] == ["resolve", "check"]
    # without a status function the job only runs when the check succeeded
    assert job["if"] == (
        "needs.resolve.outputs.build == 'true' && "
        "github.event_name != 'pull_request' && "
        "github.repository == 'music-assistant/server'"
    )
    assert triggers["push"]["branches"] == ["dev"]
    assert triggers["workflow_run"] == {
        "workflows": ["Auto Release", "Create Release"],
        "types": ["completed"],
        "branches": ["dev"],
    }
    assert "workflow_dispatch" in triggers
    require_dev = _step(job, "Require dev")
    assert require_dev["env"] == {"DISPATCH_REF": "${{ github.ref }}"}
    assert '[ "$DISPATCH_REF" != "refs/heads/dev" ]' in require_dev["run"]
    assert job["steps"][0]["name"] == "Require dev"
    assert _step(job, "Check out dev")["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert job["timeout-minutes"] > 0
    assert workflow["concurrency"] == {
        "group": "dev-image-${{ github.event.pull_request.number || 'publish' }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert all("concurrency" not in job for job in workflow["jobs"].values())


def test_dev_image_publication_builds_on_the_checked_nightly() -> None:
    """The image is built for both platforms on the digest the check ran on, not on a tag."""
    job = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["publish"]
    build = _step(job, "Build and push development image")

    assert build["with"]["file"] == "Dockerfile.dev"
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["tags"] == "${{ env.IMAGE_REPOSITORY }}:dev"
    assert build["with"]["push"] is True
    assert build["with"]["build-args"].splitlines() == [
        "BASE_IMAGE=${{ env.IMAGE_REPOSITORY }}@${{ needs.resolve.outputs.digest }}"
    ]
    assert build["with"]["labels"].splitlines() == [
        "org.opencontainers.image.version=${{ needs.resolve.outputs.version }}",
        "org.opencontainers.image.revision=${{ needs.resolve.outputs.revision }}",
        "org.opencontainers.image.base.digest=${{ needs.resolve.outputs.digest }}",
    ]
    assert _step(job, "Verify development image")["env"] == {
        "BASE_DIGEST": "${{ needs.resolve.outputs.digest }}",
        "BUILT_DIGEST": "${{ steps.build.outputs.digest }}",
    }


@needs_jq
def test_dev_image_publication_skips_a_release_run_without_a_new_nightly(tmp_path: Path) -> None:
    """After a release run with an unchanged nightly, nothing is built, started or published."""
    jobs = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    for name in ("check", "publish"):
        assert jobs[name]["if"].startswith("needs.resolve.outputs.build == 'true'"), name

    code, outputs, stderr = _run_workflow_step(
        tmp_path,
        "resolve",
        "Resolve nightly image",
        {**_nightly(), **_dev(NIGHTLY)},
        "workflow_run",
    )

    assert code == 0, stderr
    assert outputs == {
        "digest": NIGHTLY,
        "version": "2.11.0.dev2026111503",
        "revision": "abc123",
        "build": "false",
    }


@needs_jq
@pytest.mark.parametrize(
    ("event", "dev_base"),
    [
        ("workflow_run", "sha256:" + "9" * 64),
        ("workflow_run", None),
        ("workflow_dispatch", NIGHTLY),
        ("push", NIGHTLY),
    ],
)
def test_dev_image_publication_builds(tmp_path: Path, event: str, dev_base: str | None) -> None:
    """A new nightly, a first image, a start by hand and a push all build the image."""
    registry = {**_nightly(), **(_dev(dev_base) if dev_base else {})}
    code, outputs, stderr = _run_workflow_step(
        tmp_path, "resolve", "Resolve nightly image", registry, event
    )

    assert code == 0, stderr
    assert outputs["build"] == "true"
    assert outputs["digest"] == NIGHTLY


@needs_jq
@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("nightly_without_version", "has no version or revision label"),
        ("unreadable_dev", "Unable to inspect"),
    ],
)
def test_dev_image_publication_stops_on_doubt(tmp_path: Path, case: str, message: str) -> None:
    """A nightly without its labels, or a registry that cannot be read, stops the job."""
    if case == "nightly_without_version":
        registry = _nightly(labels={"org.opencontainers.image.revision": "abc123"})
    else:
        registry = {**_nightly(), f"{IMAGE_REPOSITORY}:dev": {"error": "unauthorized"}}
    code, _outputs, stderr = _run_workflow_step(
        tmp_path, "resolve", "Resolve nightly image", registry, "workflow_run"
    )

    assert code != 0
    assert message in stderr


@needs_jq
@pytest.mark.parametrize(
    ("built", "platforms", "base", "ok"),
    [
        (DEV, ("amd64", "arm64"), NIGHTLY, True),
        ("sha256:" + "8" * 64, ("amd64", "arm64"), NIGHTLY, False),
        (DEV, ("amd64",), NIGHTLY, False),
        (DEV, ("amd64", "arm64"), "sha256:" + "9" * 64, False),
    ],
)
def test_dev_image_publication_verifies_the_pushed_image(
    tmp_path: Path, built: str, platforms: tuple[str, ...], base: str, ok: bool
) -> None:
    """The tag must point at the built image, with both platforms on the nightly built on."""
    runtime = {"amd64": DEV_AMD64, "arm64": DEV_ARM64}
    registry = _dev(base, {arch: runtime[arch] for arch in platforms})
    code, _outputs, stderr = _run_workflow_step(
        tmp_path,
        "publish",
        "Verify development image",
        registry,
        "workflow_dispatch",
        {"BASE_DIGEST": NIGHTLY, "BUILT_DIGEST": built},
    )

    assert (code == 0) is ok, stderr


def _run_workflow_step(
    tmp_path: Path,
    job_name: str,
    name: str,
    registry: dict[str, Any],
    event: str,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    job = yaml.safe_load(DEV_IMAGE_WORKFLOW.read_text(encoding="utf-8"))["jobs"][job_name]
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text(STUB_DOCKER, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    # the retries of the registry wait for nothing here
    no_sleep = stub_dir / "sleep"
    no_sleep.write_text("#!/bin/sh\n", encoding="utf-8")
    no_sleep.chmod(no_sleep.stat().st_mode | stat.S_IEXEC)
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps(registry), encoding="utf-8")
    output_file = tmp_path / "github_output"
    output_file.write_text("", encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _step(job, name)["run"]],  # noqa: S607
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_REGISTRY": str(registry_file),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output_file),
            "IMAGE_REPOSITORY": IMAGE_REPOSITORY,
            "EVENT_NAME": event,
            **(env or {}),
        },
        text=True,
    )
    lines = output_file.read_text(encoding="utf-8").splitlines()
    return result.returncode, dict(line.split("=", 1) for line in lines), result.stderr


def _nightly(labels: dict[str, str] | None = None) -> dict[str, Any]:
    if labels is None:
        labels = {
            "org.opencontainers.image.version": "2.11.0.dev2026111503",
            "org.opencontainers.image.revision": "abc123",
        }
    return {
        f"{IMAGE_REPOSITORY}:nightly": {"Manifest": _index(NIGHTLY, {"amd64": NIGHTLY_AMD64})},
        f"{IMAGE_REPOSITORY}@{NIGHTLY_AMD64}": {"Image": {"config": {"Labels": labels}}},
    }


def _dev(base: str, runtime: dict[str, str] | None = None) -> dict[str, Any]:
    runtime = runtime or {"amd64": DEV_AMD64, "arm64": DEV_ARM64}
    labels = {"org.opencontainers.image.base.digest": base}
    registry: dict[str, Any] = {f"{IMAGE_REPOSITORY}:dev": {"Manifest": _index(DEV, runtime)}}
    for digest in runtime.values():
        registry[f"{IMAGE_REPOSITORY}@{digest}"] = {"Image": {"config": {"Labels": labels}}}
    return registry


def _index(digest: str, runtime: dict[str, str]) -> dict[str, Any]:
    manifests = [
        {"digest": arch_digest, "platform": {"os": "linux", "architecture": arch}}
        for arch, arch_digest in runtime.items()
    ]
    # the provenance attestation sits next to the images
    manifests.append(
        {"digest": "sha256:" + "0" * 64, "platform": {"os": "unknown", "architecture": "unknown"}}
    )
    return {"digest": digest, "manifests": manifests}


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML reads the key "on" as True
    triggers: dict[str, Any] = workflow["on"] if "on" in workflow else workflow[True]
    return triggers


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return dict(step)
    msg = f"Step {name!r} not found"
    raise AssertionError(msg)
