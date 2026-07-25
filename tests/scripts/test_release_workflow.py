"""Tests for immutable-safe release workflow helpers."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.release_workflow import (
    OCI_REVISION_ANNOTATION,
    OCI_WHEEL_ANNOTATION,
    GitRepository,
    ReleaseWorkflowError,
    channel_branch,
    determine_auto_release,
    inspect_assets,
    is_current_release,
    update_addon_release,
    verify_oci_manifest,
)

ROOT = Path(__file__).parents[2]
PINNED_WORKFLOW_FILES = (
    ROOT / ".github" / "workflows" / "release.yml",
    ROOT / ".github" / "workflows" / "auto-release.yml",
    ROOT / ".github" / "actions" / "generate-release-notes" / "action.yml",
)


@pytest.fixture(name="repository")
def repository_fixture(tmp_path: Path) -> tuple[Path, GitRepository]:
    """Create a Git repository with one initial commit."""
    _git(tmp_path, "init", "-b", "dev")
    _git(tmp_path, "config", "user.name", "Release Test")
    _git(tmp_path, "config", "user.email", "release@example.com")
    _commit(tmp_path, "initial")
    return tmp_path, GitRepository(tmp_path)


def test_channel_branch_is_explicit() -> None:
    """Release channels map to their intended source branches."""
    assert channel_branch("stable") == "stable"
    assert channel_branch("rc") == "stable"
    assert channel_branch("beta") == "dev"
    assert channel_branch("nightly") == "dev"


def test_nightly_uses_tag_commit_range_and_avoids_tag_collision(
    repository: tuple[Path, GitRepository],
) -> None:
    """Nightly discovery counts commits from tags and never reuses a failed tag."""
    path, git_repository = repository
    _git(path, "tag", "2.9.9")
    _git(path, "tag", "2.10.0.dev2026072501")
    _commit(path, "change one")
    _commit(path, "change two")

    decision = determine_auto_release(
        git_repository,
        "nightly",
        "HEAD",
        now=datetime(2026, 7, 25, 1, tzinfo=UTC),
    )

    assert decision.version == "2.10.0.dev2026072502"
    assert decision.previous_tag == "2.10.0.dev2026072501"
    assert decision.commits_since == 2
    assert decision.should_release is True


def test_nightly_requires_two_commits(repository: tuple[Path, GitRepository]) -> None:
    """A nightly is skipped when fewer than two commits follow its latest tag."""
    path, git_repository = repository
    _git(path, "tag", "2.9.9")
    _git(path, "tag", "2.10.0.dev2026072405")
    _commit(path, "only change")

    decision = determine_auto_release(
        git_repository,
        "nightly",
        "HEAD",
        now=datetime(2026, 7, 25, 5, tzinfo=UTC),
    )

    assert decision.commits_since == 1
    assert decision.should_release is False


def test_beta_rejects_latest_tag_outside_source_history(
    repository: tuple[Path, GitRepository],
) -> None:
    """A latest beta tag on another history cannot define a release range."""
    path, git_repository = repository
    source_sha = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "--orphan", "unrelated")
    (path / "state").unlink()
    _commit(path, "unrelated")
    _git(path, "tag", "2.10.0b1")

    with pytest.raises(ReleaseWorkflowError, match="not an ancestor"):
        determine_auto_release(git_repository, "beta", source_sha)


def test_stable_preserves_patch_versioning_across_diverged_branches(
    repository: tuple[Path, GitRepository],
) -> None:
    """Stable release discovery allows a branch cut but still increments patch."""
    path, git_repository = repository
    _git(path, "branch", "stable")
    _git(path, "checkout", "stable")
    _commit(path, "stable patch")
    _git(path, "tag", "2.9.9")
    _git(path, "checkout", "dev")
    _commit(path, "development")

    decision = determine_auto_release(git_repository, "stable", "HEAD")

    assert decision.version == "2.9.10"
    assert decision.previous_tag == "2.9.9"
    assert decision.commits_since == 1
    assert decision.should_release is True


def test_current_release_combines_beta_and_rc_channels(
    repository: tuple[Path, GitRepository],
) -> None:
    """An older beta retry cannot move the shared beta channel behind an RC."""
    path, git_repository = repository
    _git(path, "tag", "2.10.0b8")
    _git(path, "tag", "2.10.0rc1")

    assert is_current_release(git_repository, "rc", "2.10.0rc1") == (
        True,
        "2.10.0rc1",
    )
    assert is_current_release(git_repository, "beta", "2.10.0b8") == (
        False,
        "2.10.0rc1",
    )


def test_release_assets_match_names_sizes_and_digests(tmp_path: Path) -> None:
    """Local distributions must exactly match GitHub's two release assets."""
    version = "2.10.0b8"
    assets_directory = tmp_path / "assets"
    assets_directory.mkdir()
    wheel = assets_directory / f"music_assistant-{version}-py3-none-any.whl"
    source = assets_directory / f"music_assistant-{version}.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    release_json = tmp_path / "release.json"
    release_json.write_text(
        json.dumps(
            {
                "assets": [
                    _api_asset(wheel),
                    _api_asset(source),
                ]
            }
        ),
        encoding="utf-8",
    )

    assets = inspect_assets(
        version,
        directory=assets_directory,
        release_json=release_json,
    )

    assert assets[0].sha256 == hashlib.sha256(b"wheel").hexdigest()
    assert assets[1].sha256 == hashlib.sha256(b"source").hexdigest()


