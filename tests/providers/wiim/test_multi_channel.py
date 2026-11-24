"""Tests for a stereo pair made in the WiiM Home app, shown as one multi-channel player."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import ChannelLayout, PlayerType
from pywiim import WiiMError

from music_assistant.providers.wiim.constants import (
    BACKEND_GENERIC,
    BACKEND_OFFICIAL,
    CACHE_CATEGORY_NATIVE_SETS,
)
from music_assistant.providers.wiim.grouping import NativeGroupCoordinator, NativeGroupRole
from music_assistant.providers.wiim.helpers import (
    linkplay_slave_uuid_to_player_id,
    native_set_channel,
    native_set_layout,
    plays_set_channel,
)
from music_assistant.providers.wiim.linkplay_player import LinkPlayPlayer
from music_assistant.providers.wiim.player import WiimPlayer
from music_assistant.providers.wiim.provider import FOLLOWER_DECISION_DELAY, WiimProvider

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

LEFT_UUID = "FF98F52BFDA052FF75BD4475"
LEFT_ID = cast("str", linkplay_slave_uuid_to_player_id(LEFT_UUID))
LEFT_IP = "192.0.2.11"
RIGHT_UUID = "FF98F52BFDA052FF75BD443A"
RIGHT_ID = cast("str", linkplay_slave_uuid_to_player_id(RIGHT_UUID))
RIGHT_IP = "192.0.2.12"
THIRD_UUID = "FF31000121AD81B5D2CDC4D6"
THIRD_ID = cast("str", linkplay_slave_uuid_to_player_id(THIRD_UUID))
THIRD_IP = "192.0.2.13"
INSTANCE_ID = "wiim--test"


def _slave(uuid: str, ip: str, channel: int, group_channel: int) -> dict[str, Any]:
    """
    Return one entry of a multiroom:getSlaveList response of a WiiM speaker (wmrm 4.3).

    :param uuid: The uuid of the slave.
    :param ip: The address of the slave.
    :param channel: The channel the slave plays: 0 stereo, 1 left, 2 right.
    :param group_channel: The channel of the slave in its leader's native set, -1 for none.
    """
    return {
        "name": "Speaker",
        "uuid": uuid,
        "ip": ip,
        "version": "4.3",
        "type": "WiiMu-AmlogicA1",
        "channel": channel,
        "volume": 20,
        "mute": 0,
        "battery_percent": 0,
        "battery_charging": 0,
        "group_channel": group_channel,
    }


# The slave lists the reporter of music-assistant/support#6427 read. The left speaker leads the
# pair; the third speaker is a speaker of its own.
def _pair_alone() -> list[dict[str, Any]]:
    """Return the list of the left speaker of a pair made in WiiM Home (group_type 2)."""
    return [_slave(RIGHT_UUID, RIGHT_IP, 2, 2)]


def _ordinary_group() -> list[dict[str, Any]]:
    """Return the list of the left speaker in an ordinary group of the two (group_type 1)."""
    return [_slave(RIGHT_UUID, RIGHT_IP, 0, -1)]


def _pair_leads_group() -> list[dict[str, Any]]:
    """Return the list of the left speaker while the pair leads a group with the third one."""
    return [_slave(RIGHT_UUID, RIGHT_IP, 2, -1), _slave(THIRD_UUID, THIRD_IP, 0, -1)]


def _pair_follows_third() -> list[dict[str, Any]]:
    """Return the list of the third speaker while the pair follows it."""
    return [_slave(RIGHT_UUID, RIGHT_IP, 2, -1), _slave(LEFT_UUID, LEFT_IP, 1, -1)]


def _hand_set_channel() -> list[dict[str, Any]]:
    """Return the list of the left speaker in an ordinary group with the right channel set."""
    return [_slave(RIGHT_UUID, RIGHT_IP, 2, -1)]


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


def _read(client: MagicMock, role: str, slaves: list[dict[str, Any]]) -> None:
    """
    Let the next read of a speaker return the given role and slave list.

    :param client: The command client of the speaker.
    :param role: The role pywiim reads for the speaker.
    :param slaves: The slave list the speaker reports.
    """
    master_uuid = THIRD_UUID if role == "slave" else None
    client.get_device_group_info.return_value = MagicMock(
        role=role, master_uuid=master_uuid, slave_uuids=[]
    )
    client.get_slaves_info.return_value = slaves


def _native_player(player_id: str, client: MagicMock, ip: str = LEFT_IP) -> MagicMock:
    """Build a fake native player with the surface the coordinator uses."""
    player = MagicMock()
    player.protocol_id = player_id
    player.linkplay_backend = BACKEND_OFFICIAL
    player.native_available = True
    player.native_ip = ip
    player.make_command_client = MagicMock(return_value=client)
    return player


def _provider(*players: Any, stored: dict[str, Any] | None = None) -> MagicMock:
    """
    Return a provider whose players are the given ones, with a coordinator of its own.

    :param players: The registered players.
    :param stored: The native sets the cache holds from before a restart.
    """
    provider = MagicMock()
    provider.instance_id = INSTANCE_ID
    provider.protocols = list(players)
    by_id = {player.protocol_id: player for player in players}
    provider.get_protocol = MagicMock(side_effect=by_id.get)
    mass = provider.mass
    mass.cache.get_all = AsyncMock(return_value=dict(stored or {}))
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()
    mass.players.remove_absorbed_players = AsyncMock()
    # the removal runs as a task of its own; the call itself is what the tests check
    mass.create_task = MagicMock(side_effect=lambda coro: coro.close())
    provider.native_groups = NativeGroupCoordinator(provider)
    return provider


def _coordinator(*players: Any) -> tuple[NativeGroupCoordinator, MagicMock]:
    """Create a coordinator over a provider whose players are the given ones."""
    provider = _provider(*players)
    return provider.native_groups, provider


def _published(player: WiimPlayer | LinkPlayPlayer) -> tuple[PlayerType, ChannelLayout | None]:
    """Return the type and channel layout a protocol publishes."""
    return player.type, player.channel_layout


class TestSetRule:
    """Only a set channel identifies a set slave; left or right keeps it."""

    def test_the_pair_alone_identifies_its_right_speaker(self) -> None:
        """The leader of a pair that plays alone gives its right speaker a set channel."""
        assert native_set_channel(_pair_alone()[0]) == 2

    @pytest.mark.parametrize(
        "slaves", [_ordinary_group, _pair_leads_group, _hand_set_channel], ids=lambda f: f.__name__
    )
    def test_no_set_channel(self, slaves: Any) -> None:
        """No other list gives a set channel."""
        assert native_set_channel(slaves()[0]) is None

    def test_left_or_right_keeps_a_slave(self) -> None:
        """A slave that plays left or right can stay in a set, one that plays stereo can not."""
        assert plays_set_channel(_pair_leads_group()[0])
        assert not plays_set_channel(_ordinary_group()[0])

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


class TestNativeSet:
    """The coordinator identifies, keeps and releases the speakers of a set."""

    async def test_the_pair_alone_is_one_stereo_set(self) -> None:
        """The pair is one stereo set on its leader, which leads no group."""
        left = _native_player(LEFT_ID, _client("master", _pair_alone()))
        coordinator, provider = _coordinator(left)

        assert await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.members_of(LEFT_ID) == []
        assert coordinator.role_of(LEFT_ID) == NativeGroupRole.STANDALONE
        assert coordinator.is_native_set_member(RIGHT_ID)
        left.on_native_group_update.assert_called()
        provider.mass.cache.set.assert_awaited_once_with(
            LEFT_ID,
            {RIGHT_UUID: 2},
            expiration=86400 * 3650,
            provider=INSTANCE_ID,
            category=CACHE_CATEGORY_NATIVE_SETS,
            persistent=True,
        )

    async def test_the_identification_removes_the_player_of_the_right_speaker_once(
        self,
    ) -> None:
        """The player of the right speaker is removed once, on the identification only."""
        client = _client("master", _pair_alone())
        left = _native_player(LEFT_ID, client)
        coordinator, provider = _coordinator(left)
        remove = provider.mass.players.remove_absorbed_players

        await coordinator.refresh_leader(left, force=True)
        await coordinator.refresh_leader(left, force=True)
        _read(client, "master", _pair_leads_group())
        await coordinator.refresh_leader(left, force=True)
        _read(client, "slave", [])
        await coordinator.refresh_leader(left, force=True)

        remove.assert_called_once_with(LEFT_ID, [RIGHT_ID])

    async def test_a_set_speaker_that_got_a_player_later_is_removed_at_the_next_read(
        self,
    ) -> None:
        """A set speaker registered after its identification is reported for removal again."""
        left = _native_player(LEFT_ID, _client("master", _pair_alone()))
        coordinator, provider = _coordinator(left)
        remove = provider.mass.players.remove_absorbed_players
        await coordinator.refresh_leader(left, force=True)

        right = _native_player(RIGHT_ID, _client("slave", [], LEFT_UUID), RIGHT_IP)
        provider.protocols = [left, right]
        await coordinator.refresh_leader(left, force=True)

        assert remove.call_args_list == [call(LEFT_ID, [RIGHT_ID]), call(LEFT_ID, [RIGHT_ID])]

    async def test_an_ordinary_group_of_the_two_is_no_set(self) -> None:
        """An ordinary group of the two speakers has the right speaker as a member."""
        left = _native_player(LEFT_ID, _client("master", _ordinary_group()))
        right = _native_player(RIGHT_ID, _client("slave", [], LEFT_UUID), RIGHT_IP)
        coordinator, provider = _coordinator(left, right)

        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) is None
        assert coordinator.members_of(LEFT_ID) == [LEFT_ID, RIGHT_ID]
        provider.mass.players.remove_absorbed_players.assert_not_called()

    async def test_a_channel_set_by_hand_never_identifies_a_set(self) -> None:
        """A speaker set to the right channel in an ordinary group stays a group member."""
        left = _native_player(LEFT_ID, _client("master", _hand_set_channel()))
        right = _native_player(RIGHT_ID, _client("slave", [], LEFT_UUID), RIGHT_IP)
        coordinator, provider = _coordinator(left, right)

        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) is None
        assert not coordinator.is_native_set_member(RIGHT_ID)
        assert coordinator.members_of(LEFT_ID) == [LEFT_ID, RIGHT_ID]
        provider.mass.players.remove_absorbed_players.assert_not_called()

    async def test_the_pair_keeps_its_set_while_it_leads_a_group(self) -> None:
        """A pair that leads a group stays a set, with the third speaker as its member."""
        client = _client("master", _pair_alone())
        left = _native_player(LEFT_ID, client)
        third = _native_player(THIRD_ID, _client("slave", [], LEFT_UUID), THIRD_IP)
        coordinator, provider = _coordinator(left, third)
        await coordinator.refresh_leader(left, force=True)

        _read(client, "master", _pair_leads_group())
        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.members_of(LEFT_ID) == [LEFT_ID, THIRD_ID]
        assert coordinator.is_native_set_member(RIGHT_ID)
        provider.schedule_discovery.assert_not_called()

    async def test_the_pair_keeps_its_set_while_it_follows_the_third_speaker(self) -> None:
        """A pair that follows the third speaker stays a set and its right speaker no member."""
        client = _client("master", _pair_alone())
        left = _native_player(LEFT_ID, client)
        # the player the right speaker had before the pair was found, not removed yet
        right = _native_player(RIGHT_ID, _client("slave", [], THIRD_UUID), RIGHT_IP)
        third = _native_player(THIRD_ID, _client("master", _pair_follows_third()), THIRD_IP)
        coordinator, provider = _coordinator(left, right, third)
        await coordinator.refresh_leader(left, force=True)

        _read(client, "slave", [])
        await coordinator.refresh_leader(left, force=True)
        await coordinator.refresh_leader(third, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.members_of(THIRD_ID) == [THIRD_ID, LEFT_ID]
        assert RIGHT_ID not in coordinator.can_group_with(third)
        provider.schedule_discovery.assert_not_called()

    async def test_a_registered_set_speaker_is_no_group_member_nor_grouping_candidate(
        self,
    ) -> None:
        """A set speaker that is still registered stays out of the leader's group and candidates."""
        left = _native_player(LEFT_ID, _client("master", _pair_alone()))
        right = _native_player(RIGHT_ID, _client("slave", [], LEFT_UUID), RIGHT_IP)
        coordinator, _ = _coordinator(left, right)

        await coordinator.refresh_leader(left, force=True)

        assert coordinator.members_of(LEFT_ID) == []
        assert RIGHT_ID not in coordinator.can_group_with(left)

    @pytest.mark.parametrize("slaves", [_ordinary_group, list], ids=["ordinary_group", "alone"])
    async def test_taking_the_pair_apart_releases_the_right_speaker(self, slaves: Any) -> None:
        """A pair taken apart is a plain speaker again and discovery looks for the other one."""
        client = _client("master", _pair_alone())
        left = _native_player(LEFT_ID, client)
        coordinator, provider = _coordinator(left)
        await coordinator.refresh_leader(left, force=True)

        _read(client, "master" if slaves() else "solo", slaves())
        await coordinator.refresh_leader(left, force=True)

        assert coordinator.channel_layout_of(LEFT_ID) is None
        assert not coordinator.is_native_set_member(RIGHT_ID)
        provider.schedule_discovery.assert_called_once_with()
        provider.mass.cache.delete.assert_awaited_once_with(
            LEFT_ID, category=CACHE_CATEGORY_NATIVE_SETS, provider=INSTANCE_ID
        )

    async def test_a_registered_released_speaker_is_not_looked_for_again(self) -> None:
        """A speaker that left the set while it is registered needs no discovery."""
        client = _client("master", _pair_alone())
        left = _native_player(LEFT_ID, client)
        right = _native_player(RIGHT_ID, _client("solo", []), RIGHT_IP)
        coordinator, provider = _coordinator(left, right)
        await coordinator.refresh_leader(left, force=True)

        _read(client, "solo", [])
        await coordinator.refresh_leader(left, force=True)

        provider.schedule_discovery.assert_not_called()

    async def test_a_failed_read_changes_nothing(self) -> None:
        """A read that fails keeps the set and removes no player."""
        client = _client("master", _pair_alone())
        left = _native_player(LEFT_ID, client)
        coordinator, provider = _coordinator(left)
        client.get_slaves_info.side_effect = WiiMError("timeout")

        assert not await coordinator.refresh_leader(left, force=True)

        assert not coordinator.is_native_set_member(RIGHT_ID)
        provider.mass.players.remove_absorbed_players.assert_not_called()


