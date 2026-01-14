"""
Controller that manages the builtin webserver that hosts the api and frontend.

Unlike the streamserver (which is as simple and unprotected as possible),
this webserver allows for more fine grained configuration to better secure it.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from concurrent import futures
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

import aiofiles
from aiohttp import ClientTimeout, web
from mashumaro.exceptions import MissingField
from music_assistant_frontend import where as locate_frontend
from music_assistant_models.api import CommandMessage
from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import (
    CONF_AUTH_ALLOW_SELF_REGISTRATION,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    RESOURCES_DIR,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.webserver.helpers.ssl import (
    create_server_ssl_context,
    format_certificate_info,
    verify_ssl_certificate,
)
from music_assistant.helpers.api import parse_arguments
from music_assistant.helpers.audio import get_preview_stream
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.redirect_validation import is_allowed_redirect_url
from music_assistant.helpers.util import get_ip_addresses
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.core_controller import CoreController

from .api_docs import generate_commands_json, generate_openapi_spec, generate_schemas_json
from .auth import AuthenticationManager
from .helpers.auth_middleware import (
    get_authenticated_user,
    is_request_from_ingress,
    set_current_user,
)
from .helpers.auth_providers import BuiltinLoginProvider, get_ha_user_role
from .remote_access import RemoteAccessManager
from .sendspin_proxy import SendspinProxyHandler
from .websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, CoreConfig

    from music_assistant import MusicAssistant

DEFAULT_SERVER_PORT = 8095
INGRESS_SERVER_PORT = 8094
CONF_BASE_URL = "base_url"
CONF_ENABLE_SSL = "enable_ssl"
CONF_SSL_CERTIFICATE = "ssl_certificate"
CONF_SSL_PRIVATE_KEY = "ssl_private_key"
CONF_ACTION_VERIFY_SSL = "verify_ssl"
MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)


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
        """Return the base_url for the streamserver."""
        return self._server.base_url

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        ip_addresses = await get_ip_addresses()
        default_publish_ip = ip_addresses[0]

        # Handle verify SSL action
        ssl_verify_result = ""
        if action == CONF_ACTION_VERIFY_SSL and values:
            cert_info = await verify_ssl_certificate(
                str(values.get(CONF_SSL_CERTIFICATE, "")),
                str(values.get(CONF_SSL_PRIVATE_KEY, "")),
            )
            ssl_verify_result = format_certificate_info(cert_info)

        # Determine if SSL is enabled from values
        ssl_enabled = values.get(CONF_ENABLE_SSL, False) if values else False
        protocol = "https" if ssl_enabled else "http"
        default_base_url = f"{protocol}://{default_publish_ip}:{DEFAULT_SERVER_PORT}"
        return (
            ConfigEntry(
                key=CONF_AUTH_ALLOW_SELF_REGISTRATION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Allow User Self-Registration",
                description="Allow users to create accounts via Home Assistant OAuth.",
                hidden=not any(provider.domain == "hass" for provider in self.mass.providers),
            ),
            ConfigEntry(
                key=CONF_BASE_URL,
                type=ConfigEntryType.STRING,
                default_value=default_base_url,
                label="Base URL",
                description="The (base) URL to reach this webserver in the network. \n"
                "Override this in advanced scenarios where for example you're running "
                "the webserver behind a reverse proxy.",
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SERVER_PORT,
                label="TCP Port",
                description="The TCP port to run the webserver.",
            ),
            ConfigEntry(
                key="webserver_warn",
                type=ConfigEntryType.ALERT,
                label="Please note that the webserver is by default unencrypted. "
                "Never ever expose the webserver directly to the internet! \n\n"
                "Enable SSL below or use a reverse proxy or VPN to secure access. \n\n"
                "As an alternative, consider using the Remote Access feature which "
                "secures access to your Music Assistant instance without the need to "
                "expose your webserver directly.",
                required=False,
                depends_on=CONF_ENABLE_SSL,
                depends_on_value=False,
                hidden=bool(values.get(CONF_ENABLE_SSL, False)) if values else False,
            ),
            ConfigEntry(
                key=CONF_ENABLE_SSL,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Enable SSL/TLS",
                description="Enable HTTPS by providing an SSL certificate and private key. \n"
                "This encrypts all communication with the webserver.",
            ),
            ConfigEntry(
                key=CONF_SSL_CERTIFICATE,
                type=ConfigEntryType.STRING,
                label="SSL Certificate",
                description="Provide your SSL certificate in PEM format. You can either:\n"
                "- Paste the full contents of your certificate file, or\n"
                "- Enter an absolute file path (e.g., /ssl/fullchain.pem)\n\n"
                "This should include the full certificate chain if applicable.\n"
                "Both RSA and ECDSA certificates are supported.",
                required=False,
                depends_on=CONF_ENABLE_SSL,
            ),
            ConfigEntry(
                key=CONF_SSL_PRIVATE_KEY,
                type=ConfigEntryType.SECURE_STRING,
                label="SSL Private Key",
                description="Provide your SSL private key in PEM format. You can either:\n"
                "- Paste the full contents of your private key file, or\n"
                "- Enter an absolute file path (e.g., /ssl/privkey.pem)\n\n"
                "Both RSA and ECDSA keys are supported. The key must be unencrypted.\n"
                "This is securely encrypted and stored.",
                required=False,
                depends_on=CONF_ENABLE_SSL,
            ),
            ConfigEntry(
                key=CONF_ACTION_VERIFY_SSL,
                type=ConfigEntryType.ACTION,
                label="Verify SSL Certificate",
                description="Test your certificate and private key to verify they are valid "
                "and match each other.",
                action=CONF_ACTION_VERIFY_SSL,
                action_label="Verify",
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
                default_value="0.0.0.0",
                options=[ConfigValueOption(x, x) for x in {"0.0.0.0", *ip_addresses}],
                label="Bind to IP/interface",
                description="Bind the (web)server to this specific interface. \n"
                "Use 0.0.0.0 to bind to all interfaces. \n"
                "Set this address for example to a docker-internal network, "
                "when you are running a reverse proxy to enhance security and "
                "protect outside access to the webinterface and API. \n\n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                category="advanced",
            ),
        )

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
        # also host the image proxy on the webserver
        routes.append(("GET", "/imageproxy", self.mass.metadata.handle_imageproxy))
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
        all_ip_addresses = await get_ip_addresses()
        default_publish_ip = all_ip_addresses[0]
        if self.mass.running_as_hass_addon:
            # if we're running on the HA supervisor we start an additional TCP site
            # on the internal ("172.30.32.) IP for the HA ingress proxy
            ingress_host = next(
                (x for x in all_ip_addresses if x.startswith("172.30.32.")), default_publish_ip
            )
            ingress_tcp_site_params = (ingress_host, INGRESS_SERVER_PORT)
        else:
            ingress_tcp_site_params = None
        base_url = str(config.get_value(CONF_BASE_URL))
        port_value = config.get_value(CONF_BIND_PORT)
        assert isinstance(port_value, int)
        self.publish_port = port_value
        self.publish_ip = default_publish_ip
        bind_ip = cast("str | None", config.get_value(CONF_BIND_IP))
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
                "the Webserver in Settings --> Core modules --> Webserver\n"
                "\n"
                "################################################################################\n",
                base_url,
            )

        # Create SSL context if SSL is enabled
        ssl_context = None
        ssl_enabled = config.get_value(CONF_ENABLE_SSL, False)
        if ssl_enabled:
            ssl_context = await create_server_ssl_context(
                str(config.get_value(CONF_SSL_CERTIFICATE) or ""),
                str(config.get_value(CONF_SSL_PRIVATE_KEY) or ""),
                logger=self.logger,
            )

        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=self.publish_port,
            base_url=base_url,
            static_routes=routes,
            # add assets subdir as static_content
            static_content=("/assets", os.path.join(frontend_dir, "assets"), "assets"),
            ingress_tcp_site_params=ingress_tcp_site_params,
            # Add mass object to app for use in auth middleware
            app_state={"mass": self.mass},
            ssl_context=ssl_context,
        )
        if self.mass.running_as_hass_addon:
            # (re)announce to HA supervisor to make sure that HA picks it up
            await self._announce_to_homeassistant()

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

    async def serve_preview_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve short preview sample."""
        provider_instance_id_or_domain = request.query["provider"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": "audio/aac"})
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_instance_id_or_domain, item_id):
            await resp.write(chunk)
        return resp

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
        if handler.authenticated or handler.required_role:
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

            # Set user in context and check role
            set_current_user(user)
            if handler.required_role == "admin" and user.role != UserRole.ADMIN:
                return web.Response(
                    status=403,
                    text="Admin access required",
                )

        try:
            args = parse_arguments(handler.signature, handler.type_hints, command_msg.args)
            result: Any = handler.target(**args)
            if hasattr(result, "__anext__"):
                # handle async generator (for really large listings)
                result = [item async for item in result]
            elif asyncio.iscoroutine(result):
                result = await result
            return web.json_response(result, dumps=json_dumps)
        except Exception as e:
            # Return clean error message without stacktrace
            error_type = type(e).__name__
            error_msg = str(e)
            error = f"{error_type}: {error_msg}"
            self.logger.exception("Error executing command %s: %s", command_msg.command, error)
            return web.Response(status=500, text="Internal server error")

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
        """Render a user-friendly error page with the given message.

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
                # Insert code parameter before any hash fragment
                code_param = f"code={quote(token, safe='')}"
                if "#" in return_url:
                    url_parts = return_url.split("#", 1)
                    base_part = url_parts[0]
                    hash_part = url_parts[1]
                    separator = "&" if "?" in base_part else "?"
                    redirect_url = f"{base_part}{separator}{code_param}#{hash_part}"
                elif "?" in return_url:
                    redirect_url = f"{return_url}&{code_param}"
                else:
                    redirect_url = f"{return_url}?{code_param}"

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
            # Add code parameter to redirect URL (the token URL-encoded)
            # Important: Insert code BEFORE any hash fragment (e.g., #/) to ensure
            # it's in query params, not inside the hash where Vue Router can't access it
            code_param = f"code={quote(token, safe='')}"

            # Split URL by hash to insert code in the right place
            if "#" in final_redirect_url:
                # URL has a hash fragment (e.g., http://example.com/#/ or http://example.com/path#section)
                url_parts = final_redirect_url.split("#", 1)
                base_url = url_parts[0]
                hash_part = url_parts[1]

                # Add code to base URL (before hash)
                separator = "&" if "?" in base_url else "?"
                final_redirect_url = f"{base_url}{separator}{code_param}#{hash_part}"
            # No hash fragment, simple case
            elif "?" in final_redirect_url:
                final_redirect_url = f"{final_redirect_url}&{code_param}"
            else:
                final_redirect_url = f"{final_redirect_url}?{code_param}"

            # Load OAuth callback success page template and inject token and redirect URL
            oauth_callback_html_path = str(RESOURCES_DIR.joinpath("oauth_callback.html"))
            async with aiofiles.open(oauth_callback_html_path) as f:
                success_html = await f.read()

            # Replace template placeholders
            success_html = success_html.replace("{TOKEN}", token)
            success_html = success_html.replace("{REDIRECT_URL}", final_redirect_url)
            success_html = success_html.replace(
                "{REQUIRES_CONSENT}", "true" if requires_consent else "false"
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
        # Validate return_url if provided
        return_url = request.query.get("return_url")
        if return_url:
            is_valid, _ = is_allowed_redirect_url(return_url, request, self.base_url)
            if not is_valid:
                return web.Response(status=400, text="Invalid return_url")
        else:
            return_url = "/"

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
            return web.json_response(
                {
                    "success": True,
                    "token": token,
                    "user": user.to_dict(),
                }
            )

        except Exception as e:
            self.logger.exception("Error during setup")
            return web.json_response(
                {"success": False, "error": f"Setup failed: {e!s}"}, status=500
            )

    async def _announce_to_homeassistant(self) -> None:
        """Announce Music Assistant Ingress server to Home Assistant via Supervisor API."""
        supervisor_token = os.environ["SUPERVISOR_TOKEN"]
        addon_hostname = os.environ["HOSTNAME"]
        # Get or create auth token for the HA system user
        ha_integration_token = await self.auth.get_homeassistant_system_user_token()
        discovery_payload = {
            "service": "music_assistant",
            "config": {
                "host": addon_hostname,
                "port": INGRESS_SERVER_PORT,
                "auth_token": ha_integration_token,
            },
        }
        try:
            async with self.mass.http_session_no_ssl.post(
                "http://supervisor/discovery",
                headers={"Authorization": f"Bearer {supervisor_token}"},
                json=discovery_payload,
                timeout=ClientTimeout(total=10),
            ) as response:
                response.raise_for_status()
                result = await response.json()
                self.logger.debug(
                    "Successfully announced to Home Assistant. Discovery UUID: %s",
                    result.get("uuid"),
                )
        except Exception as err:
            self.logger.warning("Failed to announce to Home Assistant: %s", err)
