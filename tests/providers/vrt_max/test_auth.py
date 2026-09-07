"""
Tests for how VRT authentication failures are classified.

Music Assistant treats a LoginFailed as a wrong password and deliberately never retries
it, so a transport failure reported that way would leave the provider switched off until
somebody reloads it by hand. Only credentials VRT actually rejects may be a login failure;
everything else has to be temporary so it gets retried.
"""

from __future__ import annotations

import logging
from typing import Any, Self
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable

from music_assistant.providers.vrt_max.auth import VrtMaxAuth


def _auth(username: str = "user@example.com") -> VrtMaxAuth:
    """Return an auth manager with credentials configured."""
    return VrtMaxAuth(MagicMock(), MagicMock(), logging.getLogger("test"), username, "secret")


class _FakeResponse:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, payload: Any = None) -> None:
        self._payload = payload if payload is not None else {}

    async def read(self) -> bytes:
        return b""

    async def json(self, **_kwargs: Any) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeSession:
    """Session whose GET/POST return canned responses, or raise a transport error."""

    def __init__(self, *, error: Exception | None = None, post_payload: Any = None) -> None:
        self._error = error
        self._post_payload = post_payload

    def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        if self._error:
            raise self._error
        return _FakeResponse()

    def post(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        if self._error:
            raise self._error
        return _FakeResponse(self._post_payload)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_no_credentials_is_a_login_failure() -> None:
    """With nothing configured there is nothing to retry, so this stays a login failure."""
    auth = VrtMaxAuth(MagicMock(), MagicMock(), logging.getLogger("test"), "", "")

    with pytest.raises(LoginFailed):
        await auth.get_access_token()


async def test_network_failure_during_login_is_temporary() -> None:
    """A network problem at login must be retryable, not read as a wrong password."""
    session = _FakeSession(error=aiohttp.ClientError("connection refused"))

    with (
        patch("music_assistant.providers.vrt_max.auth.create_clientsession", return_value=session),
        pytest.raises(ResourceTemporarilyUnavailable),
    ):
        await _auth().get_access_token()


async def test_timeout_during_login_is_temporary() -> None:
    """A timeout is a transport failure too."""
    session = _FakeSession(error=TimeoutError("timed out"))

    with (
        patch("music_assistant.providers.vrt_max.auth.create_clientsession", return_value=session),
        pytest.raises(ResourceTemporarilyUnavailable),
    ):
        await _auth().get_access_token()


async def test_missing_xsrf_cookie_is_temporary() -> None:
    """The SSO handshake not yielding its cookie is a server problem, not a bad password."""
    session = _FakeSession()

    with (
        patch("music_assistant.providers.vrt_max.auth.create_clientsession", return_value=session),
        pytest.raises(ResourceTemporarilyUnavailable),
    ):
        await _auth().get_access_token()


async def test_rejected_credentials_are_a_login_failure() -> None:
    """VRT explicitly rejecting the account is the one case MA should not retry."""
    session = _FakeSession(post_payload={"errorCode": 1, "errorMessage": "invalid credentials"})

    with (
        patch("music_assistant.providers.vrt_max.auth.create_clientsession", return_value=session),
        patch("music_assistant.providers.vrt_max.auth._cookie_value", return_value="xsrf-token"),
        pytest.raises(LoginFailed),
    ):
        await _auth().get_access_token()


async def test_player_token_transport_failure_is_temporary() -> None:
    """The token exchange failing on the network is retryable as well."""
    auth = _auth()
    auth._identity_token = "identity"
    auth._access_token = "access"
    auth._login_expiry = 2**31
    auth._session = _FakeSession(error=aiohttp.ClientError("boom"))  # type: ignore[assignment]

    with pytest.raises(ResourceTemporarilyUnavailable):
        await auth.get_player_token()


async def test_player_token_missing_from_response_is_temporary() -> None:
    """A response without the token is an unexpected shape, not a credential problem."""
    auth = _auth()
    auth._identity_token = "identity"
    auth._access_token = "access"
    auth._login_expiry = 2**31
    auth._session = _FakeSession(post_payload={})  # type: ignore[assignment]

    with pytest.raises(ResourceTemporarilyUnavailable):
        await auth.get_player_token()
