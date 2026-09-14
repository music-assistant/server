"""Tests for the ListenBrainz scrobbler's playback report hook."""

from __future__ import annotations

import logging
from typing import Any, Self
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidToken, SetupFailedError
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.listenbrainz_scrobble import (
    CONF_API_BASE_URL,
    CONF_USER_TOKEN,
    SUPPORTED_FEATURES,
    ListenBrainzEventHandler,
    ListenBrainzScrobbleProvider,
    setup,
)

# where the throttle/retry helper sleeps between attempts; patched so retries are instant
_SLEEP = "music_assistant.helpers.throttle_retry.asyncio.sleep"


class _FakeResponse:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        payload: dict[str, bool] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload or {}
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(Mock(), (), status=self.status)

    async def json(self) -> dict[str, bool]:
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeSession:
    """Records the validate-token GET and the submit-listens POST, or raises a transport error."""

    def __init__(
        self,
        *,
        payload: dict[str, bool] | None = None,
        error: Exception | None = None,
        json_error: Exception | None = None,
        post_error: Exception | None = None,
        post_statuses: list[int] | None = None,
        post_headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload or {}
        self._error = error
        self._json_error = json_error
        self._post_error = post_error
        # one status per POST attempt; the last value is reused once the list is exhausted
        self._post_statuses = list(post_statuses) if post_statuses else [200]
        self._post_headers = post_headers or {}
        self.requested_url: str | None = None
        self.requested_headers: dict[str, str] | None = None
        self.requested_timeout: object = None
        self.posted_url: str | None = None
        self.posted_headers: dict[str, str] | None = None
        self.posted_json: dict[str, Any] | None = None
        self.posted_timeout: object = None
        self.post_calls = 0

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: object = None,
        **_kwargs: object,
    ) -> _FakeResponse:
        self.requested_url = url
        self.requested_headers = headers
        self.requested_timeout = timeout
        if self._error is not None:
            raise self._error
        return _FakeResponse(payload=self._payload, json_error=self._json_error)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: object = None,
        **_kwargs: object,
    ) -> _FakeResponse:
        self.posted_url = url
        self.posted_headers = headers
        self.posted_json = json
        self.posted_timeout = timeout
        status = self._post_statuses[min(self.post_calls, len(self._post_statuses) - 1)]
        self.post_calls += 1
        if self._post_error is not None:
            raise self._post_error
        return _FakeResponse(status=status, headers=self._post_headers)


def _mass(
    setup_data: dict[str, str] | None = None, *, http_session: _FakeSession | None = None
) -> Mock:
    """Mock the server, holding the given setup data of the provider."""
    mass = Mock()
    mass.config.get.return_value = setup_data or {}
    mass.config.decrypt_string.side_effect = lambda value: value
    if http_session is not None:
        mass.http_session = http_session
    return mass


def _config() -> Mock:
    """Mock a provider config without values, so every option falls back to its default."""
    config = Mock()
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    return config


def _provider(
    setup_data: dict[str, str] | None = None, *, http_session: _FakeSession | None = None
) -> ListenBrainzScrobbleProvider:
    """Build a ListenBrainz provider instance with the given stored setup data."""
    return ListenBrainzScrobbleProvider(
        _mass(setup_data, http_session=http_session),
        Mock(domain="listenbrainz_scrobble"),
        _config(),
        SUPPORTED_FEATURES,
    )


def _handler(session: _FakeSession) -> ListenBrainzEventHandler:
    """Build a scrobble handler wired to the given fake http session."""
    return ListenBrainzEventHandler(
        _mass(http_session=session),
        "https://api.listenbrainz.org",
        "token",
        logging.getLogger(__name__),
        _config(),
    )


