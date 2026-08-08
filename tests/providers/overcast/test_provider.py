"""Tests for the Overcast provider's login, OPML fetch and resume position logic."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from functools import partial
from http.cookies import SimpleCookie
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)

from music_assistant.providers.overcast import provider as overcast_provider
from music_assistant.providers.overcast.helpers import (
    OvercastEpisodeState,
    OvercastSubscription,
)
from music_assistant.providers.overcast.provider import OvercastProvider

FEED_A = "https://example.com/feed1.xml"
FEED_B = "https://example.com/feed2.xml"


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        set_cookie: str | None = None,
        body: str = "",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.cookies: SimpleCookie = SimpleCookie()
        if set_cookie is not None:
            self.cookies.load(set_cookie)
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Fake aiohttp session returning canned responses in order."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, str]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeRequestContext:
        self.requests.append(("GET", url))
        return _FakeRequestContext(self._responses.pop(0))

    def post(self, url: str, **kwargs: Any) -> _FakeRequestContext:
        self.requests.append(("POST", url))
        return _FakeRequestContext(self._responses.pop(0))


def _provider(responses: list[_FakeResponse]) -> MagicMock:
    """Build a provider stub with a fake http session and recorded setup data."""
    provider = MagicMock()
    provider.http_session = _FakeSession(responses)
    provider.get_setup_value = Mock(return_value="secret")
    provider.setup_data_updates = {}
    provider._update_setup_data = Mock(
        side_effect=lambda key, value: provider.setup_data_updates.update({key: value})
    )
    # real rate limit bookkeeping, so a MagicMock is not mistaken for an active window
    provider._rate_limited_until = None
    provider._rate_limit_remaining = partial(OvercastProvider._rate_limit_remaining, provider)
    return provider


def _episode_state(**overrides: Any) -> OvercastEpisodeState:
    state = OvercastEpisodeState(
        overcast_id="1001",
        title="Episode A",
        enclosure_url="https://cdn.example.com/ep-a.mp3",
        pub_date=None,
        progress_s=123,
        played=False,
        user_updated_at=datetime(2026, 3, 1, 15, 0, 0, tzinfo=UTC),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _subscription(*states: OvercastEpisodeState) -> OvercastSubscription:
    return OvercastSubscription(
        xml_url="https://example.com/feed1.xml",
        title="Feed One",
        overcast_id="111",
        episodes=list(states),
    )


async def test_login_success_persists_session_cookie() -> None:
    """A 302 to /podcasts with the session cookie logs in and persists the cookie."""
    provider = _provider(
        [_FakeResponse(status=302, headers={"Location": "/podcasts"}, set_cookie="o=abc123")]
    )
    await OvercastProvider._login(cast("OvercastProvider", provider))
    assert provider.setup_data_updates == {"session_cookie": "abc123"}


async def test_login_rejected_credentials_raise_login_failed() -> None:
    """A re-rendered login form (HTTP 200) raises LoginFailed."""
    provider = _provider([_FakeResponse(status=200)])
    with pytest.raises(LoginFailed):
        await OvercastProvider._login(cast("OvercastProvider", provider))
    assert provider.setup_data_updates == {}


async def test_opml_request_returns_document() -> None:
    """A successful export request returns the raw OPML text."""
    provider = _provider([_FakeResponse(status=200, body="<opml/>")])
    result = await OvercastProvider._request_opml(cast("OvercastProvider", provider))
    assert result == "<opml/>"


async def test_opml_rate_limit_maps_to_resource_temporarily_unavailable() -> None:
    """A 429 with Retry-After maps to ResourceTemporarilyUnavailable with that backoff."""
    provider = _provider([_FakeResponse(status=429, headers={"Retry-After": "900"})])
    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        await OvercastProvider._request_opml(cast("OvercastProvider", provider))
    assert err.value.backoff_time == 900


async def test_rate_limit_spends_no_further_requests_while_active() -> None:
    """After a 429 the export is not requested again until the window has elapsed."""
    provider = _provider([_FakeResponse(status=429, headers={"Retry-After": "900"})])
    typed_provider = cast("OvercastProvider", provider)
    with pytest.raises(ResourceTemporarilyUnavailable):
        await OvercastProvider._request_opml(typed_provider)
    assert len(provider.http_session.requests) == 1

    # the recorded window must fail the call outright, leaving the request count untouched
    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        await OvercastProvider._request_opml(typed_provider)
    assert len(provider.http_session.requests) == 1
    assert 0 < err.value.backoff_time <= 900


async def test_rate_limit_window_expiry_allows_a_new_request() -> None:
    """Once the window has passed the export is requested again."""
    provider = _provider([_FakeResponse(status=200, body="<opml/>")])
    provider._rate_limited_until = time.monotonic() - 1
    result = await OvercastProvider._request_opml(cast("OvercastProvider", provider))
    assert result == "<opml/>"
    assert provider._rate_limited_until is None


async def test_opml_rejected_session_returns_none() -> None:
    """A redirect to the login page signals an expired session by returning None."""
    provider = _provider([_FakeResponse(status=302, headers={"Location": "/login"})])
    assert await OvercastProvider._request_opml(cast("OvercastProvider", provider)) is None


async def test_opml_fetch_relogins_once_on_expired_session() -> None:
    """An expired session triggers exactly one re-login before the retry."""
    provider = _provider(
        [
            _FakeResponse(status=302, headers={"Location": "/login"}),
            _FakeResponse(status=302, headers={"Location": "/podcasts"}, set_cookie="o=new"),
            _FakeResponse(status=200, body="<opml/>"),
        ]
    )
    typed_provider = cast("OvercastProvider", provider)
    provider._request_opml = partial(OvercastProvider._request_opml, typed_provider)
    provider._login = partial(OvercastProvider._login, typed_provider)
    # call the undecorated function to bypass the cache layer
    fetch_opml = OvercastProvider._fetch_opml_text.__wrapped__  # type: ignore[attr-defined]
    result = await fetch_opml(typed_provider)
    assert result == "<opml/>"
    assert provider.setup_data_updates == {"session_cookie": "new"}


async def test_resume_position_maps_overcast_state() -> None:
    """Overcast progress maps to (fully_played, position_ms, timestamp)."""
    state = _episode_state()
    provider = MagicMock()
    provider._get_episode_stream_url = AsyncMock(return_value=state.enclosure_url)
    provider._get_subscription = AsyncMock(return_value=_subscription(state))
    result = await OvercastProvider.get_resume_position(
        cast("OvercastProvider", provider),
        "https://example.com/feed1.xml guid-a",
        MediaType.PODCAST_EPISODE,
    )
    assert result == (False, 123000, state.user_updated_at)


async def test_resume_position_without_progress_falls_back_to_playlog() -> None:
    """Without any known Overcast progress the provider defers to MA's playlog."""
    state = _episode_state(progress_s=None, played=False)
    provider = MagicMock()
    provider._get_episode_stream_url = AsyncMock(return_value=state.enclosure_url)
    provider._get_subscription = AsyncMock(return_value=_subscription(state))
    with pytest.raises(NotImplementedError):
        await OvercastProvider.get_resume_position(
            cast("OvercastProvider", provider),
            "https://example.com/feed1.xml guid-a",
            MediaType.PODCAST_EPISODE,
        )


