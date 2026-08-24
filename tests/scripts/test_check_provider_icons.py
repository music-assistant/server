"""Tests for the provider icon size check."""

from scripts.check_provider_icons import (
    BASELINE_PATH,
    MAX_SIZE_BY_SUFFIX,
    find_oversized_icons,
    main,
)
from scripts.lint_baseline import load_baseline


def test_size_budgets() -> None:
    """The icon budgets are 5 KB for svg and 20 KB for png."""
    assert MAX_SIZE_BY_SUFFIX == {".svg": 5 * 1024, ".png": 20 * 1024}


def test_oversized_icons_match_baseline() -> None:
    """Every currently-oversized icon is grandfathered in the baseline (no new offenders)."""
    assert set(find_oversized_icons()) == set(load_baseline(BASELINE_PATH))


def test_check_passes_against_baseline() -> None:
    """The check passes because the tree matches its baseline exactly."""
    assert main([]) == 0
