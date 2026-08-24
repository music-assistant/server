"""Sync Group Player implementation."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.constants import PLAYER_CONTROL_FAKE
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    APPLICATION_NAME,
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
    CONF_POWER_CONTROL,
)
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    CONF_ALLOWED_MEMBERS,
    CONF_ENTRY_SGP_NOTE,
    EXTRA_FEATURES_FROM_MEMBERS,
    IDLE_GRACE_SECONDS,
    PLAYBACK_START_TIMEOUT,
    PROVIDERS_WITH_DYNAMIC_LEADER_SWITCH,
    REFORM_DEBOUNCE_SECONDS,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

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
        # the default name, not the custom one: display_name already prefers the
        # custom name, while update_state persists this one as the default name
        self._attr_name = self.config.default_name or self.config.name or f"SyncGroup {player_id}"
        self._attr_available = True
        self._attr_device_info = DeviceInfo(model=provider.name, manufacturer=APPLICATION_NAME)
        # Group players default to "no opinion" on power. The session lifecycle
        # (form on play, dissolve on stop, debounced idle deform) governs whether
        # the group is considered active. Users who want an explicit on/off button
        # can assign 'Fake power control' which is then reflected via extra_data.
        self._attr_powered = None
        self._attr_needs_poll = True
        # task that dissolves the group after the idle grace window expires
        self._idle_grace_task: asyncio.Task[None] | None = None
        # task that re-forms the group (debounced) after the sync leader was removed
        self._reform_task: asyncio.Task[None] | None = None
        # protocol hint for the debounced re-form, snapshotted before the old
        # leader was cleared so the new leader keeps protocol continuity
        self._reform_protocol_domain: str | None = None
        # monotonic timestamp of the last playback start issued to the leader
        # (-inf means never)
        self._playback_start_at: float = float("-inf")
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

    @property
    def is_active_session(self) -> bool:
        """
        Return whether this sync group is currently holding its members.

        The session is considered active while a sync leader is set (formed and
        potentially playing/paused), while the idle grace timer is still
        pending, or while a debounced re-form is pending. ``__final_active_group``
        reads this to decide whether the configured members should be marked as
        ``active_group`` for this group.
        """
        return (
            self.sync_leader is not None
            or self._idle_grace_task is not None
            or self._reform_task is not None
        )

    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        # Config is only available after the player was registered
        self._cache.clear()  # clear to prevent loading old is_dynamic
        preset_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        if self.is_dynamic:
            # In dynamic mode the configured members act as a preset: they are
            # pulled in when the group is powered on
            self._attr_static_group_members = []
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        else:
            self._attr_static_group_members = list(preset_members)
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        # Only realign the effective member list to the preset when we are
        # dormant. Otherwise a config save (e.g. user toggling an unrelated
        # field) would wipe any dynamic joins that happened during this session.
        if not self.is_active_session:
            self._attr_group_members = list(preset_members)

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        # PlayerFeature.POWER is intentionally NOT advertised by default: it forces
        # users to remember to power the group off to release its members, which
        # makes "play X on a single member from HA" silently redirect to the whole
        # group long after playback ended. The new lifecycle forms the group on
        # play and dissolves it on stop (with a short idle grace), so explicit
        # power control is no longer required.
        # Users who DO want an explicit on/off button can assign 'Fake power
        # control' in the player config; we then advertise POWER so the UI shows
        # the toggle. The raw config value is read here to avoid recursion via
        # the power_control property (which itself may inspect supported features).
        base_features: set[PlayerFeature] = {PlayerFeature.PLAY_MEDIA}
        raw_power_conf = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_POWER_CONTROL
        )
        if raw_power_conf == PLAYER_CONTROL_FAKE:
            base_features.add(PlayerFeature.POWER)
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
    def supported_sample_rates(self) -> list[tuple[int, int]] | None:
        """Return supported sample rates as defined by the sync leader."""
        # not cached: sync_leader can change during dynamic group reforms,
        # so we always re-resolve to stay in sync with the current leader
        if leader := self.sync_leader:
            return leader.get_supported_sample_rates()
        return [(44100, 16), (48000, 16)]

    @property
    def active_source(self) -> str | None:
        """Return the active source id of the current media (if any)."""
        # NOTE: Not using 'state' here as we need the 'raw' value provided by the sync leader player
        if not self.sync_leader:
            return None
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
        # Aggregate can_group_with from ALL current group members (not just the leader).
        # A sync group can accommodate protocol switches, so a player compatible with
        # ANY current member is a valid candidate to join.
        member_ids = self._attr_group_members if self._attr_group_members else []
        # current members bypass the allow-list filter (filter constrains joiners only)
        current_members = set(member_ids)
        can_group_with: set[str] = set()
        for member_id in member_ids:
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                can_group_with.add(member_player.player_id)
                can_group_with.update(member_player.state.can_group_with)
        if can_group_with:
            return {
                pid
                for pid in can_group_with
                if pid in current_members or self._is_member_allowed(pid)
            }
        # Without any available member to derive compatibility from (empty group or
        # all members offline), offer any compatible player.
        # Actual compatibility is validated when adding members
        can_group_with = set()
        for player in self.mass.players.iter_players(return_unavailable=False):
            if not player.available or player.type == PlayerType.GROUP:
                # let's avoid showing group players as options to group with
                continue
            if (
                PlayerFeature.SET_MEMBERS in player.state.supported_features
                and player.state.can_group_with
                and not player.state.active_group
            ):
                can_group_with.add(player.player_id)
        return {pid for pid in can_group_with if self._is_member_allowed(pid)}

    @property
    def group_members(self) -> list[str]:
        """Return the list of parent player id's that are part of this sync group."""
        if (sync_leader := self.sync_leader) and sync_leader.state.group_members:
            # use state.group_members here so protocol specific id's get correctly translated
            return sync_leader.state.group_members
        return self._attr_group_members

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # keep saved player ids so the UI can render a user friendly name
        # prevents the bug where only the player ids show up during playback
        saved_ids = {
            *cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []) or []),
            *cast("list[str]", self.config.get_value(CONF_ALLOWED_MEMBERS, []) or []),
        }
        possible_players = sorted(
            [
                ConfigValueOption(x.player_id, title=x.display_name)
                for x in self.mass.players.all_players(True, False)
                if x.type != PlayerType.GROUP
                and (
                    x.player_id in saved_ids
                    or (
                        PlayerFeature.SET_MEMBERS in x.state.supported_features
                        # also include synced followers: can_group_with returns empty
                        # while slaved, but the player is still group-capable
                        and (x.state.can_group_with or x.state.synced_to)
                    )
                )
            ],
            key=lambda x: x.title or "",
        )
        entries: list[ConfigEntry] = [
            # syncgroup specific entries
            CONF_ENTRY_SGP_NOTE,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                default_value=[],
                required=False,  # needed for dynamic members (which allows empty members list)
                options=possible_players,
            ),
            ConfigEntry(
                key=CONF_DYNAMIC_GROUP_MEMBERS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
            ConfigEntry(
                key=CONF_ALLOWED_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                default_value=[],
                required=False,
                options=possible_players,
                depends_on=CONF_DYNAMIC_GROUP_MEMBERS,
                advanced=True,
            ),
        ]
        return entries

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command to group player.

        Only called when the user has assigned a power control (native or fake)
        to the group. Powering ON pre-forms the group so its members are
        captured immediately (matching the legacy behaviour for users who opt
        in). Powering OFF stops any playback and dissolves the group.

        :param powered: True to power on (form/capture), False to power off (dissolve).
        """
        # always cancel any pending idle-grace timer on explicit power transitions
        self._cancel_idle_grace_timer()

        if not powered and self.playback_state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        ):
            # stop directly via the leader to avoid re-entering our own stop()
            # logic (which would dissolve before we re-dissolve below).
            if sync_leader := self.sync_leader:
                await self.mass.players._handle_cmd_stop(sync_leader.player_id)

        if powered:
            # apply the configured preset members on power-on so unjoins
            # during a powered session stick until the next power cycle
            preset_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
            self._attr_group_members = [
                *preset_members,
                *[x for x in self._attr_group_members if x not in preset_members],
            ]
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
        """
        Send STOP command to given player.

        An explicit stop on the group dissolves the syncgroup immediately so the
        members are released back to individual control. The idle grace timer is
        intentionally only used when the queue ends naturally (playback_state
        transitions to IDLE without a stop command). Users who want the group
        to stay formed across stops can assign Fake power control and use that
        to pin the group as 'active'.
        """
        self._cancel_idle_grace_timer()
        # an explicit stop also voids any pending debounced re-form and the
        # startup marker — the user asked for silence
        self._cancel_reform_timer()
        self._playback_start_at = float("-inf")
        self._attr_current_media = None
        if sync_leader := self.sync_leader:
            # Use internal handler to target the sync leader directly,
            # bypassing group/sync redirect that would loop back to this player.
            await self.mass.players._handle_cmd_stop(sync_leader.player_id)
        # Skip the dissolve when the user has explicitly powered the group on
        # via Fake power control — they expect the group to stay 'active' until
        # they power it off, even after a stop.
        if self._attr_powered is True:
            return
        await self._dissolve_syncgroup()

    async def play(self) -> None:
        """Send PLAY (unpause) command to given player."""
        # The controller has already powered us on, but the group may not be
        # formed (e.g. after _dissolve_and_reform left us powered with no leader).
        # _form_syncgroup is idempotent so calling it here is cheap when already formed.
        await self._form_syncgroup()
        # Hold the group's playback lock until the leader actually reports playing
        # so a concurrent (un)group command can't race the in-flight start — which
        # would otherwise leave a player streaming outside the group.
        async with self._await_leader_playback():
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
            # Hold the group's playback lock until the leader confirms playback
            # (see play()) so a concurrent (un)group command can't race the start.
            async with (
                self.mass.players.get_player_lock(
                    sync_leader.player_id, PlayerLockPurpose.PLAYBACK
                ),
                self._await_leader_playback(),
            ):
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
                f"Group {self.display_name} does not allow dynamically adding/removing members!",
                translation_key="not_dynamic",
                translation_owner=self.translation_owner,
                translation_args=[self.display_name],
            )
        sync_leader = self.sync_leader or self._select_sync_leader(new_members=player_ids_to_add)
        # A start that was just issued to the leader may not be reflected in the
        # (transient) device state yet, so treat the startup window as playing —
        # otherwise a (un)group command racing an in-flight start misreads the
        # group as idle and skips the resume. An explicit pause always wins:
        # PAUSED is deliberate user intent, never startup noise.
        was_playing = self.playback_state == PlaybackState.PLAYING or (
            self.playback_state != PlaybackState.PAUSED and self._playback_recently_started
        )

        # handle additions
        final_players_to_add: list[str] = []
        can_group_with = sync_leader.state.can_group_with.copy() if sync_leader else set()
        for member_id in player_ids_to_add or []:
            if member_id == self.player_id:
                continue  # can not add self as member
            if not self._is_member_allowed(member_id):
                self.logger.warning(
                    "Player %s is not allowed to join group %s by the configured player filters, "
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
                # Fallback for a member that was grouped to the sync leader
                # outside of MA (e.g. via the Sonos app): it isn't part of our
                # tracked member list but does show up in group_members via the
                # leader's live state. Forward its removal to the sync leader
                # instead of silently skipping it.
                if (
                    self.sync_leader
                    and member_id != self.sync_leader.player_id
                    and member_id in self.sync_leader.state.group_members
                ):
                    final_players_to_remove.append(member_id)
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
                    f"Cannot remove {self.display_name} from itself as a member!",
                    translation_key="remove_self",
                    translation_owner=self.translation_owner,
                    translation_args=[self.display_name],
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
                # automatically if no remaining member is part of the live
                # session (e.g. only freshly-added players are left).
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
            # group will naturally downshift once it re-forms if every
            # remaining member can play on the leader's native protocol.
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
        elif self._reform_task is not None:
            # leaderless with a debounced re-form pending: membership just changed,
            # so re-arm the window — the re-form picks up the final member list.
            self._schedule_reform_timer()
        # NOTE: If we weren't playing before, we don't need to do anything else,
        # since the syncing will be done once playback starts
        self.mass.players.trigger_player_update(self.player_id)

    def on_group_member_updated(
        self, member_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Handle callback when a group member of the group player is updated."""
        self._update_attributes()
        super().on_group_member_updated(member_player, changed_values)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self._cancel_idle_grace_timer()
        self._cancel_reform_timer()
        await super().on_unload()
        # the player is going away; make sure we don't leave the protocol-level
        # sync group standing with a now-nonexistent leader behind it.
        if self.sync_leader is not None:
            await self._dissolve_syncgroup()

    @property
    def active_protocol_domain(self) -> str | None:
        """
        Derive the active protocol domain for this sync group on the fly.

        Returns the domain of the protocol currently carrying the live stream
        session, EXCEPT when every remaining member can also play on the
        leader's native domain — in which case the group should downshift and
        this returns that native domain. Always computed from live state so it
        cannot drift from reality.

        Because of that downshift this is a hint for the next leader selection,
        not an address for the live session: use ``_active_session_player()``
        to reach the players that are carrying the stream right now.
        """
        session_player = self._active_session_player()
        if session_player is None or self.sync_leader is None:
            return None
        domain = session_player.provider.domain
        native_domain = self.sync_leader.provider.domain
        # Keep a non-native protocol "active" for leader-selection purposes unless
        # the whole group can be reached on the leader's native domain.
        if domain != native_domain and self._all_members_can_play_on_domain(native_domain):
            return native_domain
        return domain

    def _is_member_allowed(self, player_id: str) -> bool:
        """Return whether a player is allowed to join this group given the configured filter."""
        # preset members should always be allowed to re-join
        preset_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []) or [])
        if player_id in preset_members:
            return True
        allowed_members = cast("list[str]", self.config.get_value(CONF_ALLOWED_MEMBERS, []) or [])
        return not allowed_members or player_id in allowed_members

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by syncing all (possible) members."""
        # any in-flight grace or debounced re-form timer is moot now —
        # we're (re)forming the group
        self._cancel_idle_grace_timer()
        self._cancel_reform_timer()
        self.logger.debug(
            "Forming syncgroup %s, _attr_group_members=%s, sync_leader=%s",
            self.display_name,
            self._attr_group_members,
            self.sync_leader.display_name if self.sync_leader else None,
        )
        # select new sync leader if needed
        if not self.sync_leader:
            self.sync_leader = self._select_sync_leader()

        # pin the leader ref: a concurrent command (e.g. a dissolve) may clear
        # or replace self.sync_leader while we await below
        leader = self.sync_leader
        if not leader:
            # we have no members in the group, so we can't form a syncgroup
            return

        # ensure the sync leader is first in the list
        self._attr_group_members = [
            leader.player_id,
            *[x for x in self._attr_group_members if x != leader.player_id],
        ]
        # If the leader still believes it's synced to a previous leader (e.g. we
        # just picked a new leader after dissolving the old session and the
        # protocol-level state hasn't propagated yet), wait for it to settle.
        # Without this, the subsequent play_media call hits the provider's
        # "I'm synced to another player" guard and gets rejected.
        if leader.state.synced_to is not None:
            self.logger.debug(
                "Waiting for new leader %s to report synced_to=None before forming",
                leader.display_name,
            )
            if not await self._wait_member_unsynced(leader.player_id):
                # Leader is genuinely stuck — bail out before issuing play_media
                # so we don't trigger the provider's "synced to another player"
                # rejection. The caller (play / play_media) will surface this as
                # a no-op form; the next user action can retry once the
                # protocol layer has caught up.
                self.logger.error(
                    "Aborting syncgroup form for %s: leader %s is stuck synced",
                    self.display_name,
                    leader.display_name,
                )
                self.sync_leader = None
                return
            if self.sync_leader is not leader:
                # the group was dissolved or re-led while we waited —
                # this form attempt is stale, abort
                return
        # Translate the leader's group_members (may be protocol IDs) to parent IDs
        # so we can compare against our _attr_group_members (always parent IDs)
        already_synced = set(self._translate_to_parent_ids(leader.state.group_members))
        members_to_sync = [
            x for x in self._attr_group_members if x != leader.player_id and x not in already_synced
        ]
        if members_to_sync:
            # If the sync leader is playing something independently, stop it first
            # to prevent protocol switching from trying to resume the previous playback
            # (we're about to start new playback on the syncgroup).
            # Wait for the leader to actually reach IDLE before adding members,
            # since some providers reject set_members while still playing.
            if leader.state.playback_state == PlaybackState.PLAYING:
                async with self.mass.players.wait_for_player_update(
                    leader.player_id,
                    attribute_name="playback_state",
                    attribute_value=PlaybackState.IDLE,
                    timeout=5,
                ):
                    await self.mass.players._handle_cmd_stop(leader.player_id)
                if self.sync_leader is not leader:
                    # the group was dissolved or re-led while we waited —
                    # this form attempt is stale, abort
                    return
            # use _handle_set_members directly to avoid the redirect loop
            # (cmd_set_members redirects sync-leader targets back to this syncgroup)
            async with self.mass.players.get_player_lock(
                leader.player_id, PlayerLockPurpose.PLAYBACK
            ):
                await self.mass.players._handle_set_members(
                    leader, player_ids_to_add=members_to_sync
                )

    @asynccontextmanager
    async def _await_leader_playback(self) -> AsyncIterator[None]:
        """
        Wait for the sync leader to confirm playback for the command run in the body.

        Wrap the play/resume call that targets the leader in this context manager.
        The group's playback lock (held by the caller) then stays acquired until the
        leader actually reports playing, so a concurrent (un)group command cannot
        race a start that has not yet taken effect at the device. A no-op when there
        is no leader to wait on.
        """
        if (leader := self.sync_leader) is None:
            yield
            return
        # stamp the start: device state is unreliable while a start settles, so
        # group-command decisions treat this window as playing (see set_members)
        self._playback_start_at = time.monotonic()
        async with self.mass.players.wait_for_player_update(
            leader.player_id,
            attribute_name="playback_state",
            attribute_value=PlaybackState.PLAYING,
            timeout=PLAYBACK_START_TIMEOUT,
        ):
            yield

    async def _dissolve_syncgroup(self) -> None:
        """Dissolve the current syncgroup by ungrouping all members."""
        # a dissolve is happening now — any pending grace or re-form timer is no
        # longer needed (_dissolve_and_reform re-arms the re-form right after)
        # and the session whose start the marker tracked is gone
        self._cancel_idle_grace_timer()
        self._cancel_reform_timer()
        self._playback_start_at = float("-inf")
        if sync_leader := self.sync_leader:
            # dissolve the temporary syncgroup from the player that holds the members:
            # ungrouping from a leader that no longer holds them is a no-op and would
            # leave the members grouped and streaming with no way back
            group_leader = self._protocol_group_leader(sync_leader)
            sync_children = [
                x for x in group_leader.state.group_members if x != group_leader.player_id
            ]
            if sync_children:
                # wait for the leader's state to reflect the ungroup
                # use _handle_set_members directly to avoid the redirect loop
                # (cmd_set_members redirects sync-leader targets back to this syncgroup)
                async with (
                    self.mass.players.wait_for_player_update(group_leader.player_id, timeout=5),
                    self.mass.players.get_player_lock(
                        group_leader.player_id, PlayerLockPurpose.PLAYBACK
                    ),
                ):
                    await self.mass.players._handle_set_members(
                        group_leader, player_ids_to_remove=sync_children
                    )
            if group_leader is not sync_leader:
                # our callers only ever stop the leader we track, so a provider-promoted
                # one would keep streaming on its own once the members are released
                await self.mass.players._handle_cmd_stop(group_leader.player_id)
                self.mass.players.schedule_active_output_protocol_clear(group_leader)
        # Clear the leader's active protocol once it stops playing; the controller's
        # clearing in _handle_cmd_stop is skipped for a still-grouped protocol player.
        if sync_leader:
            self.mass.players.schedule_active_output_protocol_clear(sync_leader)
        self.sync_leader = None
        self._update_attributes()
        self.update_state()

    def _select_sync_leader(
        self,
        new_members: list[str] | None = None,
        preferred_protocol_domain: str | None = None,
        preferred_member_ids: Collection[str] | None = None,
    ) -> Player | None:
        """
        Select a (new) sync leader, preferring session and protocol continuity.

        :param new_members: Optional list of newly added member ids to consider
            when no current/static members are available.
        :param preferred_protocol_domain: If provided, prefer members that
            support this protocol domain so playback keeps using the same
            protocol.
        :param preferred_member_ids: If provided, prefer members from this
            collection (e.g. the ones a live session already feeds). Outranks
            ``preferred_protocol_domain``.
        """
        if self.group_members and self.sync_leader and self.sync_leader.state.available:
            # current leader is still available, no need to select a new one
            return self.sync_leader
        # with selecting a new leader, we prioritize the static group members
        group_members = self.static_group_members or self.group_members or new_members or []
        candidates = [
            member_player
            for member_id in group_members
            if (member_player := self.mass.players.get_player(member_id))
            and member_player.state.available
        ]
        preferred_ids = set(preferred_member_ids or ())
        # preference tiers, most specific first: a member that is already fed by the
        # live session can take it over without restarting playback, and one that
        # supports the active protocol at least keeps the session on that protocol
        for reason, matches in (
            (
                "takes part in the live session",
                [x for x in candidates if x.player_id in preferred_ids],
            ),
            (
                f"supports active protocol {preferred_protocol_domain}",
                [
                    x
                    for x in candidates
                    if preferred_protocol_domain
                    and self._member_supports_protocol_domain(x, preferred_protocol_domain)
                ],
            ),
            ("first available member", candidates),
        ):
            if not matches:
                continue
            self.logger.debug(
                "Auto-selected %s as sync leader for group %s (%s)",
                matches[0].display_name,
                self.display_name,
                reason,
            )
            return matches[0]
        return None

    # -----------------------------------------------------------------------
    # Protocol awareness
    # -----------------------------------------------------------------------
    # A sync group can contain members from multiple protocol domains (e.g. a
    # Sonos that can play via either its native protocol or via AirPlay). The
    # group needs to:
    #   - track which protocol the live session is using (active_protocol_domain)
    #   - resolve which player actually owns the protocol-level session
    #     (_active_session_player) so leader/handoff bookkeeping is done on
    #     the right object
    #   - choose new leaders that keep protocol continuity
    #     (_member_supports_protocol_domain / _select_sync_leader)
    #   - downshift to the native protocol once every remaining member can play
    #     on it (_all_members_can_play_on_domain)
    #   - stay aligned with the protocol's own view of the group, both in member
    #     order (_align_members_with_session) and in who leads it
    #     (_protocol_group_leader)
    # The helpers below cover those needs. Composition decisions (which protocol
    # to use given the current member mix) intentionally live in the group:
    # individual protocol providers don't have visibility into the rest of the
    # group's members.

    def _translate_to_parent_ids(self, player_ids: list[str]) -> list[str]:
        """
        Translate a list of (possibly protocol) player IDs to parent player IDs.

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
        """
        Check if a player can be reached on the given protocol domain right now.

        :param player: The player to check.
        :param domain: The protocol domain string (e.g. "airplay", "sonos").
        """
        return domain in player.playback_domains

    def _all_members_can_play_on_domain(self, domain: str) -> bool:
        """
        Return True if every current member has a playback path on the given domain.

        Members that are unavailable or expose no playback path at all are
        ignored, so they never hold the group on a protocol.

        :param domain: The playback path domain to check (e.g. "airplay", "sonos").
        """
        for member_id in self._attr_group_members:
            member = self.mass.players.get_player(member_id)
            if member is None or not member.state.available:
                continue
            paths = member.playback_domains
            if paths and domain not in paths:
                return False
        return True

    def _active_session_player(self) -> Player | None:
        """
        Return the player that owns the live sync session.

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

    def _align_members_with_session(self, session_player: Player | None) -> None:
        """
        Re-order the tracked members to match the live session's member order.

        Members that are not part of the live session keep their relative order at
        the end of the list.

        :param session_player: The player that owns the live sync session.
        """
        if session_player is None:
            return
        # the provider's own group_members, not state.group_members: the latter is
        # set-derived for non-protocol players and loses the member order. Not
        # live_session_members either: that answers who is in the session, not in
        # which order, and a provider may derive it without preserving any.
        session_order = [
            x
            for x in self._translate_to_parent_ids(session_player.group_members)
            if x in self._attr_group_members
        ]
        if not session_order:
            return
        self._attr_group_members = [
            *session_order,
            *[x for x in self._attr_group_members if x not in session_order],
        ]

    def _protocol_group_leader(self, sync_leader: Player) -> Player:
        """
        Return the member that currently holds the protocol-level group.

        This is normally the sync leader itself, but a provider may have promoted a
        different member at the protocol level. Falls back to the sync leader when no
        member reports holding others.

        :param sync_leader: The sync leader tracked by this group.
        """
        if sync_leader.state.group_members:
            return sync_leader
        for member_id in self._attr_group_members:
            if member_id == sync_leader.player_id:
                continue
            member = self.mass.players.get_player(member_id)
            if member is None or not member.state.available:
                continue
            # a leader always lists itself alongside its members; only adopt one that
            # holds members of this group, never a group formed outside of MA
            held = [x for x in member.state.group_members if x != member_id]
            if held and not set(held).isdisjoint(self._attr_group_members):
                self.logger.warning(
                    "Syncgroup %s tracks %s as leader but %s holds the group members",
                    self.display_name,
                    sync_leader.display_name,
                    member.display_name,
                )
                return member
        return sync_leader

    def _update_attributes(self) -> None:
        """Update dynamic attributes."""
        # NOTE on what reads from `.state.*` vs the leader's raw attributes below:
        # `__final_current_media` and `__final_active_source` on a player that has
        # an ``active_group`` route through the active_group's state — so reading
        # ``sync_leader.state.current_media`` from inside the group would loop
        # back through our own state derivation (group → leader.state →
        # active_group=group → group). For those two we MUST use the leader's
        # raw attributes. ``playback_state`` / ``elapsed_time`` do not route via
        # active_group and are safe to read from ``.state.*``.
        if (sync_leader := self.sync_leader) is None:
            # no sync leader, reset playback-related attributes to default values
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None
            self._attr_current_media = None
            self._attr_active_source = None
            self._attr_poll_interval = 30
            return
        prev_state = self._attr_playback_state
        new_state = sync_leader.state.playback_state
        self._attr_playback_state = new_state
        self._attr_elapsed_time = sync_leader.state.elapsed_time
        self._attr_elapsed_time_last_updated = sync_leader.state.elapsed_time_last_updated
        # don't use 'state' for current_media here since that points back to this group
        # player when we're active_group, we need the 'raw' value from the sync leader
        # itself to avoid circular dependency and ensure it reflects the actual media
        # on the leader rather than the group.
        self._attr_current_media = sync_leader.current_media
        self._attr_active_source = sync_leader.active_source
        self._attr_poll_interval = 1 if new_state == PlaybackState.PLAYING else 30
        # idle grace handling: schedule a debounced dissolve when the leader
        # naturally transitions from PLAYING/PAUSED to IDLE. The dissolve is
        # skipped if the user has pinned the group with Fake power control.
        if new_state == PlaybackState.IDLE and prev_state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        ):
            if self._attr_powered is not True:
                self._schedule_idle_grace_timer()
        elif new_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # leader resumed playing, cancel any pending grace
            self._cancel_idle_grace_timer()

    async def _dissolve_and_reform(
        self,
        old_leader_id: str,
        leader_to_stop: Player | None = None,
        resume_playback: bool = True,
        preferred_protocol_domain: str | None = None,
    ) -> None:
        """
        Stop the current sync session, dissolve the syncgroup and schedule a re-form.

        Used when a seamless handoff isn't possible (e.g. the new leader is not
        part of the live session). The stop/dissolve happens immediately; the
        re-form (with resume) is debounced so cascaded unjoins coalesce into a
        single restart with the final member list.

        :param old_leader_id: The player_id of the departing leader.
        :param leader_to_stop: The player to stop before dissolving. Defaults
            to ``self.sync_leader`` but callers should pass the old leader
            explicitly when ``self.sync_leader`` has already been cleared.
        :param resume_playback: If True, schedule the debounced re-form which
            restarts playback on the new leader. Pass False when the group was
            not actively playing (e.g. paused or idle).
        :param preferred_protocol_domain: Optional snapshot of the active
            protocol domain taken before the old leader was cleared, passed to
            :meth:`_select_sync_leader` so the new leader is chosen to keep the
            protocol session continuous where possible.
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
            self._reform_protocol_domain = preferred_protocol_domain
            self._schedule_reform_timer()

    async def _wait_member_unsynced(self, member_id: str, timeout: float = 5.0) -> bool:
        """
        Wait until the given member reports as unsynced (synced_to is None).

        Returns ``True`` when the member is verified unsynced (downstream flows
        like leader selection / play_media on the leader are safe to proceed),
        or ``False`` when the player is genuinely stuck (the caller should
        abort rather than issue a play_media that will be rejected by the
        provider's "I'm synced to another player" guard).

        :param member_id: The player to wait on.
        :param timeout: Seconds to wait for the first state propagation.
        """
        async with self.mass.players.wait_for_player_update(
            member_id,
            attribute_name="synced_to",
            attribute_value=None,
            timeout=timeout,
        ):
            pass
        member = self.mass.players.get_player(member_id)
        if member is None or member.synced_to is None:
            return True
        # The provider didn't propagate within the timeout. Kick the member
        # from its stale parent, then wait again with a tighter budget.
        # This rescues the common "Sonos UPnP event lag" case.
        # NOTE: not the public cmd_ungroup - it re-enters this syncgroup's set_members.
        # if the stale parent is gone, no kick is possible - fall through to the final check
        if stale_parent := self.mass.players.get_player(member.synced_to):
            self.logger.warning(
                "Player %s still reports synced_to=%s after %ss; "
                "removing it from its stale parent and re-waiting",
                member.display_name,
                member.synced_to,
                timeout,
            )
            try:
                async with (
                    self.mass.players.wait_for_player_update(
                        member_id,
                        attribute_name="synced_to",
                        attribute_value=None,
                        timeout=2.0,
                    ),
                    self.mass.players.get_player_lock(
                        stale_parent.player_id, PlayerLockPurpose.PLAYBACK
                    ),
                ):
                    await self.mass.players._handle_set_members(
                        stale_parent, player_ids_to_remove=[member_id]
                    )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug(
                    "stale-parent removal recovery for %s raised: %s", member.display_name, err
                )
        member = self.mass.players.get_player(member_id)
        if member is None or member.synced_to is None:
            return True
        self.logger.error(
            "Player %s is stuck synced_to=%s; aborting dissolve+reform path",
            member.display_name,
            member.synced_to,
        )
        return False

    async def _dynamic_leader_switch(self, old_leader_id: str) -> None:
        """
        Switch the sync leader without tearing down the stream session.

        Used when the provider supports dynamic leader selection (e.g. AirPlay,
        Snapcast). The old leader is removed from the live session and the
        remaining members keep playing uninterrupted on a newly selected leader.

        If no remaining member takes part in the live session (e.g. only
        freshly-added players are left), a seamless handoff isn't possible. In
        that case we fall back to dissolve + reform, accepting a brief audio gap.

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
        # The domain the live session actually runs on. It differs from
        # `preferred_domain` once the group is due to downshift to native, and
        # a seamless handoff must stay on the protocol carrying the stream.
        session_domain = session_player.provider.domain if session_player else None
        # The members the session feeds right now: only a member from this set can take
        # it over without a restart. Tracked membership is not enough — a member can be
        # dropped from the session (or never make it in) while still being listed.
        live_member_ids = (
            self._translate_to_parent_ids(session_player.live_session_members)
            if session_player
            else []
        )

        # Remove the old leader from our group members list
        if old_leader_id in self._attr_group_members:
            self._attr_group_members.remove(old_leader_id)

        # A provider may hand the live session to its own first remaining member, so our
        # member order has to match the session's before we pick from it — otherwise we
        # end up tracking a different leader than the one that inherits the session.
        self._align_members_with_session(session_player)

        # Pick a new leader, preferring one that is already fed by the live session
        # so the session continuation is seamless.
        self.sync_leader = None
        new_leader = self._select_sync_leader(
            preferred_protocol_domain=session_domain,
            preferred_member_ids=live_member_ids,
        )

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

        # A seamless handoff requires the new leader to already be a sync_client of
        # the live session. Selection prefers such a member, so reaching this means
        # no remaining member has a stream to inherit: fall back to dissolve + reform.
        if new_leader.player_id not in live_member_ids:
            self.logger.info(
                "New leader %s is not in the live session; dissolving and re-forming syncgroup %s",
                new_leader.display_name,
                self.display_name,
            )
            # Restore sync_leader so _dissolve_and_reform -> _dissolve_syncgroup
            # can properly ungroup protocol-level members. Forward the protocol
            # hint so the new form keeps protocol continuity when possible.
            self.sync_leader = old_leader
            await self._dissolve_and_reform(
                old_leader_id,
                leader_to_stop=old_leader,
                preferred_protocol_domain=preferred_domain,
            )
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
        # - the domain that session runs on
        # - the new leader (a parent player whose protocol player is in the session)
        # So we can talk to the protocol players directly and skip the controller's
        # protocol-translation overhead in cmd_set_members.
        new_target = self._resolve_session_target(new_leader, session_domain)
        remaining_protocol_ids: list[str] = []
        for member_id in self._attr_group_members:
            if member_id == new_leader.player_id:
                continue
            if member := self.mass.players.get_player(member_id):
                if target := self._resolve_session_target(member, session_domain):
                    remaining_protocol_ids.append(target.player_id)

        # 1. Old leader's session protocol player steps out of the session.
        # Direct call (the controller's cmd_set_members would interpret this
        # self-removal as "dissolve the entire group"). The provider's set_members
        # keeps the live session running for the members that stay behind and releases
        # them, so they are briefly without a leader until step 2 picks them back up.
        if session_player is not None:
            await session_player.set_members(player_ids_to_remove=[session_player.player_id])

        # 2. New leader's protocol player takes over ownership tracking of the
        # remaining members. The members are already in the live session at the
        # protocol level (sync_clients), this just transfers the bookkeeping so
        # the new leader's protocol player reports them as its group members.
        if remaining_protocol_ids and new_target is not None:
            await new_target.set_members(player_ids_to_add=remaining_protocol_ids)

        self.update_state()

    def _schedule_idle_grace_timer(self) -> None:
        """Schedule a debounced dissolve after the leader becomes idle."""
        # any previously scheduled task is replaced so we don't end up with
        # two dissolves racing each other when the leader oscillates quickly
        self._cancel_idle_grace_timer()
        self.logger.debug(
            "Scheduling idle-grace dissolve for syncgroup %s in %ss",
            self.display_name,
            IDLE_GRACE_SECONDS,
        )
        self._idle_grace_task = self.mass.create_task(self._idle_grace_runner())

    def _cancel_idle_grace_timer(self) -> None:
        """Cancel any pending idle-grace dissolve task."""
        if self._idle_grace_task is not None:
            if not self._idle_grace_task.done():
                self._idle_grace_task.cancel()
            self._idle_grace_task = None

    async def _idle_grace_runner(self) -> None:
        """Wait the grace window, then dissolve if the group is still idle."""
        try:
            await asyncio.sleep(IDLE_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        # re-check state at fire time — playback may have resumed, the user
        # may have powered the group on, or another path may have dissolved
        # us already. Any of these means we should not dissolve here.
        self._idle_grace_task = None
        if self.sync_leader is None:
            return
        if self._attr_powered is True:
            return
        if self.sync_leader.state.playback_state != PlaybackState.IDLE:
            return
        self.logger.info(
            "Idle-grace expired for syncgroup %s, dissolving",
            self.display_name,
        )
        await self._dissolve_syncgroup()

    @property
    def _playback_recently_started(self) -> bool:
        """Return whether a playback start was issued within the settle window."""
        return (time.monotonic() - self._playback_start_at) < PLAYBACK_START_TIMEOUT

    def _schedule_reform_timer(self) -> None:
        """(Re)schedule the debounced re-form after the sync leader was removed."""
        # any previously scheduled task is replaced so cascaded unjoins coalesce
        # into a single re-form with the final member list
        self._cancel_reform_timer()
        self.logger.debug(
            "Scheduling debounced re-form for syncgroup %s in %ss",
            self.display_name,
            REFORM_DEBOUNCE_SECONDS,
        )
        self._reform_task = self.mass.create_task(self._reform_runner())

    def _cancel_reform_timer(self) -> None:
        """Cancel any pending debounced re-form task."""
        if (task := self._reform_task) is None:
            return
        self._reform_task = None
        # never cancel ourselves: the runner ends up here via play() -> _form_syncgroup
        if task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _reform_runner(self) -> None:
        """Wait the debounce window, then re-form the group and resume playback."""
        try:
            await asyncio.sleep(REFORM_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            # serialize with (un)group and playback commands targeting this group.
            # A cancellation (another unjoin re-arming the window, an explicit
            # stop) may still land while we wait for the lock.
            async with self.mass.players.get_player_lock(
                self.player_id, PlayerLockPurpose.PLAYBACK
            ):
                # re-check state at execution time — an explicit play may have
                # re-formed the group already and all members may have been
                # removed meanwhile
                if self.sync_leader is not None or not self._attr_group_members:
                    return
                # Wait for the remaining members to report as unsynced before
                # re-forming. Providers like Sonos propagate group state
                # asynchronously — the children can still report synced_to for
                # a few seconds after the leader's ungroup command returns.
                members = list(self._attr_group_members)
                unsync_results = await asyncio.gather(
                    *(self._wait_member_unsynced(m) for m in members),
                    return_exceptions=True,
                )
                stuck_members = [
                    members[i] for i, result in enumerate(unsync_results) if result is False
                ]
                if stuck_members:
                    self.logger.error(
                        "Members of group %s still report synced_to after recovery attempts: "
                        "%s; aborting re-form (no playback will resume on this call)",
                        self.display_name,
                        stuck_members,
                    )
                    return
                self.logger.info(
                    "Re-forming syncgroup %s with %s member(s) and resuming playback",
                    self.display_name,
                    len(members),
                )
                # Preselect the new leader with the protocol hint so the form
                # picks a member compatible with the previous session's protocol
                # (e.g. keep AirPlay if the session was AirPlay).
                if self._reform_protocol_domain is not None:
                    self.sync_leader = self._select_sync_leader(
                        preferred_protocol_domain=self._reform_protocol_domain
                    )
                await self.play()
        finally:
            # normal completion detaches us via play() -> _form_syncgroup already;
            # this covers the early-return paths so is_active_session settles
            if self._reform_task is asyncio.current_task():
                self._reform_task = None
                self.update_state()

    def _resolve_session_target(self, player: Player, domain: str | None) -> Player | None:
        """
        Resolve the player that participates in the live session for ``domain``.

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
            if linked.protocol_domain == domain:
                return self.mass.players.get_player(linked.output_protocol_id)
        return None
