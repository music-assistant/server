"""Tests for the native multiroom topology coordinator shared by both backends."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed
from pywiim import WiiMError
from wiim.exceptions import WiimException

from music_assistant.providers.wiim.constants import BACKEND_GENERIC, BACKEND_OFFICIAL
from music_assistant.providers.wiim.grouping import NativeGroupCoordinator, NativeGroupRole

# Two verified LinkPlay identities: the leader and a follower whose 24-char HTTP
# slave uuid maps to the follower's UDN-derived player id.
LEADER_ID = "wiim_uuid:11111111-2222-3333-4444-555555555555"
LEADER_MASTER_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_MASTER_UUID = "99999999-8888-7777-6666-555555555555"
FOLLOWER_HTTP_UUID = "A1B2C3D4E5F6A7B8C9D0E1F2"
FOLLOWER_ID = "wiim_uuid:A1B2C3D4-E5F6-A7B8-C9D0-E1F2A1B2C3D4"


def _ginfo(role: str, master: str | None = LEADER_MASTER_UUID) -> MagicMock:
    """Build a fake DeviceGroupInfo for a follower/solo device (role + master uuid)."""
    return MagicMock(role=role, master_uuid=master, slave_uuids=[])


def _leader_info(slave_uuids: list[str]) -> MagicMock:
    """Build a fake DeviceGroupInfo for a master device with the given slave uuids."""
    return MagicMock(role="master", master_uuid=None, slave_uuids=slave_uuids)


def _leader_client(slaves: list[str]) -> MagicMock:
    """
    Build a leader command client whose live group info reflects a mutable slave list.

    Tests mutate ``slaves`` (directly or from a grouping command's side effect) so the
    leader-side verification observes the membership the command produced on the device.
    ``get_slaves_info`` derives a stable per-uuid ip and ``kick_slave`` removes the slave
    at that ip, mirroring how a follower is removed from the leader.
    """

    def _ip_for(index: int) -> str:
        return f"192.168.1.{50 + index}"

    async def _info() -> MagicMock:
        return MagicMock(
            role="master" if slaves else "solo", slave_uuids=list(slaves), master_uuid=None
        )

    async def _slaves_info() -> list[dict[str, str]]:
        return [{"uuid": uuid, "ip": _ip_for(i)} for i, uuid in enumerate(slaves)]

    async def _kick(slave_ip: str) -> None:
        for i, uuid in enumerate(list(slaves)):
            if _ip_for(i) == slave_ip:
                slaves.remove(uuid)
                break

    client = MagicMock()
    client.get_device_group_info = AsyncMock(side_effect=_info)
    client.get_slaves_info = AsyncMock(side_effect=_slaves_info)
    client.get_device_info_model = AsyncMock(
        return_value=MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
    )
    client.capabilities = {"probed": True}
    client.create_group = AsyncMock()
    client.kick_slave = AsyncMock(side_effect=_kick)
    return client


def _member_client() -> MagicMock:
    """Build a follower command-client stub for a mixed-group join target."""
    client = MagicMock()
    client.capabilities = {"probed": True}
    client.get_device_info_model = AsyncMock(
        return_value=MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
    )
    # a join reads the member's own live group first (it must lead nothing); default solo
    client.get_device_group_info = AsyncMock(return_value=MagicMock(role="solo"))
    client.get_slaves_info = AsyncMock(return_value=[])
    return client


def _make_player(
    player_id: str,
    backend: str,
    *,
    available: bool = True,
    ip: str | None = "192.168.1.10",
    command_client: MagicMock | None = None,
    live_role: str = "solo",
    live_master: str | None = None,
) -> MagicMock:
    """Build a fake native player exposing exactly the coordinator's needed surface."""
    player = MagicMock()
    player.player_id = player_id
    player.linkplay_backend = backend
    player.native_available = available
    player.native_ip = ip
    player.native_device_udn = player_id.removeprefix("wiim_")
    if command_client is None:
        command_client = MagicMock()
        command_client.capabilities = {"probed": True}
        command_client.get_device_group_info = AsyncMock(
            return_value=MagicMock(role=live_role, master_uuid=live_master, slave_uuids=[])
        )
        command_client.get_slaves_info = AsyncMock(return_value=[])
        command_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
        )
    player.make_command_client = MagicMock(return_value=command_client)
    player.store_command_capabilities = MagicMock()
    player.on_native_group_update = MagicMock()
    # a modern, router-based generation so two generic peers are offerable to each other
    player.cached_device_info = MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
    return player


