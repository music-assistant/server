# ruff: noqa: ARG001, PLC0415
"""
Tests for provider/auth_session.py — Passport session helpers.

The cached authenticator is the join point between the provider's cached
``x_token`` and ya-dialogs-api's ``AuthenticatorCM`` contract: it must
populate Passport cookies via ``refresh_passport_cookies`` and refuse any
fallback to interactive Device Flow (mixing the two would let stale tokens
silently re-trigger user-code prompts mid-pipeline).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ya_passport_auth.exceptions import InvalidCredentialsError

from music_assistant.providers.yandex_alice import auth_session

if TYPE_CHECKING:
    from ya_passport_auth import SecretStr


class TestMakeCachedAuthenticatorValidation:
    """make_cached_authenticator rejects invalid input synchronously."""

    def test_empty_token_raises_value_error(self) -> None:
        """Empty x_token → ValueError before any network call is set up."""
        with pytest.raises(ValueError, match="empty"):
            auth_session.make_cached_authenticator("")

    def test_returns_callable(self) -> None:
        """Non-empty x_token → returns a callable (the AuthenticatorCM factory)."""
        factory = auth_session.make_cached_authenticator("token-123")
        assert callable(factory)


class TestCachedAuthenticatedSession:
    """cached_authenticated_session yields an aiohttp session with cookies populated."""

    @pytest.mark.asyncio
    async def test_calls_refresh_passport_cookies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inside the CM, refresh_passport_cookies is invoked exactly once with the token."""
        captured_token: list[str] = []

        async def _fake_refresh(self: Any, x_token: SecretStr) -> None:
            captured_token.append(x_token.get_secret())

        # Patch PassportClient.refresh_passport_cookies on the class to avoid touching network.
        monkeypatch.setattr(
            "ya_passport_auth.PassportClient.refresh_passport_cookies", _fake_refresh
        )

        async with auth_session.cached_authenticated_session("test-x-token") as session:
            # Session is a real aiohttp ClientSession — has a closed property.
            assert hasattr(session, "closed")

        assert captured_token == ["test-x-token"]

    @pytest.mark.asyncio
    async def test_propagates_invalid_credentials_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Yandex 401 → InvalidCredentialsError propagates out, no Device Flow fallback."""

        async def _failing_refresh(self: Any, x_token: SecretStr) -> None:
            msg = "x_token rejected"
            raise InvalidCredentialsError(msg)

        monkeypatch.setattr(
            "ya_passport_auth.PassportClient.refresh_passport_cookies", _failing_refresh
        )

        with pytest.raises(InvalidCredentialsError, match="rejected"):
            async with auth_session.cached_authenticated_session("expired-token"):
                pytest.fail("body should not execute on auth failure")

    @pytest.mark.asyncio
    async def test_empty_token_rejected_synchronously(self) -> None:
        """Empty x_token → ValueError before any aiohttp session is created."""
        with pytest.raises(ValueError, match="empty"):
            async with auth_session.cached_authenticated_session(""):
                pytest.fail("CM body should never execute")


class TestPassportClientSession:
    """passport_client_session yields a PassportClient and closes it on exit."""

    @pytest.mark.asyncio
    async def test_yields_client_and_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contextmanager yields a PassportClient that gets closed on exit."""
        close_called: list[bool] = []

        class _FakePassportClient:
            def __init__(self) -> None:
                pass

            async def close(self) -> None:
                close_called.append(True)

        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_create(config: Any = None) -> AsyncIterator[_FakePassportClient]:
            client = _FakePassportClient()
            try:
                yield client
            finally:
                await client.close()

        # Patch PassportClient.create to yield our fake.
        monkeypatch.setattr("ya_passport_auth.PassportClient.create", _fake_create)

        async with auth_session.passport_client_session() as client:
            assert isinstance(client, _FakePassportClient)

        assert close_called == [True]


class TestMakeCachedAuthenticatorFactory:
    """The factory returned by make_cached_authenticator yields cookie-loaded sessions."""

    @pytest.mark.asyncio
    async def test_factory_yields_authenticated_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each call to the factory opens a fresh refresh_passport_cookies-loaded session."""
        refresh_calls: list[str] = []

        async def _fake_refresh(self: Any, x_token: SecretStr) -> None:
            refresh_calls.append(x_token.get_secret())

        monkeypatch.setattr(
            "ya_passport_auth.PassportClient.refresh_passport_cookies", _fake_refresh
        )

        factory = auth_session.make_cached_authenticator("good-token")

        # Two invocations open two independent CMs.
        async with factory() as session1:
            assert hasattr(session1, "closed")
        async with factory() as session2:
            assert hasattr(session2, "closed")

        assert refresh_calls == ["good-token", "good-token"]
