"""Tests for a stereo pair made in the WiiM Home app, shown as one multi-channel player."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ChannelLayout, PlayerType
from pywiim import WiiMError

from music_assistant.providers.wiim.constants import BACKEND_GENERIC, BACKEND_OFFICIAL
from music_assistant.providers.wiim.grouping import NativeGroupCoordinator, NativeGroupRole
from music_assistant.providers.wiim.helpers import (
    linkplay_slave_uuid_to_player_id,
    native_set_channel,
    native_set_layout,
)
from music_assistant.providers.wiim.linkplay_player import LinkPlayPlayer
from music_assistant.providers.wiim.player import WiimPlayer
from music_assistant.providers.wiim.provider import FOLLOWER_DECISION_DELAY, WiimProvider

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

# The left speaker leads the pair; its uuid is not in the report, so any LinkPlay uuid does.
LEFT_HTTP_UUID = "FF98F52BFDA052FF75BD4475"
LEFT_ID = cast("str", linkplay_slave_uuid_to_player_id(LEFT_HTTP_UUID))
LEFT_IP = "192.168.0.75"
RIGHT_HTTP_UUID = "FF98F52BFDA052FF75BD443A"
RIGHT_ID = cast("str", linkplay_slave_uuid_to_player_id(RIGHT_HTTP_UUID))
RIGHT_IP = "192.168.0.74"

# multiroom:getSlaveList of the left speaker of a pair made in WiiM Home
# (music-assistant/support#6427)
PAIR_LEADER_SLAVE_LIST: dict[str, Any] = {
    "slaves": 1,
    "wmrm_version": "4.3",
    "surround": 0,
    "group_type": 2,
    "slave_list": [
        {
            "name": "Living Room Right",
            "uuid": RIGHT_HTTP_UUID,
            "ip": RIGHT_IP,
            "version": "4.3",
            "type": "WiiMu-AmlogicA1",
            "channel": 2,
            "volume": 20,
            "mute": 0,
            "battery_percent": 0,
            "battery_charging": 0,
            "group_channel": 2,
        }
    ],
}

# multiroom:getSlaveList of the leader of an ordinary multiroom group
# (home-assistant/core#134915)
THIRD_HTTP_UUID = "FF31000121AD81B5D2CDC4D6"
THIRD_ID = cast("str", linkplay_slave_uuid_to_player_id(THIRD_HTTP_UUID))
ORDINARY_SLAVE: dict[str, Any] = {
    "name": "Family Room",
    "uuid": THIRD_HTTP_UUID,
    "ip": "10.250.1.201",
    "version": "4.2",
    "type": "WiiMu-A31",
    "channel": 0,
    "volume": 49,
    "mute": 0,
    "battery_percent": 0,
    "battery_charging": 0,
}


def _slave_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return what pywiim's get_slaves_info() returns for a getSlaveList response."""
    return list(copy.deepcopy(response).get("slave_list", []))


def _client(role: str, slaves: list[dict[str, Any]], master_uuid: str | None = None) -> MagicMock:
    """
    Build a command client of a speaker in the given role.

    :param role: The role pywiim reads for the speaker: master, slave or solo.
    :param slaves: The slave list the speaker reports.
    :param master_uuid: The uuid of its leader, for a slave.
    """
    client = MagicMock(capabilities={"probed": True})
    client.get_device_group_info = AsyncMock(
        return_value=MagicMock(role=role, master_uuid=master_uuid, slave_uuids=[])
    )
    client.get_slaves_info = AsyncMock(return_value=slaves)
    return client


def _native_player(player_id: str, client: MagicMock, ip: str = LEFT_IP) -> MagicMock:
    """Build a fake native player with the surface the coordinator uses."""
    player = MagicMock()
    player.protocol_id = player_id
    player.linkplay_backend = BACKEND_OFFICIAL
    player.native_available = True
    player.native_ip = ip
    player.make_command_client = MagicMock(return_value=client)
    return player


