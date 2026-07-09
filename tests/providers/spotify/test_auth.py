"""
Tests for the Spotify provider's refresh-token handling.

Spotify rotates the refresh token on every refresh and revokes the previous one. If a
token was rotated while a refresh was in flight, the token we tried is merely stale, so
the stored (newer) one must be kept instead of wiping the credentials and forcing re-auth.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.spotify.constants import CONF_REFRESH_TOKEN_GLOBAL
from music_assistant.providers.spotify.provider import SpotifyProvider

USED_TOKEN = "token_a"


def _make_provider(stored_token: str | None) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with a mocked config store."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: (
            USED_TOKEN if key == CONF_REFRESH_TOKEN_GLOBAL else default
        )
    )
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


async def test_login_keeps_rotated_token_on_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked error is ignored when the stored token was rotated meanwhile."""
    prov = _make_provider(stored_token="token_b")
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
