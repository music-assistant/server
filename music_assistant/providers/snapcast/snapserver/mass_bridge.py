#!/usr/bin/env python3
"""Standalone Snapcast bridge for Music Assistant."""

# This file is part of the snapcast ecosystem (https://github.com/badaix/snapcast).
#
# Author            : ThaGhostNL <github.com/ThaGhostNL/snapserver/plugin/>
# Maintainer        : ThaGhostNL
# Provides          : Bridge between Snapcast and Music Assistant for playback
#                     control and status updates
# Version           : 1.2.0
# Short-Description : Music Assistant control script for Snapcast plugin
# Description       : This script bridges the Snapcast plugin and Music Assistant.
#                     It listens for JSON-RPC requests on stdin, forwards control
#                     commands over the Music Assistant WebSocket API, and sends
#                     updated playback properties back to Snapcast.
# Usage             : This script is intended to be run as part of the Snapcast
#                     plugin setup and must be placed
#                     inside the external SnapServer plugin directory.
#                     For a single server setup, use the user configuration
#                     parameters within this script.
#                     For multiple server setups, use the command line arguments.
# Extra Info        : This variant does not rely on the local Unix socket bridge
#                     used by the built-in Snapserver integration. Instead it
#                     talks to the Music Assistant WebSocket API and resolves the
#                     active queue for a Snapcast stream through a provider-side
#                     API command.
# CmdLine options   : Run `mass_bridge.py --help` to see all available options.

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypedDict, cast

import websocket

__version__ = "1.2.0"
__git_version__ = ""

# To get a token, create a new API token in Music Assistant
# ("/settings/profile") and copy it, then either paste it here, read it from a
# file, or set it via the environment variables MASS_ACCESS_TOKEN or
# MASS_ACCESS_TOKEN_FILE. It is recommended to use a token file or environment
# variable to avoid accidentally committing a token to version control.
MassToken = ""
MassTokenFile = ""

# User-configurable defaults for standalone/external Snapserver setups.
# CLI arguments override these values, and MASS_ACCESS_TOKEN can also supply
# the Music Assistant access token if this field is left empty.
RuntimeParams = TypedDict(
    "RuntimeParams",
    {
        "progname": str,
        "snapcast-host": str,
        "snapcast-port": int,
        "ma-websocket-ip": str,
        "ma-websocket-port": int,
        "ma-access-token": str,
        "ma-access-token-file": str,
        "stream": str | None,
    },
)

params: dict[str, str | int | None] = {
    "progname": sys.argv[0],
    "snapcast-host": None,
    "snapcast-port": None,
    "ma-websocket-ip": None,
    "ma-websocket-port": None,
    "ma-access-token": MassToken,
    "ma-access-token-file": MassTokenFile,
    "stream": None,
}

LOOP_STATUS_MAP = {
    "all": "playlist",
    "one": "track",
    "off": "none",
}
LOOP_STATUS_MAP_REVERSE = {v: k for k, v in LOOP_STATUS_MAP.items()}

MessageCallback = Callable[[dict[str, Any] | None], None]


def _read_token_file(token_file_path: str | None) -> str:
    """Read a Music Assistant access token from a text file."""
    if not token_file_path:
        return ""

    try:
        with open(token_file_path, encoding="utf-8") as file_handle:
            return file_handle.read().strip()
    except OSError as err:
        logger.warning("Unable to read Music Assistant token file '%s': %s", token_file_path, err)
        return ""


