"""
Controller for (provider specific) SyncGroup players.

A SyncGroup player is a virtual player that automatically groups multiple players
together in a sync group, where one player is the sync leader
and the other players are synced to that leader.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, cast

import shortuuid
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ConfigEntryType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_ENABLE_ICY_METADATA,
    CONF_FLOW_MODE,
    CONF_GROUP_MEMBERS,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CODEC,
    CONF_SAMPLE_RATES,
    CONF_SMART_FADES_MODE,
    SYNCGROUP_PREFIX,
)
from music_assistant.models.player import GroupPlayer, Player

if TYPE_CHECKING:
    from music_assistant.models.player_provider import PlayerProvider

    from .player_controller import PlayerController


SUPPORT_DYNAMIC_LEADER = {
    # providers that support dynamic leader selection in a syncgroup
    # meaning that if you would remove the current leader from the group,
    # the provider will automatically select a new leader from the remaining members
    # and the music keeps playing uninterrupted.
    "airplay",
    "squeezelite",
    # TODO: Get this working with Sonos as well (need to handle range requests)
}

OPTIONAL_FEATURES = {
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.PAUSE,
    PlayerFeature.PLAY_ANNOUNCEMENT,
    PlayerFeature.SEEK,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.VOLUME_MUTE,
}


class SyncGroupPlayer(GroupPlayer):
    """Helper class for a (provider specific) SyncGroup player."""

    _attr_type: PlayerType = PlayerType.GROUP
    sync_leader: Player | None = None
    """The active sync leader player for this syncgroup."""

    @cached_property
    def is_dynamic(self) -> bool:
        """Return if the player is a dynamic group player."""
        return bool(self.config.get_value(CONF_DYNAMIC_GROUP_MEMBERS, False))

    def __init__(
        self,
        provider: PlayerProvider,
        player_id: str,
    ) -> None:
        """Initialize GroupPlayer instance."""
        super().__init__(provider, player_id)
        self._attr_name = self.config.name or self.config.default_name or f"SyncGroup {player_id}"
        self._attr_available = True
        self._attr_powered = False  # group players are always powered off by default
        self._attr_device_info = DeviceInfo(model="Sync Group", manufacturer=provider.name)
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
        }

    async def on_config_updated(self) -> None:
        """Handle logic when the player is loaded or updated."""
        # Config is only available after the player was registered
        static_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        self._attr_static_group_members = static_members.copy()
        if not self.powered:
            self._attr_group_members = static_members.copy()
        if self.is_dynamic:
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        else:
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        if self.sync_leader:
            base_features = self._attr_supported_features.copy()
            # add features supported by the sync leader
            for feature in OPTIONAL_FEATURES:
                if feature in self.sync_leader.supported_features:
                    base_features.add(feature)
            return base_features
        return self._attr_supported_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        if self.powered:
            return self.sync_leader.playback_state if self.sync_leader else PlaybackState.IDLE
        else:
            return PlaybackState.IDLE

    @cached_property
    def flow_mode(self) -> bool:
        """
        Return if the player needs flow mode.

        Will by default be set to True if the player does not support PlayerFeature.ENQUEUE
        or has a flow mode config entry set to True.
        """
        if leader := self.sync_leader:
            return leader.flow_mode
        return False

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track (if any)."""
        return self.sync_leader.elapsed_time if self.sync_leader else None

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return when the elapsed time was last updated."""
        return self.sync_leader.elapsed_time_last_updated if self.sync_leader else None

    @property
    def _current_media(self) -> PlayerMedia | None:
        """Return the current media item (if any) loaded in the player."""
        return self.sync_leader._current_media if self.sync_leader else self._attr_current_media

    @property
    def _active_source(self) -> str | None:
        """Return the active source id (if any) of the player."""
        return self.sync_leader._active_source if self.sync_leader else self._attr_active_source

    @property
    def _source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        if self.sync_leader:
            return self.sync_leader._source_list
        return []

    @property
    def can_group_with(self) -> set[str]:
        """
        Return the id's of players this player can group with.

        This should return set of player_id's this player can group/sync with
        or just the provider's instance_id if all players can group with each other.
        """
        if self.is_dynamic and (leader := self.sync_leader):
            return leader.can_group_with
        elif self.is_dynamic:
            return {self.provider.instance_id}
        else:
            return set()

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries: list[ConfigEntry] = [
            # default entries for player groups
            *await super().get_config_entries(action=action, values=values),
            # add syncgroup specific entries
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                label="Group members",
                default_value=[],
                description="Select all players you want to be part of this group",
                required=False,  # needed for dynamic members (which allows empty members list)
                options=[
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.provider.players
                    if x.type != PlayerType.GROUP
                ],
            ),
            ConfigEntry(
                key="dynamic_members",
                type=ConfigEntryType.BOOLEAN,
                label="Enable dynamic members",
                description="Allow (un)joining members dynamically, so the group more or less "
                "behaves the same like manually syncing players together, "
                "with the main difference being that the group player will hold the queue.",
                default_value=False,
                required=False,
            ),
        ]
        # combine base group entries with (base) player entries for this player type
        child_player = next((x for x in self.provider.players if x.type == PlayerType.PLAYER), None)
        if child_player:
            allowed_conf_entries = (
                CONF_HTTP_PROFILE,
                CONF_ENABLE_ICY_METADATA,
                CONF_CROSSFADE_DURATION,
                CONF_OUTPUT_CODEC,
                CONF_FLOW_MODE,
                CONF_SAMPLE_RATES,
                CONF_SMART_FADES_MODE,
            )
            child_config_entries = await child_player.get_config_entries()
            entries.extend(
                [entry for entry in child_config_entries if entry.key in allowed_conf_entries]
            )
        return entries

    async def stop(self) -> None:
        """Send STOP command to given player."""
        if sync_leader := self.sync_leader:
            await sync_leader.stop()

    async def play(self) -> None:
        """Send PLAY command to given player."""
        if sync_leader := self.sync_leader:
            await sync_leader.play()

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        if sync_leader := self.sync_leader:
            await sync_leader.pause()

    async def power(self, powered: bool) -> None:
        """Handle POWER command to group player."""
        prev_power = self._attr_powered

        # always stop at power off
        if not powered and self.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.stop()
            self._attr_current_media = None

        # optimistically set the group state
        self._attr_powered = powered
        if prev_power != powered:
            self.update_state()

        if powered:
            # ensure static members are present when powering on
            for static_group_member in self._attr_static_group_members:
                member_player = self.mass.players.get(static_group_member)
                if not member_player or not member_player.available or not member_player.enabled:
                    if static_group_member in self._attr_group_members:
                        self._attr_group_members.remove(static_group_member)
                    continue
                if static_group_member not in self._attr_group_members:
                    # Always add static members when power(true) is called,
                    # this will ensure that static members that just became available are added
                    self._attr_group_members.append(static_group_member)
            # Select sync leader and handle turn on
            new_leader = self._select_sync_leader()
            # handle TURN_ON of the group player by turning on all members
            for member in self.mass.players.iter_group_members(
                self, only_powered=False, active_only=False
            ):
                await self._handle_member_collisions(member)
                if not member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await self.mass.players._handle_cmd_power(member.player_id, True)
            # Set up the sync group with the new leader
            if prev_power and new_leader == self.sync_leader:
                # Already powered on with same leader, just re-sync members without full transition
                await self._form_syncgroup()
            else:
                await self._handle_leader_transition(new_leader)
        elif prev_power and not powered:
            # handle TURN_OFF of the group player by dissolving group and turning off all members
            await self._dissolve_syncgroup()
            # turn off all group members
            for member in self.mass.players.iter_group_members(
                self, only_powered=True, active_only=True
            ):
                if member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await self.mass.players._handle_cmd_power(member.player_id, False)

        if not powered:
            # Reset to unfiltered static members list when powered off
            # (the frontend will hide unavailable members)
            self._attr_group_members = self._attr_static_group_members.copy()
            # and clear the sync leader
            self.sync_leader = None
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        # power on (which will also resync and add static members if needed)
        await self.power(True)
        # simply forward the command to the sync leader
        if sync_leader := self.sync_leader:
            await sync_leader.play_media(media)
            self._attr_current_media = deepcopy(media)
            self.update_state()
        else:
            raise RuntimeError("an empty group cannot play media, consider adding members first")

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        if sync_leader := self.sync_leader:
            await sync_leader.enqueue_next_media(media)

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        Will only be called if the PlayerFeature.SELECT_SOURCE is supported.

        :param source: The source(id) to select, as defined in the source_list.
        """
        if sync_leader := self.sync_leader:
            await sync_leader.select_source(source)
            self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if not self.is_dynamic:
            raise UnsupportedFeaturedException(
                f"Group {self.display_name} does not allow dynamically adding/removing members!"
            )
        # handle additions
        final_players_to_add: list[str] = []
        for player_id in player_ids_to_add or []:
            if player_id in self._attr_group_members:
                continue
            if player_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot add {self.display_name} to itself as a member!"
                )
            self._attr_group_members.append(player_id)
            final_players_to_add.append(player_id)
        # handle removals
        final_players_to_remove: list[str] = []
        for player_id in player_ids_to_remove or []:
            if player_id not in self._attr_group_members:
                continue
            if player_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot remove {self.display_name} from itself as a member!"
                )
            self._attr_group_members.remove(player_id)
            final_players_to_remove.append(player_id)
        self.update_state()
        if not self.powered:
            # Don't need to do anything else if the group is powered off
            # The syncing will be done once powered on
            return
        next_leader = self._select_sync_leader()
        prev_leader = self.sync_leader

        if prev_leader and next_leader is None:
            # Edge case: we no longer have any members in the group (and thus no leader)
            await self._handle_leader_transition(None)
        elif prev_leader != next_leader:
            # Edge case: we had changed the leader (or just got one)
            await self._handle_leader_transition(next_leader)
        elif self.sync_leader and (player_ids_to_add or player_ids_to_remove):
            # if the group still has the same leader, we need to (re)sync the members
            # Handle collisions for newly added players
            for player_id in final_players_to_add:
                if player := self.mass.players.get(player_id):
                    await self._handle_member_collisions(player)

            await self.sync_leader.set_members(
                player_ids_to_add=final_players_to_add,
                player_ids_to_remove=final_players_to_remove,
            )

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
        if self.sync_leader is None:
            # This is an empty group, leader will be selected once a member is added
            self._attr_group_members = []
            self.update_state()
            return
        # ensure the sync leader is first in the list
        self._attr_group_members = [
            self.sync_leader.player_id,
            *[x for x in self._attr_group_members if x != self.sync_leader.player_id],
        ]
        self.update_state()
        members_to_sync: list[str] = []
        members_to_remove: list[str] = []
        for member in self.mass.players.iter_group_members(self, active_only=False):
            # Handle collisions before attempting to sync
            await self._handle_member_collisions(member)

            if member.synced_to and member.synced_to != self.sync_leader.player_id:
                # ungroup first
                await member.ungroup()
            if member.player_id == self.sync_leader.player_id:
                # skip sync leader
                continue
            # Always add to members_to_sync to prevent them from being removed below
            members_to_sync.append(member.player_id)
        for former_members in self.sync_leader.group_members:
            if (
                former_members not in members_to_sync
            ) and former_members != self.sync_leader.player_id:
                members_to_remove.append(former_members)
        if members_to_sync or members_to_remove:
            await self.sync_leader.set_members(members_to_sync, members_to_remove)

    async def _dissolve_syncgroup(self) -> None:
        """Dissolve the current syncgroup by ungrouping all members and restoring leader queue."""
        if sync_leader := self.sync_leader:
            # dissolve the temporary syncgroup from the sync leader
            sync_children = [x for x in sync_leader.group_members if x != sync_leader.player_id]
            if sync_children:
                await sync_leader.set_members(player_ids_to_remove=sync_children)
            # Reset the leaders queue since it is no longer part of this group
            sync_leader.update_state()

    async def _handle_leader_transition(self, new_leader: Player | None) -> None:
        """Handle transition from current leader to new leader."""
        prev_leader = self.sync_leader
        was_playing = False

        if (
            prev_leader
            and new_leader
            and prev_leader != new_leader
            and self.provider.domain in SUPPORT_DYNAMIC_LEADER
        ):
            # provider supports dynamic leader selection, so just remove/add members
            await prev_leader.ungroup()
            self.sync_leader = new_leader
            # allow some time to propagate the changes before resyncing
            await asyncio.sleep(2)
            await self._form_syncgroup()
            return

        if prev_leader:
            # Save current media and playback state for potential restart
            was_playing = self.playback_state == PlaybackState.PLAYING
            # Stop current playback and dissolve existing group
            await self.stop()
            await self._dissolve_syncgroup()
            # allow some time to propagate the changes before resyncing
            await asyncio.sleep(2)

        # Set new leader
        self.sync_leader = new_leader

        if new_leader:
            # form a syncgroup with the new leader
            await self._form_syncgroup()

            # Restart playback if requested and we have media to play
            if was_playing:
                await self.mass.players._handle_cmd_resume(self.player_id)
        else:
            # We have no leader anymore, send update since we stopped playback
            self.update_state()

    def _select_sync_leader(self) -> Player | None:
        """Select the active sync leader player for a syncgroup."""
        if self.sync_leader and self.sync_leader.player_id in self.group_members:
            # Don't change the sync leader if we already have one
            return self.sync_leader
        for prefer_sync_leader in (True, False):
            for child_player in self.mass.players.iter_group_members(self):
                if prefer_sync_leader and child_player.synced_to:
                    # prefer the first player that already has sync children
                    continue
                if child_player.active_group not in (
                    None,
                    self.player_id,
                    child_player.player_id,
                ):
                    # this should not happen (because its already handled in the power on logic),
                    # but guard it just in case bad things happen
                    continue
                return child_player
        return None

    async def _handle_member_collisions(self, member: Player) -> None:
        """Handle collisions when adding a member to the sync group."""
        active_groups = member.active_groups
        for group in active_groups:
            if group == self.player_id:
                continue
            # collision: child player is part another group that is already active !
            # solve this by trying to leave the group first
            if other_group := self.mass.players.get(group):
                if (
                    other_group.supports_feature(PlayerFeature.SET_MEMBERS)
                    and member.player_id not in other_group.static_group_members
                ):
                    await other_group.set_members(player_ids_to_remove=[member.player_id])
                else:
                    # if the other group does not support SET_MEMBERS or it is a static
                    # member, we need to power it off to leave the group
                    await other_group.power(False)
        if (
            member.synced_to is not None
            and self.sync_leader
            and member.synced_to != self.sync_leader.player_id
            and (synced_to_player := self.mass.players.get(member.synced_to))
            and member.player_id in synced_to_player.group_members
        ):
            # collision: child player is synced to another player and still in that group
            # ungroup it first
            await synced_to_player.set_members(player_ids_to_remove=[member.player_id])


