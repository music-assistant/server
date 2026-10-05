"""Tests for parsing Overcast's extended OPML export."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.overcast.helpers import (
    OvercastSubscription,
    match_episode_state,
    parse_extended_opml,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "extended_export.opml"


@pytest.fixture(name="subscriptions")
def subscriptions_fixture() -> dict[str, OvercastSubscription]:
    """Parse the extended OPML fixture."""
    return parse_extended_opml(_FIXTURE_PATH.read_text())


def test_only_subscribed_feeds_are_returned(
    subscriptions: dict[str, OvercastSubscription],
) -> None:
    """Playlists and unsubscribed feeds are excluded, feeds are keyed by feed url."""
    assert set(subscriptions) == {
        "https://example.com/feed1.xml",
        "https://example.com/feed2.xml",
    }
    subscription = subscriptions["https://example.com/feed1.xml"]
    assert subscription.title == "Feed One"
    assert subscription.overcast_id == "111"


def test_episode_states(subscriptions: dict[str, OvercastSubscription]) -> None:
    """Episode playback attributes are parsed, absent attributes yield defaults."""
    episode_a, episode_b, episode_c = subscriptions["https://example.com/feed1.xml"].episodes

    assert episode_a.overcast_id == "1001"
    assert episode_a.enclosure_url == "https://cdn.example.com/ep-a.mp3?token=xyz"
    assert episode_a.progress_s == 123
    assert episode_a.played is False
    assert episode_a.user_updated_at == datetime(
        2026, 3, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=-7))
    )
    assert episode_a.pub_date == datetime(2026, 2, 27, 12, 0, 0, tzinfo=UTC)

    assert episode_b.played is True
    assert episode_b.progress_s is None

    assert episode_c.played is False
    assert episode_c.progress_s is None
    assert episode_c.user_updated_at is None


def test_single_episode_and_lenient_attribute_parsing(
    subscriptions: dict[str, OvercastSubscription],
) -> None:
    """A feed with one episode outline still parses; bad dates/ints become None."""
    (episode,) = subscriptions["https://example.com/feed2.xml"].episodes
    assert episode.pub_date is None
    assert episode.progress_s is None
    assert episode.played is True
    # a datetime without utc offset is normalized to an aware datetime
    assert episode.user_updated_at == datetime(2026, 3, 2, 0, 0, 0, tzinfo=UTC)


def test_empty_body_yields_no_subscriptions() -> None:
    """A structurally valid document without feeds parses to an empty result."""
    assert parse_extended_opml("<opml><body/></opml>") == {}


def test_malformed_document_raises_invalid_data_error() -> None:
    """A malformed XML document raises InvalidDataError."""
    with pytest.raises(InvalidDataError):
        parse_extended_opml("<opml><body>")


def test_match_episode_state(subscriptions: dict[str, OvercastSubscription]) -> None:
    """States match on exact enclosure url, with a query-stripped fallback."""
    subscription = subscriptions["https://example.com/feed1.xml"]

    exact = match_episode_state(subscription, "https://cdn.example.com/ep-b.mp3")
    assert exact is not None
    assert exact.overcast_id == "1002"

    # the fixture state carries ?token=xyz; a rotated/absent query still matches
    stripped = match_episode_state(subscription, "https://cdn.example.com/ep-a.mp3?token=other")
    assert stripped is not None
    assert stripped.overcast_id == "1001"

    assert match_episode_state(subscription, "https://cdn.example.com/unknown.mp3") is None
