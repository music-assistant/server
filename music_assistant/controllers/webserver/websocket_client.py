"""WebSocket client handler for Music Assistant API."""

from __future__ import annotations

import asyncio
import logging
from concurrent import futures
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final

from aiohttp import WSMsgType, web
from music_assistant_models.api import (
    CommandMessage,
    ErrorResultMessage,
    MessageType,
    SuccessResultMessage,
)
from music_assistant_models.auth import AuthProviderType, User, UserRole
from music_assistant_models.enums import EventType
from music_assistant_models.errors import (
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidCommand,
    InvalidToken,
)

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER, VERBOSE_LOG_LEVEL
from music_assistant.helpers.api import APICommandHandler, parse_arguments

from .helpers.auth_middleware import is_request_from_ingress, set_current_token, set_current_user
from .helpers.auth_providers import get_ha_user_details, get_ha_user_role

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.controllers.webserver import WebserverController

MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)


class WebsocketClientHandler:
    """Handle an active websocket client connection."""

    def __init__(self, webserver: WebserverController, request: web.Request) -> None:
        """Initialize an active connection."""
        self.webserver = webserver
        self.mass = webserver.mass
        self.request = request
        self.wsock = web.WebSocketResponse(heartbeat=30)
        self._to_write: asyncio.Queue[str | None] = asyncio.Queue(maxsize=MAX_PENDING_MSG)
        self._handle_task: asyncio.Task[Any] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._logger = webserver.logger
        self._authenticated_user: User | None = (
            None  # Will be set after auth command or from Ingress
        )
        self._current_token: str | None = None  # Will be set after auth command
        self._token_id: str | None = None  # Will be set after auth for tracking revocation
        self._is_ingress = is_request_from_ingress(request)
        self._events_unsub_callback: Any = None  # Will be set after authentication
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
        server_info = self.mass.get_server_info()
        await self._send_message(server_info)

        # Block until onboarding is complete
        if not self.webserver.auth.has_users and not self._is_ingress:
            await self._send_message(ErrorResultMessage("connection", 503, "Setup required"))
            await wsock.close()
            return wsock

        # For Ingress connections, auto-create/link user and subscribe to events immediately
        # For regular connections, events will be subscribed after successful authentication
        if self._is_ingress:
            await self._handle_ingress_auth()
            self._subscribe_to_events()

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
            if self._events_unsub_callback:
                self._events_unsub_callback()
                self._logger.log(VERBOSE_LOG_LEVEL, "Unsubscribed from events")

            # Unregister from webserver tracking
            self.webserver.unregister_websocket_client(self)

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

        # Check authentication if required
        if handler.authenticated or handler.required_role:
            # For Ingress, user should already be set from _handle_ingress_auth
            # For regular connections, user must be set via auth command
            if self._authenticated_user is None:
                await self._send_message(
                    ErrorResultMessage(
                        msg.message_id,
                        AuthenticationRequired.error_code,
                        "Authentication required. Please send auth command first.",
                    )
                )
                return

            # Set user and token in context for API methods
            set_current_user(self._authenticated_user)
            set_current_token(self._current_token)

            # Check role if required
            if handler.required_role == "admin":
                if self._authenticated_user.role != UserRole.ADMIN:
                    await self._send_message(
                        ErrorResultMessage(
                            msg.message_id,
                            InsufficientPermissions.error_code,
                            "Admin access required",
                        )
                    )
                    return

        # schedule task to handle the command
        self.mass.create_task(self._run_handler(handler, msg))

    async def _run_handler(self, handler: APICommandHandler, msg: CommandMessage) -> None:
        """Run command handler and send response."""
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
        # Extract token from args (support both 'token' and 'access_token' for backward compat)
        token = msg.args.get("token") if msg.args else None
        if not token:
            token = msg.args.get("access_token") if msg.args else None
        if not token:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    AuthenticationRequired.error_code,
                    "token required in args",
                )
            )
            return

        # Authenticate with token
        user = await self.webserver.auth.authenticate_with_token(token)
        if not user:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    InvalidToken.error_code,
                    "Invalid or expired token",
                )
            )
            return

        # Security: Deny homeassistant system user on regular (non-Ingress) webserver
        if not self._is_ingress and user.username == HOMEASSISTANT_SYSTEM_USER:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    InvalidToken.error_code,
                    "Home Assistant system user not allowed on regular webserver",
                )
            )
            return

        # Get token_id for tracking revocation events
        token_id = await self.webserver.auth.get_token_id_from_token(token)

        # Store authenticated user, token, and token_id
        self._authenticated_user = user
        self._current_token = token
        self._token_id = token_id
        self._logger.info("WebSocket client authenticated as %s", user.username)

        # Send success response
        await self._send_message(
            SuccessResultMessage(
                msg.message_id,
                {"authenticated": True, "user": user.to_dict()},
            )
        )

        # Subscribe to events after successful authentication
        self._subscribe_to_events()

        # Register with webserver for tracking
        self.webserver.register_websocket_client(self)

    async def _handle_ingress_auth(self) -> None:
        """Handle authentication for Ingress connections (auto-create/link user)."""
        ingress_user_id = self.request.headers.get("X-Remote-User-ID")
        ingress_username = self.request.headers.get("X-Remote-User-Name")
        ingress_display_name = self.request.headers.get("X-Remote-User-Display-Name")

        if ingress_user_id and ingress_username:
            # Try to find existing user linked to this HA user ID
            user = await self.webserver.auth.get_user_by_provider_link(
                AuthProviderType.HOME_ASSISTANT, ingress_user_id
            )

            if not user:
                # Check if a user with this username already exists
                user = await self.webserver.auth.get_user_by_username(ingress_username)

                if not user:
                    # New user - fetch details from HA
                    ha_username, ha_display_name, avatar_url = await get_ha_user_details(
                        self.mass, ingress_user_id
                    )
                    # Auto-create user for Ingress (they're already authenticated by HA)
                    role = await get_ha_user_role(self.mass, ingress_user_id)
                    user = await self.webserver.auth.create_user(
                        username=ha_username or ingress_username,
                        role=role,
                        display_name=ha_display_name or ingress_display_name,
                        avatar_url=avatar_url,
                    )

                # Link to Home Assistant provider (or create the link if user already existed)
                await self.webserver.auth.link_user_to_provider(
                    user, AuthProviderType.HOME_ASSISTANT, ingress_user_id
                )

            # Update user with HA details if available (HA is source of truth)
            # Fall back to ingress headers if API lookup doesn't return values
            _, ha_display_name, avatar_url = await get_ha_user_details(self.mass, ingress_user_id)
            final_display_name = ha_display_name or ingress_display_name
            if final_display_name or avatar_url:
                user = await self.webserver.auth.update_user(
                    user,
                    display_name=final_display_name,
                    avatar_url=avatar_url,
                )

            self._authenticated_user = user
            self._logger.debug("Ingress user authenticated: %s", user.username)
        else:
            # No HA user headers - allow homeassistant system user to connect with token
            # This allows the Home Assistant integration to connect via the internal network
            # The token authentication happens in _handle_auth_message
            self._logger.debug("Ingress connection without user headers, expecting token auth")

    def _subscribe_to_events(self) -> None:
        """Subscribe to Mass events and forward them to the client."""
        if self._events_unsub_callback is not None:
            # Already subscribed
            return

        def handle_event(event: MassEvent) -> None:
            # filter events for objects the user has no access to
            if (
                self._authenticated_user
                and self._authenticated_user.player_filter
                and event.event
                in (
                    EventType.PLAYER_ADDED,
                    EventType.PLAYER_REMOVED,
                    EventType.PLAYER_UPDATED,
                    EventType.QUEUE_ADDED,
                    EventType.QUEUE_ITEMS_UPDATED,
                    EventType.QUEUE_TIME_UPDATED,
                    EventType.QUEUE_UPDATED,
                )
                and event.object_id
                and event.object_id not in self._authenticated_user.player_filter
            ):
                return

            self._send_message_sync(event)

        self._events_unsub_callback = self.mass.subscribe(handle_event)
        self._logger.debug("Subscribed to events")

    def _cancel(self) -> None:
        """Cancel the connection."""
        if self._handle_task is not None:
            self._handle_task.cancel()
        if self._writer_task is not None:
            self._writer_task.cancel()
