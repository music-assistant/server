"""Tests for the auth provider base class and its wiring into the server."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import AuthProviderType, SignInMethod
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import CoreState, EventType, ProviderType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_PROVIDERS, HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.webserver import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    _parse_provider_user_arg,
)
from music_assistant.controllers.webserver.helpers.auth_providers import (
    HA_LINK_REFUSED_MESSAGE,
    AuthFailure,
    AuthResult,
)
from music_assistant.controllers.webserver.helpers.login_flow import (
    AuthTransport,
    ExternalIdentity,
    LoginFlowError,
    PendingLogin,
)
from music_assistant.helpers.pkce import pkce_challenge
from music_assistant.mass import MusicAssistant
from music_assistant.models.auth_provider import AuthProvider
from music_assistant.testing import full_mass_context

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    from music_assistant_models.event import MassEvent

DOMAIN = "fake_auth"
INSTANCE_ID = f"{DOMAIN}--test"
STORED_CONF = {"type": "auth", "domain": DOMAIN, "instance_id": INSTANCE_ID, "enabled": True}


class FakeAuthProvider(AuthProvider):
    """Auth provider that signs nobody in, for tests."""

    state_at_init: CoreState | None = None

    async def handle_async_init(self) -> None:
        """Record the server state the provider is set up in."""
        self.state_at_init = self.mass.state

    async def build_authorization_url(self, pending: PendingLogin) -> str:
        """Return a fixed authorization URL."""
        return f"https://idp.example/authorize?state={pending.state}"

    async def complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> ExternalIdentity:
        """Refuse every sign-in."""
        raise LoginFlowError("login_failed")


class UnavailableAuthProvider(FakeAuthProvider):
    """Auth provider that can not be used right now."""

    @property
    def sign_in_available(self) -> bool:
        """Return that the method is not available."""
        return False

    @property
    def unavailable_reason(self) -> str | None:
        """Return why the method is not available."""
        return "idp_unreachable"


class RelabelledAuthProvider(FakeAuthProvider):
    """Auth provider with its own sign-in method id and button label."""

    @property
    def sign_in_method_id(self) -> str:
        """Return a fixed sign-in method id."""
        return "fixed_id"

    @property
    def sign_in_label(self) -> str:
        """Return a button label that differs from the provider name."""
        return "Company login"


class IdentityAuthProvider(FakeAuthProvider):
    """Auth provider that signs in the identity a test gives it."""

    identity: ExternalIdentity | None = None

    @property
    def sign_in_method_id(self) -> str:
        """Return a fixed sign-in method id."""
        return "idp"

    async def complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> ExternalIdentity:
        """Return the identity of the test, or refuse when there is none."""
        if self.identity is None:
            raise LoginFlowError("idp_refused", "The identity provider refused")
        return self.identity


def _manifest() -> ProviderManifest:
    return ProviderManifest.from_dict(
        {
            "type": "auth",
            "domain": DOMAIN,
            "name": "Fake sign-in",
            "description": "Signs nobody in",
            "codeowners": [],
            "icon": "mdi-key",
            "multi_instance": True,
        }
    )


def _conf() -> ProviderConfig:
    return ProviderConfig(
        values={}, type=ProviderType.AUTH, domain=DOMAIN, instance_id=INSTANCE_ID, enabled=True
    )


def _start_pending(mass: MusicAssistant, provider_id: str) -> PendingLogin:
    return mass.webserver.auth.pending_logins.start(
        provider_id, AuthTransport.DIRECT, "http://localhost/callback", "v" * 43
    )


@pytest.fixture
def mass_auth(mass_minimal: MusicAssistant) -> MusicAssistant:
    """Return a minimal server with an auth manager and no library."""
    mass_minimal.webserver = WebserverController(mass_minimal)
    mass_minimal.music = MagicMock(unschedule_provider_sync=AsyncMock())
    mass_minimal.discovery = MagicMock(run_provider_discovery=AsyncMock())
    return mass_minimal


@pytest.fixture
async def idp(mass_auth: MusicAssistant) -> AsyncGenerator[IdentityAuthProvider]:
    """Load an identity provider on a server whose auth manager is set up."""
    webserver = mass_auth.webserver
    webserver.config = await mass_auth.config.get_core_config("webserver")
    # creating a user migrates playlog rows through the music controller
    music = MagicMock(unschedule_provider_sync=AsyncMock())
    music.database.execute = AsyncMock()
    music.database.commit = AsyncMock()
    mass_auth.music = music
    await webserver.auth.setup()
    provider = IdentityAuthProvider(mass_auth, _manifest(), _conf())
    provider.available = True
    mass_auth._providers[INSTANCE_ID] = provider
    try:
        yield provider
    finally:
        mass_auth._providers.pop(INSTANCE_ID, None)
        await webserver.auth.close()


async def test_get_sign_in_method(mass_auth: MusicAssistant) -> None:
    """The base class builds the sign-in method from the provider's members."""
    provider = FakeAuthProvider(mass_auth, _manifest(), _conf())
    assert provider.get_sign_in_method() == SignInMethod(
        provider_id=INSTANCE_ID,
        provider_type=DOMAIN,
        name="Fake sign-in",
        icon="mdi-key",
        requires_redirect=True,
        available=True,
        unavailable_reason=None,
        supports_remote_app=False,
    )
    assert provider.uses_pkce
    assert provider.uses_nonce
    assert provider.logout_url() is None

    unavailable = UnavailableAuthProvider(mass_auth, _manifest(), _conf())
    method = unavailable.get_sign_in_method()
    assert not method.available
    assert method.unavailable_reason == "idp_unreachable"

    relabelled = RelabelledAuthProvider(mass_auth, _manifest(), _conf()).get_sign_in_method()
    assert relabelled.provider_id == "fixed_id"
    assert relabelled.name == "Company login"


