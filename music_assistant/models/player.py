"""
Base class/model for a Player within Music Assistant.

All providerspecific players should inherit from this class and implement the required methods.

Note that the serverside Player object is not the same as the clientside Player object,
which is a dataclass in the models package containing the player state.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast, final

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerConfig,
)
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    HidePlayerOption,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.player import (
    EXTRA_ATTRIBUTES_TYPES,
    DeviceInfo,
    PlayerMedia,
    PlayerSource,
)
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.unique_list import UniqueList
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_EXPOSE_PLAYER_TO_HA,
    CONF_ENTRY_EXPOSE_PLAYER_TO_HA_DEFAULT_DISABLED,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HIDE_PLAYER_IN_UI,
    CONF_ENTRY_HIDE_PLAYER_IN_UI_ALWAYS_DEFAULT,
    CONF_ENTRY_HIDE_PLAYER_IN_UI_GROUP_PLAYER,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_SMART_FADES_MODE,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_FLOW_MODE,
    CONF_HIDE_PLAYER_IN_UI,
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_PRE_ANNOUNCE_CHIME_URL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.helpers.util import (
    get_changed_dataclass_values,
    validate_announcement_chime_url,
)

if TYPE_CHECKING:
    from .player_provider import PlayerProvider


CONF_ENTRY_PRE_ANNOUNCE_CUSTOM_CHIME_URL = ConfigEntry(
    key=CONF_PRE_ANNOUNCE_CHIME_URL,
    type=ConfigEntryType.STRING,
    label="Custom (pre)announcement chime URL",
    description="URL to a custom audio file to play before announcements.\n"
    "Leave empty to use the default chime.\n"
    "Supports http:// and https:// URLs pointing to "
    "audio files (.mp3, .wav, .flac, .ogg, .m4a, .aac).\n"
    "Example: http://homeassistant.local:8123/local/audio/custom_chime.mp3",
    category="announcements",
    required=False,
    depends_on=CONF_ENTRY_TTS_PRE_ANNOUNCE.key,
    depends_on_value=True,
    validate=lambda val: validate_announcement_chime_url(cast("str", val)),
)

BASE_CONFIG_ENTRIES = [
    # config entries that are valid for all player types
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_SMART_FADES_MODE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_PRE_ANNOUNCE_CUSTOM_CHIME_URL,
    CONF_ENTRY_HTTP_PROFILE,
]


class Player(ABC):
    """
    Base representation of a Player within the Music Assistant Server.

    Player Provider implementations should inherit from this base model.
    """

    _attr_type: PlayerType = PlayerType.PLAYER
    _attr_supported_features: set[PlayerFeature]
    _attr_group_members: list[str]
    _attr_static_group_members: list[str]
    _attr_device_info: DeviceInfo
    _attr_can_group_with: set[str]
    _attr_source_list: list[PlayerSource]
    _attr_available: bool = True
    _attr_name: str | None = None
    _attr_powered: bool | None = None
    _attr_playback_state: PlaybackState = PlaybackState.IDLE
    _attr_volume_level: int | None = None
    _attr_volume_muted: bool | None = None
    _attr_elapsed_time: float | None = None
    _attr_elapsed_time_last_updated: float | None = None
    _attr_active_source: str | None = None
    _attr_current_media: PlayerMedia | None = None
    _attr_needs_poll: bool = False
    _attr_poll_interval: int = 30
    _attr_hidden_by_default: bool = False
    _attr_expose_to_ha_by_default: bool = True
    _attr_enabled_by_default: bool = True

    def __init__(self, provider: PlayerProvider, player_id: str) -> None:
        """Initialize the Player."""
        # set mass as public variable
        self.mass = provider.mass
        self.logger = provider.logger
        # initialize mutable attributes
        self._attr_supported_features = set()
        self._attr_group_members = []
        self._attr_static_group_members = []
        self._attr_device_info = DeviceInfo()
        self._attr_can_group_with = set()
        self._attr_source_list = []
        # do not override/overwrite these private attributes below!
        self._cache: dict[str, Any] = {}  # storage dict for cached properties
        self._player_id = player_id
        self._provider = provider
        self.mass.config.create_default_player_config(
            player_id, self.provider_id, self.name, self.enabled_by_default
        )
        self._config = self.mass.config.get_base_player_config(player_id, self.provider_id)
        self._extra_data: dict[str, Any] = {}
        self._extra_attributes: dict[str, Any] = {}
        self._on_unload_callbacks: list[Callable[[], None]] = []
        self.__active_mass_source = player_id
        # The PlayerState is the (snapshotted) final state of the player
        # after applying any config overrides and other transformations,
        # such as the display name and player controls.
        # the state is updated when calling 'update_state' and is what is sent over the API.
        self._state = PlayerState(
            player_id=self.player_id,
            provider=self.provider_id,
            type=self.type,
            name=self.display_name,
            available=self.available,
            device_info=self.device_info,
            supported_features=self.supported_features,
            playback_state=self.playback_state,
        )

    @property
    def type(self) -> PlayerType:
        """Return the type of the player."""
        return self._attr_type

    @property
    def available(self) -> bool:
        """Return if the player is available."""
        return self._attr_available

    @property
    def name(self) -> str | None:
        """Return the name of the player."""
        return self._attr_name

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        return self._attr_supported_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        return self._attr_playback_state

    @cached_property
    def flow_mode(self) -> bool:
        """
        Return if the player needs flow mode.

        Will by default be set to True if the player does not support PlayerFeature.ENQUEUE
        or has a flow mode config entry set to True.
        """
        if bool(self._config.get_value(CONF_FLOW_MODE)) is True:
            return True
        return PlayerFeature.ENQUEUE not in self.supported_features

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info of the player."""
        return self._attr_device_info

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track (if any)."""
        return self._attr_elapsed_time

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """
        Return when the elapsed time was last updated.

        return: The (UTC) timestamp when the elapsed time was last updated,
        or None if it was never updated (or unknown).
        """
        return self._attr_elapsed_time_last_updated

    @property
    def group_members(self) -> list[str]:
        """
        Return the group members of the player.

        If there are other players synced/grouped with this player,
        this should return the id's of players synced to this player,
        and this should include the player's own id (as first item in the list).

        If there are currently no group members, this should return an empty list.
        """
        if self.type == PlayerType.PLAYER and (
            len(self._attr_group_members) >= 1 and self.player_id not in self._attr_group_members
        ):
            # always ensure the player_id is in the group_members list for players
            return [self.player_id, *self._attr_group_members]
        elif self._attr_group_members == [self.player_id]:
            return []
        return self._attr_group_members

    @property
    def static_group_members(self) -> list[str]:
        """
        Return the static group members for a player group.

        For PlayerType.GROUP return the player_ids of members that must not be removed by
        the user.
        For all other player types return an empty list.
        """
        return self._attr_static_group_members

    @property
    def can_group_with(self) -> set[str]:
        """
        Return the id's of players this player can group with.

        This should return set of player_id's this player can group/sync with
        or just the provider's instance_id if all players can group with each other.
        """
        return self._attr_can_group_with

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return self._attr_needs_poll

    @property
    def poll_interval(self) -> int:
        """
        Return the (dynamic) poll interval for the player.

        Only used if 'needs_poll' is set to True.
        This should return the interval in seconds.
        """
        return self._attr_poll_interval

    @property
    def hidden_by_default(self) -> bool:
        """Return if the player should be hidden in the UI by default."""
        return self._attr_hidden_by_default

    @property
    def expose_to_ha_by_default(self) -> bool:
        """Return if the player should be exposed to Home Assistant by default."""
        return self._attr_expose_to_ha_by_default

    @property
    def enabled_by_default(self) -> bool:
        """Return if the player should be enabled by default."""
        return self._attr_enabled_by_default

    @property
    def _powered(self) -> bool | None:
        """
        Return if the player is powered on.

        If the player does not support PlayerFeature.POWER,
        or the state is (currently) unknown, this property may return None.

        Note that this is NOT the final power state of the player,
        as it may be overridden by a playercontrol.
        Hence it's marked as a private property.
        The final power state can be retrieved by using the 'powered' property.
        """
        return self._attr_powered

    @property
    def _volume_level(self) -> int | None:
        """
        Return the current volume level (0..100) of the player.

        If the player does not support PlayerFeature.VOLUME_SET,
        or the state is (currently) unknown, this property may return None.

        Note that this is NOT the final volume level state of the player,
        as it may be overridden by a playercontrol.
        Hence it's marked as a private property.
        The final volume level state can be retrieved by using the 'volume_level' property.
        """
        return self._attr_volume_level

    @property
    def _volume_muted(self) -> bool | None:
        """
        Return the current mute state of the player.

        If the player does not support PlayerFeature.VOLUME_MUTE,
        or the state is (currently) unknown, this property may return None.

        Note that this is NOT the final muted state of the player,
        as it may be overridden by a playercontrol.
        Hence it's marked as a private property.
        The final muted state can be retrieved by using the 'volume_muted' property.
        """
        return self._attr_volume_muted

    @property
    def _active_source(self) -> str | None:
        """
        Return the (id of) the active source of the player.

        Only required if the player supports PlayerFeature.SELECT_SOURCE.

        Set to None if the player is not currently playing a source or
        the player_id if the player is currently playing a MA queue.

        Note that this is NOT the final active source of the player,
        as it may be overridden by a active group/sync membership.
        Hence it's marked as a private property.
        The final active source can be retrieved by using the 'active_source' property.
        """
        return self._attr_active_source

    @property
    def _current_media(self) -> PlayerMedia | None:
        """
        Return the current media being played by the player.

        Note that this is NOT the final current media of the player,
        as it may be overridden by a active group/sync membership.
        Hence it's marked as a private property.
        The final current media can be retrieved by using the 'current_media' property.
        """
        return self._attr_current_media

    @property
    def _source_list(self) -> list[PlayerSource]:
        """
        Return list of available (native) sources for this player.

        Note that this is NOT the final source list of the player,
        as we inject the MA queue source if the player is currently playing a MA queue.
        Hence it's marked as a private property.
        The final source list can be retrieved by using the 'source_list' property.
        """
        return self._attr_source_list

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command on the player.

        Will only be called if the PlayerFeature.POWER is supported.

        :param powered: bool if player should be powered on or off.
        """
        raise NotImplementedError("power needs to be implemented when PlayerFeature.POWER is set")

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        raise NotImplementedError(
            "volume_set needs to be implemented when PlayerFeature.VOLUME_SET is set"
        )

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        Will only be called if the PlayerFeature.VOLUME_MUTE is supported.

        :param muted: bool if player should be muted.
        """
        raise NotImplementedError(
            "volume_mute needs to be implemented when PlayerFeature.VOLUME_MUTE is set"
        )

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        raise NotImplementedError("play needs to be implemented")

    @abstractmethod
    async def stop(self) -> None:
        """
        Handle STOP command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported or
        player supports resuming of stopped playback.
        """
        raise NotImplementedError("stop needs to be implemented")

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
        raise NotImplementedError("pause needs to be implemented when PlayerFeature.PAUSE is set")

    async def next_track(self) -> None:
        """
        Handle NEXT_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        raise NotImplementedError(
            "next_track needs to be implemented when PlayerFeature.NEXT_PREVIOUS is set"
        )

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        raise NotImplementedError(
            "previous_track needs to be implemented when PlayerFeature.NEXT_PREVIOUS is set"
        )

    async def seek(self, position: int) -> None:
        """
        Handle SEEK command on the player.

        Seek to a specific position in the current track.
        Will only be called if the player reports PlayerFeature.SEEK is
        supported and the player is NOT currently playing a MA queue.

        :param position: The position to seek to, in seconds.
        """
        raise NotImplementedError("seek needs to be implemented when PlayerFeature.SEEK is set")

    @abstractmethod
    async def play_media(
        self,
        media: PlayerMedia,
    ) -> None:
        """
        Handle PLAY MEDIA command on given player.

        This is called by the Player controller to start playing Media on the player,
        which can be a MA queue item/stream or a native source.
        The provider's own implementation should work out how to handle this request.

        :param media: Details of the item that needs to be played on the player.
        """
        raise NotImplementedError("play_media needs to be implemented")

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """
        Handle enqueuing of the next (queue) item on the player.

        Called when player reports it started buffering a queue item
        and when the queue items updated.

        A PlayerProvider implementation is in itself responsible for handling this
        so that the queue items keep playing until its empty or the player stopped.

        Will only be called if the player reports PlayerFeature.ENQUEUE is
        supported and the player is currently playing a MA queue.

        This will NOT be called if the end of the queue is reached (and repeat disabled).
        This will NOT be called if the player is using flow mode to playback the queue.

         :param media: Details of the item that needs to be enqueued on the player.
        """
        raise NotImplementedError(
            "enqueue_next_media needs to be implemented when PlayerFeature.ENQUEUE is set"
        )

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Handle (native) playback of an announcement on the player.

        Will only be called if the PlayerFeature.PLAY_ANNOUNCEMENT is supported.

        :param announcement: Details of the announcement that needs to be played on the player.
        :param volume_level: The volume level to play the announcement at (0..100).
            If not set, the player should use the current volume level.
        """
        raise NotImplementedError(
            "play_announcement needs to be implemented when PlayerFeature.PLAY_ANNOUNCEMENT is set"
        )

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        raise NotImplementedError(
            "select_source needs to be implemented when PlayerFeature.SELECT_SOURCE is set"
        )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Handle SET_MEMBERS command on the player.

        Group or ungroup the given child player(s) to/from this player.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param player_ids_to_add: List of player_id's to add to the group.
        :param player_ids_to_remove: List of player_id's to remove from the group.
        """
        raise NotImplementedError(
            "set_members needs to be implemented when PlayerFeature.SET_MEMBERS is set"
        )

    async def poll(self) -> None:
        """
        Poll player for state updates.

        This is called by the Player Manager;
        if the 'needs_poll' property is True.
        """
        raise NotImplementedError("poll needs to be implemented when needs_poll is True")

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player.

        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        # Return all base config entries for a player.
        # Feel free to override but ensure to include the base entries by calling super() first.
        # To override the default config entries, simply define an entry with the same key
        # and it will be used instead of the default one.
        return [
            # config entries that are valid for all players
            *BASE_CONFIG_ENTRIES,
            # add player control entries
            *self._create_player_control_config_entries(),
            CONF_ENTRY_AUTO_PLAY,
            # audio-related config entries
            CONF_ENTRY_SAMPLE_RATES,
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_OUTPUT_CHANNELS,
            # add default entries for announce feature
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
            CONF_ENTRY_ANNOUNCE_VOLUME,
            CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
            CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
            # add default entries to hide player in UI and expose to HA
            (
                CONF_ENTRY_HIDE_PLAYER_IN_UI_ALWAYS_DEFAULT
                if self.hidden_by_default
                else CONF_ENTRY_HIDE_PLAYER_IN_UI
            ),
            (
                CONF_ENTRY_EXPOSE_PLAYER_TO_HA
                if self.expose_to_ha_by_default
                else CONF_ENTRY_EXPOSE_PLAYER_TO_HA_DEFAULT_DISABLED
            ),
        ]

    async def on_config_updated(self) -> None:
        """
        Handle logic when the player is loaded or updated.

        Override this method in your player implementation if you need
        to perform any additional setup logic after the player is registered and
        the self.config was loaded, and whenever the config changes.
        """
        return

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        for callback in self._on_unload_callbacks:
            try:
                callback()
            except Exception as err:
                self.logger.error(
                    "Error calling on_unload callback for player %s: %s",
                    self.player_id,
                    err,
                )

    async def group_with(self, target_player_id: str) -> None:
        """
        Handle GROUP_WITH command on the player.

        Group this player to the given syncleader/target.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param target_player: player_id of the target player / sync leader.
        """
        # convenience helper method
        # no need to implement unless your player/provider has an optimized way to execute this
        # default implementation will simply call set_members
        # to add the target player to the group.
        target_player = self.mass.players.get(target_player_id, raise_unavailable=True)
        assert target_player  # for type checking
        await target_player.set_members(player_ids_to_add=[self.player_id])

    async def ungroup(self) -> None:
        """
        Handle UNGROUP command on the player.

        Remove the player from any (sync)groups it currently is grouped to.
        If this player is the sync leader (or group player),
        all child's will be ungrouped and the group dissolved.

        Will only be called if the PlayerFeature.SET_MEMBERS is supported.
        """
        # convenience helper method
        # no need to implement unless your player/provider has an optimized way to execute this
        # default implementation will simply call set_members
        if self.synced_to:
            if parent_player := self.mass.players.get(self.synced_to):
                # if this player is synced to another player, remove self from that group
                await parent_player.set_members(player_ids_to_remove=[self.player_id])
        elif self.group_members:
            await self.set_members(player_ids_to_remove=self.group_members)

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        If it is part of a (permanent) group, this should also return None.
        """
        # default implementation: feel free to override
        for player in self.mass.players.all():
            if player.player_id == self.player_id:
                # skip self
                continue
            if player.type == PlayerType.PLAYER and self.player_id in player.group_members:
                # this player is synced to another player, but not part of a (permanent) group
                return player.player_id
        return None

    def _on_player_media_updated(self) -> None:  # noqa: B027
        """Handle callback when the current media of the player is updated."""
        # optional callback for players that want to be informed when the final
        # current media is updated (after applying group/sync membership logic).
        # for instance to update any display information on the physical player.

    # DO NOT OVERWRITE BELOW !
    # These properties and methods are either managed by core logic or they
    # are used to perform a very specific function. Overwriting these may
    # produce undesirable effects.

    @property
    @final
    def player_id(self) -> str:
        """Return the id of the player."""
        return self._player_id

    @property
    @final
    def provider(self) -> PlayerProvider:
        """Return the provider of the player."""
        return self._provider

    @property
    @final
    def provider_id(self) -> str:
        """Return the provider (instance) id of the player."""
        return self._provider.instance_id

    @property
    @final
    def config(self) -> PlayerConfig:
        """Return the config of the player."""
        return self._config

    @property
    @final
    def extra_attributes(self) -> dict[str, EXTRA_ATTRIBUTES_TYPES]:
        """
        Return the extra attributes of the player.

        This is a dict that can be used to pass any extra (serializable)
        attributes over the API, to be consumed by the UI (or another APi client, such as HA).
        This is not persisted and not used or validated by the core logic.
        """
        return self._extra_attributes

    @property
    @final
    def extra_data(self) -> dict[str, Any]:
        """
        Return the extra data of the player.

        This is a dict that can be used to store any extra data
        that is not part of the player state or config.
        This is not persisted and not exposed on the API.
        """
        return self._extra_data

    @cached_property
    @final
    def display_name(self) -> str:
        """Return the display name of the player."""
        if custom_name := self._config.name:
            # always prefer the custom name over the default name
            return custom_name
        return self.name or self._config.default_name or self.player_id

    @property
    @final
    def powered(self) -> bool | None:
        """
        Return the FINAL power state of the player.

        This is a convenience property which calculates the final power state
        based on the playercontrol which may have been set-up.
        """
        power_control = self.power_control
        if power_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_POWER, False))
        if power_control == PLAYER_CONTROL_NATIVE:
            return self._powered
        if power_control == PLAYER_CONTROL_NONE:
            return None
        if control := self.mass.players.get_player_control(power_control):
            return control.power_state
        return None

    @property
    @final
    def volume_level(self) -> int | None:
        """
        Return the FINAL volume level of the player.

        This is a convenience property which calculates the final volume level
        based on the playercontrol which may have been set-up.
        """
        volume_control = self.volume_control
        if volume_control == PLAYER_CONTROL_FAKE:
            return int(self.extra_data.get(ATTR_FAKE_VOLUME, 0))
        if volume_control == PLAYER_CONTROL_NATIVE:
            return self._volume_level
        if volume_control == PLAYER_CONTROL_NONE:
            return None
        if control := self.mass.players.get_player_control(volume_control):
            return control.volume_level
        return None

    @property
    @final
    def volume_muted(self) -> bool | None:
        """
        Return the FINAL mute state of the player.

        This is a convenience property which calculates the final mute state
        based on the playercontrol which may have been set-up.
        """
        mute_control = self.mute_control
        if mute_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_MUTE, False))
        if mute_control == PLAYER_CONTROL_NATIVE:
            return self._volume_muted
        if mute_control == PLAYER_CONTROL_NONE:
            return None
        if control := self.mass.players.get_player_control(mute_control):
            return control.volume_muted
        return None

    @property
    @final
    def active_source(self) -> str | None:
        """
        Return the FINAL active source of the player.

        This is a convenience property which calculates the final active source
        based on any group memberships or source plugins that can be active.
        """
        # if the player is grouped/synced, use the active source of the group/parent player
        if parent_player_id := (self.active_group or self.synced_to):
            if parent_player_id != self.player_id and (
                parent_player := self.mass.players.get(parent_player_id)
            ):
                return parent_player.active_source
        for plugin_source in self.mass.players.get_plugin_sources():
            if plugin_source.in_use_by == self.player_id:
                return plugin_source.id
        if (
            self.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and self._active_source
        ):
            # active source as reported by the player itself
            # but only if playing/paused, otherwise we always prefer the MA source
            return self._active_source
        # return the (last) known MA source
        return self.__active_mass_source

    @cached_property
    @final
    def source_list(self) -> UniqueList[PlayerSource]:
        """
        Return the FINAL source list of the player.

        This is a convenience property with the calculated final source list
        based on any group memberships or source plugins that can be active.
        """
        return self.__attr_source_list or UniqueList()

    @cached_property
    @final
    def enabled(self) -> bool:
        """Return if the player is enabled."""
        return self._config.enabled

    @property
    def corrected_elapsed_time(self) -> float | None:
        """Return the corrected/realtime elapsed time."""
        if self.elapsed_time is None or self.elapsed_time_last_updated is None:
            return None
        if self.playback_state == PlaybackState.PLAYING:
            return self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
        return self.elapsed_time

    @property
    @final
    def active_groups(self) -> list[str]:
        """
        Return the player ids of all playergroups that are currently active for this player.

        This will return the ids of the groupplayers if any groups are active.
        If no groups are currently active, this will return an empty list.
        """
        return self.__attr_active_groups or []

    @property
    @final
    def active_group(self) -> str | None:
        """
        Return the player id of the (first) playergroup that is currently active for this player.

        This will return the id of the groupplayer if a group is active.
        If no group is currently active, this will return None.
        """
        active_groups = self.active_groups
        return active_groups[0] if active_groups else None

    @property
    @final
    def current_media(self) -> PlayerMedia | None:
        """
        Return the current media being played by the player.

        This is a convenience property with the calculates current media
        based on any group memberships or source plugins that can be active.
        """
        return self.__attr_current_media

    @cached_property
    @final
    def icon(self) -> str:
        """Return the player icon."""
        return cast("str", self._config.get_value(CONF_ENTRY_PLAYER_ICON.key))

    @cached_property
    @final
    def power_control(self) -> str:
        """Return the power control type."""
        if conf := self._config.get_value(CONF_POWER_CONTROL):
            return str(conf)
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def volume_control(self) -> str:
        """Return the volume control type."""
        if conf := self._config.get_value(CONF_VOLUME_CONTROL):
            return str(conf)
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def mute_control(self) -> str:
        """Return the mute control type."""
        if conf := self._config.get_value(CONF_MUTE_CONTROL):
            return str(conf)
        return PLAYER_CONTROL_NONE

    @property
    @final
    def group_volume(self) -> int:
        """
        Return the group volume level.

        If this player is a group player or syncgroup, this will return the average volume
        level of all (powered on) child players in the group.
        If the player is not a group player or syncgroup, this will return the volume level
        of the player itself (if set), or 0 if not set.
        """
        if len(self.group_members) == 0:
            # player is not a group or syncgroup
            return self.volume_level or 0
        # calculate group volume from all (turned on) players
        group_volume = 0
        active_players = 0
        for child_player in self.mass.players.iter_group_members(
            self, only_powered=True, exclude_self=self.type != PlayerType.PLAYER
        ):
            if (child_volume := child_player.volume_level) is None:
                continue
            group_volume += child_volume
            active_players += 1
        if active_players:
            group_volume = int(group_volume / active_players)
        return group_volume

    @cached_property
    @final
    def hide_player_in_ui(self) -> set[HidePlayerOption]:
        """
        Return the hide player in UI options.

        This is a convenience property based on the config entry.
        """
        return {
            HidePlayerOption(x)
            for x in cast("list[str]", self._config.get_value(CONF_HIDE_PLAYER_IN_UI, []))
        }

    @cached_property
    @final
    def expose_to_ha(self) -> bool:
        """
        Return if the player should be exposed to Home Assistant.

        This is a convenience property that returns True if the player is set to be exposed
        to Home Assistant, based on the config entry.
        """
        return bool(self._config.get_value(CONF_EXPOSE_PLAYER_TO_HA))

    @property
    @final
    def mass_queue_active(self) -> bool:
        """
        Return if the/a Music Assistant Queue is currently active for this player.

        This is a convenience property that returns True if the
        player currently has a Music Assistant Queue as active source.
        """
        return bool(self.mass.players.get_active_queue(self))

    @property
    @final
    def state(self) -> PlayerState:
        """Return the current PlayerState of the player."""
        return self._state

    @final
    def update_state(self, force_update: bool = False, signal_event: bool = True) -> None:
        """
        Update the PlayerState with the current state of the player.

        This method should be called to update the player's state
        and signal any changes to the PlayerController.

        :param force_update: If True, a state update event will be
        pushed even if the state has not actually changed.
        :param signal_event: If True, signal the state update event to the PlayerController.
        """
        self.mass.verify_event_loop_thread("player.update_state")
        # clear the dict for the cached properties
        self._cache.clear()
        # calculate the new state
        prev_media_checksum = self._get_player_media_checksum()
        changed_values = self.__calculate_state()
        if prev_media_checksum != self._get_player_media_checksum():
            # current media changed, call the media updated callback
            self._on_player_media_updated()
        # ignore some values that are not relevant for the state
        changed_values.pop("elapsed_time_last_updated", None)
        changed_values.pop("extra_attributes.seq_no", None)
        changed_values.pop("extra_attributes.last_poll", None)
        changed_values.pop("current_media.elapsed_time_last_updated", None)
        # persist the default name if it changed
        if self.name and self.config.default_name != self.name:
            self.mass.config.set_player_default_name(self.player_id, self.name)
        # return early if nothing changed (unless force_update is True)
        if len(changed_values) == 0 and not force_update:
            return
        # signal the state update to the PlayerController
        if signal_event:
            self.mass.players.signal_player_state_update(self, changed_values)

    @final
    def set_current_media(  # noqa: PLR0913
        self,
        uri: str,
        media_type: MediaType = MediaType.UNKNOWN,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        image_url: str | None = None,
        duration: int | None = None,
        source_id: str | None = None,
        queue_item_id: str | None = None,
        custom_data: dict[str, Any] | None = None,
        clear_all: bool = False,
    ) -> None:
        """
        Set current_media helper.

        Assumes use of '_attr_current_media'.
        """
        if self._attr_current_media is None or clear_all:
            self._attr_current_media = PlayerMedia(
                uri=uri,
                media_type=media_type,
            )
        self._attr_current_media.uri = uri
        if media_type != MediaType.UNKNOWN:
            self._attr_current_media.media_type = media_type
        if title:
            self._attr_current_media.title = title
        if artist:
            self._attr_current_media.artist = artist
        if album:
            self._attr_current_media.album = album
        if image_url:
            self._attr_current_media.image_url = image_url
        if duration:
            self._attr_current_media.duration = duration
        if source_id:
            self._attr_current_media.source_id = source_id
        if queue_item_id:
            self._attr_current_media.queue_item_id = queue_item_id
        if custom_data:
            self._attr_current_media.custom_data = custom_data

    @final
    def set_config(self, config: PlayerConfig) -> None:
        """
        Set/update the player config.

        May only be called by the PlayerController.
        """
        # TODO: validate that caller is the PlayerController ?
        self._config = config

    @final
    def to_dict(self) -> dict[str, Any]:
        """Return the (serializable) dict representation of the Player."""
        return self.state.to_dict()

    @final
    def supports_feature(self, feature: PlayerFeature) -> bool:
        """Return True if this player supports the given feature."""
        return feature in self.supported_features

    @final
    def check_feature(self, feature: PlayerFeature) -> None:
        """Check if this player supports the given feature."""
        if not self.supports_feature(feature):
            raise UnsupportedFeaturedException(
                f"Player {self.display_name} does not support feature {feature.name}"
            )

    def _create_player_control_config_entries(
        self,
    ) -> list[ConfigEntry]:
        """Create config entries for player controls."""
        all_controls = self.mass.players.player_controls()
        power_controls = [x for x in all_controls if x.supports_power]
        volume_controls = [x for x in all_controls if x.supports_volume]
        mute_controls = [x for x in all_controls if x.supports_mute]
        # work out player supported features
        supports_power = PlayerFeature.POWER in self.supported_features
        supports_volume = PlayerFeature.VOLUME_SET in self.supported_features
        supports_mute = PlayerFeature.VOLUME_MUTE in self.supported_features
        # create base options per control type (and add defaults like native and fake)
        base_power_options: list[ConfigValueOption] = [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
            ConfigValueOption(title="Fake power control", value=PLAYER_CONTROL_FAKE),
        ]
        if supports_power:
            base_power_options.append(
                ConfigValueOption(title="Native power control", value=PLAYER_CONTROL_NATIVE),
            )
        base_volume_options: list[ConfigValueOption] = [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
        ]
        if supports_volume:
            base_volume_options.append(
                ConfigValueOption(title="Native volume control", value=PLAYER_CONTROL_NATIVE),
            )
        base_mute_options: list[ConfigValueOption] = [
            ConfigValueOption(title="None", value=PLAYER_CONTROL_NONE),
            ConfigValueOption(title="Fake mute control", value=PLAYER_CONTROL_FAKE),
        ]
        if supports_mute:
            base_mute_options.append(
                ConfigValueOption(title="Native mute control", value=PLAYER_CONTROL_NATIVE),
            )
        # return final config entries for all options
        return [
            # Power control config entry
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                label="Power Control",
                default_value=PLAYER_CONTROL_NATIVE if supports_power else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *base_power_options,
                    *(ConfigValueOption(x.name, x.id) for x in power_controls),
                ],
                category="player_controls",
            ),
            # Volume control config entry
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                label="Volume Control",
                default_value=PLAYER_CONTROL_NATIVE if supports_volume else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *base_volume_options,
                    *(ConfigValueOption(x.name, x.id) for x in volume_controls),
                ],
                category="player_controls",
            ),
            # Mute control config entry
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                label="Mute Control",
                default_value=PLAYER_CONTROL_NATIVE if supports_mute else PLAYER_CONTROL_NONE,
                required=True,
                options=[
                    *base_mute_options,
                    *[ConfigValueOption(x.name, x.id) for x in mute_controls],
                ],
                category="player_controls",
            ),
        ]

    def _get_player_media_checksum(self) -> str:
        """Return a checksum for the current media."""
        if not (media := self.current_media):
            return ""
        return (
            f"{media.uri}|{media.title}|{media.source_id}|{media.queue_item_id}|"
            f"{media.image_url}|{media.duration}|{media.elapsed_time}"
        )

    def __calculate_state(
        self,
    ) -> dict[str, tuple[Any, Any]]:
        """
        Calculate the (current) PlayerState.

        This method is called when we're updating the player,
        and we compare the current state with the previous state to determine
        if we need to signal a state change to API consumers.

        Returns a dict with the state attributes that have changed.
        """
        self.__attr_active_groups = self.__calculate_active_groups()
        self.__attr_current_media = self.__calculate_current_media()
        self.__attr_source_list = self.__calculate_source_list()
        prev_state = deepcopy(self._state)
        self._state = PlayerState(
            player_id=self.player_id,
            provider=self.provider_id,
            type=self.type,
            available=self.enabled and self.available,
            device_info=self.device_info,
            supported_features=self.supported_features,
            playback_state=self.playback_state,
            elapsed_time=self.elapsed_time,
            elapsed_time_last_updated=self.elapsed_time_last_updated,
            powered=self.powered,
            volume_level=self.volume_level,
            volume_muted=self.volume_muted,
            group_members=UniqueList(self.group_members),
            static_group_members=UniqueList(self.static_group_members),
            can_group_with=self.can_group_with,
            synced_to=self.synced_to,
            active_source=self.active_source,
            source_list=self.source_list,
            active_group=self.active_group,
            current_media=self.current_media,
            name=self.display_name,
            enabled=self.enabled,
            hide_player_in_ui=self.hide_player_in_ui,
            expose_to_ha=self.expose_to_ha,
            icon=self.icon,
            group_volume=self.group_volume,
            extra_attributes=self.extra_attributes,
            power_control=self.power_control,
            volume_control=self.volume_control,
            mute_control=self.mute_control,
        )

        # correct group_members if needed
        if self._state.group_members == [self.player_id]:
            self._state.group_members.clear()
        elif (
            self._state.group_members
            and self.player_id not in self._state.group_members
            and self.type == PlayerType.PLAYER
        ):
            self._state.group_members.set([self.player_id, *self._state.group_members])

        # track stop called state
        if (
            prev_state.playback_state == PlaybackState.IDLE
            and self._state.playback_state != PlaybackState.IDLE
        ):
            self.__stop_called = False
        elif (
            prev_state.playback_state != PlaybackState.IDLE
            and self._state.playback_state == PlaybackState.IDLE
        ):
            self.__stop_called = True

        # Auto correct player state if player is synced (or group child)
        # This is because some players/providers do not accurately update this info
        # for the sync child's.
        if self._state.synced_to and (sync_leader := self.mass.players.get(self._state.synced_to)):
            self._state.playback_state = sync_leader.playback_state
            self._state.elapsed_time = sync_leader.elapsed_time
            self._state.elapsed_time_last_updated = sync_leader.elapsed_time_last_updated

        return get_changed_dataclass_values(
            prev_state,
            self._state,
            recursive=True,
        )

    __attr_active_groups: list[str] | None = None

    def __calculate_active_groups(self) -> list[str]:
        """Calculate the active groups for the player."""
        active_groups = []
        for player in self.mass.players.all(return_unavailable=False, return_disabled=False):
            if player.type != PlayerType.GROUP:
                continue
            if player.player_id == self.player_id:
                continue
            if not (player.powered or player.playback_state == PlaybackState.PLAYING):
                continue
            if self.player_id in player.group_members:
                active_groups.append(player.player_id)
        return active_groups

    __attr_current_media: PlayerMedia | None = None

    def __calculate_current_media(self) -> PlayerMedia | None:
        """Calculate the current media for the player."""
        if self.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            # if an announcement is in progress, return announcement details
            return PlayerMedia(
                uri="announcement",
                media_type=MediaType.ANNOUNCEMENT,
                title="ANNOUNCEMENT",
            )
        # if the player is grouped/synced, use the current_media of the group/parent player
        if parent_player_id := (self.active_group or self.synced_to):
            if parent_player_id != self.player_id and (
                parent_player := self.mass.players.get(parent_player_id)
            ):
                return parent_player.current_media
        # if a pluginsource is currently active, return those details
        if (
            self.active_source
            and (source := self.mass.players.get_plugin_source(self.active_source))
            and source.metadata
        ):
            return PlayerMedia(
                uri=source.metadata.uri or source.id,
                media_type=MediaType.PLUGIN_SOURCE,
                title=source.metadata.title,
                artist=source.metadata.artist,
                album=source.metadata.album,
                image_url=source.metadata.image_url,
                duration=source.metadata.duration,
                source_id=source.id,
                elapsed_time=source.metadata.elapsed_time,
                elapsed_time_last_updated=source.metadata.elapsed_time_last_updated,
            )
        # if MA queue is active, return those details
        active_queue = None
        if self._current_media and self._current_media.source_id:
            active_queue = self.mass.player_queues.get(self._current_media.source_id)
        if not active_queue and self.active_source:
            active_queue = self.mass.player_queues.get(self.active_source)
        if not active_queue and self._active_source is None:
            active_queue = self.mass.player_queues.get(self.player_id)

        if active_queue and (current_item := active_queue.current_item):
            item_image_url = (
                # the image format needs to be 500x500 jpeg for maximum compatibility with players
                self.mass.metadata.get_image_url(current_item.image, size=500, image_format="jpeg")
                if current_item.image
                else None
            )
            if current_item.streamdetails and (
                stream_metadata := current_item.streamdetails.stream_metadata
            ):
                # handle stream metadata in streamdetails (e.g. for radio stream)
                return PlayerMedia(
                    uri=current_item.uri,
                    media_type=current_item.media_type,
                    title=stream_metadata.title or current_item.name,
                    artist=stream_metadata.artist,
                    album=stream_metadata.album or stream_metadata.description or current_item.name,
                    image_url=(stream_metadata.image_url or item_image_url),
                    duration=stream_metadata.duration or current_item.duration,
                    source_id=active_queue.queue_id,
                    queue_item_id=current_item.queue_item_id,
                    elapsed_time=stream_metadata.elapsed_time or int(active_queue.elapsed_time),
                    elapsed_time_last_updated=stream_metadata.elapsed_time_last_updated
                    or active_queue.elapsed_time_last_updated,
                )
            if media_item := current_item.media_item:
                # normal media item
                # we use getattr here to avoid issues with different media item types
                version = getattr(media_item, "version", None)
                album = getattr(media_item, "album", None)
                podcast = getattr(media_item, "podcast", None)
                metadata = getattr(media_item, "metadata", None)
                description = getattr(metadata, "description", None) if metadata else None
                return PlayerMedia(
                    uri=str(media_item.uri),
                    media_type=media_item.media_type,
                    title=f"{media_item.name} ({version})" if version else media_item.name,
                    artist=getattr(media_item, "artist_str", None),
                    album=album.name if album else podcast.name if podcast else description,
                    # the image format needs to be 500x500 jpeg for maximum player compatibility
                    image_url=self.mass.metadata.get_image_url(
                        current_item.media_item.image, size=500, image_format="jpeg"
                    )
                    or item_image_url
                    if current_item.media_item.image
                    else item_image_url,
                    duration=media_item.duration,
                    source_id=active_queue.queue_id,
                    queue_item_id=current_item.queue_item_id,
                    elapsed_time=int(active_queue.elapsed_time),
                    elapsed_time_last_updated=active_queue.elapsed_time_last_updated,
                )

            # fallback to basic current item details
            return PlayerMedia(
                uri=current_item.uri,
                media_type=current_item.media_type,
                title=current_item.name,
                image_url=item_image_url,
                duration=current_item.duration,
                source_id=active_queue.queue_id,
                queue_item_id=current_item.queue_item_id,
                elapsed_time=int(active_queue.elapsed_time),
                elapsed_time_last_updated=active_queue.elapsed_time_last_updated,
            )
        elif active_queue:
            # queue is active but no current item
            return None
        # return native current media if no group/queue is active
        if self._current_media:
            return PlayerMedia(
                uri=self._current_media.uri,
                media_type=self._current_media.media_type,
                title=self._current_media.title,
                artist=self._current_media.artist,
                album=self._current_media.album,
                image_url=self._current_media.image_url,
                duration=self._current_media.duration,
                source_id=self._current_media.source_id or self._active_source,
                queue_item_id=self._current_media.queue_item_id,
                elapsed_time=self._current_media.elapsed_time or int(self.elapsed_time)
                if self.elapsed_time
                else None,
                elapsed_time_last_updated=self._current_media.elapsed_time_last_updated
                or self.elapsed_time_last_updated,
            )
        return None

    __attr_source_list: UniqueList[PlayerSource] | None = None

    def __calculate_source_list(self) -> UniqueList[PlayerSource]:
        """Calculate the source list for the player."""
        sources = UniqueList(self._source_list)
        # always ensure the Music Assistant Queue is in the source list
        mass_source = next((x for x in sources if x.id == self.player_id), None)
        if mass_source is None:
            # if the MA queue is not in the source list, add it
            mass_source = PlayerSource(
                id=self.player_id,
                name="Music Assistant Queue",
                passive=False,
                # TODO: Do we want to dynamically set these based on the queue state ?
                can_play_pause=True,
                can_seek=True,
                can_next_previous=True,
            )
            sources.append(mass_source)
        # append all/any plugin sources (convert to PlayerSource to avoid deepcopy issues)
        for plugin_source in self.mass.players.get_plugin_sources():
            if hasattr(plugin_source, "as_player_source"):
                sources.append(plugin_source.as_player_source())
            else:
                sources.append(plugin_source)
        return sources

    # The id of the (last) active mass source.
    # This is to keep track of the last active MA source for the player,
    # so we can restore it when needed (e.g. after switching to a plugin source).
    __active_mass_source: str = ""

    def set_active_mass_source(self, value: str) -> None:
        """
        Set the id of the (last) active mass source.

        This is to keep track of the last active MA source for the player,
        so we can restore it when needed (e.g. after switching to a plugin source).
        """
        self.__active_mass_source = value
        self.update_state()

    __stop_called: bool = False

    def mark_stop_called(self) -> None:
        """Mark that the STOP command was called on the player."""
        self.__stop_called = True

    @property
    def stop_called(self) -> bool:
        """
        Return True if the STOP command was called on the player.

        This is used to differentiate between a user-initiated stop
        and a natural end of playback (e.g. end of track/queue).
        mainly for debugging/logging purposes by the streams controller.
        """
        return self.__stop_called

    def __hash__(self) -> int:
        """Return a hash of the Player."""
        return hash(self.player_id)

    def __str__(self) -> str:
        """Return a string representation of the Player."""
        return f"Player {self.name} ({self.player_id})"

    def __repr__(self) -> str:
        """Return a string representation of the Player."""
        return f"<Player name={self.name} id={self.player_id} available={self.available}>"

    def __eq__(self, other: object) -> bool:
        """Check equality of two Player objects."""
        if not isinstance(other, Player):
            return False
        return self.player_id == other.player_id

    def __ne__(self, other: object) -> bool:
        """Check inequality of two Player objects."""
        return not self.__eq__(other)


__all__ = [
    # explicitly re-export the models we imported from the models package,
    # for convenience reasons
    "EXTRA_ATTRIBUTES_TYPES",
    "DeviceInfo",
    "Player",
    "PlayerMedia",
    "PlayerSource",
    "PlayerState",
]


class GroupPlayer(Player):
    """Helper class for a (generic) group player."""

    _attr_type: PlayerType = PlayerType.GROUP

    @cached_property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # default implementation: groups can't be synced
        return None

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player.

        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        # Return all base config entries for a group player.
        # Feel free to override but ensure to include the base entries by calling super() first.
        # To override the default config entries, simply define an entry with the same key
        # and it will be used instead of the default one.
        return [
            *BASE_CONFIG_ENTRIES,
            CONF_ENTRY_PLAYER_ICON_GROUP,
            # add player control entries as hidden entries
            ConfigEntry(
                key=CONF_POWER_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_POWER_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_VOLUME_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_VOLUME_CONTROL,
                default_value=PLAYER_CONTROL_NATIVE,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_MUTE_CONTROL,
                type=ConfigEntryType.STRING,
                label=CONF_MUTE_CONTROL,
                # disable mute control for group players for now
                # TODO: work out if all child players support mute control
                default_value=PLAYER_CONTROL_NONE,
                hidden=True,
            ),
            CONF_ENTRY_AUTO_PLAY,
            # add default entries to hide player in UI and expose to HA
            (
                CONF_ENTRY_HIDE_PLAYER_IN_UI_ALWAYS_DEFAULT
                if self.hidden_by_default
                else CONF_ENTRY_HIDE_PLAYER_IN_UI_GROUP_PLAYER
            ),
            (
                CONF_ENTRY_EXPOSE_PLAYER_TO_HA
                if self.expose_to_ha_by_default
                else CONF_ENTRY_EXPOSE_PLAYER_TO_HA_DEFAULT_DISABLED
            ),
        ]

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        :param volume_level: volume level (0..100) to set on the player.
        """
        # Default implementation:
        # This will set the (relative) volume level on all child players.
        # free to override if you want to handle this differently.
        await self.mass.players.set_group_volume(self, volume_level)
