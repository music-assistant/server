"""Sync Group Player implementation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    APPLICATION_NAME,
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import CONF_ENTRY_SGP_NOTE, CONF_MEMBERS_FILTER, EXTRA_FEATURES_FROM_MEMBERS

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerSource

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
        static_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        self._attr_static_group_members = static_members.copy()
        if self.is_dynamic:
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        else:
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        self._attr_group_members = static_members.copy()

    @property
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
    def requires_flow_mode(self) -> bool:
        """Return if the player needs flow mode."""
        if leader := self.sync_leader:
            return leader.flow_mode
        return False

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        return self.sync_leader.state.playback_state if self.sync_leader else PlaybackState.IDLE

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track (if any)."""
        # NOTE: Not using 'state' here as we need the 'raw' value provided by the sync leader player
        if sync_leader := self.sync_leader:
            # If an output protocol is active (and not native), use the protocol player's state
            if (
                sync_leader.active_output_protocol
                and sync_leader.active_output_protocol != "native"
                and (
                    protocol_player := self.mass.players.get_player(
                        sync_leader.active_output_protocol
                    )
                )
                and protocol_player.playback_state != PlaybackState.IDLE
            ):
                return protocol_player.elapsed_time
            return sync_leader.elapsed_time
        return None

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return when the elapsed time was last updated."""
        # NOTE: Not using 'state' here as we need the 'raw' value provided by the sync leader player
        if sync_leader := self.sync_leader:
            # If an output protocol is active (and not native), use the protocol player's state
            if (
                sync_leader.active_output_protocol
                and sync_leader.active_output_protocol != "native"
                and (
                    protocol_player := self.mass.players.get_player(
                        sync_leader.active_output_protocol
                    )
                )
                and protocol_player.playback_state != PlaybackState.IDLE
            ):
                return protocol_player.elapsed_time_last_updated
            return sync_leader.elapsed_time_last_updated
        return None

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the currently playing media (if any)."""
        # NOTE: Not using 'state' here as we need the 'raw' value provided by the sync leader player
        return self.sync_leader.current_media if self.sync_leader else None

    @property
    def active_source(self) -> str | None:
        """Return the active source id of the current media (if any)."""
        # NOTE: Not using 'state' here as we need the 'raw' value provided by the sync leader player
        if not self.sync_leader:
            return None
        # if a plugin source is active on the syncleader, return that
        for plugin_source in self.mass.players.get_plugin_sources():
            if plugin_source.in_use_by == self.sync_leader.player_id:
                return plugin_source.id
        # deal with output protocols on the sync leader
        output_protocol_domain: str | None = None
        if (
            self.sync_leader.active_output_protocol
            and self.sync_leader.active_output_protocol != "native"
        ):
            if protocol_player := self.mass.players.get_player(
                self.sync_leader.active_output_protocol
            ):
                output_protocol_domain = protocol_player.provider.domain
        # active source as reported by the player itself
        if (
            self.sync_leader.active_source
            # try to catch cases where player reports an active source
            # that is actually from an active output protocol (e.g. AirPlay)
            and self.sync_leader.active_source.lower() != output_protocol_domain
            and not (
                # try to handle sendspin bridge where the player itself
                # is reporting the bridged protocol as active source
                # we need to ignore that
                output_protocol_domain == "sendspin"
                and (
                    self.sync_leader.active_source.lower()
                    in ("airplay", "cast", "chromecast", "network")
                )
            )
        ):
            return self.sync_leader.active_source
        return None

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        # NOTE: Not using 'state' here as we need the 'raw' value provided by the sync leader player
        return self.sync_leader.source_list if self.sync_leader else []

    @property
    def can_group_with(self) -> set[str]:
        """Return the id's of players this player can group with."""
        if not self.is_dynamic:
            # in case of static members,
            # we can only group with the players defined in the config, so we return those directly
            return set(self._attr_static_group_members)
        # if we already have a sync leader, we use its can_group_with as reference
        if self.sync_leader:
            return {
                self.sync_leader.player_id,
                *self.sync_leader.state.can_group_with,
            }
        members_filter = (
            cast("list[str]", self.config.get_value(CONF_MEMBERS_FILTER, []))
            if self.is_dynamic
            else []
        )
        # If we have no syncleader, but we do have group members
        # grab 'can_group_with' from the first available member
        for member_id in self._attr_group_members:
            if member_id in members_filter:
                continue
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                can_group_with = {member_player.player_id, *member_player.state.can_group_with}
                return can_group_with.difference(members_filter)
        # Empty dynamic groups can potentially group with any compatible players
        # Actual compatibility is validated when adding members
        can_group_with: set[str] = set()  # type: ignore[no-redef]
        for player in self.mass.players.all_players(return_unavailable=False):
            if not player.available or player.type == PlayerType.GROUP:
                # let's avoid showing group players as options to group with
                continue
            if (
                PlayerFeature.SET_MEMBERS in player.state.supported_features
                and player.state.can_group_with
                and not player.state.active_group
            ):
                can_group_with.add(player.player_id)
        return can_group_with.difference(members_filter)

    @property
    def group_members(self) -> list[str]:
        """Return the list of player id's that are part of this sync group."""
        if (sync_leader := self.sync_leader) and sync_leader.state.group_members:
            # prefer the group members as reported by the sync leader,
            # since that is the source of truth for the actual active group members
            # as the user may have decided to (temporarily) join/unjoin some members
            # to/from the group, which would cause our internal list to be out of
            # sync with the actual group members
            return sync_leader.state.group_members
        return self._attr_group_members

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        possible_players = sorted(
            [
                ConfigValueOption(x.display_name, x.player_id)
                for x in self.mass.players.all_players(True, False)
                if x.type != PlayerType.GROUP
                and PlayerFeature.SET_MEMBERS in x.state.supported_features
                and x.state.can_group_with
            ],
            key=lambda x: x.title,
        )
        entries: list[ConfigEntry] = [
            # syncgroup specific entries
            CONF_ENTRY_SGP_NOTE,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                label="Permanent group members",
                default_value=[],
                description="Select all static/permanent members of this sync group. "
                "These members will always be part of the group and can never be unjoined "
                "from the group. ",
                required=False,  # needed for dynamic members (which allows empty members list)
                options=possible_players,
            ),
            ConfigEntry(
                key=CONF_DYNAMIC_GROUP_MEMBERS,
                type=ConfigEntryType.BOOLEAN,
                label="Enable dynamic members",
                description="Allow (un)joining members dynamically, so the group more or less "
                "behaves the same like manually syncing players together, "
                "with the main difference being that the group player will hold the queue. \n"
                "Note that static members will always be part of the group and can never "
                "be unjoined from the group.",
                default_value=False,
                required=False,
            ),
            ConfigEntry(
                key=CONF_MEMBERS_FILTER,
                type=ConfigEntryType.STRING,
                multi_value=True,
                label="Members filter",
                description="Optionally filter the list of available members that "
                "are allowed to group with this player by excluding certain members. \n"
                "Players in this list will NOT show up in the UI as options to be "
                "added as members to the group. Also trying to join a member that "
                "is in this list to the group will be prevented.",
                default_value=[],
                required=False,
                options=possible_players,
                depends_on=CONF_DYNAMIC_GROUP_MEMBERS,
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
        await self.mass.players._handle_cmd_resume(
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

    async def set_members(  # noqa: PLR0915
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if not self.is_dynamic:
            raise UnsupportedFeaturedException(
                f"Group {self.display_name} does not allow dynamically adding/removing members!"
            )
        sync_leader = self.sync_leader or self._select_sync_leader(new_members=player_ids_to_add)
        was_playing = self.playback_state == PlaybackState.PLAYING

        # handle additions
        members_filter = (
            cast("list[str]", self.config.get_value(CONF_MEMBERS_FILTER, []))
            if self.is_dynamic
            else []
        )
        final_players_to_add: list[str] = []
        can_group_with = sync_leader.state.can_group_with.copy() if sync_leader else set()
        for member_id in player_ids_to_add or []:
            if member_id == self.player_id:
                continue  # can not add self as member
            if member_id in members_filter:
                self.logger.warning(
                    "Player %s is in the members filter list for group %s, "
                    "skipping adding it as a member to the group",
                    member_id,
                    self.display_name,
                )
                continue
            member = self.mass.players.get_player(member_id)
            if member is None or not member.available:
                continue
            if member_id not in self._attr_group_members:
                self._attr_group_members.append(member_id)
            if not sync_leader:
                continue
            if member_id != sync_leader.player_id and member_id not in can_group_with:
                self.logger.debug(
                    f"Cannot add {member.display_name} to group {self.display_name} since it's "
                    f"not compatible with the (current) sync leader"
                )
                continue
            if member_id != sync_leader.player_id:
                final_players_to_add.append(member_id)

        # handle removals
        final_players_to_remove: list[str] = []
        leader_removed = False
        for member_id in player_ids_to_remove or []:
            if member_id not in self._attr_group_members:
                continue
            if member_id in self._attr_static_group_members:
                # static members can not be removed from the group
                raise PlayerCommandFailed(
                    f"Cannot remove {member_id} from group {self.display_name} "
                    "since it's a static member!"
                )
            if self.sync_leader and member_id == self.sync_leader.player_id:
                leader_removed = True
                continue
            if member_id == self.player_id:
                raise PlayerCommandFailed(
                    f"Cannot remove {self.display_name} from itself as a member!"
                )
            self._attr_group_members.remove(member_id)
            final_players_to_remove.append(member_id)

        if self.sync_leader and leader_removed and self._attr_group_members:
            # we removed the current sync leader, but we still have members in the group
            # we need to select a new leader and re-form the syncgroup with it
            old_leader_id = self.sync_leader.player_id
            self.logger.info(
                "Removing current sync leader %s from group %s while it is active, "
                "dissolving the current syncgroup and will re-form it with a new leader",
                self.sync_leader.display_name,
                self.display_name,
            )
            await self.mass.players._handle_cmd_stop(self.sync_leader.player_id)
            await asyncio.sleep(1)
            await self._dissolve_syncgroup()
            # remove the old leader from the group members list so it won't be re-selected
            if old_leader_id in self._attr_group_members:
                self._attr_group_members.remove(old_leader_id)
            if was_playing and self._attr_group_members:
                await asyncio.sleep(2)
                await self.play()
        elif self.sync_leader and (leader_removed or not self._attr_group_members):
            # we removed the current sync leader, and we have no members left in the group
            # or we just removed the last member from the group, so we dissolve the syncgroup
            await self.mass.players._handle_cmd_stop(self.sync_leader.player_id)
            await asyncio.sleep(1)
            await self._dissolve_syncgroup()

        elif self.sync_leader:
            # just a regular member(s) added/removed action,
            # we can simply update the syncgroup members on the sync leader
            await self.mass.players.cmd_set_members(
                self.sync_leader.player_id,
                player_ids_to_add=final_players_to_add,
                player_ids_to_remove=final_players_to_remove,
            )
        else:
            # If we weren't playing before, we don't need to do anything else,
            # since the syncing will be done once playback starts
            self.update_state()

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
        self.mass.cancel_timer(f"syncgroup_dissolve_{self.player_id}")
        # always ensure static members are part of the group members,
        # even if they were (temporarily) removed by un unjoin
        self._attr_group_members = [
            *self._attr_static_group_members,
            *[x for x in self._attr_group_members if x not in self._attr_static_group_members],
        ]

        # select new sync leader if needed
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
            # If the sync leader is playing something independently, stop it first
            # to prevent protocol switching from trying to resume the previous playback
            # (we're about to start new playback on the syncgroup)
            if self.sync_leader.state.playback_state == PlaybackState.PLAYING:
                await self.mass.players._handle_cmd_stop(self.sync_leader.player_id)
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
        # with selecting a new leader, we prioritize the static group members
        group_members = self.static_group_members or self.group_members or new_members or []
        for member_id in group_members:
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                self.logger.debug(
                    f"Auto-selected {member_player.display_name} as sync leader for "
                    f"group {self.display_name}"
                )
                return member_player
        return None
