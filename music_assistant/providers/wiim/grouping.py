"""Native multiroom topology coordinator shared by both WiiM backends."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.errors import PlayerCommandFailed
from pywiim import WiiMError
from wiim.exceptions import WiimException

from .constants import BACKEND_GENERIC, BACKEND_OFFICIAL
from .helpers import linkplay_group_compatible, match_slave_uuid_to_player_id

if TYPE_CHECKING:
    from pywiim import WiiMClient
    from pywiim.models import DeviceInfo

    from .linkplay_player import LinkPlayPlayer
    from .player import WiimPlayer
    from .provider import WiimProvider

    # Either backend's player; both expose the small grouping surface used here.
    type NativePlayer = WiimPlayer | LinkPlayPlayer

# A leader's slave list is only re-read this often on the ordinary player poll; forced
# refreshes (grouping commands, RenderingControl slave events, moves) bypass the TTL so a
# real change is never delayed by it.
SLAVE_TTL = 60.0

# A device accepts a grouping command before its role has propagated, so membership is
# polled up to this long before a still-mismatched role is declared a no-op.
VERIFY_MAX_WAIT = 10.0
VERIFY_POLL_INTERVAL = 1.0

# Registration/availability announcements are coalesced into a single IO-free reconcile so
# a burst of them heals discovery-order misses just once.
RECONCILE_DEBOUNCE = 1.0
RECONCILE_TASK_ID = "wiim_native_reconcile"
REPUBLISH_TASK_ID = "wiim_native_republish"

_NATIVE_BACKENDS = (BACKEND_OFFICIAL, BACKEND_GENERIC)


class NativeGroupRole(StrEnum):
    """A player's role in the native multiroom topology."""

    LEADER = "leader"
    FOLLOWER = "follower"
    STANDALONE = "standalone"