def _resolve_runtime_params(
    argv: list[str], environ: dict[str, str] | None = None
) -> RuntimeParams:
    """Resolve effective runtime parameters from defaults, env vars, and CLI args."""
    runtime_params: dict[str, str | int | None] = {
        "progname": params["progname"],
        "snapcast-host": params["snapcast-host"],
        "snapcast-port": params["snapcast-port"],
        "ma-websocket-ip": params["ma-websocket-ip"],
        "ma-websocket-port": params["ma-websocket-port"],
        "ma-access-token": params["ma-access-token"],
        "ma-access-token-file": params["ma-access-token-file"],
        "stream": params["stream"],
    }
    runtime_params["progname"] = argv[0] if argv else runtime_params["progname"]
    cli_overrides: dict[str, str] = {}

    for arg in argv[1:]:
        if not arg.startswith("--") or "=" not in arg:
            continue
        key, value = arg[2:].split("=", 1)
        if key in runtime_params:
            cli_overrides[key] = value
            runtime_params[key] = value

    env = environ or os.environ
    snapcast_host = str(runtime_params["snapcast-host"] or "127.0.0.1")
    snapcast_port = int(runtime_params["snapcast-port"] or 1780)
    ma_websocket_ip = str(runtime_params["ma-websocket-ip"] or "127.0.0.1")
    ma_websocket_port = int(runtime_params["ma-websocket-port"] or 8095)

    token = ""
    token_file = ""
    if cli_overrides.get("ma-access-token"):
        token = cli_overrides["ma-access-token"]
    elif cli_overrides.get("ma-access-token-file"):
        token_file = cli_overrides["ma-access-token-file"]
    elif runtime_params["ma-access-token"]:
        token = str(runtime_params["ma-access-token"])
    elif runtime_params["ma-access-token-file"]:
        token_file = str(runtime_params["ma-access-token-file"])
    elif env.get("MASS_ACCESS_TOKEN"):
        token = env["MASS_ACCESS_TOKEN"]
    else:
        token_file = env.get("MASS_ACCESS_TOKEN_FILE", "")

    return {
        "progname": str(runtime_params["progname"]),
        "snapcast-host": snapcast_host,
        "snapcast-port": snapcast_port,
        "ma-websocket-ip": ma_websocket_ip,
        "ma-websocket-port": ma_websocket_port,
        "ma-access-token": token or _read_token_file(token_file),
        "ma-access-token-file": token_file,
        "stream": cast("str | None", runtime_params["stream"]),
    }


def send(json_msg: dict[str, Any]) -> None:
    """Send a JSON message to stdout."""
    sys.stdout.write(json.dumps(json_msg))
    sys.stdout.write("\n")
    sys.stdout.flush()


