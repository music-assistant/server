"""
Base class/model for a Player within Music Assistant.

All providerspecific players should inherit from this class and implement the required methods.

Note that the serverside Player object is not the same as the clientside Player object,
which is a dataclass in the models package containing the player state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.player import DeviceInfo, PlayerMedia
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
)

from .group_player import GroupPlayer
from .player import Player

if TYPE_CHECKING:
    from music_assistant.models.player_provider import PlayerProvider


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
        self._attr_name = self.config.name or f"SyncGroup {player_id}"
        self._attr_available = True
        self._attr_powered = False  # group players are always powered off by default
        self._attr_active_source = player_id
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
        return self._attr_supported_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        if self.power_state:
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

    @elapsed_time.setter
    def elapsed_time(self, value: float | None) -> None:
        """Set the elapsed time on the player."""
        raise NotImplementedError("elapsed_time is read-only on a SyncGroup player")

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return when the elapsed time was last updated."""
        return self.sync_leader.elapsed_time_last_updated if self.sync_leader else None

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
            return {self.provider.lookup_key}
        else:
            return set()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries: list[ConfigEntry] = [
            # default entries for player groups
            *await super().get_config_entries(),
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
        child_player = next((x for x in self.provider.players if x.type != PlayerType.GROUP), None)
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
            and member.synced_to != self.sync_leader
            and (synced_to_player := self.mass.players.get(member.synced_to))
            and member.player_id in synced_to_player.group_members
        ):
            # collision: child player is synced to another player and still in that group
            # ungroup it first
            await synced_to_player.set_members(player_ids_to_remove=[member.player_id])

    async def power(self, powered: bool) -> None:
        """Handle POWER command to group player."""
        # always stop at power off
        if not powered and self.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.stop()

        # optimistically set the group state
        prev_power = self._attr_powered
        self._attr_powered = powered
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
                    self._attr_group_members.append(static_group_member)
            # Select sync leader and handle turn on
            new_leader = self._select_sync_leader()
            # handle TURN_ON of the group player by turning on all members
            for member in self.mass.players.iter_group_members(
                self, only_powered=False, active_only=False
            ):
                await self._handle_member_collisions(member)
                if not member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await member.power(True)
            # Set up the sync group with the new leader
            await self._handle_leader_transition(new_leader)
        elif prev_power:
            # handle TURN_OFF of the group player by dissolving group and turning off all members
            await self._dissolve_syncgroup()
            # turn off all group members
            for member in self.mass.players.iter_group_members(
                self, only_powered=True, active_only=True
            ):
                if member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await member.power(False)

        if not powered:
            # Reset to unfiltered static members list when powered off
            # (the frontend will hide unavailable members)
            self._attr_group_members = self._attr_static_group_members.copy()
            # and clear the sync leader
            self.sync_leader = None

    async def _dissolve_syncgroup(self) -> None:
        """Dissolve the current syncgroup by ungrouping all members and restoring leader queue."""
        if sync_leader := self.sync_leader:
            # dissolve the temporary syncgroup from the sync leader
            sync_children = [x for x in sync_leader.group_members if x != sync_leader.player_id]
            if sync_children:
                await sync_leader.set_members(player_ids_to_remove=sync_children)
            # Reset the leaders queue since it is no longer part of this group
            sync_leader.active_source = None
            sync_leader.current_media = None
            sync_leader.update_state()

    async def _handle_leader_transition(self, new_leader: Player | None) -> None:
        """Handle transition from current leader to new leader."""
        prev_leader = self.sync_leader
        was_playing = False

        if prev_leader:
            # Save current media and playback state for potential restart
            was_playing = self.playback_state == PlaybackState.PLAYING
            # Stop current playback and dissolve existing group
            await self.stop()
            await self._dissolve_syncgroup()

        # Set new leader
        self.sync_leader = new_leader

        if new_leader:
            # form a syncgroup with the new leader
            await self._form_syncgroup()

            # Restart playback if requested and we have media to play
            if was_playing and self.current_media is not None:
                await new_leader.play_media(self.current_media)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        # power on (which will also resync if needed)
        await self.power(True)
        # simply forward the command to the sync leader
        if sync_leader := self.sync_leader:
            await sync_leader.play_media(media)
            self._attr_current_media = media
            self._attr_active_source = media.source_id
            self.update_state()
        else:
            raise RuntimeError("an empty group cannot play media, consider adding members first")

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        if sync_leader := self.sync_leader:
            await sync_leader.enqueue_next_media(media)

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
        for member in self.mass.players.iter_group_members(self, active_only=False):
            # Handle collisions before attempting to sync
            await self._handle_member_collisions(member)

            if member.synced_to and member.synced_to != self.sync_leader.player_id:
                # ungroup first
                await member.ungroup()
            if member.player_id == self.sync_leader.player_id:
                # skip sync leader
                continue
            if (
                member.synced_to == self.sync_leader.player_id
                and member.player_id in self.sync_leader.group_members
            ):
                # already synced
                continue
            members_to_sync.append(member.player_id)
        if members_to_sync:
            await self.sync_leader.set_members(members_to_sync)

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