class NativeGroupCoordinator:
    """
    The single native topology authority across both WiiM backends.

    It owns which players are leaders, followers or standalone and who belongs to which
    group; the official SDK and the linked protocol players remain the playback/state
    authorities. Topology is rebuilt from the raw slave-uuid lists that leaders report
    (read over a low-level command client) and resolved against the currently registered
    players, so a discovery-order miss self-heals on the next reconcile.
    """

    def __init__(self, provider: WiimProvider) -> None:
        """Initialize the coordinator for a provider instance."""
        self._provider = provider
        self._mass = provider.mass
        self._lock = asyncio.Lock()
        # serializes grouping commands so concurrent moves targeting the same member from
        # different leaders cannot interleave their check/mutate/verify phases
        self._command_lock = asyncio.Lock()
        # leader player_id -> raw slave uuids from its last successful topology read
        self._raw_slaves: dict[str, list[str]] = {}
        self._refreshed_at: dict[str, float] = {}
        # per-leader lock so overlapping refreshes cannot apply out of order
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        # players that report themselves as a follower (of a possibly unknown leader)
        self._self_follower: set[str] = set()
        # atomically rebuilt indexes; readers see a consistent snapshot between awaits
        self._members: dict[str, list[str]] = {}
        self._reverse: dict[str, str] = {}
        self._role: dict[str, NativeGroupRole] = {}

    # --- Topology queries (lock-free, read the last rebuilt snapshot) ---

    def role_of(self, player_id: str) -> NativeGroupRole:
        """Return the native topology role of a player."""
        return self._role.get(player_id, NativeGroupRole.STANDALONE)

    def members_of(self, player_id: str) -> list[str]:
        """
        Return the group member ids for a leader (leader first), else an empty list.

        :param player_id: The player whose managed members to return.
        """
        return list(self._members.get(player_id, ()))

    def leader_of(self, player_id: str) -> str | None:
        """Return the leader a follower belongs to, or None when not a follower."""
        return self._reverse.get(player_id)

    def is_unknown_leader_follower(self, player_id: str) -> bool:
        """
        Return whether a player follows a leader MA has not discovered.

        Such a device belongs to an externally-created group MA cannot see the leader of,
        so it can be neither cleanly detached nor safely regrouped and must have grouping
        withdrawn entirely.

        :param player_id: The player to check.
        """
        return self._is_unknown_leader_follower(player_id)

    def can_group_with(self, player: NativePlayer) -> set[str]:
        """
        Return every reachable peer of either backend this player may group with.

        Core applies the final grouping filter and auto-ungroups a known follower before
        regrouping it. A device whose own native grouping API is unreachable offers no native
        peers (grouping via a linked protocol is decided separately by core), and a follower
        of a leader MA has NOT discovered is excluded (it cannot be cleanly moved). Two generic
        devices are only paired when they share a known, matching router-based multiroom
        generation, so the UI never offers a generic pair that the command path would then
        reject. Cross-backend generation cannot be known without a device read, so those pairs
        stay candidates and fail closed at command time if incompatible.

        :param player: The player requesting its grouping candidates.
        """
        if not player.native_available or self._is_unknown_leader_follower(player.player_id):
            # the requesting device cannot lead a native group right now (its grouping API is
            # unreachable, or it follows a group MA has not discovered and cannot be moved),
            # so it offers no native candidates of its own.
            return set()
        return {
            peer.player_id
            for peer in self._native_players()
            if peer.native_available
            and peer.player_id != player.player_id
            and not self._is_unknown_leader_follower(peer.player_id)
            and self._offerable_pair(player, peer)
        }

    # --- Topology feeders ---

    def set_self_role(self, player_id: str, is_follower: bool) -> bool:
        """
        Record a player's own report of whether it is a follower.

        This is the fallback for a device following a leader MA has not discovered: the
        leader never lists it here, but the device itself knows it is grouped. The
        recorded value only takes effect on the next reconcile.

        :param player_id: The player reporting its own role.
        :param is_follower: Whether the device reports itself as a follower.
        :return: Whether this changed the player's recorded self-follower state.
        """
        if is_follower:
            if player_id in self._self_follower:
                return False
            self._self_follower.add(player_id)
            return True
        if player_id not in self._self_follower:
            return False
        self._self_follower.discard(player_id)
        return True

    def set_leader_slaves(
        self, player_id: str, raw_slave_uuids: list[str], observed_at: float | None = None
    ) -> bool:
        """
        Record a leader's raw slave-uuid list from a topology read.

        :param player_id: The leader that reported the slave list.
        :param raw_slave_uuids: The slave uuids exactly as the device reported them.
        :param observed_at: The monotonic time the read that produced this list *started*.
            The single-owner tie-break ranks claims by this, so a longer-running older read
            that completes late cannot outrank a newer read; defaults to now for a list set
            directly (e.g. cleared on a confirmed join), which is a genuine "now" observation.
        :return: Whether the recorded list differs from the previously cached one.
        """
        changed = self._raw_slaves.get(player_id) != raw_slave_uuids
        self._raw_slaves[player_id] = raw_slave_uuids
        self._refreshed_at[player_id] = time.monotonic() if observed_at is None else observed_at
        return changed

    async def refresh_leader(self, player: NativePlayer, *, force: bool = False) -> bool:
        """
        Re-read a player's live group state over its command client and reconcile.

        A live read yields both the device's own role (the self-follower signal) and, when
        it leads, its slave list. Reads for one player are serialized so overlapping
        refreshes cannot apply an older response after a newer one, and a failed read keeps
        the previous state so a temporarily unreachable device loses neither its members
        nor its self-role.

        :param player: The player whose live group state to read.
        :param force: Read now even if the slow TTL has not elapsed.
        :return: Whether a fresh group state was actually read and applied.
        """
        if not player.native_ip or not player.native_available:
            # no reachable address to command yet (never seen or currently unavailable);
            # keep the cached topology instead of doing a request that will only time out.
            return False
        lock = self._refresh_locks.setdefault(player.player_id, asyncio.Lock())
        async with lock:
            # re-check the TTL inside the lock: a concurrent refresh may have just run
            if not force and (
                time.monotonic() - self._refreshed_at.get(player.player_id, 0.0) < SLAVE_TTL
            ):
                return False
            client = player.make_command_client()
            # stamp the read by when it STARTED, so a longer-running older observation that
            # completes after a newer one cannot win the single-owner tie-break and move a
            # follower back to a stale leader.
            observed_at = time.monotonic()
            try:
                info = await client.get_device_group_info()
                if info.role == "slave":
                    is_follower, raw = True, []
                else:
                    # a non-slave is a leader or solo, but get_device_group_info both
                    # swallows a failed slave-list read (reporting solo) and can report a
                    # master with empty slave uuids when it derived them from status ip
                    # strings; read the authoritative getSlaveList so members resolve and a
                    # transient failure retains the cached topology (handled below).
                    is_follower = False
                    raw = self._normalize_slave_uuids(await client.get_slaves_info())
            except WiiMError as err:
                self._provider.logger.debug(
                    "Failed to read group state for %s: %s", player.player_id, err
                )
                return False
            # the player may have been unregistered/replaced during the await; never apply
            # a stale device response to a different registered instance
            if self._mass.players.get_player(player.player_id) is not player:
                return False
            # reuse the capabilities this read detected so a later fresh command client (the
            # official backend builds one per call) does not re-probe the device
            player.store_command_capabilities(client.capabilities)
            self.set_self_role(player.player_id, is_follower)
            self.set_leader_slaves(player.player_id, raw, observed_at=observed_at)
        await self.reconcile()
        return True

    def unregister(self, player_id: str) -> None:
        """Drop a permanently removed player from the topology cache."""
        self._raw_slaves.pop(player_id, None)
        self._refreshed_at.pop(player_id, None)
        self._refresh_locks.pop(player_id, None)
        self._self_follower.discard(player_id)

    def schedule_reconcile(self) -> None:
        """Schedule a debounced reconcile, coalescing bursts into one rebuild."""
        self._mass.call_later(RECONCILE_DEBOUNCE, self.reconcile, task_id=RECONCILE_TASK_ID)

    def schedule_republish(self) -> None:
        """
        Schedule every native player to re-publish its state, coalescing bursts.

        A player's ``can_group_with`` depends on its peers' native availability and
        compatibility, but a peer's health/metadata transition does not change the topology
        and so is not picked up by :meth:`reconcile`. This forces every native player to
        recompute and re-publish, so no peer keeps a stale candidate set that would offer a
        native command the coordinator then rejects.
        """
        self._mass.call_later(RECONCILE_DEBOUNCE, self._republish_all, task_id=REPUBLISH_TASK_ID)

    async def reconcile(self) -> None:
        """Rebuild the topology indexes from the cached slave lists and notify changes."""
        async with self._lock:
            self._rebuild()

    # --- Grouping commands ---

    async def set_members(
        self,
        leader: NativePlayer,
        player_ids_to_add: list[str] | None,
        player_ids_to_remove: list[str] | None,
    ) -> None:
        """
        Add or remove native group members for a leader, spanning both backends.

        Same-backend official operations keep the SDK path; every other combination joins
        or removes the follower over the low-level LinkPlay client. Every operation is
        verified against the leader's own live slave list (not the follower's role, which
        is unreachable for a legacy Wi-Fi Direct follower) and raises on a no-op.

        :param leader: The player the grouping command was issued on.
        :param player_ids_to_add: Player ids to join to this leader's group.
        :param player_ids_to_remove: Player ids to remove from this leader's group.
        """
        add_ids = player_ids_to_add or []
        remove_ids = player_ids_to_remove or []
        # reject a contradictory request before any work: core can forward the same id in
        # both lists when the parent membership is stale but the child still reports synced_to,
        # and joining then immediately removing it is never the intent.
        if len(set(add_ids)) != len(add_ids) or len(set(remove_ids)) != len(remove_ids):
            raise PlayerCommandFailed(
                f"Duplicate member id in grouping request on {leader.player_id}"
            )
        if conflicting := set(add_ids) & set(remove_ids):
            raise PlayerCommandFailed(
                f"Cannot add and remove the same member on {leader.player_id}: {conflicting}"
            )
        # core only locks the command's own leader, so serialize here: a concurrent move of
        # the same member under a different leader must not interleave with this command's
        # live check, mutation and verification.
        async with self._command_lock, AsyncExitStack() as stack:
            add_members = [self._require_member(mid) for mid in add_ids]
            remove_members = [self._require_member(mid) for mid in remove_ids]
            held: set[str] = set()
            # Phase 1: lock the leader and the explicit targets before reading their live
            # state, so a concurrent address rebuild cannot swap a generic client mid-read
            # and have the command act on a stale-address topology.
            await self._acquire_rebuild_locks(stack, [leader, *add_members, *remove_members], held)
            # authoritative preflight, now under lock: read the leader's and every addition's
            # live group and fail the whole batch if a fresh read cannot be obtained, so a
            # device that externally became a follower (or gained followers) since its last
            # poll is validated on live state, never repurposed from a stale cache.
            for player in (leader, *add_members):
                if not await self.refresh_leader(player, force=True):
                    raise PlayerCommandFailed(f"Cannot read the group state of {player.player_id}")
            # Phase 2: joining a member dissolves the followers that read just revealed, so
            # lock them too before any mutation. The recursive dissolve never re-takes locks.
            followers: list[NativePlayer] = []
            for member in (*add_members, *remove_members):
                for follower_id in self.members_of(member.player_id):
                    if follower_id == member.player_id:
                        continue
                    follower = self._mass.players.get_player(follower_id)
                    if getattr(follower, "linkplay_backend", None) in _NATIVE_BACKENDS:
                        followers.append(cast("NativePlayer", follower))
            await self._acquire_rebuild_locks(stack, followers, held)
            await self._apply_members(leader, add_members, remove_members)

    # --- Private helpers ---

    async def _acquire_rebuild_locks(
        self, stack: AsyncExitStack, players: list[NativePlayer], held: set[str]
    ) -> None:
        """
        Acquire the not-yet-held rebuild locks of the given players in a deterministic order.

        Acquiring in sorted player-id order and skipping already-held devices keeps the
        overall order stable across the command's two locking phases; the command lock
        serializes whole commands, so this can never deadlock against another command, and
        an address rebuild only ever holds a single device's lock.

        :param stack: The exit stack that releases every acquired lock when the command ends.
        :param players: The players whose rebuild locks to acquire.
        :param held: The set of player ids whose locks are already held, updated in place.
        """
        for player in sorted(players, key=lambda candidate: candidate.player_id):
            if player.player_id in held:
                continue
            held.add(player.player_id)
            if (lock := self._rebuild_lock_for(player)) is not None:
                await stack.enter_async_context(lock)

    async def _apply_members(
        self,
        leader: NativePlayer,
        add_members: list[NativePlayer],
        remove_members: list[NativePlayer],
    ) -> None:
        """Validate the whole batch, then run the grouping operations (caller holds locks)."""
        try:
            # full validation before any hardware mutation, so an unknown, unreachable or
            # generation-incompatible target fails the whole request before any speaker has
            # joined or left, rather than leaving a partially mutated group behind.
            self._guard_regroupable(leader)
            # a low-level (generic or mixed) join needs the leader's device info for the
            # compatibility gate and the join itself; read it once for the whole batch and
            # reuse it, rather than re-fetching it for every member.
            leader_info: DeviceInfo | None = None
            if any(not self._is_official_pair(leader, member) for member in add_members):
                leader_info = await self._device_info(leader)
            join_plan: dict[str, tuple[DeviceInfo | None, tuple[NativePlayer, ...]] | None] = {}
            for member in add_members:
                self._guard_regroupable(member)
                join_plan[member.player_id] = await self._prevalidate_join(
                    leader, member, leader_info
                )
            for member in remove_members:
                self._prevalidate_leave(leader, member)
            for member in add_members:
                await self._join(leader, member, join_plan[member.player_id])
            for member in remove_members:
                await self._leave(leader, member)
        finally:
            await self.refresh_leader(leader, force=True)

    def _rebuild_lock_for(self, player: NativePlayer) -> asyncio.Lock | None:
        """Return the per-device rebuild lock that grouping must hold, if the backend has one."""
        return getattr(player, "grouping_rebuild_lock", None)

    def _guard_regroupable(self, player: NativePlayer) -> None:
        """
        Assert a player can take part in an MA-driven grouping command right now.

        A device that follows a leader MA has not discovered belongs to an external group
        that cannot be cleanly detached, so it can be neither a leader nor a member here.
        Re-checked at command time because core may have cached the request (or it may have
        waited on the command lock) since the candidate set was computed.

        :param player: The leader or member to validate.
        """
        if self._is_unknown_leader_follower(player.player_id):
            raise PlayerCommandFailed(
                f"Cannot regroup {player.player_id}: it follows a group MA has not discovered"
            )

    @staticmethod
    def _is_official_pair(leader: NativePlayer, member: NativePlayer) -> bool:
        """Return whether both players are official WiiM devices (the SDK grouping path)."""
        return (
            leader.linkplay_backend == BACKEND_OFFICIAL
            and member.linkplay_backend == BACKEND_OFFICIAL
        )

    async def _prevalidate_join(
        self,
        leader: NativePlayer,
        member: NativePlayer,
        leader_info: DeviceInfo | None,
    ) -> tuple[DeviceInfo | None, tuple[NativePlayer, ...]] | None:
        """
        Validate an addition before any mutation and return the plan the join needs.

        Same-backend official joins are validated by the SDK itself (returns ``None``);
        every other combination must reach both devices and share a known, matching
        router-based multiroom generation. The member's own live group was read before the
        locks were taken, so it is validated here too: it must not still lead any slave MA
        cannot dissolve (it would be orphaned once the member becomes a follower). The
        managed followers to dissolve are captured so execution needs no further read.

        :param leader: The leader the member would join.
        :param member: The member that would join the leader.
        :param leader_info: The leader's device info, read once for the whole batch.
        """
        if self._is_official_pair(leader, member):
            return None
        if not leader.native_ip:
            raise PlayerCommandFailed(f"Cannot group {member.player_id}: leader address unknown")
        member_info = await self._device_info(member)
        if not linkplay_group_compatible(leader_info, member_info):
            raise PlayerCommandFailed(
                f"Cannot group {member.player_id} with {leader.player_id}: incompatible or "
                "legacy Wi-Fi Direct LinkPlay multiroom"
            )
        if self._leads_unmanaged_slave(member):
            raise PlayerCommandFailed(
                f"Cannot group {member.player_id}: it still leads slaves MA cannot dissolve"
            )
        followers = tuple(
            self._require_member(follower_id)
            for follower_id in self.members_of(member.player_id)
            if follower_id != member.player_id
        )
        return leader_info, followers

    def _leads_unmanaged_slave(self, member: NativePlayer) -> bool:
        """Return whether the member's live slave list holds a slave MA cannot resolve."""
        registered = {player.player_id for player in self._native_players()}
        return any(
            match_slave_uuid_to_player_id(uuid, registered) is None
            for uuid in self._raw_slaves.get(member.player_id, ())
        )

    def _prevalidate_leave(self, leader: NativePlayer, member: NativePlayer) -> None:
        """
        Validate a removal before any mutation, so a bad target fails the whole batch.

        Only a member the leader still owns is actually detached; one the freshly refreshed
        leader no longer lists (moved away or absent) is an idempotent skip in :meth:`_leave`,
        so its reachability is irrelevant and must not abort the batch. For a member still
        owned, a same-backend generic follower leaves over its own client, so an unreachable
        one cannot be detached and fails up front rather than half-way through the batch.
        Cross-backend and official removals go through the always-reachable leader (SDK
        ungroup or leader-side kick), so the follower's own reachability is not required.

        :param leader: The leader the member would leave.
        :param member: The member that would leave the leader.
        """
        if member.player_id not in self.members_of(leader.player_id):
            return
        if (
            leader.linkplay_backend == BACKEND_GENERIC
            and member.linkplay_backend == BACKEND_GENERIC
            and not member.native_available
        ):
            raise PlayerCommandFailed(f"{member.player_id} is not reachable to leave its group")

    def _native_players(self) -> list[NativePlayer]:
        """Return this provider's players that belong to either native backend."""
        return [
            cast("NativePlayer", player)
            for player in self._provider.players
            if getattr(player, "linkplay_backend", None) in _NATIVE_BACKENDS
        ]

    def _republish_all(self) -> None:
        """Re-publish the state of every registered native player."""
        for player in self._native_players():
            player.on_native_group_update()

    def _is_unknown_leader_follower(self, player_id: str) -> bool:
        """Return whether a player follows a leader MA has not discovered."""
        return (
            self.role_of(player_id) == NativeGroupRole.FOLLOWER
            and self.leader_of(player_id) is None
        )

    def _offerable_pair(self, player: NativePlayer, peer: NativePlayer) -> bool:
        """
        Return whether two players may be offered as a grouping pair in the UI.

        Two generic devices are only offered when their cached generations are both known,
        router-based and matching; any pair involving an official device is offered and
        validated at command time, where the live device generation is read.

        :param player: The player requesting candidates.
        :param peer: The candidate peer being considered.
        """
        if player.linkplay_backend == BACKEND_GENERIC and peer.linkplay_backend == BACKEND_GENERIC:
            return linkplay_group_compatible(
                getattr(player, "cached_device_info", None),
                getattr(peer, "cached_device_info", None),
            )
        return True

    def _rebuild(self) -> None:
        """Recompute roles and membership, then push state to every changed player."""
        registered: dict[str, NativePlayer] = {
            player.player_id: player for player in self._native_players()
        }
        # a fresh topology read for an unregistered player can never arrive, so its cached
        # slave list (and self-reported role) is stale forever: drop it here.
        for stale_id in [pid for pid in self._raw_slaves if pid not in registered]:
            self.unregister(stale_id)
        for stale_id in [pid for pid in self._self_follower if pid not in registered]:
            self._self_follower.discard(stale_id)

        candidate_members: dict[str, list[str]] = {}
        for leader_id, raw in self._raw_slaves.items():
            resolved = [
                member_id
                for uuid in raw
                if (member_id := match_slave_uuid_to_player_id(uuid, registered)) is not None
                and member_id != leader_id
            ]
            if resolved:
                candidate_members[leader_id] = resolved

        # a follower can be claimed by only one leader: while a moved device's old leader
        # still has it in its (not-yet-refreshed) cached list, the most recently refreshed
        # leader wins so one player never shows in two groups. Ties break on the leader id
        # so the winner is fully deterministic.
        owner: dict[str, str] = {}
        for leader_id, resolved in candidate_members.items():
            leader_key = (self._refreshed_at.get(leader_id, 0.0), leader_id)
            for member_id in resolved:
                incumbent = owner.get(member_id)
                if incumbent is None or leader_key > (
                    self._refreshed_at.get(incumbent, 0.0),
                    incumbent,
                ):
                    owner[member_id] = leader_id

        # a device that is itself another leader's follower cannot also be a leader; its own
        # (stale) slave list is ignored so nested/ghost groups never form.
        members: dict[str, list[str]] = {}
        for leader_id, resolved in candidate_members.items():
            if leader_id in owner:
                continue
            owned = [member_id for member_id in resolved if owner.get(member_id) == leader_id]
            if owned:
                members[leader_id] = [leader_id, *owned]
        reverse: dict[str, str] = {
            member_id: leader_id
            for leader_id, group in members.items()
            for member_id in group
            if member_id != leader_id
        }
        role: dict[str, NativeGroupRole] = {}
        for player_id in registered:
            if player_id in members:
                role[player_id] = NativeGroupRole.LEADER
            elif player_id in reverse:
                role[player_id] = NativeGroupRole.FOLLOWER
            elif player_id in self._self_follower:
                # following a leader MA has not discovered: a follower with no known leader
                # (leader_of stays None), still suppressed and not groupable.
                role[player_id] = NativeGroupRole.FOLLOWER
            else:
                role[player_id] = NativeGroupRole.STANDALONE

        old_members, old_reverse, old_role = self._members, self._reverse, self._role
        old_unknown = {
            player_id
            for player_id in registered
            if old_role.get(player_id) == NativeGroupRole.FOLLOWER and player_id not in old_reverse
        }
        new_unknown = {
            player_id
            for player_id in registered
            if role[player_id] == NativeGroupRole.FOLLOWER and player_id not in reverse
        }
        # a change in which players follow an undiscovered leader flips every peer's
        # can_group_with (those followers are excluded there), so all players must
        # re-publish; otherwise only those whose own role/membership actually changed.
        if old_unknown != new_unknown:
            changed_ids = list(registered)
        else:
            changed_ids = [
                player_id
                for player_id in registered
                if old_role.get(player_id, NativeGroupRole.STANDALONE) != role[player_id]
                or old_members.get(player_id, []) != members.get(player_id, [])
                or old_reverse.get(player_id) != reverse.get(player_id)
            ]
        # atomic swap: readers between awaits always see one consistent snapshot
        self._members, self._reverse, self._role = members, reverse, role
        # notify members-publishers (a leader in the old OR new snapshot) before followers:
        # a follower's synced_to scans its leaders' cached group_members, so an old leader
        # shedding it and a new leader gaining it must both refresh first.
        changed_ids.sort(
            key=lambda player_id: not (old_members.get(player_id) or members.get(player_id))
        )
        for player_id in changed_ids:
            registered[player_id].on_native_group_update()

    def _require_member(self, player_id: str) -> NativePlayer:
        """
        Return the registered native player for a grouping target.

        :param player_id: The member id supplied to the grouping command.
        """
        member = self._mass.players.get_player(player_id)
        if getattr(member, "linkplay_backend", None) not in _NATIVE_BACKENDS:
            raise PlayerCommandFailed(f"Cannot group unknown or unsupported player {player_id}")
        return cast("NativePlayer", member)

    async def _join(
        self,
        leader: NativePlayer,
        member: NativePlayer,
        plan: tuple[DeviceInfo | None, tuple[NativePlayer, ...]] | None,
    ) -> None:
        """Join a member to a leader over the correct backend path and verify it."""
        if self._is_official_pair(leader, member):
            leader_udn = cast("WiimPlayer", leader).native_device_udn
            member_udn = cast("WiimPlayer", member).native_device_udn
            try:
                await self._provider.wiim_controller.async_join_group(leader_udn, [member_udn])
            except WiimException as err:
                raise PlayerCommandFailed(f"Failed to group {member.player_id}: {err}") from err
        else:
            assert plan is not None  # a low-level join is always prevalidated
            await self._low_level_join(leader, member, plan[0], plan[1])
        await self._verify_membership(leader, member, joined=True)
        # the member is now a follower and manages no members of its own; drop any slave
        # list it cached while it was a leader so a later leave cannot resurrect a ghost
        # group, and record its follower role so it stays suppressed even if its leader is
        # unregistered before the member's next live read (whose TTL was just advanced).
        self.set_leader_slaves(member.player_id, [])
        self.set_self_role(member.player_id, True)

    async def _low_level_join(
        self,
        leader: NativePlayer,
        member: NativePlayer,
        leader_info: DeviceInfo | None,
        followers: tuple[NativePlayer, ...],
    ) -> None:
        """Join a member to a leader over the low-level LinkPlay client (generic or mixed)."""
        if not (leader_ip := leader.native_ip):
            raise PlayerCommandFailed(f"Cannot group {member.player_id}: leader address unknown")
        # the low-level join_slave (unlike the official SDK) does not disband a member that
        # leads its own group, so dissolve the followers prevalidation captured from its live
        # group first: otherwise they are orphaned once the member becomes a follower itself.
        if followers:
            await self._apply_members(member, [], list(followers))
        try:
            await member.make_command_client().join_slave(leader_ip, master_device_info=leader_info)
        except WiiMError as err:
            raise PlayerCommandFailed(f"Failed to group {member.player_id}: {err}") from err

    async def _leave(self, leader: NativePlayer, member: NativePlayer) -> None:
        """Remove a member from a leader over the correct backend path and verify it."""
        # confirm, from the leader's own live slave list, that the member is currently this
        # leader's follower. A member that is solo or grouped under a different leader is not
        # detached (idempotent), so another leader's follower is safe.
        if not await self.refresh_leader(leader, force=True):
            raise PlayerCommandFailed(
                f"Could not read the group state of leader {leader.player_id}"
            )
        if member.player_id not in self.members_of(leader.player_id):
            return
        if (
            leader.linkplay_backend == BACKEND_OFFICIAL
            and member.linkplay_backend == BACKEND_OFFICIAL
        ):
            await self._official_detach(leader, member)
        elif (
            leader.linkplay_backend == BACKEND_GENERIC
            and member.linkplay_backend == BACKEND_GENERIC
        ):
            # a same-backend generic follower always sits on the router network, so it can
            # leave itself.
            try:
                await member.make_command_client().leave_group()
            except WiiMError as err:
                raise PlayerCommandFailed(f"Failed to ungroup {member.player_id}: {err}") from err
        else:
            # a cross-backend follower is kicked from the leader, which is always reachable
            # and knows its address even on a private Wi-Fi Direct network.
            await self._leader_side_detach(leader, member)
        await self._verify_membership(leader, member, joined=False)
        # the member is confirmed no longer this leader's follower; clear any stale
        # self-follower flag from its last live read so its state and grouping unblock now
        # instead of on its next poll (a concurrently discovered new leader still wins,
        # because the reverse index outranks the self-follower flag on reconcile).
        if self.set_self_role(member.player_id, False):
            await self.reconcile()

    async def _official_detach(self, leader: NativePlayer, member: NativePlayer) -> None:
        """Ungroup an official follower via the SDK, falling back to a leader-side kick."""
        member_udn = cast("WiimPlayer", member).native_device_udn
        try:
            await self._provider.wiim_controller.async_ungroup_device(member_udn)
        except WiimException as err:
            raise PlayerCommandFailed(f"Failed to ungroup {member.player_id}: {err}") from err
        # the SDK ungroups from its own managed cache and can silently no-op on a
        # recovered/external group; if the leader still lists the follower, kick it from the
        # leader (as the mixed path does).
        if await self.refresh_leader(leader, force=True) and member.player_id in self.members_of(
            leader.player_id
        ):
            await self._leader_side_detach(leader, member)

    async def _leader_side_detach(self, leader: NativePlayer, member: NativePlayer) -> None:
        """
        Remove a follower by kicking it from the leader.

        The leader is always reachable and knows the follower's address (even the private
        Wi-Fi Direct one), so kicking from the leader works where a leave sent to the
        follower's own LAN address would not.

        :param leader: The leader the follower belongs to.
        :param member: The follower to remove.
        """
        client = leader.make_command_client()
        if not (slave_ip := await self._leader_slave_ip(client, member)):
            raise PlayerCommandFailed(
                f"Could not resolve {member.player_id}'s address on leader {leader.player_id}"
            )
        # a freshly built command client has no cached group role, so prime it as master
        # (state-only, no request) to satisfy kick_slave's master guard; membership was
        # already confirmed from the leader's live slave list above.
        await client.create_group()
        try:
            await client.kick_slave(slave_ip)
        except WiiMError as err:
            raise PlayerCommandFailed(f"Failed to ungroup {member.player_id}: {err}") from err

    async def _leader_slave_ip(self, client: WiiMClient, member: NativePlayer) -> str | None:
        """
        Resolve a follower's address from the leader's own slave list.

        Returns ``None`` only when the list is read but does not (yet) contain the member;
        a failed read raises a chained typed error rather than masquerading as "no address".
        """
        try:
            slaves = await client.get_slaves_info()
        except WiiMError as err:
            raise PlayerCommandFailed(
                f"Failed to read the leader's slave list while resolving {member.player_id}: {err}"
            ) from err
        for slave in slaves:
            if (
                isinstance(slave, dict)
                and match_slave_uuid_to_player_id(slave.get("uuid"), (member.player_id,))
                == member.player_id
            ):
                ip = slave.get("ip")
                return ip if isinstance(ip, str) and ip else None
        return None

    @staticmethod
    def _normalize_slave_uuids(slaves_info: list[dict[str, Any]]) -> list[str]:
        """Extract the slave uuids from a raw getSlaveList response (addressable entries)."""
        return [
            (slave.get("uuid") or "").replace("uuid:", "")
            for slave in slaves_info
            if isinstance(slave, dict) and slave.get("ip")
        ]

    async def _verify_membership(
        self, leader: NativePlayer, member: NativePlayer, *, joined: bool
    ) -> None:
        """
        Confirm a grouping op took effect against the leader's own live slave list.

        Verification is leader-side because a legacy Wi-Fi Direct follower moves onto the
        leader's private network and is unreachable from the LAN, while the leader always
        reports its slaves. A device accepts a grouping command before its role has
        propagated, so the leader's list is polled until it matches (or a bounded timeout
        elapses); a failed read fails closed.

        :param leader: The leader the command targeted.
        :param member: The player whose membership was expected to change.
        :param joined: Whether the member was expected to join (True) or leave.
        """
        deadline = time.monotonic() + VERIFY_MAX_WAIT
        while True:
            if not await self.refresh_leader(leader, force=True):
                raise PlayerCommandFailed(
                    f"Could not confirm grouping of {member.player_id}: "
                    f"no fresh topology for leader {leader.player_id}"
                )
            if (member.player_id in self.members_of(leader.player_id)) == joined:
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(VERIFY_POLL_INTERVAL)
        verb = "join" if joined else "leave"
        raise PlayerCommandFailed(
            f"{member.player_id} did not {verb} the group led by {leader.player_id}"
        )

    async def _device_info(self, player: NativePlayer) -> DeviceInfo:
        """
        Read a player's device info for the compatibility gate.

        A failed read raises a chained typed error rather than being collapsed into a
        "None" that the compatibility gate would then misreport as an incompatible or
        legacy device, hiding the real API failure from the caller.

        :param player: The player whose device info to read.
        """
        try:
            return await player.make_command_client().get_device_info_model()
        except WiiMError as err:
            raise PlayerCommandFailed(
                f"Could not read device info for {player.player_id}: {err}"
            ) from err
