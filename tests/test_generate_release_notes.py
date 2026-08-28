"""Tests for the generate-release-notes GitHub action script."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent
    / ".github"
    / "actions"
    / "generate-release-notes"
    / "generate_notes.py"
)


@pytest.fixture
def generate_notes(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Load the action script with its action-only dependencies stubbed."""
    # The action script depends on PyGithub and PyYAML, which are installed ad hoc
    # in the GitHub action and not part of the project's (test) dependencies.
    github_stub = types.ModuleType("github")
    github_stub.Github = object  # type: ignore[attr-defined]
    github_stub.GithubException = type("GithubException", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "github", github_stub)
    if importlib.util.find_spec("yaml") is None:
        monkeypatch.setitem(sys.modules, "yaml", types.ModuleType("yaml"))
    spec = importlib.util.spec_from_file_location("generate_notes", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCommit:
    """Mimics PyGithub Commit (.sha and .commit.message/.commit.committer.date)."""

    def __init__(self, sha: str, message: str, date: datetime | None = None) -> None:
        """Initialize fake commit."""
        self.sha = sha
        self.commit = types.SimpleNamespace(
            message=message,
            committer=types.SimpleNamespace(date=date),
        )


class FakeComparison:
    """Mimics PyGithub Comparison."""

    def __init__(
        self,
        commits: list[FakeCommit],
        behind_by: int = 0,
        merge_base_commit: FakeCommit | None = None,
    ) -> None:
        """Initialize fake comparison."""
        self.commits = commits
        self.total_commits = len(commits)
        self.behind_by = behind_by
        self.merge_base_commit = merge_base_commit


class FakePR:
    """Mimics PyGithub PullRequest."""

    def __init__(
        self,
        number: int,
        merged_at: datetime,
        title: str = "",
        author: str = "someone",
        labels: tuple[str, ...] = (),
        body: str = "",
    ) -> None:
        """Initialize fake pull request."""
        self.number = number
        self.merged = True
        self.merged_at = merged_at
        self.title = title
        self.user = types.SimpleNamespace(login=author)
        self.labels = [types.SimpleNamespace(name=label) for label in labels]
        self.body = body
        self.html_url = f"https://github.com/music-assistant/server/pull/{number}"


class FakeRepo:
    """Mimics the PyGithub Repository calls used by get_prs_between_tags."""

    def __init__(
        self,
        comparisons: dict[tuple[str, str], FakeComparison],
        pulls: dict[int, FakePR],
        tag_commits: dict[str, FakeCommit] | None = None,
    ) -> None:
        """Initialize fake repository."""
        self.comparisons = comparisons
        self.pulls = pulls
        self.tag_commits = tag_commits or {}
        self.fetched_pulls: list[int] = []

    def compare(self, base: str, head: str) -> FakeComparison:
        """Return the preset comparison for the given base/head refs."""
        return self.comparisons[(base, head)]

    def get_pull(self, number: int) -> FakePR:
        """Return the preset pull request with the given number."""
        self.fetched_pulls.append(number)
        return self.pulls[number]

    def get_git_ref(self, ref: str) -> types.SimpleNamespace:
        """Return a lightweight tag ref pointing at the preset tag commit."""
        tag_name = ref.removeprefix("tags/")
        commit = self.tag_commits[tag_name]
        return types.SimpleNamespace(object=types.SimpleNamespace(type="commit", sha=commit.sha))

    def get_commit(self, sha: str) -> FakeCommit:
        """Return the preset commit with the given sha."""
        for commit in self.tag_commits.values():
            if commit.sha == sha:
                return commit
        raise KeyError(sha)


def test_linear_release_filters_prs_merged_before_previous_tag(
    generate_notes: types.ModuleType,
) -> None:
    """Beta/nightly/patch releases: previous tag is an ancestor of the branch."""
    tag_commit = FakeCommit("tagsha", "2.9.0b1 release", datetime(2026, 6, 1, tzinfo=UTC))
    comparison = FakeComparison(
        commits=[
            FakeCommit("aaa", "Add feature (#200)"),
            FakeCommit("bbb", "Improve thing\n\nfixes #150"),
        ],
    )
    repo = FakeRepo(
        comparisons={("2.9.0b1", "headsha"): comparison},
        pulls={
            200: FakePR(200, datetime(2026, 6, 5, tzinfo=UTC)),
            150: FakePR(150, datetime(2026, 1, 10, tzinfo=UTC)),
        },
        tag_commits={"2.9.0b1": tag_commit},
    )

    prs = generate_notes.get_prs_between_tags(repo, "2.9.0b1", "headsha")

    assert [pr.number for pr in prs] == [200]


def test_minor_release_with_diverged_previous_tag(
    generate_notes: types.ModuleType,
) -> None:
    """
    Generate notes for a minor release whose previous tag diverged.

    For a minor release (e.g. 2.9.0) the previous stable tag (2.8.9) lives on the
    old stable branch, which diverged from dev at the 2.8.0 branch point. The notes
    must include everything merged to dev since the branch point, except PRs that
    already shipped in the 2.8.x patch releases.
    """
    merge_base = FakeCommit("mbsha", "2.8.0 release", datetime(2026, 3, 25, tzinfo=UTC))
    head_comparison = FakeComparison(
        commits=[
            # Merged to dev well before the 2.8.9 tag date: must be included
            FakeCommit("aaa", "Add feature X (#100)"),
            # Cherry-picked to stable and released as a 2.8.x patch: must be excluded
            FakeCommit("bbb", "Fix bug Y (#50)"),
            # Body references an old PR merged before the branch point: must be excluded
            FakeCommit("ccc", "Improve Z (#120)\n\nfixes #10"),
        ],
        behind_by=3,
        merge_base_commit=merge_base,
    )
    base_comparison = FakeComparison(
        commits=[
            # Body mentions #120 but only the first line identifies the released PR
            FakeCommit("ddd", "Fix bug Y (#50)\n\nRelates to #120"),
        ],
    )
    # 2.8.9 was tagged long after most of the 2.9.0 content was merged to dev
    tag_commit = FakeCommit("tagsha", "2.8.9 release", datetime(2026, 6, 3, tzinfo=UTC))
    repo = FakeRepo(
        comparisons={
            ("2.8.9", "headsha"): head_comparison,
            ("mbsha", "2.8.9"): base_comparison,
        },
        pulls={
            100: FakePR(100, datetime(2026, 4, 20, tzinfo=UTC)),
            50: FakePR(50, datetime(2026, 5, 1, tzinfo=UTC)),
            120: FakePR(120, datetime(2026, 6, 5, tzinfo=UTC)),
            10: FakePR(10, datetime(2026, 1, 1, tzinfo=UTC)),
        },
        tag_commits={"2.8.9": tag_commit},
    )

    prs = generate_notes.get_prs_between_tags(repo, "2.8.9", "headsha")

    assert [pr.number for pr in prs] == [100, 120]


def test_filter_dependency_bumps(generate_notes: types.ModuleType) -> None:
    """Inlined bumps are always dropped; other bumps keep only the latest one."""
    merged_at = datetime(2026, 6, 1, tzinfo=UTC)
    deps = ("dependencies",)
    prs = [
        FakePR(1, merged_at, "⬆️ Update music-assistant-frontend to 2.17.1", labels=deps),
        FakePR(2, merged_at, "Fix a bug"),
        FakePR(3, merged_at, "Bump aiohttp from 3.11.0 to 3.12.0", labels=deps),
        FakePR(4, merged_at, "⬆️ Update music-assistant-models to 1.1.100", labels=deps),
        FakePR(5, merged_at, "Bump aiohttp from 3.12.0 to 3.13.0", labels=deps),
        FakePR(6, merged_at, "⬆️ Update music-assistant-frontend to 2.17.2", labels=deps),
        FakePR(7, merged_at, "Add a feature"),
    ]

    filtered = generate_notes.filter_dependency_bumps(prs)

    assert [pr.number for pr in filtered] == [2, 5, 7]


def test_filter_dependency_bumps_drop_all(generate_notes: types.ModuleType) -> None:
    """With drop_all every labeled dependency bump is dropped, unlabeled PRs never."""
    merged_at = datetime(2026, 6, 1, tzinfo=UTC)
    deps = ("dependencies",)
    prs = [
        FakePR(1, merged_at, "Bump pytest from 9.0.3 to 9.1.1", labels=deps),
        FakePR(2, merged_at, "Fix a bug"),
        FakePR(3, merged_at, "⬆️ Update music-assistant-frontend to 2.17.2", labels=deps),
        FakePR(4, merged_at, "Bump stages for various providers"),
        FakePR(5, merged_at, "Bump `aiosendspin` to 9.1.1", labels=deps),
        FakePR(6, merged_at, "Bump the music-assistant-libs group with 2 updates", labels=deps),
    ]

    filtered = generate_notes.filter_dependency_bumps(prs, drop_all=True)

    assert [pr.number for pr in filtered] == [2, 4]


def test_write_outputs_hands_notes_over_via_file(
    generate_notes: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The notes land in the file; the step output only carries the file path."""
    notes_file = tmp_path / "release-notes.md"
    output_file = tmp_path / "github-output"
    monkeypatch.setenv("RELEASE_NOTES_FILE", str(notes_file))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    notes = "# Notes\n" + ("- a change\n" * 10_000)

    generate_notes.write_outputs(notes, ["alice", "bob"])

    assert notes_file.read_text() == notes
    output = output_file.read_text()
    assert f"release-notes-file={notes_file}" in output
    assert "- a change" not in output
    assert "contributors<<EOF\nalice,bob\nEOF" in output


def test_notes_are_shrunk_to_fit_body_limit(
    generate_notes: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversized notes lose maintenance entries first and gain a full-changelog link."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "music-assistant/server")
    merged_at = datetime(2026, 6, 1, tzinfo=UTC)
    config = {
        "categories": [
            {"title": "🐛 Bugfixes", "labels": ["bugfix"]},
            {
                "title": "🧰 Maintenance",
                "labels": ["maintenance"],
                "after-other": True,
                "collapse-after": 3,
            },
        ],
    }
    categories = {
        "🐛 Bugfixes": [
            FakePR(number, merged_at, f"Fix issue number {number}") for number in range(1, 4)
        ],
        "🧰 Maintenance": [
            FakePR(number, merged_at, f"Maintenance chore number {number}")
            for number in range(100, 140)
        ],
    }
    uncategorized: list[FakePR] = []
    maintenance = categories["🧰 Maintenance"]

    def render() -> str:
        return cast(
            "str",
            generate_notes.generate_release_notes(
                config, categories, uncategorized, [], "2.9.13", None, None
            ),
        )

    limit = len(render()) - 500
    monkeypatch.setattr(generate_notes, "MAX_BODY_CHARS", limit)

    notes = generate_notes.shrink_notes_to_limit(
        render, config, categories, uncategorized, "2.9.13", "2.10.0"
    )

    assert len(notes) <= limit
    # All bugfixes survive; only the maintenance tail was dropped
    for number in range(1, 4):
        assert f"#{number})" in notes
    assert 0 < len(maintenance) < 40
    assert "compare/2.9.13...2.10.0" in notes


def test_categorize_prs_excludes_title_prefixes(generate_notes: types.ModuleType) -> None:
    """PRs matching an excluded title prefix never appear in the notes."""
    merged_at = datetime(2026, 6, 1, tzinfo=UTC)
    config = {
        "categories": [{"title": "🐛 Bugfixes", "labels": ["bugfix"]}],
        "exclude-title-prefixes": ["Lokalise", "[Backport to stable]"],
    }
    prs = [
        FakePR(1, merged_at, "Fix a bug", labels=("bugfix",)),
        FakePR(2, merged_at, "Lokalise translations update"),
        FakePR(3, merged_at, "Lokalise: Translations update"),
        FakePR(4, merged_at, "[Backport to stable] 2.9.4"),
        FakePR(5, merged_at, "Add a feature"),
    ]

    categories, uncategorized = generate_notes.categorize_prs(prs, config)

    assert [pr.number for pr in categories["🐛 Bugfixes"]] == [1]
    assert [pr.number for pr in uncategorized] == [5]


def test_extract_frontend_changes_skips_lokalise_bullets(
    generate_notes: types.ModuleType,
) -> None:
    """Lokalise translation-sync bullets in frontend PR bodies are skipped."""
    merged_at = datetime(2026, 6, 1, tzinfo=UTC)
    prs = [
        FakePR(
            1,
            merged_at,
            "⬆️ Update music-assistant-frontend to 2.17.294",
            body=(
                "## What's changed\n"
                "- Fix the player card layout (by @someone in #100)\n"
                "- Lokalise translations update (by @github-actions[bot] in #101)\n"
                "- Lokalise: Translations update (by @marcelveldt in #102)\n"
            ),
        ),
    ]

    changes, contributors = generate_notes.extract_frontend_changes(prs)

    assert changes == ["- Fix the player card layout (by @someone in #100)"]
    assert contributors == {"someone"}