@pytest.fixture(autouse=True)
def _fast_membership_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the grouping verification retry to a single immediate check in tests."""
    monkeypatch.setattr("music_assistant.providers.wiim.grouping.VERIFY_MAX_WAIT", 0.0)
    monkeypatch.setattr("music_assistant.providers.wiim.grouping.VERIFY_POLL_INTERVAL", 0.0)


def _make_coordinator(*players: MagicMock) -> tuple[NativeGroupCoordinator, MagicMock]:
    """Create a coordinator over a provider whose player list is the given players."""
    provider = MagicMock()
    provider.players = list(players)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider.wiim_controller = MagicMock(
        async_join_group=AsyncMock(), async_ungroup_device=AsyncMock()
    )
    by_id = {player.player_id: player for player in players}
    provider.mass.players.get_player.side_effect = by_id.get
    return NativeGroupCoordinator(provider), provider


class TestTopologyResolution:
    """Reconcile resolves raw slave uuids against registered players, both directions."""

    async def test_generic_leader_resolves_generic_follower(self) -> None:
        """A generic leader's slave uuid resolves to its registered generic follower."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)

        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        assert coordinator.role_of(LEADER_ID) == NativeGroupRole.LEADER
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]
        assert coordinator.leader_of(FOLLOWER_ID) == LEADER_ID

    async def test_official_leader_resolves_generic_follower(self) -> None:
        """An official leader lists a generic follower (mixed group), leader first."""
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)

        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER

    async def test_unknown_slave_is_skipped(self) -> None:
        """A slave uuid that resolves to no registered player is ignored."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader)

        coordinator.set_leader_slaves(LEADER_ID, ["FFFFFFFFFFFFFFFFFFFFFFFF"])
        await coordinator.reconcile()

        assert coordinator.role_of(LEADER_ID) == NativeGroupRole.STANDALONE
        assert coordinator.members_of(LEADER_ID) == []

    async def test_follower_leadership_is_ignored(self) -> None:
        """A device that is another leader's follower cannot also be a leader."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)

        # follower still has a stale self-leadership entry while also being a slave
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        coordinator.set_leader_slaves(FOLLOWER_ID, ["FFFFFFFFFFFFFFFFFFFFFFFF"])
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.members_of(FOLLOWER_ID) == []

    async def test_moved_follower_belongs_to_one_group(self) -> None:
        """
        A moved follower shows in only one group.

        While it still lingers in its old leader's not-yet-refreshed cached list,
        the most recently refreshed leader wins so it never appears in two groups.
        """
        old_leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        new_leader = _make_player("wiim_uuid:99999999-8888-7777-6666-555555555555", BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(old_leader, new_leader, follower)

        # old leader read the follower first; the new leader read it more recently
        coordinator.set_leader_slaves(old_leader.player_id, [FOLLOWER_HTTP_UUID])
        coordinator.set_leader_slaves(new_leader.player_id, [FOLLOWER_HTTP_UUID])
        coordinator._refreshed_at[old_leader.player_id] = 100.0
        coordinator._refreshed_at[new_leader.player_id] = 200.0
        await coordinator.reconcile()

        assert coordinator.leader_of(FOLLOWER_ID) == new_leader.player_id
        assert coordinator.members_of(new_leader.player_id) == [new_leader.player_id, FOLLOWER_ID]
        assert coordinator.members_of(old_leader.player_id) == []
        assert coordinator.role_of(old_leader.player_id) == NativeGroupRole.STANDALONE


class TestReconcileNotifications:
    """Reconcile pushes state only to players whose role or membership changed."""

    async def test_only_changed_players_notified(self) -> None:
        """A newly formed group notifies the leader and follower exactly once."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)

        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        leader.on_native_group_update.assert_called_once()
        follower.on_native_group_update.assert_called_once()

    async def test_moved_follower_is_notified_even_when_role_unchanged(self) -> None:
        """A follower moving between leaders (still FOLLOWER) is still notified."""
        other_leader_id = "wiim_uuid:22222222-3333-4444-5555-666666666666"
        leader_a = _make_player(LEADER_ID, BACKEND_GENERIC)
        leader_b = _make_player(other_leader_id, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader_a, leader_b, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        coordinator._refreshed_at[LEADER_ID] = 100.0
        await coordinator.reconcile()
        follower.on_native_group_update.reset_mock()

        # move to leader B: role stays FOLLOWER and members stays [], only the leader changes
        coordinator.set_leader_slaves(LEADER_ID, [])
        coordinator.set_leader_slaves(other_leader_id, [FOLLOWER_HTTP_UUID])
        coordinator._refreshed_at[other_leader_id] = 200.0
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.leader_of(FOLLOWER_ID) == other_leader_id
        follower.on_native_group_update.assert_called_once()

    async def test_stale_reverse_mapping_removed_atomically(self) -> None:
        """When a leader drops a follower, that follower's reverse mapping is cleared."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        # the leader is now alone again
        coordinator.set_leader_slaves(LEADER_ID, [])
        await coordinator.reconcile()

        assert coordinator.leader_of(FOLLOWER_ID) is None
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.STANDALONE
        assert coordinator.role_of(LEADER_ID) == NativeGroupRole.STANDALONE

    async def test_leaders_notified_before_followers(self) -> None:
        """A follower reads its leader's cached members, so the leader publishes first."""
        order: list[str] = []
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        leader.on_native_group_update = MagicMock(side_effect=lambda: order.append("leader"))
        follower.on_native_group_update = MagicMock(side_effect=lambda: order.append("follower"))
        # register the follower first so the natural iteration order would notify it first
        coordinator, _ = _make_coordinator(follower, leader)

        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        assert order == ["leader", "follower"]

    async def test_dissolve_notifies_old_leader_before_follower(self) -> None:
        """On a dissolve the old leader clears its members before the follower rescans."""
        order: list[str] = []
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        # register the follower first so the natural iteration order would notify it first
        coordinator, _ = _make_coordinator(follower, leader)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()
        leader.on_native_group_update = MagicMock(side_effect=lambda: order.append("leader"))
        follower.on_native_group_update = MagicMock(side_effect=lambda: order.append("follower"))

        # dissolve: the leader drops the follower, both become standalone
        coordinator.set_leader_slaves(LEADER_ID, [])
        await coordinator.reconcile()

        assert order.index("leader") < order.index("follower")

    async def test_move_notifies_both_leaders_before_follower(self) -> None:
        """Moving a follower publishes both the old and new leader before the follower."""
        other_leader_id = "wiim_uuid:22222222-3333-4444-5555-666666666666"
        order: list[str] = []
        leader_a = _make_player(LEADER_ID, BACKEND_GENERIC)
        leader_b = _make_player(other_leader_id, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        # register the follower first so the natural order would notify it first
        coordinator, _ = _make_coordinator(follower, leader_a, leader_b)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        coordinator._refreshed_at[LEADER_ID] = 100.0
        await coordinator.reconcile()
        leader_a.on_native_group_update = MagicMock(side_effect=lambda: order.append("a"))
        leader_b.on_native_group_update = MagicMock(side_effect=lambda: order.append("b"))
        follower.on_native_group_update = MagicMock(side_effect=lambda: order.append("follower"))

        # move the follower from leader A to leader B
        coordinator.set_leader_slaves(LEADER_ID, [])
        coordinator.set_leader_slaves(other_leader_id, [FOLLOWER_HTTP_UUID])
        coordinator._refreshed_at[other_leader_id] = 200.0
        await coordinator.reconcile()

        assert order.index("follower") == len(order) - 1  # follower is notified last
        assert "a" in order  # both leaders published before it
        assert "b" in order

    async def test_unknown_follower_transition_notifies_all_players(self) -> None:
        """An unknown-leader-follower change flips every peer's can_group_with, so all re-publish."""
        me = _make_player(LEADER_ID, BACKEND_GENERIC)
        peer = _make_player("wiim_uuid:peer", BACKEND_OFFICIAL)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, peer, follower)

        # the device becomes an unknown-leader follower: every peer loses it as a candidate
        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()
        me.on_native_group_update.assert_called_once()
        peer.on_native_group_update.assert_called_once()

        for player in (me, peer, follower):
            player.on_native_group_update.reset_mock()

        # it returns to standalone: every peer regains it as a candidate
        coordinator.set_self_role(FOLLOWER_ID, False)
        await coordinator.reconcile()
        me.on_native_group_update.assert_called_once()
        peer.on_native_group_update.assert_called_once()
        follower.on_native_group_update.assert_called_once()


class TestSelfHealing:
    """Topology heals as players register and as the slow TTL elapses."""

    async def test_discovery_order_miss_heals_on_registration(self) -> None:
        """A follower discovered after its leader is placed on the next reconcile."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        coordinator, provider = _make_coordinator(leader)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()
        assert coordinator.members_of(LEADER_ID) == []  # follower not registered yet

        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        provider.players.append(follower)
        await coordinator.reconcile()

        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]

    async def test_ttl_gates_official_refresh(self) -> None:
        """An official leader re-reads its live group state only once per TTL unless forced."""
        client = MagicMock()
        client.get_device_group_info = AsyncMock(return_value=_ginfo("solo", None))
        client.get_slaves_info = AsyncMock(return_value=[])
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(leader)

        await coordinator.refresh_leader(leader, force=True)
        await coordinator.refresh_leader(leader)  # within TTL -> skipped
        assert client.get_device_group_info.await_count == 1

        await coordinator.refresh_leader(leader, force=True)  # forced -> reads again
        assert client.get_device_group_info.await_count == 2

    async def test_failed_refresh_keeps_previous_members(self) -> None:
        """A temporarily unreachable leader keeps the members it last resolved."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        client = MagicMock()
        client.get_device_group_info = AsyncMock(
            side_effect=[_leader_info([FOLLOWER_HTTP_UUID]), WiiMError("offline")]
        )
        client.get_slaves_info = AsyncMock(
            return_value=[{"uuid": FOLLOWER_HTTP_UUID, "ip": "192.168.1.50"}]
        )
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.refresh_leader(leader, force=True)
        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]

        await coordinator.refresh_leader(leader, force=True)  # read fails
        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]

    async def test_solo_with_failing_slave_list_keeps_members(self) -> None:
        """A master reported as 'solo' by a failed slave-list read keeps its members."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        client = MagicMock()
        # get_device_group_info swallows a failed slave-list read and reports solo
        client.get_device_group_info = AsyncMock(return_value=_ginfo("solo", None))
        client.get_slaves_info = AsyncMock(side_effect=WiiMError("slave list offline"))
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()
        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]

        # the strict slave-list read fails, so the refresh is non-destructive
        assert await coordinator.refresh_leader(leader, force=True) is False
        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]

    async def test_genuine_solo_clears_members(self) -> None:
        """A leader whose strict slave-list read is truly empty drops its members."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        client = MagicMock()
        client.get_device_group_info = AsyncMock(return_value=_ginfo("solo", None))
        client.get_slaves_info = AsyncMock(return_value=[])
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()
        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]

        assert await coordinator.refresh_leader(leader, force=True) is True
        assert coordinator.members_of(LEADER_ID) == []

    async def test_concurrent_reconcile_is_serialized(self) -> None:
        """Concurrent reconciles are serialized and leave a consistent snapshot."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])

        await asyncio.gather(coordinator.reconcile(), coordinator.reconcile())

        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]
        assert coordinator.leader_of(FOLLOWER_ID) == LEADER_ID


class TestCanGroupWith:
    """can_group_with widens to available peers of either backend, minus self."""

    def test_lists_available_peers_of_both_backends(self) -> None:
        """An official player can group with available official and generic peers."""
        me = _make_player(LEADER_ID, BACKEND_OFFICIAL)
        generic_peer = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        official_peer = _make_player("wiim_uuid:official-peer", BACKEND_OFFICIAL)
        coordinator, _ = _make_coordinator(me, generic_peer, official_peer)

        assert coordinator.can_group_with(me) == {FOLLOWER_ID, "wiim_uuid:official-peer"}

    def test_excludes_unavailable_and_self(self) -> None:
        """Unavailable peers and the player itself are excluded from the candidates."""
        me = _make_player(LEADER_ID, BACKEND_OFFICIAL)
        offline_peer = _make_player(FOLLOWER_ID, BACKEND_GENERIC, available=False)
        coordinator, _ = _make_coordinator(me, offline_peer)

        assert coordinator.can_group_with(me) == set()

    async def test_unavailable_member_retained_in_topology(self) -> None:
        """A registered but unavailable follower stays a member yet is not groupable."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, available=False)
        coordinator, _ = _make_coordinator(leader, follower)

        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        assert coordinator.members_of(LEADER_ID) == [LEADER_ID, FOLLOWER_ID]
        assert FOLLOWER_ID not in coordinator.can_group_with(leader)

    async def test_known_follower_can_be_moved_to_another_leader(self) -> None:
        """A follower with a known leader stays groupable (core auto-ungroups it)."""
        me = _make_player("wiim_uuid:official-peer", BACKEND_OFFICIAL)
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER

        # the follower has a known leader, so it may still be offered as a target
        assert FOLLOWER_ID in coordinator.can_group_with(me)

    async def test_unknown_leader_follower_is_excluded(self) -> None:
        """A follower of a leader MA has not discovered cannot be cleanly moved."""
        me = _make_player("wiim_uuid:official-peer", BACKEND_OFFICIAL)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, follower)
        coordinator.set_self_role(FOLLOWER_ID, True)  # follows an undiscovered leader
        await coordinator.reconcile()

        assert coordinator.can_group_with(me) == set()

    async def test_unknown_leader_follower_offered_no_candidates(self) -> None:
        """A device that itself follows an undiscovered leader gets no candidates."""
        me = _make_player(LEADER_ID, BACKEND_GENERIC)
        peer = _make_player("wiim_uuid:official-peer", BACKEND_OFFICIAL)
        coordinator, _ = _make_coordinator(me, peer)
        coordinator.set_self_role(LEADER_ID, True)  # follows an undiscovered leader
        await coordinator.reconcile()

        assert coordinator.can_group_with(me) == set()


