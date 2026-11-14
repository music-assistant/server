"""
Group Commands Mixin for the Player Controller.

The entries of a group change (group, ungroup, set members, the group players
themselves) and a player leaving its group when it can no longer be part of it.

This module provides the GroupCommandsMixin class which is inherited by
PlayerController to add the grouping commands.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import PlaybackState, PlayerType, ProviderFeature
from music_assistant_models.errors import (
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import ATTR_AVAILABLE, ATTR_ENABLED, ATTR_POWERED
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
    system_auth_context,
)
from music_assistant.helpers.api import api_command
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from .base import _PlayerControllerBase
from .constants import UNGROUP_ON_POWER_OFF_TYPES, PlayerLockPurpose, SwitchedOffAction
from .grouping import apply_group_change, grouping_snapshot, route_group_change
from .grouping_plan import ChangeKind, GroupChange, elect_ad_hoc_leader, set_members_group
from .helpers import handle_player_command

if TYPE_CHECKING:
    from music_assistant.models.player_protocol import PlayerProtocol


# How long an MA power off command claims the power off the player reports back. Covers
# the stop the command waits on and a power control that reports the new state some
# time later.
POWER_OFF_IN_PROGRESS_EXPIRY = 30.0


def _member_ids(player_ids: list[str] | None) -> tuple[str, ...] | None:
    """
    Return the players a member change names, keeping whether they were given at all.

    :param player_ids: The player ids a command was given, None when not given.
    """
    return tuple(player_ids) if player_ids is not None else None


class GroupCommandsMixin(_PlayerControllerBase):
    """Mixin class providing the grouping commands for PlayerController."""

    async def cmd_set_members(
        self,
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Join/unjoin given player(s) to/from target player.

        Will add the given player(s) to the target player (sync leader or group player).
        A player to add that is switched off at its external power control is powered on,
        and added once its device is online.
        Does not check the current user's player access, see check_grouping_access.

        :param target_player: player_id of the syncgroup leader or group player.
        :param player_ids_to_add: List of player_id's to add to the target player.
        :param player_ids_to_remove: List of player_id's to remove from the target player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerUnavailableError: if the target player is not available.
        """
        parent_player = self._get_reachable_player(target_player)
        # the members are changed under the current ids of their players
        if player_ids_to_add is not None:
            player_ids_to_add = [self._command_player_id(pid) for pid in player_ids_to_add]
        if player_ids_to_remove is not None:
            player_ids_to_remove = [self._command_player_id(pid) for pid in player_ids_to_remove]
        # refused, passed on to the group player that holds the target, or the target is
        # released from the player it is synced to first
        if await route_group_change(
            self,
            GroupChange(
                kind=ChangeKind.ROUTE,
                target_id=parent_player.player_id,
                add=_member_ids(player_ids_to_add),
                remove=_member_ids(player_ids_to_remove),
            ),
        ):
            return

        # no lock while a device comes online: such a member is left out of this change
        for member_id in player_ids_to_add or []:
            member = self.get_player(member_id)
            if member and member.available and member.offline_by_power_control:
                self._power_on_member(parent_player, member, added=True)
        # a member that is removed before its device is online does not join later
        for member_id in player_ids_to_remove or []:
            self._cancel_member_join(parent_player, member_id)

        # Use lock for playback commands to prevent protocol switches from
        # racing with concurrent play_media / play_index / resume calls.
        # A target whose group leaves member changes to it (a static group) is changed under
        # that group's lock too: moving its output to another protocol resumes the group.
        async with self.get_group_and_player_lock(parent_player.player_id):
            await self._handle_set_members(parent_player, player_ids_to_add, player_ids_to_remove)

    def check_grouping_access(
        self,
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Check that the current user may make the given cmd_set_members request.

        Call this before cmd_set_members for a request that comes from a client.

        Adding players needs access to the target player and to every player added.
        Removing players needs access to the target player or to every player removed, so a
        user can always take a player of their own out of a group. When the target player is
        a member of a group player, removing needs access to that group player as well, or to
        every player removed. A request that adds nothing needs access to the target player
        when it is empty, or when the target player is synced to another player (as it is
        detached from that player first).

        :param target_player: player_id of the syncgroup leader or group player.
        :param player_ids_to_add: List of player_id's to add to the target player.
        :param player_ids_to_remove: List of player_id's to remove from the target player.
        :raises InsufficientPermissions: if the current user may not make the request.
        """
        if (current_user := get_current_user()) is None:
            return
        target = self.get_player(target_player)
        group_id = set_members_group(grouping_snapshot(self), target_player) if target else None
        if player_ids_to_add:
            # a group player the command is passed on to only gains players the user may use
            for player_id in (target_player, *player_ids_to_add):
                self.check_player_access(player_id)
        elif not player_ids_to_remove or (target and target.synced_to and not group_id):
            # cmd_set_members detaches a synced target first, unless it passes the
            # command on to the target's group player
            self.check_player_access(target_player)
        if not player_ids_to_remove:
            return
        owners = [target_player, group_id] if group_id else [target_player]
        if not all(has_player_access(current_user, pid, self.get_player(pid)) for pid in owners):
            for player_id in player_ids_to_remove:
                self.check_player_access(player_id)

    @api_command("players/cmd/group", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command
    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """
        Handle GROUP command for given player.

        Join/add the given player(id) to the given (leader) player/sync group.
        If the target player itself is already synced to another player, this may fail.
        If the player can not be synced with the given target player, this may fail.

        NOTE: This is a convenience helper for cmd_set_members.

        :param player_id: player_id of the player to handle the command.
        :param target_player: player_id of the syncgroup leader or group player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerCommandFailed: if the target player is already synced to another player.
        :raises PlayerUnavailableError: if the target player is not available.
        :raises PlayerCommandFailed: if the player is already grouped to another player.
        :raises InsufficientPermissions: if the user may not use the player or target player.
        """
        self.check_grouping_access(target_player, player_ids_to_add=[player_id])
        await self.cmd_set_members(target_player, player_ids_to_add=[player_id])

    @api_command("players/cmd/group_many", required_scope=Scope.PLAYERS_CONTROL)
    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """
        Join given player(s) to target player.

        Will add the given player(s) to the target player (sync leader or group player).
        This is a (deprecated) alias for cmd_set_members.

        :raises InsufficientPermissions: if the user may not use the target player or a child.
        """
        self.check_grouping_access(target_player, player_ids_to_add=child_player_ids)
        await self.cmd_set_members(target_player, player_ids_to_add=child_player_ids)

    @api_command("players/cmd/ungroup", required_scope=Scope.PLAYERS_CONTROL)
    @handle_player_command(switched_off=SwitchedOffAction.IGNORE)
    async def cmd_ungroup(self, player_id: str) -> None:
        """
        Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is synced to.
        If the player is not currently grouped to any other player,
        this will silently be ignored.
        """
        await apply_group_change(self, GroupChange(kind=ChangeKind.UNGROUP, target_id=player_id))

    @api_command("players/cmd/ungroup_many", required_scope=Scope.PLAYERS_CONTROL)
    async def cmd_ungroup_many(self, player_ids: list[str]) -> None:
        """Handle UNGROUP command for all the given players."""
        for player_id in list(player_ids):
            with suppress(PlayerUnavailableError):
                await self.cmd_ungroup(player_id)

    @api_command("players/create_group_player", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def create_group_player(
        self, provider: str, name: str, members: list[str], dynamic: bool = True
    ) -> Player:
        """
        Create a new (permanent) Group Player.

        :param provider: The provider (id) to create the group player for.
        :param name: Name of the new group player.
        :param members: List of player ids to add to the group.
        :param dynamic: Whether the group is dynamic (members can change).
        """
        if not (provider_instance := self.mass.get_provider(provider)):
            raise ProviderUnavailableError(f"Provider {provider} not found")
        provider_instance = cast("PlayerProvider", provider_instance)
        if ProviderFeature.CREATE_GROUP_PLAYER not in provider_instance.supported_features:
            raise UnsupportedFeaturedException(
                f"Provider {provider} does not support creating group players"
            )
        return await provider_instance.create_group_player(name, members, dynamic)

    @api_command("players/remove_group_player", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def remove_group_player(self, player_id: str) -> None:
        """
        Remove a group player for good.

        :param player_id: ID of the group player to remove.
        """
        if (player := self.get_player(player_id)) and player.type != PlayerType.GROUP:
            raise UnsupportedFeaturedException(f"Player {player.name} is not a group player")
        await self.remove(player_id)

    def _handle_membership_cleanup_on_state_change(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Detach a player from its (sync)groups when a state change requires it."""
        # A player that became unavailable or disabled can no longer be commanded,
        # so we drop it from its parent group/leader directly. The same goes for a player
        # that is offline because it is switched off at its power control, which is still
        # reported as available.
        became_inactive = (
            (ATTR_AVAILABLE in changed_values and changed_values[ATTR_AVAILABLE][1] is False)
            or (ATTR_ENABLED in changed_values and changed_values[ATTR_ENABLED][1] is False)
            or player.offline_by_power_control
        )
        if became_inactive and (player.active_group or player.synced_to):
            self.mass.create_task(self._cleanup_player_memberships(player.player_id))

        # A player whose power was turned off outside of an MA power command (e.g. its
        # linked power control was switched off directly) must be unsynced too. We act
        # only on an explicit on->off transition, leaving players without power control
        # (powered == None) untouched. The player is still reachable here, so we route
        # through cmd_ungroup, which also hands over the playback of a sync leader that
        # plays its own queue.
        if changed_values.get(ATTR_POWERED) != (True, False):
            return
        # An MA power command ungroups the player itself before it flips the power, and a
        # provider may keep listing the followers on a leader for a while after that
        # (Sonos does), so the transition it causes would ungroup a second time. The
        # marker expires: a command whose device never reported off must not claim the
        # next external power off.
        issued_at = player.power_off_issued_at
        player.set_power_off_issued_at(None)
        if issued_at is not None and time.monotonic() - issued_at < POWER_OFF_IN_PROGRESS_EXPIRY:
            return
        if player.offline_by_power_control:
            # it went offline along with its power, so it was detached above instead
            return
        if player.type in UNGROUP_ON_POWER_OFF_TYPES and (
            player.synced_to or player.active_group or player.group_members
        ):
            self.mass.create_task(self.cmd_ungroup(player.player_id))

    async def _cleanup_player_memberships(self, player_id: str) -> None:
        """Ensure a player is detached from any groups or syncgroups."""
        await apply_group_change(self, GroupChange(kind=ChangeKind.LEAVE, target_id=player_id))

    async def _detach_member(self, member: Player) -> None:
        """
        Remove a sync member from the player it is synced to.

        Skips permission checks and locking (internal use only).

        :param member: The sync member to detach.
        """
        await apply_group_change(
            self, GroupChange(kind=ChangeKind.DETACH, target_id=member.player_id), member
        )

    @api_command("players/cmd/set_members", required_scope=Scope.PLAYERS_CONTROL)
    async def _api_cmd_set_members(
        self,
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """
        Join/unjoin given player(s) to/from target player.

        Will add the given player(s) to the target player (sync leader or group player).

        :param target_player: player_id of the syncgroup leader or group player.
        :param player_ids_to_add: List of player_id's to add to the target player.
        :param player_ids_to_remove: List of player_id's to remove from the target player.

        :raises UnsupportedFeaturedException: if the target player does not support grouping.
        :raises PlayerUnavailableError: if the target player is not available.
        :raises InsufficientPermissions: if the user may not use a player whose grouping changes.
        """
        self.check_grouping_access(target_player, player_ids_to_add, player_ids_to_remove)
        await self.cmd_set_members(target_player, player_ids_to_add, player_ids_to_remove)

    async def _handle_set_members(
        self,
        parent_player: Player,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
        *,
        new_content: bool = False,
    ) -> tuple[PlayerProtocol, object] | None:
        """
        Handle the actual set_members logic.

        Skips permission checks and locking (internal use only).

        :param parent_player: The parent player to add/remove members to/from.
        :param player_ids_to_add: List of player_id's to add to the parent player.
        :param player_ids_to_remove: List of player_id's to remove from the parent player.
        :param new_content: Set when the caller is about to publish new content, so the
            protocol owner can clear its stale metadata before the member joins.
        :return: The opened takeover transaction as a ``(owner, token)`` pair, or None
            when there is nothing to finish. On success the caller owns the returned
            transaction and must call ``on_group_content_takeover_finished`` or
            ``on_group_content_takeover_aborted`` on it; a raised call is already aborted.
        """
        return await apply_group_change(
            self,
            GroupChange(
                kind=ChangeKind.MEMBERS,
                target_id=parent_player.player_id,
                add=_member_ids(player_ids_to_add),
                remove=_member_ids(player_ids_to_remove),
                new_content=new_content,
            ),
            parent_player,
        )

    async def _transfer_ad_hoc_leadership(
        self, leader: Player, remaining_members: list[str]
    ) -> None:
        """
        Transfer leadership of an ad-hoc sync group to a remaining member.

        Called when the sync leader of an ad-hoc group is unjoined while other
        members remain and playback is active. The queue is moved to a newly
        selected leader, the remaining members are regrouped under it and playback
        resumes at the saved position (accepting a brief audio gap).

        :param leader: The current sync leader being removed from the group.
        :param remaining_members: Available group members (excluding the leader)
            that should keep playing under a new leader.
        """
        active_queue = self.get_active_queue(leader)
        was_playing = active_queue is not None and active_queue.state == PlaybackState.PLAYING
        new_leader_id = elect_ad_hoc_leader(
            grouping_snapshot(self), leader.player_id, remaining_members
        )
        self.logger.info(
            "Transferring leadership of %s to %s (%s remaining member(s))",
            leader.name,
            new_leader_id,
            len(remaining_members),
        )
        # Move the queue to the new leader. transfer_queue frees the new leader from
        # the old leader's group and stops the old leader; the playback position
        # survives because stop() stores it in resume_pos.
        # The user's permission was checked on the player they acted on; the new leader
        # is the server's choice, so these steps run as the server.
        with system_auth_context():
            await self.mass.player_queues.transfer_queue(
                leader.player_id, new_leader_id, auto_play=False
            )
            # regroup the other remaining members under the new leader
            other_members = [m for m in remaining_members if m != new_leader_id]
            if other_members:
                await self.cmd_set_members(new_leader_id, player_ids_to_add=other_members)
            if was_playing:
                await self.mass.player_queues.resume(new_leader_id)

    async def _release_group_session(self, group_id: str) -> None:
        """
        Release the session of a group player: power it off, or stop it without power control.

        Skips permission checks (internal use only).

        :param group_id: player_id of the group player.
        :raises PlayerUnavailableError: When the group player is not available.
        """
        group = self.get_player(group_id, raise_unavailable=True)
        assert group is not None  # for type checking
        async with self.get_player_lock(group.player_id, PlayerLockPurpose.PLAYBACK):
            if group.power_control != PLAYER_CONTROL_NONE:
                await self._handle_cmd_power(group.player_id, False)
            else:
                await self._handle_cmd_stop(group.player_id)
