"""
Base class/model for a Player within Music Assistant.

All providerspecific players should inherit from this class and implement the required methods.

Note that this is NOT the final state of the player,
as it may be overridden by (sync)group memberships, configuration options, or other factors.
This final state will be calculated and snapshotted in the PlayerState dataclass,
which is what is also what is sent over the API.
The final active source can be retrieved by using the 'state' property.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast, final

from music_assistant_models.constants import (
    EXTRA_ATTRIBUTES_TYPES,
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.player import (
    DeviceInfo,
    OutputProtocol,
    PlayerMedia,
    PlayerOption,
    PlayerOptionValueType,
    PlayerSoundMode,
    PlayerSource,
)
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.unique_list import UniqueList
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    ACTIVE_PROTOCOL_FEATURES,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_FAKE_MUTE,
    ATTR_FAKE_POWER,
    ATTR_FAKE_VOLUME,
    CONF_ENTRY_PLAYER_ICON,
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_FLOW_MODE,
    CONF_HIDE_IN_UI,
    CONF_LINKED_PROTOCOL_PLAYER_IDS,
    CONF_MUTE_CONTROL,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    PROTOCOL_FEATURES,
    PROTOCOL_PRIORITY,
)
from music_assistant.helpers.util import get_changed_dataclass_values

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, PlayerConfig
    from music_assistant_models.player_queue import PlayerQueue

    from .player_provider import PlayerProvider


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
    _attr_sound_mode_list: list[PlayerSoundMode]
    _attr_options: list[PlayerOption]
    _attr_available: bool = True
    _attr_name: str | None = None
    _attr_powered: bool | None = None
    _attr_playback_state: PlaybackState = PlaybackState.IDLE
    _attr_volume_level: int | None = None
    _attr_volume_muted: bool | None = None
    _attr_elapsed_time: float | None = None
    _attr_elapsed_time_last_updated: float | None = None
    _attr_active_source: str | None = None
    _attr_active_sound_mode: str | None = None
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
        self._attr_sound_mode_list = []
        self._attr_options = []
        # do not override/overwrite these private attributes below!
        self._cache: dict[str, Any] = {}  # storage dict for cached properties
        self.__attr_linked_protocols: list[OutputProtocol] = []
        self.__attr_protocol_parent_id: str | None = None
        self.__attr_active_output_protocol: str | None = None
        self._player_id = player_id
        self._provider = provider
        self.mass.config.create_default_player_config(
            player_id, self.provider_id, self.type, self.name, self.enabled_by_default
        )
        self._config = self.mass.config.get_base_player_config(player_id, self.provider_id)
        self._extra_data: dict[str, Any] = {}
        self._extra_attributes: dict[str, Any] = {}
        self._on_unload_callbacks: list[Callable[[], None]] = []
        self.__active_mass_source: str | None = None
        self.__initialized = asyncio.Event()
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
    def available(self) -> bool:
        """Return if the player is available."""
        return self._attr_available

    @property
    def type(self) -> PlayerType:
        """Return the type of the player."""
        return self._attr_type

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

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player needs flow mode for (queue) playback."""
        # Default implementation: True if the player does not support PlayerFeature.ENQUEUE
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
    def static_group_members(self) -> list[str]:
        """
        Return the static group members for a player group.

        For PlayerType.GROUP return the player_ids of members that must/can not be removed by
        the user. For all other player types return an empty list.
        """
        return self._attr_static_group_members

    @property
    def powered(self) -> bool | None:
        """
        Return if the player is powered on.

        If the player does not support PlayerFeature.POWER,
        or the state is (currently) unknown, this property may return None.
        """
        return self._attr_powered

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

    @property
    def active_source(self) -> str | None:
        """
        Return the (id of) the active source of the player.

        Only required if the player supports PlayerFeature.SELECT_SOURCE.

        Set to None if the player is not currently playing a source or
        the player_id if the player is currently playing a MA queue.
        """
        return self._attr_active_source

    @property
    def group_members(self) -> list[str]:
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

    @cached_property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # default implementation, feel free to override if your
        # provider has a more efficient way to determine this
        if self.group_members and self.group_members[0] != self.player_id:
            return self.group_members[0]
        for player in self.mass.players.all_players(
            return_unavailable=False, return_protocol_players=True
        ):
            if player.type == PlayerType.GROUP:
                continue
            if self.player_id in player.group_members and player.player_id != self.player_id:
                return player.player_id
        return None

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the current media being played by the player."""
        return self._attr_current_media

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        return self._attr_source_list

    @property
    def active_sound_mode(self) -> str | None:
        """Return active sound mode of this player."""
        return self._attr_active_sound_mode

    @cached_property
    def sound_mode_list(self) -> UniqueList[PlayerSoundMode]:
        """Return available PlayerSoundModes for Player."""
        return UniqueList(self._attr_sound_mode_list)

    @cached_property
    def options(self) -> UniqueList[PlayerOption]:
        """Return all PlayerOptions for Player."""
        return UniqueList(self._attr_options)

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

    async def stop(self) -> None:
        """
        Handle STOP command on the player.

        Will be called to stop the stream/playback if the player has play_media support.
        """
        raise NotImplementedError(
            "stop needs to be implemented when PlayerFeature.PLAY_MEDIA is set"
        )

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
        is supported and the player's currently selected source supports it.
        """
        raise NotImplementedError(
            "next_track needs to be implemented when PlayerFeature.NEXT_PREVIOUS is set"
        )

    async def previous_track(self) -> None:
        """
        Handle PREVIOUS_TRACK command on the player.

        Will only be called if the player reports PlayerFeature.NEXT_PREVIOUS
        is supported and the player's currently selected source supports it.
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
        raise NotImplementedError(
            "play_media needs to be implemented when PlayerFeature.PLAY_MEDIA is set"
        )

    async def on_protocol_playback(
        self,
        output_protocol: OutputProtocol,
    ) -> None:
        """
        Handle callback when playback starts on a protocol output.

        Called by the Player Controller after play_media is executed on a protocol player.
        Allows the native player implementation to perform special logic when protocol
        playback starts.

        Optional - providers can override to implement protocol-specific logic.

        :param output_protocol: The OutputProtocol object containing protocol details.
        """
        return  # Optional callback - no-op by default

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

    async def select_sound_mode(self, sound_mode: str) -> None:
        """
        Handle SELECT SOUND MODE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOUND_MODE is supported.

        :param source: The sound_mode(id) to select, as defined in the sound_mode_list.
        """
        raise NotImplementedError(
            "select_sound_mode needs to be implemented when PlayerFeature.SELECT_SOUND_MODE is set"
        )

    async def set_option(self, option_key: str, option_value: PlayerOptionValueType) -> None:
        """
        Handle SET_OPTION command on the player.

        Will only be called if the PlayerFeature.OPTIONS is supported.

        :param option_key: The option_key of the PlayerOption
        :param option_value: The new value of the PlayerOption
        """
        raise NotImplementedError(
            "set_option needs to be implemented when PlayerFeature.Option is set"
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
        """
        Return all (provider/player specific) Config Entries for the player.

        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        # Return any (player/provider specific) config entries for a player.
        # To override the default config entries, simply define an entry with the same key
        # and it will be used instead of the default one.
        return []

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
        target_player = self.mass.players.get_player(target_player_id, raise_unavailable=True)
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
            if parent_player := self.mass.players.get_player(self.synced_to):
                # if this player is synced to another player, remove self from that group
                await parent_player.set_members(player_ids_to_remove=[self.player_id])
        elif self.group_members:
            await self.set_members(player_ids_to_remove=self.group_members)

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
        """Return the (FINAL) display name of the player."""
        if custom_name := self._config.name:
            # always prefer the custom name over the default name
            return custom_name
        return self.name or self._config.default_name or self.player_id

    @cached_property
    @final
    def enabled(self) -> bool:
        """Return if the player is enabled."""
        return self._config.enabled

    @property
    @final
    def initialized(self) -> asyncio.Event:
        """
        Return if the player is initialized.

        Used by player controller to indicate initial registration completed.
        """
        return self.__initialized

    @property
    def corrected_elapsed_time(self) -> float | None:
        """Return the corrected/realtime elapsed time."""
        if self.elapsed_time is None or self.elapsed_time_last_updated is None:
            return None
        if self.playback_state == PlaybackState.PLAYING:
            return self.elapsed_time + (time.time() - self.elapsed_time_last_updated)
        return self.elapsed_time

    @cached_property
    @final
    def icon(self) -> str:
        """Return the player icon."""
        return cast("str", self._config.get_value(CONF_ENTRY_PLAYER_ICON.key))

    @cached_property
    @final
    def power_control(self) -> str:
        """Return the power control type."""
        if conf := self.mass.config.get_raw_player_config_value(self.player_id, CONF_POWER_CONTROL):
            return str(conf)
        # not explicitly set, use native if supported
        if PlayerFeature.POWER in self.supported_features:
            return PLAYER_CONTROL_NATIVE
        # note that we do not try to use protocol players for power control,
        # as this is very unlikely to be provided by a generic protocol and if it does,
        # it will be handled automatically on stream start/stop.
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def volume_control(self) -> str:
        """Return the volume control type."""
        if conf := self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_VOLUME_CONTROL
        ):
            return str(conf)
        # not explicitly set, use native if supported
        if PlayerFeature.VOLUME_SET in self.supported_features:
            return PLAYER_CONTROL_NATIVE
        # check for protocol player with volume support, and use that if found
        if protocol_player := self._get_protocol_player_for_feature(PlayerFeature.VOLUME_SET):
            return protocol_player.player_id
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def mute_control(self) -> str:
        """Return the mute control type."""
        if conf := self.mass.config.get_raw_player_config_value(self.player_id, CONF_MUTE_CONTROL):
            return str(conf)
        # not explicitly set, use native if supported
        if PlayerFeature.VOLUME_MUTE in self.supported_features:
            return PLAYER_CONTROL_NATIVE
        # check for protocol player with volume mute support, and use that if found
        if protocol_player := self._get_protocol_player_for_feature(PlayerFeature.VOLUME_MUTE):
            return protocol_player.player_id
        return PLAYER_CONTROL_NONE

    @cached_property
    @final
    def group_volume(self) -> int:
        """
        Return the group volume level.

        If this player is a group player or syncgroup, this will return the average volume
        level of all (powered on) child players in the group.
        If the player is not a group player or syncgroup, this will return the volume level
        of the player itself (if set), or 0 if not set.
        """
        if len(self.state.group_members) == 0:
            # player is not a group or syncgroup
            return self.state.volume_level or 0
        # calculate group volume from all (turned on) players
        group_volume = 0
        active_players = 0
        for child_player in self.mass.players.iter_group_members(
            self, only_powered=True, exclude_self=self.type != PlayerType.PLAYER
        ):
            if (child_volume := child_player.state.volume_level) is None:
                continue
            group_volume += child_volume
            active_players += 1
        if active_players:
            group_volume = int(group_volume / active_players)
        return group_volume

    @cached_property
    @final
    def hide_in_ui(self) -> bool:
        """
        Return the hide player in UI options.

        This is a convenience property based on the config entry.
        """
        return bool(self._config.get_value(CONF_HIDE_IN_UI, self.hidden_by_default))

    @cached_property
    @final
    def expose_to_ha(self) -> bool:
        """
        Return if the player should be exposed to Home Assistant.

        This is a convenience property that returns True if the player is set to be exposed
        to Home Assistant, based on the config entry.
        """
        return bool(self._config.get_value(CONF_EXPOSE_PLAYER_TO_HA, self.expose_to_ha_by_default))

    @property
    @final
    def mass_queue_active(self) -> bool:
        """
        Return if the/a Music Assistant Queue is currently active for this player.

        This is a convenience property that returns True if the
        player currently has a Music Assistant Queue as active source.
        """
        return bool(self.mass.players.get_active_queue(self))

    @cached_property
    @final
    def flow_mode(self) -> bool:
        """
        Return if the player(protocol) needs flow mode.

        Will use 'requires_flow_mode' unless overridden by flow_mode config.
        """
        # Check config override
        if bool(self._config.get_value(CONF_FLOW_MODE)) is True:
            # flow mode explicitly enabled in config
            return True
        return self.requires_flow_mode

    @property
    @final
    def supports_enqueue(self) -> bool:
        """
        Return if the player supports enqueueing tracks.

        This considers the active output protocol's capabilities if one is active.
        If a protocol player is active, checks that protocol's ENQUEUE feature.
        Otherwise checks the native player's ENQUEUE feature.
        """
        return self._check_feature_with_active_protocol(PlayerFeature.ENQUEUE)

    @property
    @final
    def supports_gapless(self) -> bool:
        """
        Return if the player supports gapless playback.

        This considers the active output protocol's capabilities if one is active.
        If a protocol player is active, checks that protocol's GAPLESS_PLAYBACK feature.
        Otherwise checks the native player's GAPLESS_PLAYBACK feature.
        """
        return self._check_feature_with_active_protocol(PlayerFeature.GAPLESS_PLAYBACK)

    @property
    @final
    def state(self) -> PlayerState:
        """Return the current (and FINAL) PlayerState of the player."""
        return self._state

    # Protocol-related properties and helpers

    @cached_property
    @final
    def is_native_player(self) -> bool:
        """Return True if this player is a native player."""
        is_universal_player = self.provider.domain == "universal_player"
        has_play_media = PlayerFeature.PLAY_MEDIA in self.supported_features
        return self.type != PlayerType.PROTOCOL and not is_universal_player and has_play_media

    @cached_property
    @final
    def output_protocols(self) -> list[OutputProtocol]:
        """
        Return all output options for this player.

        Includes:
        - Native playback (if player supports PLAY_MEDIA and is not a protocol/universal player)
        - Active protocol players from linked_output_protocols
        - Disabled protocols from cached linked_protocol_player_ids in config

        Each entry has an available flag indicating current availability.
        """
        result: list[OutputProtocol] = []

        # Add native playback option if applicable
        if self.is_native_player:
            result.append(
                OutputProtocol(
                    output_protocol_id="native",
                    name=self.provider.name,
                    protocol_domain=self.provider.domain,
                    priority=0,  # Native is always highest priority
                    available=self.available,
                    is_native=True,
                )
            )

        # Add active protocol players
        active_ids: set[str] = set()
        for linked in self.__attr_linked_protocols:
            active_ids.add(linked.output_protocol_id)
            # Check if the protocol player is actually available
            protocol_player = self.mass.players.get_player(linked.output_protocol_id)
            is_available = protocol_player.available if protocol_player else False
            if protocol_player and not is_available:
                self.logger.debug(
                    "Protocol player %s (%s) is unavailable for %s",
                    linked.output_protocol_id,
                    linked.protocol_domain,
                    self.display_name,
                )
            # Use provider name if available, else domain title
            if protocol_player:
                name = protocol_player.provider.name
            else:
                name = linked.protocol_domain.title() if linked.protocol_domain else "Unknown"
            result.append(
                OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=name,
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=is_available,
                )
            )

        # Add disabled protocols from cache
        cached_protocol_ids: list[str] = self.mass.config.get(
            f"{CONF_PLAYERS}/{self.player_id}/values/{CONF_LINKED_PROTOCOL_PLAYER_IDS}",
            [],
        )
        for protocol_id in cached_protocol_ids:
            if protocol_id in active_ids:
                continue  # Already included above
            # Get stored config to determine protocol domain
            if raw_conf := self.mass.config.get(f"{CONF_PLAYERS}/{protocol_id}"):
                provider_id = raw_conf.get("provider", "")
                protocol_domain = provider_id.split("--")[0] if provider_id else "unknown"
                priority = PROTOCOL_PRIORITY.get(protocol_domain, 100)
                result.append(
                    OutputProtocol(
                        output_protocol_id=protocol_id,
                        name=protocol_domain.title(),
                        protocol_domain=protocol_domain,
                        priority=priority,
                        available=False,  # Disabled protocols are not available
                    )
                )

        # Sort by priority (lower = more preferred)
        result.sort(key=lambda o: o.priority)
        return result

    @property
    @final
    def linked_output_protocols(self) -> list[OutputProtocol]:
        """Return the list of actively linked output protocol players."""
        return self.__attr_linked_protocols

    @property
    @final
    def protocol_parent_id(self) -> str | None:
        """Return the parent player_id if this is a protocol player linked to a native player."""
        return self.__attr_protocol_parent_id

    @property
    @final
    def active_output_protocol(self) -> str | None:
        """Return the currently active output protocol ID."""
        return self.__attr_active_output_protocol

    @final
    def set_active_output_protocol(self, protocol_id: str | None) -> None:
        """
        Set the currently active output protocol ID.

        :param protocol_id: The protocol player_id to set as active, "native" for native playback,
            or None to clear the active protocol.
        """
        if self.__attr_active_output_protocol == protocol_id:
            return  # No change
        if protocol_id == self.player_id:
            protocol_id = "native"  # Normalize to "native" for native player
        if protocol_id:
            protocol_name = protocol_id
            if protocol_id == "native":
                protocol_name = "Native"
            elif protocol_player := self.mass.players.get_player(protocol_id):
                protocol_name = protocol_player.provider.name
            self.logger.info(
                "Setting active output protocol on %s to %s",
                self.display_name,
                protocol_name,
            )
        else:
            self.logger.info(
                "Clearing active output protocol on %s",
                self.display_name,
            )
        self.__attr_active_output_protocol = protocol_id
        self.update_state()

    @final
    def set_linked_output_protocols(self, protocols: list[OutputProtocol]) -> None:
        """
        Set the actively linked output protocol players.

        :param protocols: List of OutputProtocol objects representing active protocol players.
        """
        self.__attr_linked_protocols = protocols
        self.mass.players.trigger_player_update(self.player_id)

    @final
    def set_protocol_parent_id(self, parent_id: str | None) -> None:
        """
        Set the parent player_id for protocol players.

        :param parent_id: The player_id of the parent player, or None to clear.
        """
        self.__attr_protocol_parent_id = parent_id
        self.mass.players.trigger_player_update(self.player_id)

    @final
    def get_linked_protocol(self, protocol_domain: str) -> OutputProtocol | None:
        """Get a linked protocol by domain with current availability."""
        for linked in self.__attr_linked_protocols:
            if linked.protocol_domain == protocol_domain:
                protocol_player = self.mass.players.get_player(linked.output_protocol_id)
                current_available = protocol_player.available if protocol_player else False
                return OutputProtocol(
                    output_protocol_id=linked.output_protocol_id,
                    name=protocol_player.provider.name
                    if protocol_player
                    else linked.protocol_domain.title(),
                    protocol_domain=linked.protocol_domain,
                    priority=linked.priority,
                    available=current_available,
                    is_native=False,
                )
        return None

    @final
    def get_output_protocol_by_domain(self, protocol_domain: str) -> OutputProtocol | None:
        """
        Get an output protocol by domain, including native protocol.

        Unlike get_linked_protocol, this also checks if the player's native protocol
        matches the requested domain.

        :param protocol_domain: The protocol domain to search for (e.g., "airplay", "sonos").
        """
        for output_protocol in self.output_protocols:
            if output_protocol.protocol_domain == protocol_domain:
                return output_protocol
        return None

    @final
    def get_protocol_player(self, player_id: str) -> Player | None:
        """Get the protocol Player for a given player_id."""
        if player_id == "native":
            return self if PlayerFeature.PLAY_MEDIA in self.supported_features else None
        return self.mass.players.get_player(player_id)

    @final
    def get_preferred_protocol_player(self) -> Player | None:
        """Get the best available protocol player by priority."""
        for linked in sorted(self.__attr_linked_protocols, key=lambda x: x.priority):
            if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                if protocol_player.available:
                    return protocol_player
        return None

    @final
    def update_state(self, force_update: bool = False, signal_event: bool = True) -> None:
        """
        Update the PlayerState from the current state of the player.

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
        changed_values = self.__calculate_player_state()
        if prev_media_checksum != self._get_player_media_checksum():
            # current media changed, call the media updated callback
            # debounce the callback to avoid multiple calls when multiple
            # state updates happen in a short time
            self.mass.call_later(
                1, self._on_player_media_updated, task_id=f"player_media_updated_{self.player_id}"
            )
        # ignore some values that are not relevant for the state
        changed_values.pop("elapsed_time_last_updated", None)
        changed_values.pop("extra_attributes.seq_no", None)
        changed_values.pop("extra_attributes.last_poll", None)
        changed_values.pop("current_media.elapsed_time_last_updated", None)
        # persist the default name if it changed
        if self.name and self.config.default_name != self.name:
            self.mass.config.set_player_default_name(self.player_id, self.name)
        # persist the player type if it changed
        if self.type != self._config.player_type:
            self.mass.config.set_player_type(self.player_id, self.type)
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
    def set_initialized(self) -> None:
        """Set the player as initialized."""
        self.__initialized.set()

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

    @final
    def _get_player_media_checksum(self) -> str:
        """Return a checksum for the current media."""
        if not (media := self.state.current_media):
            return ""
        return (
            f"{media.uri}|{media.title}|{media.source_id}|{media.queue_item_id}|"
            f"{media.image_url}|{media.duration}|{media.elapsed_time}"
        )

    @final
    def _check_feature_with_active_protocol(
        self, feature: PlayerFeature, active_only: bool = False
    ) -> bool:
        """
        Check if a feature is supported considering the active output protocol.

        If an active output protocol is set (and not native), checks that protocol
        player's features. Otherwise checks the native player's features.

        :param feature: The PlayerFeature to check.
        :return: True if the feature is supported by the active protocol or native player.
        """
        # If active output protocol is set and not native, check protocol player's features
        if (
            self.__attr_active_output_protocol
            and self.__attr_active_output_protocol != "native"
            and (
                protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol)
            )
        ):
            return feature in protocol_player.supported_features
        # Otherwise check native player's features
        return feature in self.supported_features

    @final
    def _get_protocol_player_for_feature(
        self,
        feature: PlayerFeature,
    ) -> Player | None:
        """Get player(protocol) which has the given PlayerFeature."""
        # prefer native player
        if feature in self.supported_features:
            return self
        # Otherwise, use the first available linked protocol
        for linked in self.linked_output_protocols:
            if (
                (protocol_player := self.mass.players.get_player(linked.output_protocol_id))
                and protocol_player.available
                and feature in protocol_player.supported_features
            ):
                return protocol_player

        return None

    @final
    def __calculate_player_state(
        self,
    ) -> dict[str, tuple[Any, Any]]:
        """
        Calculate the (current) and FINAL PlayerState.

        This method is called when we're updating the player,
        and we compare the current state with the previous state to determine
        if we need to signal a state change to API consumers.

        Returns a dict with the state attributes that have changed.
        """
        playback_state, elapsed_time, elapsed_time_last_updated = self.__final_playback_state
        prev_state = deepcopy(self._state)
        self._state = PlayerState(
            player_id=self.player_id,
            provider=self.provider_id,
            type=self.type,
            available=self.enabled and self.available,
            device_info=self.device_info,
            supported_features=self.__final_supported_features,
            playback_state=playback_state,
            elapsed_time=elapsed_time,
            elapsed_time_last_updated=elapsed_time_last_updated,
            powered=self.__final_power_state,
            volume_level=self.__final_volume_level,
            volume_muted=self.__final_volume_muted_state,
            group_members=UniqueList(self.__final_group_members),
            static_group_members=UniqueList(self.static_group_members),
            can_group_with=self.__final_can_group_with,
            synced_to=self.__final_synced_to,
            active_source=self.__final_active_source,
            source_list=self.__final_source_list,
            active_group=self.__final_active_group,
            current_media=self.__final_current_media,
            active_sound_mode=self.active_sound_mode,
            sound_mode_list=self.sound_mode_list,
            options=self.options,
            name=self.display_name,
            enabled=self.enabled,
            hide_in_ui=self.hide_in_ui,
            expose_to_ha=self.expose_to_ha,
            icon=self.icon,
            group_volume=self.group_volume,
            extra_attributes=self.extra_attributes,
            power_control=self.power_control,
            volume_control=self.volume_control,
            mute_control=self.mute_control,
            output_protocols=self.output_protocols,
            active_output_protocol=self.__attr_active_output_protocol,
        )

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
            # when we're going to idle,
            # we want to reset the active mass source after a short delay
            # this is done using a timer which gets reset if the player starts playing again
            # before the timer is up, using the task_id
            self.mass.call_later(
                2, self.set_active_mass_source, None, task_id=f"set_mass_source_{self.player_id}"
            )
        return get_changed_dataclass_values(
            prev_state,
            self._state,
            recursive=True,
        )

    @cached_property
    @final
    def __final_playback_state(self) -> tuple[PlaybackState, float | None, float | None]:
        """
        Return the FINAL playback state based on the playercontrol which may have been set-up.

        Returns a tuple of (playback_state, elapsed_time, elapsed_time_last_updated).
        """
        # If an output protocol is active (and not native), use the protocol player's state
        if (
            self.__attr_active_output_protocol
            and self.__attr_active_output_protocol != "native"
            and (
                protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol)
            )
            and protocol_player.playback_state != PlaybackState.IDLE
        ):
            return (
                protocol_player.state.playback_state,
                protocol_player.state.elapsed_time,
                protocol_player.state.elapsed_time_last_updated,
            )
        # If we're synced, use the syncleader state for playback state and elapsed time
        # NOTE: Don't do this for the active group player,
        # because the group player relies on the sync leader for state info.
        parent_id = self.__final_synced_to
        if parent_id and (parent_player := self.mass.players.get_player(parent_id)):
            return (
                parent_player.state.playback_state,
                parent_player.state.elapsed_time,
                parent_player.state.elapsed_time_last_updated,
            )
        return (self.playback_state, self.elapsed_time, self.elapsed_time_last_updated)

    @cached_property
    @final
    def __final_power_state(self) -> bool | None:
        """Return the FINAL power state based on the playercontrol which may have been set-up."""
        power_control = self.power_control
        if power_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_POWER, False))
        if power_control == PLAYER_CONTROL_NATIVE:
            return self.powered
        if power_control == PLAYER_CONTROL_NONE:
            return None
        # handle player control for power if set
        if control := self.mass.players.get_player_control(power_control):
            return control.power_state
        return None

    @cached_property
    @final
    def __final_volume_level(self) -> int | None:
        """Return the FINAL volume level based on the playercontrol which may have been set-up."""
        volume_control = self.volume_control
        if volume_control == PLAYER_CONTROL_FAKE:
            return int(self.extra_data.get(ATTR_FAKE_VOLUME, 0))
        if volume_control == PLAYER_CONTROL_NATIVE:
            return self.volume_level
        if volume_control == PLAYER_CONTROL_NONE:
            return None
        # handle protocol player as volume control
        if control := self.mass.players.get_player(volume_control):
            return control.volume_level
        # handle player control for volume if set
        if player_control := self.mass.players.get_player_control(volume_control):
            return player_control.volume_level
        return None

    @cached_property
    @final
    def __final_volume_muted_state(self) -> bool | None:
        """Return the FINAL mute state based on any playercontrol which may have been set-up."""
        mute_control = self.mute_control
        if mute_control == PLAYER_CONTROL_FAKE:
            return bool(self.extra_data.get(ATTR_FAKE_MUTE, False))
        if mute_control == PLAYER_CONTROL_NATIVE:
            return self.volume_muted
        if mute_control == PLAYER_CONTROL_NONE:
            return None
        # handle protocol player as mute control
        if control := self.mass.players.get_player(mute_control):
            return control.volume_muted
        # handle player control for mute if set
        if player_control := self.mass.players.get_player_control(mute_control):
            return player_control.volume_muted
        return None

    @cached_property
    @final
    def __final_active_group(self) -> str | None:
        """
        Return the player id of any playergroup that is currently active for this player.

        This will return the id of the groupplayer if any groups are active.
        If no groups are currently active, this will return None.
        """
        if self.type == PlayerType.PROTOCOL:
            # protocol players should not have an active group,
            # they follow the group state of their parent player
            return None
        for group_player in self.mass.players.all_players(
            return_unavailable=False, return_disabled=False
        ):
            if group_player.type != PlayerType.GROUP:
                continue
            if group_player.player_id == self.player_id:
                continue
            if group_player.playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                continue
            if self.player_id in group_player.group_members:
                return group_player.player_id
        return None

    @cached_property
    @final
    def __final_current_media(self) -> PlayerMedia | None:
        """Return the FINAL current media for the player."""
        if self.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            # if an announcement is in progress, return announcement details
            return PlayerMedia(
                uri="announcement",
                media_type=MediaType.ANNOUNCEMENT,
                title="ANNOUNCEMENT",
            )

        # if the player is grouped/synced, use the current_media of the group/parent player
        if parent_player_id := (self.__final_active_group or self.__final_synced_to):
            if parent_player_id != self.player_id and (
                parent_player := self.mass.players.get_player(parent_player_id)
            ):
                return parent_player.state.current_media
            return None  # if parent player not found, return None for current media
        # if this is a protocol player, use the current_media of the parent player
        if self.type == PlayerType.PROTOCOL and self.__attr_protocol_parent_id:
            if parent_player := self.mass.players.get_player(self.__attr_protocol_parent_id):
                return parent_player.state.current_media
        # if a pluginsource is currently active, return those details
        active_source = self.__final_active_source
        if (
            active_source
            and (source := self.mass.players.get_plugin_source(active_source))
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
        active_queue: PlayerQueue | None = None
        if not active_queue and active_source:
            active_queue = self.mass.player_queues.get(active_source)
        if not active_queue and self.active_source is None:
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
        if active_queue:
            # queue is active but no current item
            return None
        # return native current media if no group/queue is active
        if self.current_media:
            return PlayerMedia(
                uri=self.current_media.uri,
                media_type=self.current_media.media_type,
                title=self.current_media.title,
                artist=self.current_media.artist,
                album=self.current_media.album,
                image_url=self.current_media.image_url,
                duration=self.current_media.duration,
                source_id=self.current_media.source_id or active_source,
                queue_item_id=self.current_media.queue_item_id,
                elapsed_time=self.current_media.elapsed_time or int(self.elapsed_time)
                if self.elapsed_time
                else None,
                elapsed_time_last_updated=self.current_media.elapsed_time_last_updated
                or self.elapsed_time_last_updated,
            )
        return None

    @cached_property
    @final
    def __final_source_list(self) -> UniqueList[PlayerSource]:
        """Return the FINAL source list for the player."""
        sources = UniqueList(self.source_list)
        if self.type == PlayerType.PROTOCOL:
            return sources
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

    @cached_property
    @final
    def __final_group_members(self) -> list[str]:
        """Return the FINAL group members of this player."""
        if self.__final_synced_to:
            # If player is synced to another player, it has no group members itself
            return []

        # Start by translating native group_members to visible player IDs
        # This handles cases where a native player (e.g., native AirPlay) has grouped
        # protocol players (e.g., Sonos AirPlay protocol players) that need translation
        members: list[str] = []
        if self.type == PlayerType.PROTOCOL:
            # protocol players use their own group members without translation
            members.extend(self.group_members)
        else:
            translated_members = self._translate_protocol_ids_to_visible(set(self.group_members))
            for member in translated_members:
                if member.player_id not in members:
                    members.append(member.player_id)

        # If there's an active linked protocol, include its group members (translated)
        if self.__attr_active_output_protocol and self.__attr_active_output_protocol != "native":
            if protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol):
                # Translate protocol player IDs to visible player IDs
                protocol_members = self._translate_protocol_ids_to_visible(
                    set(protocol_player.group_members)
                )
                for member in protocol_members:
                    if member.player_id not in members:
                        members.append(member.player_id)

        if self.type != PlayerType.GROUP:
            # Ensure the player_id is first in the group_members list
            if len(members) > 0 and members[0] != self.player_id:
                members = [self.player_id, *[m for m in members if m != self.player_id]]
            # If the only member is self, return empty list
            if members == [self.player_id]:
                return []
        return members

    @cached_property
    @final
    def __final_synced_to(self) -> str | None:
        """
        Return the FINAL synced_to state.

        This checks both native sync state and protocol player sync state,
        translating protocol player IDs to visible player IDs.
        """
        # First check the native synced_to from the property
        if native_synced_to := self.synced_to:
            return native_synced_to

        for linked in self.__attr_linked_protocols:
            if not (protocol_player := self.mass.players.get_player(linked.output_protocol_id)):
                continue
            if protocol_player.synced_to:
                # Protocol player is synced, translate to visible player
                if proto_sync_parent := self.mass.players.get_player(protocol_player.synced_to):
                    if proto_sync_parent.type != PlayerType.PROTOCOL:
                        # Sync parent is already a visible player (e.g., native AirPlay player)
                        return proto_sync_parent.player_id
                    if proto_sync_parent.protocol_parent_id and (
                        parent := self.mass.players.get_player(proto_sync_parent.protocol_parent_id)
                    ):
                        # Sync parent is a protocol player, return its visible parent
                        return parent.player_id

        return None

    @cached_property
    @final
    def __final_supported_features(self) -> set[PlayerFeature]:
        """Return the FINAL supported features based supported output protocol(s)."""
        base_features = self.supported_features.copy()
        if self.__attr_active_output_protocol and self.__attr_active_output_protocol != "native":
            # Active linked protocol: add from that specific protocol
            if protocol_player := self.mass.players.get_player(self.__attr_active_output_protocol):
                for feature in protocol_player.supported_features:
                    if feature in ACTIVE_PROTOCOL_FEATURES:
                        base_features.add(feature)
        # Append (allowed features) from all linked protocols
        for linked in self.__attr_linked_protocols:
            if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                for feature in protocol_player.supported_features:
                    if feature in PROTOCOL_FEATURES:
                        base_features.add(feature)
        return base_features

    @cached_property
    @final
    def __final_can_group_with(self) -> set[str]:
        """
        Return the FINAL set of player id's this player can group with.

        This is a convenience property which calculates the final can_group_with set
        based on any linked protocol players and current player/grouped state.

        If player is synced to a native parent: return empty set (already grouped).
        If player is synced to a protocol: can still group with other players.
        If no active linked protocol: return can_group_with from all active output protocols.
        If active linked protocol: return native can_group_with + active protocol's.

        All protocol player IDs are translated to their visible parent player IDs.
        """

        def _should_include_player(player: Player) -> bool:
            """Check if a player should be included in the can-group-with set."""
            if not player.available:
                return False
            if player.player_id == self.player_id:
                return False  # Don't include self
            # Don't include (playing) players that have group members (they are group leaders)
            if (  # noqa: SIM103
                player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                and player.group_members
            ):
                return False
            return True

        if self.__final_synced_to:
            # player is already synced/grouped, cannot group with others
            return set()

        expanded_can_group_with = self._expand_can_group_with()
        # Scenario 1: Player is a protocol player - just return the (expanded) result
        if self.type == PlayerType.PROTOCOL:
            return {x.player_id for x in expanded_can_group_with}

        result: set[str] = set()
        # always start with the native can_group_with options (expanded from provider instance IDs)
        # NOTE we need to translate protocol player IDs to visible player IDs here as well,
        # to cover cases where a native player (e.g., native AirPlay) has grouped protocol players
        # (e.g., Sonos AirPlay protocol players)
        for player in expanded_can_group_with:
            if player.type == PlayerType.PROTOCOL:
                if not player.protocol_parent_id:
                    continue
                parent_player = self.mass.players.get_player(player.protocol_parent_id)
                if not parent_player or not _should_include_player(parent_player):
                    continue
                result.add(parent_player.player_id)
            elif _should_include_player(player):
                result.add(player.player_id)

        # Scenario 2: External source is active - don't include protocol-based grouping
        # When an external source (e.g., Spotify Connect, TV) is active, grouping via
        # protocols (AirPlay, Sendspin, etc.) wouldn't work - only native grouping is available.
        if self._has_external_source_active():
            return result

        # Translate can_group_with from active linked protocol(s) and add to result
        for linked in self.__attr_linked_protocols:
            if protocol_player := self.mass.players.get_player(linked.output_protocol_id):
                for player in self._translate_protocol_ids_to_visible(
                    protocol_player.state.can_group_with
                ):
                    if not _should_include_player(player):
                        continue
                    result.add(player.player_id)
        return result

    @cached_property
    @final
    def __final_active_source(self) -> str | None:
        """
        Calculate the final active source based on any group memberships, source plugins etc.

        Note: When an output protocol is active, the source remains the parent player's
        source since protocol players don't have their own queue/source - they only
        handle the actual streaming/playback.
        """
        # if the player is grouped/synced, use the active source of the group/parent player
        if parent_player_id := (self.__final_synced_to or self.__final_active_group):
            if parent_player := self.mass.players.get_player(parent_player_id):
                return parent_player.state.active_source
            return None  # should not happen but just in case
        if self.type == PlayerType.PROTOCOL:
            if self.protocol_parent_id and (
                parent_player := self.mass.players.get_player(self.protocol_parent_id)
            ):
                # if this is a protocol player, use the active source of the parent player
                return parent_player.state.active_source
            # fallback to None here if parent player not found,
            # protocol players should not have an active source themselves
            return None
        # if a plugin source is active that belongs to this player, return that
        for plugin_source in self.mass.players.get_plugin_sources():
            if plugin_source.in_use_by == self.player_id:
                return plugin_source.id
        output_protocol_domain: str | None = None
        if self.active_output_protocol and self.active_output_protocol != "native":
            if protocol_player := self.mass.players.get_player(self.active_output_protocol):
                output_protocol_domain = protocol_player.provider.domain
        # active source as reported by the player itself
        if (
            self.active_source
            # try to catch cases where player reports an active source
            # that is actually from an active output protocol (e.g. AirPlay)
            and self.active_source.lower() != output_protocol_domain
        ):
            return self.active_source
        # return the (last) known MA source - fallback to player's own queue source if none
        return self.__active_mass_source or self.player_id

    @final
    def _translate_protocol_ids_to_visible(self, player_ids: set[str]) -> set[Player]:
        """
        Translate protocol player IDs to their visible parent players.

        Protocol players are hidden and users interact with visible players
        (native or universal). This method translates protocol player IDs
        back to the visible (parent) players.

        :param player_ids: Set of player IDs.
        :return: Set of visible players.
        """
        result: set[Player] = set()
        if not player_ids:
            return result
        for player_id in player_ids:
            target_player = self.mass.players.get_player(player_id)
            if not target_player:
                continue
            if target_player.type != PlayerType.PROTOCOL:
                # Non-protocol player is already visible - include directly
                result.add(target_player)
                continue
            # This is a protocol player - find its visible parent
            if not target_player.protocol_parent_id:
                continue
            parent_player = self.mass.players.get_player(target_player.protocol_parent_id)
            if not parent_player:
                continue
            result.add(parent_player)
        return result

    @final
    def _has_external_source_active(self) -> bool:
        """
        Check if an external (non-MA-managed) source is currently active.

        External sources include things like Spotify Connect, TV input, etc.
        When an external source is active, protocol-based grouping is not available.

        :return: True if an external source is active, False otherwise.
        """
        active_source = self.__final_active_source
        if active_source is None:
            return False

        # Player's own ID means MA queue is (or was) active
        if active_source == self.player_id:
            return False

        # Check if it's a known queue ID
        if self.mass.player_queues.get(active_source):
            return False

        # Check if it's a plugin source - if not, it's an external source
        return not any(
            plugin_source.id == active_source
            for plugin_source in self.mass.players.get_plugin_sources()
        )

    @final
    def _expand_can_group_with(self) -> set[Player]:
        """
        Expand the 'can-group-with' to include all players from provider instance IDs.

        This method expands any provider instance IDs (e.g., "airplay", "chromecast")
        in the group members to all (available) players of that provider

        :return: Set of available players in the can-group-with.
        """
        result = set()

        for member_id in self.can_group_with:
            if player := self.mass.players.get_player(member_id):
                result.add(player)
                continue  # already a player ID
            # Check if member_id is a provider instance ID
            if provider := self.mass.get_provider(member_id):
                for player in self.mass.players.all_players(
                    return_unavailable=False,  # Only include available players
                    provider_filter=provider.instance_id,
                    return_protocol_players=True,
                ):
                    result.add(player)
        return result

    # The id of the (last) active mass source.
    # This is to keep track of the last active MA source for the player,
    # so we can restore it when needed (e.g. after switching to a plugin source).
    __active_mass_source: str | None = None

    @final
    def set_active_mass_source(self, value: str | None) -> None:
        """
        Set the id of the (last) active mass source.

        This is to keep track of the last active MA source for the player,
        so we can restore it when needed (e.g. after switching to a plugin source).
        """
        self.mass.cancel_timer(f"set_mass_source_{self.player_id}")
        self.__active_mass_source = value
        self.update_state()

    __stop_called: bool = False

    @final
    def mark_stop_called(self) -> None:
        """Mark that the STOP command was called on the player."""
        self.__stop_called = True

    @property
    @final
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
