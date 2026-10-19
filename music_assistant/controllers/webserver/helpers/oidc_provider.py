"""Optional OpenID Connect login provider."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, NotRequired

from aiohttp import ClientError
from jwt import PyJWTError
from music_assistant_models.auth import AuthProviderType, User, UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import AuthenticationFailed, InvalidDataError
from slugify import slugify

from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.util import run_to_completion

from .auth_providers import AuthResult, LoginProvider, LoginProviderConfig
from .oidc import (
    DEFAULT_OIDC_SCOPES,
    TOKEN_AUTH_METHODS,
    OIDCClient,
    OIDCProtocolError,
    validate_oidc_scopes,
    validate_oidc_url,
)
from .oidc_sessions import OIDCSession

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
CONF_OIDC_BACKCHANNEL_LOGOUT: Final = "oidc_backchannel_logout"
CONF_OIDC_GROUPS_CLAIM: Final = "oidc_groups_claim"
CONF_OIDC_ROLE_MAPPING: Final = "oidc_role_mapping"
CONF_OIDC_SCOPES: Final = "oidc_scopes"
OIDC_PROVIDER_ID: Final = "oidc"


def oidc_config_entries() -> tuple[ConfigEntry, ...]:
    """Return the webserver's OpenID Connect settings."""
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
            key=CONF_OIDC_SCOPES,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_OIDC_SCOPES,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
            advanced=True,
            validate=lambda value: isinstance(value, str) and validate_oidc_scopes(value),
        ),
        ConfigEntry(
            key=CONF_OIDC_ALLOW_SELF_REGISTRATION,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_BACKCHANNEL_LOGOUT,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_GROUPS_CLAIM,
            type=ConfigEntryType.STRING,
            default_value="groups",
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_OIDC_ROLE_MAPPING,
            type=ConfigEntryType.STRING,
            default_value="",
            required=False,
            depends_on=CONF_OIDC_ENABLED,
            requires_reload=True,
            advanced=True,
        ),
    )


def oidc_identity_id(issuer: str, subject: str) -> str:
    """Hash the exact issuer/subject pair into a provider link ID."""
    return hashlib.sha256(json_dumps([issuer, subject]).encode()).hexdigest()


def parse_role_mapping(value: str) -> dict[str, str]:
    """Parse an ordered group-to-role map; empty keeps local role management."""
    if not value.strip():
        return {}
    if len(value) > 16384:
        raise ValueError("OpenID Connect role mapping is too large")
    try:
        mapping = json_loads(value)
    except ValueError as err:
        raise ValueError("OpenID Connect role mapping must be a JSON object") from err
    if not isinstance(mapping, dict) or len(mapping) > 100:
        raise ValueError(
            "OpenID Connect role mapping must be a JSON object with at most 100 groups"
        )
    for group, role in mapping.items():
        if (
            not isinstance(group, str)
            or not 0 < len(group) <= 255
            or not isinstance(role, str)
            or not 0 < len(role) <= 255
            or role == UserRole.SERVICE
        ):
            raise ValueError("Invalid OpenID Connect group or role")
    return mapping


class OIDCProviderConfig(LoginProviderConfig):
    """Configuration for one optional OpenID Connect identity provider."""

    issuer: str
    client_id: str
    client_secret: str
    token_auth_method: str
    name: str
    allow_self_registration: bool
    backchannel_logout: NotRequired[bool]
    groups_claim: NotRequired[str]
    role_mapping: NotRequired[dict[str, str]]
    scopes: NotRequired[str]