class TestRestart:
    """A set known before a restart is known again after it."""

    async def test_the_pair_that_leads_a_group_is_known_after_a_restart(self) -> None:
        """A pair that leads a group over a restart stays a set, without a removal."""
        left = _native_player(LEFT_ID, _client("master", _pair_leads_group()))
        third = _native_player(THIRD_ID, _client("slave", [], LEFT_UUID), THIRD_IP)
        provider = _provider(left, third, stored={LEFT_ID: {RIGHT_UUID: 2}})
        coordinator: NativeGroupCoordinator = provider.native_groups

        await coordinator.load_native_sets()
        known_before_read = coordinator.is_native_set_member(RIGHT_ID)
        await coordinator.refresh_leader(left, force=True)

        provider.mass.cache.get_all.assert_awaited_once_with(
            provider=INSTANCE_ID, category=CACHE_CATEGORY_NATIVE_SETS
        )
        assert known_before_read
        assert coordinator.channel_layout_of(LEFT_ID) == ChannelLayout.STEREO
        assert coordinator.members_of(LEFT_ID) == [LEFT_ID, THIRD_ID]
        provider.mass.players.remove_absorbed_players.assert_not_called()
        provider.mass.cache.set.assert_not_awaited()

    async def test_the_provider_restores_the_sets_at_its_start(self) -> None:
        """The provider knows the sets from before a restart once it started."""
        provider = _provider(stored={LEFT_ID: {RIGHT_UUID: 2}})
        provider.logger.isEnabledFor.return_value = False
        provider.logger.level = 20

        await WiimProvider.handle_async_init(provider)

        assert provider.native_groups.is_native_set_member(RIGHT_ID)

    async def test_the_right_speaker_is_not_added_after_a_restart(self) -> None:
        """Discovery adds no player for a set speaker known from before a restart."""
        provider = _discovery_provider(stored={LEFT_ID: {RIGHT_UUID: 2}})
        await provider.native_groups.load_native_sets()

        with patch(
            "music_assistant.providers.wiim.provider.WiiMClient",
            return_value=_client("slave", [], THIRD_UUID),
        ) as client_class:
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,))

        client_class.assert_not_called()
        provider.try_add_player.assert_not_awaited()