class TestSameBackendGrouping:
    """Same-backend grouping keeps each backend's native path, verified leader-side."""

    async def test_official_add_uses_sdk_controller(self) -> None:
        """Two official players are joined via the WiiM SDK controller and verified."""
        slaves: list[str] = []
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client(slaves))
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)

        async def _join(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)  # the device joins the follower

        provider.wiim_controller.async_join_group = AsyncMock(side_effect=_join)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        provider.wiim_controller.async_join_group.assert_awaited_once_with(
            leader.native_device_udn, [follower.native_device_udn]
        )

    async def test_official_remove_uses_sdk_controller(self) -> None:
        """A managed official member is ungrouped via the SDK, no leader-side kick."""
        slaves = [FOLLOWER_HTTP_UUID]
        leader_client = _leader_client(slaves)
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=leader_client)
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)

        async def _ungroup(*_args: object, **_kwargs: object) -> None:
            slaves.clear()  # the SDK removes the follower

        provider.wiim_controller.async_ungroup_device = AsyncMock(side_effect=_ungroup)

        await coordinator.set_members(leader, None, [FOLLOWER_ID])

        provider.wiim_controller.async_ungroup_device.assert_awaited_once_with(
            follower.native_device_udn
        )
        leader_client.kick_slave.assert_not_called()  # SDK removal took effect

    async def test_official_recovered_remove_issues_leader_side_kick(self) -> None:
        """When the SDK no-ops on a recovered group, the leader kicks the follower."""
        slaves = [FOLLOWER_HTTP_UUID]
        leader_client = _leader_client(slaves)  # its kick_slave removes the follower
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=leader_client)
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_ungroup_device = AsyncMock()  # SDK no-op

        await coordinator.set_members(leader, None, [FOLLOWER_ID])

        provider.wiim_controller.async_ungroup_device.assert_awaited_once()
        leader_client.kick_slave.assert_awaited_once_with("192.168.1.50")

    async def test_official_join_no_op_raises(self) -> None:
        """An SDK join the device ignored (leader never lists it) surfaces a typed failure."""
        slaves: list[str] = []
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client(slaves))
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_join_group = AsyncMock()  # device ignores the join

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)

    async def test_official_leave_no_op_raises(self) -> None:
        """When neither the SDK nor the leader-side kick takes effect, a failure is raised."""
        slaves = [FOLLOWER_HTTP_UUID]
        leader_client = _leader_client(slaves)
        leader_client.kick_slave = AsyncMock()  # the kick also no-ops (does not remove)
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=leader_client)
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_ungroup_device = AsyncMock()  # SDK no-op

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, None, [FOLLOWER_ID])
        leader_client.kick_slave.assert_awaited_once()  # the leader-side kick was attempted

    async def test_official_join_failure_raises_typed(self) -> None:
        """An SDK error (WiimException, not pywiim's) is mapped to PlayerCommandFailed."""
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client([]))
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_join_group = AsyncMock(
            side_effect=WiimException("HTTP API not available")
        )

        with pytest.raises(PlayerCommandFailed) as exc_info:
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        assert isinstance(exc_info.value.__cause__, WiimException)

    async def test_official_remove_failure_raises_typed(self) -> None:
        """An SDK ungroup error is mapped to PlayerCommandFailed with chaining."""
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client([FOLLOWER_HTTP_UUID])
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_ungroup_device = AsyncMock(
            side_effect=WiimException("device gone")
        )

        with pytest.raises(PlayerCommandFailed) as exc_info:
            await coordinator.set_members(leader, None, [FOLLOWER_ID])
        assert isinstance(exc_info.value.__cause__, WiimException)

    async def test_generic_add_uses_low_level_join_and_verifies(self) -> None:
        """Two generic players use the low-level join_slave and confirm leader-side."""
        slaves: list[str] = []
        member_client = _member_client()

        async def _join(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)

        member_client.join_slave = AsyncMock(side_effect=_join)
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.30", command_client=_leader_client(slaves)
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        member_client.join_slave.assert_awaited_once()
        assert member_client.join_slave.await_args.args[0] == "192.168.1.30"

    async def test_generic_join_no_op_raises(self) -> None:
        """A low-level join the device ignores raises PlayerCommandFailed."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()  # device ignores it
        leader = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client([]))
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)

    async def test_generic_remove_uses_low_level_leave(self) -> None:
        """A generic member leaves via the low-level client and is verified."""
        slaves = [FOLLOWER_HTTP_UUID]
        member_client = _member_client()

        async def _leave() -> None:
            slaves.clear()

        member_client.leave_group = AsyncMock(side_effect=_leave)
        leader = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client(slaves))
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, None, [FOLLOWER_ID])

        member_client.leave_group.assert_awaited_once()

    async def test_generic_incompatible_group_raises(self) -> None:
        """An incompatible same-backend group surfaces a typed failure, not a no-op."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        # the member is a legacy Wi-Fi Direct device: the compatibility gate refuses it
        member_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=True, wmrm_version="4.2")
        )
        leader = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client([]))
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_join_clears_joined_members_cached_slaves(self) -> None:
        """A member that was a leader has its cached slave list cleared once it joins."""
        slaves: list[str] = []
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client(slaves))
        member = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, member)
        # the member had its own follower cached from when it was itself a leader
        coordinator.set_leader_slaves(FOLLOWER_ID, ["A1B2C3D4E5F6A7B8C9D0E1F2"])

        async def _join(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)

        provider.wiim_controller.async_join_group = AsyncMock(side_effect=_join)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        # the stale list is cleared so a later leave cannot resurrect a ghost group
        assert coordinator._raw_slaves.get(FOLLOWER_ID) == []


