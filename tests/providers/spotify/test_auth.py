"""
Tests for the Spotify provider's refresh-token handling.

The global refresh token is always read from the persisted setup_data, so an in-memory config
copy that lagged a rotation can never make us refresh with a stale (revoked) token. Spotify
rotates the refresh token on every refresh and revokes the previous one; if a newer token
was persisted while a refresh was in flight, the stored (newer) one is kept instead of
wiping the credentials and forcing re-auth.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import (
    LoginFailed,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.providers.spotify.constants import (
    CONF_ACCOUNT_COUNTRY,
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_NAME,
    CONF_AUDIOBOOKS_SUPPORTED,
    CONF_REFRESH_TOKEN_GLOBAL,
)
from music_assistant.providers.spotify.helpers import get_spotify_token
from music_assistant.providers.spotify.provider import SpotifyProvider

USED_TOKEN = "token_a"


def _make_provider(stored_token: str | None) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with a mocked setup_data store."""
    prov = object.__new__(SpotifyProvider)
    # the in-memory config copy has no value for the token; the global refresh token is
    # read from the persisted setup_data below, not from this object-local copy
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    config.values = {}
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    prov._auth_info_global = None

    setup_data = {CONF_REFRESH_TOKEN_GLOBAL: stored_token} if stored_token is not None else {}
    mass = MagicMock()
    # get_setup_value reads the live setup_data blob from the store
    mass.config.get = MagicMock(return_value=setup_data)
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    # the store keeps values encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


def _stub_token_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the token refresh of the provider with a fresh access token."""
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(
            return_value={
                "access_token": "access",
                "refresh_token": "fresh_token",
                "expires_at": 9999999999,
            }
        ),
    )


def _token_endpoint(status: int, text: str = "", headers: dict[str, str] | None = None) -> Any:
    """Return an http session whose token endpoint answers with the given response."""
    response = MagicMock(status=status, headers=headers or {})
    response.text = AsyncMock(return_value=text)
    response.json = AsyncMock(return_value={"access_token": "access", "expires_in": 3600})
    http_session = MagicMock()
    http_session.post = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)
        )
    )
    return http_session


def test_refresh_token_superseded_no_stored_token() -> None:
    """With no stored token there is nothing newer to protect, so it is not superseded."""
    prov = _make_provider(stored_token=None)
    assert prov._refresh_token_superseded(CONF_REFRESH_TOKEN_GLOBAL, USED_TOKEN) is False


def test_stored_refresh_token_reads_from_setup_data() -> None:
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
    monkeypatch.setattr(prov, "_update_setup_data", MagicMock())
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
    cast("MagicMock", prov.mass.config).get = MagicMock(
        side_effect=[
            {CONF_REFRESH_TOKEN_GLOBAL: USED_TOKEN},
            {CONF_REFRESH_TOKEN_GLOBAL: "token_b"},
        ]
    )
    update_setup_data = MagicMock()
    unload = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
    monkeypatch.setattr(prov, "unload_with_error", unload)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(side_effect=LoginFailed("invalid_grant: Refresh token revoked")),
    )
    with pytest.raises(LoginFailed):
        await prov.login()
    update_setup_data.assert_not_called()
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
    monkeypatch.setattr(prov, "_update_setup_data", MagicMock())
    monkeypatch.setattr("music_assistant.providers.spotify.provider.get_spotify_token", token_call)
    await prov.login()
    token_call.assert_awaited_once()


async def test_login_wipes_token_on_genuine_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked error clears the credentials when the stored token is the one we tried."""
    prov = _make_provider(stored_token=USED_TOKEN)
    update_setup_data = MagicMock()
    unload = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
    monkeypatch.setattr(prov, "unload_with_error", unload)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(side_effect=LoginFailed("invalid_grant: Refresh token revoked")),
    )
    with pytest.raises(LoginFailed):
        await prov.login()
    update_setup_data.assert_called_once_with(CONF_REFRESH_TOKEN_GLOBAL, None)
    unload.assert_called_once()


