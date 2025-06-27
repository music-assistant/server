"""
DEMO/TEST/DUMMY/TEMPLATE Player Provider for Music Assistant.

This is an empty player provider with a test/demo implementation.
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
We strongly recommend developing on either macOS or Linux and start your development
environment by running the setup.sh scripts in the scripts folder of the repository.
This will create a virtual environment and install all dependencies needed for development.

For all development instructions, please refer to the developer documentation:
https://developers.music-assistant.io
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.player import PlayerSource
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_NUMBER_OF_PLAYERS = "number_of_players"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # setup is called when the user wants to setup a new provider instance.
    # you are free to do any preflight checks here and but you must return
    # an instance of your provider.
    return DemoPlayerprovider(mass, manifest, config)


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
        # example of a ConfigEntry for the number of players to create
        ConfigEntry(
            key=CONF_NUMBER_OF_PLAYERS,
            type=ConfigEntryType.INTEGER,
            label="Number of Players",
            required=True,
            default_value="2",
            description="Number of demo players to create.",
        ),
    )


class DemoPlayerprovider(PlayerProvider):
    """
    Example/demo Player provider.

    Note that this is always subclassed from PlayerProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some convenience methods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases its not needed to override any of the builtin methods and you only
    implement the abc methods with your actual implementation.
    """

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # MANDATORY
        # you should return a set of provider-level (optional) features
        # here that your player provider supports or an empty set if none.
        # for example 'ProviderFeature.SYNC_PLAYERS' if you can sync players.
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is initialized in Music Assistant.
        # you can use this to do any async initialization of the provider,
        # such as loading configuration, setting up connections, etc.
        self.logger.info("Initializing DemoPlayerProvider with config: %s", self.config)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        # you can use this for instance to trigger custom (non-mdns) discovery of players
        # or any other logic that needs to run after the provider is fully loaded.
        self.logger.info("DemoPlayerProvider loaded")
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is unloaded from Music Assistant.
        # this means also when the provider is getting reloaded
        for player in self.players:
            # if you have any cleanup logic for the players, you can do that here.
            # e.g. disconnecting from the player, closing connections, etc.
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is enabled.

    def on_player_disabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets disabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is disabled.
        # e.g. you can stop polling the player or disconnect from it.

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        # OPTIONAL - required only if you specified ProviderFeature.REMOVE_PLAYER
        # this is used to actually remove a player.

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        # MANDATORY IF YOU WANT TO USE MDNS DISCOVERY
        # OPTIONAL if you dont use mdns for discovery of players
        # If you specify a mdns service type in the manifest.json, this method will be called
        # automatically on mdns changes for the specified service type.

        # If no mdns service type is specified, this method is omitted and you
        # can completely remove it from your provider implementation.

        if not info:
            return

        # NOTE: If you do not use mdns for discovery of players on the network,
        # you must implement your own discovery mechanism and logic to add new players
        # and update them on state changes when needed.
        # Below is a bit of example implementation but we advise to look at existing
        # player providers for more inspiration.
        name = name.split("@", 1)[1] if "@" in name else name
        player_id = info.decoded_properties["uuid"]  # this is just an example!

        if not player_id:
            return

        # handle removed player
        if state_change == ServiceStateChange.Removed:
            # check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(player_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                mass_player.available = False
                self.mass.players.update(player_id)
            return
        # handle update for existing device
        # (state change is either updated or added)
        # check if we have an existing player in the player manager
        # note that you can use this point to update the player connection info
        # if that changed (e.g. ip address)
        if mass_player := self.mass.players.get(player_id):
            # existing player found in the player manager,
            # this is an existing player that has been updated/reconnected
            # or simply a re-announcement on mdns.
            cur_address = get_primary_ip_address_from_zeroconf(info)
            if cur_address and cur_address != mass_player.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for player %s", cur_address, mass_player.display_name
                )
                mass_player.device_info = DeviceInfo(
                    model=mass_player.device_info.model,
                    manufacturer=mass_player.device_info.manufacturer,
                    ip_address=str(cur_address),
                )
            if not mass_player.available:
                # if the player was marked offline and you now receive an mdns update
                # it means the player is back online and we should try to connect to it
                self.logger.debug("Player back online: %s", mass_player.display_name)
                # you can try to connect to the player here if needed
                mass_player.available = True
            # inform the player manager of any changes to the player object
            # note that you would normally call this from some other callback from
            # the player's native api/library which informs you of changes in the player state.
            # as a last resort you can also choose to let the player manager
            # poll the player for state changes
            self.mass.players.update(player_id)
            return
        # handle new player
        self.logger.debug("Discovered device %s on %s", name, cur_address)
        # your own connection logic will probably be implemented here where
        # you connect to the player etc. using your device/provider specific library.

        # Instantiate the MA Player object and register it with the player manager
        mass_player = Player(
            player_id=player_id,
            provider=self.instance_id,
            type=PlayerType.PLAYER,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(
                model="Model XYX",
                manufacturer="Super Brand",
                ip_address=cur_address,
            ),
            # set the supported features for this player only with
            # the ones the player actually supports
            supported_features={
                PlayerFeature.POWER,  # if the player can be turned on/off
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.PLAY_ANNOUNCEMENT,  # see play_announcement method
            },
        )
        # register the player with the player manager
        await self.mass.players.register(mass_player)

        # once the player is registered, you can either instruct the player manager to
        # poll the player for state changes or you can implement your own logic to
        # listen for state changes from the player and update the player object accordingly.
        # in any case, you need to call the update method on the player manager:
        self.mass.players.update(player_id)

    async def discover_players(self) -> None:
        """Discover players for this provider."""
        # This is an optional method that you can implement if
        # you want to (manually) discover players on the
        # network and you do not use mdns discovery.
        number_of_players = int(self.config.get_value(CONF_NUMBER_OF_PLAYERS))
        self.logger.info(
            "Discovering %s demo players",
            number_of_players,
        )
        for i in range(number_of_players):
            player = DemoPlayer(
                provider=self,
                player_id=f"demo_{i}",
            )
            # register the player with the player manager
            await self.mass.players.register(player)
            # once the player is registered, you can either instruct the player manager to
            # poll the player for state changes or you can implement your own logic to
            # listen for state changes from the player and update the player object accordingly.
            # if the player state needs to be updated, you can call the update method on the player:
            # player.update_state()


class DemoPlayer(Player):
    """DemoPlayer in Music Assistant."""

    @property
    def type(self) -> PlayerType:
        """Return the type of the player."""
        # MANDATORY
        # this should return the type of the player,
        # e.g. PlayerType.PLAYER, PlayerType.STEREO_PAIR or PlayerType.GROUP
        # Note that instead of any of these properties, you can also set the
        # _attr_xxxx attributes in the __init__ method of your Player subclass.
        return PlayerType.PLAYER

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        # MANDATORY
        # this should return True if the player needs to be polled for state updates,
        # If you player does not need to be polled, you can return False.
        return True

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        # OPTIONAL
        # used in conjunction with the needs_poll property.
        # this should return the interval in seconds to poll the player for state updates.
        return 5 if self.state == PlaybackState.PLAYING else 30

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        # MANDATORY
        # this should return a set of (optional) player features that the player supports.
        # For example, PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE, etc.
        # If the player does not support any extra features, you can return an empty set.
        return {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PLAY_ANNOUNCEMENT,
        }

    # @property
    # def source_list(self) -> list[PlayerSource]:
    #     """Return list of available (native) sources for this player."""
    #     # OPTIONAL - required only if you specified PlayerFeature.SELECT_SOURCE
    #     # this is an optional property that you can implement if your
    #     # player supports (external) source control (aux, HDMI, etc.).
    #     # If your player does not support sources, you can leave this out completely.
    #     return [
    #         PlayerSource(
    #             id="line_in",
    #             name="Line-In",
    #             passive=False,
    #             can_play_pause=False,
    #             can_next_previous=False,
    #             can_seek=False,
    #         ),
    #         PlayerSource(
    #             id="spotify_connect",
    #             name="Spotify",
    #             # by specifying passive=True, we indicate that this source
    #             # is not actively selectable by the user from the UI.
    #             passive=True,
    #             can_play_pause=True,
    #             can_next_previous=True,
    #             can_seek=True,
    #         ),
    #     ]

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.POWER
        # this method should send a power on/off command to the given player.
        logger = self.provider.logger.getChild(self.name)
        if powered:
            # In this demo implementation we just set the power state to ON
            # and optimistically update the state.
            # In a real implementation you would read the actual value from the player
            # either from a callback or by polling the player.
            logger.info("Received POWER ON command on player %s", self.name)
            self._attr_powered = True
        else:
            # In this demo implementation we just set the power state to OFF
            # and optimistically update the state.
            # In a real implementation you would read the actual value from the player
            # either from a callback or by polling the player.
            logger.info("Received POWER OFF command on player %s", self.name)
            self._attr_powered = False
        # update the player state in the player manager
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_SET
        # this method should send a volume set command to the given player.

        # In this demo implementation we just set the volume level
        # and optimistically update the state.
        # In a real implementation you would send a command to the actual player and
        # get the actual value from the player either from a callback or by polling the player.
        logger = self.provider.logger.getChild(self.name)
        logger.info(
            "Received VOLUME_SET command on player %s with level %s", self.name, volume_level
        )
        self._attr_volume_level = volume_level  # volume level is between 0 and 100
        # update the player state in the player manager
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_MUTE
        # this method should send a volume mute command to the given player.
        logger = self.provider.logger.getChild(self.name)
        logger.info("Received VOLUME_MUTE command on player %s with muted %s", self.name, muted)
        self._attr_volume_muted = muted
        self.update_state()

    async def play(self) -> None:
        """Play command."""
        # MANDATORY
        # this method is mandatory and should be implemented.
        # this method should send a play/resume command to the given player.
        # normally this is the point where you would resume playback
        # on your actual player device.

        # In this demo implementation we just set the playback state to PLAYING
        # and optimistically set the playback state to PLAYING.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.name)
        logger.info("Received PLAY command on player %s", self.name)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def stop(self) -> None:
        """Stop command."""
        # MANDATORY
        # this method is mandatory and should be implemented.
        # this method should send a stop command to the given player.
        # normally this is the point where you would stop playback
        # on your actual player device.

        # In this demo implementation we just set the playback state to IDLE
        # and optimistically set the playback state to IDLE.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.name)
        logger.info("Received STOP command on player %s", self.name)
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def pause(self) -> None:
        """Pause command."""
        # OPTIONAL - required only if you specified PlayerFeature.PAUSE
        # this method should send a pause command to the given player.

        # In this demo implementation we just set the playback state to PAUSED
        # and optimistically set the playback state to PAUSED.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.name)
        logger.info("Received PAUSE command on player %s", self.name)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def next_track(self) -> None:
        """Next command."""
        # OPTIONAL - required only if you specified PlayerFeature.NEXT_PREVIOUS
        # this method should send a next track command to the given player.
        # Note that this is only needed/used if the player is playing a 3rd party
        # stream (e.g. Spotify, YouTube, etc.) and the player supports skipping to the next track.
        # When the player is playing MA content, this is already handled in the Queue controller.

    async def previous_track(self) -> None:
        """Previous command."""
        # OPTIONAL - required only if you specified PlayerFeature.NEXT_PREVIOUS
        # this method should send a previous track command to the given player.
        # Note that this is only needed/used if the player is playing a 3rd party
        # stream (e.g. Spotify, YouTube, etc.) and the player supports skipping to the next track.
        # When the player is playing MA content, this is already handled in the Queue controller.

    async def seek(self, position: int) -> None:
        """SEEK command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SEEK
        # this method should send a seek command to the given player.
        # the position is the position in seconds to seek to in the current playing item.

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        # MANDATORY
        # This method is mandatory and should be implemented.
        # This method should handle the play_media command for the given player.
        # It will be called when media needs to be played on the player.
        # The media object contains all the details needed to play the item.

        # In 99% of the cases this will be called by the Queue controller to play
        # a single item from the queue on the player and the uri within the media
        # object will then contain the URL to play that single queue item.

        # If your player provider does not support enqueuing of items,
        # the queue controller will simply call this play_media method for
        # each item in the queue to play them one by one.

        # In order to support true gapless and/or enqueuing, we offer the option of
        # 'flow_mode' playback. In that case the queue controller will stitch together
        # all songs in the playback queue into a single stream and send that to the player.
        # In that case the URI (and metadata) received here is that of the 'flow mode' stream.

        # Examples of player providers that use flow mode for playback by default are AirPlay,
        # SnapCast and Fully Kiosk.

        # Examples of player providers that optionally use 'flow mode' are Google Cast and
        # Home Assistant. They provide a config entry to enable flow mode playback.

        # Examples of player providers that natively support enqueuing of items are Sonos,
        # Slimproto and Google Cast.

        # In this demo implementation we just optimistically set the state.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.name)
        logger.info("Received PLAY_MEDIA command on player %s with uri %s", self.name, media.uri)
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next (queue) item on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.ENQUEUE
        # This method is optional and should be implemented if you want to support
        # enqueuing of the next item on the player.
        # This will be called when the player reports it started buffering a queue item
        # and when the queue items updated.
        # A PlayerProvider implementation is in itself responsible for handling this
        # so that the queue items keep playing until its empty or the player stopped.

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (native) playback of an announcement on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.PLAY_ANNOUNCEMENT
        # This method is optional and should be implemented if the player supports
        # NATIVE playback of announcements (with ducking etc.).
        # The announcement object contains all the details needed to play the announcement.
        # The volume_level is optional and can be used to set the volume level for the announcement.
        # If you do not use the announcement playerfeature, the default behavior is to play the
        # announcement as a regular media item using the play_media method and the MA player manager
        # will take care of setting the volume level for the announcement and resuming etc.

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SELECT_SOURCE
        # This method is optional and should be implemented if the player supports
        # selecting a source (e.g. HDMI, AUX, etc.) on the player.
        # The source is the source ID to select on the player.
        # available sources are specified in the Player.source_list property

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SET_MEMBERS
        # This method is optional and should be implemented if the player supports
        # syncing/grouping with other players.

    async def poll(self) -> None:
        """Poll player for state updates."""
        # OPTIONAL - This is called by the Player Manager if the 'needs_poll' property is True.
        self._set_attributes()
        self.update_state()

    async def get_config_entries(
        self,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # OPTIONAL
        # this method is optional and should be implemented if you need player specific
        # configuration entries. If you do not need player specific configuration entries,
        # you can leave this method out completely to accept the default implementation.
        # Please note that you need to call the super() method to get the default entries.
        default_entries = await super().get_config_entries()
        return [
            *default_entries,
            # example of a player specific config entry
            # you can also override a default entry by specifying the same key
            # as a default entry, but with a different type or default value.
            ConfigEntry(
                key="demo_player_setting",
                type=ConfigEntryType.STRING,
                label="Demo Player Setting",
                required=False,
                default_value="default_value",
                description="This is a demo player setting.",
            ),
        ]

    def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

    def __init__(self, provider: PlayerProvider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        # init some static variables
        self._attr_name = f"Demo Player {player_id}"
        self._set_attributes()

    def _set_attributes(self) -> None:
        """Update/set (dynamic) properties."""
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_volume_level = 50