class TestExpectedLeaderVerification:
    """Grouping is verified against the leader the command targeted."""

    async def test_leave_clears_self_follower_immediately(self) -> None:
        """A confirmed leave clears the member's stale self-follower flag at once."""
        slaves = [FOLLOWER_HTTP_UUID]
        member_client = _member_client()

        async def _leave() -> None:
            slaves.clear()

        member_client.leave_group = AsyncMock(side_effect=_leave)
        leader = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client(slaves))
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)
        # the follower reported itself a slave on its last live read
        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER

        await coordinator.set_members(leader, None, [FOLLOWER_ID])

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.STANDALONE

    async def test_leave_keeps_follower_when_new_reverse_leader_wins(self) -> None:
        """Clearing the self-flag on leave still yields FOLLOWER under a new known leader."""
        other_leader_id = "wiim_uuid:22222222-3333-4444-5555-666666666666"
        slaves_a = [FOLLOWER_HTTP_UUID]
        member_client = _member_client()

        async def _leave() -> None:
            slaves_a.clear()

        member_client.leave_group = AsyncMock(side_effect=_leave)
        leader_a = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client(slaves_a))
        leader_b = _make_player(other_leader_id, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader_a, leader_b, follower)
        coordinator.set_self_role(FOLLOWER_ID, True)
        # concurrently, leader B has discovered the follower as its slave
        coordinator.set_leader_slaves(other_leader_id, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        await coordinator.set_members(leader_a, None, [FOLLOWER_ID])

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.leader_of(FOLLOWER_ID) == other_leader_id

    async def test_join_landing_on_other_leader_raises(self) -> None:
        """A join that never appears in this leader's slave list is a no-op failure."""
        # the device "joined" but not this leader, so the leader never lists it
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client([]))
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_join_group = AsyncMock()

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)

    async def test_remove_of_member_not_under_leader_is_idempotent(self) -> None:
        """A member the leader does not list is not detached (solo or another leader)."""
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client([]))
        member_client = _member_client()
        member_client.leave_group = AsyncMock()
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL, command_client=member_client)
        coordinator, provider = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, None, [FOLLOWER_ID])

        provider.wiim_controller.async_ungroup_device.assert_not_called()
        member_client.leave_group.assert_not_called()

    async def test_join_fails_closed_when_leader_unreadable(self) -> None:
        """If the leader's live list cannot be read, the join is not confirmed."""
        leader_client = MagicMock()
        leader_client.get_device_group_info = AsyncMock(side_effect=WiiMError("offline"))
        leader_client.get_device_info_model = AsyncMock(return_value=MagicMock())
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=leader_client)
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)
        provider.wiim_controller.async_join_group = AsyncMock()

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)


