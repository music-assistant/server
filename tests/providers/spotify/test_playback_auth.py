"""
Tests for the Spotify provider's playback (librespot) authorization.

Spotify's login5 endpoint only accepts a stored credential minted with the same client id
librespot presents, so the playback credential is obtained separately from the Web API tokens
and installed into librespot's cache directory on load. An install without one cannot stream
and must be sent back through the setup flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.controllers.config.helpers import _AUTH_ERROR_CODES
from music_assistant.helpers.oauth import authorization_code_from_url
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.spotify.constants import (
    CONF_LIBRESPOT_CREDENTIALS,
    CREDENTIALS_FILE,
)
from music_assistant.providers.spotify.provider import SpotifyProvider

STORED_CREDENTIALS = '{"username": "tester", "auth_type": 1, "auth_data": "blob"}'


def _make_provider(credentials: str | None, cache_dir: str) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with the given stored credential."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    config.values = {}
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    prov.cache_dir = cache_dir
    prov._librespot_bin = "/bin/librespot"
    setup_data = {CONF_LIBRESPOT_CREDENTIALS: credentials} if credentials is not None else {}
    mass = MagicMock()
    # get_setup_value reads the live setup_data blob from the store
    mass.config.get = MagicMock(return_value=setup_data)
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    # the store keeps values encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


async def test_stored_credential_is_installed_for_librespot(
    tmp_path: Path,
) -> None:
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


async def test_missing_credential_requires_reauth(tmp_path: Path) -> None:
    """Without a stored credential the provider fails with an auth error (AUTH_REQUIRED)."""
    prov = _make_provider(None, str(tmp_path / "cache"))
    with pytest.raises(LoginFailed) as err:
        await prov._setup_librespot_auth()
    # the error code is what actually drives the provider to AUTH_REQUIRED (and so the
    # reconfigure prompt); the translation key is what the user reads
    assert err.value.error_code in _AUTH_ERROR_CODES
    assert err.value.translation_key == "playback_auth_required"


async def test_failed_attempt_loops_back_to_the_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed attempt re-offers the choice instead of aborting the already-authorized flow."""
    from music_assistant.providers.spotify import setup_flow  # noqa: PLC0415

    monkeypatch.setattr(
        setup_flow, "get_librespot_binary", AsyncMock(return_value="/bin/librespot")
    )
    # first attempt fails the way a rejected token does, second one succeeds
    monkeypatch.setattr(
        setup_flow,
        "librespot_credentials_via_pairing",
        MagicMock(side_effect=[_raising(LoginFailed("nope")), _returning(STORED_CREDENTIALS)]),
    )
    session = MagicMock()
    session.form = AsyncMock(return_value={setup_flow.CONF_PLAYBACK_AUTH_METHOD: "spotify_app"})
    session.progress_until = AsyncMock(side_effect=_run_awaitable)

    assert await setup_flow._authorize_playback(session) == STORED_CREDENTIALS
    # the form was re-shown, carrying the failure reason rather than aborting the flow
    assert session.form.await_count == 2
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "playback_auth_failed"}


async def _run_awaitable(awaitable: Any, **_kwargs: Any) -> Any:
    """Stand in for session.progress_until, which awaits the work it displays progress for."""
    return await awaitable


async def _raising(err: Exception) -> str:
    """Return a coroutine that raises, standing in for a failed credential attempt."""
    raise err


async def _returning(value: str) -> str:
    """Return a coroutine resolving to the given credential."""
    return value


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
        "http://127.0.0.1:5588/login?code=null",
        "not a url at all",
        "",
    ],
)
def test_authorization_code_from_url_rejects_unusable(url: str) -> None:
    """A denied, empty or malformed paste is reported instead of silently proceeding."""
    with pytest.raises(SetupFlowError):
        authorization_code_from_url(url)
