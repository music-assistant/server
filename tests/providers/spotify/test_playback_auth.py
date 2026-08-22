"""
Tests for the Spotify provider's playback (librespot) authorization.

Spotify's login5 endpoint only accepts a stored credential minted with the same client id
librespot presents, so the playback credential is obtained separately from the Web API tokens
and installed into librespot's cache directory on load. An install without one cannot stream
and must be sent back through the setup flow.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
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
    PAIRING_DEVICE_NAME,
)
from music_assistant.providers.spotify.helpers import _log_pairing_output
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
    pairing_mock = MagicMock(
        side_effect=[_raising(LoginFailed("nope")), _returning(STORED_CREDENTIALS)]
    )
    monkeypatch.setattr(setup_flow, "librespot_credentials_via_pairing", pairing_mock)
    session = MagicMock()
    session.form = AsyncMock(return_value={setup_flow.CONF_PLAYBACK_AUTH_METHOD: "spotify_app"})
    session.progress_until = AsyncMock(side_effect=_run_awaitable)

    assert await setup_flow._authorize_playback(session, None) == STORED_CREDENTIALS
    # the form was re-shown, carrying the failure reason rather than aborting the flow
    assert session.form.await_count == 2
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "playback_auth_failed"}
    pairing_mock.assert_called_with("/bin/librespot", PAIRING_DEVICE_NAME)


def test_pairing_device_name_matches_setup_text() -> None:
    """Pairing instructions consistently identify the temporary Spotify Connect device."""
    strings_path = Path(__file__).parents[3] / "music_assistant/providers/spotify/strings.json"
    strings = json.loads(strings_path.read_text(encoding="utf-8"))

    assert PAIRING_DEVICE_NAME == "Music Assistant Pairing"
    assert PAIRING_DEVICE_NAME in strings["setup_flow"]["playback_auth"]["description"]
    assert PAIRING_DEVICE_NAME in strings["setup_flow"]["playback_pairing"]["title"]
    assert PAIRING_DEVICE_NAME in strings["setup_flow"]["playback_pairing"]["progress_text"]
    assert PAIRING_DEVICE_NAME in strings["errors"]["pairing_not_completed"]


async def test_pairing_output_demotes_duplicate_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated librespot warnings are debug logged while distinct warnings stay visible."""

    async def stderr_lines() -> AsyncIterator[str]:
        for line in (
            "[2026-08-12T00:30:01Z WARN  libmdns] No route to host",
            "[2026-08-12T00:30:02Z WARN  libmdns] No route to host",
            "[2026-08-12T00:30:03Z WARN  libmdns] Interface unavailable",
        ):
            yield line

    process = MagicMock()
    process.iter_stderr.return_value = stderr_lines()

    with caplog.at_level(logging.DEBUG, logger="music_assistant.providers.spotify.helpers"):
        await _log_pairing_output(process)

    records = [record for record in caplog.records if "[librespot-pairing]" in record.message]
    assert [record.levelno for record in records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.WARNING,
    ]
    assert [record.getMessage() for record in records] == [
        "[librespot-pairing] [2026-08-12T00:30:01Z WARN  libmdns] No route to host",
        "[librespot-pairing] [2026-08-12T00:30:02Z WARN  libmdns] No route to host",
        "[librespot-pairing] [2026-08-12T00:30:03Z WARN  libmdns] Interface unavailable",
    ]


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


@pytest.mark.parametrize(
    ("credentials", "account_id", "differs"),
    [
        # the same account: the credential is accepted
        ('{"username": "u1", "auth_data": "blob"}', "u1", False),
        # a Spotify app logged in as someone else
        ('{"username": "u2", "auth_data": "blob"}', "u1", True),
        # either side unknown, or an unreadable credential: never block the setup
        ('{"username": "u2", "auth_data": "blob"}', None, False),
        ('{"auth_data": "blob"}', "u1", False),
        ('{"username": "", "auth_data": "blob"}', "u1", False),
        ("not json", "u1", False),
        ("[]", "u1", False),
    ],
)
def test_credential_account_comparison(
    credentials: str, account_id: str | None, differs: bool
) -> None:
    """A playback credential from another Spotify account is spotted, and only that."""
    from music_assistant.providers.spotify.setup_flow import (  # noqa: PLC0415
        _credential_account_differs,
    )

    assert _credential_account_differs(credentials, account_id) is differs


async def test_playback_authorized_with_another_account_loops_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pairing with the wrong Spotify account re-shows the step instead of storing it."""
    from music_assistant.providers.spotify import setup_flow  # noqa: PLC0415

    monkeypatch.setattr(
        setup_flow, "get_librespot_binary", AsyncMock(return_value="/bin/librespot")
    )
    # first attempt pairs the wrong account, second one gets it right
    pairing_mock = MagicMock(
        side_effect=[
            _returning('{"username": "someone_else", "auth_data": "blob"}'),
            _returning('{"username": "u1", "auth_data": "blob"}'),
        ]
    )
    monkeypatch.setattr(setup_flow, "librespot_credentials_via_pairing", pairing_mock)
    session = MagicMock()
    session.form = AsyncMock(return_value={setup_flow.CONF_PLAYBACK_AUTH_METHOD: "spotify_app"})
    session.progress_until = AsyncMock(side_effect=_run_awaitable)

    result = await setup_flow._authorize_playback(session, "u1")

    assert result == '{"username": "u1", "auth_data": "blob"}'
    # the mismatch re-showed the method step carrying the reason
    assert session.form.await_count == 2
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "playback_account_mismatch"}
