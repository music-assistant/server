"""Authentication middleware and helpers for HTTP requests and WebSocket connections."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from music_assistant_models.auth import AuthProviderType, User, UserRole

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER, MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL

from .auth_providers import get_ha_user_details, get_ha_user_role

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

# Context key for storing authenticated user in request
USER_CONTEXT_KEY = "authenticated_user"

# ContextVar for tracking current user and token across async calls
current_user: ContextVar[User | None] = ContextVar("current_user", default=None)
current_token: ContextVar[str | None] = ContextVar("current_token", default=None)


async def get_authenticated_user(request: web.Request) -> User | None:
    """Get authenticated user from request.

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
    """Require authentication for a request, raise 401 if not authenticated.

    :param request: The aiohttp request.
    """
    user = await get_authenticated_user(request)
    if not user:
        raise web.HTTPUnauthorized(
            text="Authentication required",
            headers={"WWW-Authenticate": 'Bearer realm="Music Assistant"'},
        )
    return user


async def require_admin(request: web.Request) -> User:
    """Require admin role for a request, raise 403 if not admin.

    :param request: The aiohttp request.
    """
    user = await require_authentication(request)
    if user.role != UserRole.ADMIN:
        raise web.HTTPForbidden(text="Admin access required")
    return user


def get_current_user() -> User | None:
    """
    Get the current authenticated user from context.

    :return: The current user or None if not authenticated.
    """
    return current_user.get()


def set_current_user(user: User | None) -> None:
    """
    Set the current authenticated user in context.

    :param user: The user to set as current.
    """
    current_user.set(user)


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


def is_request_from_ingress(request: web.Request) -> bool:
    """Check if request is coming from Home Assistant Ingress (internal network).

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
    """Authenticate requests and store user in context.

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
