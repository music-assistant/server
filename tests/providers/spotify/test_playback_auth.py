"""
Tests for the Spotify provider's playback (librespot) authorization.

Spotify's login5 endpoint only accepts a stored credential minted with the same client id
librespot presents, so the playback credential is collected separately from the Web API tokens
and installed into librespot's cache directory on load. An install without one cannot stream
and has to authorize playback from the provider settings.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.providers.spotify import _PENDING_BROWSER_AUTH, get_config_entries
from music_assistant.providers.spotify.constants import (
    CONF_ACTION_AUTH_PLAYBACK,
    CONF_ACTION_AUTH_PLAYBACK_BROWSER,
    CONF_ACTION_AUTH_PLAYBACK_SUBMIT,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_CLEAR_AUTH_PLAYBACK,
    CONF_LIBRESPOT_CREDENTIALS,
    CONF_PLAYBACK_CALLBACK_URL,
    CONF_REFRESH_TOKEN_GLOBAL,
    CREDENTIALS_FILE,
)
from music_assistant.providers.spotify.helpers import authorization_code_from_url
from music_assistant.providers.spotify.provider import SpotifyProvider

STORED_CREDENTIALS = '{"username": "tester", "auth_type": 1, "auth_data": "blob"}'


@pytest.fixture(autouse=True)
def _clean_pending_browser_auth() -> Generator[None]:
    """Keep the in-memory pending sign-in state from leaking between tests."""
    _PENDING_BROWSER_AUTH.clear()
    yield
    _PENDING_BROWSER_AUTH.clear()


async def test_stored_credential_is_installed_for_librespot(tmp_path: Path) -> None:
    """The stored credential is written to librespot's cache so login5 accepts it."""
    cache_dir = tmp_path / "cache"
    prov = _make_provider(STORED_CREDENTIALS, str(cache_dir))
    await prov._setup_librespot_auth()
    written = json.loads((cache_dir / CREDENTIALS_FILE).read_text(encoding="utf-8"))
    assert written["auth_data"] == "blob"


async def test_stale_cached_credential_is_replaced(tmp_path: Path) -> None:
    """A credential left in the cache from an earlier (now rejected) mint is overwritten."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    credentials_file = cache_dir / CREDENTIALS_FILE
    credentials_file.write_text('{"username": "tester", "auth_data": "stale"}', encoding="utf-8")
    prov = _make_provider(STORED_CREDENTIALS, str(cache_dir))
    await prov._setup_librespot_auth()
    written = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert written["auth_data"] == "blob"


async def test_missing_credential_fails_the_load(tmp_path: Path) -> None:
    """Without a stored credential the provider refuses to load, pointing at the settings."""
    prov = _make_provider(None, str(tmp_path / "cache"))
    with pytest.raises(LoginFailed, match="authorization required"):
        await prov._setup_librespot_auth()


async def test_pairing_action_stores_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting Music Assistant in the Spotify app stores the credential for playback."""
    _patch_librespot(monkeypatch)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.librespot_credentials_via_pairing",
        AsyncMock(return_value=STORED_CREDENTIALS),
    )
    values = _authenticated_values()

    entries = await _get_entries(action=CONF_ACTION_AUTH_PLAYBACK, values=values)

    assert values[CONF_LIBRESPOT_CREDENTIALS] == STORED_CREDENTIALS
    # with playback authorized, the authorize buttons make way for the clear action
    assert _entry(entries, CONF_ACTION_AUTH_PLAYBACK).hidden
    assert not _entry(entries, CONF_ACTION_CLEAR_AUTH_PLAYBACK).hidden


async def test_pairing_timeout_points_at_the_browser_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the device is never selected, the error names the fallback instead of just failing."""
    _patch_librespot(monkeypatch)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.librespot_credentials_via_pairing",
        AsyncMock(side_effect=TimeoutError),
    )

    with pytest.raises(LoginFailed, match="web browser"):
        await _get_entries(action=CONF_ACTION_AUTH_PLAYBACK, values=_authenticated_values())


async def test_browser_action_falls_back_to_pasting_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the browser cannot reach Music Assistant, the paste field is revealed instead."""
    _patch_librespot(monkeypatch)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.await_loopback_authorization",
        AsyncMock(side_effect=TimeoutError),
    )
    values = _authenticated_values()

    entries = await _get_entries(action=CONF_ACTION_AUTH_PLAYBACK_BROWSER, values=values)

    assert not values[CONF_LIBRESPOT_CREDENTIALS]
    # the parked verifier is what keeps the pending sign-in redeemable on the next action
    assert _PENDING_BROWSER_AUTH["session"]
    assert not _entry(entries, CONF_PLAYBACK_CALLBACK_URL).hidden
    assert not _entry(entries, CONF_ACTION_AUTH_PLAYBACK_SUBMIT).hidden