async def test_login_persists_rotated_token_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotated refresh token is flushed to disk immediately so it survives a crash."""
    prov = _make_provider(stored_token=USED_TOKEN)
    prov._sp_user = {"display_name": "tester"}  # already populated -> skip the user-info fetch
    update_setup_data = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
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
    update_setup_data.assert_called_once_with(
        CONF_REFRESH_TOKEN_GLOBAL, "token_rotated", immediate=True
    )


async def test_login_debounces_save_when_token_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged refresh token uses the normal debounced save instead of an immediate flush."""
    prov = _make_provider(stored_token=USED_TOKEN)
    prov._sp_user = {"display_name": "tester"}
    update_setup_data = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
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
    update_setup_data.assert_called_once_with(
        CONF_REFRESH_TOKEN_GLOBAL, USED_TOKEN, immediate=False
    )


async def test_login_records_the_account_on_a_legacy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config predating the stored account id gets it filled in on the next login."""
    prov = _make_provider(stored_token="fresh_token")
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(
            return_value={
                "access_token": "access",
                "refresh_token": "fresh_token",
                "expires_at": 9999999999,
            }
        ),
    )
    monkeypatch.setattr(
        prov, "_get_data", AsyncMock(return_value={"id": "u1", "display_name": "tester"})
    )
    update = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update)
    prov.mass.metadata = MagicMock()

    await prov.login()

    # the setup flow can now spot a duplicate account without loading this instance
    assert (CONF_ACCOUNT_ID, "u1") in [call.args[:2] for call in update.call_args_list]


async def test_login_leaves_a_recorded_account_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """An account id that is already stored is not rewritten on every login."""
    prov = _make_provider(stored_token="fresh_token")
    prov.mass.config.get = MagicMock(  # type: ignore[method-assign]
        return_value={CONF_REFRESH_TOKEN_GLOBAL: "fresh_token", CONF_ACCOUNT_ID: "u1"}
    )
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(
            return_value={
                "access_token": "access",
                "refresh_token": "fresh_token",
                "expires_at": 9999999999,
            }
        ),
    )
    monkeypatch.setattr(
        prov, "_get_data", AsyncMock(return_value={"id": "u1", "display_name": "tester"})
    )
    update = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update)
    prov.mass.metadata = MagicMock()

    await prov.login()

    assert CONF_ACCOUNT_ID not in [call.args[0] for call in update.call_args_list]


async def test_login_stores_the_account_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful account lookup stores the details a later load can fall back on."""
    prov = _make_provider(stored_token="fresh_token")
    _stub_token_refresh(monkeypatch)
    monkeypatch.setattr(
        prov,
        "_get_data",
        AsyncMock(return_value={"id": "u1", "display_name": "tester", "country": "NL"}),
    )
    update = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update)
    prov.mass.metadata = MagicMock()

    await prov.login()

    stored = [call.args[:2] for call in update.call_args_list]
    assert (CONF_ACCOUNT_NAME, "tester") in stored
    assert (CONF_ACCOUNT_COUNTRY, "NL") in stored


@pytest.mark.parametrize("error", [RetriesExhausted, ResourceTemporarilyUnavailable])
async def test_login_falls_back_to_the_stored_account(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """An account lookup Spotify does not answer is served from the stored account details."""
    prov = _make_provider(stored_token="fresh_token")
    prov.mass.config.get = MagicMock(  # type: ignore[method-assign]
        return_value={
            CONF_REFRESH_TOKEN_GLOBAL: "fresh_token",
            CONF_ACCOUNT_ID: "u1",
            CONF_ACCOUNT_NAME: "tester",
            CONF_ACCOUNT_COUNTRY: "NL",
        }
    )
    _stub_token_refresh(monkeypatch)
    monkeypatch.setattr(prov, "_get_data", AsyncMock(side_effect=error("limited")))
    update = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update)
    prov.mass.metadata = MagicMock()

    await prov.login()

    assert prov.account_id == "u1"
    assert prov.instance_name_postfix == "tester"
    prov.mass.metadata.set_default_preferred_language.assert_called_once_with("NL")
    # the stored details are used as they are, not written back
    assert CONF_ACCOUNT_ID not in [call.args[0] for call in update.call_args_list]


