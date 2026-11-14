"""A group player takes its members along that are switched off at their power control."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType, ProtocolRole
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_IDLE_POWER_OFF,
    CONF_POWER_CONTROL,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players import power as players_power
from music_assistant.controllers.players.constants import PlayerLockPurpose
from tests.common import (
    MockProtocol,
    MockProvider,
    attach_mock,
    use_real_create_task,
    virtual_loop_clock,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from music_assistant.models.player import Player

# seconds after its control was switched on that a device is back on the network
BOOT_TIME = 2.0


class _Speaker(NamedTuple):
    """A member of the group, the protocol of its device and its power control."""

    player: Player
    protocol: MockProtocol
    control: PlayerControl


class _Setup(NamedTuple):
    """A controller with the group Downstairs and its members, by name."""

    controller: PlayerController
    group: Player
    joined: AsyncMock
    speakers: dict[str, _Speaker]


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get = MagicMock(return_value=[])
    # the log level of the controller: the standard default
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.get_providers = MagicMock(return_value=[])
    return mass


@pytest.fixture(autouse=True)
async def _virtual_clock() -> AsyncIterator[None]:
    # a device comes online, and a wait for it gives up, at a time of the clock
    with virtual_loop_clock():
        yield


def _setup(
    mock_mass: MagicMock,
    *names: str,
    switched_off: tuple[str, ...],
    stays_away: tuple[str, ...] = (),
) -> _Setup:
    """
    Build a controller with the group Downstairs of the named speakers.

    :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
    :param names: The names of the members, which are their player ids.
    :param switched_off: The members that are switched off at their power control.
    :param stays_away: The switched-off members whose device does not come back.
    """
    controls = {name: f"switch.{name}_plug" for name in switched_off}
    mock_mass.config.get_player_value = MagicMock(
        side_effect=lambda player_id, key, default=None: (
            controls.get(player_id, default) if key == CONF_POWER_CONTROL else default
        )
    )
    use_real_create_task(mock_mass)
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
    group, group_protocol = attach_mock(
        provider,
        "proto_downstairs",
        "Downstairs",
        role=ProtocolRole.VIRTUAL,
        player_id="downstairs",
        player_type=PlayerType.GROUP,
    )
    group_protocol._attr_group_members = list(names)
    joined = AsyncMock()
    group_protocol.on_member_powered_on = joined  # type: ignore[method-assign]
    speakers: dict[str, _Speaker] = {}
    for name in names:
        player, protocol = attach_mock(provider, f"proto_{name}", name.title(), player_id=name)
        control = PlayerControl(
            id=f"switch.{name}_plug",
            provider="hass",
            name=f"{name} plug",
            supports_power=True,
            power_state=name not in switched_off,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
        )
        if name in switched_off:
            protocol._attr_available = False
            controller._controls[control.id] = control
            cast("AsyncMock", control.power_on).side_effect = _switch_on(
                player, protocol, control, comes_back=name not in stays_away
            )
        speakers[name] = _Speaker(player, protocol, control)
    for player in (*(speaker.player for speaker in speakers.values()), group):
        player.update_state(signal_event=False, force_update=True)
    return _Setup(controller, group, joined, speakers)


def _switch_on(
    player: Player, protocol: MockProtocol, control: PlayerControl, *, comes_back: bool
) -> AsyncMock:
    """Return the power on of a control, after which the device boots and is back."""

    async def _power_on() -> None:
        control.power_state = True
        player.refresh_state()
        if comes_back:
            asyncio.get_running_loop().call_later(
                BOOT_TIME, setattr, protocol, "_attr_available", True
            )

    return AsyncMock(side_effect=_power_on)


def _locked(controller: PlayerController) -> list[str]:
    """Return the keys of the player locks that are held."""
    return [key for key, lock in controller._player_command_locks.items() if lock.locked()]


async def test_a_group_with_a_member_that_can_play_does_not_wait(mock_mass: MagicMock) -> None:
    """The switched-off member is powered on in the background and joins once it is online."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    den = setup.speakers["den"]
    started = asyncio.get_running_loop().time()

    await setup.controller._power_on_group_members(setup.group)

    assert asyncio.get_running_loop().time() == started
    assert list(setup.controller._member_joins) == ["den"]
    setup.joined.assert_not_awaited()

    await asyncio.sleep(BOOT_TIME / 2)
    den.control.power_on.assert_awaited_once()  # type: ignore[union-attr]
    assert den.player.power_on_in_progress
    # nothing is held while the device is away
    assert _locked(setup.controller) == []

    await asyncio.sleep(BOOT_TIME)
    setup.joined.assert_awaited_once_with(den.player)
    assert setup.controller._member_joins == {}
    assert _locked(setup.controller) == []