class TestFollowsUnreadLeader:
    """A follower is decided on once the slave list of its leader was read."""

    async def test_a_follower_of_a_leader_that_was_read(self) -> None:
        """A follower of a leader whose slave list was read is decided on now."""
        left = _native_player(LEFT_ID, _client("master", _pair_alone()))
        coordinator, _ = _coordinator(left)
        await coordinator.refresh_leader(left, force=True)

        assert not await coordinator.follows_unread_leader(_client("slave", [], LEFT_UUID))

    async def test_a_follower_of_a_leader_that_was_not_read(self) -> None:
        """A follower of a leader that was not read waits for that read."""
        coordinator, _ = _coordinator()

        assert await coordinator.follows_unread_leader(_client("slave", [], LEFT_UUID))

    async def test_a_speaker_that_follows_nobody(self) -> None:
        """A speaker that follows no leader is decided on now."""
        coordinator, _ = _coordinator()

        assert not await coordinator.follows_unread_leader(_client("solo", []))

    async def test_a_speaker_that_does_not_answer(self) -> None:
        """A speaker whose group state cannot be read is decided on now, as before."""
        coordinator, _ = _coordinator()
        client = _client("slave", [], LEFT_UUID)
        client.get_device_group_info.side_effect = WiiMError("timeout")

        assert not await coordinator.follows_unread_leader(client)


