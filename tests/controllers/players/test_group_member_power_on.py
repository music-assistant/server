"""A group player waits for its members that are switched off at their power control."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType, ProtocolRole
from music_assistant_models.errors import (
    MusicAssistantError,
    PlayerCommandFailed,
    PlayerUnavailableError,
)
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import CONF_POWER_CONTROL
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import (
    OFFLINE_POWER_ON_TIMEOUT,
    PlayerLockPurpose,
)
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
    controls = {name: f"switch.{name}_plug" for name in names}
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
    speakers: dict[str, _Speaker] = {}
    for name in names:
        player, protocol = attach_mock(provider, f"proto_{name}", name.title(), player_id=name)
        control = PlayerControl(
            id=controls[name],
            provider="hass",
            name=f"{name} plug",
            supports_power=True,
            power_state=name not in switched_off,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
        )
        controller._controls[control.id] = control
        if name in switched_off:
            protocol._attr_available = False
            cast("AsyncMock", control.power_on).side_effect = _switch_on(
                player, protocol, control, comes_back=name not in stays_away
            )
        speakers[name] = _Speaker(player, protocol, control)
    for player in (*(speaker.player for speaker in speakers.values()), group):
        player.update_state(signal_event=False, force_update=True)
    return _Setup(controller, group, speakers)


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


def _above_debug(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return what was logged above the debug level."""
    return [record.getMessage() for record in caplog.records if record.levelno > logging.DEBUG]


def _power_ons(setup: _Setup) -> dict[str, int]:
    """Return how often the power control of each member was switched on."""
    return {
        name: cast("AsyncMock", speaker.control.power_on).await_count
        for name, speaker in setup.speakers.items()
    }


async def test_the_switched_off_members_are_powered_on_together_without_a_lock(
    mock_mass: MagicMock,
) -> None:
    """All switched-off members are powered on at once, and the wait holds no player."""
    setup = _setup(mock_mass, "kitchen", "office", "den", switched_off=("office", "den"))
    controller = setup.controller
    started = asyncio.get_running_loop().time()

    waiting = asyncio.create_task(controller._power_on_members(setup.group))
    await asyncio.sleep(BOOT_TIME / 2)

    assert _power_ons(setup) == {"kitchen": 0, "office": 1, "den": 1}
    assert not waiting.done()
    assert _locked(controller) == []
    assert await waiting == set()
    # one boot time, not one per member; the device is looked for every 0.5 s
    assert BOOT_TIME <= asyncio.get_running_loop().time() - started < BOOT_TIME + 1
    assert all(speaker.player.available_for_playback for speaker in setup.speakers.values())
    assert controller._member_power_ons == {}


async def test_a_group_whose_members_are_on_waits_for_nothing(mock_mass: MagicMock) -> None:
    """A member that is on, or plainly unreachable, is not switched on and not waited for."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=())
    # Den's control is on while its device is away: unreachable, not switched off
    den = setup.speakers["den"]
    den.protocol._attr_available = False
    den.player.update_state(signal_event=False, force_update=True)
    assert not den.player.available
    started = asyncio.get_running_loop().time()

    assert await setup.controller._power_on_members(setup.group) == set()

    assert asyncio.get_running_loop().time() == started
    assert _power_ons(setup) == {"kitchen": 0, "den": 0}


async def test_a_member_that_stays_away_is_reported_and_logged_once(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A member that does not come online is waited for as long as a power on waits, no longer."""
    setup = _setup(
        mock_mass, "kitchen", "office", "den", switched_off=("office", "den"), stays_away=("den",)
    )
    started = asyncio.get_running_loop().time()

    with caplog.at_level(logging.WARNING):
        left_out = await setup.controller._power_on_members(setup.group)

    assert left_out == {"den"}
    waited = asyncio.get_running_loop().time() - started
    assert OFFLINE_POWER_ON_TIMEOUT <= waited < OFFLINE_POWER_ON_TIMEOUT + 1
    assert [record.getMessage() for record in caplog.records] == [
        "Player Den is left out of Downstairs: Player Den did not come online after powering it on"
    ]
    assert setup.speakers["office"].player.available_for_playback
    assert setup.controller._member_power_ons == {}


