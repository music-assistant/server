"""Tests for the dynamic-playlist track filter context helper."""

from __future__ import annotations

from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.helpers.track_filter import get_track_filter, track_filter


def _track(item_id: str) -> Track:
    return Track(
        item_id=item_id,
        provider="test",
        name=item_id,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def test_no_filter_by_default() -> None:
    """Outside a track_filter block no filter is published."""
    assert get_track_filter() is None


def test_filter_published_within_block_and_reset_after() -> None:
    """The filter is visible inside the block and cleared on exit."""
    with track_filter(lambda _track: True) as active:
        assert get_track_filter() is active
    assert get_track_filter() is None


def test_allows_records_consultation() -> None:
    """allows() returns the predicate result and flags that the filter was consulted."""
    keep, skip = _track("keep"), _track("skip")
    with track_filter(lambda track: track.item_id != "skip") as active:
        assert active.called is False
        assert active.allows(keep) is True
        assert active.allows(skip) is False
        assert active.called is True


def test_nested_filters_restore_outer() -> None:
    """A nested filter shadows the outer one and the outer is restored on exit."""
    with track_filter(lambda _track: True) as outer:
        with track_filter(lambda _track: False) as inner:
            assert get_track_filter() is inner
        assert get_track_filter() is outer