class TestPublishedType:
    """Both backends publish the type and layout of the set they lead."""

    async def test_official_leader(self) -> None:
        """An official leader of a pair is a stereo multi-channel player, and a plain one after."""
        provider = _provider()
        coordinator: NativeGroupCoordinator = provider.native_groups
        device = MagicMock(available=True, ip_address=LEFT_IP, play_mode=None, current_media=None)
        device.name = "Left"
        player = WiimPlayer(
            provider,
            LEFT_ID,
            MagicMock(
                friendly_name="Left",
                model_name="WiiM Sound",
                manufacturer="Linkplay",
                udn=f"uuid:{LEFT_UUID}",
            ),
            f"http://{LEFT_IP}:49152/description.xml",
            LEFT_IP,
        )
        player._device = device
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        provider.protocols = [player]
        provider.get_protocol = MagicMock(return_value=player)
        client = _client("master", _pair_alone())

        with patch.object(player, "make_command_client", return_value=client):
            await coordinator.refresh_leader(player, force=True)
            paired = _published(player)
            _read(client, "solo", [])
            await coordinator.refresh_leader(player, force=True)
            apart = _published(player)

        assert paired == (PlayerType.MULTI_CHANNEL, ChannelLayout.STEREO)
        assert apart == (PlayerType.PLAYER, None)

    async def test_generic_leader(self) -> None:
        """A generic leader of a pair is a stereo multi-channel player."""
        provider = _provider()
        coordinator: NativeGroupCoordinator = provider.native_groups
        client = _client("master", _pair_alone())
        client.host = LEFT_IP
        player = LinkPlayPlayer(
            provider,
            LEFT_ID,
            client,
            MagicMock(friendly_name="Left"),
            f"http://{LEFT_IP}:49152/description.xml",
            device_info=MagicMock(),
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        provider.protocols = [player]
        provider.get_protocol = MagicMock(return_value=player)
        assert player.linkplay_backend == BACKEND_GENERIC

        await coordinator.refresh_leader(player, force=True)

        assert _published(player) == (PlayerType.MULTI_CHANNEL, ChannelLayout.STEREO)


def _discovery_provider(stored: dict[str, Any] | None = None) -> MagicMock:
    """
    Return a provider whose discovery finds the right speaker of the pair.

    :param stored: The native sets the cache holds from before a restart.
    """
    provider = _provider(stored=stored)
    provider.get_protocol = MagicMock(return_value=None)
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


class TestDiscovery:
    """Discovery adds no player for a speaker of a set."""

    @pytest.fixture
    def provider(self) -> MagicMock:
        """Return a provider whose discovery finds the right speaker of the pair."""
        return _discovery_provider()

    async def test_a_speaker_of_a_known_set_is_not_added(self, provider: MagicMock) -> None:
        """A speaker the leader reports in its set gets no player and no request."""
        left = _native_player(LEFT_ID, _client("master", _pair_alone()))
        provider.protocols = [left]
        provider.get_protocol = MagicMock(side_effect={LEFT_ID: left}.get)
        await provider.native_groups.refresh_leader(left, force=True)
        provider.get_protocol = MagicMock(return_value=None)

        with patch(
            "music_assistant.providers.wiim.provider.WiiMClient",
            return_value=_client("solo", []),
        ) as client_class:
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,))

        client_class.assert_not_called()
        provider.try_add_player.assert_not_awaited()
        provider.try_add_linkplay_player.assert_not_awaited()

    async def test_a_speaker_its_leader_identifies_while_it_is_asked_is_not_added(
        self, provider: MagicMock
    ) -> None:
        """A speaker whose leader is read while the speaker itself is asked gets no player."""
        left = _native_player(LEFT_ID, _client("master", _pair_alone()))
        provider.protocols = [left]
        provider.get_protocol = MagicMock(side_effect={LEFT_ID: left}.get)
        client = _client("slave", [], LEFT_UUID)
        answer: MagicMock = client.get_device_group_info.return_value

        async def _leader_is_read_meanwhile() -> MagicMock:
            await provider.native_groups.refresh_leader(left, force=True)
            return answer

        client.get_device_group_info = AsyncMock(side_effect=_leader_is_read_meanwhile)
        with patch("music_assistant.providers.wiim.provider.WiiMClient", return_value=client):
            await WiimProvider._discover_device(provider, RIGHT_IP, (RIGHT_IP,))

        client.get_device_group_info.assert_awaited_once()
        provider.try_add_player.assert_not_awaited()
        provider.try_add_linkplay_player.assert_not_awaited()

    async def test_a_follower_of_an_unread_leader_is_looked_at_again(
        self, provider: MagicMock
    ) -> None:
        """A follower whose leader was not read yet is looked at again later, not added now."""
        client = _client("slave", [], LEFT_UUID)
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
        udn=f"uuid:{LEFT_UUID}",
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
    device.name = "Left"
    protocol = WiimPlayer(
        wiim,
        LEFT_ID,
        MagicMock(
            friendly_name="Left",
            model_name="WiiM Sound",
            manufacturer="Linkplay",
            udn=f"uuid:{LEFT_UUID}",
        ),
        f"http://{LEFT_IP}:49152/description.xml",
        LEFT_IP,
    )
    client = _client("master", _pair_alone())
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
