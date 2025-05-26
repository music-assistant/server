"""
Base model for a Player within Music Assistant.

All providerspecific players should inherit from this class and implement the required methods.

Note that the serverside Player object is not the same as the clientside Player object,
which is a dataclass in the models package containing the player state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast, final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, PlayerConfig
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo, PlayerState
from music_assistant_models.unique_list import UniqueList
from propcache import cached_property

from music_assistant.constants import (
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_AUTO_PLAY,
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_EXPOSE_PLAYER_TO_HA,
    CONF_ENTRY_EXPOSE_PLAYER_TO_HA_DEFAULT_DISABLED,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HIDE_PLAYER_IN_UI,
    CONF_ENTRY_HIDE_PLAYER_IN_UI_ALWAYS_DEFAULT,
    CONF_ENTRY_HIDE_PLAYER_IN_UI_GROUP_PLAYER,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_PLAYER_ICON_GROUP,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_FLOW_MODE,
    CONF_MUTE_CONTROL,
    CONF_NAME,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
)

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia, PlayerSource

    from .player_provider import PlayerProvider


BASE_CONFIG_ENTRIES = [
    # config entries that are valid for all player types
    CONF_ENTRY_PLAYER_ICON,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    CONF_ENTRY_OUTPUT_LIMITER,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_ENTRY_TTS_PRE_ANNOUNCE,
]

EXTRA_ATTRIBUTES_TYPES = str | int | float | bool | None


class Player(ABC):
    """
    Base representation of a Player within the Music Assistant Server.

    Player Provider implementations should inherit from this base model.
    """

    _attr_type: PlayerType = PlayerType.PLAYER
    _attr_supported_features: set[PlayerFeature]
    _attr_group_members: UniqueList[str]
    _attr_device_info: DeviceInfo
    _attr_can_group_with: set[str]
    _attr_source_list: UniqueList[PlayerSource]
    _attr_available: bool = True
    _attr_name: str | None = None
    _attr_powered: bool | None = None
    _attr_playback_state: PlaybackState = PlaybackState.IDLE
    _attr_volume_level: int | None = None
    _attr_volume_muted: bool | None = None
    _attr_elapsed_time: float | None = None
    _attr_elapsed_time_last_updated: datetime | None = None
    _attr_synced_to: str | None = None
    _atr_active_source: str | None = None
    _attr_current_media: PlayerMedia | None = None
    _attr_needs_poll: bool = False
    _attr_poll_interval: int = 30
    _attr_hidden_by_default: bool = False
    _attr_expose_to_ha_by_default: bool = False
    _attr_enabled_by_default: bool = True

    def __init__(self, provider: PlayerProvider, player_id: str) -> None:
        """Initialize the Player."""
        # set mass as public variable
        self.mass = provider.mass
        # initialize mutable attributes
        self._attr_supported_features = set()
        self._attr_group_members = UniqueList()
        self._attr_device_info = DeviceInfo()
        self._attr_can_group_with = set()
        self._attr_source_list = UniqueList()
        # do not override/overwrite these private attributes
        self._player_id = player_id
        self._provider = provider
        self._config = self.mass.config.get_base_player_config(player_id, self.provider_id)
        self._extra_data: dict[str, Any] = {}
        self._extra_attributes: dict[str, Any] = {}
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
    def powered(self) -> bool | None:
        """
        Return if the player is powered on.

        If the player does not support PlayerFeature.POWER,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_powered

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        return self._attr_playback_state

    @property
    def volume_level(self) -> int | None:
        """
        Return the current volume level (0..100) of the player.

        If the player does not support PlayerFeature.VOLUME_SET,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_volume_level

    @property
    def volume_muted(self) -> bool | None:
        """
        Return the current mute state of the player.

        If the player does not support PlayerFeature.VOLUME_MUTE,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_volume_muted

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
    def elapsed_time_last_updated(self) -> datetime | None:
        """
        Return when the elapsed time was last updated.

        return: The (UTC) datetime when the elapsed time was last updated,
        or None if it was never updated (or unknown).
        """
        return self._attr_elapsed_time_last_updated

    @property
    def group_members(self) -> UniqueList[str]:
        """
        Return the group members of the player.

        If there are other players synced/grouped with this player,
        this should return the id's of players synced to this player,
        and this should include the player's own id (as first item in the list).

        If there are currently no group members, this should return an empty list.
        """
        return self._attr_group_members

    @property
    def can_group_with(self) -> set[str]:
        """
        Return the id's of players this player can group with.

        This should return set of player_id's this player can group/sync with
        or just the provider's instance_id if all players can group with each other.
        """
        return self._attr_can_group_with

    @property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        """
        return self._attr_synced_to

    @property
    def active_source(self) -> str | None:
        """
        Return the (id of) the active source of the player.

        Set to None if the player is not currently playing a source or
        the player_id if the player is currently playing a MA queue.
        """
        return self._atr_active_source

    @property
    def source_list(self) -> UniqueList[PlayerSource]:
        """Return list of available (native) sources for this player."""
        return self._attr_source_list

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the current media being played by the player."""
        return self._attr_current_media

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

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command on the player.

        Will only be called if the PlayerFeature.POWER is supported.

        :param powered: bool if player should be powered on or off.
        """
        raise NotImplementedError

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Will only be called if the PlayerFeature.VOLUME_SET is supported.

        :param volume_level: volume level (0..100) to set on the player.
        """
        raise NotImplementedError

    async def volume_mute(self, muted: bool) -> None:
        """
        Handle VOLUME MUTE command on the player.

        Will only be called if the PlayerFeature.VOLUME_MUTE is supported.

        :param muted: bool if player should be muted.
        """
        # will only be called for players with Mute feature set.
        raise NotImplementedError

    @abstractmethod
    async def play(self) -> None:
        """Handle PLAY command on the player."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Handle STOP command on the player."""
        raise NotImplementedError

    async def pause(self) -> None:
        """
        Handle PAUSE command on the player.

        Will only be called if the player reports PlayerFeature.PAUSE is supported.
        """
        raise NotImplementedError

    async def next_track(self) -> None:
        """
        Handle NEXT_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        raise NotImplementedError

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player is not currently playing a MA queue.
        """
        raise NotImplementedError

    async def seek(self, position: int) -> None:
        """
        Handle SEEK command on the player.

        Seek to a specific position in the current track.
        Will only be called if the player reports PlayerFeature.SEEK is
        supported and the player is NOT currently playing a MA queue.

        :param position: The position to seek to, in seconds.
        """
        raise NotImplementedError

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
        raise NotImplementedError

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
        raise NotImplementedError

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
        raise NotImplementedError

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        raise NotImplementedError

    async def group_with(self, target_player_id: str) -> None:
        """
        Handle GROUP_WITH command on the player.

        Group this player to the given syncleader/target.
        Will only be called if the PlayerFeature.SET_MEMBERS is supported.

        :param target_player: player_id of the target player / sync leader.
        """
        raise NotImplementedError

    async def ungroup(self) -> None:
        """
        Handle UNGROUP command on the player.

        Remove the player from any (sync)groups it currently is grouped to.
        If this player is the sync leader (or group player),
        all child's will be ungrouped and the group dissolved.

        Will only be called if the PlayerFeature.SET_MEMBERS is supported.
        """
        raise NotImplementedError

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
        raise NotImplementedError

    async def poll(self) -> None:
        """
        Poll player for state updates.

        This is called by the Player Manager;
        if the 'needs_poll' property is True.
        """
        raise NotImplementedError

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # return all base config entries for a player,
        # feel free to override but ensure to call super() first
        # to override the default config entries, simply define an entry with the same key
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
        """Return the provider id of the player."""
        return self._provider.lookup_key

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

        This is a dict that can be used to pass any extra (serializable) attributes
        on the API or to the UI.
        This is not persisted and not used by the core logic.
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
        if custom_name := self._config.get_value(CONF_NAME):
            return custom_name
        return self.name

    @cached_property
    @final
    def enabled(self) -> bool:
        """Return if the player is enabled."""
        return self._config.enabled

    @cached_property
    @final
    def active_group(self) -> str | None:
        """
        Return the player id of the (first) playergroup that is currently active for this player.

        This will return the id of the groupplayer if a group is active.
        If no group is currently active, this will return None.
        """
        for player in self.mass.players.players(return_unavailable=False, return_disabled=False):
            if player.type != PlayerType.GROUP:
                continue
            if not (player.powered or player.playback_state == PlaybackState.PLAYING):
                continue
            if player.player_id in self.group_members:
                return player.player_id
        return None

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
    def state(self) -> PlayerState:
        """Return the current PlayerState of the player."""
        return self._state

    def to_dict(self) -> dict[str, Any]:
        """Return the (serializable) dict representation of the Player."""
        return self.state.to_dict()

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


class PlayerGroup(Player):
    """Helper class for (sync) PlayerGroups."""

    _attr_type: PlayerType = PlayerType.GROUP

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # return all base config entries for a group player,
        # feel free to override but ensure to call super() first
        # to override the default config entries, simply define an entry with the same key
        # and it will be used instead of the default one.
        return (
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
        )

    async def volume_set(self, volume_level: int) -> None:
        """
        Handle VOLUME_SET command on the player.

        Default implementation for group players:
        This will set the (relative) volume level on all child players.

        :param volume_level: volume level (0..100) to set on the player.
        """
        raise NotImplementedError
