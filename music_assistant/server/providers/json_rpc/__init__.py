"""JSON-RPC API which is more or less compatible with Logitech Media Server."""
from __future__ import annotations

from typing import Any

from aiohttp import web

from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.models.enums import PlayerState
from music_assistant.server.models.plugin import PluginProvider

from .models import (
    CommandErrorMessage,
    CommandMessage,
    CommandResultMessage,
    PlayerItem,
    PlayersResponse,
    PlayerStatusResponse,
    player_item_from_mass,
    player_status_from_mass,
)

# ruff: noqa: ARG002, E501

ArgsType = list[int | str]
KwargsType = dict[str, Any]


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


class JSONRPCApi(PluginProvider):
    """Basic JSON-RPC API implementation, (partly) compatible with Logitech Media Server."""

    async def setup(self) -> None:
        """Handle async initialization of the plugin."""
        self.mass.webapp.router.add_get("/jsonrpc.js", self._handle_jsonrpc)
        self.mass.webapp.router.add_post("/jsonrpc.js", self._handle_jsonrpc)

    async def _handle_jsonrpc(self, request: web.Request) -> web.Response:
        """Handle request for image proxy."""
        command_msg: CommandMessage = await request.json(loads=json_loads)
        self.logger.debug("Received request: %s", command_msg)

        if command_msg["method"] == "slim.request":
            # Slim request handler
            # {"method":"slim.request","id":1,"params":["aa:aa:ca:5a:94:4c",["status","-", 2, "tags:xcfldatgrKN"]]}
            player_id = command_msg["params"][0]
            command = str(command_msg["params"][1][0])
            args, kwargs = parse_args(command_msg["params"][1][1:])

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
                if cmd_result is None:
                    cmd_result = {}
                elif not isinstance(cmd_result, dict):
                    # individual values are returned with underscore ?!
                    cmd_result = {f"_{command}": cmd_result}
                result: CommandResultMessage = {
                    **command_msg,
                    "result": cmd_result,
                }
            else:
                # no handler found
                self.logger.warning("No handler for %s", command)
                result: CommandErrorMessage = {
                    **command_msg,
                    "error": {"code": -1, "message": "Invalid command"},
                }
            # return the response to the client
            return web.json_response(result, dumps=json_dumps)

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

    def _handle_status(
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
        queue_items = self.mass.players.queues.items(queue.queue_id)[
            start_index : start_index + limit
        ]
        # we ignore the tags, just always send all info
        return player_status_from_mass(player=player, queue=queue, queue_items=queue_items)

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
