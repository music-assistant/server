"""Sync Group Player implementation."""

from __future__ import annotations

import asyncio
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
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import CONF_ENTRY_SGP_NOTE, EXTRA_FEATURES_FROM_MEMBERS

if TYPE_CHECKING:
    from .provider import SyncGroupProvider


class SyncGroupPlayer(Player):
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

    @cached_property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # groups can't be synced
        return None

    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        # Config is only available after the player was registered
        self._cache.clear()  # clear to prevent loading old is_dynamic
        default_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        if self.is_dynamic:
            self._attr_static_group_members = []
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        else:
            self._attr_static_group_members = default_members.copy()
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
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
        if not self.sync_leader:
            return base_features
        # add features supported by the sync leader
        for feature in EXTRA_FEATURES_FROM_MEMBERS:
            if feature in self.sync_leader.state.supported_features:
                base_features.add(feature)
        return base_features

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        return self.sync_leader.state.playback_state if self.sync_leader else PlaybackState.IDLE

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player needs flow mode."""
        if leader := self.sync_leader:
            return leader.flow_mode
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
    def can_group_with(self) -> set[str]:
        """Return the id's of players this player can group with."""
        if not self.is_dynamic:
            # in case of static members,
            # we can only group with the players defined in the config, so we return those directly
            return set(self._attr_static_group_members)
        # if we already have a sync leader, we use its can_group_with as reference
        if self.sync_leader:
            return {self.sync_leader.player_id, *self.sync_leader.state.can_group_with}
        # If we have no members, but we do have default members in the config,
        # we can group with players that are compatible with those
        default_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        for member_id in default_members:
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                return {*default_members, *member_player.state.can_group_with}
        # Dynamic groups can potentially group with any compatible players
        # Actual compatibility is validated when adding members
        temp_can_group_with = set()
        for player in self.mass.players.all_players(return_unavailable=False):
            if not player.available or player.type == PlayerType.GROUP:
                # let's avoid showing group players as options to group with
                continue
            if (
                PlayerFeature.SET_MEMBERS in player.state.supported_features
                and player.state.can_group_with
                and not player.state.active_group
            ):
                temp_can_group_with.add(player.player_id)
        return temp_can_group_with

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
        return entries

    async def stop(self) -> None:
        """Send STOP command to given player."""
        self._attr_current_media = None
        if sync_leader := self.sync_leader:
            # Use internal handler to bypass group redirect logic and avoid infinite loop
            # (sync_leader is part of this group, so redirect would loop back here)
            await self.mass.players._handle_cmd_stop(sync_leader.player_id)
        # dissolve the sync group since we stopped playback
        self.mass.call_later(
            5, self._dissolve_syncgroup, task_id=f"syncgroup_dissolve_{self.player_id}"
        )

    async def play(self) -> None:
        """Send PLAY (unpause) command to given player."""
        await self.mass.players.cmd_resume(
            self.player_id, self._attr_active_source, self._attr_current_media
        )

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        self._attr_current_media = media
        self._attr_active_source = media.source_id if media.source_id else None
        await self._form_syncgroup()
        # simply forward the command to the sync leader
        if sync_leader := self.sync_leader:
            # Use internal handler to bypass group redirect logic and preserve protocol selection
            await self.mass.players._handle_play_media(sync_leader.player_id, media)
            self.update_state()
        else:
            raise RuntimeError("An empty group cannot play media, consider adding members first")

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        if sync_leader := self.sync_leader:
            if PlayerFeature.ENQUEUE not in sync_leader.state.supported_features:
                # this may happen in race conditions where we just switched sync leaders
                # and the new leader doesn't support enqueueing next media.
                return
            # Use internal handler to bypass group redirect logic and avoid infinite loop
            await self.mass.players._handle_enqueue_next_media(sync_leader.player_id, media)

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
        was_playing = self.playback_state == PlaybackState.PLAYING
        needs_restart = False
        if prev_leader and prev_leader.player_id in (player_ids_to_remove or []):
            # We're removing the current sync leader while the group is active
            # We need to select a new leader before we can handle the member changes
            self.logger.debug(
                "Removing current sync leader %s from group %s while it is active, "
                "selecting a new leader and dissolving the current syncgroup",
                prev_leader.display_name,
                self.display_name,
            )
            if was_playing:
                await self.mass.players._handle_cmd_stop(prev_leader.player_id)
                await asyncio.sleep(1)
            await self._dissolve_syncgroup()
            await asyncio.sleep(2)
            needs_restart = was_playing

        cur_leader = self._select_sync_leader(new_members=player_ids_to_add)
        # handle additions
        final_players_to_add: list[str] = []
        can_group_with = cur_leader.state.can_group_with.copy() if cur_leader else set()
        for member_id in player_ids_to_add or []:
            if member_id == self.player_id:
                continue  # can not add self as member
            member = self.mass.players.get_player(member_id)
            if member is None or not member.available:
                continue
            if member_id not in self._attr_group_members:
                self._attr_group_members.append(member_id)
            if not cur_leader:
                continue
            if member_id != cur_leader.player_id and member_id not in can_group_with:
                self.logger.debug(
                    f"Cannot add {member.display_name} to group {self.display_name} since it's "
                    f"not compatible with the current sync leader"
                )
                continue
            if member_id != cur_leader.player_id:
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
        if needs_restart:
            await self.play()
            return
        if not was_playing:
            # Don't need to do anything else if the group is not active
            # The syncing will be done once playback starts
            return
        if cur_leader:
            await self.mass.players.cmd_set_members(
                cur_leader.player_id,
                player_ids_to_add=final_players_to_add,
                player_ids_to_remove=final_players_to_remove,
            )

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
        self.mass.cancel_timer(f"syncgroup_dissolve_{self.player_id}")
        if not self.sync_leader:
            self.sync_leader = self._select_sync_leader()

        if not self.sync_leader:
            # we have no members in the group, so we can't form a syncgroup
            return

        # ensure the sync leader is first in the list
        self._attr_group_members = [
            self.sync_leader.player_id,
            *[x for x in self._attr_group_members if x != self.sync_leader.player_id],
        ]
        members_to_sync = [
            x
            for x in self._attr_group_members
            if x != self.sync_leader.player_id and x not in self.sync_leader.state.group_members
        ]
        if members_to_sync:
            await self.mass.players.cmd_set_members(self.sync_leader.player_id, members_to_sync)

    async def _dissolve_syncgroup(self) -> None:
        """Dissolve the current syncgroup by ungrouping all members."""
        if sync_leader := self.sync_leader:
            # dissolve the temporary syncgroup from the sync leader
            sync_children = [
                x for x in sync_leader.state.group_members if x != sync_leader.player_id
            ]
            if sync_children:
                await self.mass.players.cmd_set_members(sync_leader.player_id, [], sync_children)
        self.sync_leader = None
        self.update_state()

    def _select_sync_leader(self, new_members: list[str] | None = None) -> Player | None:
        """Select a (new) sync leader."""
        if self.group_members and self.sync_leader and self.sync_leader.state.available:
            # current leader is still available, no need to select a new one
            return self.sync_leader
        default_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        group_members = self.group_members or default_members or new_members or []
        for member_id in group_members:
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                self.logger.debug(
                    f"Auto-selected {member_player.display_name} as sync leader for "
                    f"group {self.display_name}"
                )
                return member_player
        return None