async def test_login_without_a_stored_account_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without stored account details an unanswered account lookup fails the login."""
    prov = _make_provider(stored_token="fresh_token")
    _stub_token_refresh(monkeypatch)
    monkeypatch.setattr(prov, "_get_data", AsyncMock(side_effect=RetriesExhausted("limited")))
    monkeypatch.setattr(prov, "_update_setup_data", MagicMock())

    with pytest.raises(RetriesExhausted):
        await prov.login()
    assert prov._sp_user is None


@pytest.mark.parametrize("error", [RateLimited, ResourceTemporarilyUnavailable])
async def test_login_keeps_the_provider_on_a_temporary_token_error(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """A token refresh Spotify cannot serve right now neither unloads nor clears credentials."""
    prov = _make_provider(stored_token=USED_TOKEN)
    update_setup_data = MagicMock()
    unload = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
    monkeypatch.setattr(prov, "unload_with_error", unload)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.provider.get_spotify_token",
        AsyncMock(side_effect=error("limited")),
    )

    with pytest.raises(error):
        await prov.login()
    update_setup_data.assert_not_called()
    unload.assert_not_called()
    prov.mass.create_task.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("answer", "stored", "expected"),
    [
        (RetriesExhausted("limited"), True, True),
        (RetriesExhausted("limited"), None, False),
        (ResourceTemporarilyUnavailable("down"), True, True),
    ],
)
async def test_audiobook_support_falls_back_to_the_stored_answer(
    monkeypatch: pytest.MonkeyPatch, answer: Exception, stored: bool | None, expected: bool
) -> None:
    """An audiobook check Spotify does not answer uses the stored answer, False without one."""
    prov = _make_provider(stored_token=USED_TOKEN)
    setup_data: dict[str, Any] = {CONF_REFRESH_TOKEN_GLOBAL: USED_TOKEN}
    if stored is not None:
        setup_data[CONF_AUDIOBOOKS_SUPPORTED] = stored
    prov.mass.config.get = MagicMock(return_value=setup_data)  # type: ignore[method-assign]
    monkeypatch.setattr(prov, "_get_data", AsyncMock(side_effect=answer))
    update = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update)

    assert await prov._test_audiobook_support() is expected
    update.assert_not_called()


async def test_audiobook_support_stores_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An answered audiobook check is stored for a later load."""
    prov = _make_provider(stored_token=USED_TOKEN)
    monkeypatch.setattr(prov, "_get_data", AsyncMock(return_value={"items": []}))
    update = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update)

    assert await prov._test_audiobook_support() is True
    update.assert_called_once_with(CONF_AUDIOBOOKS_SUPPORTED, True)


async def test_token_endpoint_rate_limit_is_temporary() -> None:
    """A rate limited token refresh raises RateLimited with the wait Spotify asked for."""
    http_session = _token_endpoint(429, headers={"Retry-After": "120"})

    with pytest.raises(RateLimited) as err:
        await get_spotify_token(http_session, "client", USED_TOKEN)
    assert err.value.backoff_time == 120


async def test_token_endpoint_server_error_is_temporary() -> None:
    """A token refresh that fails on Spotify's side raises ResourceTemporarilyUnavailable."""
    http_session = _token_endpoint(503)

    with pytest.raises(ResourceTemporarilyUnavailable) as err:
        await get_spotify_token(http_session, "client", USED_TOKEN)
    assert not isinstance(err.value, RateLimited)
    http_session.post.assert_called_once()


async def test_token_endpoint_revoked_token_fails_the_login() -> None:
    """A revoked refresh token still raises LoginFailed."""
    http_session = _token_endpoint(400, text='{"error": "invalid_grant"}')

    with pytest.raises(LoginFailed):
        await get_spotify_token(http_session, "client", USED_TOKEN)
    http_session.post.assert_called_once()
