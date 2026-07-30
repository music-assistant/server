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
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable

from music_assistant.providers.overcast.helpers import (
    OvercastEpisodeState,
    OvercastSubscription,
)
from music_assistant.providers.overcast.provider import OvercastProvider


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
    provider._last_applied_ts = watermark
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
