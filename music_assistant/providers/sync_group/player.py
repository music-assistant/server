"""Sync Group Player implementation."""

from __future__ import annotations

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

from .constants import (
    CONF_ENTRY_SGP_NOTE,
    CONF_MEMBERS_FILTER,
    EXTRA_FEATURES_FROM_MEMBERS,
    SUPPORT_DYNAMIC_LEADER,
)

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
        self._active_protocol_domain: str | None = None

    @cached_property
    def is_dynamic(self) -> bool:
        """Return if the player is a dynamic group player."""
        return bool(self.config.get_value(CONF_DYNAMIC_GROUP_MEMBERS, False))

    @property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # groups can't be synced
        return None

    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        # Config is only available after the player was registered
        self._cache.clear()  # clear to prevent loading old is_dynamic
        static_members_conf = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        static_members: list[str] = []
        # TEMP: migrate protocol id's to protocol parent id's for static members
        # TODO: remove this logic once 2.8 is released and we start the 2.9 cycle.
        changes_made = False
        for member_id in static_members_conf:
            if (
                member_player := self.mass.players.get_player(member_id)
            ) and member_player.protocol_parent_id:
                static_members.append(member_player.protocol_parent_id)
                changes_made = True
            else:
                static_members.append(member_id)
        if changes_made:
            self.mass.config.set_raw_player_config_value(
                self.player_id, CONF_GROUP_MEMBERS, static_members
            )

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
        # but we can gain some features based on the capabilities of the members
        # set_members is only supported if it's a dynamic group
        base_features: set[PlayerFeature] = {PlayerFeature.PLAY_MEDIA}
        if self.is_dynamic:
            base_features.add(PlayerFeature.SET_MEMBERS)
        if self.sync_leader:
            # add features supported by the sync leader
            for feature in EXTRA_FEATURES_FROM_MEMBERS:
                if feature in self.sync_leader.state.supported_features:
                    base_features.add(feature)
        else:
            # derive features from all (configured) group members
            # so that features like volume control are always advertised
            for member_id in self._attr_group_members:
                member_player = self.mass.players.get_player(member_id)
                if member_player and member_player.state.available:
                    for feature in EXTRA_FEATURES_FROM_MEMBERS:
                        if feature in member_player.state.supported_features:
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
        members_filter = (
            cast("list[str]", self.config.get_value(CONF_MEMBERS_FILTER, []))
            if self.is_dynamic
            else []
        )
        # Aggregate can_group_with from ALL current group members (not just the leader).
        # A sync group can accommodate protocol switches, so a player compatible with
        # ANY current member is a valid candidate to join.
        member_ids = self._attr_group_members if self._attr_group_members else []
        if member_ids:
            can_group_with: set[str] = set()
            for member_id in member_ids:
                if member_id in members_filter:
                    continue
                member_player = self.mass.players.get_player(member_id)
                if member_player and member_player.state.available:
                    can_group_with.add(member_player.player_id)
                    can_group_with.update(member_player.state.can_group_with)
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
        """Return the list of parent player id's that are part of this sync group."""
        if (sync_leader := self.sync_leader) and sync_leader.state.group_members:
            # The sync leader's group_members may contain protocol IDs (e.g. apc...)
            # when playing via a protocol. Translate back to parent IDs so callers
            # always get protocol-independent IDs.
            return self._translate_to_parent_ids(sync_leader.state.group_members)
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
            # Use internal handler to target the sync leader directly,
            # bypassing group/sync redirect that would loop back to this player.
            await self.mass.players._handle_cmd_stop(sync_leader.player_id)
        # Clear cached protocol domain so leader selection isn't biased
        # if playback restarts before the group is dissolved.
        self._active_protocol_domain = None
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
        self._attr_active_source = media.source_id or None
        await self._form_syncgroup()
        if sync_leader := self.sync_leader:
            # Use internal handler to target the sync leader directly,
            # bypassing group/sync redirect that would loop back to this player.
            await self.mass.players._handle_play_media(sync_leader.player_id, media)
            self._update_active_protocol()
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
        await self._set_members(player_ids_to_add, player_ids_to_remove)

    async def _set_members(  # noqa: PLR0915
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command (serialized by controller's play lock)."""
        if not self.is_dynamic:
            raise UnsupportedFeaturedException(
                f"Group {self.display_name} does not allow dynamically adding/removing members!"
            )
        # Cancel any pending dissolve from a previous stop() call
        self.mass.cancel_timer(f"syncgroup_dissolve_{self.player_id}")
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
            old_leader_id = self.sync_leader.player_id
            protocol_domain = self._get_leader_protocol_domain()

            if was_playing and protocol_domain in SUPPORT_DYNAMIC_LEADER:
                # protocol supports dynamic leader switching: remove only the departing
                # leader from the stream session, remaining members keep playing
                await self._dynamic_leader_switch(old_leader_id)
            else:
                # protocol doesn't support dynamic leader switching or not playing:
                # dissolve the entire syncgroup and re-form with a new leader
                self.logger.info(
                    "Removing current sync leader %s from group %s while it is active, "
                    "dissolving the current syncgroup and will re-form it with a new leader",
                    self.sync_leader.display_name,
                    self.display_name,
                )
                # Use internal handler to stop the sync leader directly,
                # bypassing group redirect that would loop back to this player.
                await self.mass.players.wait_for_player_update(
                    self.sync_leader.player_id,
                    timeout=5,
                    action=self.mass.players._handle_cmd_stop(self.sync_leader.player_id),
                )
                await self._dissolve_syncgroup()
                # remove the old leader from the group members list
                # so it won't be re-selected
                if old_leader_id in self._attr_group_members:
                    self._attr_group_members.remove(old_leader_id)
                if was_playing and self._attr_group_members:
                    await self.play()
        elif self.sync_leader and (leader_removed or not self._attr_group_members):
            # we removed the current sync leader, and we have no members left in the group
            # or we just removed the last member from the group, so we dissolve the syncgroup
            # Use internal handler to stop the sync leader directly,
            # bypassing group redirect that would loop back to this player.
            await self.mass.players.wait_for_player_update(
                self.sync_leader.player_id,
                timeout=5,
                action=self.mass.players._handle_cmd_stop(self.sync_leader.player_id),
            )
            await self._dissolve_syncgroup()

        elif self.sync_leader:
            # just a regular member(s) added/removed action,
            # we can simply update the syncgroup members on the sync leader
            await self.mass.players.cmd_set_members(
                self.sync_leader.player_id,
                player_ids_to_add=final_players_to_add,
                player_ids_to_remove=final_players_to_remove,
            )
            # update protocol domain (may have changed due to protocol switch)
            if final_players_to_add:
                self._update_active_protocol()
        # NOTE: If we weren't playing before, we don't need to do anything else,
        # since the syncing will be done once playback starts
        self.mass.players.trigger_player_update(self.player_id)

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
        self.mass.cancel_timer(f"syncgroup_dissolve_{self.player_id}")
        self.logger.debug(
            "Forming syncgroup %s, _attr_group_members=%s, sync_leader=%s",
            self.display_name,
            self._attr_group_members,
            self.sync_leader.display_name if self.sync_leader else None,
        )
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
        # Translate the leader's group_members (may be protocol IDs) to parent IDs
        # so we can compare against our _attr_group_members (always parent IDs)
        already_synced = set(self._translate_to_parent_ids(self.sync_leader.state.group_members))
        members_to_sync = [
            x
            for x in self._attr_group_members
            if x != self.sync_leader.player_id and x not in already_synced
        ]
        if members_to_sync:
            # If the sync leader is playing something independently, stop it first
            # to prevent protocol switching from trying to resume the previous playback
            # (we're about to start new playback on the syncgroup)
            # Use internal handler to stop the sync leader directly,
            # bypassing group redirect that would loop back to this player.
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
                # wait for the leader's state to reflect the ungroup
                await self.mass.players.wait_for_player_update(
                    sync_leader.player_id,
                    timeout=5,
                    action=self.mass.players.cmd_set_members(
                        sync_leader.player_id, [], sync_children
                    ),
                )
        # Clear the leader's active protocol so it doesn't persist
        # after the sync group is dissolved. The controller's normal
        # clearing (in _handle_cmd_stop) is skipped when the protocol
        # player had multiple group members at stop time.
        if sync_leader and sync_leader.state.playback_state != PlaybackState.PLAYING:
            sync_leader.set_active_output_protocol(None)
        self.sync_leader = None
        self._active_protocol_domain = None
        self.update_state()

    def _select_sync_leader(self, new_members: list[str] | None = None) -> Player | None:
        """Select a (new) sync leader, preferring protocol continuity."""
        if self.group_members and self.sync_leader and self.sync_leader.state.available:
            # current leader is still available, no need to select a new one
            return self.sync_leader
        # with selecting a new leader, we prioritize the static group members
        group_members = self.static_group_members or self.group_members or new_members or []

        # if we have an active protocol, prefer members that support it
        if self._active_protocol_domain:
            for member_id in group_members:
                member_player = self.mass.players.get_player(member_id)
                if (
                    member_player
                    and member_player.state.available
                    and self._member_supports_protocol_domain(
                        member_player, self._active_protocol_domain
                    )
                ):
                    self.logger.debug(
                        "Auto-selected %s as sync leader for group %s "
                        "(supports active protocol %s)",
                        member_player.display_name,
                        self.display_name,
                        self._active_protocol_domain,
                    )
                    return member_player

        # fallback: pick any available member
        for member_id in group_members:
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                self.logger.debug(
                    f"Auto-selected {member_player.display_name} as sync leader for "
                    f"group {self.display_name}"
                )
                return member_player
        return None

    def _translate_to_parent_ids(self, player_ids: list[str]) -> list[str]:
        """Translate a list of (possibly protocol) player IDs to parent player IDs.

        Protocol players (e.g. AirPlay `apc...`) are translated to their parent
        (e.g. Sonos `RINCON_...`). Non-protocol IDs pass through unchanged.

        :param player_ids: List of player IDs that may be protocol or parent IDs.
        """
        result: list[str] = []
        for pid in player_ids:
            if player := self.mass.players.get_player(pid):
                parent_id = player.protocol_parent_id or pid
                if parent_id not in result:
                    result.append(parent_id)
            elif pid not in result:
                result.append(pid)
        return result

    def _member_supports_protocol_domain(self, player: Player, domain: str) -> bool:
        """Check if a player supports the given protocol domain.

        :param player: The player to check.
        :param domain: The protocol domain string (e.g. "airplay", "sonos").
        """
        if player.provider.domain == domain:
            return True
        for protocol in player.linked_output_protocols:
            if protocol.protocol_domain == domain and protocol.available:
                return True
        return False

    def _update_active_protocol(self) -> None:
        """Update the cached active protocol domain from the sync leader."""
        self._active_protocol_domain = self._get_leader_protocol_domain()

    def _get_leader_protocol_domain(self) -> str | None:
        """Get the protocol domain of the current sync leader's active output."""
        if not self.sync_leader:
            return None
        if (
            self.sync_leader.active_output_protocol
            and self.sync_leader.active_output_protocol != "native"
        ):
            if protocol_player := self.mass.players.get_player(
                self.sync_leader.active_output_protocol
            ):
                return protocol_player.provider.domain
        return self.sync_leader.provider.domain

    async def _dynamic_leader_switch(self, old_leader_id: str) -> None:
        """Switch the sync leader without tearing down the stream session.

        Used when the protocol supports dynamic leader selection (e.g. AirPlay, Snapcast).
        The old leader is removed from the stream session while remaining members
        keep playing uninterrupted, then a new leader is selected.

        :param old_leader_id: The player_id of the leader being removed.
        """
        old_leader = self.sync_leader
        assert old_leader is not None

        self.logger.info(
            "Dynamic leader switch: removing %s from group %s, remaining members keep playing",
            old_leader.display_name,
            self.display_name,
        )

        # Remove the old leader directly at the protocol level, bypassing the
        # controller's cmd_set_members which would interpret self-removal as
        # "dissolve the entire group" (rewriting the removal list).
        group_target = old_leader
        remove_id = old_leader_id
        if (
            old_leader.active_output_protocol
            and old_leader.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get_player(old_leader.active_output_protocol))
        ):
            group_target = protocol_player
            remove_id = protocol_player.player_id
        await group_target.set_members(player_ids_to_remove=[remove_id])

        # Remove the old leader from our group members
        if old_leader_id in self._attr_group_members:
            self._attr_group_members.remove(old_leader_id)

        # Select a new leader from the remaining members.
        # _active_protocol_domain is preserved so _select_sync_leader
        # will prefer a member that supports the current protocol.
        self.sync_leader = None
        new_leader = self._select_sync_leader()
        self.sync_leader = new_leader

        if new_leader:
            # Ensure the new leader is first in the members list
            self._attr_group_members = [
                new_leader.player_id,
                *[x for x in self._attr_group_members if x != new_leader.player_id],
            ]
            self.logger.info(
                "Dynamic leader switch complete: %s is now leader of group %s",
                new_leader.display_name,
                self.display_name,
            )
            # Sync remaining members to the new leader at the protocol level.
            # Without this, the new leader's protocol player won't know about
            # the remaining group members, causing state tracking mismatches.
            remaining_members = [m for m in self._attr_group_members if m != new_leader.player_id]
            if remaining_members:
                await self.mass.players.cmd_set_members(
                    new_leader.player_id,
                    player_ids_to_add=remaining_members,
                )
        self.update_state()
