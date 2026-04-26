"""Sync Group Player implementation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    APPLICATION_NAME,
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
)
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    CONF_ENTRY_SGP_NOTE,
    CONF_MEMBERS_FILTER,
    EXTRA_FEATURES_FROM_MEMBERS,
    PROVIDERS_WITH_DYNAMIC_LEADER_SWITCH,
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
        self._attr_powered = False  # group players are always powered off by default
        self._attr_needs_poll = True
        self._update_attributes()

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
        # by default we don't have any features, except play_media and power
        # but we can gain some features based on the capabilities of the members
        # NOTE: set_members is only supported if it's a dynamic group
        # we use the power feature as a proxy for "is group active/formed"
        base_features: set[PlayerFeature] = {PlayerFeature.PLAY_MEDIA, PlayerFeature.POWER}
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
            # use state.group_members here so protocol specific id's get correctly translated
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

    async def power(self, powered: bool) -> None:
        """Handle POWER command to group player."""
        # always stop at power off
        if not powered and self.playback_state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        ):
            await self.stop()

        if powered:
            # form syncgroup when powering on
            await self._form_syncgroup()
        else:
            # dissolve syncgroup when powering off
            await self._dissolve_syncgroup()

        if self._attr_powered != powered:
            self._attr_powered = powered
            self._update_attributes()
            self.update_state()

    async def stop(self) -> None:
        """Send STOP command to given player."""
        self._attr_current_media = None
        if sync_leader := self.sync_leader:
            # Use internal handler to target the sync leader directly,
            # bypassing group/sync redirect that would loop back to this player.
            await self.mass.players._handle_cmd_stop(sync_leader.player_id)

    async def play(self) -> None:
        """Send PLAY (unpause) command to given player."""
        # The controller has already powered us on, but the group may not be
        # formed (e.g. after _dissolve_and_reform left us powered with no leader).
        # _form_syncgroup is idempotent so calling it here is cheap when already formed.
        await self._form_syncgroup()
        await self.mass.players.cmd_resume(
            self.player_id, self._attr_active_source, self._attr_current_media
        )

    async def poll(self) -> None:
        """Poll player for state updates."""
        self._update_attributes()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        self._attr_current_media = media
        self._attr_active_source = media.source_id or None
        # The controller has already powered us on, but the group may not be
        # formed (e.g. after _dissolve_and_reform left us powered with no leader).
        # _form_syncgroup is idempotent so calling it here is cheap when already formed.
        await self._form_syncgroup()
        if sync_leader := self.sync_leader:
            # Use internal handler to target the sync leader directly,
            # bypassing group/sync redirect that would loop back to this player.
            await self.mass.players._handle_play_media(sync_leader.player_id, media)
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
            if not sync_leader:
                # no leader yet (e.g. empty group) - just register the member
                # the leader and protocol selection happen on the next form/play
                if member_id not in self._attr_group_members:
                    self._attr_group_members.append(member_id)
                continue
            if member_id != sync_leader.player_id and member_id not in can_group_with:
                # incompatible with the current leader's protocols - do NOT register
                # the member or it will linger in _attr_group_members forever without
                # ever actually being synced.
                self.logger.debug(
                    f"Cannot add {member.display_name} to group {self.display_name} since it's "
                    f"not compatible with the (current) sync leader"
                )
                continue
            if member_id not in self._attr_group_members:
                self._attr_group_members.append(member_id)
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
            session_player = self._active_session_player()
            supports_handoff = (
                session_player is not None
                and session_player.provider.domain in PROVIDERS_WITH_DYNAMIC_LEADER_SWITCH
            )

            if was_playing and supports_handoff:
                # protocol supports dynamic leader switching: try to remove
                # only the departing leader and keep remaining members playing.
                # _dynamic_leader_switch will fall back to dissolve+reform
                # automatically if the chosen new leader isn't already part of
                # the live session (e.g. a freshly-added player).
                await self._dynamic_leader_switch(old_leader_id)
            else:
                # protocol doesn't support dynamic leader switching or not playing
                await self._dissolve_and_reform(old_leader_id, resume_playback=was_playing)
        elif self.sync_leader and (leader_removed or not self._attr_group_members):
            # we removed the current sync leader, and we have no members left in the group
            # or we just removed the last member from the group, so we dissolve the syncgroup
            # Use internal handler to stop the sync leader directly,
            # bypassing group redirect that would loop back to this player.
            async with self.mass.players.wait_for_player_update(
                self.sync_leader.player_id, timeout=5
            ):
                await self.mass.players._handle_cmd_stop(self.sync_leader.player_id)
            await self._dissolve_syncgroup()

        elif self.sync_leader:
            # just a regular member(s) added/removed action,
            # we can simply update the syncgroup members on the sync leader.
            # `active_protocol_domain` is derived from live state, so the
            # group will naturally downshift on the next leader selection if
            # the last protocol-requiring member was removed.
            # use _handle_set_members directly to avoid the redirect loop
            # (cmd_set_members redirects sync-leader targets back to this syncgroup)
            async with self.mass.players.get_player_lock(
                self.sync_leader.player_id, PlayerLockPurpose.PLAYBACK
            ):
                await self.mass.players._handle_set_members(
                    self.sync_leader,
                    player_ids_to_add=final_players_to_add,
                    player_ids_to_remove=final_players_to_remove,
                )
        # NOTE: If we weren't playing before, we don't need to do anything else,
        # since the syncing will be done once playback starts
        self.mass.players.trigger_player_update(self.player_id)

    def on_group_member_updated(
        self, member_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when a group member of the group player is updated."""
        self._update_attributes()
        super().on_group_member_updated(member_player, changed_values)

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
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
            # (we're about to start new playback on the syncgroup).
            # Wait for the leader to actually reach IDLE before adding members,
            # since some providers reject set_members while still playing.
            if self.sync_leader.state.playback_state == PlaybackState.PLAYING:
                async with self.mass.players.wait_for_player_update(
                    self.sync_leader.player_id,
                    attribute_name="playback_state",
                    attribute_value=PlaybackState.IDLE,
                    timeout=5,
                ):
                    await self.mass.players._handle_cmd_stop(self.sync_leader.player_id)
            # use _handle_set_members directly to avoid the redirect loop
            # (cmd_set_members redirects sync-leader targets back to this syncgroup)
            async with self.mass.players.get_player_lock(
                self.sync_leader.player_id, PlayerLockPurpose.PLAYBACK
            ):
                await self.mass.players._handle_set_members(
                    self.sync_leader, player_ids_to_add=members_to_sync
                )

    async def _dissolve_syncgroup(self) -> None:
        """Dissolve the current syncgroup by ungrouping all members."""
        if sync_leader := self.sync_leader:
            # dissolve the temporary syncgroup from the sync leader
            sync_children = [
                x for x in sync_leader.state.group_members if x != sync_leader.player_id
            ]
            if sync_children:
                # wait for the leader's state to reflect the ungroup
                # use _handle_set_members directly to avoid the redirect loop
                # (cmd_set_members redirects sync-leader targets back to this syncgroup)
                async with (
                    self.mass.players.wait_for_player_update(sync_leader.player_id, timeout=5),
                    self.mass.players.get_player_lock(
                        sync_leader.player_id, PlayerLockPurpose.PLAYBACK
                    ),
                ):
                    await self.mass.players._handle_set_members(
                        sync_leader, player_ids_to_remove=sync_children
                    )
        # Clear the leader's active protocol so it doesn't persist
        # after the sync group is dissolved. The controller's normal
        # clearing (in _handle_cmd_stop) is skipped when the protocol
        # player had multiple group members at stop time.
        if sync_leader and sync_leader.state.playback_state != PlaybackState.PLAYING:
            sync_leader.set_active_output_protocol(None)
        self.sync_leader = None
        self._update_attributes()
        self.update_state()

    def _select_sync_leader(
        self,
        new_members: list[str] | None = None,
        preferred_protocol_domain: str | None = None,
    ) -> Player | None:
        """Select a (new) sync leader, preferring protocol continuity.

        :param new_members: Optional list of newly added member ids to consider
            when no current/static members are available.
        :param preferred_protocol_domain: If provided, prefer members that
            support this protocol domain so the live session can keep playing
            on the same protocol. Typically a snapshot of
            :attr:`active_protocol_domain` taken before the old leader is
            cleared.
        """
        if self.group_members and self.sync_leader and self.sync_leader.state.available:
            # current leader is still available, no need to select a new one
            return self.sync_leader
        # with selecting a new leader, we prioritize the static group members
        group_members = self.static_group_members or self.group_members or new_members or []

        # if a preferred protocol is given, prefer members that support it
        if preferred_protocol_domain:
            for member_id in group_members:
                member_player = self.mass.players.get_player(member_id)
                if (
                    member_player
                    and member_player.state.available
                    and self._member_supports_protocol_domain(
                        member_player, preferred_protocol_domain
                    )
                ):
                    self.logger.debug(
                        "Auto-selected %s as sync leader for group %s "
                        "(supports active protocol %s)",
                        member_player.display_name,
                        self.display_name,
                        preferred_protocol_domain,
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

    def _any_member_requires_protocol_domain(self, domain: str) -> bool:
        """Return True if any current member can only play via the given protocol domain.

        A member "requires" the protocol when all of its available playback
        paths are on that domain — i.e. it has no native playback path outside
        this domain AND no linked output protocol outside this domain. This
        covers plain protocol-domain players (e.g. AirPlay) as well as
        UniversalPlayer wrappers whose native ``provider.domain`` is
        ``universal_player`` but which can still only play via a single
        linked protocol.

        :param domain: The protocol domain string (e.g. "airplay", "sonos").
        """
        for member_id in self._attr_group_members:
            member = self.mass.players.get_player(member_id)
            if member is None or not member.state.available:
                continue
            # Collect the set of available playback path domains for this member.
            paths: set[str] = set()
            if member.is_native_player:
                paths.add(member.provider.domain)
            for protocol in member.linked_output_protocols:
                if protocol.available:
                    paths.add(protocol.protocol_domain)
            if not paths:
                # nothing available at all, skip rather than force a protocol
                continue
            if paths == {domain}:
                return True
        return False

    def _active_session_player(self) -> Player | None:
        """Return the player that owns the live sync session.

        If the current sync leader has a non-native active output protocol,
        returns the protocol player that carries the stream; otherwise returns
        the native sync leader itself. Returns ``None`` if there is no leader.
        """
        if not self.sync_leader:
            return None
        if (
            self.sync_leader.active_output_protocol
            and self.sync_leader.active_output_protocol != "native"
            and (
                protocol_player := self.mass.players.get_player(
                    self.sync_leader.active_output_protocol
                )
            )
        ):
            return protocol_player
        return self.sync_leader

    @property
    def active_protocol_domain(self) -> str | None:
        """Derive the active protocol domain for this sync group on the fly.

        Returns the domain of the protocol currently carrying the live stream
        session, EXCEPT when no remaining member actually requires that
        non-native protocol — in which case the group should downshift and
        this returns the leader's native provider domain. Always computed
        from live state so it cannot drift from reality.
        """
        session_player = self._active_session_player()
        if session_player is None or self.sync_leader is None:
            return None
        domain = session_player.provider.domain
        native_domain = self.sync_leader.provider.domain
        # If a non-native protocol is in use, only keep it as "active" for
        # leader-selection purposes when some member still requires it.
        if domain != native_domain and not self._any_member_requires_protocol_domain(domain):
            return native_domain
        return domain

    def _update_attributes(self) -> None:
        """Update dynamic attributes."""
        # NOTE: Always read the *raw* attributes (not `.state.*`) from the sync leader.
        # The leader's `state.playback_state` is derived through __final_playback_state which,
        # when this group is powered, treats us as the leader's active_group and routes its
        # state back to ours - creating a circular dependency that strands both at IDLE.
        if (sync_leader := self.sync_leader) is None:
            # no sync leader, reset playback-related attributes to default values
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None
            self._attr_current_media = None
            self._attr_active_source = None
            self._attr_poll_interval = 30
            return
        self._attr_playback_state = sync_leader.state.playback_state
        self._attr_elapsed_time = sync_leader.state.elapsed_time
        self._attr_elapsed_time_last_updated = sync_leader.state.elapsed_time_last_updated
        # don't use 'state' for current_media here since that points back to this group
        # player when we're active_group, we need the 'raw' value from the sync leader
        # itself to avoid circular dependency and ensure it reflects the actual media
        # on the leader rather than the group.
        self._attr_current_media = sync_leader.current_media
        self._attr_active_source = sync_leader.active_source
        self._attr_poll_interval = 1 if self._attr_playback_state == PlaybackState.PLAYING else 30

    def _is_player_in_session(self, player: Player, session_player: Player | None) -> bool:
        """Return True if ``player`` is already a sync_client of the live session.

        A seamless leader handoff only works when the candidate's resolved
        protocol player is already in the active ``AirPlayStreamSession`` (or
        equivalent). If not (e.g. a freshly-added player that has never played
        anything), we must fall back to dissolve + reform.

        :param player: The candidate new leader.
        :param session_player: The protocol player that owns the live session
            (snapshot taken before the old leader was cleared via
            ``_active_session_player()``).
        """
        if session_player is None:
            return False
        session = getattr(getattr(session_player, "stream", None), "session", None)
        if session is None:
            # No session object (e.g. Snapcast, Sendspin) — assume handoff is
            # safe if the provider declared support for it.
            return True
        sync_clients = getattr(session, "sync_clients", None)
        if sync_clients is None:
            # Session exists but doesn't expose sync_clients — same assumption.
            return True
        # Resolve player to the protocol player that would own the session
        target: Player = player
        if (
            player.active_output_protocol
            and player.active_output_protocol != "native"
            and (p := self.mass.players.get_player(player.active_output_protocol))
        ):
            target = p
        return target in sync_clients

    async def _dissolve_and_reform(
        self,
        old_leader_id: str,
        leader_to_stop: Player | None = None,
        resume_playback: bool = True,
    ) -> None:
        """Stop the current sync session, dissolve the syncgroup, and optionally re-form.

        Used when a seamless handoff isn't possible (e.g. the new leader is not
        part of the live session). Accepts a brief audio gap in exchange for
        correctness: the old session is fully torn down before a fresh one starts.

        :param old_leader_id: The player_id of the departing leader.
        :param leader_to_stop: The player to stop before dissolving. Defaults
            to ``self.sync_leader`` but callers should pass the old leader
            explicitly when ``self.sync_leader`` has already been cleared.
        :param resume_playback: If True, call ``play()`` after dissolving to
            restart playback on the new leader. Pass False when the group was
            not actively playing (e.g. paused or idle).
        """
        leader_to_stop = leader_to_stop or self.sync_leader
        if leader_to_stop:
            self.logger.info(
                "Dissolving syncgroup %s (leader %s) and re-forming with a new leader",
                self.display_name,
                leader_to_stop.display_name,
            )
            async with self.mass.players.wait_for_player_update(
                leader_to_stop.player_id, timeout=5
            ):
                await self.mass.players._handle_cmd_stop(leader_to_stop.player_id)
        await self._dissolve_syncgroup()
        if old_leader_id in self._attr_group_members:
            self._attr_group_members.remove(old_leader_id)
        if resume_playback and self._attr_group_members:
            # Wait for the remaining members to report as unsynced before
            # re-forming. Providers like Sonos propagate group state
            # asynchronously — the children can still report synced_to for
            # a few seconds after the leader's ungroup command returns.
            await asyncio.gather(
                *(self._wait_member_unsynced(m) for m in self._attr_group_members),
                return_exceptions=True,
            )
            await self.play()

    async def _wait_member_unsynced(self, member_id: str, timeout: float = 5.0) -> None:
        """Wait until the given member reports as unsynced (synced_to is None)."""
        async with self.mass.players.wait_for_player_update(
            member_id,
            attribute_name="synced_to",
            attribute_value=None,
            timeout=timeout,
        ):
            pass

    async def _dynamic_leader_switch(self, old_leader_id: str) -> None:
        """Switch the sync leader without tearing down the stream session.

        Used when the provider supports dynamic leader selection (e.g. AirPlay,
        Snapcast). The old leader is removed from the live session and the
        remaining members keep playing uninterrupted on a newly selected leader.

        If the selected new leader is not already part of the live session (e.g.
        a freshly-added player), a seamless handoff isn't possible. In that case
        we fall back to dissolve + reform, accepting a brief audio gap.

        :param old_leader_id: The player_id of the leader being removed.
        """
        old_leader = self.sync_leader
        assert old_leader is not None

        self.logger.info(
            "Dynamic leader switch: removing %s from group %s, remaining members keep playing",
            old_leader.display_name,
            self.display_name,
        )

        # Snapshot the currently active protocol and session before clearing
        # the leader — we need both for new-leader selection and for the
        # handoff-eligibility check.
        preferred_domain = self.active_protocol_domain
        session_player = self._active_session_player()

        # Remove the old leader from our group members list
        if old_leader_id in self._attr_group_members:
            self._attr_group_members.remove(old_leader_id)

        # Pick a new leader preferring one that supports the currently active
        # protocol so the session continuation is seamless.
        self.sync_leader = None
        new_leader = self._select_sync_leader(preferred_protocol_domain=preferred_domain)

        if not new_leader:
            # No remaining members to take over — stop the old leader's
            # session and dissolve the group entirely. Restore sync_leader
            # so _dissolve_syncgroup can properly ungroup protocol members.
            self.sync_leader = old_leader
            self.logger.info(
                "No remaining members for group %s after removing %s, stopping",
                self.display_name,
                old_leader.display_name,
            )
            async with self.mass.players.wait_for_player_update(old_leader.player_id, timeout=5):
                await self.mass.players._handle_cmd_stop(old_leader.player_id)
            await self._dissolve_syncgroup()
            return

        # A seamless handoff requires the new leader to already be a
        # sync_client of the live session. If it's a freshly-added player
        # with no existing stream, fall back to dissolve + reform.
        if not self._is_player_in_session(new_leader, session_player):
            self.logger.info(
                "New leader %s is not in the live session; dissolving and re-forming syncgroup %s",
                new_leader.display_name,
                self.display_name,
            )
            # Restore sync_leader so _dissolve_and_reform -> _dissolve_syncgroup
            # can properly ungroup protocol-level members.
            self.sync_leader = old_leader
            await self._dissolve_and_reform(old_leader_id, leader_to_stop=old_leader)
            return

        self.sync_leader = new_leader
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

        # Hand off at the protocol level. We already know:
        # - the old session player (the protocol player that owns the live session)
        # - the active protocol domain
        # - the new leader (a parent player whose protocol player is in the session)
        # So we can talk to the protocol players directly and skip the controller's
        # protocol-translation overhead in cmd_set_members.
        new_target = self._resolve_session_target(new_leader, preferred_domain)
        remaining_protocol_ids: list[str] = []
        for member_id in self._attr_group_members:
            if member_id == new_leader.player_id:
                continue
            if member := self.mass.players.get_player(member_id):
                if target := self._resolve_session_target(member, preferred_domain):
                    remaining_protocol_ids.append(target.player_id)

        # 1. Old leader's session protocol player steps out of the session.
        # Direct call (the controller's cmd_set_members would interpret this
        # self-removal as "dissolve the entire group"). The provider's set_members
        # implementation handles "remove self while other clients remain" by
        # promoting another sync_client at the protocol level.
        if session_player is not None:
            await session_player.set_members(player_ids_to_remove=[session_player.player_id])

        # 2. New leader's protocol player takes over ownership tracking of the
        # remaining members. The members are already in the live session at the
        # protocol level (sync_clients), this just transfers the bookkeeping so
        # the new leader's protocol player reports them as its group members.
        if remaining_protocol_ids and new_target is not None:
            await new_target.set_members(player_ids_to_add=remaining_protocol_ids)

        self.update_state()

    def _resolve_session_target(self, player: Player, domain: str | None) -> Player | None:
        """Resolve the player that participates in the live session for ``domain``.

        For a player whose own provider domain matches, returns the player itself.
        For a parent player with a linked protocol on that domain, returns the
        corresponding protocol player. Returns ``None`` when nothing matches.

        :param player: The player to resolve (parent or protocol player).
        :param domain: The protocol domain string of the active session
            (e.g. "airplay"). May be None, in which case ``player`` is returned.
        """
        if domain is None:
            return player
        if player.provider.domain == domain:
            return player
        for linked in player.linked_output_protocols:
            if linked.protocol_domain == domain and linked.available:
                return self.mass.players.get_player(linked.output_protocol_id)
        return None
