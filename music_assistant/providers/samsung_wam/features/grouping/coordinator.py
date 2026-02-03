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
        """Initialize the group coordinator.

        :param provider: The SamsungWamProvider instance.
        """
        super().__init__(provider)
        # Lock to ensure only one grouping command is processed at a time.
        self._lock = asyncio.Lock()

        # Primary Map (Leader -> Children): Used to quickly get all members of a specific group.
        self.states: dict[str, set[str]] = {}
        # Reverse Lookup Map (Child -> Leader): Used for O(1) lookups to see if a player is grouped.
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
        """Rebuild internal maps entirely from the current reality of all physical players."""
        self.states.clear()
        self._child_to_leader.clear()
        for player in self.players.values():
            synced_to = getattr(player, "synced_to_internal", None)
            if synced_to:
                self.states.setdefault(synced_to, set()).add(player.player_id)
                self._child_to_leader[player.player_id] = synced_to

    def register_player(self, player: WamPlayer) -> None:
        """Register a new player for group tracking.

        :param player: The WamPlayer to register.
        """
        self._update_player_membership(player.player_id, player.synced_to)
        if player.synced_to:
            if leader_player := self.players.get(player.synced_to):
                leader_player.update_state()

    def unregister_player(self, player: WamPlayer) -> None:
        """Unregister a player from group tracking.

        :param player: The WamPlayer to unregister.
        """
        self._remove_player_from_all_groups(player.player_id)

    # --- MA Initiated ---

    async def set_members(
        self,
        leader_id: str,
        adds: list[str] | None = None,
        removes: list[str] | None = None,
    ) -> None:
        """Handle group membership changes.

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
        """Compute the new group membership set based on requested additions and removals.

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

        target_children.discard(leader_id)  # Sanity check: leader cannot be its own child
        return target_children

    async def _apply_group(self, leader_id: str, target_children: set[str]) -> None:
        """Apply absolute group membership changes.

        :param leader_id: The ID of the target group leader.
        :param target_children: The complete, desired set of child members.
        """
        async with self._lock:
            leader = self.players.get(leader_id)
            if not leader:
                return

            children_before = [
                p for pid in self.states.get(leader_id, set()) if (p := self.players.get(pid))
            ]
            children_after = [p for pid in target_children if (p := self.players.get(pid))]
            children_to_remove = set(children_before) - set(children_after)

            ungroup_tasks = [child.grouping.leave_group() for child in children_to_remove]
            await asyncio.gather(*ungroup_tasks)

            if children_after:
                group_name = f"{leader.log_name} Group"
                await leader.grouping.form_group(children_after, group_name)
            else:
                await leader.grouping.leave_group()

    # --- Hardware Initiated ---

    def on_player_state_changed(self, player: WamPlayer) -> None:
        """Process a state broadcast from a speaker and detect external grouping changes.

        This handles scenarios where a speaker boots up into a previous group,
        or a user changes groups via the official Samsung WAM mobile app.

        :param player: The WamPlayer whose state changed.
        """
        old_leader_id = self._child_to_leader.get(player.player_id)
        new_leader_id = getattr(player, "synced_to_internal", None)

        if old_leader_id == new_leader_id:
            return

        affected_leader_ids = {old_leader_id, new_leader_id}
        self._update_player_membership(player.player_id, new_leader_id)

        for pid in affected_leader_ids:
            if pid and pid != player.player_id:
                if p := self.players.get(pid):
                    p.update_state()

    # --- Internal Helpers ---

    def _update_player_membership(self, player_id: str, new_leader_id: str | None) -> None:
        """Update the dual-maps to reflect a player's new group status.

        :param player_id: The child player ID.
        :param new_leader_id: The new leader's ID (or None if ungrouped).
        """
        self._remove_player_from_all_groups(player_id)
        if new_leader_id:
            self.states.setdefault(new_leader_id, set()).add(player_id)
            self._child_to_leader[player_id] = new_leader_id

    def _remove_player_from_all_groups(self, player_id: str) -> None:
        """Scrub a player from the dual-maps completely.

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