async def test_load_and_unload_notify_the_auth_manager(mass_auth: MusicAssistant) -> None:
    """Load and unload fire the event, and unload drops only that provider's sign-ins."""
    mass_auth.config.set(f"{CONF_PROVIDERS}/{INSTANCE_ID}", dict(STORED_CONF))
    events: list[MassEvent] = []
    mass_auth.subscribe(events.append, EventType.AUTH_SIGNIN_METHODS_UPDATED)
    auth = mass_auth.webserver.auth
    provider = FakeAuthProvider(mass_auth, _manifest(), _conf())

    with patch.object(auth, "on_auth_provider_changed", wraps=auth.on_auth_provider_changed) as spy:
        await mass_auth._register_loaded_provider(provider, _conf())
        await mass_auth._tracked_tasks[f"post_load_provider_{INSTANCE_ID}"]
        await asyncio.sleep(0)
        spy.assert_called_once_with(INSTANCE_ID, loaded=True)
        assert len(events) == 1
        assert events[0].data is None
        assert [m.provider_id for m in auth.get_sign_in_methods()] == [INSTANCE_ID]

        own = _start_pending(mass_auth, INSTANCE_ID)
        other = _start_pending(mass_auth, "other--provider")
        await mass_auth.unload_provider(INSTANCE_ID)
        await asyncio.sleep(0)
        spy.assert_called_with(INSTANCE_ID, loaded=False)

    assert len(events) == 2
    assert auth.get_sign_in_methods() == []
    assert auth.pending_logins.pop(own.state) is None
    assert auth.pending_logins.pop(other.state) is other