def send_error(message_id: str, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    send(
        {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": message_id,
        }
    )


class MusicAssistantControl:
    """Bridge between Snapcast JSON-RPC and the Music Assistant API."""

    def __init__(
        self,
        stream: str,
        snapcast_host: str,
        snapcast_port: int,
        ma_websocket_ip: str,
        ma_websocket_port: int,
        ma_access_token: str,
    ) -> None:
        """Initialize the bridge."""
        self.stream = stream
        self.snapcast_host = snapcast_host
        self.snapcast_port = snapcast_port
        self.ma_websocket_ip = ma_websocket_ip
        self.ma_websocket_port = ma_websocket_port
        self.ma_access_token = ma_access_token

        self._metadata: dict[str, Any] = {}
        self._properties: dict[str, Any] = self._default_properties()
        self._request_callbacks: dict[str, MessageCallback] = {}
        self._ws: Any = None
        self._authenticated = False
        self._stopped = False
        self._shutdown_event = threading.Event()
        self._send_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._resolve_retry_lock = threading.Lock()
        self._current_queue_id: str | None = None
        self._current_player_id: str | None = None
        self._resolve_retry_timer: threading.Timer | None = None
        self._ws_thread = threading.Thread(target=self._ws_loop, name="massBridgeWS")
        self._ws_thread.daemon = True
        self._ws_thread.start()

    def _default_properties(self) -> dict[str, Any]:
        """Return the default unresolved Snapcast property set."""
        return {
            "canGoNext": False,
            "canGoPrevious": False,
            "canPlay": False,
            "canPause": False,
            "canSeek": False,
            "canControl": False,
            "playbackStatus": "stopped",
            "loopStatus": "none",
            "shuffle": False,
            "volume": 0,
            "mute": False,
            "rate": 1.0,
            "position": 0.0,
            "metadata": {},
        }

    def _create_properties(self, mass_queue_details: dict[str, Any]) -> dict[str, Any]:
        """Create Snapcast properties from Music Assistant queue details."""
        current_queue_item: dict[str, Any] | None = mass_queue_details.get("current_item")
        next_queue_item: dict[str, Any] | None = mass_queue_details.get("next_item")
        current_index: int = mass_queue_details.get("current_index") or 0
        properties: dict[str, Any] = {
            "canGoNext": bool(next_queue_item is not None),
            "canGoPrevious": bool(current_index > 0),
            "canPlay": bool(current_queue_item is not None),
            "canPause": bool(current_queue_item is not None),
            "canSeek": bool(current_queue_item and current_queue_item.get("duration") is not None),
            "canControl": bool(current_queue_item is not None),
            "playbackStatus": mass_queue_details.get("state", "stopped"),
            "loopStatus": LOOP_STATUS_MAP.get(mass_queue_details.get("repeat_mode", "off"), "none"),
            "shuffle": mass_queue_details.get("shuffle_enabled", False),
            "volume": 0,
            "mute": False,
            "rate": 1.0,
            "position": mass_queue_details.get("elapsed_time", 0.0),
            "metadata": {},
        }
        image_url: str | None = None
        if current_queue_item and (media_item := current_queue_item.get("media_item")):
            if image_path := current_queue_item.get("image", {}).get("path"):
                image_path_encoded = urllib.parse.quote_plus(image_path)
                image_url = (
                    f"http://{self.ma_websocket_ip}:{self.ma_websocket_port}/imageproxy"
                    f"?path={image_path_encoded}"
                    f"&provider={current_queue_item['image']['provider']}"
                    "&size=512"
                )
            properties["metadata"] = {
                "trackId": media_item.get("uri") or current_queue_item.get("queue_item_id"),
                "duration": media_item.get("duration"),
                "title": media_item.get("name") or current_queue_item.get("name"),
                "artUrl": image_url,
            }
            if "artists" in media_item:
                properties["metadata"]["artist"] = [x["name"] for x in media_item["artists"]]
                properties["metadata"]["artistSort"] = [
                    x["sort_name"] for x in media_item["artists"]
                ]
            if media_item.get("album"):
                properties["metadata"]["album"] = media_item["album"]["name"]
                properties["metadata"]["albumSort"] = media_item["album"]["sort_name"]
        elif current_queue_item:
            properties["metadata"] = {
                "title": current_queue_item.get("name"),
                "trackId": current_queue_item.get("queue_item_id"),
                "artUrl": image_url,
            }
        return properties

    def _clear_resolved_state(self) -> None:
        """Reset the bridge to its unresolved read-only state."""
        self._current_queue_id = None
        self._current_player_id = None
        self._properties = self._default_properties()

    def _ws_url(self) -> str:
        """Return the Music Assistant websocket URL."""
        return f"ws://{self.ma_websocket_ip}:{self.ma_websocket_port}/ws"

    def _handle_auth_result(self, result: dict[str, Any] | None) -> None:
        """Handle the response to the MA websocket auth command."""
        authenticated = bool(result and result.get("authenticated"))
        self._authenticated = authenticated
        if not authenticated:
            logger.warning("Authentication with Music Assistant WebSocket API failed")
            self._clear_resolved_state()
            self.send_snapcast_properties_notification(self._properties)
            if self._ws is not None:
                with suppress(Exception):
                    self._ws.close()
            return

        logger.info("Authenticated with Music Assistant WebSocket API")
        if self._ws is not None:
            with suppress(Exception):
                # After the initial handshake/auth completes we want a blocking recv loop.
                # The connect timeout is still useful for establishing the websocket, but
                # leaving the socket timeout at 10 seconds causes healthy idle connections
                # to be treated as failures.
                self._ws.settimeout(None)
        self._resolve_stream_state(notify=True)

    def _disconnect_websocket(self, reason: str, *, notify: bool) -> None:
        """Transition the bridge to a disconnected read-only state."""
        if self._authenticated or self._ws is not None:
            logger.info("Music Assistant websocket disconnected: %s", reason)
        self._authenticated = False
        self._cancel_stream_state_retry()
        self._clear_resolved_state()
        with self._callback_lock:
            self._request_callbacks.clear()
        ws = self._ws
        self._ws = None
        if ws is not None:
            with suppress(Exception):
                ws.close()
        if notify:
            self.send_snapcast_properties_notification(self._properties)

    def _reconnect_delay(self, attempt: int) -> float:
        """Return an exponential reconnect delay with bounded jitter."""
        base_delay = min(30.0, float(2 ** max(attempt - 1, 0)))
        return max(1.0, base_delay + random.uniform(0.0, 0.5))

    def _ws_loop(self) -> None:
        """Maintain the Music Assistant websocket connection."""
        logger.info("Started Music Assistant websocket loop")
        attempt = 0
        while not self._stopped:
            try:
                self._connect_and_read()
                attempt = 0
            except Exception as err:
                attempt += 1
                self._disconnect_websocket(str(err), notify=True)
                delay = self._reconnect_delay(attempt)
                logger.warning("WebSocket loop error: %s. Reconnecting in %.1f seconds", err, delay)
                if self._shutdown_event.wait(delay):
                    break

    def _connect_and_read(self) -> None:
        """Connect to the MA websocket and read messages until disconnect."""
        ws_url = self._ws_url()
        logger.info("Connecting to Music Assistant WebSocket: %s", ws_url)
        ws = websocket.create_connection(ws_url, timeout=10, enable_multithread=True)
        self._ws = ws
        self._authenticated = False
        self.send_request("auth", callback=self._handle_auth_result, token=self.ma_access_token)

        while not self._stopped:
            message = ws.recv()
            if message is None:
                raise ConnectionError("WebSocket closed by server")
            if message == "":
                raise ConnectionError("Empty websocket frame received")
            self._handle_ws_message(str(message))

    def _fetch_snapcast_server_status(self) -> dict[str, Any] | None:
        """Fetch the current Snapserver status via HTTP JSON-RPC."""
        request_data = json.dumps(
            {"id": 1, "jsonrpc": "2.0", "method": "Server.GetStatus"}
        ).encode()
        request = urllib.request.Request(
            f"http://{self.snapcast_host}:{self.snapcast_port}/jsonrpc",
            data=request_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                payload = json.loads(response.read().decode())
        except Exception as err:
            logger.debug("Unable to fetch Snapserver status: %s", err)
            return None

        if not isinstance(payload, dict):
            return None
        result = payload.get("result")
        if isinstance(result, dict) and "server" in result:
            server_payload = result["server"]
            return cast(
                "dict[str, Any] | None",
                server_payload if isinstance(server_payload, dict) else None,
            )
        if isinstance(result, dict):
            return cast("dict[str, Any]", result)
        return None

    def _has_existing_inactive_snapcast_stream(self) -> bool:
        """Return True if a matching idle Snapcast stream is still visible on the server."""
        if not (status := self._fetch_snapcast_server_status()):
            return False

        candidate_names = {self.stream}
        for snap_stream in status.get("streams", []):
            if snap_stream.get("status") != "idle":
                continue
            if snap_stream.get("id") in candidate_names:
                return True
            raw_uri = snap_stream.get("uri", {}).get("raw", "")
            if not raw_uri:
                continue
            parsed = urllib.parse.urlparse(raw_uri)
            stream_name = urllib.parse.parse_qs(parsed.query).get("name", [None])[0]
            if stream_name and urllib.parse.unquote_plus(stream_name) in candidate_names:
                return True
        return False

    def _schedule_stream_state_retry(self) -> None:
        """Schedule a delayed resolve retry after reconnect or stale stream state."""
        with self._resolve_retry_lock:
            if self._resolve_retry_timer is not None:
                return
            timer = threading.Timer(2.0, self._retry_resolve_stream_state)
            timer.daemon = True
            self._resolve_retry_timer = timer
            timer.start()

    def _retry_resolve_stream_state(self) -> None:
        """Run the deferred stream-state retry."""
        with self._resolve_retry_lock:
            self._resolve_retry_timer = None
        self._resolve_stream_state(notify=True)

    def _cancel_stream_state_retry(self) -> None:
        """Cancel any pending stream-state retry timer."""
        with self._resolve_retry_lock:
            if self._resolve_retry_timer is not None:
                self._resolve_retry_timer.cancel()
                self._resolve_retry_timer = None

    def _resolve_stream_state(self, notify: bool = True) -> None:
        """Resolve the current visible Snapcast stream to a MA queue."""

        def handle_result(result: dict[str, Any] | None) -> None:
            if not result or not result.get("queue"):
                logger.info("Resolve miss for Snapcast stream '%s'", self.stream)
                self._clear_resolved_state()
                if result is None and self._has_existing_inactive_snapcast_stream():
                    self._schedule_stream_state_retry()
                else:
                    self._cancel_stream_state_retry()
                if notify:
                    logger.info("Snapcast stream '%s' remains in read-only state", self.stream)
                    self.send_snapcast_properties_notification(self._properties)
                return

            self._cancel_stream_state_retry()
            self._current_queue_id = result.get("queue_id")
            self._current_player_id = result.get("player_id")
            self._properties = self._create_properties(result["queue"])
            if notify:
                self.send_snapcast_properties_notification(self._properties)

        if not self.send_request(
            "snapcast/resolve_control_stream", callback=handle_result, stream=self.stream
        ):
            self._clear_resolved_state()
            if notify:
                self.send_snapcast_properties_notification(self._properties)

    def send_request(
        self, command: str, callback: MessageCallback | None = None, **args: str | float | bool
    ) -> bool:
        """Send a request to Music Assistant via the websocket connection."""
        if self._ws is None:
            logger.debug("Cannot send request - websocket not connected")
            return False
        if command != "auth" and not self._authenticated:
            logger.debug("Cannot send request - websocket not authenticated")
            return False

        request_id = uuid.uuid4().hex[:10]
        payload = {
            "message_id": request_id,
            "command": command,
            "args": args,
        }
        if callback:
            with self._callback_lock:
                self._request_callbacks[request_id] = callback
        with self._send_lock:
            try:
                self._ws.send(json.dumps(payload))
            except Exception as err:
                logger.warning("Failed to send websocket request: %s", err)
                with self._callback_lock:
                    self._request_callbacks.pop(request_id, None)
                return False
        return True

    def send_snapcast_log_notification(self, message: str, severity: str = "Info") -> None:
        """Send a log message back to Snapcast."""
        send(
            {
                "jsonrpc": "2.0",
                "method": "Plugin.Stream.Log",
                "params": {"severity": severity, "message": message},
            }
        )

    def send_snapcast_properties_notification(self, properties: dict[str, Any]) -> None:
        """Publish updated player properties to Snapcast."""
        send(
            {
                "jsonrpc": "2.0",
                "method": "Plugin.Stream.Player.Properties",
                "params": properties,
            }
        )

    def send_snapcast_stream_ready_notification(self) -> None:
        """Notify Snapcast that the bridge is ready."""
        send({"jsonrpc": "2.0", "method": "Plugin.Stream.Ready"})

    def _handle_ws_message(self, message: str) -> None:
        """Handle an incoming Music Assistant websocket message."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError as err:
            logger.warning("Invalid websocket payload: %s", err)
            return

        if "message_id" in data:
            message_id = data["message_id"]
            with self._callback_lock:
                callback = self._request_callbacks.pop(message_id, None)
            if callback:
                callback(data.get("result"))
            return

        if data.get("event") == "queue_updated" and data.get("object_id") == self._current_queue_id:
            if queue_data := data.get("data"):
                self._properties = self._create_properties(queue_data)
                self.send_snapcast_properties_notification(self._properties)
            return

        if (
            data.get("event") == "queue_time_updated"
            and data.get("object_id") == self._current_queue_id
        ):
            updated_properties = {
                **self._properties,
                "position": float(data.get("data") or 0.0),
            }
            self._properties = updated_properties
            self.send_snapcast_properties_notification(updated_properties)
            return

        if data.get("event") == "player_updated":
            self._resolve_stream_state(notify=True)

    def handle_snapcast_request(self, request: dict[str, Any]) -> None:
        """Handle a Snapcast JSON-RPC request from stdin."""
        message_id = request["id"]
        interface, cmd = request["method"].rsplit(".", 1)

        if interface != "Plugin.Stream.Player" or cmd not in (
            "Control",
            "SetProperty",
            "GetProperties",
        ):
            send_error(message_id, -32601, "Method not found")
            return

        if cmd == "GetProperties":
            send({"jsonrpc": "2.0", "result": self._properties, "id": message_id})
            return

        if self._current_queue_id is None:
            self._resolve_stream_state(notify=False)
        if self._current_queue_id is None:
            send_error(
                message_id,
                -32000,
                f"No active Music Assistant queue resolved for stream '{self.stream}'",
            )
            return

        queue_id = self._current_queue_id

        if cmd == "Control":
            command = request["params"]["command"]
            control_params = request["params"].get("params", {})
            if command == "next":
                self.send_request("player_queues/next", queue_id=queue_id)
            elif command == "previous":
                self.send_request("player_queues/previous", queue_id=queue_id)
            elif command == "play":
                self.send_request("player_queues/play", queue_id=queue_id)
            elif command == "pause":
                self.send_request("player_queues/pause", queue_id=queue_id)
            elif command == "playPause":
                self.send_request("player_queues/play_pause", queue_id=queue_id)
            elif command == "stop":
                self.send_request("player_queues/stop", queue_id=queue_id)
            elif command == "setPosition":
                self.send_request(
                    "player_queues/seek",
                    queue_id=queue_id,
                    position=float(control_params["position"]),
                )
            elif command == "seek":
                self.send_request(
                    "player_queues/skip",
                    queue_id=queue_id,
                    seconds=float(control_params["offset"]),
                )
        elif cmd == "SetProperty":
            properties = request["params"]
            if "shuffle" in properties:
                self.send_request(
                    "player_queues/shuffle",
                    queue_id=queue_id,
                    shuffle_enabled=properties["shuffle"],
                )
            if "loopStatus" in properties:
                self.send_request(
                    "player_queues/repeat",
                    queue_id=queue_id,
                    repeat_mode=LOOP_STATUS_MAP_REVERSE[properties["loopStatus"]],
                )

        send({"jsonrpc": "2.0", "result": "ok", "id": message_id})

    def stop(self) -> None:
        """Stop the bridge."""
        self._stopped = True
        self._cancel_stream_state_retry()
        self._disconnect_websocket("stop requested", notify=False)
        ws_thread = getattr(self, "_ws_thread", None)
        if ws_thread is not None and threading.current_thread() is not ws_thread:
            ws_thread.join(timeout=2)
        self._shutdown_event.set()


def main() -> None:
    """Run the standalone bridge entrypoint."""
    runtime_params = _resolve_runtime_params(sys.argv)
    stream = runtime_params["stream"]

    if not stream:
        print(  # noqa: T201
            f"Usage: {runtime_params['progname']} --stream=<stream_display_name>",
            file=sys.stderr,
        )
        sys.exit(1)

    ctrl = MusicAssistantControl(
        stream=stream,
        snapcast_host=runtime_params["snapcast-host"],
        snapcast_port=runtime_params["snapcast-port"],
        ma_websocket_ip=runtime_params["ma-websocket-ip"],
        ma_websocket_port=runtime_params["ma-websocket-port"],
        ma_access_token=runtime_params["ma-access-token"],
    )
    ctrl.send_snapcast_stream_ready_notification()

    try:
        while not ctrl._shutdown_event.is_set():
            line = sys.stdin.readline()
            if not line:
                break
            ctrl.handle_snapcast_request(json.loads(line))
    finally:
        ctrl.stop()


log_format_stderr = "%(asctime)s %(module)s %(levelname)s: %(message)s"
logger = logging.getLogger("mass_bridge")
logger.propagate = False
logger.setLevel(logging.INFO)
if not logger.handlers:
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter(log_format_stderr))
    logger.addHandler(log_handler)


if __name__ == "__main__":
    main()
