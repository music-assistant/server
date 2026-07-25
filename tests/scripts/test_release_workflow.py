"""Tests for immutable-safe release workflow helpers."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts.release_workflow import (
    OCI_REVISION_ANNOTATION,
    OCI_WHEEL_ANNOTATION,
    GitRepository,
    ReleaseWorkflowError,
    channel_branch,
    compare_frontend_versions,
    compare_release_versions,
    determine_auto_release,
    inspect_assets,
    is_current_release,
    select_release,
    update_addon_release,
    verify_oci_manifest,
)

ROOT = Path(__file__).parents[2]
DEPENDENCY_AUTO_MERGE_WORKFLOW = (
    ROOT / ".github" / "workflows" / "auto-merge-dependency-updates.yml"
)
PINNED_ACTION_FILES = (
    ROOT / ".github" / "workflows" / "release.yml",
    ROOT / ".github" / "workflows" / "auto-release.yml",
    ROOT / ".github" / "workflows" / "build-base-image.yml",
    ROOT / ".github" / "workflows" / "dependabot-sync-manifests.yml",
    ROOT / ".github" / "workflows" / "pr-labels.yaml",
    ROOT / ".github" / "actions" / "generate-release-notes" / "action.yml",
)
LEGACY_AUTH_FILES = (
    *PINNED_ACTION_FILES,
    DEPENDENCY_AUTO_MERGE_WORKFLOW,
    ROOT / ".github" / "release-notes-config.yml",
)
FORBIDDEN_LEGACY_IDENTIFIERS = tuple(
    separator.join(parts)
    for separator, parts in (
        ("_", ("PRIVILEGED", "GITHUB", "TOKEN")),
        ("_", ("TRIAGE", "GITHUB", "TOKEN")),
        ("_", ("TRIAGE", "APP", "ID")),
        ("_", ("TRIAGE", "APP", "PRIVATE", "KEY")),
        ("-", ("music", "assistant", "machine")),
    )
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


@pytest.mark.parametrize(
    ("current", "requested", "relation"),
    [
        ("2.10.0.dev2026072502", "2.10.0.dev2026072501", "newer"),
        ("2.10.0b1", "2.10.0.dev2026072502", "newer"),
        ("2.10.0rc1", "2.10.0b20", "newer"),
        ("2.10.0", "2.10.0rc4", "newer"),
        ("2.9.10", "2.10.0", "older"),
        ("2.10.0b8", "2.10.0b8", "equal"),
    ],
)
def test_release_version_order(
    current: str,
    requested: str,
    relation: str,
) -> None:
    """Rolling aliases use release ordering across all supported stages."""
    assert compare_release_versions(current, requested) == relation


@pytest.mark.parametrize(
    ("current", "requested", "relation"),
    [
        ("2.17.235", "2.17.234", "newer"),
        ("2.17.186.post3", "2.17.186", "newer"),
        ("2.17.186", "2.17.186.post1", "older"),
        ("2.17.228", "2.17.228.0", "equal"),
    ],
)
def test_frontend_version_order(
    current: str,
    requested: str,
    relation: str,
) -> None:
    """Frontend dispatches compare numeric and post-release versions."""
    assert compare_frontend_versions(current, requested) == relation


def test_select_release_returns_none_without_an_exact_tag() -> None:
    """Release selection ignores nonmatching tags across every API page."""
    release_pages = [
        [{"id": 10, "tag_name": "2.10.0b7"}],
        [],
        [{"id": 11, "tag_name": "2.10.0B8"}],
    ]

    assert select_release(release_pages, "2.10.0b8") is None


def test_select_release_finds_one_exact_draft_across_pages() -> None:
    """Release selection returns the exact draft and its existing id."""
    expected = {
        "id": 359740600,
        "tag_name": "2.10.0b8",
        "draft": True,
        "immutable": False,
    }
    release_pages = [
        [{"id": 10, "tag_name": "2.10.0b7"}],
        [expected],
        [{"id": 12, "tag_name": "2.10.0b80"}],
    ]

    assert select_release(release_pages, "2.10.0b8") is expected


def test_select_release_rejects_duplicate_exact_tags() -> None:
    """Release selection fails closed when multiple releases use the exact tag."""
    release_pages = [
        [{"id": 359740600, "tag_name": "2.10.0b8"}],
        [{"id": 359752771, "tag_name": "2.10.0b8"}],
    ]

    with pytest.raises(
        ReleaseWorkflowError,
        match=re.escape("Multiple releases match exact tag 2.10.0b8: 359740600, 359752771"),
    ):
        select_release(release_pages, "2.10.0b8")


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


def test_automation_drops_legacy_credentials() -> None:
    """Ensure migrated automation does not reference retired bot credentials."""
    for automation_path in LEGACY_AUTH_FILES:
        automation = automation_path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_LEGACY_IDENTIFIERS:
            assert forbidden not in automation


def test_automation_pins_external_actions() -> None:
    """Ensure migrated automation pins external actions to immutable commits."""
    action_pattern = re.compile(
        r"^\s*(?:-\s+)?uses:\s+([^./][^@]+)@(\S+)(?:\s+#\s+(.+))?$",
        re.MULTILINE,
    )
    for workflow_path in PINNED_ACTION_FILES:
        workflow = workflow_path.read_text(encoding="utf-8")
        matches = action_pattern.findall(workflow)
        assert matches
        for _action, ref, comment in matches:
            assert re.fullmatch(r"[0-9a-f]{40}", ref)
            assert re.fullmatch(r"v\d+(?:\.\d+){0,2}", comment)


@pytest.mark.parametrize(
    ("login", "user_type", "user_id", "head_repository", "trusted"),
    [
        (
            "musicassistant-bot[bot]",
            "Bot",
            "304008617",
            "music-assistant/server",
            True,
        ),
        ("musicassistant-bot", "Bot", "304008617", "music-assistant/server", False),
        (
            "musicassistant-bot[bot]",
            "User",
            "304008617",
            "music-assistant/server",
            False,
        ),
        ("marcelveldt", "User", "6389780", "music-assistant/server", False),
        (
            "musicassistant-bot[bot]",
            "Bot",
            "304008617",
            "untrusted/server",
            False,
        ),
        (
            "musicassistant-bot[bot]",
            "Bot",
            "123456789",
            "music-assistant/server",
            False,
        ),
    ],
)
def test_dependency_auto_merge_app_bot_identity_contract(
    login: str,
    user_type: str,
    user_id: str,
    head_repository: str,
    trusted: bool,
) -> None:
    """Accept only the expected same-repository GitHub App bot identity."""
    workflow = yaml.safe_load(DEPENDENCY_AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8"))
    expected = workflow["env"]

    is_trusted_app = (
        login == expected["EXPECTED_APP_BOT_LOGIN"]
        and user_type == "Bot"
        and user_id == expected["EXPECTED_APP_BOT_ID"]
        and head_repository == "music-assistant/server"
    )

    assert is_trusted_app is trusted


@pytest.mark.parametrize(
    ("login", "user_type", "user_id", "trusted"),
    [
        ("musicassistant-bot[bot]", "Bot", "304008617", True),
        ("marcelveldt", "User", "6389780", False),
        ("musicassistant-bot", "Bot", "304008617", False),
        ("musicassistant-bot[bot]", "User", "304008617", False),
        ("musicassistant-bot[bot]", "Bot", "123456789", False),
    ],
)
def test_dependency_auto_merge_commit_author_contract(
    login: str,
    user_type: str,
    user_id: str,
    trusted: bool,
) -> None:
    """Accept commits only from the expected GitHub App bot identity."""
    workflow = yaml.safe_load(DEPENDENCY_AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8"))
    expected = workflow["env"]

    is_trusted_author = (
        login == expected["EXPECTED_APP_BOT_LOGIN"]
        and user_type == "Bot"
        and user_id == expected["EXPECTED_APP_BOT_ID"]
    )

    assert is_trusted_author is trusted


def test_dependency_auto_merge_enforces_app_bot_identity_contract() -> None:
    """Ensure the workflow enforces the tested App bot identity contract."""
    workflow_text = DEPENDENCY_AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert workflow_text.count("musicassistant-bot[bot]") == 1
    assert workflow["env"] == {
        "EXPECTED_APP_BOT_LOGIN": "musicassistant-bot[bot]",
        "EXPECTED_APP_BOT_LOGIN_ENCODED": "musicassistant-bot%5Bbot%5D",
        "EXPECTED_APP_BOT_ID": "304008617",
    }

    job = workflow["jobs"]["auto-merge"]
    steps = {step["name"]: step for step in job["steps"]}
    source_step = steps["Verify PR is from trusted source"]
    assert source_step["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "BASE_REPOSITORY": "${{ github.repository }}",
        "HEAD_REPOSITORY": "${{ github.event.pull_request.head.repo.full_name }}",
        "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
        "PR_AUTHOR_ID": "${{ github.event.pull_request.user.id }}",
        "PR_AUTHOR_TYPE": "${{ github.event.pull_request.user.type }}",
    }
    source_check = source_step["run"]
    for required_check in (
        'if [ "$PR_AUTHOR" != "$EXPECTED_APP_BOT_LOGIN" ] ||',
        '[ "$PR_AUTHOR_TYPE" != "Bot" ] ||',
        '[ "$PR_AUTHOR_ID" != "$EXPECTED_APP_BOT_ID" ] ||',
        '[ "$HEAD_REPOSITORY" != "$BASE_REPOSITORY" ]; then',
        'gh api "/users/$EXPECTED_APP_BOT_LOGIN_ENCODED"',
    ):
        assert required_check in source_check
    assert "collaborators/" not in source_check

    author_check = steps["Verify commit authors"]["run"]
    assert '--arg login "$EXPECTED_APP_BOT_LOGIN"' in author_check
    assert '--argjson id "$EXPECTED_APP_BOT_ID"' in author_check
    assert ".author.login != $login" in author_check
    assert 'author.type != "Bot"' in author_check
    assert ".author.id != $id" in author_check
    assert "UNTRUSTED_AUTHORS" in author_check
    assert "UNATTRIBUTED" in author_check
    assert "COMMIT_COUNT" in author_check
    assert "collaborators/" not in author_check

    assert "auto-update-frontend-" in job["if"]
    assert "auto-update-models-" in job["if"]
    labels_check = steps["Verify PR labels and source"]["run"]
    assert '"dependencies"' in labels_check
    assert "auto-update-frontend-*" in labels_check
    assert "auto-update-models-*" in labels_check
    files_check = steps["Verify only dependency files were changed"]["run"]
    assert '"pyproject.toml"' in files_check
    assert '"requirements_all.txt"' in files_check
    diff_check = steps["Verify changes are version bumps"]["run"]
    assert "UNEXPECTED=" in diff_check
    assert "No added version pin found" in diff_check
    availability_check = steps["Wait for package availability on PyPI"]["run"]
    assert "python3 -m pip download --no-deps" in availability_check
    assert "--approve" in steps["Auto-approve PR"]["run"]
    assert "--auto --squash" in steps["Enable auto-merge"]["run"]


def test_release_workflow_uses_minimum_preflight_permissions_and_expected_app() -> None:
    """Resolve can discover drafts while preflight and App tokens stay minimal."""
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    jobs = yaml.safe_load(workflow)["jobs"]

    assert jobs["resolve"]["permissions"] == {"contents": "write"}
    assert jobs["preflight"]["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert workflow.count("actions/create-github-app-token@") == 5
    assert workflow.count("outputs.installation-id") == 5
    assert 'EXPECTED_APP_INSTALLATION_ID: "146062122"' in workflow
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", workflow)) == {
        "MUSIC_ASSISTANT_BOT_PRIVATE_KEY"
    }
    assert set(re.findall(r"vars\.([A-Z0-9_]+)", workflow)) == {"MUSIC_ASSISTANT_BOT_CLIENT_ID"}
    assert "ref: main" not in workflow
    assert "HEAD:main" not in workflow
    assert "compare-release-versions" in workflow
    assert "compare-frontend-versions" in workflow
    assert "idempotency_key" in workflow
    assert "repos/music-assistant/home-assistant-addon" in workflow
    assert "'.default_branch'" in workflow


def test_release_workflow_dispatch_source_sha_is_optional_for_recovery() -> None:
    """Direct recovery keeps source_sha optional while workflow_call stays required."""
    workflow = cast(
        "Mapping[object, Any]",
        yaml.safe_load(
            (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        ),
    )
    triggers = _workflow_triggers(workflow)

    assert triggers["workflow_dispatch"]["inputs"]["source_sha"] == {
        "description": (
            "Exact full commit SHA for draft/published recovery; leave empty to "
            "resolve the current channel branch head"
        ),
        "required": False,
        "type": "string",
    }
    assert triggers["workflow_call"]["inputs"]["source_sha"] == {
        "description": "Exact source commit to release",
        "required": True,
        "type": "string",
    }


def test_release_workflow_exact_source_resolution_uses_requested_sha() -> None:
    """Resolve exact source commit honors recovery SHAs and rejects bad ones."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    resolve_step = _workflow_step(
        cast("dict[str, Any]", yaml.safe_load(workflow)),
        "resolve",
        "Resolve exact source commit",
    )

    assert resolve_step["env"] == {"REQUESTED_SHA": "${{ inputs.source_sha }}"}
    resolve_run = str(resolve_step["run"])

    expected_resolution = """\
if [ -n "$REQUESTED_SHA" ]; then
  requested_sha=$(printf '%s' "$REQUESTED_SHA" |
    tr '[:upper:]' '[:lower:]')
  if ! [[ "$requested_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "source_sha must be a full commit SHA" >&2
    exit 1
  fi
  source_sha=$(git -C source rev-parse "$requested_sha^{commit}")
  if ! git -C source merge-base --is-ancestor "$source_sha" "$branch_sha"; then
    echo "$source_sha is not part of ${{ steps.branch.outputs.branch }}" >&2
    exit 1
  fi
else
  source_sha="$branch_sha"
fi
echo "sha=$source_sha" >> "$GITHUB_OUTPUT"
"""
    assert expected_resolution in resolve_run