async def test_browser_action_completes_on_the_same_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser on the Music Assistant host reports back, so no pasting is needed."""
    _patch_librespot(monkeypatch)
    monkeypatch.setattr(
        "music_assistant.providers.spotify.await_loopback_authorization",
        AsyncMock(return_value={"code": "abc123"}),
    )
    values = _authenticated_values()

    entries = await _get_entries(action=CONF_ACTION_AUTH_PLAYBACK_BROWSER, values=values)

    assert values[CONF_LIBRESPOT_CREDENTIALS] == STORED_CREDENTIALS
    assert not _PENDING_BROWSER_AUTH
    assert _entry(entries, CONF_PLAYBACK_CALLBACK_URL).hidden


async def test_pasted_address_completes_the_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The address copied from the browser is redeemed into a playback credential."""
    access_token = _patch_librespot(monkeypatch)
    values = _authenticated_values()
    _PENDING_BROWSER_AUTH["session"] = "verifier"
    values[CONF_PLAYBACK_CALLBACK_URL] = "http://127.0.0.1:5588/login?code=abc123"

    await _get_entries(action=CONF_ACTION_AUTH_PLAYBACK_SUBMIT, values=values)

    assert access_token.await_args is not None
    assert access_token.await_args.args[1:] == ("abc123", "verifier")
    assert values[CONF_LIBRESPOT_CREDENTIALS] == STORED_CREDENTIALS
    # the redeemed sign-in leaves nothing behind to retry with
    assert not _PENDING_BROWSER_AUTH
    assert not values[CONF_PLAYBACK_CALLBACK_URL]


async def test_submit_without_a_started_signin_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completing an authorization that was never started reports what to do first."""
    _patch_librespot(monkeypatch)
    values = _authenticated_values()
    values[CONF_PLAYBACK_CALLBACK_URL] = "http://127.0.0.1:5588/login?code=abc123"

    with pytest.raises(InvalidDataError):
        await _get_entries(action=CONF_ACTION_AUTH_PLAYBACK_SUBMIT, values=values)


async def test_rejected_credential_is_discarded(tmp_path: Path) -> None:
    """A credential Spotify refuses is dropped, so playback can be authorized again."""
    prov = _make_provider(STORED_CREDENTIALS, str(tmp_path / "cache"))
    prov.discard_playback_credentials()
    set_raw_value = cast("MagicMock", prov.mass.config.set_raw_provider_config_value)
    set_raw_value.assert_called_once_with(prov.instance_id, CONF_LIBRESPOT_CREDENTIALS, None, False)


async def test_settings_offer_authorization_when_not_authorized() -> None:
    """Without a credential the authorize buttons are shown and the clear action is not."""
    values = _authenticated_values()

    entries = await _get_entries(action=CONF_ACTION_CLEAR_AUTH_PLAYBACK, values=values)

    assert not _entry(entries, CONF_ACTION_AUTH_PLAYBACK).hidden
    assert not _entry(entries, CONF_ACTION_AUTH_PLAYBACK_BROWSER).hidden
    assert _entry(entries, CONF_ACTION_CLEAR_AUTH_PLAYBACK).hidden


async def test_clearing_account_auth_clears_playback_authorization() -> None:
    """The credential belongs to the connected account, so it goes when that account does."""
    values = _authenticated_values()
    values[CONF_LIBRESPOT_CREDENTIALS] = STORED_CREDENTIALS

    await _get_entries(action=CONF_ACTION_CLEAR_AUTH, values=values)

    assert not values[CONF_LIBRESPOT_CREDENTIALS]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:5588/login?code=abc123", "abc123"),
        ("  http://127.0.0.1:5588/login?code=abc123&state=x  ", "abc123"),
        ("https://127.0.0.1:5588/login?state=x&code=abc123", "abc123"),
    ],
)
def test_authorization_code_from_url(url: str, expected: str) -> None:
    """The code is recovered from the (dead) loopback URL the user pastes back."""
    assert authorization_code_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5588/login?error=access_denied",
        "not a url at all",
        "",
    ],
)
def test_authorization_code_from_url_rejects_unusable(url: str) -> None:
    """A denied, empty or malformed paste is reported instead of silently proceeding."""
    with pytest.raises(InvalidDataError):
        authorization_code_from_url(url)


def _make_provider(credentials: str | None, cache_dir: str) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with the given stored credential."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: (
            credentials if key == CONF_LIBRESPOT_CREDENTIALS else default
        )
    )
    prov.config = config
    prov.logger = MagicMock()
    prov.cache_dir = cache_dir
    prov._librespot_bin = "/bin/librespot"
    prov.mass = MagicMock()
    return prov


def _patch_librespot(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub out the librespot binary and token exchange, returning the token exchange mock."""
    monkeypatch.setattr(
        "music_assistant.providers.spotify.get_librespot_binary",
        AsyncMock(return_value="/bin/librespot"),
    )
    monkeypatch.setattr(
        "music_assistant.providers.spotify.librespot_credentials_via_token",
        AsyncMock(return_value=STORED_CREDENTIALS),
    )
    access_token = AsyncMock(return_value="access-token")
    monkeypatch.setattr("music_assistant.providers.spotify.keymaster_access_token", access_token)
    return access_token


def _authenticated_values() -> dict[str, ConfigValueType]:
    """Return config values for an instance whose account is connected."""
    return {
        "session_id": "session",
        CONF_REFRESH_TOKEN_GLOBAL: "refresh-token",
        CONF_LIBRESPOT_CREDENTIALS: None,
        CONF_PLAYBACK_CALLBACK_URL: None,
    }


async def _get_entries(action: str, values: dict[str, ConfigValueType]) -> tuple[ConfigEntry, ...]:
    """Run the provider's config entries for the given action."""
    mass: Any = MagicMock()
    return await get_config_entries(mass, instance_id="spotify--test", action=action, values=values)


def _entry(entries: tuple[ConfigEntry, ...], key: str) -> ConfigEntry:
    """Return the config entry with the given key."""
    return next(entry for entry in entries if entry.key == key)
