"""Builtin player provider."""
import logging
import time
from typing import List

from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import run_periodic
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import (
    DeviceInfo,
    PlaybackState,
    Player,
    PlayerFeature,
)
from music_assistant.models.provider import PlayerProvider

PROV_ID = "builtin_player"
PROV_NAME = "Music Assistant"
LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []
PLAYER_CONFIG_ENTRIES = []
PLAYER_FEATURES = []

EVENT_WEBPLAYER_CMD = "webplayer command"
EVENT_WEBPLAYER_STATE = "webplayer state"
EVENT_WEBPLAYER_REGISTER = "webplayer register"


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = MassPlayerProvider()
    await mass.async_register_provider(prov)


class MassPlayerProvider(PlayerProvider):
    """
    Built-in PlayerProvider.

    Provides virtual players in the frontend using websockets.
    """

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
        return []

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        # listen for websockets events to dynamically create players
        self.mass.add_event_listener(
            self.async_handle_mass_event,
            [EVENT_WEBPLAYER_STATE, EVENT_WEBPLAYER_REGISTER],
        )
        self.mass.add_job(self.async_check_players())
        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for player in self.players:
            await player.async_cmd_stop()

    async def async_handle_mass_event(self, msg, msg_details):
        """Handle received event for the webplayer component."""
        player = self.mass.players.get_player(msg_details["player_id"])
        if not player:
            # register new player
            player = WebsocketsPlayer(
                self.mass, msg_details["player_id"], msg_details["name"]
            )
            await self.mass.players.async_add_player(player)
        await player.handle_player_state(msg_details)

    @run_periodic(30)
    async def async_check_players(self) -> None:
        """Invalidate players that did not send a heartbeat message in a while."""
        cur_time = time.time()
        offline_players = []
        for player in self.players:
            if not isinstance(player, WebsocketsPlayer):
                continue
            if cur_time - player.last_message > 30:
                offline_players.append(player.player_id)
        for player_id in offline_players:
            await self.mass.players.async_remove_player(player_id)


class WebsocketsPlayer(Player):
    """
    Implementation of a player using pure HTML/javascript.

    Used in the front-end.
    Communication is handled through the websocket connection
    and our internal event bus.
    """

    def __init__(self, mass: MusicAssistantType, player_id: str, player_name: str):
        """Initialize the webplayer."""
        self._player_id = player_id
        self._player_name = player_name
        self._powered = True
        self._elapsed_time = 0
        self._state = PlaybackState.Stopped
        self._current_uri = ""
        self._volume_level = 100
        self._muted = False
        self._device_info = DeviceInfo()
        self.last_message = time.time()

    async def handle_player_state(self, data: dict):
        """Handle state event from player."""
        if "volume_level" in data:
            self._volume_level = data["volume_level"]
        if "muted" in data:
            self._muted = data["muted"]
        if "state" in data:
            self._state = PlaybackState(data["state"])
        if "elapsed_time" in data:
            self._elapsed_time = data["elapsed_time"]
        if "current_uri" in data:
            self._current_uri = data["current_uri"]
        if "name" in data:
            self._player_name = data["name"]
        if "device_info" in data:
            for key, value in data["device_info"].items():
                setattr(self._device_info, key, value)
        self.last_message = time.time()
        self.update_state()

    @property
    def player_id(self) -> str:
        """Return player id of this player."""
        return self._player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return name of the player."""
        return self._player_name

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._powered

    @property
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        return self._elapsed_time

    @property
    def state(self) -> PlaybackState:
        """Return current PlaybackState of player."""
        return self._state

    @property
    def current_uri(self) -> str:
        """Return currently loaded uri of player (if any)."""
        return self._current_uri

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._volume_level

    @property
    def muted(self) -> bool:
        """Return current mute state of player."""
        return self._muted

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for this player."""
        return self._device_info

    @property
    def should_poll(self) -> bool:
        """Return True if this player should be polled for state updates."""
        return False

    @property
    def features(self) -> List[PlayerFeature]:
        """Return list of features this player supports."""
        return PLAYER_FEATURES

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return player specific config entries (if any)."""
        return PLAYER_CONFIG_ENTRIES

    async def async_cmd_play_uri(self, uri: str) -> None:
        """
        Play the specified uri/url on the player.

            :param uri: uri/url to send to the player.
        """
        data = {"player_id": self.player_id, "cmd": "play_uri", "uri": uri}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_stop(self) -> None:
        """Send STOP command to player."""
        data = {"player_id": self.player_id, "cmd": "stop"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_play(self) -> None:
        """Send PLAY command to player."""
        data = {"player_id": self.player_id, "cmd": "play"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        data = {"player_id": self.player_id, "cmd": "pause"}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        self._powered = True
        self.update_state()

    async def async_cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        await self.async_cmd_stop()
        self._powered = False
        self.update_state()

    async def async_cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to player.

            :param volume_level: volume level to set (0..100).
        """
        data = {
            "player_id": self.player_id,
            "cmd": "volume_set",
            "volume_level": volume_level,
        }
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)

    async def async_cmd_volume_mute(self, is_muted: bool = False) -> None:
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        data = {"player_id": self.player_id, "cmd": "volume_mute", "is_muted": is_muted}
        self.mass.signal_event(EVENT_WEBPLAYER_CMD, data)