async def test_apply_playback_states_respects_watermark() -> None:
    """Only states newer than the watermark are pushed to the playlog."""
    watermark = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    old_state = _episode_state(
        overcast_id="old",
        enclosure_url="https://cdn.example.com/ep-old.mp3",
        user_updated_at=datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC),
    )
    new_state = _episode_state(
        overcast_id="new",
        enclosure_url="https://cdn.example.com/ep-new.mp3",
        played=True,
        user_updated_at=datetime(2026, 3, 2, 0, 0, 0, tzinfo=UTC),
    )
    parsed_podcast = {
        "title": "Feed One",
        "cover_url": None,
        "episodes": [
            {"title": "Old", "guid": "g-old", "enclosures": [{"url": old_state.enclosure_url}]},
            {"title": "New", "guid": "g-new", "enclosures": [{"url": new_state.enclosure_url}]},
        ],
    }
    provider = MagicMock()
    provider._feed_watermarks = {FEED_A: watermark}
    provider.instance_id = "overcast--test"
    provider.domain = "overcast"
    provider.mass.music.mark_item_played = AsyncMock()
    result = await OvercastProvider._apply_playback_states(
        cast("OvercastProvider", provider),
        "https://example.com/feed1.xml",
        _subscription(old_state, new_state),
        parsed_podcast,
    )
    assert result == new_state.user_updated_at
    provider.mass.music.mark_item_played.assert_awaited_once()
    call = provider.mass.music.mark_item_played.await_args
    assert call.kwargs["fully_played"] is True
    assert call.kwargs["seconds_played"] == 123
    # the playlog flag is sticky, so a sync must never claim the user asked for the play
    assert call.kwargs["user_initiated"] is False


async def test_apply_playback_states_ignores_other_feeds_watermark() -> None:
    """A feed is gated by its own watermark only, never by one of another feed."""
    state = _episode_state(played=True, user_updated_at=datetime(2026, 2, 1, tzinfo=UTC))
    parsed_podcast = {
        "title": "Feed One",
        "cover_url": None,
        "episodes": [{"title": "A", "guid": "g-a", "enclosures": [{"url": state.enclosure_url}]}],
    }
    provider = MagicMock()
    provider._feed_watermarks = {FEED_B: datetime(2026, 3, 1, tzinfo=UTC)}
    provider.instance_id = "overcast--test"
    provider.domain = "overcast"
    provider.mass.music.mark_item_played = AsyncMock()
    result = await OvercastProvider._apply_playback_states(
        cast("OvercastProvider", provider),
        FEED_A,
        _subscription(state),
        parsed_podcast,
    )
    assert result == state.user_updated_at
    provider.mass.music.mark_item_played.assert_awaited_once()