class TestMixedGrouping:
    """Cross-backend grouping commands the follower, verified from the leader's list."""

    async def test_mixed_add_joins_follower_to_leader_ip(self) -> None:
        """A cross-backend add sends a low-level join to the follower's leader IP."""
        slaves: list[str] = []
        member_client = _member_client()

        async def _join_slave(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)

        member_client.join_slave = AsyncMock(side_effect=_join_slave)
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client(slaves)
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        member_client.join_slave.assert_awaited_once()
        assert member_client.join_slave.await_args.args[0] == leader.native_ip
        member_client.close.assert_not_called()

    async def test_mixed_add_of_unknown_leader_follower_raises(self) -> None:
        """Joining a member that follows an undiscovered group fails typed at command time."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        # the member's own live read reports it is a slave of a leader MA cannot see
        member_client.get_device_group_info = AsyncMock(
            return_value=MagicMock(role="slave", master_uuid=OTHER_MASTER_UUID)
        )
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)
        coordinator.set_self_role(FOLLOWER_ID, True)  # follows an undiscovered leader
        await coordinator.reconcile()

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_mixed_add_dissolves_members_existing_group_first(self) -> None:
        """A member that leads its own group is dissolved before joining as a follower."""
        sub_id = "wiim_uuid:B1B2C3D4-E5F6-A7B8-C9D0-E1F2B1B2C3D4"
        sub_http_uuid = "B1B2C3D4E5F6A7B8C9D0E1F2"
        # the member currently leads a generic sub-follower; leaving drops it from the
        # member's own live slave list, which the dissolve verification then reads.
        member_slaves = [sub_http_uuid]

        async def _sub_leave() -> None:
            member_slaves.remove(sub_http_uuid)

        sub_client = _member_client()
        sub_client.leave_group = AsyncMock(side_effect=_sub_leave)
        sub_follower = _make_player(sub_id, BACKEND_GENERIC, command_client=sub_client)
        member_client = _leader_client(member_slaves)
        member = _make_player(
            FOLLOWER_ID, BACKEND_GENERIC, ip="192.168.1.40", command_client=member_client
        )
        new_slaves: list[str] = []
        member_client.join_slave = AsyncMock(
            side_effect=lambda *_a, **_k: new_slaves.append(FOLLOWER_HTTP_UUID)
        )
        leader = _make_player(
            LEADER_ID,
            BACKEND_OFFICIAL,
            ip="192.168.1.20",
            command_client=_leader_client(new_slaves),
        )
        coordinator, _ = _make_coordinator(leader, member, sub_follower)
        coordinator.set_leader_slaves(FOLLOWER_ID, [sub_http_uuid])
        await coordinator.reconcile()
        assert coordinator.members_of(FOLLOWER_ID) == [FOLLOWER_ID, sub_id]

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        sub_client.leave_group.assert_awaited_once()  # the member's group was dissolved
        member_client.join_slave.assert_awaited_once()  # before the member itself joined
        assert member_client.join_slave.await_args.args[0] == leader.native_ip

    async def test_mixed_remove_kicks_follower_from_leader(self) -> None:
        """A cross-backend/recovered remove kicks the follower from the leader."""
        slaves = [FOLLOWER_HTTP_UUID]
        leader_client = _leader_client(slaves)  # its kick_slave removes the follower
        member_client = _member_client()
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=leader_client
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, None, [FOLLOWER_ID])

        leader_client.kick_slave.assert_awaited_once_with("192.168.1.50")
        # the fresh command client is primed as master so kick_slave's guard passes
        leader_client.create_group.assert_awaited_once()
        member_client.leave_group.assert_not_called()  # the follower is not commanded directly

    async def test_mixed_add_generic_leader_official_follower(self) -> None:
        """The follower direction also works: generic leader, official follower."""
        slaves: list[str] = []
        member_client = _member_client()

        async def _join_slave(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)

        member_client.join_slave = AsyncMock(side_effect=_join_slave)
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.30", command_client=_leader_client(slaves)
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        assert member_client.join_slave.await_args.args[0] == "192.168.1.30"
        member_client.close.assert_not_called()

    async def test_mixed_join_no_op_raises(self) -> None:
        """A join the leader never lists surfaces a typed failure."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()  # device ignores it
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)

    async def test_mixed_remove_no_op_raises(self) -> None:
        """A follower the leader still lists after a leave surfaces a typed failure."""
        leader_client = _leader_client([FOLLOWER_HTTP_UUID])
        leader_client.kick_slave = AsyncMock()  # device ignores the kick (does not remove)
        member_client = _member_client()
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=leader_client
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, None, [FOLLOWER_ID])
        leader_client.kick_slave.assert_awaited_once()

    async def test_missing_leader_ip_raises(self) -> None:
        """A mixed add with an unknown leader address fails typed instead of guessing."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip=None, command_client=_leader_client([])
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_mixed_add_rejects_wifi_direct_leader(self) -> None:
        """A mixed join to a legacy Wi-Fi Direct leader fails typed instead of stranding the follower."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        leader_client = _leader_client([])
        leader_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=True)
        )
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=leader_client
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_mixed_add_rejects_wifi_direct_member(self) -> None:
        """A mixed join of a legacy Wi-Fi Direct follower is refused too, not only the leader."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        # the follower itself is the legacy device; pywiim would pick Wi-Fi Direct for it
        member_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=True)
        )
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_join_records_member_self_follower(self) -> None:
        """A confirmed join records the member's follower role so an unregistered leader can't strand it."""
        slaves: list[str] = []
        member_client = _member_client()
        member_client.join_slave = AsyncMock(
            side_effect=lambda *_a, **_k: slaves.append(FOLLOWER_HTTP_UUID)
        )
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client(slaves)
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, provider = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)
        assert coordinator.leader_of(FOLLOWER_ID) == LEADER_ID

        # the leader is unregistered before the member's next live read; the recorded
        # self-follower role keeps the member suppressed rather than groupable
        provider.players.remove(leader)
        coordinator.unregister(LEADER_ID)
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.leader_of(FOLLOWER_ID) is None
        assert coordinator.can_group_with(follower) == set()

    async def test_remove_fails_typed_when_slave_list_unreadable(self) -> None:
        """A remove whose leader slave-list read fails raises instead of silently no-op'ing."""
        client = MagicMock()
        # the leader reads as 'solo' only because its slave-list endpoint is failing
        client.get_device_group_info = AsyncMock(return_value=_ginfo("solo", None))
        client.get_slaves_info = AsyncMock(side_effect=WiiMError("slave list offline"))
        client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
        )
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=client)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=_member_client())
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, None, [FOLLOWER_ID])

    async def test_api_error_is_chained(self) -> None:
        """A device API error during a mixed join is chained into the typed failure."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock(side_effect=WiiMError("boom"))
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed) as exc_info:
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        assert isinstance(exc_info.value.__cause__, WiiMError)

    async def test_mixed_join_passes_leader_device_info(self) -> None:
        """The leader's device info is passed so pywiim can pick the join mode."""
        slaves: list[str] = []
        leader_info = MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
        leader_client = _leader_client(slaves)
        leader_client.get_device_info_model = AsyncMock(return_value=leader_info)
        member_client = _member_client()

        async def _join_slave(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)

        member_client.join_slave = AsyncMock(side_effect=_join_slave)
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=leader_client
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        assert member_client.join_slave.await_args.kwargs["master_device_info"] is leader_info

    async def test_join_waits_for_role_to_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A join whose membership appears a moment later is not declared a no-op."""
        monkeypatch.setattr("music_assistant.providers.wiim.grouping.VERIFY_MAX_WAIT", 5.0)
        reads = {"count": 0}

        async def _slaves() -> list[dict[str, str]]:
            reads["count"] += 1
            # the follower only shows up in the leader's slave list on the second read
            return (
                [{"uuid": FOLLOWER_HTTP_UUID, "ip": "192.168.1.50"}] if reads["count"] >= 2 else []
            )

        leader_client = MagicMock()
        leader_client.get_device_group_info = AsyncMock(return_value=_ginfo("solo", None))
        leader_client.get_slaves_info = AsyncMock(side_effect=_slaves)
        leader_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
        )
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=leader_client
        )
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        assert reads["count"] >= 2  # it retried until the membership propagated


class TestSetMembersLifecycle:
    """set_members always refreshes the leader's topology, even after a failure."""

    async def test_forces_leader_refresh_on_success(self) -> None:
        """After a successful command the leader's live group state is re-read."""
        slaves: list[str] = []
        client = _leader_client(slaves)
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        follower = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, follower)

        async def _join(*_args: object, **_kwargs: object) -> None:
            slaves.append(FOLLOWER_HTTP_UUID)

        provider.wiim_controller.async_join_group = AsyncMock(side_effect=_join)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        client.get_device_group_info.assert_awaited()

    async def test_forces_leader_refresh_after_failure(self) -> None:
        """A failed command still refreshes the leader topology before propagating."""
        client = _leader_client([])
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        member_client = _member_client()
        member_client.join_slave = AsyncMock(side_effect=WiiMError("boom"))
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, follower)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        client.get_device_group_info.assert_awaited()

    async def test_unknown_member_raises(self) -> None:
        """A grouping target that is not a registered native player fails typed."""
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader)
        provider.mass.players.get_player.side_effect = None
        provider.mass.players.get_player.return_value = None

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, ["wiim_uuid:ghost"], None)


