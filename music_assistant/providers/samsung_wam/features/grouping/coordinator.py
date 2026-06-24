"""Grouping coordinator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant.providers.samsung_wam.features.base import WamProviderFeatureBase

from .consts import GROUP_SYNC_DEBOUNCE, GROUP_SYNC_TASK_ID

if TYPE_CHECKING:
    from music_assistant.providers.samsung_wam.player import WamPlayer
    from music_assistant.providers.samsung_wam.provider import SamsungWamProvider


class GroupingCoordinator(WamProviderFeatureBase):
    """Manages group membership state across all registered players."""

    def __init__(self, provider: SamsungWamProvider) -> None:
        """
        Initialize the group coordinator.

        :param provider: The SamsungWamProvider instance.
        """
        super().__init__(provider)
        # Ensures only one grouping command runs at a time
        self._lock = asyncio.Lock()

        # leader → children; for looking up a group's members
        self.states: dict[str, set[str]] = {}
        # child → leader; for O(1) lookups of a player's leader
        self._child_to_leader: dict[str, str] = {}

    # --- Lifecycle & Synchronization ---

    def start_sync_task(self) -> None:
        """Schedule the initial state sync task."""
        self.mass.call_later(
            GROUP_SYNC_DEBOUNCE, self.synchronize_states, task_id=GROUP_SYNC_TASK_ID
        )

    def stop_sync_task(self) -> None:
        """Cancel the state sync task."""
        self.mass.cancel_timer(GROUP_SYNC_TASK_ID)
        self.mass.cancel_task(GROUP_SYNC_TASK_ID)

    def synchronize_states(self) -> None:
        """Rebuild internal membership maps from the current state of all registered players."""
        self.states.clear()
        self._child_to_leader.clear()
        for player in self.players:
            synced_to = player.synced_to
            if synced_to:
                self.states.setdefault(synced_to, set()).add(player.player_id)
                self._child_to_leader[player.player_id] = synced_to

    def register_player(self, player: WamPlayer) -> None:
        """
        Register a new player for group tracking.

        :param player: The WamPlayer to register.
        """
        self._update_player_membership(player.player_id, player.synced_to)
        if player.synced_to:
            if leader_player := self.provider.get_player(player.synced_to):
                leader_player.state_sync.refresh_state()

    def unregister_player(self, player: WamPlayer) -> None:
        """
        Unregister a player from group tracking.

        :param player: The WamPlayer to unregister.
        """
        old_leader_id = self._child_to_leader.get(player.player_id)
        self._remove_player_from_all_groups(player.player_id)
        if old_leader_id and (leader_player := self.provider.get_player(old_leader_id)):
            leader_player.state_sync.refresh_state()

    # --- Provider Initiated ---

    async def set_members(
        self,
        leader_id: str,
        adds: list[str] | None = None,
        removes: list[str] | None = None,
    ) -> None:
        """
        Handle group membership changes.

        :param leader_id: The target group leader.
        :param adds: List of member IDs to add.
        :param removes: List of member IDs to remove.
        """
        target_children = self._calculate_target_members(leader_id, adds, removes)
        await self._apply_group(leader_id=leader_id, target_children=target_children)

    def _calculate_target_members(
        self,
        leader_id: str,
        adds: list[str] | None,
        removes: list[str] | None,
    ) -> set[str]:
        """
        Compute the new group membership set based on requested additions and removals.

        :param leader_id: The target group leader.
        :param adds: List of member IDs to add.
        :param removes: List of member IDs to remove.
        :return: A set of player IDs representing the desired target children.
        """
        target_children = set(self.states.get(leader_id, set()))

        if adds:
            target_children.update(adds)
        if removes:
            target_children.difference_update(removes)

        target_children.discard(leader_id)  # A player cannot be its own group member
        return target_children

    async def _apply_group(self, leader_id: str, target_children: set[str]) -> None:
        """
        Apply the desired group membership update for a leader player.

        :param leader_id: The ID of the target group leader.
        :param target_children: The complete, desired set of child members.
        """
        async with self._lock:
            leader = self.provider.get_player(leader_id)
            if not leader:
                return

            children_before = [
                p
                for pid in self.states.get(leader_id, set())
                if (p := self.provider.get_player(pid))
            ]
            children_after = [p for pid in target_children if (p := self.provider.get_player(pid))]
            children_to_remove = set(children_before) - set(children_after)

            # Update state first to prevent stale member lists while the hardware commands run
            for pid in target_children:
                self._update_player_membership(pid, leader_id)
            for child in children_to_remove:
                self._update_player_membership(child.player_id, None)

            try:
                ungroup_tasks = [child.grouping.leave_group() for child in children_to_remove]
                await asyncio.gather(*ungroup_tasks)

                new_members = set(children_after) - set(children_before)
                if new_members:
                    # Adding members requires a full group command to the leader
                    group_name = f"{leader.log_name} Group"
                    await leader.grouping.form_group(children_after, group_name)
                elif not children_after:
                    await leader.grouping.leave_group()
            except Exception as err:
                self.logger.warning("Group operation failed; resyncing state: %s", err)
                self.synchronize_states()
                leader.state_sync.refresh_state()
                raise

    # --- Speaker Initiated ---

    def on_player_state_changed(self, player: WamPlayer) -> None:
        """
        Detect and propagate external group membership changes for a player.

        Handles cases such as a speaker powering on already grouped, or groups
        being changed externally.

        :param player: The WamPlayer whose state changed.
        """
        old_leader_id = self._child_to_leader.get(player.player_id)
        new_leader_id = player.synced_to

        if old_leader_id == new_leader_id:
            return

        affected_leader_ids = {old_leader_id, new_leader_id}
        self._update_player_membership(player.player_id, new_leader_id)

        for pid in affected_leader_ids:
            if pid and pid != player.player_id:
                if p := self.provider.get_player(pid):
                    p.state_sync.refresh_state()

    # --- Internal Helpers ---

    def _update_player_membership(self, player_id: str, new_leader_id: str | None) -> None:
        """
        Update the membership maps for a player's group change.

        :param player_id: The player ID to update.
        :param new_leader_id: The new leader's ID, or None if the player is ungrouped.
        """
        self._remove_player_from_all_groups(player_id)
        if new_leader_id:
            self.states.setdefault(new_leader_id, set()).add(player_id)
            self._child_to_leader[player_id] = new_leader_id

    def _remove_player_from_all_groups(self, player_id: str) -> None:
        """
        Remove a player from all membership maps.

        :param player_id: The player ID to remove.
        """
        if old_leader_id := self._child_to_leader.pop(player_id, None):
            if old_leader_id in self.states:
                self.states[old_leader_id].discard(player_id)
                if not self.states[old_leader_id]:
                    del self.states[old_leader_id]
        if player_id in self.states:
            for child_id in self.states.pop(player_id, set()):
                self._child_to_leader.pop(child_id, None)
