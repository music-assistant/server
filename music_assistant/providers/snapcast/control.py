#!/usr/bin/env python3
"""
Control Music Assistant Snapcast plugin.

This script is a bridge between Music Assistant and Snapcast
It listens to the MA websocket and sends metadata to Snapcast
and listens for player commands
"""

import json
import logging
import sys
import threading
import urllib.parse
from collections.abc import Callable
from time import sleep
from typing import Any

import shortuuid
import websocket

LOOP_STATUS_MAP = {
    "all": "playlist",
    "one": "track",
    "off": "none",
}
LOOP_STATUS_MAP_REVERSE = {v: k for k, v in LOOP_STATUS_MAP.items()}

MessageCallback = Callable[[dict[str, Any]], None]


def send(json_msg: dict[str, Any]):
    """Send a message to stdout."""
    sys.stdout.write(json.dumps(json_msg))
    sys.stdout.write("\n")
    sys.stdout.flush()


class MusicAssistantControl:
    """Music Assistant websocket remote control Snapcast plugin."""

    def __init__(
        self, queue_id: str, streamserver_ip: str, streamserver_port: int, api_port: int
    ) -> None:
        """Initialize."""
        self.queue_id = queue_id
        self.api_port = api_port
        self.streamserver_ip = streamserver_ip
        self.streamserver_port = streamserver_port
        self._metadata = {}
        self._properties = {}
        self._request_callbacks: dict[str, MessageCallback] = {}
        self._seek_offset = 0.0
        self.websocket = websocket.WebSocketApp(
            url=f"ws://localhost:{api_port}/ws",
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_open=self._on_ws_open,
            on_close=self._on_ws_close,
        )
        self._stopped = False
        self.websocket_thread = threading.Thread(target=self._websocket_loop, args=())
        self.websocket_thread.name = "massControl"
        self.websocket_thread.start()

    def stop(self):
        """Stop the websocket thread."""
        self._stopped = True
        self.websocket.close()
        self.websocket_thread.join()

    def handle_snapcast_request(self, request: dict[str, Any]) -> None:
        """Handle (JSON RPC) message from Snapcast."""
        id: str = request["id"]  # noqa: A001
        interface, cmd = request["method"].rsplit(".", 1)

        queue_id = self.queue_id

        # deny invalid commands
        if interface != "Plugin.Stream.Player" or cmd not in (
            "Control",
            "SetProperty",
            "GetProperties",
        ):
            send(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": "Method not found"},
                    "id": id,
                }
            )

        if cmd == "Control":
            command = request["params"]["command"]
            params = request["params"].get("params", {})
            logger.debug(f"Control command: {command}, params: {params}")
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
                position = float(params["position"])
                self.send_request("player_queues/seek", queue_id=queue_id, position=position)
            elif command == "seek":
                seek_offset = float(params["offset"])
                self.send_request("player_queues/skip", queue_id=queue_id, seconds=seek_offset)
        elif cmd == "SetProperty":
            properties = request["params"]
            logger.debug(f"SetProperty: {property}")
            if "shuffle" in property:
                self.send_request(
                    "player_queues/shuffle",
                    queue_id=queue_id,
                    shuffle_enabled=properties["shuffle"],
                )
            if "loopStatus" in properties:
                value = properties["loopStatus"]
                self.send_request(
                    "player_queues/repeat",
                    queue_id=queue_id,
                    repeat_mode=LOOP_STATUS_MAP_REVERSE[value],
                )
            # if "volume" in properties:
            #     self.send_request("core.mixer.set_volume", {"volume": int(properties["volume"])})
            # if "mute" in properties:
            #     self.send_request("core.mixer.set_mute", {"mute": properties["mute"]})
        elif cmd == "GetProperties":

            def handle_result(result: dict[str, Any]):
                send(
                    {
                        "jsonrpc": "2.0",
                        "result": self._create_properties(result),
                        "id": id,
                    }
                )

            self.send_request("player_queues/get", callback=handle_result, queue_id=queue_id)
            return

        # always acknowledge the request
        send({"jsonrpc": "2.0", "result": "ok", "id": id})

    def send_snapcast_log_notification(self, message: str, severity: str = "Info") -> None:
        """Send log message to Snapcast."""
        send(
            {
                "jsonrpc": "2.0",
                "method": "Plugin.Stream.Log",
                "params": {"severity": severity, "message": message},
            }
        )

    def send_snapcast_properties_notification(self, properties: dict[str, Any]) -> None:
        """Send properties to Snapcast."""
        send(
            {
                "jsonrpc": "2.0",
                "method": "Plugin.Stream.Player.Properties",
                "params": properties,
            }
        )

    def send_snapcast_stream_ready_notification(self) -> None:
        """Send stream ready notification to Snapcast."""
        send({"jsonrpc": "2.0", "method": "Plugin.Stream.Ready"})

    def _websocket_loop(self):
        logger.info("Started websocket loop")
        while not self._stopped:
            try:
                self.websocket.run_forever()
                sleep(2)
            except (Exception, KeyboardInterrupt) as e:
                logger.info(f"Exception: {e!s}")

    def _create_properties(self, mass_queue_details: dict[str, Any]) -> dict[str, Any]:
        """Create snapcast properties from Music Assistant queue details."""
        current_queue_item: dict[str, Any] | None = mass_queue_details.get("current_item")
        next_queue_item: dict[str, Any] | None = mass_queue_details.get("next_item")
        properties: dict[str, Any] = {
            "canGoNext": next_queue_item is not None,
            "canGoPrevious": mass_queue_details["current_index"] > 0,
            "canPlay": current_queue_item is not None,
            "canPause": current_queue_item is not None,
            "canSeek": current_queue_item and current_queue_item.get("duration") is not None,
            "canControl": True,
            "playbackStatus": mass_queue_details["state"],
            "loopStatus": LOOP_STATUS_MAP[mass_queue_details["repeat_mode"]],
            "shuffle": mass_queue_details["shuffle_enabled"],
            "volume": 0,
            "mute": False,
            "rate": 1.0,
            "position": mass_queue_details["elapsed_time"],
        }
        if current_queue_item and (media_item := current_queue_item.get("media_item")):
            image_url: str | None = None
            if image_path := current_queue_item.get("image", {}).get("path"):
                image_path_encoded = urllib.parse.quote_plus(image_path)
                image_url = (
                    # we prefer the streamserver for the imageproxy because it is enabled by default
                    # where the api server is by default protected
                    f"http://{self.streamserver_ip}:{self.streamserver_port}/imageproxy?path={image_path_encoded}"
                    f"&provider={current_queue_item['image']['provider']}"
                    "&size=512"
                )
            properties["metadata"] = {
                "trackId": media_item["uri"],
                "duration": media_item["duration"],
                "title": media_item["name"],
                "artUrl": image_url,
            }
            if "artists" in media_item:
                properties["metadata"]["artist"] = [x["name"] for x in media_item["artists"]]
                properties["metadata"]["artistSort"] = [
                    x["sort_name"] for x in media_item["artists"]
                ]
            if "album" in media_item:
                properties["metadata"]["album"] = media_item["album"]["name"]
                properties["metadata"]["albumSort"] = media_item["album"]["sort_name"]
        elif current_queue_item:
            properties["metadata"] = {
                "title": current_queue_item["name"],
                "trackId": current_queue_item["queue_item_id"],
                "artUrl": image_url,
            }

        return properties

    def _on_ws_message(self, ws, message: str):
        # TODO: error handling
        logger.debug("websocket message received: %s", message)
        data = json.loads(message)

        # Request response
        if "message_id" in data:
            message_id = data["message_id"]
            if callback := self._request_callbacks.pop(message_id, None):
                if result := data.get("result"):
                    callback(result)
                # TODO: handle failed requests
            return

        # Event
        if "event" in data and data["object_id"] == self.queue_id:
            event = data["event"]
            if event == "queue_updated":
                properties = self._create_properties(data["data"])
                self.send_snapcast_properties_notification(properties)
                return

    def _on_ws_error(self, ws, error):
        logger.error("Websocket error")
        logger.error(error)

    def _on_ws_open(self, ws):
        logger.info("Snapcast RPC websocket opened")
        self.send_snapcast_stream_ready_notification()

    def _on_ws_close(self, ws, close_status_code, close_msg):
        logger.info("Snapcast RPC websocket closed")

    def send_request(
        self, command: str, callback: MessageCallback | None = None, **args: dict[str, Any]
    ) -> None:
        """Send request to Music Assistant."""
        msg_id = shortuuid.random(10)
        command_msg = {
            "message_id": msg_id,
            "command": command,
            "args": args,
        }
        logger.debug("send_request: %s", command_msg)
        if callback:
            self._request_callbacks[msg_id] = callback
        self.websocket.send(json.dumps(command_msg))