class TestRegistrationCleanup:
    """Unregistering a player drops its topology cache entry."""

    async def test_unregister_drops_stale_leadership(self) -> None:
        """A removed leader's cached members disappear on the next reconcile."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, provider = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        provider.players.remove(leader)
        coordinator.unregister(LEADER_ID)
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.STANDALONE

    async def test_reconcile_drops_cache_for_unregistered_leader(self) -> None:
        """Cache entries for players no longer registered are pruned during reconcile."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, provider = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        await coordinator.reconcile()

        provider.players.remove(leader)  # gone, but no explicit unregister
        await coordinator.reconcile()

        assert LEADER_ID not in coordinator._raw_slaves  # pruned
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.STANDALONE

    async def test_refresh_discards_response_for_replaced_player(self) -> None:
        """A read finishing after the player was replaced is not applied to the new one."""
        client = MagicMock()
        client.get_device_group_info = AsyncMock(return_value=_leader_info([FOLLOWER_HTTP_UUID]))
        client.get_slaves_info = AsyncMock(
            return_value=[{"uuid": FOLLOWER_HTTP_UUID, "ip": "192.168.1.50"}]
        )
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, provider = _make_coordinator(leader, follower)
        # a fast unregister/re-register replaced the player instance during the read
        replacement = _make_player(LEADER_ID, BACKEND_OFFICIAL)
        provider.mass.players.get_player.side_effect = {
            LEADER_ID: replacement,
            FOLLOWER_ID: follower,
        }.get

        result = await coordinator.refresh_leader(leader, force=True)

        assert result is False
        assert coordinator.members_of(LEADER_ID) == []  # stale response not applied


class TestSelfReportedFollower:
    """A device following a leader MA has not discovered still reports as a follower."""

    async def test_self_role_yields_follower_without_known_leader(self) -> None:
        """A self-reported follower is FOLLOWER even though its leader is unknown."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(follower)

        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.leader_of(FOLLOWER_ID) is None

    async def test_self_reported_follower_excluded_from_can_group_with(self) -> None:
        """A self-reported follower is not offered as a grouping target."""
        me = _make_player(LEADER_ID, BACKEND_OFFICIAL)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, follower)

        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()

        assert coordinator.can_group_with(me) == set()

    async def test_known_leader_takes_precedence_over_self_role(self) -> None:
        """A resolved reverse leader is preferred; leader_of returns the known leader."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, follower)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID])
        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.leader_of(FOLLOWER_ID) == LEADER_ID

    async def test_self_role_cleared_returns_to_standalone(self) -> None:
        """Clearing the self-follower flag returns the player to standalone."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(follower)
        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()

        coordinator.set_self_role(FOLLOWER_ID, False)
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.STANDALONE

    async def test_unregister_clears_self_role(self) -> None:
        """Unregistering a player drops its self-reported follower flag."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(follower)
        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER

        coordinator.unregister(FOLLOWER_ID)
        await coordinator.reconcile()

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.STANDALONE

    async def test_refresh_feeds_self_follower_from_live_role(self) -> None:
        """A live read reporting 'slave' under an unknown leader yields a follower role."""
        client = MagicMock()
        # follower of a leader MA does not manage (its master is not a registered player)
        client.get_device_group_info = AsyncMock(return_value=_ginfo("slave", OTHER_MASTER_UUID))
        player = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(player)

        await coordinator.refresh_leader(player, force=True)

        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.leader_of(FOLLOWER_ID) is None

    async def test_live_self_role_retained_on_failed_refresh(self) -> None:
        """A failed live read keeps the last self-role instead of clearing it."""
        client = MagicMock()
        client.get_device_group_info = AsyncMock(
            side_effect=[_ginfo("slave", OTHER_MASTER_UUID), WiiMError("offline")]
        )
        player = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(player)

        await coordinator.refresh_leader(player, force=True)
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER

        await coordinator.refresh_leader(player, force=True)  # read fails
        assert coordinator.role_of(FOLLOWER_ID) == NativeGroupRole.FOLLOWER


class TestConcurrentRefresh:
    """Overlapping refreshes for one leader must not apply out of order."""

    async def test_same_leader_refresh_is_serialized(self) -> None:
        """Concurrent forced refreshes for one leader run one at a time."""
        concurrency = {"current": 0, "max": 0}

        async def _slow_read() -> MagicMock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
            await asyncio.sleep(0.01)
            concurrency["current"] -= 1
            return _ginfo("solo", None)

        client = MagicMock()
        client.get_device_group_info = AsyncMock(side_effect=_slow_read)
        client.get_slaves_info = AsyncMock(return_value=[])
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=client)
        coordinator, _ = _make_coordinator(leader)

        await asyncio.gather(
            coordinator.refresh_leader(leader, force=True),
            coordinator.refresh_leader(leader, force=True),
        )

        assert client.get_device_group_info.await_count == 2
        assert concurrency["max"] == 1  # the reads never overlapped


class TestCommandSerialization:
    """Grouping commands are serialized so concurrent moves cannot interleave."""

    async def test_concurrent_commands_do_not_interleave(self) -> None:
        """A second command waits for the first to finish its whole sequence."""
        other_leader_id = "wiim_uuid:22222222-3333-4444-5555-666666666666"
        other_follower_http = "C1B2C3D4E5F6A7B8C9D0E1F2"
        other_follower_id = "wiim_uuid:C1B2C3D4-E5F6-A7B8-C9D0-E1F2C1B2C3D4"
        gate = asyncio.Event()
        order: list[str] = []

        slaves_a: list[str] = []
        member_a_client = _member_client()

        async def _join_a(*_args: object, **_kwargs: object) -> None:
            order.append("a")
            await gate.wait()  # hold the first command mid-sequence
            slaves_a.append(FOLLOWER_HTTP_UUID)

        member_a_client.join_slave = AsyncMock(side_effect=_join_a)

        slaves_b: list[str] = []
        member_b_client = _member_client()

        async def _join_b(*_args: object, **_kwargs: object) -> None:
            order.append("b")
            slaves_b.append(other_follower_http)

        member_b_client.join_slave = AsyncMock(side_effect=_join_b)

        leader_a = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client(slaves_a)
        )
        member_a = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_a_client)
        leader_b = _make_player(
            other_leader_id,
            BACKEND_OFFICIAL,
            ip="192.168.1.21",
            command_client=_leader_client(slaves_b),
        )
        member_b = _make_player(other_follower_id, BACKEND_GENERIC, command_client=member_b_client)
        coordinator, _ = _make_coordinator(leader_a, member_a, leader_b, member_b)

        task_a = asyncio.create_task(coordinator.set_members(leader_a, [FOLLOWER_ID], None))
        for _ in range(50):
            await asyncio.sleep(0)
            if order == ["a"]:
                break
        assert order == ["a"]  # the first command is mid-sequence, holding the lock

        task_b = asyncio.create_task(coordinator.set_members(leader_b, [other_follower_id], None))
        for _ in range(50):
            await asyncio.sleep(0)
        assert order == ["a"]  # the second command is blocked behind the first

        gate.set()
        await asyncio.gather(task_a, task_b)
        assert order == ["a", "b"]  # only after the first finished did the second run


