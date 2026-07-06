"""Authentication middleware and helpers for HTTP requests and WebSocket connections."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextvars import ContextVar
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self, cast

from aiohttp import web
from music_assistant_models.auth import AuthProviderType, Scope, User, UserRole
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER, MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL

from .auth_providers import get_ha_user_details, get_ha_user_role

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

# Context key for storing authenticated user in request
USER_CONTEXT_KEY = "authenticated_user"

_GUEST_SCOPES: Final[frozenset[Scope]] = frozenset(
    {
        Scope.LIBRARY_READ,
        Scope.PLAYERS_READ,
        Scope.PLAYERS_CONTROL,
        Scope.QUEUES_READ,
        Scope.QUEUES_CONTROL,
        Scope.PROVIDERS_READ,
        Scope.CONFIG_PLAYERS_READ,
    }
)
_USER_SCOPES: Final[frozenset[Scope]] = _GUEST_SCOPES | {
    Scope.LIBRARY_WRITE,
    Scope.CONFIG_PROVIDERS_READ,
    Scope.CONFIG_CORE_READ,
    Scope.USERS_INVITE,
    Scope.SYSTEM_READ,
}

# Scopes granted to each of the builtin user roles.
# Roles are identified by their (string) role id to allow for custom roles in the future:
# a role id not present in this mapping simply grants no scopes at all.
ROLE_SCOPES: Final[Mapping[str, frozenset[Scope]]] = {
    UserRole.ADMIN: frozenset({Scope.ALL}),
    UserRole.USER: _USER_SCOPES,
    UserRole.GUEST: _GUEST_SCOPES,
    # service accounts (such as the Home Assistant integration) get
    # slightly elevated rights over a regular user
    UserRole.SERVICE: _USER_SCOPES | {Scope.CONFIG_PLAYERS_WRITE, Scope.USERS_IMPERSONATE},
}

# ContextVar for tracking current user and token across async calls
current_user: ContextVar[User | None] = ContextVar("current_user", default=None)
current_token: ContextVar[str | None] = ContextVar("current_token", default=None)
# ContextVar to impersonate another user. Admin permissions required. Used in HA context.
impersonated_user: ContextVar[User | None] = ContextVar("impersonated_user", default=None)
# ContextVar for tracking the sendspin player associated with the current connection
sendspin_player_id: ContextVar[str | None] = ContextVar("sendspin_player_id", default=None)


async def get_authenticated_user(request: web.Request) -> User | None:
    """
    Get authenticated user from request.

    :param request: The aiohttp request.
    """
    # Check if user is already in context (from middleware)
    if USER_CONTEXT_KEY in request:
        return cast("User | None", request[USER_CONTEXT_KEY])

    mass: MusicAssistant = request.app["mass"]

    # Check for Home Assistant Ingress connections
    if is_request_from_ingress(request):
        ingress_user_id = request.headers.get("X-Remote-User-ID")
        ingress_username = request.headers.get("X-Remote-User-Name")
        ingress_display_name = request.headers.get("X-Remote-User-Display-Name")

        # Require all Ingress headers to be present for security
        if not (ingress_user_id and ingress_username):
            return None

        # Try to find existing user linked to this HA user ID
        user = await mass.webserver.auth.get_user_by_provider_link(
            AuthProviderType.HOME_ASSISTANT, ingress_user_id
        )
        if not user:
            user = await mass.webserver.auth.get_user_by_username(ingress_username)
            if not user:
                # New user - fetch details from HA
                ha_username, ha_display_name, avatar_url = await get_ha_user_details(
                    mass, ingress_user_id
                )
                role = await get_ha_user_role(mass, ingress_user_id)
                user = await mass.webserver.auth.create_user(
                    username=ha_username or ingress_username,
                    role=role,
                    display_name=ha_display_name or ingress_display_name,
                    avatar_url=avatar_url,
                )

            # Link to Home Assistant provider (or create the link if user already existed)
            await mass.webserver.auth.link_user_to_provider(
                user, AuthProviderType.HOME_ASSISTANT, ingress_user_id
            )

        # Update user with HA details if available (HA is source of truth)
        # Fall back to ingress headers if API lookup doesn't return values
        _, ha_display_name, avatar_url = await get_ha_user_details(mass, ingress_user_id)
        final_display_name = ha_display_name or ingress_display_name
        LOGGER.log(
            VERBOSE_LOG_LEVEL,
            "Ingress auth for user %s: ha_display_name=%s, ingress_display_name=%s, "
            "final_display_name=%s, avatar_url=%s",
            user.username,
            ha_display_name,
            ingress_display_name,
            final_display_name,
            avatar_url,
        )
        if final_display_name or avatar_url:
            user = await mass.webserver.auth.update_user(
                user,
                display_name=final_display_name,
                avatar_url=avatar_url,
            )
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "Updated user %s: display_name=%s, avatar_url=%s",
                user.username,
                user.display_name,
                user.avatar_url,
            )

        # Store in request context
        request[USER_CONTEXT_KEY] = user
        return user

    # Try to authenticate from Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None

    # Expected format: "Bearer <token>"
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    token = parts[1]

    # Authenticate with token (works for both user tokens and API keys)
    user = await mass.webserver.auth.authenticate_with_token(token)
    if user:
        # Security: Deny homeassistant system user on regular (non-Ingress) webserver
        if not is_request_from_ingress(request) and user.username == HOMEASSISTANT_SYSTEM_USER:
            # Reject system user on regular webserver (should only use Ingress server)
            return None

        # Store in request context
        request[USER_CONTEXT_KEY] = user

    return user


async def require_authentication(request: web.Request) -> User:
    """
    Require authentication for a request, raise 401 if not authenticated.

    :param request: The aiohttp request.
    """
    user = await get_authenticated_user(request)
    if not user:
        raise web.HTTPUnauthorized(
            text="Authentication required",
            headers={"WWW-Authenticate": 'Bearer realm="Music Assistant"'},
        )
    return user


def has_scope(user: User, scope: Scope) -> bool:
    """
    Check if the given user is granted the given scope (through its role).

    :param user: The user to check.
    :param scope: The scope required.
    """
    role_scopes = ROLE_SCOPES.get(user.role, frozenset())
    return Scope.ALL in role_scopes or scope in role_scopes


async def resolve_impersonated_user(mass: MusicAssistant, user: str) -> User:
    """
    Resolve and validate the user to impersonate for the current call.

    The authenticated caller may always impersonate itself, impersonating
    another user requires the users.impersonate scope.

    :param mass: The MusicAssistant instance.
    :param user: The user_id or username of the user to impersonate.
    """
    authenticated_user = current_user.get()
    if authenticated_user is None:
        raise InsufficientPermissions("Authentication is necessary to impersonate another user.")
    target_user = await mass.webserver.auth.get_user(user)
    if target_user is None:
        target_user = await mass.webserver.auth.get_user_by_username(user)
    if target_user is None:
        raise InvalidDataError(f"A user with user id or name {user} is not available.")
    if target_user.user_id != authenticated_user.user_id and not has_scope(
        authenticated_user, Scope.USERS_IMPERSONATE
    ):
        raise InsufficientPermissions(
            "The users.impersonate scope is required to impersonate another user."
        )
    return target_user


async def resolve_command_impersonation(mass: MusicAssistant, args: dict[str, Any]) -> User | None:
    """
    Pop and resolve the optional impersonation argument for an API command invocation.

    Returns the user to impersonate for the command, or None if no
    impersonation was requested.

    :param mass: The MusicAssistant instance.
    :param args: The (mutable) arguments dict of the incoming command.
    """
    user_arg = args.pop("user", None)
    # username is accepted as (deprecated) alias for user
    username_arg = args.pop("username", None)
    # deliberately treat None and empty string as "no impersonation requested":
    # optional fields in automations/scripts commonly template to an empty string
    if target := user_arg or username_arg:
        return await resolve_impersonated_user(mass, str(target))
    return None


def get_current_user() -> User | None:
    """
    Get the current authenticated user from context.

    :return: The current user or None if not authenticated.
    """
    if impersonated_user := get_impersonated_user():
        return impersonated_user
    return current_user.get()


def set_current_user(user: User | None) -> None:
    """
    Set the current authenticated user in context.

    :param user: The user to set as current.
    """
    current_user.set(user)


def get_impersonated_user() -> User | None:
    """
    Get the current impersonated user from context.

    :return: The current impersonated user or None if not existing.
    """
    return impersonated_user.get()


def set_impersonated_user(user: User | None) -> None:
    """
    Set the current impersonated user in context.

    :param user: The user to set as impersonated.
    """
    impersonated_user.set(user)


def get_current_token() -> str | None:
    """
    Get the current authentication token from context.

    :return: The current token or None if not authenticated.
    """
    return current_token.get()


def set_current_token(token: str | None) -> None:
    """
    Set the current authentication token in context.

    :param token: The token to set as current.
    """
    current_token.set(token)


def get_sendspin_player_id() -> str | None:
    """
    Get the sendspin player ID associated with the current connection.

    :return: The sendspin player ID or None if not a sendspin connection.
    """
    return sendspin_player_id.get()


def set_sendspin_player_id(player_id: str | None) -> None:
    """
    Set the sendspin player ID for the current connection.

    :param player_id: The sendspin player ID to set.
    """
    sendspin_player_id.set(player_id)


def is_request_from_ingress(request: web.Request) -> bool:
    """
    Check if request is coming from Home Assistant Ingress (internal network).

    Security is enforced by socket-level verification (IP/port binding), not headers.
    Only requests on the internal ingress TCP site (172.30.32.x:8094) are accepted.

    :param request: The aiohttp request.
    """
    # Check if ingress site is configured in the app
    ingress_site_params = request.app.get("ingress_site")
    if not ingress_site_params:
        # No ingress site configured, can't be an ingress request
        return False

    try:
        # Security: Verify the request came through the ingress site by checking socket
        # to prevent bypassing authentication on the regular webserver
        transport = request.transport
        if transport:
            sockname = transport.get_extra_info("sockname")
            if sockname and len(sockname) >= 2:
                server_ip, server_port = sockname[0], sockname[1]
                expected_ip, expected_port = ingress_site_params
                # Request must match the ingress site's bind address and port
                return bool(server_ip == expected_ip and server_port == expected_port)
    except Exception:  # noqa: S110
        pass

    return False


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """
    Authenticate requests and store user in context.

    :param request: The aiohttp request.
    :param handler: The request handler.
    """
    # Skip authentication for ingress requests (HA handles auth)
    if is_request_from_ingress(request):
        return cast("web.StreamResponse", await handler(request))

    # Unauthenticated routes (static files, info, login, setup, etc.)
    unauthenticated_paths = [
        "/info",
        "/login",
        "/setup",
        "/auth/",
        "/api-docs/",
        "/assets/",
        "/favicon.ico",
        "/manifest.json",
        "/index.html",
        "/",
    ]

    # Check if path should bypass auth
    for path_prefix in unauthenticated_paths:
        if request.path.startswith(path_prefix):
            return cast("web.StreamResponse", await handler(request))

    # Try to authenticate
    user = await get_authenticated_user(request)

    # Store user in context (might be None for unauthenticated requests)
    request[USER_CONTEXT_KEY] = user

    # Let the handler decide if authentication is required
    # The handler will call require_authentication() if needed
    return cast("web.StreamResponse", await handler(request))


class ImpersonatedUser:
    """
    Optional impersonated user context manager, for use by internal (server) code.

    API commands should instead be registered with the allow_impersonation flag,
    which handles impersonation centrally in the command dispatch.

    Nested use possible: passing None for the user is a no-op which preserves
    any impersonation already active in the current context.
    """

    def __init__(self, mass: MusicAssistant, user: str | None) -> None:
        """
        Initialize ImpersonatedUser.

        :param mass: The MusicAssistant instance.
        :param user: The user_id or username of the user to impersonate, or None for a no-op.
        """
        self.mass = mass
        self.user = user
        self.previous_impersonated_user = impersonated_user.get()

    async def __aenter__(self) -> Self:
        """Set the impersonated user if applicable."""
        if self.user is None:
            # no-op: nothing to impersonate (e.g. playback from a hardware button
            # or an external protocol without a user context)
            return self
        set_impersonated_user(await resolve_impersonated_user(self.mass, self.user))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Unset the impersonated user."""
        set_impersonated_user(self.previous_impersonated_user)
        return None
