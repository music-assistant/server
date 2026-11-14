"""
Volume Mixin for the Player Controller.

The volume and mute commands of a player and of a group of players, the volume limits
of a player and the hand over of its level when another protocol takes over its volume.

This module provides the VolumeMixin class which is inherited by
PlayerController to add the volume and mute commands.
"""

from __future__ import annotations

import asyncio
import time
from typing import cast

from music_assistant_models.auth import Scope
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.constants import (
    ATTR_FAKE_MUTE,
    ATTR_FAKE_VOLUME,
    ATTR_GROUP_VOLUME_SNAPSHOT,
    ATTR_MUTE_LOCK,
    ATTR_PREVIOUS_VOLUME,
    ATTR_VOLUME_TARGET,
    CONF_ENTRY_MAX_VOLUME,
    CONF_ENTRY_MIN_VOLUME,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_VOLUME_STEP,
)
from music_assistant.helpers.api import api_command
from music_assistant.models.player import Player, PlayerState

from .base import _PlayerControllerBase
from .constants import PlayerLockPurpose, SwitchedOffAction
from .helpers import handle_player_command

# How long the volume level of the last command outranks the level the player reports.
# Long enough to cover a burst of volume nudges on a player that only reports its volume
# back some time later, short enough for a change made on the device itself to win again.
VOLUME_TARGET_EXPIRY = 2.0


