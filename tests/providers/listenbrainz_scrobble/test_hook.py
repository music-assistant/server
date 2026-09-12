"""Tests for the ListenBrainz scrobbler's playback report hook."""

from __future__ import annotations

from typing import Self
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


class _FakeResponse:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, payload: dict[str, bool], json_error: Exception | None = None) -> None:
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, bool]:
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeSession:
    """Session whose GET returns a canned validate-token response, or raises a transport error."""

    def __init__(
        self,
        *,
        payload: dict[str, bool] | None = None,
        error: Exception | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload or {}
        self._error = error
        self._json_error = json_error
        self.requested_url: str | None = None
        self.requested_headers: dict[str, str] | None = None
        self.requested_timeout: object = None

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
        return _FakeResponse(self._payload, self._json_error)


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


async def test_setup_declares_the_scrobble_feature() -> None:
    """The provider tells the server it records plays, or it would never be handed any."""
    provider = await setup(_mass(), Mock(domain="listenbrainz_scrobble"), _config())

    assert isinstance(provider, ListenBrainzScrobbleProvider)
    assert ProviderFeature.SCROBBLE in provider.supported_features


async def test_the_handler_is_built_from_a_valid_token() -> None:
    """A valid stored token gives the provider a handler that reports to ListenBrainz."""
    session = _FakeSession(payload={"valid": True})
    provider = _provider({CONF_USER_TOKEN: "token"}, http_session=session)

    with patch("music_assistant.providers.listenbrainz_scrobble.ListenBrainz") as client_cls:
        await provider.handle_async_init()
    await provider.loaded_in_mass()

    # the check is authenticated and bounded by a finite timeout, and the client must
    # not repeat the blocking check
    assert session.requested_headers == {"Authorization": "Token token"}
    assert isinstance(session.requested_timeout, aiohttp.ClientTimeout)
    client_cls.return_value.set_auth_token.assert_called_once_with("token", check_validity=False)
    assert isinstance(provider._handler, ListenBrainzEventHandler)


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

    with patch("music_assistant.providers.listenbrainz_scrobble.ListenBrainz"):
        await provider.handle_async_init()

    assert session.requested_url == "https://lb.example.com/1/validate-token"


async def test_the_hook_forwards_the_report_to_the_handler() -> None:
    """A report handed to the provider reaches its scrobble handler."""
    provider = _provider()
    provider._handler = Mock(on_media_item_played=AsyncMock())
    report = _report()

    await provider.on_media_item_played(report)

    provider._handler.on_media_item_played.assert_awaited_once_with(report)