# a second follower identity for whole-batch validation tests
SECOND_HTTP_UUID = "D1B2C3D4E5F6A7B8C9D0E1F2"
SECOND_ID = "wiim_uuid:D1B2C3D4-E5F6-A7B8-C9D0-E1F2D1B2C3D4"


class TestBatchValidation:
    """The whole batch is validated before any speaker joins or leaves."""

    async def test_incompatible_later_addition_aborts_before_any_join(self) -> None:
        """A legacy/incompatible target later in the batch stops every join up front."""
        good_slaves: list[str] = []
        good_client = _member_client()
        good_client.join_slave = AsyncMock(
            side_effect=lambda *_a, **_k: good_slaves.append(FOLLOWER_HTTP_UUID)
        )
        bad_client = _member_client()
        bad_client.join_slave = AsyncMock()
        # the second target is a legacy Wi-Fi Direct device the compatibility gate refuses
        bad_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=True, wmrm_version="4.2")
        )
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.20", command_client=_leader_client([])
        )
        good = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=good_client)
        bad = _make_player(SECOND_ID, BACKEND_GENERIC, command_client=bad_client)
        coordinator, _ = _make_coordinator(leader, good, bad)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID, SECOND_ID], None)
        # the whole batch was validated first, so the compatible target never joined either
        good_client.join_slave.assert_not_called()
        bad_client.join_slave.assert_not_called()

    async def test_leader_following_unknown_group_cannot_lead(self) -> None:
        """A leader that itself follows an undiscovered group is refused before any mutation."""
        leader_client = _leader_client([])
        # the leader's own live read reports it is a slave of a leader MA cannot see
        leader_client.get_device_group_info = AsyncMock(
            return_value=MagicMock(role="slave", master_uuid=OTHER_MASTER_UUID)
        )
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.20", command_client=leader_client
        )
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        member = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, member)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_is_unknown_leader_follower_query(self) -> None:
        """The public query flags a self-reported follower whose leader is undiscovered."""
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(follower)
        coordinator.set_self_role(FOLLOWER_ID, True)
        await coordinator.reconcile()
        assert coordinator.is_unknown_leader_follower(FOLLOWER_ID) is True

        coordinator.set_self_role(FOLLOWER_ID, False)
        await coordinator.reconcile()
        assert coordinator.is_unknown_leader_follower(FOLLOWER_ID) is False

    async def test_unreachable_generic_removal_aborts_batch(self) -> None:
        """An unreachable generic follower fails a removal batch before any detach runs."""
        slaves = [FOLLOWER_HTTP_UUID, SECOND_HTTP_UUID]
        good_client = _member_client()
        good_client.leave_group = AsyncMock()
        leader = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client(slaves))
        good = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=good_client)
        bad = _make_player(SECOND_ID, BACKEND_GENERIC, available=False)  # unreachable
        coordinator, _ = _make_coordinator(leader, good, bad)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, None, [FOLLOWER_ID, SECOND_ID])
        good_client.leave_group.assert_not_called()

    def test_incompatible_generic_peer_not_offered(self) -> None:
        """A legacy/incompatible generic peer is not offered as a grouping candidate."""
        me = _make_player(LEADER_ID, BACKEND_GENERIC)
        peer = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        # the peer is a legacy Wi-Fi Direct device, so the pair is guaranteed to fail
        peer.cached_device_info = MagicMock(needs_wifi_direct_multiroom=True, wmrm_version="4.2")
        coordinator, _ = _make_coordinator(me, peer)

        assert coordinator.can_group_with(me) == set()

    def test_compatible_generic_peer_is_offered(self) -> None:
        """A modern, matching-generation generic peer is offered as a candidate."""
        me = _make_player(LEADER_ID, BACKEND_GENERIC)
        peer = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, peer)

        assert coordinator.can_group_with(me) == {FOLLOWER_ID}

    async def test_join_fails_closed_when_member_leads_unmanaged_slave(self) -> None:
        """A member still leading an addressable slave MA cannot resolve is not orphaned."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        member_client.get_device_group_info = AsyncMock(return_value=MagicMock(role="master"))
        # the slave resolves to no registered player, so it cannot be cleanly dissolved
        member_client.get_slaves_info = AsyncMock(
            return_value=[{"uuid": "FFFFFFFFFFFFFFFFFFFFFFFF", "ip": "192.168.1.77"}]
        )
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        member = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, member)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_join_dissolves_follower_gained_since_cache(self) -> None:
        """A follower discovered only in the member's live read is still dissolved first."""
        sub_http = "B1B2C3D4E5F6A7B8C9D0E1F2"
        sub_id = "wiim_uuid:B1B2C3D4-E5F6-A7B8-C9D0-E1F2B1B2C3D4"
        member_slaves = [sub_http]

        async def _sub_leave() -> None:
            member_slaves.remove(sub_http)

        sub_client = _member_client()
        sub_client.leave_group = AsyncMock(side_effect=_sub_leave)
        sub = _make_player(sub_id, BACKEND_GENERIC, command_client=sub_client)
        member_client = _leader_client(member_slaves)
        new_slaves: list[str] = []
        member_client.join_slave = AsyncMock(
            side_effect=lambda *_a, **_k: new_slaves.append(FOLLOWER_HTTP_UUID)
        )
        member = _make_player(
            FOLLOWER_ID, BACKEND_GENERIC, ip="192.168.1.40", command_client=member_client
        )
        leader = _make_player(
            LEADER_ID,
            BACKEND_OFFICIAL,
            ip="192.168.1.20",
            command_client=_leader_client(new_slaves),
        )
        # NOTE: the coordinator cache is never seeded for the member; only its live read finds sub
        coordinator, _ = _make_coordinator(leader, member, sub)

        await coordinator.set_members(leader, [FOLLOWER_ID], None)

        sub_client.leave_group.assert_awaited_once()
        member_client.join_slave.assert_awaited_once()

    async def test_leader_info_read_once_per_batch(self) -> None:
        """The leader's device info is read once for a batch, not once per added member."""
        slaves: list[str] = []
        leader_client = _leader_client(slaves)
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.30", command_client=leader_client
        )
        first_client = _member_client()
        first_client.join_slave = AsyncMock(
            side_effect=lambda *_a, **_k: slaves.append(FOLLOWER_HTTP_UUID)
        )
        second_client = _member_client()
        second_client.join_slave = AsyncMock(
            side_effect=lambda *_a, **_k: slaves.append(SECOND_HTTP_UUID)
        )
        first = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=first_client)
        second = _make_player(SECOND_ID, BACKEND_GENERIC, command_client=second_client)
        coordinator, _ = _make_coordinator(leader, first, second)

        await coordinator.set_members(leader, [FOLLOWER_ID, SECOND_ID], None)

        assert leader_client.get_device_info_model.await_count == 1

    async def test_refresh_skips_unavailable_device(self) -> None:
        """A device that is unavailable is not polled again; its cached topology is kept."""
        client = MagicMock()
        client.get_device_group_info = AsyncMock()
        player = _make_player(LEADER_ID, BACKEND_GENERIC, available=False, command_client=client)
        coordinator, _ = _make_coordinator(player)

        assert await coordinator.refresh_leader(player, force=True) is False
        client.get_device_group_info.assert_not_called()

    async def test_live_read_reveals_unknown_leader_follower_and_aborts(self) -> None:
        """A member whose cache said standalone but now reads as a slave of an unknown leader fails."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        member_client.get_device_group_info = AsyncMock(
            return_value=MagicMock(role="slave", master_uuid=OTHER_MASTER_UUID)
        )
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        member = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, member)  # cache starts empty (standalone)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_combined_batch_aborts_before_join_when_leader_unreadable(self) -> None:
        """A combined add/remove batch does not join anything if the leader's list is unreadable."""
        leader_client = MagicMock()
        leader_client.capabilities = {}
        leader_client.get_device_group_info = AsyncMock(return_value=MagicMock(role="solo"))
        leader_client.get_slaves_info = AsyncMock(side_effect=WiiMError("offline"))
        leader_client.get_device_info_model = AsyncMock(
            return_value=MagicMock(needs_wifi_direct_multiroom=False, wmrm_version="4.2")
        )
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.30", command_client=leader_client
        )
        add_client = _member_client()
        add_client.join_slave = AsyncMock()
        add_member = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=add_client)
        remove_member = _make_player(SECOND_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader, add_member, remove_member)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], [SECOND_ID])
        add_client.join_slave.assert_not_called()

    async def test_addition_with_unreadable_group_aborts_batch(self) -> None:
        """A batch fails before any join if an addition's live group cannot be read."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        member_client.get_device_group_info = AsyncMock(side_effect=WiiMError("offline"))
        leader = _make_player(
            LEADER_ID, BACKEND_GENERIC, ip="192.168.1.20", command_client=_leader_client([])
        )
        member = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, member)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        member_client.join_slave.assert_not_called()

    async def test_join_surfaces_device_info_failure(self) -> None:
        """A device-info read error surfaces as a chained failure, not a false incompatibility."""
        member_client = _member_client()
        member_client.join_slave = AsyncMock()
        member_client.get_device_info_model = AsyncMock(side_effect=WiiMError("boom"))
        leader = _make_player(
            LEADER_ID, BACKEND_OFFICIAL, ip="192.168.1.20", command_client=_leader_client([])
        )
        member = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=member_client)
        coordinator, _ = _make_coordinator(leader, member)

        with pytest.raises(PlayerCommandFailed) as exc_info:
            await coordinator.set_members(leader, [FOLLOWER_ID], None)
        assert isinstance(exc_info.value.__cause__, WiiMError)
        member_client.join_slave.assert_not_called()

    async def test_unavailable_requester_offers_no_native_peers(self) -> None:
        """A device whose own grouping API is unreachable offers no native candidates."""
        me = _make_player(LEADER_ID, BACKEND_GENERIC, available=False)
        peer = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, peer)

        assert coordinator.can_group_with(me) == set()

    async def test_available_requester_still_offers_peers(self) -> None:
        """A reachable device is unaffected: it still offers its available native peers."""
        me = _make_player(LEADER_ID, BACKEND_GENERIC)
        peer = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(me, peer)

        assert coordinator.can_group_with(me) == {FOLLOWER_ID}

    async def test_add_remove_conflict_rejected(self) -> None:
        """The same id in both add and remove is a contradictory request and is rejected."""
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client([]))
        member = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, member)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID], [FOLLOWER_ID])
        provider.wiim_controller.async_join_group.assert_not_called()

    async def test_duplicate_ids_rejected(self) -> None:
        """A duplicate id within a single add/remove list is rejected up front."""
        leader = _make_player(LEADER_ID, BACKEND_OFFICIAL, command_client=_leader_client([]))
        member = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, provider = _make_coordinator(leader, member)

        with pytest.raises(PlayerCommandFailed):
            await coordinator.set_members(leader, [FOLLOWER_ID, FOLLOWER_ID], None)
        provider.wiim_controller.async_join_group.assert_not_called()

    async def test_newer_observation_wins_ownership_despite_later_apply(self) -> None:
        """A follower belongs to the most recently OBSERVED leader, not the last one applied."""
        other_leader_id = "wiim_uuid:22222222-3333-4444-5555-666666666666"
        leader_a = _make_player(LEADER_ID, BACKEND_GENERIC)
        leader_b = _make_player(other_leader_id, BACKEND_GENERIC)
        follower = _make_player(FOLLOWER_ID, BACKEND_GENERIC)
        coordinator, _ = _make_coordinator(leader_a, leader_b, follower)

        # leader B observed the follower more recently, but leader A's older observation is
        # applied afterwards (a longer-running read that completed late)
        coordinator.set_leader_slaves(other_leader_id, [FOLLOWER_HTTP_UUID], observed_at=200.0)
        coordinator.set_leader_slaves(LEADER_ID, [FOLLOWER_HTTP_UUID], observed_at=100.0)
        await coordinator.reconcile()

        assert coordinator.leader_of(FOLLOWER_ID) == other_leader_id

    async def test_republish_all_republishes_every_native_player(self) -> None:
        """schedule_republish's work re-publishes every registered native player's state."""
        leader = _make_player(LEADER_ID, BACKEND_GENERIC)
        peer = _make_player(FOLLOWER_ID, BACKEND_OFFICIAL)
        coordinator, _ = _make_coordinator(leader, peer)

        coordinator._republish_all()

        leader.on_native_group_update.assert_called_once()
        peer.on_native_group_update.assert_called_once()

    async def test_removal_of_absent_unreachable_member_is_skipped_not_aborted(self) -> None:
        """An unreachable removal target the leader no longer lists is skipped, not fatal."""
        slaves = [FOLLOWER_HTTP_UUID]  # the leader currently lists only the reachable member
        good_client = _member_client()

        async def _leave() -> None:
            slaves.remove(FOLLOWER_HTTP_UUID)

        good_client.leave_group = AsyncMock(side_effect=_leave)
        leader = _make_player(LEADER_ID, BACKEND_GENERIC, command_client=_leader_client(slaves))
        good = _make_player(FOLLOWER_ID, BACKEND_GENERIC, command_client=good_client)
        gone = _make_player(SECOND_ID, BACKEND_GENERIC, available=False)  # unreachable, not owned
        coordinator, _ = _make_coordinator(leader, good, gone)

        # removing both must not abort on the absent, unreachable target
        await coordinator.set_members(leader, None, [FOLLOWER_ID, SECOND_ID])

        good_client.leave_group.assert_awaited_once()