class OIDCLoginProvider(LoginProvider):
    """Sign in through an OIDC identity provider."""

    def __init__(self, mass: MusicAssistant, config: OIDCProviderConfig) -> None:
        """Initialize without contacting the identity provider."""
        super().__init__(mass, OIDC_PROVIDER_ID, config)
        self._oidc_config = config
        self.client = OIDCClient(
            mass.http_session,
            config["issuer"],
            config["client_id"],
            config["client_secret"],
            config["token_auth_method"],
            config.get("scopes", DEFAULT_OIDC_SCOPES),
        )
        self._role_mapping = config.get("role_mapping", {})
        self._groups_claim = config.get("groups_claim", "groups")
        if not self._groups_claim or len(self._groups_claim) > 255:
            raise ValueError("Invalid OpenID Connect groups claim name")

    @property
    def name(self) -> str:
        """Return the display name configured by the administrator."""
        return self._oidc_config["name"]

    @property
    def allow_self_registration(self) -> bool:
        """Return whether new identities may create accounts."""
        return self._oidc_config["allow_self_registration"]

    @property
    def provider_type(self) -> AuthProviderType:
        """Return the OIDC authentication provider type."""
        return AuthProviderType.OIDC

    @property
    def requires_redirect(self) -> bool:
        """Return True because OIDC signs in at the identity provider."""
        return True

    @property
    def backchannel_logout_enabled(self) -> bool:
        """Return whether back-channel logout is enabled."""
        return self._oidc_config.get("backchannel_logout", False)

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """Reject credentials; OIDC requires the authorization code flow."""
        return AuthResult(success=False, error="Use the OpenID Connect sign-in flow")

    async def build_authorization_url(self, pending: PendingLogin) -> str | None:
        """Build the identity provider URL for this pending sign-in."""
        try:
            return await self.client.authorization_url(pending)
        except (ClientError, TimeoutError, PyJWTError, OIDCProtocolError) as err:
            self.logger.warning("OIDC authorization could not start (%s)", type(err).__name__)
            raise AuthenticationFailed("OpenID Connect provider is unavailable") from err

    async def complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> User:
        """Exchange an authorization code and resolve the linked user."""
        try:
            async with asyncio.timeout(max(0, pending.expires_at - monotonic())):
                return await self._complete_authorization(pending, params)
        except TimeoutError as err:
            raise AuthenticationFailed(
                "This sign-in is invalid or has expired", translation_key="sign_in_expired"
            ) from err

    async def handle_backchannel_logout(self, token: str) -> None:
        """Validate a logout notification and revoke matching sessions."""
        if not self.backchannel_logout_enabled:
            raise AuthenticationFailed("OpenID Connect back-channel logout is disabled")
        try:
            claims = await self.client.validate_logout_token(token)
        except (ClientError, TimeoutError, PyJWTError, OIDCProtocolError) as err:
            self.logger.warning("OIDC logout validation failed (%s)", type(err).__name__)
            raise AuthenticationFailed("Invalid OpenID Connect logout token") from err
        await run_to_completion(self._apply_backchannel_logout(claims))

    async def _apply_backchannel_logout(self, claims: dict[str, Any]) -> None:
        async with self.auth_manager.user_link_lock:
            await self.auth_manager.oidc_sessions.logout(
                self.client.issuer, self.client.client_id, claims
            )

    async def _complete_authorization(
        self, pending: PendingLogin, params: Mapping[str, str]
    ) -> User:
        if not (code := params.get("code")):
            raise AuthenticationFailed("No authorization code received")
        try:
            claims = await self.client.authenticate(
                pending, code, groups_claim=self._groups_claim if self._role_mapping else None
            )
        except (ClientError, TimeoutError, PyJWTError, OIDCProtocolError) as err:
            # Log only the exception type to avoid exposing tokens or provider responses.
            self.logger.warning("OIDC sign-in validation failed (%s)", type(err).__name__)
            raise AuthenticationFailed("OpenID Connect sign-in failed") from err
        async with self.auth_manager.user_link_lock:
            session = OIDCSession(
                issuer=self.client.issuer,
                client_id=self.client.client_id,
                subject=claims["sub"],
                session_id=claims.get("sid"),
                issued_at=claims["iat"],
                expires_at=claims["exp"],
                login_expires_at=pending.expires_at,
            )
            await self.auth_manager.oidc_sessions.validate_session(session)
            user = await self._get_or_create_user(claims)
            session.ensure_login_active()
            pending.oidc_session = session
            return user

    async def _get_or_create_user(self, claims: dict[str, Any]) -> User:
        identity_id = oidc_identity_id(self.client.issuer, claims["sub"])
        user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.OIDC, identity_id, include_disabled=True
        )
        mapped_role: str | None = None
        if self._role_mapping:
            try:
                mapped_role = self._mapped_role(claims)
                if user is not None:
                    user = await self.auth_manager.sync_oidc_user_role(user, mapped_role)
                else:
                    await self._ensure_mapped_role_exists(mapped_role)
            except (AuthenticationFailed, InvalidDataError) as err:
                if user is not None:
                    await self.auth_manager.oidc_sessions.revoke_identity(
                        self.client.issuer, self.client.client_id, claims["sub"]
                    )
                raise AuthenticationFailed("OpenID Connect role mapping denied access") from err
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
        deployment = self.auth_manager.oidc_deployment
        bootstrap = bool(
            deployment and deployment.bootstrap_admin and mapped_role == UserRole.ADMIN
        )
        if not self.auth_manager.has_users and not bootstrap:
            if deployment and deployment.bootstrap_admin:
                raise AuthenticationFailed(
                    "Initial setup requires a mapped OpenID Connect administrator"
                )
            raise AuthenticationFailed("Create a local administrator before enabling OIDC")
        preferred_username = claims.get("preferred_username")
        username = slugify(preferred_username) if isinstance(preferred_username, str) else ""
        username = username[:50] if len(username) >= 2 else "oidc-user"
        # A matching username does not prove ownership of a local or HA account.
        if await self.auth_manager.get_user_by_username(username, include_disabled=True):
            username = f"{username[:37]}-{identity_id[:12]}"
        display_name = claims.get("name")
        return await self.auth_manager.create_user(
            username=username,
            role=mapped_role or UserRole.USER,
            display_name=display_name
            if isinstance(display_name, str) and display_name
            else username,
            oidc_bootstrap=bootstrap,
            provider_link=(AuthProviderType.OIDC, identity_id),
        )

    async def _ensure_mapped_role_exists(self, role_id: str) -> None:
        if role_id not in {role.role_id for role in await self.auth_manager.get_roles()}:
            raise InvalidDataError("The mapped Music Assistant role does not exist")

    def _mapped_role(self, claims: dict[str, Any]) -> str:
        groups = claims.get(self._groups_claim)
        if (
            not isinstance(groups, list)
            or len(groups) > 1000
            or any(not isinstance(group, str) or len(group) > 255 for group in groups)
        ):
            raise AuthenticationFailed("Missing or invalid OpenID Connect groups claim")
        for group, role in self._role_mapping.items():
            if group in groups:
                return role
        raise AuthenticationFailed("No OpenID Connect group grants a Music Assistant role")
