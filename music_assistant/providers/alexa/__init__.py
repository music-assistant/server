"""Alexa player provider support for Music Assistant."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from alexapy import (
    AlexaAPI,
    AlexaLogin,
)
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.models.player_provider import PlayerProvider

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        PlayerConfig,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_URL = "url"

SUPPORTED_FEATURES = {ProviderFeature.UNKNOWN}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AlexaProvider(mass, manifest, config)


async def get_config_entries() -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(key=CONF_URL, type=ConfigEntryType.STRING, label="URL", required=True),
        ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, label="E-Mail", required=True),
        ConfigEntry(
            key=CONF_PASSWORD, type=ConfigEntryType.STRING, label="Password", required=True
        ),
    )


class AlexaProvider(PlayerProvider):
    """Implementation of an Alexa Device Provider."""

    class AlexaDevice:
        """Representation of an Alexa Device."""

        _device_type: str
        device_serial_number: str
        _device_family: str
        _cluster_members: str
        _locale: str

        async def createobject(self, player_id: str, login: AlexaLogin) -> None:
            """Initialize Alexa Device."""
            devices = await AlexaAPI.get_devices(login)

            for device in devices:
                if device["accountName"] == player_id:
                    self._device_type = device["deviceType"]
                    self.device_serial_number = device["serialNumber"]
                    self._device_family = device["deviceOwnerCustomerId"]
                    self._cluster_members = device["clusterMembers"]
                    self._locale = "en-US"

    login: AlexaLogin

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.login = AlexaLogin(
            url=self.config.get_value(CONF_URL),
            email=self.config.get_value(CONF_USERNAME),
            password=self.config.get_value(CONF_PASSWORD),
            outputpath=lambda x: x,
        )

        self.login._cookiefile = [
            self.login._outputpath(
                f"/home/user/music-assistant_server/music_assistant/providers/alexa/alexa_media.{self.config.get_value(CONF_USERNAME)}.pickle"
            ),
        ]

        await self.login.login(cookies=await self.login.load_cookie())

        devices = await AlexaAPI.get_devices(self.login)

        for device in devices:
            if device.get("capabilities") and "MUSIC_SKILL" in device.get("capabilities"):
                dev_name = device["accountName"]
                player_id = dev_name
                player = Player(
                    player_id=player_id,
                    provider=self.instance_id,
                    type=PlayerType.PLAYER,
                    name=player_id,
                    available=True,
                    powered=False,
                    device_info=DeviceInfo(),
                    supported_features={PlayerFeature.VOLUME_SET},
                )
                await self.mass.players.register_or_update(player)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
        )

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return

        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.stop()

        player.state = PlayerState.IDLE
        self.mass.players.update(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return

        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.play()

        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return

        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.pause()

        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return

        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.set_volume(volume_level / 100)

        player.volume_level = volume_level
        self.mass.players.update(player_id)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
            return

        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.set_volume(0)

        player.volume_level = 0
        self.mass.players.update(player_id)

    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given queue.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Players controller to start playing a mediaitem on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - media: Details of the item that needs to be played on the player.
        """
        if not (player := self.mass.players.get(player_id)):
            return

        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.run_custom("Ask music assistant to play audio")

        player.current_media = media
        player.elapsed_time = 0
        player.elapsed_time_last_updated = time.time()
        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """
        Handle enqueuing of the next (queue) item on the player.

        Called when player reports it started buffering a queue item
        and when the queue items updated.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.
        """
        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.next()

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is grouped to.

            - player_id: player_id of the player to handle the command.
        """

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