def test_release_workflow_discovers_exact_drafts_from_paginated_releases() -> None:
    """Resolve discovers draft releases through the paginated releases endpoint."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    resolve_run = _workflow_step_run(
        workflow,
        job_name="resolve",
        step_name="Inspect existing tag and release",
    )

    assert "gh api --paginate --slurp" in resolve_run
    assert '"repos/$GITHUB_REPOSITORY/releases?per_page=100"' in resolve_run
    assert "release_workflow.py select-release" in resolve_run
    assert '--release-json "$release_json"' in resolve_run
    assert "release_id=$(sed -n 's/^release_id=//p' \"$release_lookup\")" in resolve_run


def test_release_workflow_reuses_resolved_draft_id() -> None:
    """Draft recovery and updates revalidate and reuse the resolved release id."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    parsed_workflow = cast("dict[str, Any]", yaml.safe_load(workflow))
    recover_step = _workflow_step(
        parsed_workflow,
        "build_artifacts",
        "Recover matching draft assets",
    )
    assert recover_step["env"]["RELEASE_ID"] == "${{ needs.resolve.outputs.release_id }}"
    recover_run = str(recover_step["run"])
    assert 'if [ -z "$RELEASE_ID" ]; then' in recover_run
    assert 'gh api "repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID"' in recover_run
    assert "'.tag_name // empty'" in recover_run
    assert "'.draft'" in recover_run
    assert "'.immutable'" in recover_run
    assert "'.target_commitish'" in recover_run
    assert "gh release download" not in recover_run
    assert "Draft must contain exactly one $asset_name asset" in recover_run
    assert '"repos/$GITHUB_REPOSITORY/releases/assets/$asset_id"' in recover_run

    draft_step = _workflow_step(
        parsed_workflow,
        "prepare_draft",
        "Create or update matching draft",
    )
    assert draft_step["env"]["RELEASE_ID"] == "${{ needs.resolve.outputs.release_id }}"
    draft_run = str(draft_step["run"])
    assert 'if [ -n "$RELEASE_ID" ]; then' in draft_run
    assert 'gh api "repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID"' in draft_run
    assert '"repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID" \\\n' in draft_run
    assert '"repos/$GITHUB_REPOSITORY/releases" \\\n' in draft_run
    assert "release_exists=true" in draft_run
    assert "release_exists=false" in draft_run

    replace_run = str(
        _workflow_step(
            parsed_workflow,
            "prepare_draft",
            "Replace draft assets",
        )["run"]
    )
    assert "gh release upload" not in replace_run
    assert (
        "https://uploads.github.com/repos/$GITHUB_REPOSITORY/releases/"
        "$RELEASE_ID/assets?name=$asset_name"
    ) in replace_run


