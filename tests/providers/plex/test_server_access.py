"""Tests for connecting to a Plex server as the right Plex user (owner or shared user)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from music_assistant_models.errors import LoginFailed
from plexapi.exceptions import BadRequest, Unauthorized

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.constants import (
    CONF_AUTH_TOKEN,
    CONF_LIBRARY_ID,
    CONF_LOCAL_SERVER_IP,
    CONF_LOCAL_SERVER_PORT,
    CONF_LOCAL_SERVER_SSL,
    CONF_LOCAL_SERVER_VERIFY_CERT,
)
from music_assistant.providers.plex.helpers import (
    CONF_LIBRARY_TYPE,
    SUPPORTED_FEATURES,
    PlexSectionInfo,
    PlexServerAccessError,
    get_section_info,
    resolve_server_auth_token,
)
from music_assistant.providers.plex.setup_flow import run_setup

PLEX_URL = "http://192.168.1.77:32400"
MACHINE_ID = "server-abc"
OWNER = "owner-user"
SHARED = "shared-user"

OWNER_ACCOUNT_TOKEN = "owner-account-token"
SHARED_ACCOUNT_TOKEN = "shared-account-token"
SHARED_SERVER_TOKEN = "shared-server-token"
OTHER_SERVER_TOKEN = "other-server-token"
ALL_TOKENS = (OWNER_ACCOUNT_TOKEN, SHARED_ACCOUNT_TOKEN, SHARED_SERVER_TOKEN, OTHER_SERVER_TOKEN)

SECTION = PlexSectionInfo(
    display_name="Server / Music",
    section_title="Music",
    server_name="Server",
    section_type="artist",
    is_tracking_progress=False,
)


class FakePlexServer:
    """
    The identity a Plex server gives a request, as observed on a real server.

    A token the server issued identifies its user. A shared user's plex.tv account token
    is not one of them: it is rejected (401), unless the request comes from a network the
    server allows without authentication - then the request runs as the server owner.
    """

    def __init__(self, allow_without_auth: bool) -> None:
        """Initialize the server with its owner and the tokens it issued."""
        self.allow_without_auth = allow_without_auth
        self.issued_tokens = {OWNER_ACCOUNT_TOKEN: OWNER, SHARED_SERVER_TOKEN: SHARED}

    def user_for(self, token: str) -> str:
        """Return the Plex user a request with this token runs as."""
        if token in self.issued_tokens:
            return self.issued_tokens[token]
        if self.allow_without_auth:
            return OWNER
        raise Unauthorized("(401) unauthorized")


def _session() -> MagicMock:
    """Build a requests session whose /identity response reports the server's machine id."""
    session = MagicMock(spec=requests.Session)
    session.get.return_value.json.return_value = {
        "MediaContainer": {"machineIdentifier": MACHINE_ID}
    }
    return session


def _resource(machine_id: str, access_token: str | None, owned: bool) -> SimpleNamespace:
    """Build a plex.tv server resource as returned by MyPlexAccount.resources()."""
    return SimpleNamespace(
        clientIdentifier=machine_id, accessToken=access_token, owned=owned, provides="server"
    )


def _shared_account(resources: list[SimpleNamespace] | Exception | None = None) -> MagicMock:
    account = MagicMock(authToken=SHARED_ACCOUNT_TOKEN)
    if resources is None:
        resources = [
            _resource("other-server", OTHER_SERVER_TOKEN, owned=False),
            _resource(MACHINE_ID, SHARED_SERVER_TOKEN, owned=False),
        ]
    if isinstance(resources, Exception):
        account.resources.side_effect = resources
    else:
        account.resources.return_value = resources
    return account


def _resolve_shared(account: MagicMock) -> str:
    return resolve_server_auth_token(
        SHARED_ACCOUNT_TOKEN, PLEX_URL, _session(), myplex_account=account
    )


def _assert_no_tokens(text: str) -> None:
    for token in ALL_TOKENS:
        assert token not in text


# --- which Plex user the connection runs as -------------------------------------------


@pytest.mark.parametrize("allow_without_auth", [False, True])
def test_shared_server_token_runs_as_shared_user(allow_without_auth: bool) -> None:
    """The resolved token identifies the shared user, also on a no-auth network."""
    token = _resolve_shared(_shared_account())
    assert FakePlexServer(allow_without_auth).user_for(token) == SHARED


