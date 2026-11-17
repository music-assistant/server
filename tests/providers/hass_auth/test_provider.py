"""Tests for signing in to Music Assistant with a Home Assistant account."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, MagicMock, PropertyMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from music_assistant_models.auth import AuthProviderType, User, UserRole
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import RateLimited
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_AUTH_ALLOW_SELF_REGISTRATION
from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers import login_flow
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.controllers.webserver.helpers.auth_providers import (
    HA_LINK_REFUSED_MESSAGE,
    AuthFailure,
    AuthResult,
)
from music_assistant.controllers.webserver.helpers.login_flow import (
    MAX_PENDING_LOGINS,
    PENDING_LOGIN_TTL,
    AuthTransport,
    LoginFlowError,
)
from music_assistant.helpers.pkce import pkce_challenge
from music_assistant.providers import hass_auth
from music_assistant.providers.hass_auth import HomeAssistantAuthProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.mass import MusicAssistant

HA_URL = "http://ha.local:8123"


@pytest.fixture
async def auth_manager(mass_minimal: MusicAssistant) -> AsyncGenerator[AuthenticationManager]:
    """
    Provide the authentication manager of a minimal server, with its database set up.

    :param mass_minimal: The minimal server to set up the authentication manager on.
    """
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    webserver.config = await mass_minimal.config.get_core_config("webserver")
    # creating the first user migrates playlog rows through the music controller,
    # which the minimal server does not run
    mass_minimal.music = MagicMock()
    mass_minimal.music.database.execute = AsyncMock()
    mass_minimal.music.database.commit = AsyncMock()
    await webserver.auth.setup()
    try:
        yield webserver.auth
    finally:
        mass_minimal._providers.pop("hass", None)
        mass_minimal._providers.pop("hass_auth", None)
        await webserver.auth.close()


@pytest.fixture
def provider(auth_manager: AuthenticationManager) -> HomeAssistantAuthProvider:
    """
    Load the Home Assistant sign-in, next to a hass plugin that is not connected.

    :param auth_manager: The authentication manager the provider signs users in with.
    """
    mass = auth_manager.mass
    _set_hass_provider(mass)
    manifest = ProviderManifest.from_dict(
        json.loads((Path(hass_auth.__file__).parent / "manifest.json").read_text())
    )
    config = ProviderConfig(
        values={}, type=ProviderType.AUTH, domain="hass_auth", instance_id="hass_auth", enabled=True
    )
    provider = HomeAssistantAuthProvider(mass, manifest, config)
    provider.available = True
    mass._providers[provider.instance_id] = provider
    return provider


async def test_sign_in_method(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """The provider offers the sign-in under the id and type clients use today."""
    method = provider.get_sign_in_method()

    assert method.provider_id == "homeassistant"
    assert method.provider_type == "hass_auth"
    assert method.name == "Home Assistant"
    assert method.icon == "md:home-assistant"
    assert method.available
    assert method.requires_redirect
    assert {
        "provider_id": "homeassistant",
        "provider_type": AuthProviderType.HOME_ASSISTANT.value,
        "requires_redirect": True,
        "supports_remote_app": False,
    } in await auth_manager.get_login_providers()


@pytest.mark.parametrize("hass_state", ["not_loaded", "unavailable", "no_url"])
async def test_sign_in_is_unavailable_without_the_hass_plugin(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider, hass_state: str
) -> None:
    """
    Without a usable hass plugin no Home Assistant sign-in is listed, started or completed.

    :param hass_state: What is wrong with the hass plugin.
    """
    mass = auth_manager.mass
    if hass_state == "not_loaded":
        del mass._providers["hass"]
    elif hass_state == "unavailable":
        mass._providers["hass"].available = False
    else:
        _set_hass_provider(mass, url=None)
    pending = auth_manager.pending_logins.start(
        provider.instance_id, AuthTransport.DIRECT, f"{HA_URL}/cb", idp_code_verifier="v"
    )

    assert not provider.sign_in_available
    providers = await auth_manager.get_login_providers()
    assert [p["provider_id"] for p in providers] == ["builtin"]
    assert await auth_manager.get_authorization_url("homeassistant") is None
    result = await auth_manager.handle_oauth_callback("ha_code", pending.state)
    assert result == AuthResult(success=False, error="Invalid provider")


async def test_ha_login_callback_creates_a_user_with_the_role_of_its_ha_account(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """A new Home Assistant user is created with its HA role, details and link."""
    result = await _ha_login_callback(
        auth_manager, provider, "ha_carol", ("carol", "Carol from HA", None), admin=True
    )

    assert result.success
    assert result.user is not None
    assert result.user.username == "carol"
    assert result.user.display_name == "Carol from HA"
    assert result.user.role == UserRole.ADMIN
    link = await _get_ha_link(auth_manager, "ha_carol")
    assert link is not None
    assert link["user_id"] == result.user.user_id


@pytest.mark.parametrize("display_name", [None, "Alice from HA"], ids=["no_details", "details"])
async def test_ha_login_callback_refuses_a_disabled_user(
    auth_manager: AuthenticationManager,
    provider: HomeAssistantAuthProvider,
    display_name: str | None,
) -> None:
    """
    The HA login callback refuses to sign in a disabled user.

    :param display_name: The display name Home Assistant returns for the user, if any.
    """
    await _create_user(auth_manager, "alice", ha_user_id="ha_alice", disabled=True)

    result = await _ha_login_callback(
        auth_manager, provider, "ha_alice", ("alice", display_name, None)
    )

    assert result == AuthResult(success=False, error="User account is disabled")


async def test_ha_login_callback_signs_in_an_enabled_linked_user(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """The HA login callback signs in the linked user and refreshes its display name."""
    linked = await _create_user(auth_manager, "alice", ha_user_id="ha_alice")

    result = await _ha_login_callback(
        auth_manager, provider, "ha_alice", ("alice", "Alice from HA", None)
    )

    assert result.success
    assert result.user is not None
    assert result.user.user_id == linked.user_id
    assert result.user.display_name == "Alice from HA"


async def test_ha_login_callback_refuses_a_new_user_with_self_registration_off(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """With self-registration off, the HA login refuses an unknown HA user and creates nothing."""
    auth_manager.webserver.config.update({CONF_AUTH_ALLOW_SELF_REGISTRATION: False})
    user_count = len(await auth_manager.list_users())

    result = await _ha_login_callback(
        auth_manager, provider, "ha_carol", ("carol", "Carol from HA", None)
    )

    assert result == AuthResult(
        success=False, error="Self-registration is disabled. Please contact an administrator."
    )
    assert len(await auth_manager.list_users()) == user_count
    assert await _get_ha_link(auth_manager, "ha_carol") is None


async def test_ha_login_callback_keeps_the_username_link_guards(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """A non-admin HA user named like an admin account is refused and not linked."""
    await auth_manager.create_user(username="alice", role=UserRole.ADMIN)

    result = await _ha_login_callback(auth_manager, provider, "ha_alice", ("alice", None, None))

    assert result == AuthResult(success=False, error=HA_LINK_REFUSED_MESSAGE)
    assert await _get_ha_link(auth_manager, "ha_alice") is None


@pytest.mark.parametrize(
    ("token_details", "token_error", "ha_user_id", "error"),
    [
        (
            None,
            ValueError("invalid_grant"),
            "ha_alice",
            "Home Assistant did not accept the sign-in",
        ),
        ({}, None, "ha_alice", "No access token received from HA"),
        ({"access_token": "t"}, None, None, "Failed to get user ID from Home Assistant"),
    ],
    ids=["code_refused", "no_access_token", "no_user_id"],
)
async def test_ha_login_callback_reports_home_assistant_errors(
    auth_manager: AuthenticationManager,
    provider: HomeAssistantAuthProvider,
    token_details: dict[str, Any] | None,
    token_error: Exception | None,
    ha_user_id: str | None,
    error: str,
) -> None:
    """
    A sign-in Home Assistant does not complete fails with its reason and creates nobody.

    :param token_details: What Home Assistant returns for the code.
    :param token_error: What Home Assistant raises for the code, if anything.
    :param ha_user_id: The user id Home Assistant reports for the token, if any.
    :param error: The expected error.
    """
    pending = _start_pending(auth_manager, provider)
    get_token = AsyncMock(return_value=token_details, side_effect=token_error)
    with (
        patch.object(hass_auth, "get_token", get_token),
        patch.object(
            provider, "_fetch_ha_user_id_via_websocket", AsyncMock(return_value=ha_user_id)
        ),
    ):
        result = await auth_manager.handle_oauth_callback("ha_code", pending.state)

    assert result == AuthResult(success=False, error=error)
    assert await auth_manager.list_users() == []


@pytest.mark.parametrize(
    ("origin", "expected_redirect_uri"),
    [
        ("http://192.168.1.10:8095", "http://192.168.1.10:8095/auth/callback"),
        ("http://localhost:8095", "http://localhost:8095/auth/callback"),
        ("https://example.com", "https://example.com/ma/auth/callback"),
        ("https://evil.example", "https://example.com/ma/auth/callback"),
    ],
)
@pytest.mark.usefixtures("provider")
async def test_ha_login_client_id_follows_the_callback_origin(
    auth_manager: AuthenticationManager,
    origin: str,
    expected_redirect_uri: str,
) -> None:
    """
    The client_id sent to Home Assistant has the scheme and host of the redirect URI.

    :param origin: The origin the client reached the server on.
    :param expected_redirect_uri: The expected callback URL.
    """
    with _webserver_urls("http://192.168.1.10:8095", "https://example.com/ma"):
        started = await auth_manager.get_authorization_url("homeassistant", origin=origin)
    assert started is not None
    query = parse_qs(urlparse(started[0]).query)

    assert query["redirect_uri"] == [expected_redirect_uri]
    redirect = urlparse(expected_redirect_uri)
    assert query["client_id"] == [f"{redirect.scheme}://{redirect.netloc}"]


async def test_ha_login_callback_exchanges_the_code_for_the_client_id_it_was_issued_to(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """The HA login exchanges its code with the client_id and PKCE pair it was started with."""
    with _webserver_urls("http://192.168.1.10:8095", "https://example.com/ma"):
        started = await auth_manager.get_authorization_url(
            "homeassistant", "https://example.com/ma/#/home"
        )
    assert started is not None
    assert started[0].startswith(f"{HA_URL}/auth/authorize?")
    query = parse_qs(urlparse(started[0]).query)
    assert query["client_id"] == ["https://example.com"]
    assert query["redirect_uri"] == ["https://example.com/ma/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert started[1].provider_id == provider.instance_id

    get_token = AsyncMock(return_value={})
    with patch.object(hass_auth, "get_token", get_token):
        result = await auth_manager.handle_oauth_callback("ha_code", query["state"][0])

    get_token.assert_awaited_once_with(
        HA_URL, "ha_code", client_id="https://example.com", code_verifier=ANY
    )
    assert get_token.await_args is not None
    code_verifier = get_token.await_args.kwargs["code_verifier"]
    digest = hashlib.sha256(code_verifier.encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert result == AuthResult(success=False, error="No access token received from HA")


@pytest.mark.usefixtures("provider")
async def test_ha_login_callback_refuses_an_expired_state(
    auth_manager: AuthenticationManager,
) -> None:
    """A HA login that completes after its sign-in expired is refused without a token exchange."""
    state = await _start_ha_login(auth_manager)

    get_token = AsyncMock(return_value={"access_token": "ha_token"})
    with (
        _monotonic_after(PENDING_LOGIN_TTL),
        patch.object(hass_auth, "get_token", get_token),
    ):
        result = await auth_manager.handle_oauth_callback("ha_code", state)

    assert result == AuthResult(success=False, error="Invalid or expired state parameter")
    get_token.assert_not_awaited()
    assert state not in auth_manager.pending_logins._pending


async def test_ha_login_callback_refuses_a_sign_in_that_returns_to_the_app(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """A HA login started for the remote app can not be completed on the server's callback."""
    pending = auth_manager.pending_logins.start(
        provider.instance_id,
        AuthTransport.REMOTE,
        "https://app.music-assistant.io/auth/callback/",
        redirect_target="app",
        client_code_challenge="x" * 43,
        idp_code_verifier="login_verifier",
    )

    get_token = AsyncMock(return_value={"access_token": "ha_token"})
    with patch.object(hass_auth, "get_token", get_token):
        result = await auth_manager.handle_oauth_callback("ha_code", pending.state)

    assert result == AuthResult(success=False, error="Invalid or expired state parameter")
    get_token.assert_not_awaited()


