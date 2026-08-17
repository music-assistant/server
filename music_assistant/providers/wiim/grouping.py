"""Native multiroom topology coordinator shared by both WiiM backends."""

from __future__ import annotations

import asyncio
import time
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

    def can_group_with(self, player: NativePlayer) -> set[str]:
        """
        Return every reachable peer of either backend this player may group with.

        Core applies the final grouping filter and auto-ungroups a known follower before
        regrouping it, so only a follower of a leader MA has NOT discovered is excluded
        here (it cannot be cleanly moved); every other reachable peer is a candidate.

        :param player: The player requesting its grouping candidates.
        """
        if self._is_unknown_leader_follower(player.player_id):
            # this device follows a group MA has not discovered: it cannot be cleanly
            # detached, so it must not be offered any grouping candidates of its own.
            return set()
        return {
            peer.player_id
            for peer in self._native_players()
            if peer.native_available
            and peer.player_id != player.player_id
            and not self._is_unknown_leader_follower(peer.player_id)
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

    def set_leader_slaves(self, player_id: str, raw_slave_uuids: list[str]) -> bool:
        """
        Record a leader's raw slave-uuid list from a topology read.

        :param player_id: The leader that reported the slave list.
        :param raw_slave_uuids: The slave uuids exactly as the device reported them.
        :return: Whether the recorded list differs from the previously cached one.
        """
        changed = self._raw_slaves.get(player_id) != raw_slave_uuids
        self._raw_slaves[player_id] = raw_slave_uuids
        self._refreshed_at[player_id] = time.monotonic()
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
        if not player.native_ip:
            # no address to command yet (never seen or currently unavailable)
            return False
        lock = self._refresh_locks.setdefault(player.player_id, asyncio.Lock())
        async with lock:
            # re-check the TTL inside the lock: a concurrent refresh may have just run
            if not force and (
                time.monotonic() - self._refreshed_at.get(player.player_id, 0.0) < SLAVE_TTL
            ):
                return False
            client = player.make_command_client()
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
            self.set_leader_slaves(player.player_id, raw)
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
        # core only locks the command's own leader, so serialize here: a concurrent move of
        # the same member under a different leader must not interleave with this command's
        # live check, mutation and verification.
        async with self._command_lock:
            await self._apply_members(leader, player_ids_to_add, player_ids_to_remove)

    # --- Private helpers ---

    async def _apply_members(
        self,
        leader: NativePlayer,
        player_ids_to_add: list[str] | None,
        player_ids_to_remove: list[str] | None,
    ) -> None:
        """Run the add/remove grouping operations for a leader (caller holds the lock)."""
        try:
            for member_id in player_ids_to_add or []:
                await self._join(leader, self._require_member(member_id))
            for member_id in player_ids_to_remove or []:
                await self._leave(leader, self._require_member(member_id))
        finally:
            await self.refresh_leader(leader, force=True)

    def _native_players(self) -> list[NativePlayer]:
        """Return this provider's players that belong to either native backend."""
        return [
            cast("NativePlayer", player)
            for player in self._provider.players
            if getattr(player, "linkplay_backend", None) in _NATIVE_BACKENDS
        ]

    def _is_unknown_leader_follower(self, player_id: str) -> bool:
        """Return whether a player follows a leader MA has not discovered."""
        return (
            self.role_of(player_id) == NativeGroupRole.FOLLOWER
            and self.leader_of(player_id) is None
        )

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

    async def _join(self, leader: NativePlayer, member: NativePlayer) -> None:
        """Join a member to a leader over the correct backend path and verify it."""
        if self._is_unknown_leader_follower(member.player_id):
            # re-check at command time: another player's cached can_group_with may still
            # list this device after a live refresh flipped only its internal role. It
            # follows a group MA has not discovered and cannot be cleanly detached first.
            raise PlayerCommandFailed(
                f"Cannot group {member.player_id}: it follows a group MA has not discovered"
            )
        if (
            leader.linkplay_backend == BACKEND_OFFICIAL
            and member.linkplay_backend == BACKEND_OFFICIAL
        ):
            leader_udn = cast("WiimPlayer", leader).native_device_udn
            member_udn = cast("WiimPlayer", member).native_device_udn
            try:
                await self._provider.wiim_controller.async_join_group(leader_udn, [member_udn])
            except WiimException as err:
                raise PlayerCommandFailed(f"Failed to group {member.player_id}: {err}") from err
        else:
            await self._low_level_join(leader, member)
        await self._verify_membership(leader, member, joined=True)
        # the member is now a follower and manages no members of its own; drop any slave
        # list it cached while it was a leader so a later leave cannot resurrect a ghost
        # group, and record its follower role so it stays suppressed even if its leader is
        # unregistered before the member's next live read (whose TTL was just advanced).
        self.set_leader_slaves(member.player_id, [])
        self.set_self_role(member.player_id, True)

    async def _low_level_join(self, leader: NativePlayer, member: NativePlayer) -> None:
        """Join a member to a leader over the low-level LinkPlay client (generic or mixed)."""
        if not (leader_ip := leader.native_ip):
            raise PlayerCommandFailed(f"Cannot group {member.player_id}: leader address unknown")
        # a Wi-Fi Direct join moves the follower onto the leader's private network, where MA
        # can no longer poll or control it, and a cross-generation group is unsupported. Only
        # group two devices that both use a known, matching router-based multiroom generation,
        # rather than form an uncontrollable or broken group.
        leader_info = await self._device_info(leader)
        member_info = await self._device_info(member)
        if not linkplay_group_compatible(leader_info, member_info):
            raise PlayerCommandFailed(
                f"Cannot group {member.player_id} with {leader.player_id}: incompatible or "
                "legacy Wi-Fi Direct LinkPlay multiroom"
            )
        # the low-level join_slave (unlike the official SDK) does not disband a member that
        # leads its own group, so dissolve that group first: otherwise its followers are
        # orphaned once the member becomes this leader's follower.
        if followers := [m for m in self.members_of(member.player_id) if m != member.player_id]:
            await self._apply_members(member, None, followers)
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
        """Resolve a follower's address from the leader's own slave list."""
        try:
            slaves = await client.get_slaves_info()
        except WiiMError as err:
            self._provider.logger.debug("Failed to read leader slave list: %s", err)
            return None
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

    async def _device_info(self, player: NativePlayer) -> DeviceInfo | None:
        """
        Read a player's device info for the compatibility gate, best-effort.

        :param player: The player whose device info to read.
        """
        try:
            return await player.make_command_client().get_device_info_model()
        except WiiMError as err:
            self._provider.logger.debug(
                "Could not read device info for %s: %s", player.player_id, err
            )
            return None