def test_owner_account_token_runs_as_owner() -> None:
    """The owner keeps connecting as the owner with its account token."""
    account = MagicMock(authToken=OWNER_ACCOUNT_TOKEN)
    account.resources.return_value = [_resource(MACHINE_ID, OWNER_ACCOUNT_TOKEN, owned=True)]
    token = resolve_server_auth_token(
        OWNER_ACCOUNT_TOKEN, PLEX_URL, _session(), myplex_account=account
    )
    assert FakePlexServer(allow_without_auth=False).user_for(token) == OWNER


def test_shared_account_token_is_rejected_or_runs_as_owner() -> None:
    """
    Document why a shared user may never fall back to its account token.

    The shared user's plain account token is rejected, or runs as the server owner.
    """
    with pytest.raises(Unauthorized):
        FakePlexServer(allow_without_auth=False).user_for(SHARED_ACCOUNT_TOKEN)
    assert FakePlexServer(allow_without_auth=True).user_for(SHARED_ACCOUNT_TOKEN) == OWNER


@pytest.mark.parametrize(
    "resources",
    [
        pytest.param([], id="no-resources"),
        pytest.param(
            [_resource("other-server", OTHER_SERVER_TOKEN, owned=False)], id="other-server-only"
        ),
        pytest.param([_resource(MACHINE_ID, None, owned=False)], id="no-access-token"),
        pytest.param(BadRequest("(401) unauthorized"), id="plex.tv-error"),
    ],
)
def test_shared_user_without_verified_token_fails_without_leaking_tokens(
    resources: list[SimpleNamespace] | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Without its own verified server token a shared user fails, and no token is exposed."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PlexServerAccessError) as err:
        _resolve_shared(_shared_account(resources))
    _assert_no_tokens(str(err.value))
    _assert_no_tokens(caplog.text)


# --- cached section lookup --------------------------------------------------------------


def _cache_mass(cached: Any = None) -> MagicMock:
    mass = MagicMock()
    mass.cache.get = AsyncMock(return_value=cached)
    mass.cache.set = AsyncMock()
    return mass


async def _section_info(mass: MagicMock) -> list[PlexSectionInfo]:
    return await get_section_info(
        mass, SHARED_ACCOUNT_TOKEN, False, "192.168.1.77", "32400", False, "plex_instance_1"
    )


async def test_cached_sections_need_verified_access() -> None:
    """Revoked access fails before a cached section list could be used."""
    mass = _cache_mass(cached=[SECTION.__dict__])
    with (
        patch(
            "music_assistant.providers.plex.helpers.resolve_server_auth_token",
            side_effect=PlexServerAccessError("no access"),
        ),
        pytest.raises(PlexServerAccessError),
    ):
        await _section_info(mass)
    mass.cache.get.assert_not_called()


async def test_cached_sections_are_keyed_on_verified_server_token() -> None:
    """A cached section list is only reused for the server token verified just now."""
    mass = _cache_mass(cached=[SECTION.__dict__])
    with (
        patch(
            "music_assistant.providers.plex.helpers.resolve_server_auth_token",
            return_value=SHARED_SERVER_TOKEN,
        ),
        patch("music_assistant.providers.plex.helpers.PlexServer") as plex_server,
    ):
        assert await _section_info(mass) == [SECTION]
    assert mass.cache.get.call_args.kwargs["checksum"] == SHARED_SERVER_TOKEN
    plex_server.assert_not_called()


async def test_uncached_sections_are_read_with_verified_server_token() -> None:
    """Without a cached list the sections are read and cached with the verified token."""
    mass = _cache_mass(cached=None)
    with (
        patch(
            "music_assistant.providers.plex.helpers.resolve_server_auth_token",
            return_value=SHARED_SERVER_TOKEN,
        ),
        patch("music_assistant.providers.plex.helpers.PlexServer") as plex_server,
    ):
        plex_server.return_value.library.sections.return_value = []
        await _section_info(mass)
    assert plex_server.call_args.args == (PLEX_URL, SHARED_SERVER_TOKEN)
    assert mass.cache.set.call_args.kwargs["checksum"] == SHARED_SERVER_TOKEN


# --- provider connection ----------------------------------------------------------------


def _make_provider() -> PlexProvider:
    """Create a PlexProvider set up with a plex.tv account token."""
    mock_mass = MagicMock()
    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"
    config_values = {"library_type": "music", "log_level": "INFO"}
    mock_config.get_value = lambda key: config_values.get(key)
    setup_data = {
        CONF_AUTH_TOKEN: SHARED_ACCOUNT_TOKEN,
        CONF_LOCAL_SERVER_IP: "192.168.1.77",
        CONF_LOCAL_SERVER_PORT: 32400,
        CONF_LOCAL_SERVER_SSL: False,
        CONF_LOCAL_SERVER_VERIFY_CERT: False,
        CONF_LIBRARY_ID: "Server / Music",
        CONF_LIBRARY_TYPE: "music",
    }
    mock_mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mock_mass.config.get_raw_provider_config_value = lambda _instance_id, _key: None
    mock_mass.config.decrypt_string = lambda value: value
    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"
    provider = PlexProvider(mock_mass, mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider.get_myplex_account_and_refresh_token = AsyncMock(  # type: ignore[method-assign]
        return_value=_shared_account()
    )
    provider._cleanup_stale_library_mappings = AsyncMock()  # type: ignore[method-assign]
    return provider


async def test_provider_connects_with_resolved_server_token() -> None:
    """The provider connects to the server with the resolved server token."""
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.plex.resolve_server_auth_token",
            return_value=SHARED_SERVER_TOKEN,
        ),
        patch("music_assistant.providers.plex.PlexServer") as plex_server,
    ):
        await provider.handle_async_init()
    assert plex_server.call_args.args == (PLEX_URL, SHARED_SERVER_TOKEN)


