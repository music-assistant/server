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
from ya_passport_auth.ma import BORROW_SOURCE_OWN, BorrowedCredentialSource

from music_assistant.providers.yandex_station import setup
from music_assistant.providers.yandex_station.constants import (
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_MUSIC_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
)

from .test_provider_cascade import _make_provider, _updates

_MOD = "music_assistant.providers.yandex_station.provider"


class _YandexMusicOwner:
    """
    Linked Yandex Music instance following MA's ``get_setup_value`` contract.

    Setup data wins when the key is present (including an explicit None);
    otherwise the legacy config value is returned.
    """

    domain = "yandex_music"
    type = ProviderType.MUSIC

    def __init__(
        self,
        *,
        setup_data: dict[str, str | None] | None = None,
        config: dict[str, str | None] | None = None,
    ) -> None:
        self.setup_data = setup_data or {}
        self._config = config or {}
        self.reads: list[str] = []

    def get_setup_value(self, key: str, default: object = None) -> object:
        self.reads.append(key)
        if key in self.setup_data:
            return self.setup_data[key]
        return self._config.get(key, default)


def _ym_owner(token: str | None, x_token: str | None) -> _YandexMusicOwner:
    """Owner whose tokens live only in its legacy config."""
    return _YandexMusicOwner(config={"token": token, "x_token": x_token})


def _ym_owner_setup_data(token: str | None, x_token: str | None) -> _YandexMusicOwner:
    """Owner whose tokens live in setup data (guided setup / rotation)."""
    return _YandexMusicOwner(setup_data={"token": token, "x_token": x_token})


def _borrow_provider(owner: _YandexMusicOwner | None) -> Any:
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

    async def test_rotated_setup_data_tokens_win_over_stale_config(self) -> None:
        """Tokens rotated into setup data replace stale ones left in the owner's config."""
        owner = _YandexMusicOwner(
            setup_data={"token": "test-music-rotated", "x_token": "test-x-rotated"},
            config={"token": "test-music-stale", "x_token": "test-x-stale"},
        )
        provider = _borrow_provider(owner)
        with (
            mock.patch(f"{_MOD}.ClientSession") as http_cls,
            mock.patch(f"{_MOD}.PassportClient"),
            mock.patch(f"{_MOD}.YandexSession") as session_cls,
        ):
            http_cls.return_value = mock.MagicMock(closed=False, close=mock.AsyncMock())
            session_cls.return_value.login_token = mock.AsyncMock(return_value=True)

            assert await provider._init_session() is True

        kwargs = session_cls.call_args.kwargs
        assert kwargs["music_token"].get_secret() == "test-music-rotated"
        assert kwargs["x_token"].get_secret() == "test-x-rotated"

    def test_borrow_source_is_the_shared_library_source(self) -> None:
        """Station uses the library's borrowed-credentials source, not a local subclass."""
        provider = _borrow_provider(_ym_owner_setup_data("test-music-ym", "test-x-ym"))

        assert type(provider._borrow_source) is BorrowedCredentialSource
        assert provider._borrow_source.instance_id == "ym-1"

    async def test_startup_reads_owner_tokens_once(self) -> None:
        """Session bootstrap reads the linked owner's token pair exactly once."""
        owner = _ym_owner_setup_data("test-music-ym", "test-x-ym")
        provider = _borrow_provider(owner)

        creds = await provider._resolve_borrowed_tokens()

        assert sorted(owner.reads) == ["token", "x_token"]
        assert creds.music_token.get_secret() == "test-music-ym"
        assert creds.x_token is not None
        assert creds.x_token.get_secret() == "test-x-ym"

    async def test_reads_exact_linked_instance_even_if_unavailable(self) -> None:
        """Credentials never come from a different Yandex Music account."""
        linked_owner = _ym_owner_setup_data("test-music-linked", "test-x-linked")
        fallback_owner = _ym_owner_setup_data("test-music-fallback", "test-x-fallback")
        provider = _borrow_provider(None)

        def get_provider(_instance_id: str, *, return_unavailable: bool = False) -> object:
            return linked_owner if return_unavailable else fallback_owner

        provider.mass.get_provider.side_effect = get_provider

        creds = await provider._resolve_borrowed_tokens()

        assert creds.music_token.get_secret() == "test-music-linked"
        assert all(
            call.kwargs.get("return_unavailable") is True
            for call in provider.mass.get_provider.call_args_list
        )

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
        with pytest.raises(ResourceTemporarilyUnavailable, match="not loaded"):
            await provider._init_session()
        assert _updates(provider) == []

    async def test_waits_for_ready_event_then_reads_once(self) -> None:
        """While Yandex Music loads, Station waits on MA's readiness event, then reads once."""
        owner = _ym_owner_setup_data("test-music-ym", "test-x-ym")
        provider = _borrow_provider(None)
        provider.mass.get_provider.side_effect = [None, owner]
        ready_event = mock.MagicMock()
        ready_event.wait = mock.AsyncMock(return_value=True)
        get_provider_ready_event = mock.MagicMock(return_value=ready_event)
        object.__setattr__(provider.mass, "get_provider_ready_event", get_provider_ready_event)

        creds = await provider._resolve_borrowed_tokens()

        get_provider_ready_event.assert_called_once_with("yandex_music")
        ready_event.wait.assert_awaited_once()
        assert creds.music_token.get_secret() == "test-music-ym"
        assert sorted(owner.reads) == ["token", "x_token"]

    async def test_selected_instance_still_missing_is_left_to_core_retry(self) -> None:
        """
        Another account becoming ready does not trigger polling for the selected one.

        The selected instance is checked once after the readiness event; if it is
        still missing, the transient error reaches MA's provider-load retry.
        """
        provider = _borrow_provider(None)
        ready_event = mock.MagicMock()
        ready_event.wait = mock.AsyncMock(return_value=True)
        object.__setattr__(
            provider.mass,
            "get_provider_ready_event",
            mock.MagicMock(return_value=ready_event),
        )

        with pytest.raises(ResourceTemporarilyUnavailable, match="ym-1") as exc_info:
            await provider._resolve_borrowed_tokens()

        assert provider.mass.get_provider.call_count == 2
        assert exc_info.value.__suppress_context__ or exc_info.value.__context__ is None

    async def test_passport_failure_does_not_wait_for_provider_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient token failure goes straight to MA's retry, without a readiness wait."""
        provider = _borrow_provider(_ym_owner("test-music-ym", "test-x-ym"))
        source = provider._borrow_source
        assert source is not None
        resolve_credentials = mock.AsyncMock(
            side_effect=ResourceTemporarilyUnavailable("Passport unavailable")
        )
        monkeypatch.setattr(source, "resolve_credentials", resolve_credentials)
        get_provider_ready_event = mock.MagicMock()
        object.__setattr__(provider.mass, "get_provider_ready_event", get_provider_ready_event)

        with pytest.raises(ResourceTemporarilyUnavailable, match="Passport unavailable"):
            await provider._resolve_borrowed_tokens()

        resolve_credentials.assert_awaited_once()
        get_provider_ready_event.assert_not_called()


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

    async def test_reauth_reads_owner_tokens_once(self) -> None:
        """401 recovery reads the linked owner's token pair exactly once."""
        owner = _ym_owner_setup_data("test-music-ym", "test-x-ym")
        provider = _borrow_provider(owner)
        provider._session = mock.MagicMock()
        provider._session.login_token = mock.AsyncMock(return_value=True)

        assert await provider._silent_reauth() is True

        assert sorted(owner.reads) == ["token", "x_token"]