def _report() -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a fully played track."""
    return MediaItemPlaybackProgressReport(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        name="track",
        duration=180,
        seconds_played=180,
        fully_played=True,
        is_playing=False,
    )


def _playing_report() -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a track that is currently playing."""
    return MediaItemPlaybackProgressReport(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        name="track",
        artists=["artist a", "artist b"],
        album="album",
        duration=180,
        seconds_played=42,
        fully_played=False,
        is_playing=True,
    )


async def test_setup_declares_the_scrobble_feature() -> None:
    """The provider tells the server it records plays, or it would never be handed any."""
    provider = await setup(_mass(), Mock(domain="listenbrainz_scrobble"), _config())

    assert isinstance(provider, ListenBrainzScrobbleProvider)
    assert ProviderFeature.SCROBBLE in provider.supported_features


async def test_the_handler_is_built_from_a_valid_token() -> None:
    """A valid stored token gives the provider a handler that reports to ListenBrainz."""
    session = _FakeSession(payload={"valid": True})
    provider = _provider({CONF_USER_TOKEN: "token"}, http_session=session)

    await provider.handle_async_init()
    await provider.loaded_in_mass()

    # the check is authenticated and bounded by a finite timeout
    assert session.requested_headers == {"Authorization": "Token token"}
    assert isinstance(session.requested_timeout, aiohttp.ClientTimeout)
    assert isinstance(provider._handler, ListenBrainzEventHandler)
    assert provider._handler._token == "token"
    assert provider._handler._api_base_url == "https://api.listenbrainz.org"


async def test_an_invalid_token_fails_setup() -> None:
    """An invalid user token stops the provider loading with an auth error, not a retry loop."""
    provider = _provider(
        {CONF_USER_TOKEN: "token"}, http_session=_FakeSession(payload={"valid": False})
    )

    with pytest.raises(InvalidToken):
        await provider.handle_async_init()


async def test_an_unreachable_service_fails_setup() -> None:
    """A ListenBrainz that cannot be reached surfaces as a setup error, not a raw one."""
    provider = _provider(
        {CONF_USER_TOKEN: "token"},
        http_session=_FakeSession(error=aiohttp.ClientConnectionError()),
    )

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()


async def test_a_malformed_response_fails_setup() -> None:
    """A response that cannot be parsed surfaces as a setup error, not a raw one."""
    provider = _provider(
        {CONF_USER_TOKEN: "token"}, http_session=_FakeSession(json_error=ValueError("no json"))
    )

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()


async def test_an_unexpected_response_is_a_setup_error() -> None:
    """A 200 response without a valid flag stays retryable instead of failing auth."""
    provider = _provider({CONF_USER_TOKEN: "token"}, http_session=_FakeSession(payload={}))

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()


async def test_a_trailing_slash_in_the_base_url_is_normalized() -> None:
    """A configured base URL keeps a single slash before the validate-token path."""
    session = _FakeSession(payload={"valid": True})
    provider = _provider(
        {CONF_USER_TOKEN: "token", CONF_API_BASE_URL: "https://lb.example.com/"},
        http_session=session,
    )

    await provider.handle_async_init()

    assert session.requested_url == "https://lb.example.com/1/validate-token"


async def test_now_playing_is_submitted_with_a_bounded_request() -> None:
    """A now-playing update posts the listen with a finite timeout and no listened_at."""
    session = _FakeSession()
    handler = _handler(session)

    await handler._update_now_playing(_playing_report())

    assert session.posted_url == "https://api.listenbrainz.org/1/submit-listens"
    assert session.posted_headers == {"Authorization": "Token token"}
    assert isinstance(session.posted_timeout, aiohttp.ClientTimeout)
    assert session.posted_timeout.total == 30
    assert session.posted_json is not None
    assert session.posted_json["listen_type"] == "playing_now"
    listen = session.posted_json["payload"][0]
    # a now-playing update must never carry a listened_at timestamp
    assert "listened_at" not in listen
    metadata = listen["track_metadata"]
    assert metadata["track_name"] == "track"
    assert metadata["artist_name"] == "artist a, artist b"
    assert metadata["release_name"] == "album"
    assert metadata["additional_info"]["duration"] == 180
    assert metadata["additional_info"]["duration_played"] == 42


