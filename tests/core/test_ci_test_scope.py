"""Tests for the CI test-scope selector (scripts/ci_test_scope.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_test_scope import decide, provider_name


@pytest.fixture(name="repo")
def repo_fixture(tmp_path: Path) -> Path:
    """Build a fake repo with one dir-based and one flat-file provider test."""
    (tmp_path / "tests" / "providers" / "spotify").mkdir(parents=True)
    (tmp_path / "tests" / "providers" / "spotify" / "test_init.py").touch()
    (tmp_path / "tests" / "providers" / "test_audible.py").touch()
    return tmp_path


def test_provider_name_recognizes_all_forms() -> None:
    """provider_name maps source dirs, test dirs and flat test files."""
    assert provider_name("music_assistant/providers/spotify/provider.py") == "spotify"
    assert provider_name("tests/providers/spotify/test_init.py") == "spotify"
    assert provider_name("tests/providers/test_audible.py") == "audible"
    assert provider_name("music_assistant/helpers/util.py") is None
    assert provider_name("tests/providers/__init__.py") is None


def test_single_provider_dir(repo: Path) -> None:
    """A change to one dir-based provider runs only its test dir."""
    assert decide(["music_assistant/providers/spotify/provider.py"], repo) == (
        "partial",
        ["tests/providers/spotify"],
    )


def test_flat_file_provider(repo: Path) -> None:
    """A provider whose tests are a flat file targets that file (both directions)."""
    expected = ("partial", ["tests/providers/test_audible.py"])
    assert decide(["music_assistant/providers/audible/provider.py"], repo) == expected
    assert decide(["tests/providers/test_audible.py"], repo) == expected


def test_multiple_providers_sorted(repo: Path) -> None:
    """Multiple changed providers are returned sorted by name."""
    mode, paths = decide(
        ["music_assistant/providers/spotify/provider.py", "tests/providers/test_audible.py"],
        repo,
    )
    assert mode == "partial"
    assert paths == ["tests/providers/test_audible.py", "tests/providers/spotify"]


@pytest.mark.parametrize(
    "path",
    [
        "music_assistant/helpers/util.py",
        "music_assistant/controllers/streams/controller.py",
        "tests/core/test_genres.py",
        "tests/providers/__init__.py",
        "music_assistant/translations/en.json",
        "pyproject.toml",
        ".github/workflows/test.yml",
        "scripts/ci_test_scope.py",
    ],
)
def test_shared_changes_force_full(repo: Path, path: str) -> None:
    """Shared code, deps, translations and CI changes run the full suite."""
    assert decide([path], repo) == ("full", [])


def test_provider_without_tests_forces_full(repo: Path) -> None:
    """A changed provider that has no tests falls back to the full suite."""
    assert decide(["music_assistant/providers/ghost/provider.py"], repo) == ("full", [])


def test_docs_only_skips(repo: Path) -> None:
    """A PR touching only docs / unrelated workflows runs nothing."""
    assert decide(["README.md", ".github/workflows/release.yml"], repo) == ("skip", [])


def test_no_changes_skips(repo: Path) -> None:
    """An empty change set runs nothing."""
    assert decide([], repo) == ("skip", [])
