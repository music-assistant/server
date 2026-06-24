"""Tests for the provider icon size check."""

from scripts.check_provider_icons import (
    BASELINE_PATH,
    MAX_ICON_SIZE,
    find_oversized_icons,
    main,
)
from scripts.lint_baseline import load_baseline


def test_size_budget_is_5kb() -> None:
    """The icon budget is exactly 5 KB."""
    assert MAX_ICON_SIZE == 5 * 1024


def test_oversized_icons_match_baseline() -> None:
    """Every currently-oversized icon is grandfathered in the baseline (no new offenders)."""
    assert set(find_oversized_icons()) == set(load_baseline(BASELINE_PATH))


def test_check_passes_against_baseline() -> None:
    """The check passes because the tree matches its baseline exactly."""
    assert main([]) == 0