async def test_a_power_control_that_fails_leaves_the_member_out_with_one_warning(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A control that can not switch its member on does not end the wait for the others."""
    setup = _setup(mock_mass, "kitchen", "office", "den", switched_off=("office", "den"))
    den = setup.speakers["den"]
    cast("AsyncMock", den.control.power_on).side_effect = PlayerCommandFailed("No answer")

    with caplog.at_level(logging.WARNING):
        left_out = await setup.controller._power_on_members(setup.group)

    assert left_out == {"den"}
    assert [record.getMessage() for record in caplog.records] == [
        "Player Den is left out of Downstairs: No answer"
    ]
    assert setup.speakers["office"].player.available_for_playback
    assert not den.player.power_on_in_progress
    assert den.player.offline_by_power_control


async def test_a_command_that_holds_the_group_powers_on_no_member(mock_mass: MagicMock) -> None:
    """Nothing is waited for under the lock of the group: its command did that before it."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    started = asyncio.get_running_loop().time()

    async with controller.get_player_lock("downstairs", PlayerLockPurpose.PLAYBACK):
        assert await controller._power_on_members(setup.group) == set()

    assert asyncio.get_running_loop().time() == started
    assert _power_ons(setup) == {"kitchen": 0, "den": 0}


async def test_playback_the_server_continues_powers_on_no_member(mock_mass: MagicMock) -> None:
    """Playback that the server continues by itself leaves a switched-off member off."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    started = asyncio.get_running_loop().time()

    with controller.without_member_power_on():
        assert await controller._power_on_members(setup.group) == set()

    assert asyncio.get_running_loop().time() == started
    assert _power_ons(setup) == {"kitchen": 0, "den": 0}
    # a command that starts the group afterwards does power it on
    assert await controller._power_on_members(setup.group) == set()
    assert _power_ons(setup) == {"kitchen": 0, "den": 1}


async def test_calling_the_power_on_off_ends_the_wait_as_unavailable(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A wait that is called off fails, leaves the controls on and logs nothing above debug."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",), stays_away=("den",))
    controller = setup.controller
    den = setup.speakers["den"]
    waiting = asyncio.create_task(controller._power_on_members(setup.group))
    await asyncio.sleep(0.75)
    waited_for = den.player.power_on_in_progress

    with caplog.at_level(logging.DEBUG):
        controller._cancel_member_power_ons(setup.group)
        with pytest.raises(PlayerUnavailableError, match="was called off"):
            await waiting

    assert _above_debug(caplog) == []
    assert waited_for
    assert not den.player.power_on_in_progress
    assert den.control.power_state is True
    cast("AsyncMock", den.control.power_off).assert_not_awaited()
    assert controller._member_power_ons == {}


async def test_a_command_that_is_cancelled_ends_the_power_on_of_its_members(
    mock_mass: MagicMock,
) -> None:
    """A command that is cancelled while it waits is cancelled, and leaves no power on behind."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",), stays_away=("den",))
    controller = setup.controller
    waiting = asyncio.create_task(controller._power_on_members(setup.group))
    await asyncio.sleep(0.75)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert not setup.speakers["den"].player.power_on_in_progress
    assert controller._member_power_ons == {}