def _coordinator(*players: Any) -> tuple[NativeGroupCoordinator, MagicMock]:
    """Create a coordinator over a provider whose players are the given ones."""
    provider = MagicMock()
    provider.protocols = list(players)
    by_id = {player.protocol_id: player for player in players}
    provider.get_protocol = MagicMock(side_effect=by_id.get)
    return NativeGroupCoordinator(provider), provider


def _published(player: WiimPlayer | LinkPlayPlayer) -> tuple[PlayerType, ChannelLayout | None]:
    """Return the type and channel layout a protocol publishes."""
    return player.type, player.channel_layout


class TestSetRule:
    """A slave whose group_channel is set belongs to its leader's set."""

    def test_the_right_speaker_of_a_pair_belongs_to_the_set(self) -> None:
        """The right speaker of a pair reports its set channel."""
        assert native_set_channel(PAIR_LEADER_SLAVE_LIST["slave_list"][0]) == 2

    def test_a_member_of_an_ordinary_group_belongs_to_no_set(self) -> None:
        """A member of an ordinary multiroom group reports no set channel."""
        assert native_set_channel(ORDINARY_SLAVE) is None

    @pytest.mark.parametrize(
        ("channels", "layout"),
        [
            ([], None),
            ([2], ChannelLayout.STEREO),
            ([1], ChannelLayout.STEREO),
            ([1, 2], ChannelLayout.UNKNOWN),
        ],
    )
    def test_layout(self, channels: list[int], layout: ChannelLayout | None) -> None:
        """A single left or right speaker makes a stereo pair, any other set is unknown."""
        assert native_set_layout(channels) == layout


