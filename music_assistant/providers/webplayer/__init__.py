"""Webplayer support."""
import logging
import time
from typing import List

from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import Player, PlayerState
from music_assistant.models.playerprovider import PlayerProvider
from music_assistant.utils import run_periodic

PROV_ID = "webplayer"
PROV_NAME = "WebPlayer"
LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []

EVENT_WEBPLAYER_CMD = "webplayer command"
EVENT_WEBPLAYER_STATE = "webplayer state"
EVENT_WEBPLAYER_REGISTER = "webplayer register"


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = WebPlayerProvider()
    await mass.async_register_provider(prov)


class WebPlayerProvider(PlayerProvider):
    """
    Implementation of a player using pure HTML/javascript.

    Used in the front-end.
    Communication is handled through the websocket connection
    and our internal event bus.
    """

    _players = {}

    ### Provider specific implementation #####

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return CONFIG_ENTRIES

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        self.mass.add_event_listener(
            self.async_handle_mass_event,
            [EVENT_WEBPLAYER_STATE, EVENT_WEBPLAYER_REGISTER],
        )
        self.mass.add_job(self.async_check_players())

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit. Called on shutdown."""
        # nothing to do ?

    async def async_handle_mass_event(self, msg, msg_details):
        """Handle received event for the webplayer component."""
        if msg == EVENT_WEBPLAYER_REGISTER:
            # register new player
            player_id = msg_details["player_id"]
            player = Player(
                player_id=player_id,
                provider_id=PROV_ID,
                name=msg_details["name"],
                powered=True,
            )
            await self.mass.player_manager.async_add_player(player)

        elif msg == EVENT_WEBPLAYER_STATE:
            await self.__async_handle_player_state(msg_details)

    @run_periodic(30)
    async def async_check_players(self) -> None:
        """Invalidate players that did not send a heartbeat message in a while."""
        cur_time = time.time()
        offline_players = []
        for player in self._players.values():
            if cur_time - player.last_message > 30:
                offline_players.append(player.player_id)
        for player_id in offline_players:
            await self.mass.player_manager.async_remove_player(player_id)
            self._players.pop(player_id, None)

    async def async_cmd_stop(self, player_id: str) -> None:
        """Send stop command to player."""
        data = {"player_id": player_id, "cmd": "stop"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_play(self, player_id: str) -> None:
        """Send play command to player."""
        data = {"player_id": player_id, "cmd": "play"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_pause(self, player_id: str):
        """Send pause command to player."""
        data = {"player_id": player_id, "cmd": "pause"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_power_on(self, player_id: str) -> None:
        """Send power ON command to player."""
        self._players[player_id].powered = True  # not supported on webplayer
        data = {"player_id": player_id, "cmd": "stop"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_power_off(self, player_id: str) -> None:
        """Send power OFF command to player."""
        await self.async_cmd_stop(player_id)
        self._players[player_id].powered = False

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send new volume level command to player."""
        data = {
            "player_id": player_id,
            "cmd": "volume_set",
            "volume_level": volume_level,
        }
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """Send mute command to player."""
        data = {"player_id": player_id, "cmd": "volume_mute", "is_muted": is_muted}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """Play single uri on player."""
        data = {"player_id": player_id, "cmd": "play_uri", "uri": uri}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def __async_handle_player_state(self, data):
        """Handle state event from player."""
        player_id = data["player_id"]
        player = self._players[player_id]
        if "volume_level" in data:
            player.volume_level = data["volume_level"]
        if "muted" in data:
            player.muted = data["muted"]
        if "state" in data:
            player.state = PlayerState(data["state"])
        if "cur_time" in data:
            player.elapsed_time = data["elapsed_time"]
        if "current_uri" in data:
            player.current_uri = data["current_uri"]
        if "powered" in data:
            player.powered = data["powered"]
        if "name" in data:
            player.name = data["name"]
        player.last_message = time.time()
        self.mass.add_job(self.mass.player_manager.async_update_player(player))
