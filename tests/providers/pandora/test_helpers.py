"""Tests for the Pandora provider's pure helpers."""

from __future__ import annotations

from music_assistant.providers.pandora.helpers import read_account_flags


def test_flags_are_returned_as_a_set() -> None:
    """A normal login response yields the account's capability flags."""
    response = {"config": {"flags": ["highQualityStreamingAvailable", "adFreeSkip"]}}
    assert read_account_flags(response) == {"highQualityStreamingAvailable", "adFreeSkip"}


def test_missing_config_yields_no_flags() -> None:
    """A response without a config block reports no capabilities, not a crash."""
    assert read_account_flags({}) == set()


def test_null_config_yields_no_flags() -> None:
    """Pandora sends config: null on some accounts; that is absence, not a type error."""
    assert read_account_flags({"config": None}) == set()


def test_null_flags_yield_no_flags() -> None:
    """flags: null is the present-but-null case the two-arg get would miss."""
    assert read_account_flags({"config": {"flags": None}}) == set()


def test_free_account_flags_do_not_include_high_quality() -> None:
    """Measured free-tier payload: ad-supported replay/skip, and no high-quality streaming."""
    response = {"config": {"flags": ["adSupportedReplay", "adSupportedSkip"]}}
    assert "highQualityStreamingAvailable" not in read_account_flags(response)