async def test_a_member_that_is_called_off_itself_is_left_out_without_a_warning(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A member whose own power on is called off is left out, and the others are waited for."""
    setup = _setup(
        mock_mass, "kitchen", "office", "den", switched_off=("office", "den"), stays_away=("den",)
    )
    controller = setup.controller
    waiting = asyncio.create_task(controller._power_on_members(setup.group))
    await asyncio.sleep(0.75)

    with caplog.at_level(logging.DEBUG):
        controller._cancel_power_on(setup.speakers["den"].player)
        assert await waiting == {"den"}

    assert _above_debug(caplog) == []
    assert setup.speakers["office"].player.available_for_playback


async def test_an_announcement_to_a_group_switches_no_member_on(mock_mass: MagicMock) -> None:
    """An announcement to a group leaves a member that is switched off as it is."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    run_announcement = AsyncMock()
    controller._run_announcement = run_announcement  # type: ignore[method-assign]
    started = asyncio.get_running_loop().time()

    await controller.play_announcement("downstairs", url="http://ma.local/doorbell.mp3")

    run_announcement.assert_awaited_once()
    assert asyncio.get_running_loop().time() == started
    assert _power_ons(setup) == {"kitchen": 0, "den": 0}


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


async def _call_off(setup: _Setup, after: float) -> None:
    """Start the group, and call the wait for its members off after the given seconds."""
    waiting = asyncio.create_task(setup.controller._power_on_members(setup.group))
    await asyncio.sleep(after)
    setup.controller._cancel_member_power_ons(setup.group)
    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await waiting


async def test_a_member_that_still_boots_is_waited_for_again_and_its_control_left_alone(
    mock_mass: MagicMock,
) -> None:
    """A new start of the group waits again for the member whose power on was called off."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    den = setup.speakers["den"]
    await _call_off(setup, after=BOOT_TIME / 4)
    await asyncio.sleep(BOOT_TIME / 4)
    shown_as_available = den.player.available
    started = asyncio.get_running_loop().time()

    waiting = asyncio.create_task(controller._power_on_members(setup.group))
    await asyncio.sleep(0.1)

    assert not shown_as_available
    assert den.player.power_on_in_progress
    assert den.player.available
    assert await waiting == set()
    # the device is back when its boot is over, which started with the first power on
    assert asyncio.get_running_loop().time() - started < BOOT_TIME
    assert den.player.available_for_playback
    assert _power_ons(setup) == {"kitchen": 0, "den": 1}


async def test_a_member_that_still_boots_gets_the_time_that_is_left_of_its_power_on(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A member that is waited for again is given up when its first power on would have been."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",), stays_away=("den",))
    controller = setup.controller
    started = asyncio.get_running_loop().time()
    await _call_off(setup, after=10)
    await asyncio.sleep(10)

    with caplog.at_level(logging.WARNING):
        assert await controller._power_on_members(setup.group) == {"den"}

    waited = asyncio.get_running_loop().time() - started
    assert OFFLINE_POWER_ON_TIMEOUT <= waited < OFFLINE_POWER_ON_TIMEOUT + 1
    assert _above_debug(caplog) == [
        "Player Den is left out of Downstairs: Player Den did not come online after powering it on"
    ]
    # its time is over: a later start does not wait for it
    later = asyncio.get_running_loop().time()
    assert await controller._power_on_members(setup.group) == set()
    assert asyncio.get_running_loop().time() == later
    assert _power_ons(setup) == {"kitchen": 0, "den": 1}


async def test_a_member_that_went_away_after_it_was_online_is_not_waited_for(
    mock_mass: MagicMock,
) -> None:
    """Only a power on that was called off before the device was online is taken up again."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",))
    controller = setup.controller
    den = setup.speakers["den"]
    assert await controller._power_on_members(setup.group) == set()

    # its device drops off the network, with its control still on
    den.protocol._attr_available = False
    den.player.update_state(signal_event=False, force_update=True)
    started = asyncio.get_running_loop().time()

    assert await controller._power_on_members(setup.group) == set()
    assert asyncio.get_running_loop().time() == started
    assert _power_ons(setup) == {"kitchen": 0, "den": 1}


@pytest.mark.parametrize("command", ["cmd_stop", "cmd_pause"])
async def test_a_stop_of_a_group_that_plays_no_queue_calls_the_wait_off(
    mock_mass: MagicMock, command: str
) -> None:
    """A stop or a pause of the group ends the wait, also when no queue takes the command."""
    setup = _setup(mock_mass, "kitchen", "den", switched_off=("den",), stays_away=("den",))
    controller = setup.controller
    mock_mass.player_queues.get = MagicMock(return_value=None)
    waiting = asyncio.create_task(controller._power_on_members(setup.group))
    await asyncio.sleep(0.75)

    with suppress(MusicAssistantError):
        # a group that plays nothing may refuse the command itself
        await getattr(controller, command)("downstairs")

    with pytest.raises(PlayerUnavailableError, match="was called off"):
        await waiting
    assert _power_ons(setup) == {"kitchen": 0, "den": 1}
