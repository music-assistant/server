"""
Tests for the Spotify provider's refresh-token handling.

The global refresh token is always read from the persisted store, so an in-memory config
copy that lagged a rotation can never make us refresh with a stale (revoked) token. Spotify
rotates the refresh token on every refresh and revokes the previous one; if a newer token
was persisted while a refresh was in flight, the stored (newer) one is kept instead of
wiping the credentials and forcing re-auth.
"""

from __future__ import annotations

import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.spotify.constants import CONF_REFRESH_TOKEN_GLOBAL
from music_assistant.providers.spotify.provider import SpotifyProvider

USED_TOKEN = "token_a"


def _make_provider(stored_token: str | None) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with a mocked config store."""
    prov = object.__new__(SpotifyProvider)
    # the in-memory config copy has no dev token configured; the global refresh token is
    # read from the persisted store below, not from this object-local copy
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    prov._auth_info_global = None

    mass = MagicMock()
    mass.config.get_raw_provider_config_value = MagicMock(return_value=stored_token)
    # the raw store keeps the value encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


def test_refresh_token_superseded_no_stored_token() -> None:
    """With no stored token there is nothing newer to protect, so it is not superseded."""
    prov = _make_provider(stored_token=None)
    assert prov._refresh_token_superseded(CONF_REFRESH_TOKEN_GLOBAL, USED_TOKEN) is False


def test_stored_refresh_token_decrypts_stored_value() -> None:
    """_stored_refresh_token returns the decrypted persisted token, or None when unset."""
    prov = _make_provider(stored_token="token_x")
    assert prov._stored_refresh_token(CONF_REFRESH_TOKEN_GLOBAL) == "token_x"
    assert (
        _make_provider(stored_token=None)._stored_refresh_token(CONF_REFRESH_TOKEN_GLOBAL) is None
    )


async def test_login_reads_token_from_persisted_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh token is read from the persisted store, not a stale in-memory config copy."""
    prov = _make_provider(stored_token="fresh_token")
    prov._sp_user = {"display_name": "tester"}
    token_call = AsyncMock(
        return_value={
            "access_token": "access",
            "refresh_token": "fresh_token",
            "expires_at": 9999999999,
        }
    )
    monkeypatch.setattr(prov, "_update_config_value", MagicMock())
    monkeypatch.setattr(prov, "_setup_librespot_auth", AsyncMock())
    monkeypatch.setattr("music_assistant.providers.spotify.provider.get_spotify_token", token_call)
    await prov.login()
    # the token sent to Spotify must come from the persisted store
    assert token_call.await_args is not None
    assert token_call.await_args.args[2] == "fresh_token"


async def test_login_keeps_token_when_rotated_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked error is ignored when a newer token was persisted during the refresh."""
    prov = _make_provider(stored_token=USED_TOKEN)
    # the initial read uses token_a; the superseded re-check sees a newer token_b that was
    # persisted while the refresh was in flight
    cast("MagicMock", prov.mass.config).get_raw_provider_config_value = MagicMock(
        side_effect=[USED_TOKEN, "token_b"]
    )
    update_config = MagicMock()
    unload = MagicMock()
    monkeypatch.setattr(prov, "_update_config_value", update_config)
    monkeypatch.setattr(prov, "unload_with_error", unload)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(side_effect=LoginFailed("invalid_grant: Refresh token revoked")),
    )
    with pytest.raises(LoginFailed):
        await prov.login()
    update_config.assert_not_called()
    unload.assert_not_called()


async def test_login_returns_cached_token_while_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached access token that is still valid is returned without contacting Spotify."""
    prov = _make_provider(stored_token=USED_TOKEN)
    cached = {
        "access_token": "cached",
        "refresh_token": USED_TOKEN,
        "expires_at": time.time() + 3600,
    }
    prov._auth_info_global = cached
    token_call = AsyncMock()
    monkeypatch.setattr("music_assistant.providers.spotify.provider.get_spotify_token", token_call)
    assert await prov.login() is cached
    token_call.assert_not_awaited()


async def test_login_refreshes_when_cached_token_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired cached access token triggers a refresh instead of being served."""
    prov = _make_provider(stored_token=USED_TOKEN)
    prov._sp_user = {"display_name": "tester"}
    prov._auth_info_global = {
        "access_token": "old",
        "refresh_token": USED_TOKEN,
        "expires_at": time.time() - 10,
    }
    token_call = AsyncMock(
        return_value={
            "access_token": "new",
            "refresh_token": USED_TOKEN,
            "expires_at": time.time() + 3600,
        }
    )
    monkeypatch.setattr(prov, "_update_config_value", MagicMock())
    monkeypatch.setattr(prov, "_setup_librespot_auth", AsyncMock())
    monkeypatch.setattr("music_assistant.providers.spotify.provider.get_spotify_token", token_call)
    await prov.login()
    token_call.assert_awaited_once()


async def test_login_wipes_token_on_genuine_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked error clears the credentials when the stored token is the one we tried."""
    prov = _make_provider(stored_token=USED_TOKEN)
    update_config = MagicMock()
    unload = MagicMock()
    monkeypatch.setattr(prov, "_update_config_value", update_config)
    monkeypatch.setattr(prov, "unload_with_error", unload)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(side_effect=LoginFailed("invalid_grant: Refresh token revoked")),
    )
    with pytest.raises(LoginFailed):
        await prov.login()
    update_config.assert_called_once_with(CONF_REFRESH_TOKEN_GLOBAL, None)
    unload.assert_called_once()


async def test_login_persists_rotated_token_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated refresh token is flushed to disk immediately so it survives a crash."""
    prov = _make_provider(stored_token=USED_TOKEN)
    prov._sp_user = {"display_name": "tester"}  # already populated -> skip the user-info fetch
    update_config = MagicMock()
    monkeypatch.setattr(prov, "_update_config_value", update_config)
    monkeypatch.setattr(prov, "_setup_librespot_auth", AsyncMock())
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(
            return_value={
                "access_token": "access",
                "refresh_token": "token_rotated",
                "expires_at": 9999999999,
            }
        ),
    )
    await prov.login()
    update_config.assert_called_once_with(
        CONF_REFRESH_TOKEN_GLOBAL, "token_rotated", encrypted=True, immediate=True
    )


async def test_login_debounces_save_when_token_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged refresh token uses the normal debounced save instead of an immediate flush."""
    prov = _make_provider(stored_token=USED_TOKEN)
    prov._sp_user = {"display_name": "tester"}
    update_config = MagicMock()
    monkeypatch.setattr(prov, "_update_config_value", update_config)
    monkeypatch.setattr(prov, "_setup_librespot_auth", AsyncMock())
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(
            return_value={
                "access_token": "access",
                "refresh_token": USED_TOKEN,
                "expires_at": 9999999999,
            }
        ),
    )
    await prov.login()
    update_config.assert_called_once_with(
        CONF_REFRESH_TOKEN_GLOBAL, USED_TOKEN, encrypted=True, immediate=False
    )