class SyncGroupController:
    """Controller managing SyncGroup players."""

    def __init__(self, player_controller: PlayerController) -> None:
        """Initialize SyncGroupController."""
        self.player_controller = player_controller
        self.mass = player_controller.mass

    async def create_group_player(
        self, provider: PlayerProvider, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """
        Create new SyncGroup Player.

        :param provider: The provider to create the group player for
        :param name: Name of the group player
        :param members: List of player ids to add to the group
        :param dynamic: Whether the group is dynamic (members can change)
        """
        # default implementation for providers that support syncing players
        if ProviderFeature.SYNC_PLAYERS not in provider.supported_features:
            # the frontend should already prevent this, but just in case
            raise UnsupportedFeaturedException(
                f"Provider {provider.name} does not support player syncing!"
            )
        # Create a new syncgroup player with the given members
        members = [x for x in members if x in [y.player_id for y in provider.players]]
        player_id = f"{SYNCGROUP_PREFIX}{shortuuid.random(8).lower()}"
        self.mass.config.create_default_player_config(
            player_id=player_id,
            provider=provider.instance_id,
            name=name,
            enabled=True,
            values={
                CONF_GROUP_MEMBERS: members,
                CONF_DYNAMIC_GROUP_MEMBERS: dynamic,
            },
        )
        return await self._register_syncgroup_player(player_id, provider)

    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player.

        :param player_id: ID of the group player to remove.
        """
        # we simply permanently unregister the syncgroup player and wipe its config
        await self.mass.players.unregister(player_id, True)

    async def _register_syncgroup_player(self, player_id: str, provider: PlayerProvider) -> Player:
        """Register a syncgroup player."""
        syncgroup = SyncGroupPlayer(provider, player_id)
        await self.mass.players.register_or_update(syncgroup)
        return syncgroup

    async def on_provider_loaded(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is loaded."""
        # register existing syncgroup players for this provider
        for player_conf in await self.mass.config.get_player_configs(provider.instance_id):
            if player_conf.player_id.startswith(SYNCGROUP_PREFIX):
                await self._register_syncgroup_player(player_conf.player_id, provider)

    async def on_provider_unload(self, provider: PlayerProvider) -> None:
        """Handle logic when a provider is (about to get) unloaded."""
        # unregister existing syncgroup players for this provider
        for player in self.mass.players.all(
            provider_filter=provider.instance_id, return_sync_groups=True
        ):
            if player.player_id.startswith(SYNCGROUP_PREFIX):
                await self.mass.players.unregister(player.player_id, False)
