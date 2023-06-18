"""
CLI interface which is more or less compatible with Logitech Media Server.

Implemented protocols: CometD, Telnet and JSON-RPC.

NOTE: This only implements the bare minimum to have functional players.
Output is adjusted to conform to Music Assistant logic or just for simplification.
Goal is player compatibility, not API compatibility.
Users that need more, should just stay with a full blown LMS server.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.helpers.util import empty_queue, select_free_port
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import EventType, PlayerState, QueueOption, RepeatMode
from music_assistant.common.models.errors import MusicAssistantError
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.media_items import MediaItemType
from music_assistant.common.models.queue_item import QueueItem

from .models import (
    PLAYMODE_MAP,
    REPEATMODE_MAP,
    CometDResponse,
    CommandErrorMessage,
    CommandMessage,
    CommandResultMessage,
    PlayerItem,
    PlayersResponse,
    PlayerStatusResponse,
    ServerStatusResponse,
    SlimMenuItem,
    SlimSubscribeMessage,
    menu_item_from_media_item,
    menu_item_from_queue_item,
    player_item_from_mass,
    playlist_item_from_mass,
)

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

    from . import SlimprotoProvider


# ruff: noqa: ARG002, E501

ArgsType = list[int | str]
KwargsType = dict[str, Any]


@dataclass
class CometDClient:
    """Representation of a connected CometD client."""

    client_id: str
    player_id: str = ""
    queue: asyncio.Queue[CometDResponse] = field(default_factory=asyncio.Queue)
    last_seen: int = int(time.time())
    first_event: CometDResponse | None = None
    meta_subscriptions: set[str] = field(default_factory=set)
    slim_subscriptions: dict[str, SlimSubscribeMessage] = field(default_factory=dict)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


def parse_value(raw_value: int | str) -> int | str | tuple[str, int | str]:
    """
    Transform API param into a usable value.

    Integer values are sometimes sent as string so we try to parse that.
    """
    if isinstance(raw_value, str):
        if ":" in raw_value:
            # this is a key:value value
            key, val = raw_value.split(":", 1)
            if val.isnumeric():
                val = int(val)
            return (key, val)
        if raw_value.isnumeric():
            # this is an integer sent as string
            return int(raw_value)
    return raw_value


def parse_args(raw_values: list[int | str]) -> tuple[ArgsType, KwargsType]:
    """Pargse Args and Kwargs from raw CLI params."""
    args: ArgsType = []
    kwargs: KwargsType = {}
    for raw_value in raw_values:
        value = parse_value(raw_value)
        if isinstance(value, tuple):
            kwargs[value[0]] = value[1]
        else:
            args.append(value)
    return (args, kwargs)


class LmsCli:
    """Basic LMS CLI (json rpc and telnet) implementation, (partly) compatible with Logitech Media Server."""

    cli_port: int = 9090
    _unsub_callback: Callable | None = None
    _periodic_task: asyncio.Task | None = None

    def __init__(
        self, slimproto: SlimprotoProvider, enable_telnet: bool, enable_json: bool
    ) -> None:
        """Initialize."""
        self.slimproto = slimproto
        self.enable_telnet = enable_telnet
        self.enable_json = enable_json
        self.logger = self.slimproto.logger.getChild("cli")
        self.mass = self.slimproto.mass
        self._cometd_clients: dict[str, CometDClient] = {}

    async def setup(self) -> None:
        """Handle async initialization of the plugin."""
        if self.enable_json:
            self.logger.info("Registering jsonrpc endpoints on the webserver")
            self.mass.webserver.register_route("/jsonrpc.js", self._handle_jsonrpc)
            self.mass.webserver.register_route("/cometd", self._handle_cometd)
            self._unsub_callback = self.mass.subscribe(
                self._on_mass_event,
                (EventType.PLAYER_UPDATED, EventType.QUEUE_UPDATED),
            )
            self._periodic_task = self.mass.create_task(self._do_periodic())
        if self.enable_telnet:
            self.cli_port = await select_free_port(9090, 9190)
            self.logger.info("Starting (telnet) CLI on port %s", self.cli_port)
            await asyncio.start_server(self._handle_cli_client, "0.0.0.0", self.cli_port)

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        self.mass.webserver.unregister_route("/jsonrpc.js")
        self.mass.webserver.unregister_route("/cometd")
        if self._unsub_callback:
            self._unsub_callback()
            self._unsub_callback = None
        if self._periodic_task:
            self._periodic_task.cancel()
            self._periodic_task = None

    async def _handle_cli_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the legacy CLI."""
        # https://raw.githubusercontent.com/Logitech/slimserver/public/7.8/HTML/EN/html/docs/cli-api.html
        # https://github.com/elParaguayo/LMS-CLI-Documentation/blob/master/LMS-CLI.md
        self.logger.info("Client connected on Telnet CLI")
        try:
            while True:
                raw_request = await reader.readline()
                raw_request = raw_request.strip().decode("utf-8")
                # request comes in as url encoded strings, separated by space
                raw_params = [urllib.parse.unquote(x) for x in raw_request.split(" ")]
                # the first param is either a macaddress or a command
                if ":" in raw_params[0]:
                    # assume this is a mac address (=player_id)
                    player_id = raw_params[0]
                    command = raw_params[1]
                    command_params = raw_params[2:]
                else:
                    player_id = ""
                    command = raw_params[0]
                    command_params = raw_params[1:]

                args, kwargs = parse_args(command_params)

                response: str = raw_request

                # check if we have a handler for this command
                # note that we only have support for very limited commands
                # just enough for compatibility with players but not to be used as api
                # with 3rd party tools!
                if handler := getattr(self, f"_handle_{command}", None):
                    self.logger.debug(
                        "Handling CLI-request (player: %s command: %s - args: %s - kwargs: %s)",
                        player_id,
                        command,
                        str(args),
                        str(kwargs),
                    )
                    cmd_result: list[str] = handler(player_id, *args, **kwargs)
                    if asyncio.iscoroutine(cmd_result):
                        cmd_result = await cmd_result

                    if isinstance(cmd_result, dict):
                        result_parts = dict_to_strings(cmd_result)
                        result_str = " ".join(urllib.parse.quote(x) for x in result_parts)
                    elif not cmd_result:
                        result_str = ""
                    else:
                        result_str = str(cmd_result)
                    response += " " + result_str
                else:
                    self.logger.warning(
                        "No handler for %s (player: %s - args: %s - kwargs: %s)",
                        command,
                        player_id,
                        str(args),
                        str(kwargs),
                    )
                # echo back the request and the result (if any)
                response += "\n"
                writer.write(response.encode("utf-8"))
                await writer.drain()
        except ConnectionResetError:
            pass
        except Exception as err:
            self.logger.debug("Error handling CLI command", exc_info=err)
        finally:
            self.logger.debug("Client disconnected from Telnet CLI")

    async def _handle_jsonrpc(self, request: web.Request) -> web.Response:
        """Handle request on JSON-RPC endpoint."""
        command_msg: CommandMessage = await request.json(loads=json_loads)
        self.logger.debug("Received request: %s", command_msg)
        cmd_result = await self._handle_request(command_msg["params"])
        if cmd_result is None:
            result: CommandErrorMessage = {
                **command_msg,
                "error": {"code": -1, "message": "Invalid command"},
            }
        else:
            result: CommandResultMessage = {
                **command_msg,
                "result": cmd_result,
            }
        # return the response to the client
        return web.json_response(result, dumps=json_dumps)

    async def _handle_cometd(self, request: web.Request) -> web.Response:  # noqa: PLR0912
        """
        Handle CometD request on the json CLI.

        https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Web/Cometd.pm
        """
        logger = self.logger.getChild("cometd")
        # ruff: noqa: PLR0915
        clientid: str = ""
        response = []
        streaming = False
        json_msg: list[dict[str, Any]] = await request.json()
        # cometd message is an array of commands/messages
        for cometd_msg in json_msg:
            channel = cometd_msg.get("channel")
            # try to figure out clientid
            if not clientid:
                clientid = cometd_msg.get("clientId")
            if not clientid and channel == "/meta/handshake":
                # generate new clientid
                clientid = shortuuid.uuid()
                self._cometd_clients[clientid] = CometDClient(
                    client_id=clientid,
                )
            elif not clientid and channel in ("/slim/subscribe", "/slim/request"):
                # pull clientId out of response channel
                clientid = cometd_msg["data"]["response"].split("/")[1]
            elif not clientid and channel == "/slim/unsubscribe":
                # pull clientId out of unsubscribe
                clientid = cometd_msg["data"]["unsubscribe"].split("/")[1]
            assert clientid, "No clientID provided"
            logger.debug("Incoming message for channel '%s' - clientid: %s", channel, clientid)

            # messageid is optional but if provided we must pass it along
            msgid = cometd_msg.get("id", "")

            if clientid not in self._cometd_clients:
                # If a client sends any request and we do not have a valid clid record
                # because the streaming connection has been lost for example, re-handshake them
                return web.json_response(
                    [
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": None,
                            "successful": False,
                            "timestamp": time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime()),
                            "error": "invalid clientId",
                            "advice": {
                                "reconnect": "handshake",
                                "interval": 0,
                            },
                        }
                    ]
                )

            # get the cometd_client object for the clientid
            cometd_client = self._cometd_clients[clientid]
            cometd_client.last_seen = int(time.time())

            if channel == "/meta/handshake":
                # handshake message
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "version": "1.0",
                        "supportedConnectionTypes": ["long-polling", "streaming"],
                        "clientId": clientid,
                        "successful": True,
                        "advice": {
                            "reconnect": "retry",  # one of "none", "retry", "handshake"
                            "interval": 0,  # initial interval is 0 to support long-polling's connect request
                            "timeout": 60000,
                        },
                    }
                )
                # playerid (mac) and uuid belonging to the client is sent in the ext field
                if player_id := cometd_msg.get("ext", {}).get("mac"):
                    cometd_client.player_id = player_id
                    if (uuid := cometd_msg.get("ext", {}).get("uuid")) and (
                        player := self.mass.players.get(player_id)
                    ):
                        player.extra_data["uuid"] = uuid

            elif channel in ("/meta/connect", "/meta/reconnect"):
                # (re)connect message
                logger.debug("Client (re-)connected: %s", clientid)
                streaming = cometd_msg["connectionType"] == "streaming"
                # confirm the connection
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                        "timestamp": time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime()),
                        "advice": {
                            # update interval for streaming mode
                            "interval": 5000
                            if streaming
                            else 0
                        },
                    }
                )
                # TODO: do we want to implement long-polling support too ?
                # https://github.com/Logitech/slimserver/blob/d9ebda7ebac41e82f1809dd85b0e4446e0c9be36/Slim/Web/Cometd.pm#L292

            elif channel == "/meta/disconnect":
                # disconnect message
                logger.debug("CometD Client disconnected: %s", clientid)
                self._cometd_clients.pop(clientid)
                return web.json_response(
                    [
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": clientid,
                            "successful": True,
                            "timestamp": time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime()),
                        }
                    ]
                )

            elif channel == "/meta/subscribe":
                cometd_client.meta_subscriptions.add(cometd_msg["subscription"])
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                        "subscription": cometd_msg["subscription"],
                    }
                )

            elif channel == "/meta/unsubscribe":
                if cometd_msg["subscription"] in cometd_client.meta_subscriptions:
                    cometd_client.meta_subscriptions.remove(cometd_msg["subscription"])
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                        "subscription": cometd_msg["subscription"],
                    }
                )
            elif channel == "/slim/subscribe":  # noqa: SIM114
                # A request to execute & subscribe to some Logitech Media Server event
                # A valid /slim/subscribe message looks like this:
                # {
                #   channel  => '/slim/subscribe',
                #   id       => <unique id>,
                #   data     => {
                #     response => '/slim/serverstatus', # the channel all messages should be sent back on
                #     request  => [ '', [ 'serverstatus', 0, 50, 'subscribe:60' ],
                #     priority => <value>, # optional priority value, is passed-through with the response
                #   }
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                    }
                )
                cometd_client.slim_subscriptions[cometd_msg["data"]["response"]] = cometd_msg
                # Return one-off result now, rest is handled by the subscription logic
                self._handle_cometd_request(cometd_client, cometd_msg)

            elif channel == "/slim/unsubscribe":
                # A request to unsubscribe from a Logitech Media Server event, this is not the same as /meta/unsubscribe
                # A valid /slim/unsubscribe message looks like this:
                # {
                #   channel  => '/slim/unsubscribe',
                #   data     => {
                #     unsubscribe => '/slim/serverstatus',
                #   }
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                    }
                )
                cometd_client.slim_subscriptions.pop(cometd_msg["data"]["unsubscribe"], None)

            elif channel == "/slim/request":
                # A request to execute a one-time Logitech Media Server event
                # A valid /slim/request message looks like this:
                # {
                #   channel  => '/slim/request',
                #   id       => <unique id>, (optional)
                #   data     => {
                #     response => '/slim/<clientId>/request',
                #     request  => [ '', [ 'menu', 0, 100, ],
                #     priority => <value>, # optional priority value, is passed-through with the response
                #   }
                if not msgid:
                    # If the caller does not want the response, id will be undef
                    logger.debug("Not sending response to request, caller does not want it")
                else:
                    # This response is optional, but we do it anyway
                    response.append(
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": clientid,
                            "successful": True,
                        }
                    )
                    self._handle_cometd_request(cometd_client, cometd_msg)
            else:
                logger.warning("Unhandled channel %s", channel)
                # always reply with the (default) response to every message
                response.append(
                    {
                        "channel": channel,
                        "id": msgid,
                        "clientId": clientid,
                        "successful": True,
                    }
                )
        # append any remaining messages from the queue
        while True:
            try:
                msg = cometd_client.queue.get_nowait()
                response.append(msg)
            except asyncio.QueueEmpty:
                break
        # send response
        headers = {
            "Server": "Logitech Media Server (7.9.9 - 1667251155)",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Expires": "-1",
            "Connection": "keep-alive",
        }
        # regular command/handshake messages are just replied and connection closed
        if not streaming:
            return web.json_response(response, headers=headers)

        # streaming mode: send messages from the queue to the client
        # the subscription connection is kept open and events are streamed to the client
        headers.update(
            {
                "Content-Type": "application/json",
            }
        )
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        chunk = json_dumps(response).encode("utf8")
        await resp.write(chunk)

        # keep delivering messages to the client until it disconnects
        # keep sending messages/events from the client's queue
        while True:
            # make sure we always send an array of messages
            msg = [await cometd_client.queue.get()]
            try:
                chunk = json_dumps(msg).encode("utf8")
                await resp.write(chunk)
                cometd_client.last_seen = int(time.time())
            except ConnectionResetError:
                return
        return resp

    def _handle_cometd_request(self, client: CometDClient, cometd_request: dict[str, Any]) -> None:
        """Handle request for CometD client (and put result on client queue)."""

        async def _handle():
            result = await self._handle_request(cometd_request["data"]["request"])
            await client.queue.put(
                {
                    "channel": cometd_request["data"]["response"],
                    "id": cometd_request["id"],
                    "data": result,
                    "ext": {"priority": cometd_request["data"].get("priority")},
                }
            )

        self.mass.create_task(_handle())

    async def _handle_request(self, params: tuple[str, list[str | int]]) -> Any:
        """Handle command for either JSON or CometD request."""
        # Slim request handler
        # {"method":"slim.request","id":1,"params":["aa:aa:ca:5a:94:4c",["status","-", 2, "tags:xcfldatgrKN"]]}
        self.logger.debug(
            "Handling request: %s",
            str(params),
        )
        player_id = params[0]
        command = str(params[1][0])
        args, kwargs = parse_args(params[1][1:])
        if player_id and "seq_no" in kwargs and (player := self.mass.players.get(player_id)):
            player.extra_data["seq_no"] = int(kwargs["seq_no"])
        if handler := getattr(self, f"_handle_{command}", None):
            # run handler for command
            cmd_result = handler(player_id, *args, **kwargs)
            if asyncio.iscoroutine(cmd_result):
                cmd_result = await cmd_result
            if cmd_result is None:
                cmd_result = {}
            elif not isinstance(cmd_result, dict):
                # individual values are returned with underscore ?!
                cmd_result = {f"_{command}": cmd_result}
            return cmd_result
        # no handler found
        self.logger.warning("No handler for %s", command)
        return None

    def _handle_players(
        self,
        player_id: str,
        start_index: int | str = 0,
        limit: int = 999,
        **kwargs,
    ) -> PlayersResponse:
        """Handle players command."""
        players: list[PlayerItem] = []
        for index, mass_player in enumerate(self.mass.players.all()):
            if isinstance(start_index, int) and index < start_index:
                continue
            if len(players) > limit:
                break
            players.append(player_item_from_mass(start_index + index, mass_player))
        return PlayersResponse(count=len(players), players_loop=players)

    async def _handle_status(
        self,
        player_id: str,
        offset: int | str = "-",
        limit: int = 2,
        menu: str = "",
        useContextMenu: int | bool = False,  # noqa: N803
        tags: str = "xcfldatgrKN",
        **kwargs,
    ) -> PlayerStatusResponse:
        """Handle player status command."""
        player = self.mass.players.get(player_id)
        if player is None:
            return None
        queue = self.mass.players.queues.get_active_queue(player_id)
        assert queue is not None
        start_index = queue.current_index or 0 if offset == "-" else offset
        queue_items: list[QueueItem] = []
        index = 0
        async for item in self.mass.players.queues.items(queue.queue_id):
            if index >= start_index:
                queue_items.append(item)
            if len(queue_items) == limit:
                break
            index += 1
        # base details
        result = {
            "player_name": player.display_name,
            "player_connected": int(player.available),
            "player_needs_upgrade": False,
            "player_is_upgrading": False,
            "power": int(player.powered),
            "signalstrength": 0,
            "waitingToPlay": 0,  # TODO?
        }
        # additional details if player powered
        if player.powered:
            result = {
                **result,
                "mode": PLAYMODE_MAP[queue.state],
                "remote": 1,
                "current_title": "Music Assistant",
                "time": queue.elapsed_time,
                "rate": 1,
                "duration": queue.current_item.duration if queue.current_item else 0,
                "sleep": 0,
                "will_sleep_in": 0,
                "sync_master": player.synced_to,
                "sync_slaves": ",".join(player.group_childs),
                "mixer volume": player.volume_level,
                "playlist repeat": REPEATMODE_MAP[queue.repeat_mode],
                "playlist shuffle": int(queue.shuffle_enabled),
                "playlist_timestamp": queue.elapsed_time_last_updated,
                "playlist_cur_index": queue.current_index,
                "playlist_tracks": queue.items,
                "seq_no": player.extra_data.get("seq_no", 0),
                "player_ip": player.device_info.address,
                "digital_volume_control": 1,
                "can_seek": 1,
                "playlist mode": "off",
                "playlist_loop": [
                    playlist_item_from_mass(
                        self.mass,
                        item,
                        queue.current_index + index,
                        queue.current_index == (queue.current_index + index),
                    )
                    for index, item in enumerate(queue_items)
                ],
            }
        # additional details if menu requested
        if menu == "menu":
            # in menu-mode the regular playlist_loop is replaced by item_loop
            result.pop("playlist_loop", None)
            presets = await self._get_preset_items(player_id)
            preset_data: list[dict] = []
            preset_loop: list[int] = []
            for _, media_item in presets:
                preset_data.append(
                    {
                        "URL": media_item["params"]["uri"],
                        "text": media_item["track"],
                        "type": "audio",
                    }
                )
                preset_loop.append(1)
            while len(preset_loop) < 10:
                preset_data.append({})
                preset_loop.append(0)
            result = {
                **result,
                "alarm_state": "none",
                "alarm_snooze_seconds": 540,
                "alarm_timeout_seconds": 3600,
                "count": len(queue_items),
                "offset": offset,
                "base": {
                    "actions": {
                        "more": {
                            "itemsParams": "params",
                            "window": {"isContextMenu": 1},
                            "cmd": ["contextmenu"],
                            "player": 0,
                            "params": {"context": "playlist", "menu": "track"},
                        }
                    }
                },
                "preset_loop": preset_loop,
                "preset_data": preset_data,
                "item_loop": [
                    menu_item_from_queue_item(
                        self.mass,
                        item,
                        queue.current_index + index,
                        queue.current_index == (queue.current_index + index),
                    )
                    for index, item in enumerate(queue_items)
                ],
            }
        # additional details if contextmenu requested
        if bool(useContextMenu):
            result = {
                **result,
                # TODO ?!,
            }

        return result

    async def _handle_serverstatus(
        self,
        player_id: str,
        start_index: int = 0,
        limit: int = 2,
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle server status command."""
        if start_index == "-":
            start_index = 0
        players: list[PlayerItem] = []
        for index, mass_player in enumerate(self.mass.players.all()):
            if isinstance(start_index, int) and index < start_index:
                continue
            if len(players) > limit:
                break
            players.append(player_item_from_mass(start_index + index, mass_player))
        return ServerStatusResponse(
            {
                "httpport": self.mass.webserver.port,
                "ip": self.mass.base_ip,
                "version": "7.999.999",
                "uuid": self.mass.server_id,
                # TODO: set these vars ?
                "info total duration": 0,
                "info total genres": 0,
                "sn player count": 0,
                "lastscan": 1685548099,
                "info total albums": 0,
                "info total songs": 0,
                "info total artists": 0,
                "players_loop": players,
                "player count": len(players),
                "other player count": 0,
                "other_players_loop": [],
            }
        )

    async def _handle_firmwareupgrade(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle firmwareupgrade command."""
        return {"firmwareUpgrade": 0, "relativeFirmwareUrl": "/firmware/baby_7.7.3_r16676.bin"}

    async def _handle_artworkspec(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle firmwareupgrade command."""
        # https://github.com/Logitech/slimserver/blob/e9c2f88e7ca60b3648b66116240f3f5fe6ca3188/Slim/Control/Commands.pm#L224
        return None

    def _handle_mixer(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player mixer command."""
        arg = args[0] if args else "?"
        player = self.mass.players.get(player_id)
        assert player is not None

        # <playerid> mixer volume <0 .. 100|-100 .. +100|?>
        if subcommand == "volume" and isinstance(arg, int):
            if "seq_no" in kwargs:
                # handle a (jive based) squeezebox that already executed the command
                # itself and just reports the new state
                player.volume_level = arg
                # self.mass.players.update(player_id)
            else:
                self.mass.create_task(self.mass.players.cmd_volume_set, player_id, arg)
            return
        if subcommand == "volume" and arg == "?":
            return player.volume_level
        if subcommand == "volume" and "+" in arg:
            volume_level = min(100, player.volume_level + int(arg.split("+")[1]))
            self.mass.create_task(self.mass.players.cmd_volume_set, player_id, volume_level)
            return
        if subcommand == "volume" and "-" in arg:
            volume_level = max(0, player.volume_level - int(arg.split("-")[1]))
            self.mass.create_task(self.mass.players.cmd_volume_set, player_id, volume_level)
            return

        # <playerid> mixer muting <0|1|toggle|?|>
        if subcommand == "muting" and isinstance(arg, int):
            self.mass.create_task(self.mass.players.cmd_volume_mute, player_id, int(arg))
            return
        if subcommand == "muting" and arg == "toggle":
            self.mass.create_task(
                self.mass.players.cmd_volume_mute, player_id, not player.volume_muted
            )
            return
        if subcommand == "muting":
            return int(player.volume_muted)
        self.logger.warning(
            "No handler for mixer/%s (player: %s - args: %s - kwargs: %s)",
            subcommand,
            player_id,
            str(args),
            str(kwargs),
        )

    def _handle_time(self, player_id: str, number: str | int) -> int | None:
        """Handle player `time` command."""
        # <playerid> time <number|-number|+number|?>
        # The "time" command allows you to query the current number of seconds that the
        # current song has been playing by passing in a "?".
        # You may jump to a particular position in a song by specifying a number of seconds
        # to seek to. You may also jump to a relative position within a song by putting an
        # explicit "-" or "+" character before a number of seconds you would like to seek.
        player_queue = self.mass.players.queues.get_active_queue(player_id)
        assert player_queue is not None

        if number == "?":
            return int(player_queue.corrected_elapsed_time)

        if isinstance(number, str) and ("+" in number or "-" in number):
            jump = int(number.split("+")[1])
            self.mass.create_task(self.mass.players.queues.skip, player_queue.queue_id, jump)
        else:
            self.mass.create_task(self.mass.players.queues.seek, player_queue.queue_id, number)

    def _handle_power(self, player_id: str, value: str | int, *args, **kwargs) -> int | None:
        """Handle player `time` command."""
        # <playerid> power <0|1|?|>
        # The "power" command turns the player on or off.
        # Use 0 to turn off, 1 to turn on, ? to query and
        # no parameter to toggle the power state of the player.
        player = self.mass.players.get(player_id)
        assert player is not None

        if value == "?":
            return int(player.powered)
        if "seq_no" in kwargs:
            # handle a (jive based) squeezebox that already executed the command
            # itself and just reports the new state
            player.powered = bool(value)
            # self.mass.players.update(player_id)
            return

        self.mass.create_task(self.mass.players.cmd_power, player_id, bool(value))

    def _handle_playlist(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player `playlist` command."""
        arg = args[0] if args else "?"
        queue = self.mass.players.queues.get_active_queue(player_id)
        assert queue is not None

        # <playerid> playlist index <index|+index|-index|?> <fadeInSecs>
        if subcommand == "index" and isinstance(arg, int):
            self.mass.create_task(self.mass.players.queues.play_index, player_id, arg)
            return
        if subcommand == "index" and arg == "?":
            return queue.current_index
        if subcommand == "index" and "+" in arg:
            next_index = (queue.current_index or 0) + int(arg.split("+")[1])
            self.mass.create_task(self.mass.players.queues.play_index, player_id, next_index)
            return
        if subcommand == "index" and "-" in arg:
            next_index = (queue.current_index or 0) - int(arg.split("-")[1])
            self.mass.create_task(self.mass.players.queues.play_index, player_id, next_index)
            return
        if subcommand == "shuffle":
            self.mass.players.queues.set_shuffle(queue.queue_id, not queue.shuffle_enabled)
            return
        if subcommand == "repeat":
            if queue.repeat_mode == RepeatMode.ALL:
                new_repeat_mode = RepeatMode.OFF
            elif queue.repeat_mode == RepeatMode.OFF:
                new_repeat_mode = RepeatMode.ONE
            else:
                new_repeat_mode = RepeatMode.ALL
            self.mass.players.queues.set_repeat(queue.queue_id, new_repeat_mode)
            return
        if subcommand == "crossfade":
            self.mass.players.queues.set_crossfade(queue.queue_id, not queue.crossfade_enabled)
            return

        self.logger.warning("Unhandled command: playlist/%s", subcommand)

    def _handle_playlistcontrol(
        self,
        player_id: str,
        *args,
        cmd: str,
        uri: str,
        **kwargs,
    ) -> int | None:
        """Handle player `playlistcontrol` command."""
        queue = self.mass.players.queues.get_active_queue(player_id)
        if cmd == "play":
            self.mass.create_task(
                self.mass.players.queues.play_media(queue.queue_id, uri, QueueOption.PLAY)
            )
            return
        if cmd == "load":
            self.mass.create_task(
                self.mass.players.queues.play_media(queue.queue_id, uri, QueueOption.REPLACE)
            )
            return
        if cmd == "add":
            self.mass.create_task(
                self.mass.players.queues.play_media(queue.queue_id, uri, QueueOption.ADD)
            )
            return
        if cmd == "insert":
            self.mass.create_task(
                self.mass.players.queues.play_media(queue.queue_id, uri, QueueOption.IN)
            )
            return
        self.logger.warning("Unhandled command: playlistcontrol/%s", cmd)

    def _handle_play(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player `play` command."""
        queue = self.mass.players.queues.get_active_queue(player_id)
        assert queue is not None
        self.mass.create_task(self.mass.players.queues.play, player_id)

    def _handle_stop(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player `stop` command."""
        queue = self.mass.players.queues.get_active_queue(player_id)
        assert queue is not None
        self.mass.create_task(self.mass.players.queues.stop, player_id)

    def _handle_pause(
        self,
        player_id: str,
        force: int = 0,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player `stop` command."""
        queue = self.mass.players.queues.get_active_queue(player_id)
        assert queue is not None

        if force or queue.state == PlayerState.PLAYING:
            self.mass.create_task(self.mass.players.queues.pause, player_id)
        else:
            self.mass.create_task(self.mass.players.queues.play, player_id)

    def _handle_mode(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player 'mode' command."""
        if subcommand == "play":
            return self._handle_play(player_id, *args, **kwargs)
        if subcommand == "pause":
            return self._handle_pause(player_id, *args, **kwargs)
        if subcommand == "stop":
            return self._handle_stop(player_id, *args, **kwargs)

        self.logger.warning(
            "No handler for mode/%s (player: %s - args: %s - kwargs: %s)",
            subcommand,
            player_id,
            str(args),
            str(kwargs),
        )

    def _handle_button(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player 'button' command."""
        player = self.mass.players.get(player_id)
        assert player is not None

        if subcommand == "volup":
            self.mass.create_task(self.mass.players.cmd_volume_up, player_id)
            return
        if subcommand == "voldown":
            self.mass.create_task(self.mass.players.cmd_volume_down, player_id)
            return
        if subcommand == "power":
            self.mass.create_task(self.mass.players.cmd_power, player_id, not player.powered)
            return
        # queue related button commands
        queue = self.mass.players.queues.get_active_queue(player_id)
        if subcommand == "jump_fwd":
            self.mass.create_task(self.mass.players.queues.next, queue.queue_id)
            return
        if subcommand == "jump_rew":
            self.mass.create_task(self.mass.players.queues.previous, queue.queue_id)
            return
        if subcommand == "fwd":
            self.mass.create_task(self.mass.players.queues.skip, queue.queue_id, 10)
            return
        if subcommand == "rew":
            self.mass.create_task(self.mass.players.queues.skip, queue.queue_id, -10)
            return
        if subcommand == "shuffle":
            self.mass.players.queues.set_shuffle(queue.queue_id, not queue.shuffle_enabled)
            return
        if subcommand == "repeat":
            if queue.repeat_mode == RepeatMode.ALL:
                new_repeat_mode = RepeatMode.OFF
            elif queue.repeat_mode == RepeatMode.OFF:
                new_repeat_mode = RepeatMode.ONE
            else:
                new_repeat_mode = RepeatMode.ALL
            self.mass.players.queues.set_repeat(queue.queue_id, new_repeat_mode)
            return
        if subcommand.startswith("preset_"):
            preset_index = subcommand.split("preset_")[1].split(".")[0]
            if preset_uri := self.mass.config.get_raw_player_config_value(
                player_id, f"preset_{preset_index}"
            ):
                option = QueueOption.REPLACE if "playlist" in preset_uri else QueueOption.PLAY
                self.mass.create_task(
                    self.mass.players.queues.play_media, queue.queue_id, preset_uri, option
                )
            return

        self.logger.warning(
            "No handler for button/%s (player: %s - args: %s - kwargs: %s)",
            subcommand,
            player_id,
            str(args),
            str(kwargs),
        )

    async def _handle_menu(
        self,
        player_id: str,
        offset: int = 0,
        limit: int = 10,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle menu request from CLI."""
        menu_items = []
        # we keep it simple for now and only add the presets to the 'My Music' menu
        for preset_id, media_item in await self._get_preset_items(player_id):
            menu_items.append(
                {
                    **media_item,
                    "id": f"preset_{preset_id}",
                    "node": "myMusic",
                    # prefer short title in menu structure
                    "text": media_item["track"],
                    "homeMenuText": media_item["text"],
                    "weight": 80,
                }
            )
        # add basic queue settings such as shuffle and repeat
        menu_items += [
            {
                "node": "settings",
                "isANode": 1,
                "id": "settingsAudio",
                "text": "Audio",
                "weight": 35,
            },
            {
                "selectedIndex": 1,
                "actions": {
                    "do": {
                        "choices": [
                            {"player": 0, "cmd": ["playlist", "repeat", "0"]},
                            {"player": 0, "cmd": ["playlist", "repeat", "1"]},
                            {"player": 0, "cmd": ["playlist", "repeat", "2"]},
                        ]
                    }
                },
                "choiceStrings": ["Off", "Song", "Playlist"],
                "id": "settingsRepeat",
                "node": "settings",
                "text": "Repeat",
                "weight": 20,
            },
            {
                "actions": {
                    "do": {
                        "choices": [
                            {"cmd": ["playlist", "shuffle", "0"], "player": 0},
                            {"cmd": ["playlist", "shuffle", "1"], "player": 0},
                        ]
                    }
                },
                "choiceStrings": ["Off", "On"],
                "selectedIndex": 1,
                "id": "settingsShuffle",
                "node": "settings",
                "weight": 10,
                "text": "Shuffle",
            },
            {
                "actions": {
                    "do": {
                        "choices": [
                            {"cmd": ["playlist", "crossfade", "0"], "player": 0},
                            {"cmd": ["playlist", "crossfade", "1"], "player": 0},
                        ]
                    }
                },
                "choiceStrings": ["Off", "On"],
                "selectedIndex": 1,
                "iconStyle": "hm_settingsAudio",
                "id": "settingsXfade",
                "node": "settings",
                "weight": 10,
                "text": "Crossfade",
            },
        ]
        return {
            "item_loop": menu_items[offset:limit],
            "offset": offset,
            "count": len(menu_items[offset:limit]),
        }

    async def _handle_browselibrary(
        self,
        player_id: str,
        subcommand: str,
        offset: int = 0,
        limit: int = 10,
        mode: str = "playlists",
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle menustatus request from CLI."""
        if mode == "albumartists":
            items = (
                await self.mass.music.artists.album_artists(True, limit=limit, offset=offset)
            ).items
        elif mode == "artists":
            items = (await self.mass.music.artists.db_items(True, limit=limit, offset=offset)).items
        elif mode == "artist" and "uri" in kwargs:
            artist = await self.mass.music.get_item_by_uri(kwargs["uri"])
            items = await self.mass.music.artists.tracks(artist.item_id, artist.provider)
        elif mode == "albums":
            items = (await self.mass.music.albums.db_items(True, limit=limit, offset=offset)).items
        elif mode == "album" and "uri" in kwargs:
            album = await self.mass.music.get_item_by_uri(kwargs["uri"])
            items = await self.mass.music.albums.tracks(album.item_id, album.provider)
        elif mode == "playlists":
            items = (
                await self.mass.music.playlists.db_items(True, limit=limit, offset=offset)
            ).items
        elif mode == "radios":
            items = (await self.mass.music.radio.db_items(True, limit=limit, offset=offset)).items
        elif mode == "playlist" and "uri" in kwargs:
            playlist = await self.mass.music.get_item_by_uri(kwargs["uri"])
            items = [
                x
                async for x in self.mass.music.playlists.tracks(playlist.item_id, playlist.provider)
            ]
        else:
            items = []
        return {
            "base": {
                "actions": {
                    "go": {
                        "params": {"menu": 1, "mode": "playlisttracks"},
                        "itemsParams": "commonParams",
                        "player": 0,
                        "cmd": ["browselibrary", "items"],
                    },
                    "add": {
                        "player": 0,
                        "itemsParams": "commonParams",
                        "params": {"menu": 1, "cmd": "add"},
                        "cmd": ["playlistcontrol"],
                    },
                    "more": {
                        "player": 0,
                        "itemsParams": "commonParams",
                        "params": {"menu": 1, "cmd": "add"},
                        "cmd": ["playlistcontrol"],
                    },
                    "play": {
                        "cmd": ["playlistcontrol"],
                        "itemsParams": "commonParams",
                        "params": {"menu": 1, "cmd": "play"},
                        "player": 0,
                        "nextWindow": "nowPlaying",
                    },
                    "play-hold": {
                        "cmd": ["playlistcontrol"],
                        "itemsParams": "commonParams",
                        "params": {"menu": 1, "cmd": "load"},
                        "player": 0,
                        "nextWindow": "nowPlaying",
                    },
                    "add-hold": {
                        "itemsParams": "commonParams",
                        "params": {"menu": 1, "cmd": "insert"},
                        "player": 0,
                        "cmd": ["playlistcontrol"],
                    },
                }
            },
            "window": {"windowStyle": "icon_list"},
            "item_loop": [
                {
                    **menu_item_from_media_item(self.mass, item, include_actions=True),
                    "presetParams": {
                        "favorites_title": item.name,
                        "favorites_url": item.uri,
                        "favorites_type": item.media_type.value,
                        "icon": self.mass.metadata.get_image_url(item.image, 256)
                        if item.image
                        else "",
                    },
                    "textkey": item.name[0].upper(),
                    "commonParams": {
                        "uri": item.uri,
                        "noEdit": 1,
                        f"{item.media_type.value}_id": item.item_id,
                    },
                }
                for item in items
            ],
            "offset": offset,
            "count": len(items),
        }

    def _handle_menustatus(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle menustatus request from CLI."""
        return None

    def _handle_displaystatus(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle displaystatus request from CLI."""
        return None

    def _handle_date(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle date request from CLI."""
        return {"date_epoch": int(time.time()), "date": "0000-00-00T00:00:00+00:00"}

    async def _on_mass_event(self, event: MassEvent) -> None:
        """Forward ."""
        player_id = event.object_id
        if not player_id:
            return
        for client in self._cometd_clients.values():
            if sub := client.slim_subscriptions.get(
                f"/{client.client_id}/slim/playerstatus/{player_id}"
            ):
                self._handle_cometd_request(client, sub)

    async def _do_periodic(self) -> None:
        """Execute periodic sending of state and cleanup."""
        while True:
            # cleanup orphaned clients
            disconnected_clients = set()
            for cometd_client in self._cometd_clients.values():
                if (time.time() - cometd_client.last_seen) > 80:
                    disconnected_clients.add(cometd_client.client_id)
                    continue
            for clientid in disconnected_clients:
                client = self._cometd_clients.pop(clientid)
                empty_queue(client.queue)
                self.logger.debug("Cleaned up disconnected CometD Client: %s", clientid)
            # handle client subscriptions
            for cometd_client in self._cometd_clients.values():
                for sub in cometd_client.slim_subscriptions.values():
                    self._handle_cometd_request(cometd_client, sub)

            await asyncio.sleep(60)

    async def _get_preset_items(self, player_id: str) -> list[tuple[int, SlimMenuItem]]:
        """Return all presets for a player."""
        preset_items: list[tuple[int, MediaItemType]] = []
        for preset_index in range(1, 100):
            if preset_conf := self.mass.config.get_raw_player_config_value(
                player_id, f"preset_{preset_index}"
            ):
                with contextlib.suppress(MusicAssistantError):
                    media_item = await self.mass.music.get_item_by_uri(preset_conf)
                    slim_media_item = menu_item_from_media_item(self.mass, media_item, True)
                    preset_items.append((preset_index, slim_media_item))
            else:
                break
        return preset_items


def dict_to_strings(source: dict) -> list[str]:
    """Convert dict to key:value strings (used in slimproto cli)."""
    result: list[str] = []

    for key, value in source.items():
        if value in (None, ""):
            continue
        if isinstance(value, list):
            for subval in value:
                if isinstance(subval, dict):
                    result += dict_to_strings(subval)
                else:
                    result.append(str(subval))
        elif isinstance(value, dict):
            result += dict_to_strings(value)
        else:
            result.append(f"{key}:{str(value)}")
    return result
