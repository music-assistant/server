"""Authentication of the requests to the builtin MCP server."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.auth import AuthProviderType

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER, MASS_LOGGER_NAME
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_authenticated_user,
    is_request_from_ingress,
    resolve_impersonated_user,
    set_current_token,
    set_current_user,
    set_impersonated_user,
)
from music_assistant.helpers.util import get_ip_addresses, normalize_ip_address

from .registry import ToolError

if TYPE_CHECKING:
    from aiohttp import web
    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant

# The key in the _meta of a tools/call request that holds the id of the Home Assistant user
# the call is made for, as Home Assistant sends it (reverse-DNS prefix, as the MCP spec asks)
META_HA_USER_ID: Final = "io.home-assistant/user_id"

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.mcp_server")


async def is_request_from_home_assistant(request: web.Request) -> bool:
    """
    Return whether the request comes from Home Assistant on the internal (ingress) site.

    Home Assistant runs on the host network, as this server does, so it connects from one of
    this server's own addresses and such a request needs no token.

    :param request: The aiohttp request.
    """
    # stands in for the bearer token Home Assistant older than 2026.12 does not send,
    # see handle_async_init
    if not is_request_from_ingress(request) or request.transport is None:
        return False
    peername = request.transport.get_extra_info("peername")
    sockname = request.transport.get_extra_info("sockname")
    if not peername or not sockname:
        return False
    # the host may connect from its LAN address rather than the bridge address the
    # internal site is bound to, so any address of this host counts
    local_addresses = {sockname[0], "127.0.0.1", "::1", *await get_ip_addresses(include_ipv6=True)}
    return normalize_ip_address(peername[0]) in local_addresses


async def authenticate_request(
    mass: MusicAssistant, request: web.Request, from_home_assistant: bool
) -> User | None:
    """
    Authenticate the request and set the user and token of the request context.

    :param mass: The Music Assistant instance.
    :param request: The aiohttp request.
    :param from_home_assistant: Whether the request comes from Home Assistant on the
        internal site, which runs as the Home Assistant system user without a token.
    :return: The authenticated user, None when the request is not authenticated.
    """
    token: str | None = None
    if from_home_assistant:
        user: User | None = await mass.webserver.auth.get_homeassistant_system_user()
        if request.transport:
            LOGGER.debug(
                "Accepted Home Assistant request without a token from %s (local address %s)",
                request.transport.get_extra_info("peername"),
                request.transport.get_extra_info("sockname"),
            )
    else:
        user = await get_authenticated_user(request)
        auth_header = request.headers.get("Authorization", "")
        if user and auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
        if user is None and is_request_from_ingress(request) and request.transport:
            LOGGER.debug(
                "Refused unauthenticated request on the internal site from %s (local address %s)",
                request.transport.get_extra_info("peername"),
                request.transport.get_extra_info("sockname"),
            )
    set_current_user(user)
    set_current_token(token)
    set_impersonated_user(None)
    return user


async def impersonate_home_assistant_user(mass: MusicAssistant, params: dict[str, Any]) -> None:
    """
    Act as the Home Assistant user named in the _meta of a tools/call request, if linked.

    Only applies to a request authenticated as the Home Assistant system user. A Home
    Assistant user that is not linked to a Music Assistant user leaves the request running
    as the Home Assistant system user.

    :param mass: The Music Assistant instance.
    :param params: The params of the tools/call request.
    :raises ToolError: When the linked Music Assistant account is disabled.
    :raises InsufficientPermissions: When the current user may not impersonate another user.
    """
    authenticated_user = current_user.get()
    if authenticated_user is None or authenticated_user.username != HOMEASSISTANT_SYSTEM_USER:
        return
    meta = params.get("_meta")
    ha_user_id = meta.get(META_HA_USER_ID) if isinstance(meta, dict) else None
    if not isinstance(ha_user_id, str) or not ha_user_id:
        return
    linked_user = await mass.webserver.auth.get_user_by_provider_link(
        AuthProviderType.HOME_ASSISTANT, ha_user_id, include_disabled=True
    )
    if linked_user is None:
        return
    if not linked_user.enabled:
        raise ToolError(f"The Music Assistant account of {linked_user.username} is disabled")
    set_impersonated_user(
        await resolve_impersonated_user(
            mass, AuthProviderType.HOME_ASSISTANT, ha_user_id, required=False
        )
    )