async def test_provider_access_error_is_login_failure() -> None:
    """No verified server token fails the login, without connecting with the account token."""
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.plex.resolve_server_auth_token",
            side_effect=PlexServerAccessError("no access"),
        ),
        patch("music_assistant.providers.plex.PlexServer") as plex_server,
        pytest.raises(LoginFailed) as err,
    ):
        await provider.handle_async_init()
    plex_server.assert_not_called()
    _assert_no_tokens(str(err.value))


# --- setup flow -------------------------------------------------------------------------


class _FlowSession:
    """Minimal setup session that answers every form with fixed values."""

    def __init__(self) -> None:
        self.mass = MagicMock()
        self.mass.config.get = lambda _key, default=None: default
        self.context = SimpleNamespace(setup_data={}, instance_id=None)
        self.forms: list[tuple[str, dict[str, Any] | None]] = []
        self.finished: dict[str, Any] | None = None

    async def form(
        self,
        _entries: list[Any],
        step_id: str,
        errors: dict[str, Any] | None = None,
        last_step: bool = False,
    ) -> dict[str, Any]:
        self.forms.append((step_id, errors))
        if step_id == "server":
            return {
                CONF_LOCAL_SERVER_IP: "192.168.1.77",
                CONF_LOCAL_SERVER_PORT: 32400,
                CONF_LOCAL_SERVER_SSL: False,
                CONF_LOCAL_SERVER_VERIFY_CERT: False,
            }
        return {CONF_LIBRARY_ID: "Server / Music", CONF_LIBRARY_TYPE: "music"}

    async def progress_until(self, awaitable: Any, **_kwargs: Any) -> Any:
        return await awaitable

    async def finish(self, data: dict[str, Any]) -> None:
        self.finished = data


async def test_setup_flow_reports_server_access_denied() -> None:
    """A shared user without a verified server token gets a clear setup error."""
    session = _FlowSession()
    with (
        patch(
            "music_assistant.providers.plex.setup_flow.discover_local_servers",
            AsyncMock(return_value=(None, None)),
        ),
        patch(
            "music_assistant.providers.plex.setup_flow._authenticate",
            AsyncMock(return_value=SHARED_ACCOUNT_TOKEN),
        ),
        patch(
            "music_assistant.providers.plex.setup_flow.get_section_info",
            AsyncMock(side_effect=[PlexServerAccessError("no access"), [SECTION]]),
        ),
    ):
        await run_setup(session)  # type: ignore[arg-type]
    assert ("server", {"base": "server_access_denied"}) in session.forms
    assert session.finished is not None