async def test_a_group_without_a_member_that_can_play_waits_for_the_first(
    mock_mass: MagicMock,
) -> None:
    """A group whose members are all switched off returns once the first device is back."""
    setup = _setup(
        mock_mass, "kitchen", "den", switched_off=("kitchen", "den"), stays_away=("kitchen",)
    )
    started = asyncio.get_running_loop().time()

    await setup.controller._power_on_group_members(setup.group)

    assert BOOT_TIME <= asyncio.get_running_loop().time() - started < BOOT_TIME + 1
    assert setup.speakers["den"].player.available_for_playback
    # the other one is still waited for, and joins when it comes
    assert setup.controller._member_joins["kitchen"].owner_id == "downstairs"
    setup.controller._cancel_power_on(setup.group)
    await asyncio.sleep(1)


async def test_a_group_whose_members_all_stay_away_fails_as_unavailable(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A group none of whose members comes online fails, and each member is logged once."""
    setup = _setup(
        mock_mass, "kitchen", "den", switched_off=("kitchen", "den"), stays_away=("kitchen", "den")
    )

    with (
        caplog.at_level(logging.WARNING),
        patch.object(players_power, "OFFLINE_POWER_ON_TIMEOUT", 5),
        pytest.raises(PlayerUnavailableError, match="None of the members of Downstairs"),
    ):
        await setup.controller._power_on_group_members(setup.group)
    await asyncio.sleep(1)

    assert sorted(record.getMessage() for record in caplog.records) == [
        "Player Den did not come online after powering it on, it is left out of Downstairs",
        "Player Kitchen did not come online after powering it on, it is left out of Downstairs",
    ]
    assert setup.controller._member_joins == {}
    setup.joined.assert_not_awaited()


async def test_a_member_is_powered_on_once_and_joins_who_took_it_along_last(
    mock_mass: MagicMock,
) -> None:
    """A second request for a member that is powered on already only names its new owner."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    den = setup.speakers["den"]
    provider = MockProvider("other_provider", instance_id="other", mass=mock_mass)
    upstairs, upstairs_protocol = attach_mock(
        provider,
        "proto_upstairs",
        "Upstairs",
        role=ProtocolRole.VIRTUAL,
        player_id="upstairs",
        player_type=PlayerType.GROUP,
    )
    upstairs_protocol.on_member_powered_on = AsyncMock()  # type: ignore[method-assign]

    online = setup.controller._power_on_member(setup.group, den.player)
    assert setup.controller._power_on_member(upstairs, den.player) is online
    assert await online is True
    await asyncio.sleep(1)

    den.control.power_on.assert_awaited_once()  # type: ignore[union-attr]
    upstairs_protocol.on_member_powered_on.assert_awaited_once_with(den.player)
    setup.joined.assert_not_awaited()


async def test_a_member_that_was_added_to_a_leader_is_added_again_once_it_is_online(
    mock_mass: MagicMock,
) -> None:
    """A switched-off player that was added to a leader is added by the same command again."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    set_members = AsyncMock()
    controller.cmd_set_members = set_members  # type: ignore[method-assign]
    kitchen, den = setup.speakers["kitchen"].player, setup.speakers["den"].player

    assert await controller._power_on_member(kitchen, den, added=True) is True
    await asyncio.sleep(1)

    set_members.assert_awaited_once_with("kitchen", player_ids_to_add=["den"])
    assert controller._member_joins == {}


async def test_calling_off_the_power_on_of_the_group_keeps_its_member_out(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A member whose power on its group called off does not join, which is no warning."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    den = setup.speakers["den"]
    await setup.controller._power_on_group_members(setup.group)
    await asyncio.sleep(BOOT_TIME / 2)

    with caplog.at_level(logging.WARNING):
        setup.controller._cancel_power_on(setup.group)
        assert setup.controller._member_joins == {}
        assert not den.player.power_on_in_progress
        await asyncio.sleep(BOOT_TIME * 2)

    setup.joined.assert_not_awaited()
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    # the control is left as it is
    den.control.power_off.assert_not_awaited()  # type: ignore[union-attr]


async def test_a_member_whose_group_is_stopped_while_it_waits_for_the_lock_stays_out(
    mock_mass: MagicMock,
) -> None:
    """A member that is online but still waits for its group's lock does not join a stopped group."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller

    async with controller.get_player_lock("downstairs", PlayerLockPurpose.PLAYBACK):
        await controller._power_on_group_members(setup.group)
        await asyncio.sleep(BOOT_TIME * 2)
        # online, and waiting for the lock of its group
        assert setup.speakers["den"].player.available_for_playback
        assert list(controller._member_joins) == ["den"]
        controller._cancel_power_on(setup.group)
    await asyncio.sleep(1)

    setup.joined.assert_not_awaited()


async def test_a_member_that_waits_to_join_is_not_counted_as_idle(mock_mass: MagicMock) -> None:
    """The idle power off of a member only counts once it joined what it was powered on for."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    den = setup.speakers["den"]
    # switched on, online and stopped, with an idle power off of one minute
    den.control.power_state = True
    den.protocol._attr_available = True
    config_get_value = den.player.config.get_value
    den.player.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, *args, **kwargs: (
            1 if key == CONF_IDLE_POWER_OFF else config_get_value(key, *args, **kwargs)
        )
    )
    den.player.update_state(signal_event=False, force_update=True)
    assert controller._get_idle_power_off_minutes(den.player) == 1

    controller._member_joins["den"] = players_power.MemberJoin(
        "downstairs", False, asyncio.get_running_loop().create_future()
    )

    assert controller._get_idle_power_off_minutes(den.player) == 0


async def test_an_announcement_to_a_group_switches_no_member_on(mock_mass: MagicMock) -> None:
    """An announcement to a group leaves a member that is switched off as it is."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    run_announcement = AsyncMock()
    controller._run_announcement = run_announcement  # type: ignore[method-assign]

    await controller.play_announcement("downstairs", url="http://ma.local/doorbell.mp3")
    await asyncio.sleep(BOOT_TIME * 2)

    run_announcement.assert_awaited_once()
    assert controller._member_joins == {}
    setup.speakers["den"].control.power_on.assert_not_awaited()  # type: ignore[union-attr]


async def test_the_playback_an_announcement_restores_switches_no_member_on(
    mock_mass: MagicMock,
) -> None:
    """A group that plays again after its announcement leaves a switched-off member as it is."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    setup.group.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True

    await setup.controller._power_on_switched_off_player("downstairs")
    await asyncio.sleep(BOOT_TIME * 2)

    assert setup.controller._member_joins == {}
    setup.speakers["den"].control.power_on.assert_not_awaited()  # type: ignore[union-attr]


async def test_an_announcement_leaves_the_volume_of_a_switched_off_member_alone(
    mock_mass: MagicMock,
) -> None:
    """The announcement volume is only set on the members that play the announcement."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    set_volume = AsyncMock()
    controller._unmute_and_set_announcement_volume = set_volume  # type: ignore[method-assign]

    await controller._prepare_for_announcement(
        setup.group,
        volume_level=None,
        prev_state=PlaybackState.IDLE,
        prev_synced_to=None,
        prev_group=None,
        prev_media_name=None,
        prev_volumes={},
        prev_muted=set(),
    )

    assert [call.args[0].player_id for call in set_volume.await_args_list] == ["kitchen"]