class TestCoordinator:
    """The coordinator keeps the speakers of a set out of the topology of groups."""

    async def test_the_leader_of_a_pair_is_a_stereo_set_without_group_members(self) -> None:
        """The pair is one stereo set on its leader, which leads no group."""
        left = _native_player(LEFT_ID, _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST)))
        coordinator, _ = _coordinator(left)

        assert await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.members_of(LEFT_ID) == []
        assert coordinator.role_of(LEFT_ID) == NativeGroupRole.STANDALONE
        assert coordinator.is_native_set_member(RIGHT_ID)
        left.on_native_group_update.assert_called()

    async def test_an_ordinary_group_is_no_set(self) -> None:
        """The leader of an ordinary group has the group members and no layout."""
        leader = _native_player(LEFT_ID, _client("master", [copy.deepcopy(ORDINARY_SLAVE)]))
        third = _native_player(THIRD_ID, _client("slave", [], LEFT_HTTP_UUID))
        coordinator, _ = _coordinator(leader, third)

        await coordinator.refresh_leader(leader, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) is None
        assert coordinator.members_of(LEFT_ID) == [LEFT_ID, THIRD_ID]
        assert not coordinator.is_native_set_member(THIRD_ID)

    async def test_a_pair_leads_an_ordinary_group(self) -> None:
        """A third speaker grouped with a pair is a group member, the right speaker is not."""
        slaves = [*_slave_list(PAIR_LEADER_SLAVE_LIST), copy.deepcopy(ORDINARY_SLAVE)]
        left = _native_player(LEFT_ID, _client("master", slaves))
        third = _native_player(THIRD_ID, _client("slave", [], LEFT_HTTP_UUID))
        coordinator, _ = _coordinator(left, third)

        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.members_of(LEFT_ID) == [LEFT_ID, THIRD_ID]
        assert coordinator.leader_of(THIRD_ID) == LEFT_ID

    async def test_a_registered_set_speaker_is_no_group_member_nor_grouping_candidate(
        self,
    ) -> None:
        """A set speaker that is still registered stays out of the leader's group and candidates."""
        left = _native_player(LEFT_ID, _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST)))
        right = _native_player(RIGHT_ID, _client("slave", [], LEFT_HTTP_UUID), RIGHT_IP)
        coordinator, _ = _coordinator(left, right)

        await coordinator.refresh_leader(left, force=True)

        assert coordinator.members_of(LEFT_ID) == []
        assert RIGHT_ID not in coordinator.can_group_with(left)

    async def test_a_registered_set_speaker_reads_as_follower_of_an_unknown_leader(
        self,
    ) -> None:
        """A set speaker that is still registered follows a leader whose group it is not in."""
        left = _native_player(LEFT_ID, _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST)))
        right = _native_player(RIGHT_ID, _client("slave", [], LEFT_HTTP_UUID), RIGHT_IP)
        coordinator, _ = _coordinator(left, right)

        await coordinator.refresh_leader(left, force=True)
        await coordinator.refresh_leader(right, force=True)

        assert coordinator.role_of(RIGHT_ID) == NativeGroupRole.FOLLOWER
        assert coordinator.is_unknown_leader_follower(RIGHT_ID)

    async def test_taking_the_pair_apart_looks_for_the_right_speaker_again(self) -> None:
        """The leader is a plain speaker again and discovery looks for the other speaker."""
        client = _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST))
        left = _native_player(LEFT_ID, client)
        coordinator, provider = _coordinator(left)
        await coordinator.refresh_leader(left, force=True)

        # the left speaker after the pair was taken apart in WiiM Home
        client.get_device_group_info.return_value = MagicMock(role="solo", slave_uuids=[])
        client.get_slaves_info.return_value = _slave_list(
            {"slaves": 0, "wmrm_version": "4.3", "surround": 0, "group_type": -1}
        )
        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) is None
        assert not coordinator.is_native_set_member(RIGHT_ID)
        provider.schedule_discovery.assert_called_once_with()

    async def test_a_pair_that_joins_another_group_stays_a_set(self) -> None:
        """The leader of a pair that follows another leader keeps its set."""
        client = _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST))
        left = _native_player(LEFT_ID, client)
        coordinator, provider = _coordinator(left)
        await coordinator.refresh_leader(left, force=True)

        client.get_device_group_info.return_value = MagicMock(
            role="slave", master_uuid=THIRD_HTTP_UUID, slave_uuids=[]
        )
        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.is_native_set_member(RIGHT_ID)
        provider.schedule_discovery.assert_not_called()

    async def test_a_registered_set_speaker_is_not_looked_for_again(self) -> None:
        """A speaker that left the set while it is registered needs no discovery."""
        client = _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST))
        left = _native_player(LEFT_ID, client)
        right = _native_player(RIGHT_ID, _client("solo", []), RIGHT_IP)
        coordinator, provider = _coordinator(left, right)
        await coordinator.refresh_leader(left, force=True)

        client.get_slaves_info.return_value = []
        await coordinator.refresh_leader(left, force=True)

        provider.schedule_discovery.assert_not_called()


class TestFollowsUnreadLeader:
    """A follower is decided on once the slave list of its leader was read."""

    async def test_a_follower_of_a_leader_that_was_read(self) -> None:
        """A follower of a leader whose slave list was read is decided on now."""
        left = _native_player(LEFT_ID, _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST)))
        coordinator, _ = _coordinator(left)
        await coordinator.refresh_leader(left, force=True)

        assert not await coordinator.follows_unread_leader(_client("slave", [], LEFT_HTTP_UUID))

    async def test_a_follower_of_a_leader_that_was_not_read(self) -> None:
        """A follower of a leader that was not read waits for that read."""
        coordinator, _ = _coordinator()

        assert await coordinator.follows_unread_leader(_client("slave", [], LEFT_HTTP_UUID))

    async def test_a_speaker_that_follows_nobody(self) -> None:
        """A speaker that follows no leader is decided on now."""
        coordinator, _ = _coordinator()

        assert not await coordinator.follows_unread_leader(_client("solo", []))

    async def test_a_speaker_that_does_not_answer(self) -> None:
        """A speaker whose group state cannot be read is decided on now, as before."""
        coordinator, _ = _coordinator()
        client = _client("slave", [], LEFT_HTTP_UUID)
        client.get_device_group_info.side_effect = WiiMError("timeout")

        assert not await coordinator.follows_unread_leader(client)


