"""Request handling for the Subsonic API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web
from libopensonic.media import OpenSubsonicExtension
from libopensonic.media import User as SubsonicUser
from multidict import MultiDict, MultiDictProxy
from music_assistant_models.auth import Scope

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    has_scope,
    set_current_user,
)

from .actions import ActionEndpoints
from .auth import verify_credentials
from .browse import BrowseEndpoints
from .constants import REST_PREFIX, ErrorCode
from .jukebox import JukeboxEndpoints
from .playlists import PlaylistEndpoints
from .request import SubsonicRequest
from .response import (
    CORS_HEADERS,
    SubsonicError,
    build_error_response,
    build_preflight_response,
    build_response,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from . import SubsonicServerProvider
    from .request import EndpointHandler, EndpointResult

CREDENTIAL_PARAMS = frozenset({"u", "p", "t", "s", "apiKey"})
SUPPORTED_EXTENSIONS: tuple[OpenSubsonicExtension, ...] = (
    OpenSubsonicExtension(name="formPost", versions=[1]),
    OpenSubsonicExtension(name="songLyrics", versions=[1]),
)


class SubsonicApi:
    """Dispatch ``/rest/*`` requests to the Subsonic endpoint handlers."""

    def __init__(self, provider: SubsonicServerProvider) -> None:
        """
        Initialize the API.

        :param provider: The plugin instance that owns this API.
        """
        self.provider = provider
        self.mass = provider.mass
        browse = BrowseEndpoints(self.mass)
        self._endpoints: dict[str, EndpointHandler] = {
            "ping": self._ping,
            "getLicense": self._get_license,
            "getOpenSubsonicExtensions": self._get_open_subsonic_extensions,
            "getUser": self._get_user,
            **browse.endpoints(),
            **ActionEndpoints(browse).endpoints(),
            **PlaylistEndpoints(browse).endpoints(),
            **JukeboxEndpoints(provider).endpoints(),
        }

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """
        Handle a single Subsonic API request.

        :param request: The incoming HTTP request for a path below ``/rest/``.
        """
        if request.method == "OPTIONS":
            return build_preflight_response(request.headers.get("Access-Control-Request-Headers"))
        response = await self._respond(request)
        response.headers.update(CORS_HEADERS)
        return response

    async def _respond(self, request: web.Request) -> web.StreamResponse:
        params = await _collect_params(request)
        response_format = params.get("f")
        callback = params.get("callback")
        endpoint = request.path.removeprefix(REST_PREFIX).removesuffix(".view")
        self.provider.logger.debug(
            "%s %s from %s: %s",
            request.method,
            endpoint,
            params.get("c"),
            {key: value for key, value in params.items() if key not in CREDENTIAL_PARAMS},
        )
        try:
            result = await self._dispatch(endpoint, params)
        except SubsonicError as err:
            self.provider.logger.debug("%s failed with error %s: %s", endpoint, err.code, err)
            return build_error_response(err, self.mass.version, response_format, callback)
        if isinstance(result, web.StreamResponse):
            return result
        return build_response(result, self.mass.version, response_format, callback)

    async def _dispatch(self, endpoint: str, params: MultiDictProxy[str]) -> EndpointResult:
        user = await self._authenticate(params)
        if (handler := self._endpoints.get(endpoint)) is None:
            raise SubsonicError(ErrorCode.GENERIC, f"Unsupported endpoint: {endpoint}")
        set_current_user(user)
        try:
            return await handler(SubsonicRequest(params=params, user=user))
        finally:
            set_current_user(None)

    async def _authenticate(self, params: MultiDictProxy[str]) -> User:
        if params.get("apiKey") is not None:
            raise SubsonicError(
                ErrorCode.AUTH_MECHANISM_NOT_SUPPORTED, "API key authentication is not supported"
            )
        if (username := params.get("u")) is None:
            raise SubsonicError(ErrorCode.MISSING_PARAMETER, "Required parameter is missing: u")
        user = await self.mass.webserver.auth.get_user_by_username(username)
        if user is None or not verify_credentials(
            self.provider.get_app_password(user.user_id),
            password=params.get("p"),
            token=params.get("t"),
            salt=params.get("s"),
        ):
            raise SubsonicError(ErrorCode.WRONG_CREDENTIALS, "Wrong username or password")
        if not has_scope(user, Scope.LIBRARY_READ):
            raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "User may not read the library")
        return user

    async def _ping(self, request: SubsonicRequest) -> dict[str, Any]:
        return {}

    async def _get_license(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"license": {"valid": True}}

    async def _get_open_subsonic_extensions(self, request: SubsonicRequest) -> dict[str, Any]:
        return {"openSubsonicExtensions": [ext.to_dict() for ext in SUPPORTED_EXTENSIONS]}

    async def _get_user(self, request: SubsonicRequest) -> dict[str, Any]:
        username = request.require("username")
        user: User | None = request.user
        if username != request.user.username:
            if not has_scope(request.user, Scope.USERS_READ):
                raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "Only an admin may read other users")
            user = await self.mass.webserver.auth.get_user_by_username(username)
        if user is None:
            raise SubsonicError(ErrorCode.NOT_FOUND, f"User not found: {username}")
        library_write = has_scope(user, Scope.LIBRARY_WRITE)
        jukebox = self.provider.jukebox_player_id is not None and has_scope(
            user, Scope.QUEUES_CONTROL
        )
        subsonic_user = SubsonicUser(
            username=user.username,
            scrobbling_enabled=True,
            admin_role=has_scope(user, Scope.USERS_MANAGE),
            settings_role=False,
            download_role=False,
            upload_role=False,
            playlist_role=library_write,
            cover_art_role=False,
            comment_role=False,
            podcast_role=False,
            stream_role=False,
            jukebox_role=jukebox,
            share_role=False,
            video_conversion_role=False,
        )
        return {"user": subsonic_user.to_dict()}


async def _collect_params(request: web.Request) -> MultiDictProxy[str]:
    params: MultiDict[str] = MultiDict(request.query)
    if request.method == "POST" and request.content_type == "application/x-www-form-urlencoded":
        form = await request.post()
        params.extend((key, value) for key, value in form.items() if isinstance(value, str))
    return MultiDictProxy(params)