if __name__ == "__main__":
    # Parse command line
    queue_id = None
    api_port = None
    streamserver_ip = None
    streamserver_port = None
    for arg in sys.argv:
        if arg.startswith("--stream="):
            stream_id = arg.split("=")[1]
        if arg.startswith("--queueid="):
            queue_id = arg.split("=")[1]
        if arg.startswith("--streamserver-ip="):
            streamserver_ip = arg.split("=")[1]
        if arg.startswith("--streamserver-port="):
            streamserver_port = arg.split("=")[1]
        if arg.startswith("--api-port="):
            api_port = arg.split("=")[1]

    if not queue_id or not api_port:
        print("Usage: --stream=<stream_id> --api_port=<api_port>")  # noqa: T201
        sys.exit()

    log_format_stderr = "%(asctime)s %(module)s %(levelname)s: %(message)s"
    log_level = logging.INFO
    logger = logging.getLogger("meta_mass")
    logger.propagate = False
    logger.setLevel(log_level)

    # Log to stderr
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter(log_format_stderr))
    logger.addHandler(log_handler)

    logger.debug(
        "Initializing for stream_id %s, queue_id %s and api_port %s", stream_id, queue_id, api_port
    )

    ctrl = MusicAssistantControl(queue_id, streamserver_ip, int(streamserver_port), int(api_port))

    # keep listening for messages on stdin and forward them
    try:
        for line in sys.stdin:
            try:
                ctrl.handle_snapcast_request(json.loads(line))
            except Exception as e:
                send(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Parse error", "data": str(e)},
                        "id": id,
                    }
                )
    except (SystemExit, KeyboardInterrupt):
        sys.exit(0)