class TestPublishedType:
    """Both backends publish the type and layout of the set they lead."""

    async def test_official_leader(self) -> None:
        """An official leader of a pair is a stereo multi-channel player, and a plain one after."""
        provider = MagicMock()
        provider.native_groups = coordinator = NativeGroupCoordinator(provider)
        device = MagicMock(available=True, ip_address=LEFT_IP, play_mode=None, current_media=None)
        device.name = "Living Room Left"
        player = WiimPlayer(
            provider,
            LEFT_ID,
            MagicMock(
                friendly_name="Living Room Left",
                model_name="WiiM Sound",
                manufacturer="Linkplay",
                udn=f"uuid:{LEFT_HTTP_UUID}",
            ),
            f"http://{LEFT_IP}:49152/description.xml",
            LEFT_IP,
        )
        player._device = device
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        provider.protocols = [player]
        provider.get_protocol = MagicMock(return_value=player)
        client = _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST))

        with patch.object(player, "make_command_client", return_value=client):
            await coordinator.refresh_leader(player, force=True)
            paired = _published(player)
            client.get_slaves_info.return_value = []
            await coordinator.refresh_leader(player, force=True)
            apart = _published(player)

        assert paired == (PlayerType.MULTI_CHANNEL, ChannelLayout.STEREO)
        assert apart == (PlayerType.PLAYER, None)

    async def test_generic_leader(self) -> None:
        """A generic leader of a pair is a stereo multi-channel player."""
        provider = MagicMock()
        provider.native_groups = coordinator = NativeGroupCoordinator(provider)
        client = _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST))
        client.host = LEFT_IP
        player = LinkPlayPlayer(
            provider,
            LEFT_ID,
            client,
            MagicMock(friendly_name="Living Room Left"),
            f"http://{LEFT_IP}:49152/description.xml",
            device_info=MagicMock(),
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        provider.protocols = [player]
        provider.get_protocol = MagicMock(return_value=player)
        assert player.linkplay_backend == BACKEND_GENERIC

        await coordinator.refresh_leader(player, force=True)

        assert _published(player) == (PlayerType.MULTI_CHANNEL, ChannelLayout.STEREO)


class TestDiscovery:
    """Discovery adds no player for a speaker of a set."""

    @pytest.fixture
    def provider(self) -> MagicMock:
        """Return a provider whose discovery finds the right speaker of the pair."""
        provider = MagicMock()
        provider.protocols = []
        provider.get_protocol = MagicMock(return_value=None)
        provider.native_groups = NativeGroupCoordinator(provider)
        provider.mass.players.wants_protocol = MagicMock(return_value=True)
        provider._probe_locations = AsyncMock(
            return_value=(
                MagicMock(
                    udn=RIGHT_ID.removeprefix("wiim_"),
                    manufacturer="Linkplay",
                    model_name="WiiM Sound",
                ),
                f"http://{RIGHT_IP}:49152/description.xml",
            )
        )
        provider.try_add_player = AsyncMock()
        provider.try_add_linkplay_player = AsyncMock()
        return provider

    async def test_a_speaker_of_a_known_set_is_not_added(self, provider: MagicMock) -> None:
        """A speaker the leader reports in its set gets no player and no request."""
        left = _native_player(LEFT_ID, _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST)))
        provider.protocols = [left]
        provider.get_protocol = MagicMock(side_effect={LEFT_ID: left}.get)
        await provider.native_groups.refresh_leader(left, force=True)

        with patch(
            "music_assistant.providers.wiim.provider.WiiMClient",
            return_value=_client("solo", []),
        ) as client_class:
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,))

        client_class.assert_not_called()
        provider.try_add_player.assert_not_awaited()
        provider.try_add_linkplay_player.assert_not_awaited()

    async def test_a_follower_of_an_unread_leader_is_looked_at_again(
        self, provider: MagicMock
    ) -> None:
        """A follower whose leader was not read yet is looked at again later, not added now."""
        client = _client("slave", [], LEFT_HTTP_UUID)
        with patch("music_assistant.providers.wiim.provider.WiiMClient", return_value=client):
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,), "AA:BB")

        provider.try_add_player.assert_not_awaited()
        provider.try_add_linkplay_player.assert_not_awaited()
        provider.mass.call_later.assert_called_once_with(
            FOLLOWER_DECISION_DELAY,
            provider._discover_device,
            RIGHT_IP,
            (RIGHT_IP,),
            "AA:BB",
            task_id=f"wiim_follower_{RIGHT_IP}",
            follower_deferred=True,
        )

    async def test_a_follower_looked_at_again_is_added_when_still_undecided(
        self, provider: MagicMock
    ) -> None:
        """A follower whose leader is still unknown at the second look is added, as before."""
        with patch("music_assistant.providers.wiim.provider.WiiMClient") as client_class:
            await WiimProvider._discover_device(
                provider, RIGHT_IP, (RIGHT_IP,), follower_deferred=True
            )

        client_class.assert_not_called()
        provider.try_add_player.assert_awaited_once()

    async def test_a_speaker_that_follows_nobody_is_added(self, provider: MagicMock) -> None:
        """A speaker that follows no leader is added at once."""
        with patch(
            "music_assistant.providers.wiim.provider.WiiMClient",
            return_value=_client("solo", []),
        ):
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,))

        provider.mass.call_later.assert_not_called()
        provider.try_add_player.assert_awaited_once()

    async def test_a_speaker_of_a_disabled_player_is_not_contacted(
        self, provider: MagicMock
    ) -> None:
        """Discovery sends no request to a speaker whose player is disabled."""
        provider.mass.players.wants_protocol = MagicMock(return_value=False)
        with patch(
            "music_assistant.providers.wiim.provider.WiiMClient",
            return_value=_client("solo", []),
        ) as client_class:
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,))

        client_class.assert_not_called()
        provider.try_add_player.assert_not_awaited()


