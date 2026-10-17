"""Optional OpenID Connect login provider."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.auth import AuthProviderType, User, UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import AuthenticationFailed
from slugify import slugify

from music_assistant.helpers.json import json_dumps

from .auth_providers import AuthResult, LoginProvider, LoginProviderConfig
from .oidc import TOKEN_AUTH_METHODS, OIDCClient, validate_oidc_url

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

    from .login_flow import PendingLogin

CONF_OIDC_ENABLED: Final = "oidc_enabled"
CONF_OIDC_NAME: Final = "oidc_name"
CONF_OIDC_ISSUER: Final = "oidc_issuer"
CONF_OIDC_CLIENT_ID: Final = "oidc_client_id"
CONF_OIDC_CLIENT_SECRET: Final = "oidc_client_secret"  # noqa: S105 - config key
CONF_OIDC_TOKEN_AUTH_METHOD: Final = "oidc_token_auth_method"  # noqa: S105 - config key
CONF_OIDC_ALLOW_SELF_REGISTRATION: Final = "oidc_allow_self_registration"
OIDC_PROVIDER_ID: Final = "oidc"


def oidc_config_entries() -> tuple[ConfigEntry, ...]:
    """Return opt-in OpenID Connect settings for the webserver core configuration."""
    return (
        ConfigEntry(
            key=CONF_OIDC_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_NAME,
            type=ConfigEntryType.STRING,
            default_value="OpenID Connect",
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_ISSUER,
            type=ConfigEntryType.STRING,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
            validate=lambda value: isinstance(value, str) and validate_oidc_url(value, issuer=True),
        ),
        ConfigEntry(
            key=CONF_OIDC_CLIENT_ID,
            type=ConfigEntryType.STRING,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_CLIENT_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_TOKEN_AUTH_METHOD,
            type=ConfigEntryType.STRING,
            default_value="client_secret_basic",
            options=[ConfigValueOption(x, x) for x in TOKEN_AUTH_METHODS],
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_ALLOW_SELF_REGISTRATION,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
        ),
    )


def oidc_identity_id(issuer: str, subject: str) -> str:
    """Return the stable provider link ID for an exact OIDC issuer and subject pair."""
    return hashlib.sha256(json_dumps([issuer, subject]).encode()).hexdigest()


class OIDCProviderConfig(LoginProviderConfig):
    """Configuration for one optional OpenID Connect identity provider."""

    issuer: str
    client_id: str
    client_secret: str
    token_auth_method: str
    name: str
    allow_self_registration: bool


class OIDCLoginProvider(LoginProvider):
    """Sign in through an OIDC identity provider without changing other login methods."""

    def __init__(self, mass: MusicAssistant, config: OIDCProviderConfig) -> None:
        """Initialize the provider without fetching discovery or contacting the identity provider."""
        super().__init__(mass, OIDC_PROVIDER_ID, config)
        self._oidc_config = config
        self.client = OIDCClient(
            mass.http_session,
            config["issuer"],
            config["client_id"],
            config["client_secret"],
            config["token_auth_method"],
        )
        self._user_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Return the display name configured by the administrator."""
        return self._oidc_config["name"]

    @property
    def allow_self_registration(self) -> bool:
        """Return whether verified, unlinked OIDC identities may create regular accounts."""
        return self._oidc_config["allow_self_registration"]

    @property
    def provider_type(self) -> AuthProviderType:
        """Return the OIDC authentication provider type."""
        return AuthProviderType.OIDC

    @property
    def requires_redirect(self) -> bool:
        """Return True because OIDC signs in at the identity provider."""
        return True

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """Refuse credential-based sign-in; OIDC requires the authorization code flow."""
        return AuthResult(success=False, error="Use the OpenID Connect sign-in flow")

    async def build_authorization_url(self, pending: PendingLogin) -> str | None:
        """Build the identity provider URL for this pending sign-in."""
        try:
            return await self.client.authorization_url(pending)
        except Exception as err:
            self.logger.warning("OIDC authorization could not start (%s)", type(err).__name__)
            raise AuthenticationFailed("OpenID Connect provider is unavailable") from err

    async def complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> User:
        """Verify an OIDC authorization code and resolve its linked Music Assistant user."""
        if not (code := params.get("code")):
            raise AuthenticationFailed("No authorization code received")
        try:
            claims = await self.client.authenticate(pending, code)
        except Exception as err:
            # Never expose provider response bodies, codes, tokens or secrets in errors/logs.
            self.logger.warning("OIDC sign-in validation failed (%s)", type(err).__name__)
            raise AuthenticationFailed("OpenID Connect sign-in failed") from err
        async with self._user_lock:
            return await self._get_or_create_user(claims)

    async def _get_or_create_user(self, claims: dict[str, Any]) -> User:
        identity_id = oidc_identity_id(self.client.issuer, claims["sub"])
        user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.OIDC, identity_id, include_disabled=True
        )
        if user is not None:
            if not user.enabled:
                raise AuthenticationFailed(
                    "User account is disabled", translation_key="user_account_disabled"
                )
            return user
        if not self.allow_self_registration:
            raise AuthenticationFailed(
                "Self-registration is disabled. Please contact an administrator.",
                translation_key="self_registration_disabled",
            )
        if not self.auth_manager.has_users:
            raise AuthenticationFailed("Create a local administrator before enabling OIDC")
        preferred_username = claims.get("preferred_username")
        username = slugify(preferred_username) if isinstance(preferred_username, str) else ""
        username = username[:50] if len(username) >= 2 else "oidc-user"
        # An IdP username/email is not proof of ownership of a local or HA account.
        # A collision creates a distinct account, without touching the existing user.
        if await self.auth_manager.get_user_by_username(username, include_disabled=True):
            username = f"{username[:37]}-{identity_id[:12]}"
        display_name = claims.get("name")
        user = await self.auth_manager.create_user(
            username=username,
            role=UserRole.USER,
            display_name=display_name
            if isinstance(display_name, str) and display_name
            else username,
        )
        await self.auth_manager.link_user_to_provider(user, AuthProviderType.OIDC, identity_id)
        return user
