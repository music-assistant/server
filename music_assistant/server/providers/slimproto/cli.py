"""JSON-RPC API which is more or less compatible with Logitech Media Server."""
from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

import shortuuid
from aiohttp import web

from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.helpers.util import select_free_port
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import PlayerState

from .models import (
    CometDResponse,
    CommandErrorMessage,
    CommandMessage,
    PlayerItem,
    PlayersResponse,
    PlayerStatusResponse,
    ServerStatusResponse,
    player_item_from_mass,
    player_status_from_mass,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

    from . import SlimprotoProvider


# ruff: noqa: ARG002, E501

ArgsType = list[int | str]
KwargsType = dict[str, Any]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = LmsCli(mass, manifest, config)
    await prov.handle_setup()
    return prov


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
            key, val = raw_value.split(":")
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

    def __init__(self, slimproto: SlimprotoProvider) -> None:
        """Initialize."""
        self.slimproto = slimproto
        self.logger = self.slimproto.logger.getChild("cli")
        self.mass = self.slimproto.mass
        self._cometd_clients: dict[str, asyncio.Queue[CometDResponse]] = {}
        self._player_map: dict[str, str] = {}

    async def setup(self) -> None:
        """Handle async initialization of the plugin."""
        self.logger.info("Registering jsonrpc endpoints on the webserver")
        self.mass.webserver.register_route("/jsonrpc.js", self._handle_jsonrpc)
        self.mass.webserver.register_route("/cometd", self._handle_cometd)
        # setup (telnet) cli for players requesting basic info on that port
        self.cli_port = await select_free_port(9090, 9190)
        self.logger.info("Starting (telnet) CLI on port %s", self.cli_port)
        await asyncio.start_server(self._handle_cli_client, "0.0.0.0", self.cli_port)

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        self.mass.webserver.unregister_route("/jsonrpc.js")

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
        cmd_result = await self._handle_command(command_msg["params"][0], command_msg["params"][1])
        if cmd_result is None:
            result: CommandErrorMessage = {
                **command_msg,
                "error": {"code": -1, "message": "Invalid command"},
            }
        else:
            result: CommandErrorMessage = {
                **command_msg,
                "error": {"code": -1, "message": "Invalid command"},
            }
        # return the response to the client
        return web.json_response(result, dumps=json_dumps)

    async def _handle_cometd(self, request: web.Request) -> web.Response:
        """
        Handle CometD request on the json CLI.

        https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Web/Cometd.pm
        """
        # ruff: noqa: PLR0915
        responses = []
        is_subscribe_connection = False
        clientid = ""
        json_msg: list[dict[str, Any]] = await request.json()
        # cometd message is an array of commands/messages
        for cometd_msg in json_msg:
            channel = cometd_msg.get("channel")
            # try to figure out clientid
            if not clientid and cometd_msg.get("clientId"):
                clientid = cometd_msg["clientId"]
            elif not clientid and cometd_msg.get("data", {}).get("response"):
                clientid = cometd_msg["data"]["response"].split("/")[1]
            # messageid is optional but if provided we must pass it along
            msgid = cometd_msg.get("id", "")
            response = {
                "channel": channel,
                "successful": True,
                "id": msgid,
                "clientId": clientid,
            }
            self._cometd_clients.setdefault(clientid, asyncio.Queue())

            if channel == "/meta/handshake":
                # handshake message
                response["version"] = "1.0"
                response["clientId"] = shortuuid.uuid()
                response["supportedConnectionTypes"] = ["streaming"]
                response["advice"] = {
                    "reconnect": "retry",
                    "timeout": 60000,
                    "interval": 0,
                }

            elif channel in ("/meta/connect", "/meta/reconnect"):
                # (re)connect message
                self.logger.debug("CometD Client (re-)connected: %s", clientid)
                response["timestamp"] = time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime())
                response["advice"] = {"interval": 5000}

            elif channel == "/meta/subscribe":
                is_subscribe_connection = True
                response["subscription"] = cometd_msg.get("subscription")

            elif channel in ("/slim/request", "/slim/subscribe"):
                # async request (similar to json rpc call)
                result = await self._handle_command(
                    cometd_msg["data"]["request"][0], cometd_msg["data"]["request"][1]
                )
                # create mapping table which client is subscribing to which player
                if player_id := cometd_msg["data"]["request"][0]:
                    # create mapping table which client is subscribing to which player
                    self._player_map[clientid] = player_id

                # the result is posted on the client queue
                await self._cometd_clients[clientid].put(
                    {
                        "channel": cometd_msg["data"]["response"],
                        "id": msgid,
                        "data": result,
                        "ext": {"priority": ""},
                    }
                )
            else:
                self.logger.warning("Unhandled CometD channel %s", channel)

            # always reply with the (default) response to every message
            responses.append(response)

        # regular command/handshake messages are just replied and connection closed
        if not is_subscribe_connection:
            return web.json_response(responses)

        # the subscription connection is kept open and events are streamed to the client
        resp = web.StreamResponse(
            status=200,
            reason="OK",
        )
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        await resp.write(json_dumps(responses).encode("iso-8859-1"))

        # as some sort of heartbeat, the server- and playerstatus is sent every 30 seconds
        loop = asyncio.get_running_loop()

        async def send_status():
            if clientid not in self._cometd_clients:
                return

            player = self.mass.players.get(self._player_map.get(clientid))
            await self._cometd_clients[clientid].put(
                {
                    "channel": f"/{clientid}/slim/serverstatus",
                    "id": "1",
                    "data": await self._handle_serverstatus("", []),
                }
            )
            if player is not None:
                await self._cometd_clients[clientid].put(
                    {
                        "channel": f"/{clientid}/slim/status",
                        "id": "1",
                        "data": await self._handle_status(player.player_id, []),
                    }
                )

            # reschedule self
            loop.call_later(30, loop.create_task, send_status())

        loop.create_task(send_status())

        # keep delivering messages to the client until it disconnects
        try:
            # keep sending messages/events from the client's queue
            while clientid in self._cometd_clients and not resp.task.done():
                msg = await self._cometd_clients[clientid].get()
                # make sure we always send an array of messages
                data = json_dumps([msg]).encode("iso-8859-1")
                await resp.write(data)
        except ConnectionResetError:
            pass
        finally:
            self._cometd_clients.pop(clientid, None)
            self.logger.debug("CometD Client disconnected: %s", clientid)
        return resp

    async def _handle_command(self, player_id: str, params: list[str | int]) -> Any:
        """Handle command for either JSON or CometD request."""
        # Slim request handler
        # {"method":"slim.request","id":1,"params":["aa:aa:ca:5a:94:4c",["status","-", 2, "tags:xcfldatgrKN"]]}
        command = str(params[0])
        args, kwargs = parse_args(params[1:])
        if handler := getattr(self, f"_handle_{command}", None):
            # run handler for command
            self.logger.debug(
                "Handling JSON-RPC-request (player: %s command: %s - args: %s - kwargs: %s)",
                player_id,
                command,
                str(args),
                str(kwargs),
            )
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
        self.logger.warning("No handler for %s", str(params))
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
        *args,
        start_index: int | str = "-",
        limit: int = 2,
        tags: str = "xcfldatgrKN",
        **kwargs,
    ) -> PlayerStatusResponse:
        """Handle player status command."""
        player = self.mass.players.get(player_id)
        assert player is not None
        queue = self.mass.players.queues.get_active_queue(player_id)
        assert queue is not None
        if start_index == "-":
            start_index = queue.current_index or 0
        queue_items = []
        index = 0
        async for item in self.mass.players.queues.items(queue.queue_id):
            if index >= start_index:
                queue_items.append(item)
            if len(queue_items) == limit:
                break
            index += 1
        # we ignore the tags, just always send all info
        return player_status_from_mass(
            self.mass, player=player, queue=queue, queue_items=queue_items
        )

    async def _handle_serverstatus(
        self,
        player_id: str,
        *args,
        start_index: int = 0,
        limit: int = 2,
        tags: str = "xcfldatgrKN",
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle server status command."""
        players: list[PlayerItem] = []
        for index, mass_player in enumerate(self.mass.players.all()):
            if isinstance(start_index, int) and index < start_index:
                continue
            if len(players) > limit:
                break
            players.append(player_item_from_mass(start_index + index, mass_player))
        return ServerStatusResponse(
            {
                "ip": self.mass.base_ip,
                "httpport": str(self.mass.webserver.port),
                "version": "7.9",
                "uuid": self.mass.server_id,
                # TODO: set these vars ?
                "info total duration": 0,
                "info total genres": 0,
                "sn player count": 0,
                "lastscan": "0",
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
        return {"firmwareUpgrade": 0}

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

        if isinstance(number, str) and "+" in number or "-" in number:
            jump = int(number.split("+")[1])
            self.mass.create_task(self.mass.players.queues.skip, jump)
        else:
            self.mass.create_task(self.mass.players.queues.seek, number)

    def _handle_power(self, player_id: str, value: str | int) -> int | None:
        """Handle player `time` command."""
        # <playerid> power <0|1|?|>
        # The "power" command turns the player on or off.
        # Use 0 to turn off, 1 to turn on, ? to query and
        # no parameter to toggle the power state of the player.
        player = self.mass.players.get(player_id)
        assert player is not None

        if value == "?":
            return int(player.powered)

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

        self.logger.warning("Unhandled command: playlist/%s", subcommand)

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
        assert queue is not None
        if subcommand == "fwd":
            self.mass.create_task(self.mass.players.queues.next, player_id)
            return
        if subcommand == "rew":
            self.mass.create_task(self.mass.players.queues.previous, player_id)
            return
        self.logger.warning(
            "No handler for button/%s (player: %s - args: %s - kwargs: %s)",
            subcommand,
            player_id,
            str(args),
            str(kwargs),
        )


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
            result += dict_to_strings(subval)
        else:
            result.append(f"{key}:{str(value)}")
    return result
