"""Unix socket server for Snapcast control script communication.

This module provides a secure communication channel between the Snapcast control script
and Music Assistant, avoiding the need to expose the WebSocket API to the control script.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

LOOP_STATUS_MAP = {
    "all": "playlist",
    "one": "track",
    "off": "none",
}
LOOP_STATUS_MAP_REVERSE = {v: k for k, v in LOOP_STATUS_MAP.items()}


class SnapcastSocketServer:
    """Unix socket server for a single Snapcast control script connection.

    Each stream gets its own socket server instance to handle control script communication.
    The socket provides a secure IPC channel that doesn't require authentication since
    only local processes can connect.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        queue_id: str,
        socket_path: str,
        streamserver_ip: str,
        streamserver_port: int,
    ) -> None:
        """Initialize the socket server.

        :param mass: The MusicAssistant instance.
        :param queue_id: The queue ID this socket serves.
        :param socket_path: Path to the Unix socket file.
        :param streamserver_ip: IP address of the stream server (for image proxy).
        :param streamserver_port: Port of the stream server (for image proxy).
        """
        self.mass = mass
        self.queue_id = queue_id
        self.socket_path = socket_path
        self.streamserver_ip = streamserver_ip
        self.streamserver_port = streamserver_port
        self._server: asyncio.AbstractServer | None = None
        self._client_writer: asyncio.StreamWriter | None = None
        self._unsub_callback: Any = None
        self._logger = LOGGER.getChild(queue_id)

    async def start(self) -> None:
        """Start the Unix socket server."""
        # Ensure the socket file doesn't exist
        socket_path = Path(self.socket_path)
        socket_path.unlink(missing_ok=True)

        # Create the socket server
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        # Set permissions so only the current user can access
        Path(self.socket_path).chmod(0o600)
        self._logger.debug("Started Unix socket server at %s", self.socket_path)

        # Subscribe to queue events
        self._unsub_callback = self.mass.subscribe(
            self._handle_mass_event,
            (EventType.QUEUE_UPDATED,),
            self.queue_id,
        )

    async def stop(self) -> None:
        """Stop the Unix socket server."""
        if self._unsub_callback:
            self._unsub_callback()
            self._unsub_callback = None

        if self._client_writer:
            self._client_writer.close()
            with suppress(Exception):
                await self._client_writer.wait_closed()
            self._client_writer = None

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Clean up socket file
        Path(self.socket_path).unlink(missing_ok=True)
        self._logger.debug("Stopped Unix socket server")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a control script connection."""
        self._logger.debug("Control script connected")
        self._client_writer = writer

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    message = json.loads(line.decode().strip())
                    await self._handle_message(message)
                except json.JSONDecodeError as err:
                    self._logger.warning("Invalid JSON from control script: %s", err)
                except Exception as err:
                    self._logger.exception("Error handling control script message: %s", err)
        except asyncio.CancelledError:
            pass
        except ConnectionResetError:
            self._logger.debug("Control script connection reset")
        finally:
            self._client_writer = None
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._logger.debug("Control script disconnected")

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle a message from the control script.

        :param message: The JSON message from the control script.
        """
        msg_id = message.get("message_id")
        command = message.get("command")
        args = message.get("args", {})

        if not command:
            await self._send_error(msg_id, "Missing command")
            return

        try:
            result = await self._execute_command(command, args)
            await self._send_result(msg_id, result)
        except Exception as err:
            self._logger.exception("Error executing command %s: %s", command, err)
            await self._send_error(msg_id, str(err))

    async def _execute_command(self, command: str, args: dict[str, Any]) -> Any:
        """Execute a Music Assistant API command.

        :param command: The API command to execute.
        :param args: The arguments for the command.
        :return: The result of the command.
        """
        handler = self.mass.command_handlers.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")

        # Execute the handler
        result = handler.target(**args)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def _send_result(self, msg_id: str | None, result: Any) -> None:
        """Send a success result to the control script.

        :param msg_id: The message ID from the request.
        :param result: The result data.
        """
        if not self._client_writer:
            return

        response: dict[str, Any] = {"message_id": msg_id}
        if result is not None:
            # Convert result to dict if it has to_dict method
            if hasattr(result, "to_dict"):
                response["result"] = result.to_dict()
            else:
                response["result"] = result

        await self._send_message(response)

    async def _send_error(self, msg_id: str | None, error: str) -> None:
        """Send an error result to the control script.

        :param msg_id: The message ID from the request.
        :param error: The error message.
        """
        if not self._client_writer:
            return

        response = {
            "message_id": msg_id,
            "error": error,
        }
        await self._send_message(response)

    async def _send_message(self, message: dict[str, Any]) -> None:
        """Send a message to the control script.

        :param message: The message to send.
        """
        if not self._client_writer:
            return

        try:
            data = json.dumps(message) + "\n"
            self._client_writer.write(data.encode())
            await self._client_writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self._logger.debug("Failed to send message - connection closed")
            self._client_writer = None

    def _handle_mass_event(self, event: Any) -> None:
        """Handle Music Assistant events and forward to control script.

        :param event: The Music Assistant event.
        """
        if not self._client_writer:
            return

        # Forward queue_updated events
        if event.event == EventType.QUEUE_UPDATED and event.object_id == self.queue_id:
            event_msg = {
                "event": "queue_updated",
                "object_id": event.object_id,
                "data": event.data.to_dict() if hasattr(event.data, "to_dict") else event.data,
            }
            # Schedule the send in the event loop
            asyncio.create_task(self._send_message(event_msg))
