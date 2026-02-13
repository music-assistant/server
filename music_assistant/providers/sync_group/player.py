"""Sync Group Player implementation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    APPLICATION_NAME,
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
)
from music_assistant.models.player import DeviceInfo, GroupPlayer, Player, PlayerMedia, PlayerSource

from .constants import CONF_ENTRY_SGP_NOTE, EXTRA_FEATURES_FROM_MEMBERS, SUPPORT_DYNAMIC_LEADER

if TYPE_CHECKING:
    from .provider import SyncGroupProvider


class SyncGroupPlayer(GroupPlayer):
    """Sync Group Player implementation."""

    _attr_type: PlayerType = PlayerType.GROUP
    sync_leader: Player | None = None
    """The active sync leader player for this syncgroup."""

    def __init__(
        self,
        provider: SyncGroupProvider,
        player_id: str,
    ) -> None:
        """Initialize SyncGroupPlayer instance."""
        super().__init__(provider, player_id)
        self._attr_name = self.config.name or self.config.default_name or f"SyncGroup {player_id}"
        self._attr_available = True
        self._attr_device_info = DeviceInfo(model=provider.name, manufacturer=APPLICATION_NAME)
        # Allow grouping with any player that supports syncing
        # The actual compatibility is checked via can_group_with on each player
        self._attr_can_group_with = set()

    @cached_property
    def is_dynamic(self) -> bool:
        """Return if the player is a dynamic group player."""
        return bool(self.config.get_value(CONF_DYNAMIC_GROUP_MEMBERS, False))

    @property
    def is_active(self) -> bool:
        """Return if the sync group player is active."""
        return len(self._attr_group_members) > 0 and self.sync_leader is not None

    async def on_config_updated(self) -> None:
        """Handle logic when the player is loaded or updated."""
        # Config is only available after the player was registered
        self._cache.clear()  # clear to prevent loading old is_dynamic
        default_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        if self.is_dynamic:
            self._attr_static_group_members = []
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        else:
            self._attr_static_group_members = default_members.copy()
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        if not self.powered:
            self._attr_group_members = default_members.copy()

    @cached_property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        # by default we don't have any features, except play_media
        # but we can gain some features based on the capabilities of the sync leader
        # set_members is only supported if it's a dynamic group
        base_features: set[PlayerFeature] = {PlayerFeature.PLAY_MEDIA}
        if self.is_dynamic:
            base_features.add(PlayerFeature.SET_MEMBERS)
        if not self.is_active:
            return base_features
        members = self.group_members
        reference_player: Player | None = self.sync_leader or (
            self.mass.players.get_player(members[0]) if members else None
        )
        if reference_player:
            # add features supported by the sync leader
            for feature in EXTRA_FEATURES_FROM_MEMBERS:
                if feature in reference_player.supported_features:
                    base_features.add(feature)
            return base_features
        return base_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        return self.sync_leader.state.playback_state if self.sync_leader else PlaybackState.IDLE

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player needs flow mode."""
        if leader := self.sync_leader:
            return leader.requires_flow_mode
        return False

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track (if any)."""
        return self.sync_leader.state.elapsed_time if self.sync_leader else None

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return when the elapsed time was last updated."""
        return self.sync_leader.state.elapsed_time_last_updated if self.sync_leader else None

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the current media item (if any) loaded in the player."""
        return (
            self.sync_leader.state.current_media if self.sync_leader else self._attr_current_media
        )

    @property
    def active_source(self) -> str | None:
        """Return the active source id (if any) of the player."""
        return self.sync_leader.active_source if self.sync_leader else self._attr_active_source

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        if self.sync_leader:
            return self.sync_leader.source_list
        return []

    @property
    def can_group_with(self) -> set[str]:
        """Return the id's of players this player can group with."""
        # if we already have members, we can only group with players
        # that are compatible with the current members
        if self.group_members:
            for member_id in self.group_members:
                member_player = self.mass.players.get_player(member_id)
                if member_player:
                    return member_player.state.can_group_with
        # If we have no members, but we do have default members in the config,
        # we can group with players that are compatible with those
        default_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        for member_id in default_members:
            member_player = self.mass.players.get_player(member_id)
            if member_player:
                return member_player.state.can_group_with
        if self.is_dynamic:
            # Dynamic groups can potentially group with any compatible players
            # Actual compatibility is validated when adding members
            temp_can_group_with = set()
            for player in self.mass.players.all_players():
                if not player.available or player.type == PlayerType.GROUP:
                    # let's avoid showing group players as options to group with
                    continue
                if (
                    PlayerFeature.SET_MEMBERS in player.state.supported_features
                    and player.state.can_group_with
                ):
                    temp_can_group_with.add(player.player_id)
            return temp_can_group_with
        # this should not happen since we should always have default members
        # in the config for static groups, but just in case, we return an empty
        # set to prevent grouping with incompatible players
        return set()

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries: list[ConfigEntry] = [
            # syncgroup specific entries
            CONF_ENTRY_SGP_NOTE,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                label="Group members",
                default_value=[],
                description="Select all players you want to be part of this sync group. "
                "Only compatible players (based on their sync protocol) can be grouped together.",
                required=False,  # needed for dynamic members (which allows empty members list)
                options=[
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all_players(True, False)
                    if x.type != PlayerType.GROUP
                ],
            ),
            ConfigEntry(
                key=CONF_DYNAMIC_GROUP_MEMBERS,
                type=ConfigEntryType.BOOLEAN,
                label="Enable dynamic members",
                description="Allow (un)joining members dynamically, so the group more or less "
                "behaves the same like manually syncing players together, "
                "with the main difference being that the group player will hold the queue.",
                default_value=False,
                required=False,
            ),
        ]
        # TODO: Add streaming/audio config entries similar to universal_group if needed
        # For now, the sync is handled by the underlying protocol, so we may not need these
        return entries

    async def stop(self) -> None:
        """Send STOP command to given player."""
        if sync_leader := self.sync_leader:
            await sync_leader.stop()
        # dissolve the sync group since we stopped playback
        await self._dissolve_syncgroup()

    async def play(self) -> None:
        """Send PLAY (unpause) command to given player."""
        if sync_leader := self.sync_leader:
            await sync_leader.play()

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        if sync_leader := self.sync_leader:
            await sync_leader.pause()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if not self.is_active:
            await self._form_syncgroup()
        # simply forward the command to the sync leader
        if sync_leader := self.sync_leader:
            await sync_leader.play_media(media)
            self._attr_current_media = deepcopy(media)
            self.update_state()
        else:
            raise RuntimeError("An empty group cannot play media, consider adding members first")

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
        prev_leader = self.sync_leader
        # handle additions
        final_players_to_add: list[str] = []
        can_group_with = self.sync_leader.state.can_group_with.copy() if self.sync_leader else set()
        for member_id in player_ids_to_add or []:
            if member_id == self.player_id:
                continue  # can not add self as member
            member = self.mass.players.get_player(member_id)
            if member is None or not member.available:
                continue
            # At this point, member is guaranteed to be not None
            if not prev_leader:
                # auto select first member as new leader if we don't have one yet,
                # or if the current leader is not available anymore
                self.sync_leader = member
                can_group_with = member.state.can_group_with.copy()
                self.logger.debug(
                    f"Auto-selected {member.display_name} as sync leader for "
                    f"group {self.display_name} since it has no leader yet"
                )
            elif member_id not in can_group_with:
                self.logger.debug(
                    f"Cannot add {member.display_name} to group {self.display_name} since it's "
                    f"not compatible with the current sync leader"
                )
                continue
            self._attr_group_members.append(member_id)
            final_players_to_add.append(member_id)
        # handle removals
        final_players_to_remove: list[str] = []
        for member_id in player_ids_to_remove or []:
            if member_id not in self._attr_group_members:
                continue
            if member_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot remove {self.display_name} from itself as a member!"
                )
            self._attr_group_members.remove(member_id)
            final_players_to_remove.append(member_id)
        self.update_state()
        if not self.powered:
            # Don't need to do anything else if the group is powered off
            # The syncing will be done once powered on
            return

        if prev_leader and self.sync_leader is None:
            # Edge case: we no longer have any members in the group (and thus no leader)
            await self._handle_leader_transition(None)
        elif prev_leader and prev_leader != self.sync_leader:
            # Edge case: we had changed the leader (or just got one)
            await self._handle_leader_transition(self.sync_leader)
        elif self.sync_leader and (player_ids_to_add or player_ids_to_remove):
            # if the group still has the same leader, we need to (re)sync the members
            await self.mass.players.cmd_set_members(
                self.sync_leader.player_id,
                player_ids_to_add=final_players_to_add,
                player_ids_to_remove=final_players_to_remove,
            )

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
        # make sure that we add the default members from the config
        default_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        for default_member in default_members:
            if default_member not in self._attr_group_members:
                self._attr_group_members.append(default_member)
        # if dynamic mode is disabled, our default members from config are marked as static members
        if not self.is_dynamic:
            self._attr_static_group_members = default_members.copy()
        else:
            self._attr_static_group_members = []

        # prepare group members for syncing
        for member_id in self._attr_group_members:
            member_player = self.mass.players.get_player(member_id)
            if not member_player or not member_player.state.available:
                # remove unavailable members from the group
                self._attr_group_members.remove(member_id)
                continue
            # At this point, member_player is guaranteed to be not None
            if (
                member_player.state.synced_to
                and self.sync_leader
                and member_player.state.synced_to != self.sync_leader.player_id
            ):
                # ungroup first if the member is currently synced to another player
                await member_player.ungroup()
            if not self.sync_leader:
                # set the first available member as the sync leader
                self.sync_leader = member_player

        if not self.sync_leader:
            # we have no members in the group, so we can't form a syncgroup
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
            if member.player_id == self.sync_leader.player_id:
                # skip sync leader
                continue
            # Always add to members_to_sync to prevent them from being removed below
            members_to_sync.append(member.player_id)
        for former_member in self.sync_leader.group_members:
            if former_member not in members_to_sync and former_member != self.sync_leader.player_id:
                members_to_remove.append(former_member)
        if members_to_sync or members_to_remove:
            await self.mass.players.cmd_set_members(
                self.sync_leader.player_id, members_to_sync, members_to_remove
            )

    async def _dissolve_syncgroup(self) -> None:
        """Dissolve the current syncgroup by ungrouping all members."""
        if sync_leader := self.sync_leader:
            # dissolve the temporary syncgroup from the sync leader
            sync_children = [
                x for x in sync_leader.state.group_members if x != sync_leader.player_id
            ]
            if sync_children:
                await self.mass.players.cmd_set_members(sync_leader.player_id, [], sync_children)
        self._attr_group_members = []
        self.update_state()

    async def _handle_leader_transition(self, new_leader: Player | None) -> None:
        """Handle transition from current leader to new leader."""
        prev_leader = self.sync_leader
        was_playing = False

        if prev_leader and new_leader and prev_leader != new_leader:
            # Check if the provider supports dynamic leader selection
            # For cross-provider sync groups, we need to check the provider domain
            provider_protocol = None
            if prev_leader.active_output_protocol and (
                proto_prov := self.mass.get_provider(prev_leader.active_output_protocol)
            ):
                provider_protocol = proto_prov.domain
            else:
                provider_protocol = prev_leader.provider.domain

            if provider_protocol and provider_protocol in SUPPORT_DYNAMIC_LEADER:
                # provider/protocol supports dynamic leader selection, so just remove/add members
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