class VolumeMixin(_PlayerControllerBase):
    """Mixin class providing the volume and mute commands for PlayerController."""

    @api_command("players/cmd/volume_set", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given player.

        :param player_id: player_id of the player to handle the command.
        :param volume_level: volume level (0..100) to set on the player.
        """
        volume_level = max(0, min(100, volume_level))
        # record the level and invalidate the group volume state up front, before waiting
        # for the volume lock: a command that is still queued would otherwise undo what a
        # command issued after it already recorded.
        # skip for group players since _handle_cmd_volume_set redirects those to
        # set_group_volume which creates/uses the snapshot itself
        if (player := self.get_player(player_id)) and player.type != PlayerType.GROUP:
            self._record_volume_target(player, volume_level)
            self._invalidate_group_volume_snapshot(player_id)
        async with self.get_player_lock(player_id, PlayerLockPurpose.VOLUME):
            await self._handle_cmd_volume_set(player_id, volume_level, record_target=False)

    @api_command("players/cmd/volume_up", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_volume_up(self, player_id: str) -> None:
        """
        Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get_player(player_id)):
            return
        if player.type == PlayerType.GROUP:
            await self.cmd_group_volume_up(player_id)
            return
        current_volume = self._volume_nudge_base(player) or 0
        new_volume = min(100, current_volume + self._get_volume_step(current_volume))
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_volume_down(self, player_id: str) -> None:
        """
        Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get_player(player_id)):
            return
        if player.type == PlayerType.GROUP:
            await self.cmd_group_volume_down(player_id)
            return
        current_volume = self._volume_nudge_base(player) or 0
        new_volume = max(0, current_volume - self._get_volume_step(current_volume))
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_group_volume(
        self,
        player_id: str,
        volume_level: int,
    ) -> None:
        """
        Handle adjusting the overall/group volume to a playergroup (or synced players).

        Will set a new (overall) volume level to a group player or syncgroup.

        :param player_id: Player ID of group player or syncleader to handle the command.
        :param volume_level: Volume level (0..100) to set to the group.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        group_player = self._resolve_group_volume_player(player)
        if group_player is None:
            # treat as normal player volume change
            await self.cmd_volume_set(player_id, volume_level)
            return
        async with self.get_player_lock(group_player.player_id, PlayerLockPurpose.GROUP_VOLUME):
            await self.set_group_volume(group_player, volume_level)

    @api_command("players/cmd/group_volume_up", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_group_volume_up(self, player_id: str) -> None:
        """
        Send VOLUME_UP command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        # step from the volume of the group as a whole, which is not the volume of the
        # addressed player when the command is addressed to one of its synced members
        group_player = self._resolve_group_volume_player(player) or player
        async with self.get_player_lock(group_player.player_id, PlayerLockPurpose.GROUP_VOLUME):
            cur_volume = self._group_volume_nudge_base(group_player)
            if cur_volume is None:
                return
            new_volume = min(100, cur_volume + self._get_volume_step(cur_volume))
            await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_down", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_group_volume_down(self, player_id: str) -> None:
        """
        Send VOLUME_DOWN command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        group_player = self._resolve_group_volume_player(player) or player
        async with self.get_player_lock(group_player.player_id, PlayerLockPurpose.GROUP_VOLUME):
            cur_volume = self._group_volume_nudge_base(group_player)
            if cur_volume is None:
                return
            new_volume = max(0, cur_volume - self._get_volume_step(cur_volume))
            await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_mute", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_group_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Handle muting a playergroup (or synced players) as a whole.

        A group player or syncleader mutes all of its members, a synced player is
        redirected to its syncleader and an ungrouped player is muted on its own.

        :param player_id: Player ID of the player to handle the command.
        :param muted: bool if the group should be muted.
        """
        player = self.get_player(player_id, True)
        assert player is not None  # for type checker
        if player.type == PlayerType.GROUP or player.group_members:
            # dedicated group player or sync leader
            await self._mute_group_members(player, muted)
            return
        if player.synced_to and (sync_leader := self.get_player(player.synced_to)):
            # redirect to sync leader
            await self._mute_group_members(sync_leader, muted)
            return
        # treat as normal player mute
        await self.cmd_volume_mute(player_id, muted)

    @api_command("players/cmd/volume_mute", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(lock=PlayerLockPurpose.VOLUME, switched_off=SwitchedOffAction.IGNORE)
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get_player(player_id, True)
        assert player

        if player.type == PlayerType.GROUP:
            # redirect to special group mute control
            await self.cmd_group_volume_mute(player_id, muted)
            return

        # clearing the mute lock may not depend on mute support, otherwise a lock set
        # while the player still had a mute control would outlive a control change
        if not muted:
            player.extra_data.pop(ATTR_MUTE_LOCK, None)

        mute_control = player.mute_control
        if mute_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(f"Player {player.name} does not support muting")

        # Set mute lock for players in a group
        # This prevents auto-unmute when group volume changes
        had_mute_lock = ATTR_MUTE_LOCK in player.extra_data
        if muted and self._is_in_group(player.state):
            player.extra_data[ATTR_MUTE_LOCK] = True

        try:
            await self._handle_cmd_volume_mute(player, mute_control, muted)
        except Exception:
            # a mute that did not happen may not leave a lock behind, but a lock
            # earned by an earlier successful mute must survive
            if not had_mute_lock:
                player.extra_data.pop(ATTR_MUTE_LOCK, None)
            raise

    def scale_volume_to_device(self, player_id: str, logical_volume: int) -> int:
        """Scale logical volume (0-100) to device volume (min_volume-max_volume)."""
        min_volume, max_volume = self._get_volume_limits(player_id)
        if min_volume == 0 and max_volume == 100:
            return logical_volume
        # Scale: logical 0 -> min_volume, logical 100 -> max_volume
        return min_volume + (logical_volume * (max_volume - min_volume)) // 100

    def scale_volume_from_device(self, player_id: str, device_volume: int) -> int:
        """Scale device volume (min_volume-max_volume) to logical volume (0-100)."""
        min_volume, max_volume = self._get_volume_limits(player_id)
        if min_volume == 0 and max_volume == 100:
            return device_volume
        volume_range = max_volume - min_volume
        if volume_range == 0:
            return 0
        # Scale to 0-100 without clamping so that out-of-range device volumes
        # produce distinct logical values, ensuring state change detection triggers
        # volume limit enforcement
        return ((device_volume - min_volume) * 100) // volume_range

    async def set_group_volume(self, group_player: Player, volume_level: int) -> None:
        """
        Set the overall volume for a player group or synced players.

        Uses interpolation to adjust all child volumes while preserving their
        relative balance. A snapshot of child volumes is cached on first call and
        used as the reference point for subsequent adjustments.

        :param group_player: The group player or sync leader.
        :param volume_level: Target volume level (0..100).
        """
        cur_volume = group_player.group_volume
        if cur_volume is None:
            return

        children: list[Player] = []
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=False
        ):
            if child_player.volume_control == PLAYER_CONTROL_NONE:
                continue
            children.append(child_player)
        if not children:
            return

        # cache a snapshot of child volumes on the group player as reference for interpolation.
        # scaling up: each child interpolates from its snapshot value toward 100.
        # scaling down: each child interpolates from its snapshot value toward 0.
        # this ensures the relative balance is preserved and all children converge
        # to 0 and 100 at the extremes. the snapshot is invalidated when a child's
        # individual volume or the group membership changes, and rebuilt when the
        # children it holds are no longer the ones being adjusted.
        # the levels a nudge steps from are the ones the members were last commanded, so
        # the snapshot has to read the same source, or a change a member has not confirmed
        # yet puts the reference above the level being set and turns a step up into one down
        snapshot: dict[str, int] | None = group_player.extra_data.get(ATTR_GROUP_VOLUME_SNAPSHOT)
        if snapshot is None or snapshot.keys() != {c.player_id for c in children}:
            snapshot = {c.player_id: self._volume_nudge_base(c) or 0 for c in children}
            group_player.extra_data[ATTR_GROUP_VOLUME_SNAPSHOT] = snapshot

        base_group = max(snapshot.values())

        coros = []
        for child_player in children:
            child_base = snapshot.get(child_player.player_id, 0)
            if volume_level >= base_group:
                # scaling up: interpolate each child from snapshot toward 100
                if base_group >= 100:
                    new_child_volume = child_base
                else:
                    progress = (volume_level - base_group) / (100 - base_group)
                    new_child_volume = round(child_base + (100 - child_base) * progress)
            elif base_group == 0:
                new_child_volume = 0
            else:
                # scaling down: interpolate each child from snapshot toward 0
                progress = volume_level / base_group
                new_child_volume = round(child_base * progress)
            new_child_volume = max(0, min(100, new_child_volume))
            coros.append(self._set_member_volume(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

        # notify active AudioSource once at the group level to prevent
        # feedback loops from per-child callbacks with different volume values
        await self._notify_source_volume_change(group_player, volume_level)

    def _get_volume_step(self, current_volume: int) -> int:
        """
        Return the step size for a single volume increment at the given level.

        A configured (non-zero) `volume_step` is a flat step. The default of 0 keeps the
        adaptive ladder, which takes finer steps near the ends of the range.
        """
        if configured := self.get_config_value(CONF_VOLUME_STEP, 0, return_type=int):
            return configured
        if current_volume < 10 or current_volume > 90:
            return 1
        if current_volume < 30 or current_volume > 70:
            return 2
        return 3

    def _get_volume_limits(self, player_id: str) -> tuple[int, int]:
        """Get the configured min/max volume limits for a player."""
        min_volume = int(
            cast(
                "int",
                self.mass.config.get_player_value(
                    player_id, CONF_MIN_VOLUME, CONF_ENTRY_MIN_VOLUME.default_value
                ),
            )
        )
        max_volume = int(
            cast(
                "int",
                self.mass.config.get_player_value(
                    player_id, CONF_MAX_VOLUME, CONF_ENTRY_MAX_VOLUME.default_value
                ),
            )
        )
        return min_volume, max_volume

    def _enforce_volume_limits(self, player: Player) -> bool:
        """
        Clamp device volume to min/max range when changed externally.

        :param player: The player to check the volume of.
        :return: True if the volume was outside the configured range and got corrected.
        """
        player_id = player.player_id
        min_volume, max_volume = self._get_volume_limits(player_id)
        if min_volume == 0 and max_volume == 100:
            return False
        # state.volume_level is the resolved logical volume, available for all
        # volume control types; a device volume outside the configured range
        # surfaces here as a value outside 0-100 (scaling does not clamp)
        logical_volume = player.volume_level
        if logical_volume is None or 0 <= logical_volume <= 100:
            return False
        clamped = max(0, min(100, logical_volume))
        # correct via the regular volume-set path so scaling and redirection apply
        self.mass.create_task(self._handle_cmd_volume_set(player_id, clamped))
        return True

    def _invalidate_group_volume_snapshot(self, player_id: str) -> None:
        """Clear the cached group volume snapshot for all groups this player belongs to."""
        player = self.get_player(player_id)
        if not player:
            return
        if player.group_members:
            player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
        for group_player in self._get_player_groups(player):
            group_player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
        if player.synced_to and (leader := self.get_player(player.synced_to)):
            leader.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)

    def _record_volume_target(self, player: Player, volume_level: int) -> None:
        """Remember the volume level just commanded, as the base for the next nudge."""
        if self._stays_silent_on_volume_change(player):
            volume_level = 0
        player.extra_data[ATTR_VOLUME_TARGET] = (volume_level, time.monotonic())

    def _volume_nudge_base(self, player: Player) -> int | None:
        """Return the volume level a volume nudge for the given player steps from."""
        target = self._unexpired_volume_target(player)
        if target is not None:
            return target
        return player.volume_level

    def _group_volume_nudge_base(self, group_player: Player) -> int | None:
        """Return the volume level a group volume nudge for the given group steps from."""
        if not group_player.group_members:
            # an ungrouped player is stepped through its own volume, so it is that
            # volume the command lands on and that a following nudge steps from
            return self._volume_nudge_base(group_player)
        # mirrors Player.group_volume, but steps from the level last commanded to each
        # member instead of the level it reports, so the group is not held back by a
        # member that has not confirmed the previous nudge yet
        base: int | None = None
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=group_player.type != PlayerType.PLAYER
        ):
            if child_player.volume_control == PLAYER_CONTROL_NONE:
                continue
            if (child_volume := self._volume_nudge_base(child_player)) is None:
                continue
            if base is None or child_volume > base:
                base = child_volume
        return base

    def _unexpired_volume_target(self, player: Player) -> int | None:
        """Return the volume level last commanded, or None once it is too old to trust."""
        if (target := player.extra_data.get(ATTR_VOLUME_TARGET)) is None:
            return None
        volume_level, issued_at = target
        if time.monotonic() - issued_at < VOLUME_TARGET_EXPIRY:
            return cast("int", volume_level)
        del player.extra_data[ATTR_VOLUME_TARGET]
        return None

    def _resolve_group_volume_player(self, player: Player) -> Player | None:
        """
        Return the player whose group a group volume command applies to.

        Returns None if the given player is not grouped at all. Commands addressed to a
        synced member and to its sync leader resolve to the same player, so they read
        and guard one and the same group.

        :param player: The player the command was addressed to.
        """
        # the group volume lock this resolves to may not share the VOLUME purpose:
        # set_group_volume sets the volume of the members concurrently and a sync leader
        # is a member of its own group, so a group command would wait on its own lock.
        if player.type == PlayerType.GROUP or player.group_members:
            # dedicated group player or sync leader
            return player
        if player.synced_to:
            # a synced player follows its sync leader
            return self.get_player(player.synced_to)
        return None

    async def _set_member_volume(self, player_id: str, volume_level: int) -> None:
        """
        Set the volume of a single member as part of a group volume change.

        :param player_id: player_id of the member to handle the command.
        :param volume_level: logical volume level (0..100) to set on the member.
        """
        # record before waiting for the lock, for the same reason as cmd_volume_set
        if member := self.get_player(player_id):
            self._record_volume_target(member, volume_level)
        # take the volume lock of the member itself, so a group volume change and an
        # individual volume command for that member can not overtake one another
        async with self.get_player_lock(player_id, PlayerLockPurpose.VOLUME):
            await self._handle_cmd_volume_set(player_id, volume_level, record_target=False)

    async def _handle_cmd_volume_set(
        self, player_id: str, volume_level: int, *, record_target: bool = True
    ) -> None:
        """
        Handle Player volume set command.

        Skips permission checks and locking (internal use only).

        :param player_id: player_id of the player to handle the command.
        :param volume_level: logical volume level (0..100) to set on the player.
        :param record_target: Set to False when the caller already recorded the level as
            the base for the next volume nudge, before it waited for the volume lock.
        """
        player = self._get_reachable_player(player_id)

        # Clamp logical volume to 0-100
        volume_level = max(0, min(100, volume_level))

        if player.type == PlayerType.GROUP:
            # redirect to special group volume control
            await self.cmd_group_volume(player_id, volume_level)
            return

        # A muted player stays muted: only an explicit unmute lifts it, and the level
        # set here is the one it plays at once that happens. Fake mute is the exception,
        # because it is simulated with the volume itself.
        if self._stays_silent_on_volume_change(player):
            # a locked player stays silent, the volume it holds is the one
            # that gets restored once it is unmuted again
            volume_level = 0
            # the lock may have been earned after the caller recorded the level it asked
            # for, which is then not the level this player ends up at
            record_target = True
        else:
            player.extra_data.pop(ATTR_FAKE_MUTE, None)

        if record_target:
            self._record_volume_target(player, volume_level)

        # Scale logical volume (0-100) to device volume (min_volume-max_volume)
        device_volume = self.scale_volume_to_device(player_id, volume_level)
        # from now on the player shows the level its volume control reports
        player.release_held_volume(PlayerFeature.VOLUME_SET)

        await self._notify_source_volume_change(player, volume_level)

        # Handle native volume control support
        if player.volume_control == PLAYER_CONTROL_NATIVE and (own := player.own_protocol):
            # player supports volume command natively: forward to player
            await own.volume_set(device_volume)
            return
        # Handle fake volume control support
        if player.volume_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake volume control - so we (optimistically) update the state
            # and store the state in the cache. Fake volume uses the logical volume (no scaling).
            player.extra_data[ATTR_FAKE_VOLUME] = volume_level
            player.update_state()
            return
        # player has no volume support at all
        if player.volume_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.name} does not support volume control"
            )
        # handle external player control
        if player_control := self._controls.get(player.volume_control):
            control_name = player_control.name
            self.logger.debug("Redirecting volume command to PlayerControl %s", control_name)
            if not player_control.supports_volume:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            assert player_control.volume_set is not None
            # forward the already-scaled device volume; the external control sets the
            # raw device volume and does not apply min/max scaling of its own
            await player_control.volume_set(device_volume)
            return
        if protocol_player := player.attached_protocols.get(player.volume_control):
            # forward the already-scaled device volume: the limits configured on the
            # player are the only ones that apply to the command
            self.logger.debug(
                "Redirecting volume command to protocol %s",
                protocol_player.provider.manifest.name,
            )
            await protocol_player.volume_set(device_volume)
            return

    @staticmethod
    def _is_in_group(state: PlayerState) -> bool:
        """Check if the player with the given state is currently grouped with other players."""
        # a sync leader has neither synced_to nor active_group set, but it does lead its
        # own group_members, which stays empty for a player that is not grouped at all
        return bool(state.synced_to or state.active_group or state.group_members)

    def _has_active_mute_lock(self, player: Player) -> bool:
        """
        Check if the given player holds a mute lock that still applies to it.

        A lock is only earned inside a group and only holds for as long as the player
        is still grouped, so it can not outlive the group it was earned in.

        :param player: The player to check.
        """
        return bool(player.extra_data.get(ATTR_MUTE_LOCK)) and self._is_in_group(player.state)

    def _stays_silent_on_volume_change(self, player: Player) -> bool:
        """Check if a volume command for the given player lands at 0 to keep it silent."""
        return (
            self._has_active_mute_lock(player)
            and player.mute_control == PLAYER_CONTROL_FAKE
            and bool(player.extra_data.get(ATTR_FAKE_MUTE))
        )

    async def _mute_group_members(self, group_player: Player, muted: bool) -> None:
        """
        Mute or unmute all mute capable members of a player group or synced players.

        :param group_player: The group player or sync leader.
        :param muted: bool if the group should be muted.
        """
        coros = []
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=False
        ):
            if child_player.mute_control == PLAYER_CONTROL_NONE:
                # members without a mute control are left alone, just like the
                # group mute state itself is calculated from the capable members only
                continue
            coros.append(self.cmd_volume_mute(child_player.player_id, muted))
        await asyncio.gather(*coros)

    async def _handle_cmd_volume_mute(self, player: Player, mute_control: str, muted: bool) -> None:
        """
        Send the mute command to the given player's mute control.

        Skips permission checks, locking and mute lock bookkeeping (internal use only).

        :param player: the player to handle the command.
        :param mute_control: the already resolved mute control of the player.
        :param muted: bool if player should be muted.
        """
        # from now on the player shows the mute its mute control reports
        player.release_held_volume(PlayerFeature.VOLUME_MUTE)
        if mute_control == PLAYER_CONTROL_NATIVE and (own := player.own_protocol):
            # player supports mute command natively: forward to player
            await own.volume_mute(muted)
            return
        if mute_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake mute control - so we use volume instead
            self.logger.debug(
                "Using volume for muting for player %s",
                player.name,
            )
            if muted:
                already_muted = bool(player.extra_data.get(ATTR_FAKE_MUTE))
                if not already_muted:
                    # on a repeated mute command the volume is already 0
                    player.extra_data[ATTR_PREVIOUS_VOLUME] = player.volume_level
                await self._handle_cmd_volume_set(player.player_id, 0)
                # set the flag after the volume command, as that clears it
                player.extra_data[ATTR_FAKE_MUTE] = True
                player.update_state()
            else:
                was_muted = bool(player.extra_data.get(ATTR_FAKE_MUTE))
                player.extra_data[ATTR_FAKE_MUTE] = False
                player.update_state()
                if not was_muted:
                    # the volume is the one the user is listening at, restoring
                    # anything here would turn a no-op unmute into a volume change
                    return
                stored_volume: int | None = player.extra_data.pop(ATTR_PREVIOUS_VOLUME, None)
                # the volume was still unknown at mute time, so pick a low volume
                # rather than blasting the speaker at some assumed level
                await self._handle_cmd_volume_set(
                    player.player_id, 1 if stored_volume is None else stored_volume
                )
            return

        # handle external player control
        if player_control := self._controls.get(mute_control):
            control_name = player_control.name
            self.logger.debug("Redirecting mute command to PlayerControl %s", control_name)
            if not player_control.supports_mute:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            assert player_control.mute_set is not None
            await player_control.mute_set(muted)
            return

        # handle a protocol as volume_mute control
        if protocol_player := player.attached_protocols.get(mute_control):
            self.logger.debug(
                "Redirecting mute command to protocol %s",
                protocol_player.provider.manifest.name,
            )
            await protocol_player.volume_mute(muted)
            return

        # the configured control disappeared after the mute control was resolved
        raise UnsupportedFeaturedException(f"Player {player.name} does not support muting")

    async def _hand_over_volume(
        self, player: Player, volume_control: str | None, mute_control: str | None
    ) -> None:
        """
        Give the protocols that take over the volume and mute control of a player its level and mute.

        The level and mute of a player are its own, whichever of its protocols controls
        them: a protocol that takes over starts out with them, before it renders any audio
        of the player. A protocol that does not take them is logged, and what the hand-over
        is part of goes on.

        :param player: The player whose controls move.
        :param volume_control: The volume control that takes over, None when it stays.
        :param mute_control: The mute control that takes over, None when it stays.
        """
        # read both first: what a protocol reports back is published right away
        volume_level, volume_muted = player.volume_level, player.volume_muted
        if (
            volume_control
            and volume_level is not None
            and (protocol := player.get_control_protocol(volume_control))
        ):
            try:
                await protocol.volume_set(
                    self.scale_volume_to_device(player.player_id, volume_level)
                )
            except Exception as err:
                self.logger.warning(
                    "Could not give %s the volume of %s: %s",
                    protocol.protocol_id,
                    player.display_name,
                    err,
                )
        if (
            mute_control
            and volume_muted is not None
            and (protocol := player.get_control_protocol(mute_control))
        ):
            try:
                await protocol.volume_mute(volume_muted)
            except Exception as err:
                self.logger.warning(
                    "Could not give %s the mute of %s: %s",
                    protocol.protocol_id,
                    player.display_name,
                    err,
                )
