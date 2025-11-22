"""
Controller that manages the builtin webserver that hosts the api and frontend.

Unlike the streamserver (which is as simple and unprotected as possible),
this webserver allows for more fine grained configuration to better secure it.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from concurrent import futures
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

import aiofiles
from aiohttp import WSMsgType, web
from mashumaro.exceptions import MissingField
from music_assistant_frontend import where as locate_frontend
from music_assistant_models.api import (
    CommandMessage,
    ErrorResultMessage,
    MessageType,
    SuccessResultMessage,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import InvalidCommand

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT, VERBOSE_LOG_LEVEL
from music_assistant.helpers.api import APICommandHandler, api_command, parse_arguments
from music_assistant.helpers.audio import get_preview_stream
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.util import get_ip_addresses
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.auth import UserAuthProvider, UserRole
from music_assistant.models.core_controller import CoreController

from .api_docs import generate_commands_reference, generate_openapi_spec, generate_schemas_reference
from .auth import AuthenticationManager
from .helpers.auth_middleware import (
    get_authenticated_user,
    get_current_user,
    is_request_from_ingress,
    set_current_user,
)
from .helpers.auth_providers import BuiltinLoginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, CoreConfig
    from music_assistant_models.event import MassEvent

    from music_assistant import MusicAssistant

DEFAULT_SERVER_PORT = 8095
INGRESS_SERVER_PORT = 8094
CONF_BASE_URL = "base_url"
CONF_AUTH_ENABLED = "auth_enabled"
CONF_AUTH_ALLOW_SELF_REGISTRATION = "auth_allow_self_registration"
CONF_AUTH_HA_ENABLED = "auth_ha_enabled"
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
        default_base_url = f"http://{default_publish_ip}:{DEFAULT_SERVER_PORT}"
        return (
            ConfigEntry(
                key="webserver_warn",
                type=ConfigEntryType.ALERT,
                label="Please note that the webserver is unprotected. "
                "Never ever expose the webserver directly to the internet! \n\n"
                "Use a reverse proxy or VPN to secure access.",
                required=False,
            ),
            ConfigEntry(
                key=CONF_AUTH_HA_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Enable Home Assistant OAuth",
                description="Allow users to sign in with their Home Assistant account. \n"
                "Requires the Home Assistant provider (plugin) to be configured. \n"
                "Uses hass_client for seamless authentication - "
                "no manual OAuth app setup required!",
                depends_on=CONF_AUTH_ENABLED,
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
            # Authentication settings
            ConfigEntry(
                key=CONF_AUTH_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Enable Authentication",
                description="Enable authentication for the web interface and API. \n"
                "When enabled, users must log in to access Music Assistant. \n"
                "Note that while this is now still a provisional feature, "
                "it will be mandatory in future releases!",
                category="advanced",
            ),
            ConfigEntry(
                key=CONF_AUTH_ALLOW_SELF_REGISTRATION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Allow Self-Registration",
                description="Allow users to create accounts via Home Assistant OAuth. \n"
                "New users will have USER role by default.",
                category="advanced",
                depends_on=CONF_AUTH_HA_ENABLED,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
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
        # add index
        index_path = os.path.join(frontend_dir, "index.html")
        handler = partial(self._server.serve_static, index_path)
        routes.append(("GET", "/", handler))
        # add info
        routes.append(("GET", "/info", self._handle_server_info))
        # add logging
        routes.append(("GET", "/music-assistant.log", self._handle_application_log))
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
        routes.append(("GET", "/api-docs/schemas", self._handle_schemas_reference))
        routes.append(("GET", "/api-docs/schemas/", self._handle_schemas_reference))
        routes.append(("GET", "/api-docs/openapi.json", self._handle_openapi_spec))
        routes.append(("GET", "/api-docs/swagger", self._handle_swagger_ui))
        routes.append(("GET", "/api-docs/swagger/", self._handle_swagger_ui))
        # add authentication routes
        routes.append(("GET", "/login", self._handle_login_page))
        routes.append(("POST", "/auth/login", self._handle_auth_login))
        routes.append(("POST", "/auth/logout", self._handle_auth_logout))
        routes.append(("GET", "/auth/providers", self._handle_auth_providers))
        routes.append(("GET", "/auth/authorize", self._handle_auth_authorize))
        routes.append(("GET", "/auth/callback", self._handle_auth_callback))
        # add first-time setup routes
        routes.append(("GET", "/setup", self._handle_setup_page))
        routes.append(("POST", "/auth/setup", self._handle_setup))
        # Initialize authentication manager
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
        if not self.mass.config.onboard_done:
            self.logger.warning(
                "\n\n################################################################################\n"
                "Starting webserver on  %s:%s - base url: %s\n"
                "If this is incorrect, see the documentation how to configure the Webserver\n"
                "in Settings --> Core modules --> Webserver\n"
                "################################################################################\n",
                bind_ip,
                self.publish_port,
                base_url,
            )
        else:
            self.logger.info(
                "Starting webserver on  %s:%s - base url: %s\n#\n",
                bind_ip,
                self.publish_port,
                base_url,
            )
        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=self.publish_port,
            base_url=base_url,
            static_routes=routes,
            # add assets subdir as static_content
            static_content=("/assets", os.path.join(frontend_dir, "assets"), "assets"),
            ingress_tcp_site_params=ingress_tcp_site_params,
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        for client in set(self.clients):
            await client.disconnect()
        await self._server.close()
        await self.auth.close()

    async def serve_preview_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve short preview sample."""
        provider_instance_id_or_domain = request.query["provider"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": "audio/aac"})
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_instance_id_or_domain, item_id):
            await resp.write(chunk)
        return resp

    async def _handle_server_info(self, request: web.Request) -> web.Response:
        """Handle request for server info."""
        return web.json_response(self.mass.get_server_info().to_dict())

    async def _handle_ws_client(self, request: web.Request) -> web.WebSocketResponse:
        connection = WebsocketClientHandler(self, request)
        if lang := request.headers.get("Accept-Language"):
            self.mass.metadata.set_default_preferred_language(lang.split(",")[0])
        try:
            self.clients.add(connection)
            return await connection.handle_client()
        finally:
            self.clients.remove(connection)

    async def _handle_jsonrpc_api_command(self, request: web.Request) -> web.Response:
        """Handle incoming JSON RPC API command."""
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
                error = f"Missing field in JSON: {e!s}"
                self.logger.error("Unhandled JSONRPC API error: %s", error)
                return web.Response(status=400, text=error)

        # work out handler for the given path/command
        handler = self.mass.command_handlers.get(command_msg.command)
        if handler is None:
            error = f"Invalid Command: {command_msg.command}"
            self.logger.error("Unhandled JSONRPC API error: %s", error)
            return web.Response(status=400, text=error)

        # Check authentication if required
        if handler.authenticated or handler.required_role:
            # Skip auth for ingress requests (HA handles it)
            if not is_request_from_ingress(request):
                user = await get_authenticated_user(request)
                if not user:
                    return web.Response(
                        status=401,
                        text="Authentication required",
                        headers={"WWW-Authenticate": 'Bearer realm="Music Assistant"'},
                    )

                # Set user in context for API methods
                set_current_user(user)

                # Check role if required
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
            self.logger.error("Error executing command %s: %s", command_msg.command, error)
            return web.Response(status=500, text=error)

    async def _handle_application_log(self, request: web.Request) -> web.Response:
        """Handle request to get the application log."""
        log_data = await self.mass.get_application_log()
        return web.Response(text=log_data, content_type="text/text")

    async def _handle_api_intro(self, request: web.Request) -> web.Response:
        """Handle request for API introduction/documentation page."""
        intro_html_path = os.path.join(
            os.path.dirname(__file__), "..", "helpers", "resources", "api_docs.html"
        )
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

    async def _handle_commands_reference(self, request: web.Request) -> web.Response:
        """Handle request for commands reference page (generated on-the-fly)."""
        html = generate_commands_reference(self.mass.command_handlers, server_url=self.base_url)
        return web.Response(text=html, content_type="text/html")

    async def _handle_schemas_reference(self, request: web.Request) -> web.Response:
        """Handle request for schemas reference page (generated on-the-fly)."""
        html = generate_schemas_reference(self.mass.command_handlers)
        return web.Response(text=html, content_type="text/html")

    async def _handle_swagger_ui(self, request: web.Request) -> web.FileResponse:
        """Handle request for Swagger UI."""
        swagger_html_path = os.path.join(
            os.path.dirname(__file__), "..", "helpers", "resources", "swagger_ui.html"
        )
        return await self._server.serve_static(swagger_html_path, request)

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        """Handle request for login page."""
        # If auth is enabled and no users exist, redirect to setup
        if self.auth.enabled and not await self.auth.has_users():
            return web.Response(status=302, headers={"Location": "/setup"})

        login_html_path = os.path.join(
            os.path.dirname(__file__), "..", "helpers", "resources", "login.html"
        )
        async with aiofiles.open(login_html_path) as f:
            html_content = await f.read()
        return web.Response(text=html_content, content_type="text/html")

    async def _handle_auth_login(self, request: web.Request) -> web.Response:
        """Handle login request."""
        if not request.can_read_body:
            return web.Response(status=400, text="Body required")

        body = await request.json()
        provider_id = body.get("provider_id")
        credentials = body.get("credentials", {})

        if not provider_id:
            return web.json_response(
                {"success": False, "error": "Provider ID required"}, status=400
            )

        # Authenticate with provider
        auth_result = await self.auth.authenticate_with_credentials(provider_id, credentials)

        if not auth_result.success or not auth_result.user:
            return web.json_response({"success": False, "error": auth_result.error}, status=401)

        # Create token for user
        device_name = body.get(
            "device_name", f"{request.headers.get('User-Agent', 'Unknown')[:50]}"
        )
        token = await self.auth.create_token(auth_result.user, device_name)

        return web.json_response(
            {
                "success": True,
                "token": token,
                "user": auth_result.user.to_dict(),
            }
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
                await self.auth.revoke_token(token_row["token_id"], user)

        return web.json_response({"success": True})

    async def _handle_auth_providers(self, request: web.Request) -> web.Response:
        """Handle request for available login providers."""
        providers = await self.auth.get_login_providers()
        return web.json_response(providers)

    async def _handle_auth_authorize(self, request: web.Request) -> web.Response:
        """Handle OAuth authorization request."""
        provider_id = request.query.get("provider_id")
        redirect_uri = request.query.get("redirect_uri")

        if not provider_id or not redirect_uri:
            return web.Response(status=400, text="provider_id and redirect_uri required")

        auth_url = await self.auth.get_authorization_url(provider_id, redirect_uri)
        if not auth_url:
            return web.Response(
                status=400, text="Provider does not support OAuth or is not configured"
            )

        return web.json_response({"authorization_url": auth_url})

    async def _handle_auth_callback(self, request: web.Request) -> web.Response:
        """Handle OAuth callback."""
        code = request.query.get("code")
        state = request.query.get("state")
        provider_id = request.query.get("provider_id", "google")  # Default to google for now

        if not code or not state:
            return web.Response(status=400, text="code and state required")

        redirect_uri = f"{self.base_url}/auth/callback"
        auth_result = await self.auth.handle_oauth_callback(provider_id, code, state, redirect_uri)

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

        # Return success page with token
        success_html = f"""
        <html>
        <head>
            <title>Login Successful</title>
        </head>
        <body>
            <h1>Login Successful!</h1>
            <p>Redirecting...</p>
            <script>
                // Store token in localStorage
                localStorage.setItem('auth_token', '{token}');
                // Redirect to home
                window.location.href = '/';
            </script>
        </body>
        </html>
        """
        return web.Response(text=success_html, content_type="text/html")

    async def _handle_setup_page(self, request: web.Request) -> web.Response:
        """Handle request for first-time setup page."""
        # Check if setup is needed
        if await self.auth.has_users():
            # Already has users, redirect to login
            return web.Response(status=302, headers={"Location": "/login"})

        # Serve setup page
        setup_html_path = os.path.join(
            os.path.dirname(__file__), "..", "helpers", "resources", "setup.html"
        )
        async with aiofiles.open(setup_html_path) as f:
            html_content = await f.read()
        return web.Response(text=html_content, content_type="text/html")

    async def _handle_setup(self, request: web.Request) -> web.Response:
        """Handle first-time setup request to create admin user."""
        # Check if setup is still needed
        if await self.auth.has_users():
            return web.json_response(
                {"success": False, "error": "Setup already completed"}, status=400
            )

        if not request.can_read_body:
            return web.Response(status=400, text="Body required")

        body = await request.json()
        username = body.get("username", "").strip()
        password = body.get("password", "")

        # Validation
        if not username or len(username) < 3:
            return web.json_response(
                {"success": False, "error": "Username must be at least 3 characters"}, status=400
            )

        if not password or len(password) < 8:
            return web.json_response(
                {"success": False, "error": "Password must be at least 8 characters"}, status=400
            )

        try:
            # Get built-in provider

            builtin_provider = self.auth.login_providers.get("builtin")
            if not builtin_provider:
                return web.json_response(
                    {"success": False, "error": "Built-in auth provider not available"}, status=500
                )

            # Create admin user with password

            if not isinstance(builtin_provider, BuiltinLoginProvider):
                return web.json_response(
                    {"success": False, "error": "Built-in provider configuration error"}, status=500
                )

            user = await builtin_provider.create_user_with_password(
                username, password, role=UserRole.ADMIN
            )

            # Create token for the new admin
            device_name = f"Setup ({request.headers.get('User-Agent', 'Unknown')[:50]})"
            token = await self.auth.create_token(user, device_name)

            self.logger.info("First admin user created: %s", username)

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

    # User Management API Methods

    @api_command("auth/users", required_role="admin")
    async def get_users(self) -> list[dict[str, Any]]:
        """
        Get all users (admin only).

        :return: List of user dictionaries.
        """
        users = await self.auth.list_users()
        return [user.to_dict() for user in users]

    @api_command("auth/user", required_role="admin")
    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        """
        Get user by ID (admin only).

        :param user_id: The user ID.
        :return: User dictionary or None if not found.
        """
        user = await self.auth.get_user(user_id)
        return user.to_dict() if user else None

    @api_command("auth/user/update_role", required_role="admin")
    async def update_user_role(self, user_id: str, role: str) -> dict[str, Any]:
        """
        Update user role (admin only).

        :param user_id: The user ID.
        :param role: New role ("admin" or "user").
        :return: Success status.
        """
        admin_user = get_current_user()
        if not admin_user:
            return {"success": False, "error": "Not authenticated"}

        new_role = UserRole(role)
        success = await self.auth.update_user_role(user_id, new_role, admin_user)
        return {"success": success}

    @api_command("auth/user/enable", required_role="admin")
    async def enable_user(self, user_id: str) -> dict[str, Any]:
        """
        Enable user account (admin only).

        :param user_id: The user ID.
        :return: Success status.
        """
        admin_user = get_current_user()
        if not admin_user:
            return {"success": False, "error": "Not authenticated"}

        success = await self.auth.enable_user(user_id, admin_user)
        return {"success": success}

    @api_command("auth/user/disable", required_role="admin")
    async def disable_user(self, user_id: str) -> dict[str, Any]:
        """
        Disable user account (admin only).

        :param user_id: The user ID.
        :return: Success status.
        """
        admin_user = get_current_user()
        if not admin_user:
            return {"success": False, "error": "Not authenticated"}

        success = await self.auth.disable_user(user_id, admin_user)
        return {"success": success}

    @api_command("auth/user/delete", required_role="admin")
    async def delete_user(self, user_id: str) -> dict[str, Any]:
        """
        Delete user account (admin only).

        :param user_id: The user ID.
        :return: Success status.
        """
        admin_user = get_current_user()
        if not admin_user:
            return {"success": False, "error": "Not authenticated"}

        # Don't allow deleting yourself
        if user_id == admin_user.user_id:
            return {"success": False, "error": "Cannot delete your own account"}

        # Delete user from database
        try:
            await self.auth.database.delete("users", {"user_id": user_id})
            await self.auth.database.commit()
            return {"success": True}
        except Exception as e:
            self.logger.exception("Error deleting user")
            return {"success": False, "error": str(e)}

    @api_command("auth/change_password")
    async def change_password(self, old_password: str, new_password: str) -> dict[str, Any]:
        """
        Change current user's password (built-in auth only).

        :param old_password: Current password.
        :param new_password: New password.
        :return: Success status.
        """
        current_user_obj = get_current_user()
        if not current_user_obj:
            return {"success": False, "error": "Not authenticated"}

        # Get built-in provider
        builtin_provider = self.auth.login_providers.get("builtin")
        if not builtin_provider or not isinstance(builtin_provider, BuiltinLoginProvider):
            return {"success": False, "error": "Built-in auth not available"}

        # Change password
        try:
            await builtin_provider.change_password(current_user_obj, old_password, new_password)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @api_command("auth/tokens")
    async def get_my_tokens(self) -> list[dict[str, Any]]:
        """
        Get current user's auth tokens.

        :return: List of token dictionaries.
        """
        user = get_current_user()
        if not user:
            return []

        tokens = await self.auth.get_user_tokens(user)
        return [token.to_dict() for token in tokens]

    @api_command("auth/token/revoke")
    async def revoke_token(self, token_id: str) -> dict[str, Any]:
        """
        Revoke an auth token.

        :param token_id: The token ID to revoke.
        :return: Success status.
        """
        user = get_current_user()
        if not user:
            return {"success": False, "error": "Not authenticated"}

        success = await self.auth.revoke_token(token_id, user)
        return {"success": success}

    @api_command("auth/user/providers")
    async def get_my_providers(self) -> list[dict[str, Any]]:
        """
        Get current user's linked authentication providers.

        :return: List of provider links.
        """
        user = get_current_user()
        if not user:
            return []

        # Get provider links from database
        rows = await self.auth.database.get_rows("user_auth_providers", {"user_id": user.user_id})
        providers = [UserAuthProvider.from_dict(dict(row)) for row in rows]
        return [p.to_dict() for p in providers]

    @api_command("auth/user/unlink_provider", required_role="admin")
    async def unlink_provider(self, user_id: str, provider_type: str) -> dict[str, Any]:
        """
        Unlink authentication provider from user (admin only).

        :param user_id: The user ID.
        :param provider_type: Provider type to unlink.
        :return: Success status.
        """
        try:
            await self.auth.database.delete(
                "user_auth_providers", {"user_id": user_id, "provider_type": provider_type}
            )
            await self.auth.database.commit()
            return {"success": True}
        except Exception as e:
            self.logger.exception("Error unlinking provider")
            return {"success": False, "error": str(e)}


class WebsocketClientHandler:
    """Handle an active websocket client connection."""

    def __init__(self, webserver: WebserverController, request: web.Request) -> None:
        """Initialize an active connection."""
        self.webserver = webserver
        self.mass = webserver.mass
        self.request = request
        self.wsock = web.WebSocketResponse(heartbeat=55)
        self._to_write: asyncio.Queue[str | None] = asyncio.Queue(maxsize=MAX_PENDING_MSG)
        self._handle_task: asyncio.Task[Any] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._logger = webserver.logger
        self._authenticated_user: Any = None  # Will be set after auth command or from Ingress
        self._is_ingress = "X-Ingress-Path" in request.headers
        # try to dynamically detect the base_url of a client if proxied or behind Ingress
        self.base_url: str | None = None
        if forward_host := request.headers.get("X-Forwarded-Host"):
            ingress_path = request.headers.get("X-Ingress-Path", "")
            forward_proto = request.headers.get("X-Forwarded-Proto", request.protocol)
            self.base_url = f"{forward_proto}://{forward_host}{ingress_path}"

    async def disconnect(self) -> None:
        """Disconnect client."""
        self._cancel()
        if self._writer_task is not None:
            await self._writer_task

    async def handle_client(self) -> web.WebSocketResponse:
        """Handle a websocket response."""
        # ruff: noqa: PLR0915
        request = self.request
        wsock = self.wsock
        try:
            async with asyncio.timeout(10):
                await wsock.prepare(request)
        except TimeoutError:
            self._logger.warning("Timeout preparing request from %s", request.remote)
            return wsock

        self._logger.log(VERBOSE_LOG_LEVEL, "Connection from %s", request.remote)
        self._handle_task = asyncio.current_task()
        self._writer_task = self.mass.create_task(self._writer())

        # send server(version) info when client connects
        await self._send_message(self.mass.get_server_info())

        # forward all events to clients
        def handle_event(event: MassEvent) -> None:
            self._send_message_sync(event)

        unsub_callback = self.mass.subscribe(handle_event)

        disconnect_warn = None

        try:
            while not wsock.closed:
                msg = await wsock.receive()

                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break

                if msg.type != WSMsgType.TEXT:
                    continue

                self._logger.log(VERBOSE_LOG_LEVEL, "Received: %s", msg.data)

                try:
                    command_msg = CommandMessage.from_json(msg.data)
                except ValueError:
                    disconnect_warn = f"Received invalid JSON: {msg.data}"
                    break

                await self._handle_command(command_msg)

        except asyncio.CancelledError:
            self._logger.debug("Connection closed by client")

        except Exception:
            self._logger.exception("Unexpected error inside websocket API")

        finally:
            # Handle connection shutting down.
            unsub_callback()
            self._logger.log(VERBOSE_LOG_LEVEL, "Unsubscribed from events")

            try:
                self._to_write.put_nowait(None)
                # Make sure all error messages are written before closing
                await self._writer_task
                await wsock.close()
            except asyncio.QueueFull:  # can be raised by put_nowait
                self._writer_task.cancel()

            finally:
                if disconnect_warn is None:
                    self._logger.log(VERBOSE_LOG_LEVEL, "Disconnected")
                else:
                    self._logger.warning("Disconnected: %s", disconnect_warn)

        return wsock

    async def _handle_command(self, msg: CommandMessage) -> None:
        """Handle an incoming command from the client."""
        self._logger.debug("Handling command %s", msg.command)

        # Handle special "auth" command
        if msg.command == "auth":
            await self._handle_auth_command(msg)
            return

        # work out handler for the given path/command
        handler = self.mass.command_handlers.get(msg.command)

        if handler is None:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    InvalidCommand.error_code,
                    f"Invalid command: {msg.command}",
                )
            )
            self._logger.warning("Invalid command: %s", msg.command)
            return

        # Check authentication if required (skip for ingress connections)
        if (handler.authenticated or handler.required_role) and not self._is_ingress:
            if self._authenticated_user is None:
                await self._send_message(
                    ErrorResultMessage(
                        msg.message_id,
                        401,
                        "Authentication required. Please send auth command first.",
                    )
                )
                return

            # Set user in context for API methods
            set_current_user(self._authenticated_user)

            # Check role if required
            if handler.required_role == "admin":
                if self._authenticated_user.role != UserRole.ADMIN:
                    await self._send_message(
                        ErrorResultMessage(
                            msg.message_id,
                            403,
                            "Admin access required",
                        )
                    )
                    return

        # schedule task to handle the command
        self.mass.create_task(self._run_handler(handler, msg))

    async def _run_handler(self, handler: APICommandHandler, msg: CommandMessage) -> None:
        try:
            args = parse_arguments(handler.signature, handler.type_hints, msg.args)
            result: Any = handler.target(**args)
            if hasattr(result, "__anext__"):
                # handle async generator (for really large listings)
                items: list[Any] = []
                async for item in result:
                    items.append(item)
                    if len(items) >= 500:
                        await self._send_message(
                            SuccessResultMessage(msg.message_id, items, partial=True)
                        )
                        items = []
                result = items
            elif asyncio.iscoroutine(result):
                result = await result
            await self._send_message(SuccessResultMessage(msg.message_id, result))
        except Exception as err:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.exception("Error handling message: %s", msg)
            else:
                self._logger.error("Error handling message: %s: %s", msg.command, str(err))
            err_msg = str(err) or err.__class__.__name__
            await self._send_message(
                ErrorResultMessage(msg.message_id, getattr(err, "error_code", 999), err_msg)
            )

    async def _writer(self) -> None:
        """Write outgoing messages."""
        # Exceptions if Socket disconnected or cancelled by connection handler
        with suppress(RuntimeError, ConnectionResetError, *CANCELLATION_ERRORS):
            while not self.wsock.closed:
                if (process := await self._to_write.get()) is None:
                    break

                if callable(process):
                    message: str = process()
                else:
                    message = process
                self._logger.log(VERBOSE_LOG_LEVEL, "Writing: %s", message)
                await self.wsock.send_str(message)

    async def _send_message(self, message: MessageType) -> None:
        """Send a message to the client (for large response messages).

        Runs JSON serialization in executor to avoid blocking for large messages.
        Closes connection if the client is not reading the messages.

        Async friendly.
        """
        # Run JSON serialization in executor to avoid blocking for large messages
        loop = asyncio.get_running_loop()
        _message = await loop.run_in_executor(None, message.to_json)

        try:
            self._to_write.put_nowait(_message)
        except asyncio.QueueFull:
            self._logger.error("Client exceeded max pending messages: %s", MAX_PENDING_MSG)

            self._cancel()

    def _send_message_sync(self, message: MessageType) -> None:
        """Send a message from a sync context (for small messages like events).

        Serializes inline without executor overhead since events are typically small.
        """
        _message = message.to_json()

        try:
            self._to_write.put_nowait(_message)
        except asyncio.QueueFull:
            self._logger.error("Client exceeded max pending messages: %s", MAX_PENDING_MSG)

            self._cancel()

    async def _handle_auth_command(self, msg: CommandMessage) -> None:
        """Handle WebSocket authentication command.

        :param msg: The auth command message with access token.
        """
        # Extract token from args
        token = msg.args.get("access_token") if msg.args else None
        if not token:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    400,
                    "access_token required in args",
                )
            )
            return

        # Authenticate with token
        user = await self.webserver.auth.authenticate_with_token(token)
        if not user:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    401,
                    "Invalid or expired token",
                )
            )
            return

        # Store authenticated user
        self._authenticated_user = user
        self._logger.info("WebSocket client authenticated as %s", user.username)

        # Send success response
        await self._send_message(
            SuccessResultMessage(
                msg.message_id,
                {"authenticated": True, "user": user.to_dict()},
            )
        )

    def _cancel(self) -> None:
        """Cancel the connection."""
        if self._handle_task is not None:
            self._handle_task.cancel()
        if self._writer_task is not None:
            self._writer_task.cancel()
