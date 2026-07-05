"""Tests for the requirements file generator."""

from __future__ import annotations

import pytest

from scripts.gen_requirements_all import GIT_REPO_REGEX


@pytest.mark.parametrize(
    "line",
    [
        "git+https://github.com/owner/repo",
        "git+https://github.com/owner/repo@v1.0.0",
        "git+https://github.com/owner/some-repo@feature_branch",
    ],
)
def test_git_repo_regex_matches(line: str) -> None:
    """Valid git requirement lines are recognized."""
    assert GIT_REPO_REGEX.match(line)


@pytest.mark.parametrize(
    "line",
    [
        # chars like ^ [ ] were accidentally accepted by the former @-_ char range
        "git+https://github.com/owner/repo@v1^0",
        "git+https://github.com/owner/repo@[tag]",
        "git+https://example.com/repo; rm -rf /",
    ],
)
def test_git_repo_regex_rejects(line: str) -> None:
    """Lines with unexpected characters are not recognized as git requirements."""
    assert not GIT_REPO_REGEX.match(line)
