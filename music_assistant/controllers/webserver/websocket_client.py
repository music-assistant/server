"""WebSocket client handler for Music Assistant API."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from concurrent import futures
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from aiohttp import WSMsgType, web
from music_assistant_models.api import (
    CommandMessage,
    ErrorResultMessage,
    MessageType,
    SuccessResultMessage,
)
from music_assistant_models.auth import Scope, User
from music_assistant_models.enums import EventType
from music_assistant_models.errors import (
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidCommand,
    InvalidToken,
    MusicAssistantError,
    ResourceBusyError,
)
from music_assistant_models.event import MassEvent
from music_assistant_models.favorite_update import FavoriteUpdate
from music_assistant_models.media_items import MediaItem, Playlist
from music_assistant_models.media_items.metadata import IMAGE_PROXY_ID_RESOLVER
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER, VERBOSE_LOG_LEVEL
from music_assistant.helpers.api import APICommandHandler, parse_arguments
from music_assistant.helpers.provider_access import access_allows, with_derived_provider_filter
from music_assistant.helpers.throttle_retry import RequestPriority, set_request_priority

from .helpers.auth_middleware import (
    has_scope,
    is_request_from_ingress,
    player_access_filter,
    resolve_command_impersonation,
    resolve_ingress_user,
    set_current_client_id,
    set_current_token,
    set_current_user,
    set_impersonated_user,
    set_sendspin_player_id,
)

if TYPE_CHECKING:
    from music_assistant.controllers.webserver import WebserverController

MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)
MAX_PENDING_IMAGES = 2
IMAGE_SEND_TIMEOUT = 30


@dataclass
class _ImageResponse:
    """Serialized image response and its writer acknowledgement."""

    message: str
    sent: asyncio.Future[None]


class WebsocketClientHandler:
    """Handle an active websocket client connection."""

    def __init__(self, webserver: WebserverController, request: web.Request) -> None:
        """Initialize an active connection."""
        self.webserver = webserver
        self.mass = webserver.mass
        self.request = request
        self.client_id = uuid4().hex
        self.wsock = web.WebSocketResponse(heartbeat=25)
        self._to_write: asyncio.Queue[str | _ImageResponse | None] = asyncio.Queue(
            maxsize=MAX_PENDING_MSG
        )
        self._image_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._handle_task: asyncio.Task[Any] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._logger = webserver.logger
        self._authenticated_user: User | None = (
            None  # Will be set after auth command or from Ingress
        )
        self._current_token: str | None = None  # Will be set after auth command
        self._token_id: str | None = None  # Will be set after auth for tracking revocation
        self._sendspin_player_id: str | None = None  # Set if client is a sendspin web player
        self._sendspin_player_is_private = False  # whether that bound player is a private client
        self._locale: str | None = None  # UI locale declared by the client (auth arg / set_locale)
        self._is_ingress = is_request_from_ingress(request)
        self._events_unsub_callback: Any = None  # Will be set after authentication
        # uris of the personal playlists this client was told are gone
        self._hidden_playlists: set[str] = set()
        # Track WebRTC session ID if this is a WebRTC gateway connection
        self._webrtc_session_id: str | None = request.query.get("webrtc_session_id")
        # try to dynamically detect the base_url of a client if proxied or behind Ingress
        self.base_url: str | None = None
        if forward_host := request.headers.get("X-Forwarded-Host"):
            ingress_path = request.headers.get("X-Ingress-Path", "")
            forward_proto = request.headers.get("X-Forwarded-Proto", request.protocol)
            self.base_url = f"{forward_proto}://{forward_host}{ingress_path}"

    @property
    def token_id(self) -> str | None:
        """Return the id of the auth token this client authenticated with, if any."""
        return self._token_id

    @property
    def authenticated_user(self) -> User | None:
        """Return the user this client authenticated as, if any."""
        return self._authenticated_user

    @property
    def webrtc_session_id(self) -> str | None:
        """Return the id of the WebRTC session this client connected through, if any."""
        return self._webrtc_session_id

    def matches_token(self, token: str) -> bool:
        """
        Return True if this client authenticated with the given access token.

        :param token: The access token to compare against.
        """
        return self._current_token == token

    def bind_sendspin_player(self, player_id: str) -> None:
        """
        Bind a sendspin web player to this connection.

        :param player_id: Id of the sendspin player this connection owns.
        """
        self._sendspin_player_id = player_id
        self._sendspin_player_is_private = False

    async def disconnect(self) -> None:
        """Disconnect client and wait for its writer to finish."""
        self.cancel()
        if self._writer_task is not None:
            await self._writer_task

    def cancel(self) -> None:
        """Cancel the connection, without waiting for its writer to finish."""
        self._cancel_image_tasks()
        if self._handle_task is not None:
            self._handle_task.cancel()
        if self._writer_task is not None:
            self._writer_task.cancel()

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
            await self._send_message(
                ErrorResultMessage(
                    "connection", 503, "Setup required", translation_key="setup_required"
                )
            )
            await wsock.close()
            return wsock

        disconnect_warn = None

        try:
            # For Ingress connections, auto-create/link user and subscribe to events immediately
            # For regular connections (and Ingress without a signed-in user), events will be
            # subscribed after successful authentication
            if self._is_ingress:
                await self._handle_ingress_auth()

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
            self._cancel_image_tasks()
            if self._image_tasks:
                await asyncio.gather(*self._image_tasks, return_exceptions=True)
            if self._events_unsub_callback:
                self._events_unsub_callback()
                self._logger.log(VERBOSE_LOG_LEVEL, "Unsubscribed from events")

            # Unregister from webserver tracking
            self.webserver.unregister_websocket_client(self)

            # Drop any dashboard registrations owned by this connection
            self.mass.dashboard.handle_client_disconnected(self.client_id)

            try:
                self._to_write.put_nowait(None)
                # Image cancellation removes response deadlines, so bound this final flush too.
                async with asyncio.timeout(IMAGE_SEND_TIMEOUT):
                    await asyncio.shield(self._writer_task)
            except asyncio.QueueFull, TimeoutError:
                self._writer_task.cancel()
                with suppress(*CANCELLATION_ERRORS):
                    await self._writer_task
            finally:
                await wsock.close()
                if disconnect_warn is None:
                    self._logger.log(VERBOSE_LOG_LEVEL, "Disconnected")
                else:
                    self._logger.warning("Disconnected: %s", disconnect_warn)

        return wsock

    async def _handle_command(self, msg: CommandMessage) -> None:
        """Handle an incoming command from the client."""
        self._logger.log(VERBOSE_LOG_LEVEL, "Handling command %s", msg.command)

        # Handle special "auth" command
        if msg.command == "auth":
            await self._handle_auth_command(msg)
            return

        # Handle special "translations/set_locale" command (updates connection state)
        if msg.command == "translations/set_locale":
            await self._handle_set_locale_command(msg)
            return

        # work out handler for the given path/command
        handler = self.mass.command_handlers.get(msg.command)

        if handler is None:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    InvalidCommand.error_code,
                    f"Invalid command: {msg.command}",
                    translation_key="invalid_command",
                )
            )
            self._logger.warning("Invalid command: %s", msg.command)
            return

        # Put this connection's identity in context for the API methods. ContextVars live
        # for as long as the connection does, so every command sets all of them: an
        # unauthenticated handler must see this connection's own (possibly absent) user
        # rather than whatever the command before it left behind.
        set_current_client_id(self.client_id)
        set_current_user(self._authenticated_user)
        set_current_token(self._current_token)
        set_sendspin_player_id(self._sendspin_player_id)
        set_request_priority(RequestPriority.NORMAL)

        # Check authentication if required
        if handler.authenticated or handler.required_scope:
            # For Ingress, user should already be set from _handle_ingress_auth
            # For regular connections, user must be set via auth command
            if self._authenticated_user is None:
                await self._send_message(
                    ErrorResultMessage(
                        msg.message_id,
                        AuthenticationRequired.error_code,
                        "Authentication required. Please send auth command first.",
                        translation_key="authentication_required",
                    )
                )
                return

            # Check scope if required
            if handler.required_scope and not has_scope(
                self._authenticated_user, handler.required_scope
            ):
                await self._send_message(
                    ErrorResultMessage(
                        msg.message_id,
                        InsufficientPermissions.error_code,
                        f"This command requires the {handler.required_scope_label} scope",
                        translation_key="insufficient_permissions",
                    )
                )
                return

        if msg.command == "metadata/get_image":
            if self._closing:
                return
            if len(self._image_tasks) >= MAX_PENDING_IMAGES:
                await self._send_message(
                    ErrorResultMessage(
                        msg.message_id,
                        ResourceBusyError.error_code,
                        "Too many pending image requests. Retry after a response is received.",
                        translation_key=ResourceBusyError.translation_key,
                    )
                )
                return
            # Reserve before yielding: admission includes fetching, serialization, queueing,
            # and the actual socket write, not just the metadata handler's semaphore.
            task = self.mass.create_task(self._run_handler(handler, msg))
            self._image_tasks.add(task)
            task.add_done_callback(self._image_tasks.discard)
            return

        # schedule task to handle the command
        self.mass.create_task(self._run_handler(handler, msg))

    def _cancel_image_tasks(self) -> None:
        """Stop admitting images and cancel this connection's image commands."""
        self._closing = True
        for task in self._image_tasks:
            task.cancel()

    async def _run_handler(self, handler: APICommandHandler, msg: CommandMessage) -> None:
        """Run command handler and send response."""
        try:
            # handle the optional impersonation argument for impersonation-enabled commands
            if handler.allow_impersonation and msg.args:
                if impersonation_user := await resolve_command_impersonation(self.mass, msg.args):
                    set_impersonated_user(impersonation_user)
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
            elif inspect.iscoroutine(result):
                result = await result
            await self._send_message(SuccessResultMessage(msg.message_id, result))
        except MusicAssistantError as err:
            # Expected operational errors (player unavailable, queue empty, etc.)
            # Log at warning level since these are normal error responses, not crashes.
            self._logger.warning("%s: %s", msg.command, err)
            err_msg = str(err) or err.__class__.__name__
            # err_msg is the English fallback; the translation_key (per-type default or a
            # provider override) localizes `details` to the connection locale at serialization.
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    err.error_code,
                    err_msg,
                    translation_key=err.translation_key,
                    translation_args=err.translation_args,
                    translation_owner=err.translation_owner,
                )
            )
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
        try:
            with suppress(RuntimeError, ConnectionResetError, *CANCELLATION_ERRORS):
                while not self.wsock.closed:
                    if (process := await self._to_write.get()) is None:
                        break

                    if isinstance(process, _ImageResponse):
                        if process.sent.cancelled():
                            continue
                        message = process.message
                    elif callable(process):
                        message = process()
                    else:
                        message = process
                    self._logger.log(VERBOSE_LOG_LEVEL, "Writing: %s", message)
                    await self.wsock.send_str(message)
                    if isinstance(process, _ImageResponse) and not process.sent.done():
                        process.sent.set_result(None)
        finally:
            self._cancel_image_tasks()
            # Discard queued payloads on writer failure rather than retaining image bytes
            # until the connection object is eventually collected.
            while not self._to_write.empty():
                pending = self._to_write.get_nowait()
                if isinstance(pending, _ImageResponse) and not pending.sent.done():
                    pending.sent.cancel()

    async def _send_message(self, message: MessageType) -> None:
        """
        Send a message to the client (for large response messages).

        Runs JSON serialization in executor to avoid blocking for large messages.
        Closes connection if the client is not reading the messages.

        Async friendly.
        """
        # Run JSON serialization in executor to avoid blocking for large messages.
        # copy_context() propagates the IMAGE_PROXY_ID_RESOLVER and TRANSLATION_RESOLVER
        # ContextVars into the executor thread so that nested models can inject `proxy_id`
        # and localize human-readable fields via their `__post_serialize__` hooks.
        loop = asyncio.get_running_loop()
        token = IMAGE_PROXY_ID_RESOLVER.set(self.mass.metadata.compute_image_id)
        token_loc = TRANSLATION_RESOLVER.set(
            partial(self.mass.translations.get_translation, locale=self._locale)
        )
        try:
            ctx = contextvars.copy_context()
            _message = await loop.run_in_executor(None, ctx.run, message.to_json)
        finally:
            IMAGE_PROXY_ID_RESOLVER.reset(token)
            TRANSLATION_RESOLVER.reset(token_loc)

        image_response = asyncio.current_task() in self._image_tasks
        sent = loop.create_future() if image_response else None
        try:
            self._to_write.put_nowait(_ImageResponse(_message, sent) if sent else _message)
        except asyncio.QueueFull:
            self._logger.error("Client exceeded max pending messages: %s", MAX_PENDING_MSG)
            self.cancel()
            return

        if sent is not None:
            try:
                async with asyncio.timeout(IMAGE_SEND_TIMEOUT):
                    await sent
            except TimeoutError:
                self._logger.warning("Timeout writing image response; disconnecting client")
                self.cancel()
                raise asyncio.CancelledError from None

    def _send_message_sync(self, message: MessageType) -> None:
        """
        Send a message from a sync context (for small messages like events).

        Serializes inline without executor overhead since events are typically small.
        """
        token = IMAGE_PROXY_ID_RESOLVER.set(self.mass.metadata.compute_image_id)
        token_loc = TRANSLATION_RESOLVER.set(
            partial(self.mass.translations.get_translation, locale=self._locale)
        )
        try:
            _message = message.to_json()
        finally:
            IMAGE_PROXY_ID_RESOLVER.reset(token)
            TRANSLATION_RESOLVER.reset(token_loc)

        try:
            self._to_write.put_nowait(_message)
        except asyncio.QueueFull:
            self._logger.error("Client exceeded max pending messages: %s", MAX_PENDING_MSG)

            self.cancel()

    async def _handle_auth_command(self, msg: CommandMessage) -> None:
        """
        Handle WebSocket authentication command.

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
                    translation_key="invalid_token",
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

        # Optionally store the UI locale declared with the auth command and warm it up
        if msg.args and (locale := msg.args.get("locale")):
            self._locale = locale
            await self.mass.translations.ensure_locale_loaded(locale)

        # Send success response
        await self._send_message(
            SuccessResultMessage(
                msg.message_id,
                {
                    "authenticated": True,
                    "user": with_derived_provider_filter(self.mass, user).to_dict(),
                },
            )
        )

        # Subscribe to events after successful authentication
        self._subscribe_to_events()

        # Register with webserver for tracking
        self.webserver.register_websocket_client(self)

    async def _handle_set_locale_command(self, msg: CommandMessage) -> None:
        """
        Handle the WebSocket set_locale command (updates the connection's UI locale).

        :param msg: The set_locale command message; expects a "locale" arg.
        """
        locale = msg.args.get("locale") if msg.args else None
        if not locale:
            await self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    InvalidCommand.error_code,
                    "locale required in args",
                )
            )
            return
        self._locale = locale
        await self.mass.translations.ensure_locale_loaded(locale)
        await self._send_message(SuccessResultMessage(msg.message_id, {"locale": locale}))

    async def _handle_ingress_auth(self) -> None:
        """Handle authentication for Ingress connections (auto-create/link user, subscribe)."""
        if user := await resolve_ingress_user(self.mass, self.request.headers):
            self._authenticated_user = user
            self._logger.debug("Ingress user authenticated: %s", user.username)
            self._subscribe_to_events()
        else:
            # No (enabled) HA user - allow homeassistant system user to connect with token
            # This allows the Home Assistant integration to connect via the internal network
            # The token authentication happens in _handle_auth_command
            self._logger.debug("Ingress connection without a signed-in user, expecting token auth")

    def _is_own_private_player(self, object_id: str | None) -> bool:
        """
        Return whether the object is the private client player this connection announced.

        Binding can happen before the sendspin player registers, so the private status is
        latched the first time an event for the bound id arrives while the player exists,
        and kept afterwards so the owner still receives its player's removal event. A
        shared speaker announced as the client id never latches, so it stays filtered.

        :param object_id: The event's object id (a player or queue id), or None.
        """
        if object_id is None or object_id != self._sendspin_player_id:
            return False
        if not self._sendspin_player_is_private:
            player = self.mass.players.get_player(object_id)
            self._sendspin_player_is_private = player is not None and player.private
        return self._sendspin_player_is_private

    def _subscribe_to_events(self) -> None:
        """Subscribe to Mass events and forward them to the client."""
        if self._events_unsub_callback is not None:
            # Already subscribed
            return

        def handle_event(event: MassEvent) -> None:
            # Latch the bound player's private status on every event, before applying the
            # filter: the user may be unrestricted now and restricted later, and the flag
            # must already be set so the owner still receives the player's removal event.
            own_private_player = self._is_own_private_player(event.object_id)
            # filter events for objects the user has no access to
            player_filter = player_access_filter(self._authenticated_user)
            if (
                player_filter is not None
                and event.event
                in (
                    EventType.PLAYER_ADDED,
                    EventType.PLAYER_REMOVED,
                    EventType.PLAYER_UPDATED,
                    EventType.PLAYER_SLEEP_TIMER_UPDATED,
                    EventType.QUEUE_ADDED,
                    EventType.QUEUE_ITEMS_UPDATED,
                    EventType.QUEUE_TIME_UPDATED,
                    EventType.QUEUE_UPDATED,
                )
                and event.object_id
                and event.object_id not in player_filter
                # the private client player this connection announced is always allowed
                and not own_private_player
            ):
                return

            if event.event == EventType.SETUP_FLOW_UPDATED:
                # setup flow steps carry prefilled values, OAuth urls and the
                # flow_id guarding the unauthenticated callback route - only
                # users who could interact with the flow may receive them
                user = self._authenticated_user
                if user is None:
                    return
                access = (
                    self.mass.config.get_setup_flow_access(event.object_id)
                    if event.object_id
                    else None
                )
                if access is None:
                    # flow already popped (terminal step race): the flow kind is no
                    # longer known, so require both config scopes to be safe
                    if not has_scope(user, Scope.CONFIG_PROVIDERS_WRITE) or not has_scope(
                        user, Scope.CONFIG_PLAYERS_WRITE
                    ):
                        return
                elif not access.allows(user):
                    return

            if isinstance(event.data, Playlist) and not self._forward_playlist_event(
                event, event.data
            ):
                return

            if isinstance(event.data, FavoriteUpdate) and (
                self._authenticated_user is None
                or event.data.user_id != self._authenticated_user.user_id
            ):
                # a like or dislike is the business of its own user only
                return

            if isinstance(event.data, MediaItem) and event.data.favorite is not None:
                # a library item carries the favorite state of the user that touched it; every
                # client keeps its own and learns of changes through the favorite event
                event = MassEvent(
                    event=event.event,
                    object_id=event.object_id,
                    data=replace(event.data, favorite=None),
                )

            if event.event == EventType.TASKS_UPDATED:
                if self._authenticated_user is None:
                    return
                task_data = self.mass.tasks.list_tasks_for_user(self._authenticated_user)
                self._send_message_sync(
                    MassEvent(
                        event=event.event,
                        object_id=event.object_id,
                        data=task_data,
                    )
                )
                return

            if event.event == EventType.PROVIDERS_UPDATED:
                # the payload is signalled unfiltered, so narrow it down to the
                # music sources this client's user may see
                if self._authenticated_user is None:
                    return
                provider_data = self.mass.get_providers_for_user(self._authenticated_user)
                self._send_message_sync(
                    MassEvent(
                        event=event.event,
                        object_id=event.object_id,
                        data=provider_data,
                    )
                )
                return

            self._send_message_sync(event)

        self._events_unsub_callback = self.mass.subscribe(handle_event)
        self._logger.debug("Subscribed to events")

    def _forward_playlist_event(self, event: MassEvent, playlist: Playlist) -> bool:
        """
        Return whether an event about a playlist may reach this client as it was signalled.

        A personal playlist is only announced to the users who may see it. A client whose
        user may no longer see it is instead told once that the playlist is gone, so it
        drops the row it may still hold.

        :param event: The event about the playlist.
        :param playlist: The playlist the event carries.
        """
        uri = event.object_id
        if playlist.access is None or access_allows(playlist.access, self._authenticated_user):
            if uri:
                self._hidden_playlists.discard(uri)
            return True
        if not uri or uri in self._hidden_playlists:
            return False
        self._hidden_playlists.add(uri)
        # only an update can take a playlist away from a client that still holds it; one
        # created or removed out of sight was never held, nor is anything held before login
        if event.event == EventType.MEDIA_ITEM_UPDATED and self._authenticated_user:
            self._send_message_sync(MassEvent(event=EventType.MEDIA_ITEM_DELETED, object_id=uri))
        return False
