"""
Controller that manages the builtin webserver that hosts the api and frontend.

Unlike the streamserver (which is as simple and unprotected as possible),
this webserver allows for more fine grained configuration to better secure it.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from concurrent import futures
from contextlib import aclosing
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

import aiofiles
from aiohttp import web
from mashumaro.exceptions import MissingField
from music_assistant_frontend import where as locate_frontend
from music_assistant_models.api import CommandMessage
from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError
from music_assistant_models.media_items.metadata import IMAGE_PROXY_ID_RESOLVER
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.constants import (
    CONF_AUTH_ALLOW_SELF_REGISTRATION,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_VALUE_AUTO,
    DEFAULT_HOST,
    INGRESS_SERVER_PORT,
    RESOURCES_DIR,
    SENDSPIN_SERVER_PORT,
    VERBOSE_LOG_LEVEL,
    WILDCARD_BIND_IPS,
)
from music_assistant.controllers.webserver.helpers.ssl import (
    create_server_ssl_context,
    format_certificate_info,
    verify_ssl_certificate,
)
from music_assistant.helpers.api import parse_arguments
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.redirect_validation import (
    build_code_redirect_url,
    is_allowed_redirect_url,
)
from music_assistant.helpers.util import (
    format_ip_for_url,
    get_ip_addresses,
    get_publish_ip_candidates,
)
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.core_controller import CoreController

from .api_docs import generate_commands_json, generate_openapi_spec, generate_schemas_json
from .auth import AuthenticationManager
from .helpers.auth_middleware import (
    get_authenticated_user,
    has_scope,
    is_request_from_ingress,
    resolve_command_impersonation,
    set_current_peer_address,
    set_current_token,
    set_current_user,
    set_impersonated_user,
)
from .helpers.auth_providers import BuiltinLoginProvider, get_ha_user_role
from .remote_access import RemoteAccessManager
from .sendspin_proxy import SendspinProxyHandler
from .websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant
    from music_assistant.helpers.api import APICommandHandler

DEFAULT_SERVER_PORT = 8095
CONF_BASE_URL = "base_url"
CONF_ENABLE_SSL = "enable_ssl"
CONF_SSL_CERTIFICATE = "ssl_certificate"
CONF_SSL_PRIVATE_KEY = "ssl_private_key"
CONF_ACTION_VERIFY_SSL = "verify_ssl"
MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)


def _get_publish_addresses(
    bind_ip: str | None, publish_ip: str, publish_candidates: tuple[str, ...]
) -> list[str]:
    """
    Return the IP addresses the webserver should publish/advertise.

    :param bind_ip: The configured bind IP (None or a wildcard means all interfaces).
    :param publish_ip: The resolved primary publish IP.
    :param publish_candidates: Host addresses reachable from the local network, ranked.
    """
    addresses = [publish_ip]
    if bind_ip and bind_ip not in WILDCARD_BIND_IPS:
        return addresses
    # bound to all interfaces: also publish the primary address of the other
    # IP family (if any) so both IPv4-only and IPv6-only clients can connect
    publish_is_ipv6 = ":" in publish_ip
    for ip in publish_candidates:
        if (":" in ip) != publish_is_ipv6:
            addresses.append(ip)
            break
    return addresses


def _get_internal_connect_ip(bind_ip: str | None, publish_ip: str) -> str:
    """
    Return the IP address to reach a server running on this host.

    :param bind_ip: The server's configured bind IP (None or a wildcard means all interfaces).
    :param publish_ip: The server's resolved publish IP.
    """
    if bind_ip and bind_ip not in WILDCARD_BIND_IPS:
        # bound to one specific interface, so loopback would not reach the server
        return bind_ip
    # Use IPv6 loopback if publish_ip is IPv6 (indicates IPv6-only host)
    return "::1" if ":" in publish_ip else "127.0.0.1"


def _locale_from_request(request: web.Request) -> str | None:
    """
    Determine the UI locale for an HTTP request from the standard ``Accept-Language`` header.

    Returns None when the header is absent, so the server falls back to the English source.

    :param request: The aiohttp request.
    """
    header = request.headers.get("Accept-Language")
    if not header:
        return None
    # take the first/highest-priority tag, dropping any quality factor ("nl-NL,nl;q=0.9" -> "nl-NL")
    locale = header.split(",", 1)[0].split(";", 1)[0].strip()
    return locale or None


class WebserverController(CoreController):
    """Core Controller that manages the builtin webserver that hosts the api and frontend."""

    domain: str = "webserver"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize instance."""
        super().__init__(mass)
        self._server = Webserver(self.logger, enable_dynamic_routes=True)
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.clients: set[WebsocketClientHandler] = set()
        # the URL that the "auto" base_url setting resolves to, detected at setup
        self._auto_base_url: str = ""
        # whether SSL is switched on in the config, resolved at setup
        self._ssl_configured: bool = False
        # whether the webserver actually serves TLS, resolved at setup
        self._ssl_active: bool = False
        self.bind_ip: str | None = None
        self.publish_addresses: list[str] = []
        self.manifest.name = "Web Server (frontend and api)"
        self.manifest.description = (
            "The built-in webserver that hosts the Music Assistant Websockets API and frontend"
        )
        self.manifest.icon = "web-box"
        self.auth = AuthenticationManager(self)
        self.remote_access = RemoteAccessManager(self)
        self._sendspin_proxy = SendspinProxyHandler(self)

    @property
    def base_url(self) -> str:
        """Return the base_url for the webserver."""
        config = getattr(self, "config", None)
        if config is None:
            return ""
        base_url = str(config.get_value(CONF_BASE_URL) or CONF_VALUE_AUTO)
        if base_url == CONF_VALUE_AUTO:
            return self._auto_base_url
        return base_url.removesuffix("/")

    @property
    def internal_base_url(self) -> str:
        """Return the URL to reach this webserver's own API from this host."""
        # the advertised address is not necessarily dialable here: a configured base URL
        # routes out through DNS and a reverse proxy just to come back in, and a published
        # IP need not exist on this host at all (e.g. a container or NAT setup), so derive
        # the address from what the webserver actually binds to
        connect_ip = _get_internal_connect_ip(self.bind_ip, self.publish_ip)
        protocol = "https" if self._ssl_active else "http"
        return f"{protocol}://{format_ip_for_url(connect_ip)}:{self.publish_port}"

    @property
    def internal_sendspin_url(self) -> str:
        """Return the URL to reach the in-process Sendspin server from this host."""
        # the advertised address is not necessarily dialable here (e.g. a container or
        # NAT setup), so derive the address from what the Sendspin server actually binds to
        connect_ip = _get_internal_connect_ip(
            self.mass.streams.bind_ip, str(self.mass.streams.publish_ip)
        )
        return f"ws://{format_ip_for_url(connect_ip)}:{SENDSPIN_SERVER_PORT}/sendspin"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return await self._build_config_entries()

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...] | None:
        """Handle a one-shot action button press and re-render the config entries."""
        if action == CONF_ACTION_VERIFY_SSL:
            # the certificate/key are read from the stored config, so they must be saved
            # before verifying - the action no longer receives the (unsaved) form values
            cert_info = await verify_ssl_certificate(
                str(self.get_config_value(CONF_SSL_CERTIFICATE, "")),
                str(self.get_config_value(CONF_SSL_PRIVATE_KEY, "")),
            )
            return await self._build_config_entries(format_certificate_info(cert_info))
        return await super().handle_config_action(action)

    async def setup(self, config: CoreConfig) -> None:  # noqa: PLR0915
        """Async initialize of module."""
        self.config = config
        # work out all routes
        routes: list[tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]]] = []
        # frontend routes
        frontend_dir = locate_frontend()
        for filename in next(os.walk(frontend_dir))[2]:
            if filename.endswith(".py"):
                continue
            filepath = os.path.join(frontend_dir, filename)
            handler = partial(self._server.serve_static, filepath)
            routes.append(("GET", f"/{filename}", handler))
        # add index (with onboarding check)
        self._index_path = os.path.join(frontend_dir, "index.html")
        routes.append(("GET", "/", self._handle_index))
        routes.append(("HEAD", "/", self._handle_index))
        # add logo
        logo_path = str(RESOURCES_DIR.joinpath("logo.png"))
        handler = partial(self._server.serve_static, logo_path)
        routes.append(("GET", "/logo.png", handler))
        # add common CSS for HTML resources
        common_css_path = str(RESOURCES_DIR.joinpath("common.css"))
        handler = partial(self._server.serve_static, common_css_path)
        routes.append(("GET", "/resources/common.css", handler))
        # add info
        routes.append(("GET", "/info", self._handle_server_info))
        routes.append(("OPTIONS", "/info", self._handle_cors_preflight))
        # add websocket api
        routes.append(("GET", "/ws", self._handle_ws_client))
        # the canonical /imageproxy/<image_id> form is registered as a dynamic
        # route on the webserver by MetaDataController.post_setup()
        # also host the audio preview service
        routes.append(("GET", "/preview", self.serve_preview_stream))
        # add jsonrpc api
        routes.append(("POST", "/api", self._handle_jsonrpc_api_command))
        # add api documentation
        routes.append(("GET", "/api-docs", self._handle_api_intro))
        routes.append(("GET", "/api-docs/", self._handle_api_intro))
        routes.append(("GET", "/api-docs/commands", self._handle_commands_reference))
        routes.append(("GET", "/api-docs/commands/", self._handle_commands_reference))
        routes.append(("GET", "/api-docs/commands.json", self._handle_commands_json))
        routes.append(("GET", "/api-docs/schemas", self._handle_schemas_reference))
        routes.append(("GET", "/api-docs/schemas/", self._handle_schemas_reference))
        routes.append(("GET", "/api-docs/schemas.json", self._handle_schemas_json))
        routes.append(("GET", "/api-docs/openapi.json", self._handle_openapi_spec))
        routes.append(("GET", "/api-docs/swagger", self._handle_swagger_ui))
        routes.append(("GET", "/api-docs/swagger/", self._handle_swagger_ui))
        # add authentication routes
        routes.append(("GET", "/login", self._handle_login_page))
        routes.append(("POST", "/auth/login", self._handle_auth_login))
        routes.append(("OPTIONS", "/auth/login", self._handle_cors_preflight))
        routes.append(("POST", "/auth/logout", self._handle_auth_logout))
        routes.append(("GET", "/auth/me", self._handle_auth_me))
        routes.append(("PATCH", "/auth/me", self._handle_auth_me_update))
        routes.append(("GET", "/auth/providers", self._handle_auth_providers))
        routes.append(("GET", "/auth/authorize", self._handle_auth_authorize))
        routes.append(("GET", "/auth/callback", self._handle_auth_callback))
        # add first-time setup routes
        routes.append(("GET", "/setup", self._handle_setup_page))
        routes.append(("POST", "/setup", self._handle_setup))
        # add sendspin proxy route (authenticated WebSocket proxy to internal sendspin server)
        routes.append(("GET", "/sendspin", self._sendspin_proxy.handle_sendspin_proxy))
        await self.auth.setup()
        # start the webserver
        if self.mass.running_as_hass_addon:
            # if we're running on the HA supervisor we start an additional TCP site
            # on the internal ("172.30.32.) IP for the HA ingress proxy - that address
            # lives on a docker bridge, so it needs the unfiltered adapter list
            all_ip_addresses = await get_ip_addresses(include_ipv6=True)
            ingress_host = next(
                (x for x in all_ip_addresses if x.startswith("172.30.32.")), all_ip_addresses[0]
            )
            ingress_tcp_site_params = (ingress_host, INGRESS_SERVER_PORT)
        else:
            ingress_tcp_site_params = None
        port_value = config.get_value(CONF_BIND_PORT)
        assert isinstance(port_value, int)
        self.publish_port = port_value
        bind_ip = cast("str | None", config.get_value(CONF_BIND_IP))
        # Create SSL context if SSL is enabled
        ssl_context = None
        self._ssl_configured = bool(config.get_value(CONF_ENABLE_SSL, False))
        if self._ssl_configured:
            ssl_context = await create_server_ssl_context(
                str(config.get_value(CONF_SSL_CERTIFICATE) or ""),
                str(config.get_value(CONF_SSL_PRIVATE_KEY) or ""),
                logger=self.logger,
            )
        # a missing or invalid certificate falls back to plain HTTP, so every URL we hand
        # out must follow the context that was actually created, not the configured value
        self._ssl_active = ssl_context is not None
        protocol = "https" if self._ssl_active else "http"
        publish_candidates = await get_publish_ip_candidates(include_ipv6=True)
        self._resolve_publish_state(bind_ip, publish_candidates, protocol)

        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=self.publish_port,
            static_routes=routes,
            # add assets subdir as static_content
            static_content=("/assets", os.path.join(frontend_dir, "assets"), "assets"),
            ingress_tcp_site_params=ingress_tcp_site_params,
            # Add mass object to app for use by the auth helpers
            app_state={"mass": self.mass},
            ssl_context=ssl_context,
        )
        # adopt what the server actually bound to: a configured port of 0 is only resolved
        # by the OS at bind time and an unavailable bind IP falls back to all interfaces
        self.publish_port = cast("int", self._server.port)
        self._resolve_publish_state(self._server.bind_ip, publish_candidates, protocol)
        base_url = self.base_url
        # print a big fat message in the log where the webserver is running
        # because this is a common source of issues for people with more complex setups
        if not self.auth.has_users:
            self.logger.warning(
                "\n\n################################################################################\n"
                "###                           SETUP REQUIRED                                 ###\n"
                "################################################################################\n"
                "\n"
                "Music Assistant is running in setup mode.\n"
                "Please complete the setup by visiting:\n"
                "\n"
                "    %s/setup\n"
                "\n"
                "################################################################################\n",
                base_url,
            )
        else:
            self.logger.info(
                "\n"
                "################################################################################\n"
                "\n"
                "Webserver available on: %s\n"
                "\n"
                "If this address is incorrect, see the documentation on how to configure\n"
                "the Webserver in Settings --> System --> Webserver\n"
                "\n"
                "################################################################################\n",
                base_url,
            )

        # Setup remote access after webserver is running
        await self.remote_access.setup()

    async def close(self) -> None:
        """Cleanup on exit."""
        await self.remote_access.close()
        for client in set(self.clients):
            await client.disconnect()
        await self._server.close()
        await self.auth.close()

    def register_websocket_client(self, client: WebsocketClientHandler) -> None:
        """Register a WebSocket client for tracking."""
        self.clients.add(client)

    def unregister_websocket_client(self, client: WebsocketClientHandler) -> None:
        """Unregister a WebSocket client."""
        self.clients.discard(client)

    def disconnect_websockets_for_token(self, token_id: str) -> None:
        """Disconnect all WebSocket clients using a specific token."""
        for client in list(self.clients):
            if hasattr(client, "_token_id") and client._token_id == token_id:
                username = (
                    client._authenticated_user.username if client._authenticated_user else "unknown"
                )
                self.logger.warning(
                    "Disconnecting WebSocket client due to token revocation: %s",
                    username,
                )
                client._cancel()

    def disconnect_websockets_for_user(self, user_id: str) -> None:
        """Disconnect all WebSocket clients for a specific user."""
        for client in list(self.clients):
            if (
                hasattr(client, "_authenticated_user")
                and client._authenticated_user
                and client._authenticated_user.user_id == user_id
            ):
                self.logger.warning(
                    "Disconnecting WebSocket client due to user action: %s",
                    client._authenticated_user.username,
                )
                client._cancel()

    def set_sendspin_player_for_user(self, user_id: str, player_id: str) -> None:
        """
        Set the sendspin player_id on websocket clients for a specific user.

        This is called by the sendspin proxy when a client connects, allowing
        the player controller to auto-whitelist the player for that user's session.

        :param user_id: The user ID to set the sendspin player for.
        :param player_id: The sendspin player ID to set.
        """
        for client in list(self.clients):
            if client._authenticated_user and client._authenticated_user.user_id == user_id:
                client._sendspin_player_id = player_id
                self.logger.debug(
                    "Set sendspin player %s for websocket client of user %s",
                    player_id,
                    client._authenticated_user.username,
                )

    def set_sendspin_player_for_webrtc_session(self, session_id: str, player_id: str) -> None:
        """
        Set the sendspin player_id on a websocket client for a WebRTC session.

        This is called by the WebRTC gateway when it extracts the client_id from
        the sendspin auth message, allowing auto-whitelisting of the player.

        :param session_id: The WebRTC session ID.
        :param player_id: The sendspin player ID to set.
        """
        for client in list(self.clients):
            if client._webrtc_session_id == session_id:
                client._sendspin_player_id = player_id
                username = (
                    client._authenticated_user.username
                    if client._authenticated_user
                    else "unauthenticated"
                )
                self.logger.debug(
                    "Set sendspin player %s for WebRTC session %s (user: %s)",
                    player_id,
                    session_id,
                    username,
                )
                return

    async def serve_preview_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve short preview sample."""
        provider_instance_id_or_domain = request.query["provider"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": "audio/aac"})
        await resp.prepare(request)
        preview_stream = self.mass.streams.get_preview_stream(
            provider_instance_id_or_domain, item_id
        )
        # aclosing guarantees the preview stream (and the ffmpeg process behind it)
        # is torn down immediately when the client disconnects, instead of lingering
        # until garbage collection finalizes the abandoned generator.
        async with aclosing(preview_stream):
            async for chunk in preview_stream:
                await resp.write(chunk)
        return resp

    def _resolve_publish_state(
        self, bind_ip: str | None, publish_candidates: tuple[str, ...], protocol: str
    ) -> None:
        """
        Resolve the addresses and base URL to advertise for the given bind address.

        Reads ``self.publish_port``, so set that first.

        :param bind_ip: Address the webserver binds to (None or a wildcard means all interfaces).
        :param publish_candidates: Host addresses reachable from the local network, ranked.
        :param protocol: URL scheme the webserver serves.
        """
        self.bind_ip = bind_ip
        if bind_ip and bind_ip not in WILDCARD_BIND_IPS:
            self.publish_ip = bind_ip
        else:
            self.publish_ip = publish_candidates[0]
        self.publish_addresses = _get_publish_addresses(
            bind_ip, self.publish_ip, publish_candidates
        )
        self._auto_base_url = (
            f"{protocol}://{format_ip_for_url(self.publish_ip)}:{self.publish_port}"
        )

    async def _build_config_entries(self, ssl_verify_result: str = "") -> tuple[ConfigEntry, ...]:
        """Build this module's config entries, optionally carrying an SSL verify result."""
        ip_addresses = await get_ip_addresses(include_ipv6=True)
        return (
            ConfigEntry(
                key=CONF_AUTH_ALLOW_SELF_REGISTRATION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                hidden=not any(provider.domain == "hass" for provider in self.mass.providers),
                requires_reload=False,
            ),
            ConfigEntry(
                key=CONF_BASE_URL,
                type=ConfigEntryType.STRING,
                default_value=CONF_VALUE_AUTO,
                requires_reload=False,
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SERVER_PORT,
                requires_reload=True,
            ),
            # the two alerts are mutually exclusive: the generic one while SSL is switched off,
            # and the SSL specific one when a certificate failed to load and left the webserver
            # on plain HTTP
            ConfigEntry(
                key="webserver_warn",
                type=ConfigEntryType.ALERT,
                required=False,
                hidden=self._ssl_configured,
                depends_on=CONF_ENABLE_SSL,
                depends_on_value=False,
            ),
            ConfigEntry(
                key="ssl_inactive_warn",
                type=ConfigEntryType.ALERT,
                required=False,
                hidden=not self._ssl_configured or self._ssl_active,
                depends_on=CONF_ENABLE_SSL,
            ),
            ConfigEntry(
                key=CONF_ENABLE_SSL,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_SSL_CERTIFICATE,
                type=ConfigEntryType.STRING,
                required=False,
                depends_on=CONF_ENABLE_SSL,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_SSL_PRIVATE_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                depends_on=CONF_ENABLE_SSL,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_ACTION_VERIFY_SSL,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_VERIFY_SSL,
                depends_on=CONF_ENABLE_SSL,
                required=False,
            ),
            ConfigEntry(
                key="ssl_verify_result",
                type=ConfigEntryType.LABEL,
                label=ssl_verify_result,
                hidden=not ssl_verify_result,
                depends_on=CONF_ENABLE_SSL,
                required=False,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_HOST,
                options=[ConfigValueOption(x, title=x) for x in {DEFAULT_HOST, *ip_addresses}],
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
        )

    async def _handle_cors_preflight(self, request: web.Request) -> web.Response:
        """Handle CORS preflight OPTIONS request."""
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",  # Cache preflight for 24 hours
            },
        )

    async def _handle_server_info(self, request: web.Request) -> web.Response:
        """Handle request for server info."""
        server_info = self.mass.get_server_info()
        # Add CORS headers to allow frontend to call from any origin
        return web.json_response(
            server_info.to_dict(),
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    async def _handle_ws_client(self, request: web.Request) -> web.WebSocketResponse:
        connection = WebsocketClientHandler(self, request)
        if lang := request.headers.get("Accept-Language"):
            self.mass.metadata.set_default_preferred_language(lang.split(",")[0])
        try:
            self.clients.add(connection)
            return await connection.handle_client()
        finally:
            self.clients.discard(connection)

    async def _handle_jsonrpc_api_command(self, request: web.Request) -> web.Response:
        """Handle incoming JSON RPC API command."""
        # These requests carry no connection identity, so the peer address is all an
        # unauthenticated handler has to tell one caller apart from another.
        set_current_peer_address(request.remote)
        # Fail early if we don't have any users yet
        if not self.auth.has_users:
            return web.Response(status=503, text="Setup required")
        if not request.can_read_body:
            return web.Response(status=400, text="Body required")
        cmd_data = await request.read()
        self.logger.log(VERBOSE_LOG_LEVEL, "Received on JSONRPC API: %s", cmd_data)
        try:
            command_msg = CommandMessage.from_json(cmd_data)
        except ValueError:
            error = f"Invalid JSON: {cmd_data.decode()}"
            self.logger.error("Unhandled JSONRPC API error: %s", error)
            return web.Response(status=400, text=error)
        except MissingField as e:
            # be forgiving if message_id is missing
            cmd_data_dict = json_loads(cmd_data)
            if e.field_name == "message_id" and "command" in cmd_data_dict:
                cmd_data_dict["message_id"] = "unknown"
                command_msg = CommandMessage.from_dict(cmd_data_dict)
            else:
                error = f"Missing field in JSON: {e.field_name}"
                self.logger.error("Unhandled JSONRPC API error: %s", error)
                return web.Response(status=400, text="Invalid JSON: missing required field")

        # work out handler for the given path/command
        handler = self.mass.command_handlers.get(command_msg.command)
        if handler is None:
            error = f"Invalid Command: {command_msg.command}"
            self.logger.error("Unhandled JSONRPC API error: %s", error)
            return web.Response(status=400, text=error)

        # Check authentication if required
        if error_response := await self._authenticate_api_command(request, handler):
            return error_response

        try:
            # handle the optional impersonation argument for impersonation-enabled commands
            if handler.allow_impersonation and command_msg.args:
                if impersonation_user := await resolve_command_impersonation(
                    self.mass, command_msg.args
                ):
                    set_impersonated_user(impersonation_user)
            args = parse_arguments(handler.signature, handler.type_hints, command_msg.args)
            result: Any = handler.target(**args)
            if hasattr(result, "__anext__"):
                # handle async generator (for really large listings)
                result = [item async for item in result]
            elif inspect.iscoroutine(result):
                result = await result
            # Determine the UI locale for this request from the HTTP headers and warm it up
            # so localized strings can be injected during dict serialization without disk I/O.
            locale = _locale_from_request(request)
            await self.mass.translations.ensure_locale_loaded(locale)
            return self._localized_json_response(result, locale)
        except InsufficientPermissions as e:
            return web.Response(status=403, text=str(e))
        except InvalidDataError as e:
            return web.Response(status=400, text=str(e))
        except Exception as e:
            # Return clean error message without stacktrace
            error_type = type(e).__name__
            error_msg = str(e)
            error = f"{error_type}: {error_msg}"
            self.logger.exception("Error executing command %s: %s", command_msg.command, error)
            return web.Response(status=500, text="Internal server error")

    async def _authenticate_api_command(
        self, request: web.Request, handler: APICommandHandler
    ) -> web.Response | None:
        """
        Authenticate the request and check the handler's required scope.

        Sets the authenticated user in context and returns an error response
        if authentication or the scope check failed, None otherwise.
        """
        if not (handler.authenticated or handler.required_scope):
            return None
        try:
            user = await get_authenticated_user(request)
        except Exception as e:
            self.logger.exception("Authentication error: %s", e)
            return web.Response(
                status=401,
                text="Authentication failed",
                headers={"WWW-Authenticate": 'Bearer realm="Music Assistant"'},
            )

        if not user:
            return web.Response(
                status=401,
                text="Authentication required",
                headers={"WWW-Authenticate": 'Bearer realm="Music Assistant"'},
            )

        # Set user and token in context and check the required scope
        set_current_user(user)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            set_current_token(auth_header[7:])
        if handler.required_scope and not has_scope(user, handler.required_scope):
            return web.Response(
                status=403,
                text=f"This command requires the {handler.required_scope} scope",
            )
        return None

    def _localized_json_response(self, result: Any, locale: str | None) -> web.Response:
        """
        Serialize a command result to a JSON response with the per-request resolvers bound.

        Sets the image-proxy resolver (for ``proxy_id`` injection) and the translation
        resolver (to localize human-readable fields) for the given locale during dict
        serialization, then resets them.
        """
        token = IMAGE_PROXY_ID_RESOLVER.set(self.mass.metadata.compute_image_id)
        token_loc = TRANSLATION_RESOLVER.set(
            partial(self.mass.translations.get_translation, locale=locale)
        )
        try:
            return web.json_response(result, dumps=json_dumps)
        finally:
            IMAGE_PROXY_ID_RESOLVER.reset(token)
            TRANSLATION_RESOLVER.reset(token_loc)

    async def _handle_api_intro(self, request: web.Request) -> web.Response:
        """Handle request for API introduction/documentation page."""
        intro_html_path = str(RESOURCES_DIR.joinpath("api_docs.html"))
        # Read the template
        async with aiofiles.open(intro_html_path) as f:
            html_content = await f.read()

        # Replace placeholders (escape values to prevent XSS)
        html_content = html_content.replace("{VERSION}", html.escape(self.mass.version))
        html_content = html_content.replace("{BASE_URL}", html.escape(self.base_url))
        html_content = html_content.replace("{SERVER_HOST}", html.escape(request.host))

        return web.Response(text=html_content, content_type="text/html")

    async def _handle_openapi_spec(self, request: web.Request) -> web.Response:
        """Handle request for OpenAPI specification (generated on-the-fly)."""
        spec = generate_openapi_spec(
            self.mass.command_handlers, server_url=self.base_url, version=self.mass.version
        )
        return web.json_response(spec)

    async def _handle_commands_reference(self, request: web.Request) -> web.FileResponse:
        """Handle request for commands reference page."""
        commands_html_path = str(RESOURCES_DIR.joinpath("commands_reference.html"))
        return await self._server.serve_static(commands_html_path, request)

    async def _handle_commands_json(self, request: web.Request) -> web.Response:
        """Handle request for commands JSON data (generated on-the-fly)."""
        commands_data = generate_commands_json(self.mass.command_handlers)
        return web.json_response(commands_data)

    async def _handle_schemas_reference(self, request: web.Request) -> web.FileResponse:
        """Handle request for schemas reference page."""
        schemas_html_path = str(RESOURCES_DIR.joinpath("schemas_reference.html"))
        return await self._server.serve_static(schemas_html_path, request)

    async def _handle_schemas_json(self, request: web.Request) -> web.Response:
        """Handle request for schemas JSON data (generated on-the-fly)."""
        schemas_data = generate_schemas_json(self.mass.command_handlers)
        return web.json_response(schemas_data)

    async def _handle_swagger_ui(self, request: web.Request) -> web.FileResponse:
        """Handle request for Swagger UI."""
        swagger_html_path = str(RESOURCES_DIR.joinpath("swagger_ui.html"))
        return await self._server.serve_static(swagger_html_path, request)

    async def _render_error_page(self, error_message: str, status: int = 403) -> web.Response:
        """
        Render a user-friendly error page with the given message.

        :param error_message: The error message to display to the user.
        :param status: HTTP status code for the response.
        """
        error_html_path = str(RESOURCES_DIR.joinpath("error.html"))
        async with aiofiles.open(error_html_path) as f:
            html_content = await f.read()
        # Replace placeholder with the actual error message (escape to prevent XSS)
        html_content = html_content.replace("{{ERROR_MESSAGE}}", html.escape(error_message))
        return web.Response(text=html_content, content_type="text/html", status=status)

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        """Handle request for index page (Vue frontend)."""
        is_ingress_request = is_request_from_ingress(request)

        if (not self.auth.has_users or not self.mass.config.onboard_done) and is_ingress_request:
            # a non-admin user tries to access the index via HA ingress
            # while we're not yet onboarded, prevent that as it leads to a bad UX
            ingress_user_id = request.headers.get("X-Remote-User-ID", "")
            role = await get_ha_user_role(self.mass, ingress_user_id)
            if role != UserRole.ADMIN:
                return await self._render_error_page(
                    "Administrator permissions are required to complete the initial setup. "
                    "Please ask a Home Assistant administrator to complete the setup first."
                )
            # NOTE: For ingress admin user,
            # we allow access to index, user will be auto created and then forwarded to the
            # frontend (which will take care of onboarding)

        if not self.auth.has_users and not is_ingress_request:
            # non ingress request and no users yet, redirect to setup
            return web.Response(status=302, headers={"Location": "setup"})

        # Serve the Vue frontend index.html
        return await self._server.serve_static(self._index_path, request)

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        """Handle request for login page (external client OAuth callback scenario)."""
        if not self.auth.has_users:
            # not yet onboarded (no first admin user exists), redirect to setup
            return_url = request.query.get("return_url", "")
            device_name = request.query.get("device_name", "")
            setup_url = (
                f"/setup?return_url={return_url}&device_name={device_name}"
                if return_url
                else "/setup"
            )
            return web.Response(status=302, headers={"Location": setup_url})
        # Serve login page for external clients
        login_html_path = str(RESOURCES_DIR.joinpath("login.html"))
        async with aiofiles.open(login_html_path) as f:
            html_content = await f.read()
        return web.Response(text=html_content, content_type="text/html")

    async def _handle_auth_login(self, request: web.Request) -> web.Response:
        """Handle login request."""
        # Block until onboarding is complete
        if not self.auth.has_users:
            return web.json_response(
                {"success": False, "error": "Setup required"},
                status=403,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                },
            )

        try:
            if not request.can_read_body:
                return web.Response(status=400, text="Body required")

            body = await request.json()
            provider_id = body.get("provider_id", "builtin")  # Default to built-in provider
            credentials = body.get("credentials", {})
            return_url = body.get("return_url")  # Optional return URL for redirect after login

            # Authenticate with provider
            auth_result = await self.auth.authenticate_with_credentials(provider_id, credentials)

            if not auth_result.success or not auth_result.user:
                return web.json_response(
                    {"success": False, "error": auth_result.error},
                    status=401,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, Authorization",
                    },
                )

            # Create token for user
            device_name = body.get(
                "device_name", f"{request.headers.get('User-Agent', 'Unknown')[:50]}"
            )
            token = await self.auth.create_token(auth_result.user, device_name)

            # Prepare response data
            response_data = {
                "success": True,
                "token": token,
                "user": auth_result.user.to_dict(),
            }

            # If return_url provided, append code parameter and return as redirect_to
            if return_url:
                # SECURITY FIX (GHSA-j369-4c4w-7qmq): only forward the token to trusted
                # destinations. is_allowed_redirect_url returns (True, "external") for any
                # unknown external URL, so checking is_valid alone would still leak the JWT.
                # Unlike _handle_auth_authorize/_handle_auth_callback, this endpoint appends
                # the token immediately with no consent step, so "external" must be rejected.
                _, category = is_allowed_redirect_url(return_url, request, self.base_url)
                if category != "trusted":
                    return web.Response(status=400, text="Invalid return_url")

                redirect_url = build_code_redirect_url(return_url, token)

                response_data["redirect_to"] = redirect_url
                self.logger.debug(
                    "Login successful, returning redirect_to: %s",
                    redirect_url.replace(token, "***TOKEN***"),
                )

            # Add CORS headers to allow login from any origin
            return web.json_response(
                response_data,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                },
            )
        except Exception:
            self.logger.exception("Error during login")
            return web.json_response(
                {"success": False, "error": "Login failed"},
                status=500,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                },
            )

    async def _handle_auth_logout(self, request: web.Request) -> web.Response:
        """Handle logout request."""
        user = await get_authenticated_user(request)
        if not user:
            return web.Response(status=401, text="Not authenticated")

        # Get token from request
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # Find and revoke the token
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            token_row = await self.auth.database.get_row("auth_tokens", {"token_hash": token_hash})
            if token_row:
                await self.auth.database.delete("auth_tokens", {"token_id": token_row["token_id"]})

        return web.json_response({"success": True})

    async def _handle_auth_me(self, request: web.Request) -> web.Response:
        """Handle request for current user information."""
        user = await get_authenticated_user(request)
        if not user:
            return web.Response(status=401, text="Not authenticated")

        return web.json_response(user.to_dict())

    async def _handle_auth_me_update(self, request: web.Request) -> web.Response:
        """Handle request to update current user's profile."""
        user = await get_authenticated_user(request)
        if not user:
            return web.Response(status=401, text="Not authenticated")

        try:
            if not request.can_read_body:
                return web.Response(status=400, text="Body required")

            body = await request.json()
            username = body.get("username")
            display_name = body.get("display_name")
            avatar_url = body.get("avatar_url")

            # Update user
            updated_user = await self.auth.update_user(
                user,
                username=username,
                display_name=display_name,
                avatar_url=avatar_url,
            )

            return web.json_response({"success": True, "user": updated_user.to_dict()})
        except Exception:
            self.logger.exception("Error updating user profile")
            return web.json_response(
                {"success": False, "error": "Failed to update profile"}, status=500
            )

    async def _handle_auth_providers(self, request: web.Request) -> web.Response:
        """Handle request for available login providers."""
        try:
            providers = await self.auth.get_login_providers()
            return web.json_response(providers)
        except Exception:
            self.logger.exception("Error getting auth providers")
            return web.json_response({"error": "Failed to get auth providers"}, status=500)

    async def _handle_auth_authorize(self, request: web.Request) -> web.Response:
        """Handle OAuth authorization request."""
        try:
            provider_id = request.query.get("provider_id")
            return_url = request.query.get("return_url")

            self.logger.debug(
                "OAuth authorize request: provider_id=%s, return_url=%s", provider_id, return_url
            )

            if not provider_id:
                return web.Response(status=400, text="provider_id required")

            # Validate return_url if provided
            if return_url:
                is_valid, _ = is_allowed_redirect_url(return_url, request, self.base_url)
                if not is_valid:
                    return web.Response(status=400, text="Invalid return_url")

            auth_url = await self.auth.get_authorization_url(provider_id, return_url)
            if not auth_url:
                return web.Response(
                    status=400, text="Provider does not support OAuth or is not configured"
                )

            return web.json_response({"authorization_url": auth_url})
        except Exception:
            self.logger.exception("Error during OAuth authorization")
            return web.json_response({"error": "Authorization failed"}, status=500)

    async def _handle_auth_callback(self, request: web.Request) -> web.Response:
        """Handle OAuth callback."""
        try:
            code = request.query.get("code")
            state = request.query.get("state")
            provider_id = request.query.get("provider_id")

            if not code or not state or not provider_id:
                return web.Response(status=400, text="code, state, and provider_id required")

            redirect_uri = f"{self.base_url}/auth/callback?provider_id={provider_id}"
            auth_result = await self.auth.handle_oauth_callback(
                provider_id, code, state, redirect_uri
            )

            if not auth_result.success or not auth_result.user:
                # Return error page
                error_html = f"""
                <html>
                <body>
                    <h1>Authentication Failed</h1>
                    <p>{html.escape(auth_result.error or "Unknown error")}</p>
                    <a href="/login">Back to Login</a>
                </body>
                </html>
                """
                return web.Response(text=error_html, content_type="text/html", status=400)

            # Create token
            device_name = f"OAuth ({provider_id})"
            token = await self.auth.create_token(auth_result.user, device_name)

            # Determine redirect URL (use return_url from OAuth flow or default to root)
            final_redirect_url = auth_result.return_url or "/"
            requires_consent = False

            # Validate redirect URL for security
            if auth_result.return_url:
                is_valid, category = is_allowed_redirect_url(
                    auth_result.return_url, request, self.base_url
                )
                if not is_valid:
                    self.logger.warning("Invalid return_url blocked: %s", auth_result.return_url)
                    final_redirect_url = "/"
                elif category == "external":
                    # External domain - require user consent
                    requires_consent = True
            final_redirect_url = build_code_redirect_url(final_redirect_url, token)

            # Load OAuth callback success page template and inject token and redirect URL
            oauth_callback_html_path = str(RESOURCES_DIR.joinpath("oauth_callback.html"))
            async with aiofiles.open(oauth_callback_html_path) as f:
                success_html = await f.read()

            # Replace the redirect last so its untrusted contents cannot match another placeholder.
            success_html = success_html.replace(
                "{REQUIRES_CONSENT}", "true" if requires_consent else "false"
            )
            success_html = success_html.replace("{TOKEN}", _serialize_script_value(token))
            success_html = success_html.replace(
                "{REDIRECT_URL}", _serialize_script_value(final_redirect_url)
            )

            return web.Response(text=success_html, content_type="text/html")
        except Exception:
            self.logger.exception("Error during OAuth callback")
            error_html = """
            <html>
            <body>
                <h1>Authentication Failed</h1>
                <p>An error occurred during authentication</p>
                <a href="/login">Back to Login</a>
            </body>
            </html>
            """
            return web.Response(text=error_html, content_type="text/html", status=500)

    async def _handle_setup_page(self, request: web.Request) -> web.Response:
        """Handle request for first-time setup page."""
        # Setup forwards the admin token here with no consent step, so require a trusted destination.
        return_url = request.query.get("return_url")
        if return_url:
            _, category = is_allowed_redirect_url(return_url, request, self.base_url)
            if category != "trusted":
                return web.Response(status=400, text="Invalid return_url")

        if self.auth.has_users:
            # this should not happen, but guard anyways
            return await self._render_error_page("Setup has already been completed.")

        setup_html_path = str(RESOURCES_DIR.joinpath("setup.html"))
        async with aiofiles.open(setup_html_path) as f:
            html_content = await f.read()

        return web.Response(text=html_content, content_type="text/html")

    async def _handle_setup(self, request: web.Request) -> web.Response:
        """Handle first-time setup request to create admin user (non-ingress only)."""
        if self.auth.has_users:
            return web.json_response(
                {"success": False, "error": "Setup already completed"}, status=400
            )

        if not request.can_read_body:
            return web.Response(status=400, text="Body required")

        body = await request.json()
        username = body.get("username", "").strip()
        password = body.get("password", "")

        # Validation
        if not username or len(username) < 2:
            return web.json_response(
                {"success": False, "error": "Username must be at least 2 characters"}, status=400
            )

        if not password or len(password) < 8:
            return web.json_response(
                {"success": False, "error": "Password must be at least 8 characters"}, status=400
            )

        try:
            builtin_provider = self.auth.login_providers.get("builtin")
            if not builtin_provider:
                return web.json_response(
                    {"success": False, "error": "Built-in auth provider not available"},
                    status=500,
                )

            if not isinstance(builtin_provider, BuiltinLoginProvider):
                return web.json_response(
                    {"success": False, "error": "Built-in provider configuration error"},
                    status=500,
                )

            # Create admin user with password
            user = await builtin_provider.create_user_with_password(
                username, password, role=UserRole.ADMIN
            )

            # Create token for the new admin
            device_name = body.get(
                "device_name", f"Setup ({request.headers.get('User-Agent', 'Unknown')[:50]})"
            )
            token = await self.auth.create_token(user, device_name)

            self.logger.info("First admin user created: %s", username)

            # Return token - frontend will complete onboarding via config/onboard_complete
            response_data: dict[str, Any] = {
                "success": True,
                "token": token,
                "user": user.to_dict(),
            }

            # Only forward the token to a trusted destination (no consent step here).
            return_url = body.get("return_url")
            if return_url and isinstance(return_url, str):
                _, category = is_allowed_redirect_url(return_url, request, self.base_url)
                if category == "trusted":
                    response_data["redirect_to"] = build_code_redirect_url(
                        return_url, token, {"onboard": "true"}
                    )
                else:
                    self.logger.warning("Ignoring untrusted setup return_url: %s", return_url)

            return web.json_response(response_data)

        except Exception as e:
            self.logger.exception("Error during setup")
            return web.json_response(
                {"success": False, "error": f"Setup failed: {e!s}"}, status=500
            )


def _serialize_script_value(value: str) -> str:
    """Serialize a string for use inside an HTML script element."""
    return (
        json_dumps(value)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