async def test_the_player_of_a_pair_is_published_as_a_stereo_multi_channel_player(
    scenario: Scenario,
) -> None:
    """The player of the leader of a pair shows as one stereo multi-channel player."""
    wiim: Any = await scenario.add_provider("wiim")
    wiim.wiim_controller = MagicMock(add_device=AsyncMock(), remove_device=AsyncMock())
    wiim.native_groups = NativeGroupCoordinator(wiim)
    device = MagicMock(
        udn=f"uuid:{LEFT_HTTP_UUID}",
        ip_address=LEFT_IP,
        model_name="WiiM Sound",
        manufacturer="Linkplay",
        firmware_version="5.2.1",
        available=True,
        supports_http_api=True,
        supported_input_modes=("Network",),
        volume=20,
        is_muted=False,
        play_mode=None,
        current_media=None,
        async_update_http_status=AsyncMock(),
        sync_device_duration_and_position=AsyncMock(),
        disconnect=AsyncMock(),
    )
    device.name = "Living Room Left"
    protocol = WiimPlayer(
        wiim,
        LEFT_ID,
        MagicMock(
            friendly_name="Living Room Left",
            model_name="WiiM Sound",
            manufacturer="Linkplay",
            udn=f"uuid:{LEFT_HTTP_UUID}",
        ),
        f"http://{LEFT_IP}:49152/description.xml",
        LEFT_IP,
    )
    client = _client("master", _slave_list(PAIR_LEADER_SLAVE_LIST))
    with (
        patch(
            "music_assistant.providers.wiim.player.async_create_wiim_device",
            AsyncMock(return_value=device),
        ),
        patch.object(protocol, "make_command_client", return_value=client),
    ):
        await scenario.mass.players.register(protocol)
        await scenario.settle()

    player = protocol.player
    assert player is not None
    assert player.type == PlayerType.MULTI_CHANNEL
    assert player.channel_layout == ChannelLayout.STEREO
    assert player.group_members == []
