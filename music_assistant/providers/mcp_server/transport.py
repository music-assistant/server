"""Stateless streamable HTTP transport of the builtin MCP server: one JSON-RPC message per POST."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from music_assistant_models.errors import MusicAssistantError
from yarl import URL

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_current_user,
    has_scope,
    set_current_peer_address,
)
from music_assistant.helpers.api import redact_json_secrets
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.throttle_retry import RequestPriority, set_request_priority

from .auth import (
    authenticate_request,
    impersonate_home_assistant_user,
    is_request_from_home_assistant,
)
from .registry import TOOLS, ToolError
from .tools import INSTRUCTIONS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .registry import MCPTool

MCP_PATH: Final = "/mcp"
SUPPORTED_PROTOCOL_VERSIONS: Final = ("2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL_VERSION: Final = SUPPORTED_PROTOCOL_VERSIONS[-1]
# the protocol version a client means when it sends no MCP-Protocol-Version header
DEFAULT_PROTOCOL_VERSION: Final = SUPPORTED_PROTOCOL_VERSIONS[0]
# the first protocol version with structured tool results
STRUCTURED_CONTENT_VERSION: Final = "2025-06-18"

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.mcp_server")


class MCPTransport:
    """Handle the HTTP requests to the MCP endpoint."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the transport.

        :param mass: The Music Assistant instance.
        """
        self.mass = mass

    async def handle_request(self, request: web.Request) -> web.Response:
        """
        Handle a request to the public MCP endpoint, which needs a bearer token.

        :param request: The aiohttp request.
        """
        return await self._handle(request, from_home_assistant=False)

    async def handle_home_assistant_request(self, request: web.Request) -> web.Response:
        """
        Handle a request to the private MCP endpoint that only Home Assistant knows.

        Home Assistant on the host reaches it without a token; any other caller needs one.

        :param request: The aiohttp request.
        """
        return await self._handle(request, await is_request_from_home_assistant(request))

    async def _handle(self, request: web.Request, from_home_assistant: bool) -> web.Response:
        """Handle a request to one of the MCP endpoints."""
        set_current_peer_address(request.remote)
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        if refusal := await self._refuse_request(request, from_home_assistant):
            return refusal
        protocol_version = request.headers.get("MCP-Protocol-Version", DEFAULT_PROTOCOL_VERSION)
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return _error_response(
                None, INVALID_REQUEST, f"Unsupported protocol version {protocol_version}", 400
            )
        set_request_priority(RequestPriority.NORMAL)
        body = await request.read()
        if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL):
            LOGGER.log(VERBOSE_LOG_LEVEL, "Received on MCP: %s", redact_json_secrets(body))
        try:
            message = json_loads(body)
        except ValueError:
            return _error_response(None, PARSE_ERROR, "Parse error", 400)
        if not isinstance(message, dict):
            return _error_response(None, INVALID_REQUEST, "Expected a single JSON-RPC message", 400)
        if "method" not in message or "id" not in message:
            # a notification (such as notifications/initialized) or a response, which
            # needs no answer
            return web.Response(status=202)
        return await self._handle_request_message(message, protocol_version)

    async def _refuse_request(
        self, request: web.Request, from_home_assistant: bool
    ) -> web.Response | None:
        """Authenticate the request and return the response refusing it, None to accept it."""
        if not from_home_assistant and not self._origin_allowed(request.headers.get("Origin")):
            return web.Response(status=403, text="Origin not allowed")
        if not self.mass.webserver.auth.has_users:
            return web.Response(status=503, text="Setup required")
        if await authenticate_request(self.mass, request, from_home_assistant) is None:
            return web.Response(
                status=401,
                text="Authentication required",
                headers={"WWW-Authenticate": 'Bearer realm="Music Assistant"'},
            )
        if request.content_type != "application/json":
            return web.Response(status=415, text="Content-Type must be application/json")
        if not _accepts_json(request.headers.get("Accept", "")):
            return web.Response(status=406, text="Accept must include application/json")
        return None

    async def _handle_request_message(
        self, message: dict[str, Any], protocol_version: str
    ) -> web.Response:
        """Handle a JSON-RPC request message and return the response to it."""
        message_id = message["id"]
        method = message["method"]
        params = message.get("params") or {}
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error_response(message_id, INVALID_REQUEST, "Invalid request")
        if not isinstance(params, dict):
            return _error_response(message_id, INVALID_PARAMS, "Params must be an object")
        match method:
            case "initialize":
                return _result_response(message_id, self._initialize(params))
            case "ping":
                return _result_response(message_id, {})
            case "tools/list":
                return _result_response(
                    message_id, {"tools": [tool.to_dict() for tool in TOOLS.values()]}
                )
            case "tools/call":
                return await self._call_tool(message_id, params, protocol_version)
        return _error_response(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return the result of the initialize request."""
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": (
                requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Music Assistant", "version": self.mass.version},
            "instructions": INSTRUCTIONS,
        }

    async def _call_tool(
        self, message_id: Any, params: dict[str, Any], protocol_version: str
    ) -> web.Response:
        """Run the tool of a tools/call request and return its result."""
        name = params.get("name")
        tool = TOOLS.get(name) if isinstance(name, str) else None
        arguments = params.get("arguments") or {}
        if tool is None:
            return _error_response(message_id, INVALID_PARAMS, f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            return _error_response(message_id, INVALID_PARAMS, "Arguments must be an object")
        try:
            await impersonate_home_assistant_user(self.mass, params)
            self._check_scopes(tool)
            result = await tool.call(self.mass, arguments)
        except (ToolError, MusicAssistantError) as err:
            return _tool_error_response(message_id, str(err) or type(err).__name__)
        except Exception:
            LOGGER.exception("Error running MCP tool %s", tool.name)
            return _tool_error_response(message_id, "Internal error")
        tool_result: dict[str, Any] = {
            "content": [{"type": "text", "text": json_dumps(result)}],
            "isError": False,
        }
        if protocol_version >= STRUCTURED_CONTENT_VERSION:
            tool_result["structuredContent"] = result
        return _result_response(message_id, tool_result)

    def _check_scopes(self, tool: MCPTool) -> None:
        """Raise when the authenticated or the impersonated user may not use the tool."""
        for user in (current_user.get(), get_current_user()):
            if user is None or not all(has_scope(user, scope) for scope in tool.scopes):
                scopes = ", ".join(tool.scopes)
                raise ToolError(f"Not allowed: the {tool.name} tool requires the {scopes} scope")

    def _origin_allowed(self, origin: str | None) -> bool:
        """Return whether a request with the given Origin header may use the endpoint."""
        if origin is None:
            return True
        webserver = self.mass.webserver
        scheme = URL(webserver.base_url).scheme or "http"
        allowed = {
            URL.build(scheme=scheme, host=host, port=webserver.publish_port)
            for host in ("localhost", "127.0.0.1", "::1")
        }
        allowed.update(
            URL(url) for url in (webserver.base_url, webserver.external_url) if url is not None
        )
        try:
            return URL(origin).origin() in {url.origin() for url in allowed if url.absolute}
        except ValueError:
            return False


def _accepts_json(accept: str) -> bool:
    """Return whether an Accept header includes application/json."""
    return any(part.split(";")[0].strip() == "application/json" for part in accept.split(","))


def _result_response(message_id: Any, result: dict[str, Any]) -> web.Response:
    """Return a JSON-RPC response with the given result."""
    return _json_response({"jsonrpc": "2.0", "id": message_id, "result": result})


def _tool_error_response(message_id: Any, error: str) -> web.Response:
    """Return a JSON-RPC response with a tool call that failed, for the model to read."""
    return _result_response(
        message_id, {"content": [{"type": "text", "text": error}], "isError": True}
    )


def _error_response(message_id: Any, code: int, message: str, status: int = 200) -> web.Response:
    """Return a JSON-RPC error response."""
    return _json_response(
        {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}},
        status,
    )


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    """Return a JSON response."""
    return web.Response(status=status, text=json_dumps(data), content_type="application/json")