def _in_progress_provider(local_position_ms: int) -> MagicMock:
    """Build a provider stub whose playlog already holds the given local position."""
    provider = MagicMock()
    provider._feed_watermarks = {}
    provider.instance_id = "overcast--test"
    provider.domain = "overcast"
    provider.mass.music.get_resume_position = AsyncMock(return_value=(False, local_position_ms))
    provider.mass.music.mark_item_played = AsyncMock()
    return provider


async def test_apply_playback_states_keeps_further_local_progress() -> None:
    """An Overcast position behind the one MA recorded itself is not written to the playlog."""
    state = _episode_state(progress_s=1200)
    parsed_podcast = {
        "title": "Feed One",
        "cover_url": None,
        "episodes": [{"title": "A", "guid": "g-a", "enclosures": [{"url": state.enclosure_url}]}],
    }
    provider = _in_progress_provider(local_position_ms=1800 * 1000)
    result = await OvercastProvider._apply_playback_states(
        cast("OvercastProvider", provider),
        FEED_A,
        _subscription(state),
        parsed_podcast,
    )
    assert result is None
    provider.mass.music.mark_item_played.assert_not_awaited()


async def test_apply_playback_states_applies_progress_ahead_of_the_playlog() -> None:
    """An Overcast position ahead of MA's own record is applied as usual."""
    state = _episode_state(progress_s=1200)
    parsed_podcast = {
        "title": "Feed One",
        "cover_url": None,
        "episodes": [{"title": "A", "guid": "g-a", "enclosures": [{"url": state.enclosure_url}]}],
    }
    provider = _in_progress_provider(local_position_ms=600 * 1000)
    result = await OvercastProvider._apply_playback_states(
        cast("OvercastProvider", provider),
        FEED_A,
        _subscription(state),
        parsed_podcast,
    )
    assert result == state.user_updated_at
    assert provider.mass.music.mark_item_played.await_args.kwargs["seconds_played"] == 1200


def _sync_provider(applied: datetime, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Build a provider stub syncing two feeds, of which the second one is unreachable."""
    provider = MagicMock()
    provider._feed_watermarks = {}
    provider.instance_id = "overcast--test"
    provider.domain = "overcast"
    provider.max_episodes = 0
    provider._get_opml_subscriptions = AsyncMock(
        return_value={FEED_A: _subscription(), FEED_B: _subscription()}
    )
    provider._apply_playback_states = AsyncMock(return_value=applied)
    provider.mass.cache.set = AsyncMock()
    provider._store_watermarks = partial(OvercastProvider._store_watermarks, provider)

    async def _parsed_feed(*, feed_url: str, **_kwargs: Any) -> dict[str, Any]:
        if feed_url == FEED_B:
            raise MediaNotFoundError("feed is down")
        return {"title": "Feed One", "episodes": []}

    monkeypatch.setattr(overcast_provider, "refresh_cached_podcast", _parsed_feed)
    monkeypatch.setattr(overcast_provider, "parse_podcast", Mock(return_value=MagicMock()))
    return provider


async def test_failing_feed_does_not_advance_another_feeds_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable feed keeps its own (absent) watermark while others advance."""
    applied = datetime(2026, 3, 2, tzinfo=UTC)
    provider = _sync_provider(applied, monkeypatch)
    podcasts = [
        podcast
        async for podcast in OvercastProvider.get_library_podcasts(
            cast("OvercastProvider", provider)
        )
    ]
    assert len(podcasts) == 1
    assert provider._feed_watermarks == {FEED_A: applied}
    assert provider.mass.cache.set.await_args.kwargs["data"] == {FEED_A: applied.isoformat()}


async def test_watermark_of_unsubscribed_feed_is_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feed that is no longer subscribed keeps its watermark, so it can be re-subscribed."""
    applied = datetime(2026, 3, 2, tzinfo=UTC)
    unsubscribed = datetime(2026, 1, 1, tzinfo=UTC)
    provider = _sync_provider(applied, monkeypatch)
    provider._feed_watermarks = {"https://example.com/gone.xml": unsubscribed}
    async for _ in OvercastProvider.get_library_podcasts(cast("OvercastProvider", provider)):
        pass
    assert provider.mass.cache.set.await_args.kwargs["data"] == {
        "https://example.com/gone.xml": unsubscribed.isoformat(),
        FEED_A: applied.isoformat(),
    }