@pytest.mark.parametrize("safe_mode", [False, True])
async def test_auth_provider_loads_before_running(tmp_path: Path, safe_mode: bool) -> None:
    """An enabled auth provider is loaded before the server runs, in safe mode too."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    settings = {CONF_PROVIDERS: {INSTANCE_ID: STORED_CONF}}
    (data_path / "settings.json").write_text(json.dumps(settings))
    module = MagicMock()
    module.setup = AsyncMock(
        side_effect=lambda mass, manifest, config: FakeAuthProvider(mass, manifest, config)
    )
    original_load_manifests = MusicAssistant._MusicAssistant__load_provider_manifests  # type: ignore[attr-defined]

    async def load_manifests(self: MusicAssistant) -> None:
        await original_load_manifests(self)
        self._provider_manifests[DOMAIN] = _manifest()

    with (
        patch.object(MusicAssistant, "_MusicAssistant__load_provider_manifests", load_manifests),
        patch("music_assistant.mass.load_provider_module", AsyncMock(return_value=module)),
    ):
        async with full_mass_context(tmp_path, safe_mode=safe_mode) as mass:
            provider = mass.get_provider(INSTANCE_ID)
            assert isinstance(provider, FakeAuthProvider)
            assert provider.state_at_init not in (None, CoreState.RUNNING)
            assert INSTANCE_ID in [m.provider_id for m in mass.webserver.auth.get_sign_in_methods()]


async def test_remove_auth_provider_config(mass_auth: MusicAssistant) -> None:
    """Removing an auth provider drops its pending sign-ins, and only those."""
    config = mass_auth.config
    config.set(f"{CONF_PROVIDERS}/{INSTANCE_ID}", dict(STORED_CONF))
    own = _start_pending(mass_auth, INSTANCE_ID)
    other = _start_pending(mass_auth, "other--provider")
    with patch.object(mass_auth, "unload_provider", AsyncMock()) as unload:
        await config.remove_provider_config(INSTANCE_ID)

    unload.assert_awaited_once_with(INSTANCE_ID, True)
    assert config.get(f"{CONF_PROVIDERS}/{INSTANCE_ID}") is None
    pending_logins = mass_auth.webserver.auth.pending_logins
    assert pending_logins.pop(own.state) is None
    assert pending_logins.pop(other.state) is other


async def test_redirect_sign_in_refuses_an_unknown_method(idp: IdentityAuthProvider) -> None:
    """A sign-in for a method id no loaded auth provider offers is neither started nor completed."""
    auth = idp.mass.webserver.auth
    pending = _start_pending(idp.mass, INSTANCE_ID)

    # the instance id is not the sign-in method id
    for method_id in ("unknown", INSTANCE_ID):
        assert await auth.get_authorization_url(method_id) is None
        result = await auth.get_auth_url(method_id)
        assert result["authorization_url"] is None
    callback = await auth.handle_oauth_callback("unknown", "code", pending.state)

    assert callback == AuthResult(success=False, error="Invalid provider")
    assert list(auth.pending_logins._pending) == [pending.state]


async def test_redirect_sign_in_dispatches_to_the_auth_provider(
    idp: IdentityAuthProvider,
) -> None:
    """A redirect sign-in is started and completed by the auth provider of its method id."""
    auth = idp.mass.webserver.auth
    user = await auth.create_user(username="alice", display_name="Alice")
    await auth.link_user_to_provider(user, AuthProviderType.HOME_ASSISTANT, "ha_alice")
    idp.identity = _ha_identity("ha_alice", "alice", display_name="Alice from HA")

    started = await auth.get_authorization_url("idp")
    assert started is not None
    auth_url, pending = started
    assert auth_url == f"https://idp.example/authorize?state={pending.state}"
    assert pending.provider_id == INSTANCE_ID
    assert pending.redirect_uri.endswith("/auth/callback?provider_id=idp")
    result = await auth.handle_oauth_callback("idp", "idp_code", pending.state)

    assert result.success
    assert result.user is not None
    assert result.user.user_id == user.user_id
    assert result.user.display_name == "Alice from HA"


@pytest.mark.parametrize("case", ["linked_elsewhere", "system_user"])
async def test_redirect_sign_in_keeps_the_ha_link_guards(
    idp: IdentityAuthProvider, case: str
) -> None:
    """
    An identity the Home Assistant link guards refuse signs nobody in and links nothing.

    :param case: Why the link is refused.
    """
    auth = idp.mass.webserver.auth
    if case == "linked_elsewhere":
        other = await auth.create_user(username="alice")
        await auth.link_user_to_provider(other, AuthProviderType.HOME_ASSISTANT, "ha_other")
        idp.identity = _ha_identity("ha_alice", "alice")
    else:
        idp.identity = _ha_identity("ha_alice", HOMEASSISTANT_SYSTEM_USER)
    pending = _start_pending(idp.mass, INSTANCE_ID)

    result = await auth.handle_oauth_callback("idp", "idp_code", pending.state)

    assert result == AuthResult(success=False, error=HA_LINK_REFUSED_MESSAGE)
    assert await auth.get_user_by_provider_link(AuthProviderType.HOME_ASSISTANT, "ha_alice") is None


@pytest.mark.parametrize(
    ("identity", "error", "translation_key"),
    [
        (None, "The identity provider refused", "idp_refused"),
        (
            ExternalIdentity(provider_type=AuthProviderType.BUILTIN, issuer="i", subject="s"),
            "Signing in with this identity type is not supported",
            "authentication_failed",
        ),
    ],
    ids=["provider_refusal", "unsupported_identity_type"],
)
async def test_app_sign_in_passes_on_a_refusal(
    idp: IdentityAuthProvider,
    identity: ExternalIdentity | None,
    error: str,
    translation_key: str,
) -> None:
    """
    A refused remote app sign-in reports why, with the translation key of the reason.

    :param identity: The identity the provider returns, None when it refuses.
    :param error: The expected error.
    :param translation_key: The expected translation key.
    """
    auth = idp.mass.webserver.auth
    idp.identity = identity
    verifier = "client-verifier-" + "v" * 40
    pending = auth.pending_logins.start(
        INSTANCE_ID,
        AuthTransport.REMOTE,
        "https://app.music-assistant.io/auth/callback/",
        "v" * 43,
        redirect_target="app",
        client_code_challenge=pkce_challenge(verifier),
    )

    result = await auth.exchange_authorization_code(pending.state, "idp_code", verifier)

    assert isinstance(result, AuthFailure)
    assert result.error == error
    assert result.translation_key == translation_key
    assert await auth.list_users() == []


def _ha_identity(subject: str, username: str, display_name: str | None = None) -> ExternalIdentity:
    """
    Return a Home Assistant identity.

    :param subject: The Home Assistant user id.
    :param username: The Home Assistant username.
    :param display_name: The display name from Home Assistant, if any.
    """
    return ExternalIdentity(
        provider_type=AuthProviderType.HOME_ASSISTANT,
        issuer="http://ha.local:8123",
        subject=subject,
        username=username,
        display_name=display_name,
    )


@pytest.mark.parametrize("provider", ["unknown", "not_a_provider", None])
def test_impersonation_refuses_unknown_provider_type(provider: Any) -> None:
    """The impersonation argument never resolves to the UNKNOWN provider type."""
    with pytest.raises(InvalidDataError, match="Invalid auth provider type"):
        _parse_provider_user_arg({"provider": provider, "user_id": "someone"})