async def test_a_scrobble_is_submitted_with_a_bounded_request() -> None:
    """A completed play posts a single listen with a finite timeout and a listened_at."""
    session = _FakeSession()
    handler = _handler(session)

    await handler._scrobble(_report())

    assert session.posted_url == "https://api.listenbrainz.org/1/submit-listens"
    assert session.posted_headers == {"Authorization": "Token token"}
    assert isinstance(session.posted_timeout, aiohttp.ClientTimeout)
    assert session.posted_timeout.total == 30
    assert session.posted_json is not None
    assert session.posted_json["listen_type"] == "single"
    assert isinstance(session.posted_json["payload"][0]["listened_at"], int)


async def test_a_successful_submission_marks_the_track_scrobbled() -> None:
    """A submission that succeeds records the track so it is not scrobbled twice."""
    session = _FakeSession()
    handler = _handler(session)

    await handler.on_media_item_played(_report())

    assert session.post_calls == 1
    assert handler.last_scrobbled == "library://track/1"


@pytest.mark.parametrize(
    "session",
    [
        # a request that never connects
        _FakeSession(post_error=aiohttp.ClientConnectionError()),
        # a request that stalls until the timeout fires: the case this fix exists for
        _FakeSession(post_error=TimeoutError()),
        # a non-transient rejection (e.g. a revoked token)
        _FakeSession(post_statuses=[401]),
    ],
    ids=["connection-error", "timeout", "auth-error"],
)
async def test_a_failed_submission_is_swallowed(session: _FakeSession) -> None:
    """A transport, timeout, or non-retryable error while scrobbling is swallowed, not raised."""
    handler = _handler(session)

    await handler.on_media_item_played(_report())

    # these are not retried: one attempt, then the failure is logged and dropped
    assert session.post_calls == 1
    assert handler.last_scrobbled is None


@pytest.mark.parametrize("status", [429, 503], ids=["rate-limited", "server-error"])
async def test_a_transient_response_is_retried_then_swallowed(status: int) -> None:
    """A rate-limit or server error is retried with backoff, then dropped once retries run out."""
    session = _FakeSession(post_statuses=[status], post_headers={"Retry-After": "0"})
    handler = _handler(session)

    with patch(_SLEEP, new=AsyncMock()):
        await handler.on_media_item_played(_report())

    # retried up to the throttler's attempt budget, then swallowed without marking the track
    assert session.post_calls == 3
    assert handler.last_scrobbled is None


async def test_a_submission_recovers_after_a_retry() -> None:
    """A listen that is rate-limited once still lands (and is recorded) on the retry."""
    session = _FakeSession(post_statuses=[429, 200], post_headers={"Retry-After": "0"})
    handler = _handler(session)

    with patch(_SLEEP, new=AsyncMock()):
        await handler.on_media_item_played(_report())

    assert session.post_calls == 2
    assert handler.last_scrobbled == "library://track/1"


async def test_a_rate_limited_now_playing_update_is_not_retried() -> None:
    """A now-playing update is real-time, so a rate-limit reply is dropped, not retried."""
    session = _FakeSession(post_statuses=[429], post_headers={"Retry-After": "0"})
    handler = _handler(session)

    await handler.on_media_item_played(_playing_report())

    # sent once and dropped; retrying would only push a stale track
    assert session.post_calls == 1
    assert handler.currently_playing is None


async def test_the_hook_forwards_the_report_to_the_handler() -> None:
    """A report handed to the provider reaches its scrobble handler."""
    provider = _provider()
    provider._handler = Mock(on_media_item_played=AsyncMock())
    report = _report()

    await provider.on_media_item_played(report)

    provider._handler.on_media_item_played.assert_awaited_once_with(report)
