"""Authentication of the requests to the builtin MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.auth import AuthProviderType

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_authenticated_user,
    resolve_impersonated_user,
    set_current_token,
    set_current_user,
    set_impersonated_user,
)

from .registry import ToolError

if TYPE_CHECKING:
    from aiohttp import web
    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant

# The key in the _meta of a tools/call request that holds the id of the Home Assistant user
# the call is made for, as Home Assistant sends it (reverse-DNS prefix, as the MCP spec asks)
META_HA_USER_ID: Final = "io.home-assistant/user_id"


async def authenticate_request(request: web.Request) -> User | None:
    """
    Authenticate the request and set the user and token of the request context.

    :param request: The aiohttp request.
    :return: The authenticated user, None when the request is not authenticated.
    """
    token: str | None = None
    user = await get_authenticated_user(request)
    auth_header = request.headers.get("Authorization", "")
    if user and auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
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