async def test_ha_login_callback_refuses_a_malformed_code(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """A HA login callback with a code that is not URL safe never reaches Home Assistant."""
    pending = _start_pending(auth_manager, provider)

    get_token = AsyncMock(return_value={"access_token": "ha_token"})
    with patch.object(hass_auth, "get_token", get_token):
        result = await auth_manager.handle_oauth_callback("ha_code&client_id=x", pending.state)

    assert result == AuthResult(success=False, error="Invalid authorization code")
    get_token.assert_not_awaited()


@pytest.mark.usefixtures("provider")
async def test_starting_a_ha_login_drops_expired_ones(
    auth_manager: AuthenticationManager,
) -> None:
    """Abandoned HA logins are dropped once they expired and a new one starts."""
    abandoned = [await _start_ha_login(auth_manager) for _ in range(3)]

    with _monotonic_after(PENDING_LOGIN_TTL):
        state = await _start_ha_login(auth_manager)

    assert list(auth_manager.pending_logins._pending) == [state]
    assert not set(abandoned) & set(auth_manager.pending_logins._pending)


@pytest.mark.usefixtures("provider")
async def test_pending_ha_logins_are_capped(
    auth_manager: AuthenticationManager,
) -> None:
    """Starting a HA login beyond the limit is refused and keeps the pending ones valid."""
    states = [await _start_ha_login(auth_manager) for _ in range(MAX_PENDING_LOGINS)]

    with pytest.raises(RateLimited):
        await auth_manager.get_authorization_url("homeassistant")
    assert list(auth_manager.pending_logins._pending) == states

    get_token = AsyncMock(return_value={})
    with patch.object(hass_auth, "get_token", get_token):
        result = await auth_manager.handle_oauth_callback("ha_code", states[0])
    assert result == AuthResult(success=False, error="No access token received from HA")
    get_token.assert_awaited_once()


async def test_concurrent_ha_logins_stay_within_the_limit(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """HA logins that start together while Home Assistant is slow to answer stay within the limit."""
    ha_answers = asyncio.Event()

    async def slow_ha_url() -> str:
        await ha_answers.wait()
        return HA_URL

    with patch.object(provider, "_get_external_ha_url", slow_ha_url):
        starts = [
            asyncio.create_task(auth_manager.get_authorization_url("homeassistant"))
            for _ in range(MAX_PENDING_LOGINS + 10)
        ]
        await asyncio.sleep(0)
        ha_answers.set()
        results = await asyncio.gather(*starts, return_exceptions=True)

    assert len(auth_manager.pending_logins._pending) == MAX_PENDING_LOGINS
    assert sum(isinstance(result, RateLimited) for result in results) == 10


@pytest.mark.parametrize(
    ("configured_url", "network_urls", "expected_ha_url"),
    [
        (
            "http://supervisor/core/api",
            {"external": "https://ha.example.com", "cloud": None, "internal": None},
            "https://ha.example.com",
        ),
        (
            "http://supervisor/core/api",
            {
                "external": None,
                "cloud": "https://abc.ui.nabu.casa",
                "internal": "http://192.168.1.5:8123",
            },
            "https://abc.ui.nabu.casa",
        ),
        ("https://ha.example.org", None, "https://ha.example.org"),
        (
            "http://supervisor/core/api",
            {"external": None, "cloud": None, "internal": "http://192.168.1.5:8123"},
            None,
        ),
        (
            "http://supervisor/core/api",
            {"external": "http://homeassistant.local:8123", "cloud": None, "internal": None},
            None,
        ),
        ("http://192.168.1.5:8123", {"external": "http://10.0.0.5:8123"}, None),
        ("http://supervisor/core/api", None, None),
    ],
    ids=[
        "external_url",
        "cloud_url",
        "public_configured_url",
        "internal_url_only",
        "mdns_external_url",
        "private_urls",
        "home_assistant_unreachable",
    ],
)
async def test_ha_login_for_the_remote_app_needs_a_public_home_assistant_url(
    auth_manager: AuthenticationManager,
    provider: HomeAssistantAuthProvider,
    configured_url: str,
    network_urls: dict[str, str | None] | None,
    expected_ha_url: str | None,
) -> None:
    """
    A HA login returns to the remote app only through a Home Assistant URL that is public.

    :param configured_url: The Home Assistant URL the hass provider is configured with.
    :param network_urls: What Home Assistant reports as its URLs, None when not connected.
    :param expected_ha_url: The Home Assistant URL the browser is sent to, if any.
    """
    _set_hass_provider(auth_manager.mass, url=configured_url, network_urls=network_urls)
    pending = auth_manager.pending_logins.start(
        provider.instance_id,
        AuthTransport.REMOTE,
        "https://app.music-assistant.io/auth/callback/",
        redirect_target="app",
        client_code_challenge="c" * 43,
        idp_code_verifier="ha_verifier",
    )

    if expected_ha_url is None:
        with pytest.raises(LoginFlowError):
            await provider.build_authorization_url(pending)
        assert not provider.supports_remote_app
        return
    auth_url = await provider.build_authorization_url(pending)
    assert provider.supports_remote_app
    assert auth_url.startswith(f"{expected_ha_url}/auth/authorize?")
    query = parse_qs(urlparse(auth_url).query)
    assert query["client_id"] == ["https://app.music-assistant.io"]
    assert query["redirect_uri"] == ["https://app.music-assistant.io/auth/callback/"]
    assert query["state"] == [pending.state]
    digest = hashlib.sha256(b"ha_verifier").digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert query["code_challenge_method"] == ["S256"]


async def test_remote_app_support_reuses_the_ha_urls_between_sign_in_listings(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """Listing the sign-in methods again does not ask Home Assistant for its URLs again."""
    hass_provider = _set_hass_provider(
        auth_manager.mass,
        url="http://supervisor/core/api",
        network_urls={"external": "https://ha.example.com"},
    )

    # the first listing starts the lookup
    assert isinstance(provider.supports_remote_app, bool)
    await asyncio.sleep(0)
    assert provider.supports_remote_app
    assert provider.supports_remote_app

    hass_provider.hass.send_command.assert_awaited_once_with("network/url")


async def test_concurrent_ha_url_lookups_share_one_request(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """Lookups of the public Home Assistant URL that arrive together ask Home Assistant once."""
    hass_provider = _set_hass_provider(
        auth_manager.mass,
        url="http://supervisor/core/api",
        network_urls={"external": "https://ha.example.com"},
    )

    results = await asyncio.gather(*(provider._get_public_ha_url() for _ in range(5)))

    assert results == ["https://ha.example.com"] * 5
    hass_provider.hass.send_command.assert_awaited_once_with("network/url")


async def test_a_failed_ha_lookup_is_not_retried_right_away(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> None:
    """After Home Assistant fails to report its URLs, lookups wait before asking again."""
    hass_provider = _set_hass_provider(
        auth_manager.mass, url="http://supervisor/core/api", network_urls={}
    )
    hass_provider.hass.send_command.side_effect = TimeoutError

    assert await provider._get_public_ha_url() is None
    assert await provider._get_public_ha_url() is None

    hass_provider.hass.send_command.assert_awaited_once_with("network/url")


@pytest.mark.parametrize(
    ("disabled", "self_registration", "translation_key"),
    [(True, True, "user_account_disabled"), (False, False, "self_registration_disabled")],
    ids=["disabled_user", "self_registration_off"],
)
async def test_ha_login_exchange_refuses_a_user_that_may_not_sign_in(
    auth_manager: AuthenticationManager,
    provider: HomeAssistantAuthProvider,
    disabled: bool,
    self_registration: bool,
    translation_key: str,
) -> None:
    """
    The remote app exchange refuses a disabled user and an unknown one without self-registration.

    :param disabled: Whether the linked user exists and is disabled (else no user exists).
    :param self_registration: Whether self-registration is allowed.
    :param translation_key: The expected translation key of the error.
    """
    mass = auth_manager.mass
    auth_manager.webserver.config.update({CONF_AUTH_ALLOW_SELF_REGISTRATION: self_registration})
    if disabled:
        await _create_user(auth_manager, "alice", ha_user_id="ha_alice", disabled=True)
    user_count = len(await auth_manager.list_users())
    verifier = "client-verifier-" + "v" * 40
    pending = auth_manager.pending_logins.start(
        provider.instance_id,
        AuthTransport.REMOTE,
        "https://app.music-assistant.io/auth/callback/",
        redirect_target="app",
        client_code_challenge=pkce_challenge(verifier),
        idp_code_verifier="ha_verifier",
    )
    _set_hass_provider(mass, ha_user_id="ha_alice", details=("alice", None, None))
    get_token = AsyncMock(return_value={"access_token": "ha_token"})

    with (
        patch.object(hass_auth, "get_token", get_token),
        patch.object(
            provider, "_fetch_ha_user_id_via_websocket", AsyncMock(return_value="ha_alice")
        ),
    ):
        result = await auth_manager.exchange_authorization_code(pending.state, "ha_code", verifier)

    assert isinstance(result, AuthFailure)
    assert result.success is False
    assert result.translation_key == translation_key
    get_token.assert_awaited_once_with(
        HA_URL,
        "ha_code",
        client_id="https://app.music-assistant.io",
        code_verifier="ha_verifier",
    )
    assert len(await auth_manager.list_users()) == user_count


def _set_hass_provider(
    mass: MusicAssistant,
    *,
    url: str | None = HA_URL,
    network_urls: dict[str, str | None] | None = None,
    ha_user_id: str = "ha_alice",
    admin: bool = False,
    details: tuple[str | None, str | None, str | None] = (None, None, None),
) -> MagicMock:
    """
    Load a mock hass plugin, marked ready, and return it.

    :param mass: The server to load the mock on.
    :param url: The Home Assistant URL the plugin is configured with.
    :param network_urls: The URLs Home Assistant reports, None when it is not connected.
    :param ha_user_id: The Home Assistant user id the plugin knows about.
    :param admin: Whether that Home Assistant account is an admin.
    :param details: The (username, display_name, avatar_url) the plugin returns for the user.
    """
    hass_provider = MagicMock(domain="hass", instance_id="hass", available=True, url=url)
    hass_provider.get_user_details = AsyncMock(return_value=details)
    hass_provider.hass.connected = network_urls is not None
    group_ids = ["system-admin"] if admin else ["system-users"]

    async def send_command(command: str) -> Any:
        if command == "network/url":
            return network_urls
        return [{"id": ha_user_id, "group_ids": group_ids}]

    hass_provider.hass.send_command = AsyncMock(side_effect=send_command)
    mass._providers["hass"] = hass_provider
    mass.get_provider_ready_event("hass").set()
    return hass_provider


async def _ha_login_callback(
    auth_manager: AuthenticationManager,
    provider: HomeAssistantAuthProvider,
    ha_user_id: str,
    details: tuple[str | None, str | None, str | None],
    *,
    admin: bool = False,
) -> AuthResult:
    """
    Complete a Home Assistant login for the given HA user and return its result.

    :param auth_manager: The authentication manager the user signs in with.
    :param provider: The Home Assistant sign-in provider.
    :param ha_user_id: The Home Assistant user id the login resolves to.
    :param details: The (username, display_name, avatar_url) Home Assistant returns for the user.
    :param admin: Whether the Home Assistant account is an admin.
    """
    _set_hass_provider(auth_manager.mass, ha_user_id=ha_user_id, admin=admin, details=details)
    pending = _start_pending(auth_manager, provider)
    with (
        patch.object(hass_auth, "get_token", AsyncMock(return_value={"access_token": "ha_token"})),
        patch.object(
            provider, "_fetch_ha_user_id_via_websocket", AsyncMock(return_value=ha_user_id)
        ),
    ):
        return await auth_manager.handle_oauth_callback("ha_code", pending.state)


def _start_pending(
    auth_manager: AuthenticationManager, provider: HomeAssistantAuthProvider
) -> login_flow.PendingLogin:
    """
    Start a Home Assistant sign-in that returns to the server and return it.

    :param auth_manager: The authentication manager to start the sign-in on.
    :param provider: The Home Assistant sign-in provider.
    """
    return auth_manager.pending_logins.start(
        provider.instance_id,
        AuthTransport.DIRECT,
        "http://ma.local:8095/auth/callback",
        idp_code_verifier="login_verifier",
    )


async def _start_ha_login(auth_manager: AuthenticationManager) -> str:
    """
    Start a HA login and return its OAuth state.

    :param auth_manager: The authentication manager to start the login with.
    """
    started = await auth_manager.get_authorization_url("homeassistant")
    assert started is not None
    return parse_qs(urlparse(started[0]).query)["state"][0]


async def _create_user(
    auth_manager: AuthenticationManager,
    username: str,
    *,
    ha_user_id: str | None = None,
    disabled: bool = False,
) -> User:
    """
    Create a user with display name "Old name", optionally linked to HA and disabled.

    :param auth_manager: The authentication manager to create the user with.
    :param username: The username of the user.
    :param ha_user_id: The Home Assistant user id to link the user to, if any.
    :param disabled: Whether to disable the user's account (as an admin would).
    """
    user = await auth_manager.create_user(username=username, display_name="Old name")
    if ha_user_id:
        await auth_manager.link_user_to_provider(user, AuthProviderType.HOME_ASSISTANT, ha_user_id)
    if disabled:
        admin = await auth_manager.create_user(username=f"{username}_admin", role=UserRole.ADMIN)
        set_current_user(admin)
        await auth_manager.disable_user(user.user_id)
    return user


async def _get_ha_link(
    auth_manager: AuthenticationManager, ha_user_id: str
) -> dict[str, object] | None:
    """
    Return the stored link of the given Home Assistant user id, if any.

    :param auth_manager: The authentication manager whose database holds the links.
    :param ha_user_id: The Home Assistant user id to look up.
    """
    row = await auth_manager.database.get_row(
        "user_auth_providers",
        {"provider_type": AuthProviderType.HOME_ASSISTANT.value, "provider_user_id": ha_user_id},
    )
    return dict(row) if row else None


@contextmanager
def _webserver_urls(base_url: str, external_url: str | None) -> Iterator[None]:
    """
    Make the webserver report the given base URL and External URL.

    :param base_url: The base URL the webserver reports.
    :param external_url: The External URL the webserver reports.
    """
    with (
        patch.object(
            WebserverController, "base_url", new_callable=PropertyMock, return_value=base_url
        ),
        patch.object(
            WebserverController,
            "external_url",
            new_callable=PropertyMock,
            return_value=external_url,
        ),
    ):
        yield


@contextmanager
def _monotonic_after(seconds: float) -> Iterator[None]:
    """
    Make the pending sign-ins see a clock the given number of seconds ahead.

    :param seconds: How far ahead of the real clock the pending sign-ins' clock runs.
    """
    now = time.monotonic()
    clock = MagicMock(monotonic=MagicMock(return_value=now + seconds))
    with patch.object(login_flow, "time", clock):
        yield
