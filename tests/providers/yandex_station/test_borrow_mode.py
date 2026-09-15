"""
Borrow mode: the Station uses a linked Yandex Music instance's account.

Covers spec 0001 — account-source dropdown, borrow-mode session
bootstrap (read-only against the linked instance) and the 401 re-derive
path. Own-mode behavior is pinned by the existing cascade suite.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr
from ya_passport_auth.ma import BORROW_SOURCE_OWN, BorrowedCredentialSource

from music_assistant.providers.yandex_station import setup
from music_assistant.providers.yandex_station.borrow import YandexMusicCredentialSource
from music_assistant.providers.yandex_station.constants import (
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_MUSIC_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)

from .test_provider_cascade import _make_provider, _updates

_MOD = "music_assistant.providers.yandex_station.provider"


def _ym_owner(token: str | None, x_token: str | None) -> mock.MagicMock:
    owner = mock.MagicMock()
    owner.domain = "yandex_music"
    owner.type = ProviderType.MUSIC
    owner.config.get_value = lambda key: {"token": token, "x_token": x_token}.get(key)
    return owner


def _ym_owner_setup_data(token: str | None, x_token: str | None) -> mock.MagicMock:
    owner = _ym_owner(None, None)
    owner.get_setup_value = lambda key, default=None: {
        "token": token,
        "x_token": x_token,
    }.get(key, default)
    return owner


def _borrow_provider(owner: mock.MagicMock | None) -> Any:
    provider = _make_provider(
        {
            CONF_YM_INSTANCE: "ym-1",
            CONF_MUSIC_TOKEN: None,
            CONF_X_TOKEN: None,
        }
    )
    object.__setattr__(provider.mass, "get_provider", mock.MagicMock(return_value=owner))
    return provider


class TestConfigEntries:
    """The account source and auth actions moved to the setup flow."""

    async def test_no_account_source_or_auth_actions(self) -> None:
        """The options surface exposes only genuine options, no source/auth entries."""
        provider = _make_provider({})
        entries = await provider.get_config_entries()
        keys = {e.key for e in entries}
        assert CONF_YM_INSTANCE not in keys
        assert keys.isdisjoint({"auth_device", "auth_qr", "auth_cookies", "clear_auth"})
        assert CONF_INTERCEPT_FEATURE_ENABLED in keys


class TestSetup:
    """setup() credential gating (reads from setup_data via get_setup_value)."""

    async def test_setup_allows_borrow_without_own_tokens(self) -> None:
        """Borrow mode passes setup() with no own tokens configured."""
        mass = mock.MagicMock()
        config = mock.MagicMock()
        with mock.patch(
            "music_assistant.providers.yandex_station.YandexStationProvider"
        ) as provider_cls:
            provider_cls.return_value.get_setup_value = mock.MagicMock(
                side_effect=lambda key, default=None: "ym-1" if key == CONF_YM_INSTANCE else default
            )
            await setup(mass, mock.MagicMock(), config)
        provider_cls.assert_called_once()

    async def test_setup_rejects_own_without_tokens(self) -> None:
        """Own mode with no music/x token fails setup() fast with LoginFailed."""
        mass = mock.MagicMock()
        config = mock.MagicMock()
        with mock.patch(
            "music_assistant.providers.yandex_station.YandexStationProvider"
        ) as provider_cls:
            provider_cls.return_value.get_setup_value = mock.MagicMock(
                side_effect=lambda key, default=None: (
                    BORROW_SOURCE_OWN if key == CONF_YM_INSTANCE else default
                )
            )
            with pytest.raises(LoginFailed):
                await setup(mass, mock.MagicMock(), config)


class TestBorrowInitSession:
    """Session bootstrap from the linked instance (read-only)."""

    async def test_builds_session_from_linked_tokens(self) -> None:
        """YandexSession gets the borrowed tokens; nothing is persisted."""
        provider = _borrow_provider(_ym_owner("test-music-ym", "test-x-ym"))
        with (
            mock.patch(f"{_MOD}.ClientSession") as http_cls,
            mock.patch(f"{_MOD}.PassportClient"),
            mock.patch(f"{_MOD}.YandexSession") as session_cls,
        ):
            http_cls.return_value = mock.MagicMock(closed=False, close=mock.AsyncMock())
            session_instance = mock.MagicMock()
            session_instance.login_token = mock.AsyncMock(return_value=True)
            session_cls.return_value = session_instance

            assert await provider._init_session() is True

        kwargs = session_cls.call_args.kwargs
        assert kwargs["x_token"].get_secret() == "test-x-ym"
        assert kwargs["music_token"].get_secret() == "test-music-ym"
        assert kwargs["refresh_token"] is None
        # Read-only: nothing persisted on either side.
        assert _updates(provider) == []

    async def test_builds_session_from_linked_setup_data_tokens(self) -> None:
        """Guided-flow setup data supplies borrowed credentials."""
        provider = _borrow_provider(_ym_owner_setup_data("test-music-ym", "test-x-ym"))
        with (
            mock.patch(f"{_MOD}.ClientSession") as http_cls,
            mock.patch(f"{_MOD}.PassportClient"),
            mock.patch(f"{_MOD}.YandexSession") as session_cls,
        ):
            http_cls.return_value = mock.MagicMock(closed=False, close=mock.AsyncMock())
            session_instance = mock.MagicMock()
            session_instance.login_token = mock.AsyncMock(return_value=True)
            session_cls.return_value = session_instance

            assert await provider._init_session() is True

        kwargs = session_cls.call_args.kwargs
        assert kwargs["x_token"].get_secret() == "test-x-ym"
        assert kwargs["music_token"].get_secret() == "test-music-ym"
        assert _updates(provider) == []

    def test_setup_data_uses_unavailable_linked_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guided-flow credentials never fall back to a different Yandex Music account."""
        linked_owner = _ym_owner_setup_data("test-music-linked", "test-x-linked")
        fallback_owner = _ym_owner_setup_data("test-music-fallback", "test-x-fallback")

        def get_provider(_instance_id: str, *, return_unavailable: bool = False) -> mock.MagicMock:
            return linked_owner if return_unavailable else fallback_owner

        mass = mock.MagicMock()
        mass.get_provider.side_effect = get_provider
        source = YandexMusicCredentialSource(mass, "ym-1")
        monkeypatch.setattr(BorrowedCredentialSource, "read_tokens", lambda _source: (None, None))

        music_token, x_token = source.read_tokens()

        assert music_token is not None
        assert x_token is not None
        assert music_token.get_secret() == "test-music-linked"
        assert x_token.get_secret() == "test-x-linked"
        mass.get_provider.assert_called_once_with("ym-1", return_unavailable=True)

    async def test_ym_not_loaded_is_transient(self) -> None:
        """A not-yet-loaded linked instance is a retryable condition."""
        provider = _borrow_provider(None)
        ready_event = mock.MagicMock()
        ready_event.wait = mock.AsyncMock(side_effect=TimeoutError)
        object.__setattr__(
            provider.mass,
            "get_provider_ready_event",
            mock.MagicMock(return_value=ready_event),
        )
        with (
            pytest.raises(ResourceTemporarilyUnavailable, match="not loaded"),
        ):
            await provider._init_session()
        assert _updates(provider) == []

    async def test_waits_for_linked_instance_during_startup(self) -> None:
        """Station waits briefly when Yandex Music is still loading."""
        owner = _ym_owner_setup_data("test-music-ym", "test-x-ym")
        provider = _borrow_provider(None)
        provider.mass.get_provider.side_effect = [None, owner, owner, owner, owner]
        ready_event = mock.MagicMock()
        ready_event.wait = mock.AsyncMock(return_value=True)
        object.__setattr__(
            provider.mass,
            "get_provider_ready_event",
            mock.MagicMock(return_value=ready_event),
        )
        with (
            mock.patch(f"{_MOD}.ClientSession") as http_cls,
            mock.patch(f"{_MOD}.PassportClient"),
            mock.patch(f"{_MOD}.YandexSession") as session_cls,
        ):
            http_cls.return_value = mock.MagicMock(closed=False, close=mock.AsyncMock())
            session_instance = mock.MagicMock()
            session_instance.login_token = mock.AsyncMock(return_value=True)
            session_cls.return_value = session_instance

            assert await provider._init_session() is True

        ready_event.wait.assert_awaited_once()
        kwargs = session_cls.call_args.kwargs
        assert kwargs["music_token"].get_secret() == "test-music-ym"

    async def test_waits_for_provider_ready_event_without_polling(self) -> None:
        """Startup waits on MA's readiness event before retrying the exact instance."""
        owner = _ym_owner_setup_data("test-music-ym", "test-x-ym")
        provider = _borrow_provider(None)
        provider.mass.get_provider.side_effect = [None, *([owner] * 10)]
        ready_event = mock.MagicMock()
        ready_event.wait = mock.AsyncMock(return_value=True)
        get_provider_ready_event = mock.MagicMock(return_value=ready_event)
        object.__setattr__(provider.mass, "get_provider_ready_event", get_provider_ready_event)
        source = provider._borrow_source
        assert source is not None

        music_token, x_token = await provider._resolve_borrowed_tokens()

        ready_event.wait.assert_awaited_once()
        get_provider_ready_event.assert_called_once_with("yandex_music")
        assert music_token.get_secret() == "test-music-ym"
        assert x_token is not None
        assert x_token.get_secret() == "test-x-ym"

    async def test_passport_failure_is_not_retried_as_provider_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient token refresh failure must reach MA's normal retry handling."""
        provider = _borrow_provider(_ym_owner("test-music-ym", "test-x-ym"))
        source = provider._borrow_source
        assert source is not None
        read_tokens = mock.MagicMock(
            return_value=(SecretStr("test-music-ym"), SecretStr("test-x-ym"))
        )
        resolve_music_token = mock.AsyncMock(
            side_effect=ResourceTemporarilyUnavailable("Passport unavailable")
        )
        monkeypatch.setattr(source, "read_tokens", read_tokens)
        monkeypatch.setattr(source, "resolve_music_token", resolve_music_token)

        with pytest.raises(ResourceTemporarilyUnavailable, match="Passport unavailable"):
            await provider._resolve_borrowed_tokens()

        read_tokens.assert_called_once()
        resolve_music_token.assert_awaited_once()


class TestBorrowSilentReauth:
    """401 recovery re-derives cookies without rotation."""

    async def test_rereads_linked_tokens_and_refreshes_cookies(self) -> None:
        """Reauth re-reads owner tokens and refreshes cookies, never rotates."""
        provider = _borrow_provider(_ym_owner("test-music-ym", "test-x-ym"))
        provider._session = mock.MagicMock()
        provider._session.login_token = mock.AsyncMock(return_value=True)

        with mock.patch("ya_passport_auth.ma.cascade.refresh_credentials") as rotate:
            assert await provider._silent_reauth() is True

        rotate.assert_not_called()
        assert provider._session.x_token.get_secret() == "test-x-ym"
        assert provider._session.music_token.get_secret() == "test-music-ym"
        provider._session.login_token.assert_awaited()
        assert _updates(provider) == []