def test_release_assets_reject_extras(tmp_path: Path) -> None:
    """A draft with any extra asset is not safe to publish."""
    version = "2.10.0b8"
    wheel = tmp_path / f"music_assistant-{version}-py3-none-any.whl"
    source = tmp_path / f"music_assistant-{version}.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    (tmp_path / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ReleaseWorkflowError, match="exactly"):
        inspect_assets(version, directory=tmp_path)


def test_release_assets_reject_duplicate_api_entries(tmp_path: Path) -> None:
    """Two API entries with the same expected name are not two exact assets."""
    version = "2.10.0b8"
    wheel = tmp_path / f"music_assistant-{version}-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    release_json = tmp_path / "release.json"
    release_json.write_text(
        json.dumps({"assets": [_api_asset(wheel), _api_asset(wheel)]}),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseWorkflowError, match="duplicate"):
        inspect_assets(version, release_json=release_json)


def test_oci_manifest_requires_exact_platforms_and_provenance() -> None:
    """The exact image identifies its source and wheel on both target platforms."""
    source_sha = "a" * 40
    wheel_sha = "b" * 64
    manifest = {
        "digest": f"sha256:{'c' * 64}",
        "annotations": {
            OCI_REVISION_ANNOTATION: source_sha,
            OCI_WHEEL_ANNOTATION: wheel_sha,
        },
        "manifests": [
            {
                "digest": f"sha256:{'d' * 64}",
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": f"sha256:{'e' * 64}",
                "platform": {"os": "linux", "architecture": "arm64"},
            },
            {
                "digest": f"sha256:{'f' * 64}",
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ],
    }

    digest, runtime_digests = verify_oci_manifest(manifest, source_sha, wheel_sha)

    assert digest == f"sha256:{'c' * 64}"
    assert runtime_digests == [f"sha256:{'d' * 64}", f"sha256:{'e' * 64}"]


def test_addon_update_replaces_duplicate_version_and_retains_three(tmp_path: Path) -> None:
    """Repeated downstream runs converge on one canonical three-release changelog."""
    config = tmp_path / "config.yaml"
    changelog = tmp_path / "CHANGELOG.md"
    config.write_text("name: Test\nversion: old\nstage: stable\n", encoding="utf-8")
    changelog.write_text(
        "# [new] - 01.01.2026\n\nold duplicate\n\n\n"
        "# [older] - 31.12.2025\n\nolder notes\n\n\n"
        "# [new] - 30.12.2025\n\nsecond duplicate\n\n\n"
        "# [older] - 30.12.2025\n\nolder duplicate\n\n\n"
        "# [oldest] - 29.12.2025\n\noldest notes\n",
        encoding="utf-8",
    )

    update_addon_release(
        config,
        changelog,
        version="new",
        release_date="02.01.2026",
        notes="canonical notes",
    )
    first_result = changelog.read_text(encoding="utf-8")
    update_addon_release(
        config,
        changelog,
        version="new",
        release_date="02.01.2026",
        notes="canonical notes",
    )

    assert config.read_text(encoding="utf-8").splitlines()[1] == "version: new"
    assert changelog.read_text(encoding="utf-8") == first_result
    assert first_result.count("# [new]") == 1
    assert first_result.count("# [older]") == 1
    assert "# [older]" in first_result
    assert "# [oldest]" in first_result


def test_release_workflows_pin_external_actions_and_drop_legacy_token() -> None:
    """Touched release flows use commit-pinned actions and no privileged PAT."""
    action_pattern = re.compile(r"^\s*uses:\s+([^./][^@]+)@(\S+)(?:\s+#\s+(.+))?$", re.MULTILINE)
    for workflow_path in PINNED_WORKFLOW_FILES:
        workflow = workflow_path.read_text(encoding="utf-8")
        assert "PRIVILEGED_GITHUB_TOKEN" not in workflow
        matches = action_pattern.findall(workflow)
        assert matches
        for _action, ref, comment in matches:
            assert re.fullmatch(r"[0-9a-f]{40}", ref)
            assert re.fullmatch(r"v\d+(?:\.\d+){0,2}", comment)


def _api_asset(path: Path) -> dict[str, str | int]:
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "state": "uploaded",
        "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
    }


def _commit(path: Path, message: str) -> str:
    state = path / "state"
    previous = state.read_text(encoding="utf-8") if state.exists() else ""
    state.write_text(f"{previous}{message}\n", encoding="utf-8")
    _git(path, "add", "state")
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