def test_release_workflow_avoids_prepublication_tag_release_lookups() -> None:
    """Only post-publication work may use GitHub's published tag endpoint."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    parsed_workflow = cast("dict[str, Any]", yaml.safe_load(workflow))
    tag_endpoint = "repos/$GITHUB_REPOSITORY/releases/tags/$VERSION"

    for job_name in ("resolve", "build_artifacts", "prepare_draft"):
        for step in parsed_workflow["jobs"][job_name]["steps"]:
            assert tag_endpoint not in str(step.get("run", ""))

    publication_run = _workflow_step_run(
        workflow,
        job_name="publish_release",
        step_name="Verify immutable release and assets",
    )
    assert tag_endpoint in publication_run
    assert workflow.count(tag_endpoint) == 2


def test_release_workflow_publication_state_uses_release_ids() -> None:
    """Publication lookup must use release IDs and fail closed on mismatches."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "DRAFT_RELEASE_ID: ${{ needs.prepare_draft.outputs.release_id }}" in workflow
    assert "PUBLISHED_RELEASE_ID: ${{ needs.resolve.outputs.release_id }}" in workflow
    assert "RELEASE_STATE: ${{ needs.resolve.outputs.release_state }}" in workflow

    publication_step = _workflow_step_run(
        workflow,
        job_name="publish_release",
        step_name="Detect live publication state",
    )

    assert 'gh api "repos/$GITHUB_REPOSITORY/releases/tags/$VERSION"' not in publication_step
    assert 'gh api "repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID"' in publication_step
    assert 'case "$RELEASE_STATE" in' in publication_step
    assert "new|draft)" in publication_step
    assert "Missing release id for $RELEASE_STATE release $VERSION" in publication_step
    assert "tag_name" in publication_step
    assert "Release $VERSION resolves to tag $live_tag_name" in publication_step
    assert "Release $VERSION is neither a mutable draft nor immutable" in publication_step


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


def _workflow_triggers(workflow: Mapping[object, Any]) -> dict[str, Any]:
    trigger = workflow.get("on")
    if trigger is None:
        trigger = workflow[True]
    assert isinstance(trigger, dict)
    return cast("dict[str, Any]", trigger)


def _workflow_step(workflow: Mapping[str, Any], job_name: str, step_name: str) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    for step in jobs[job_name]["steps"]:
        if step.get("name") == step_name:
            return cast("dict[str, Any]", step)
    msg = f"Step {step_name!r} not found in job {job_name!r}"
    raise AssertionError(msg)


def _workflow_step_run(workflow: str, job_name: str, step_name: str) -> str:
    return str(_workflow_step(yaml.safe_load(workflow), job_name, step_name)["run"])
