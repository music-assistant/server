"""
DEMO/TEMPLATE Player Provider for Music Assistant.

This is an empty player provider with no actual implementation.
Its meant to get started developing a new player provider for Music Assistant.

Use it as a reference to discover what methods exists and what they should return.
Also it is good to look at existing player providers to get a better understanding,
due to the fact that providers may be flexible and support different features and/or
ways to discover players on the network.

In general, the actual device communication should reside in a separate library.
You can then reference your library in the manifest in the requirements section,
which is a list of (versioned!) python modules (pip syntax) that should be installed
when the provider is selected by the user.

To add a new player provider to Music Assistant, you need to create a new folder
in the providers folder with the name of your provider (e.g. 'my_player_provider').
In that folder you should create (at least) a __init__.py file and a manifest.json file.

Optional is an icon.svg file that will be used as the icon for the provider in the UI,
but we also support that you specify a material design icon in the manifest.json file.

IMPORTANT NOTE:
We strongly recommend developing on either MacOS or Linux and start your development
environment by running the setup.sh scripts in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.
See also our general DEVELOPMENT.md guide in the repository for more information.

"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from alexapy import (
    AlexaAPI,
    AlexaLogin,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
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
    CONF_ENTRY_ENFORCE_MP3_DEFAULT_ENABLED,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.models.player_provider import PlayerProvider

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigValueType,
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
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    #  an instance of the provider.
    return AlexaProvider(mass, manifest, config)


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
    # Config Entries are used to configure the Player Provider if needed.
    # See the models of ConfigEntry and ConfigValueType for more information what is supported.
    # The ConfigEntry is a dataclass that represents a single configuration entry.
    # The ConfigValueType is an Enum that represents the type of value that
    # can be stored in a ConfigEntry.
    # If your provider does not need any configuration, you can return an empty tuple.
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

    # async def unload(self) -> None:
    #     """
    #     Handle unload/close of the provider.

    #     Called when provider is deregistered (e.g. MA exiting or config reloading).
    #     """
    #     # OPTIONAL
    #     # this is an optional method that you can implement if
    #     # relevant or leave out completely if not needed.
    #     # it will be called when the provider is unloaded from Music Assistant.
    #     # this means also when the provider is getting reloaded

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # OPTIONAL
        # this method is optional and should be implemented if you need player specific
        # configuration entries. If you do not need player specific configuration entries,
        # you can leave this method out completely to accept the default implementation.
        # Please note that you need to call the super() method to get the default entries.
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            CONF_ENTRY_ENFORCE_MP3_DEFAULT_ENABLED,
        )

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        # OPTIONAL
        # this will be called whenever a player config changes
        # you can use this to react to changes in player configuration
        # but this is completely optional and you can leave it out if not needed.

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
        # OPTIONAL - required only if you specified PlayerFeature.SEEK
        # this method should handle the seek command for the given player.
        # the position is the position in seconds to seek to in the current playing item.

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
        # MANDATORY
        # this method should handle the play_media command for the given player.
        # It will be called when media needs to be played on the player.
        # The media object contains all the details needed to play the item.

        # In 99% of the cases this will be called by the Queue controller to play
        # a single item from the queue on the player and the uri within the media
        # object will then contain the URL to play that single queue item.

        # If your player provider does not support enqueuing of items,
        # the queue controller will simply call this play_media method for
        # each item in the queue to play them one by one.

        # In order to support true gapless and/or crossfade, we offer the option of
        # 'flow_mode' playback. In that case the queue controller will stitch together
        # all songs in the playback queue into a single stream and send that to the player.
        # In that case the URI (and metadata) received here is that of the 'flow mode' stream.

        # Examples of player providers that use flow mode for playback by default are Airplay,
        # SnapCast and Fully Kiosk.

        # Examples of player providers that optionally use 'flow mode' are Google Cast and
        # Home Assistant. They provide a config entry to enable flow mode playback.

        # Examples of player providers that natively support enqueuing of items are Sonos,
        # Slimproto and Google Cast.
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
        # this method should handle the enqueuing of the next queue item on the player.
        # if not (player := self.mass.players.get(player_id)):
        #     return
        device_object = self.AlexaDevice()
        await device_object.createobject(player_id, self.login)
        api = AlexaAPI(device_object, self.login)
        await api.next()
        # await AlexaAPI.next(player_id)

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        # OPTIONAL - required only if you specified ProviderFeature.SYNC_PLAYERS
        # this method should handle the sync command for the given player.
        # you should join the given player to the target_player/syncgroup.

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is grouped to.

            - player_id: player_id of the player to handle the command.
        """

    async def play_announcement(
        self, player_id: str, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        # OPTIONAL - required only if you specified PlayerFeature.PLAY_ANNOUNCEMENT
        # This method should handle the playback of an announcement on the given player.
        # The announcement object contains all the details needed to play the announcement.
        # The volume_level is optional and can be used to set the volume level for the announcement.
        # If you do not use the announcement playerfeature, the default behavior is to play the
        # announcement as a regular media item using the play_media method and the MA player manager
        # will take care of setting the volume level for the announcement and resuming etc.

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        # OPTIONAL
        # This method is optional and should be implemented if you specified 'needs_poll'
        # on the Player object. This method should poll the player for state changes
        # and update the player object in the player manager if needed.
        # This method will be called at the interval specified in the poll_interval attribute.
