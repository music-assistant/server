"""Tests for the Sendspin output of a device that passes from one of its bridges to another."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.models.types import GoodbyeReason

from music_assistant.constants import CONF_PROTOCOL_EXPERIMENTAL_NOTE
from music_assistant.providers.airplay.sendspin_bridge import SendspinAirPlayBridge
from music_assistant.providers.chromecast.constants import SENDSPIN_CAST_EXPERIMENTAL_NOTE
from music_assistant.providers.chromecast.sendspin_bridge import SendspinChromecastBridge
from music_assistant.providers.sendspin.bridge_role import BRIDGE_ROLE_ID
from music_assistant.providers.sendspin.constants import CONF_SENDSPIN_CAST_OPT_IN
from music_assistant.providers.squeezelite.bridge_role import SLIMPROTO_BRIDGE_ROLE_ID
from music_assistant.providers.squeezelite.sendspin_bridge import SendspinSqueezeliteBridge

from .bridge_harness import (
    AIRPLAY_CLIENT,
    AIRPLAY_ID,
    CAST_CLIENT,
    CAST_ID,
    OTHER_INTERFACE_AIRPLAY_CLIENT,
    OTHER_INTERFACE_AIRPLAY_ID,
    SAME_ID_AIRPLAY_ID,
    SQUEEZELITE_CLIENT,
    SQUEEZELITE_ID,
    BridgedDevice,
    hello,
)

# the devices of a test, whose Sendspin servers are closed after it
_DEVICES: list[BridgedDevice] = []


@pytest.fixture(autouse=True)
async def _close_servers() -> AsyncGenerator[None]:
    """Close the Sendspin servers of the devices of a test."""
    yield
    while _DEVICES:
        await _DEVICES.pop().server.close()


def _device(**kwargs: Any) -> BridgedDevice:
    """
    Return a device whose Sendspin server is closed after the test.

    :param kwargs: What the device has, see BridgedDevice.
    """
    device = BridgedDevice(**kwargs)
    _DEVICES.append(device)
    return device


async def _bridged(device: BridgedDevice, carrier: str, client_id: str = AIRPLAY_CLIENT) -> None:
    """Attach every protocol of the device, AirPlay first, and check which bridge carries."""
    await device.attach(device.airplay)
    for name in ("squeezelite", "cast"):
        if protocol := getattr(device, name, None):
            await device.attach(protocol)
    assert device.state() == (carrier, client_id, carrier, "on")


async def _cast_carries(device: BridgedDevice, client_id: str) -> None:
    """Let the user switch on the output Cast leaves off by default, so its bridge carries it."""
    assert device.state() == (None, client_id, "chromecast", "off-default")
    await device.switch_output(True)
    assert device.state() == ("chromecast", client_id, "chromecast", "on")


@pytest.mark.parametrize("slow_teardown", [False, True])
async def test_switching_squeezelite_off_hands_the_output_to_airplay(slow_teardown: bool) -> None:
    """The AirPlay bridge takes the output over once the user switched squeezelite off."""
    device = _device(slow_teardown=slow_teardown)
    await _bridged(device, "squeezelite")
    assert device.manager_of(device.airplay).get_bridge(AIRPLAY_ID) is None
    device.timeline.clear()

    await device.switch(device.squeezelite, False)

    assert device.timeline == [("stopped", "squeezelite"), ("registered", "airplay")]
    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")


async def test_switching_squeezelite_back_on_hands_the_output_back() -> None:
    """Squeezelite takes the output back once it is switched on and found again."""
    device = _device()
    await _bridged(device, "squeezelite")
    await device.switch(device.squeezelite, False)
    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")
    device.timeline.clear()

    await device.switch(device.squeezelite, True)
    # switched on, squeezelite only takes the output once it is there
    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")
    await device.attach(device.squeezelite)

    assert device.timeline == [("stopped", "airplay"), ("registered", "squeezelite")]
    assert device.state() == ("squeezelite", AIRPLAY_CLIENT, "squeezelite", "on")


async def test_starting_up_with_squeezelite_switched_off_gives_airplay_the_device() -> None:
    """The AirPlay bridge takes the output of a device that starts with squeezelite switched off."""
    device = _device()
    device.store_output(SQUEEZELITE_CLIENT, SQUEEZELITE_ID)
    device.records()[SQUEEZELITE_ID]["enabled"] = False

    await device.attach(device.airplay)

    assert device.state() == ("airplay", SQUEEZELITE_CLIENT, "airplay", "on")


async def test_disabling_the_squeezelite_provider_switches_squeezelite_off() -> None:
    """A squeezelite protocol whose provider the user disabled leaves the output to AirPlay."""
    device = _device()
    device.store_output(SQUEEZELITE_CLIENT, SQUEEZELITE_ID)
    device.config.set("providers/squeezelite/enabled", False)

    await device.attach(device.airplay)

    assert device.state() == ("airplay", SQUEEZELITE_CLIENT, "airplay", "on")


async def test_a_squeezelite_output_that_is_away_keeps_the_device() -> None:
    """Nothing takes the output while its squeezelite protocol is switched on but does not answer."""
    device = _device()
    await _bridged(device, "squeezelite")
    output = dict(device.output())

    await device.detach(device.squeezelite)

    assert device.manager_of(device.airplay).get_bridge(AIRPLAY_ID) is None
    assert device.output() == output
    assert device.state() == (None, AIRPLAY_CLIENT, "squeezelite", "on")


async def test_an_output_under_the_same_client_id_is_taken_over_with_its_settings() -> None:
    """Squeezelite carries the output AirPlay had under the client id of both, as it is."""
    device = _device(airplay_id=SAME_ID_AIRPLAY_ID)
    await device.attach(device.airplay)
    device.output()["values"]["output_channels"] = "left"
    await device.attach(device.squeezelite)

    await device.switch(device.airplay, False)

    assert device.output()["values"]["output_channels"] == "left"
    assert device.state() == ("squeezelite", SQUEEZELITE_CLIENT, "squeezelite", "on")


@pytest.mark.parametrize("slow_teardown", [False, True])
async def test_switching_airplay_off_hands_the_output_to_cast(slow_teardown: bool) -> None:
    """Cast takes the output of a device without squeezelite, and leaves it switched off."""
    device = _device(cast=True, squeezelite=False, slow_teardown=slow_teardown)
    await _bridged(device, "airplay")
    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None

    await device.switch(device.airplay, False)

    # Sendspin over Cast is opt-in for an output the user did not switch on over Cast
    assert device.state() == (None, AIRPLAY_CLIENT, "chromecast", "off-default")
    assert CAST_ID not in device.bridges, "the Cast bridge registered an output that is off"
    assert (
        device.output()["values"][CONF_PROTOCOL_EXPERIMENTAL_NOTE]
        == SENDSPIN_CAST_EXPERIMENTAL_NOTE
    )

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")
    assert CONF_PROTOCOL_EXPERIMENTAL_NOTE not in device.output()["values"]


async def test_airplay_goes_before_cast_when_squeezelite_is_switched_off() -> None:
    """The order between the bridges that take over stays: AirPlay before Cast."""
    device = _device(cast=True)
    await _bridged(device, "squeezelite")

    await device.switch(device.squeezelite, False)

    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None
    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")


async def test_squeezelite_takes_the_output_of_a_cast_protocol_that_is_away() -> None:
    """A bridge earlier in the order takes the output of a carrier that is away."""
    device = _device(cast=True)
    device.records()[AIRPLAY_ID]["enabled"] = False
    # the output of the Cast protocol of the device, which is away
    device.store_output(AIRPLAY_CLIENT, CAST_ID, values={CONF_SENDSPIN_CAST_OPT_IN: True})

    await device.attach(device.squeezelite)

    assert device.state() == ("squeezelite", AIRPLAY_CLIENT, "squeezelite", "on")


@pytest.mark.parametrize(
    ("first", "taker"), [("squeezelite", "airplay"), ("airplay", "chromecast")]
)
@pytest.mark.parametrize(
    "slow_teardown", [False, True], ids=["taker stops at once", "taker takes a moment"]
)
async def test_the_first_bridge_takes_back_a_client_of_the_same_id(
    first: str, taker: str, slow_teardown: bool
) -> None:
    """The first bridge switched on again registers the client once the taker let go of it."""
    device = _device(
        airplay_id=SAME_ID_AIRPLAY_ID,
        cast=taker == "chromecast",
        squeezelite=first == "squeezelite",
        slow_teardown=slow_teardown,
    )
    await _bridged(device, first, SQUEEZELITE_CLIENT)
    await device.switch(device.protocols[first], False)
    if taker == "chromecast":
        await _cast_carries(device, CAST_CLIENT)
    assert device.state() == (taker, SQUEEZELITE_CLIENT, taker, "on")
    device.timeline.clear()

    await device.switch(device.protocols[first], True)
    await device.attach(device.protocols[first])

    assert device.timeline == [("stopped", taker), ("registered", first)]
    assert device.state() == (first, SQUEEZELITE_CLIENT, first, "on")


async def test_squeezelite_takes_the_output_over_from_cast_and_keeps_it_while_away() -> None:
    """Squeezelite and Cast hand the output over in turn, never side by side."""
    device = _device(cast=True)
    device.records()[AIRPLAY_ID]["enabled"] = False
    # the output the user switched on while Cast carried it
    device.store_output(
        CAST_CLIENT,
        CAST_ID,
        values={
            CONF_SENDSPIN_CAST_OPT_IN: True,
            CONF_PROTOCOL_EXPERIMENTAL_NOTE: SENDSPIN_CAST_EXPERIMENTAL_NOTE,
        },
    )
    await device.attach(device.cast)
    assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")
    device.timeline.clear()

    await device.attach(device.squeezelite)

    assert device.timeline == [("stopped", "chromecast"), ("registered", "squeezelite")]
    assert device.state() == ("squeezelite", SQUEEZELITE_CLIENT, "squeezelite", "on")
    device.timeline.clear()

    await device.detach(device.squeezelite)

    # a carrier that is away keeps its place
    assert device.timeline == [("stopped", "squeezelite")]
    assert device.state() == (None, SQUEEZELITE_CLIENT, "squeezelite", "on")

    await device.switch(device.squeezelite, False)

    assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")


@pytest.mark.parametrize("kind", ["airplay", "squeezelite", "chromecast"])
@pytest.mark.parametrize("taken_over", [True, False])
async def test_a_bridge_that_stops_leaves_a_client_another_bridge_took_over(
    kind: str, taken_over: bool
) -> None:
    """The real bridges only remove the client they still hold."""
    device = _device(cast=True, airplay_id=SAME_ID_AIRPLAY_ID)
    server = device.server
    provider = MagicMock(mass=device.mass, logger=logging.getLogger(f"test.{kind}"))
    bridge: Any
    if kind == "airplay":
        bridge = SendspinAirPlayBridge(provider, device.airplay, server)
        role_id, protocol_id, taker_id = BRIDGE_ROLE_ID, SAME_ID_AIRPLAY_ID, SQUEEZELITE_ID
    elif kind == "squeezelite":
        bridge = SendspinSqueezeliteBridge(provider, device.squeezelite, server)
        role_id, protocol_id, taker_id = (
            SLIMPROTO_BRIDGE_ROLE_ID,
            SQUEEZELITE_ID,
            SAME_ID_AIRPLAY_ID,
        )
    else:
        bridge = SendspinChromecastBridge(provider, device.cast, server, CAST_CLIENT)
        role_id, protocol_id, taker_id = BRIDGE_ROLE_ID, CAST_ID, SAME_ID_AIRPLAY_ID
    bridge._sendspin_client = server.register_external_player(
        hello(SQUEEZELITE_CLIENT, role_id), on_stream_start=MagicMock()
    )
    (bridge._bridge_role,) = bridge._sendspin_client.roles_by_family("player")
    bridge._bridge_client_id = SQUEEZELITE_CLIENT
    sendspin = device.providers["sendspin"]
    sendspin.hold_bridge_client(SQUEEZELITE_CLIENT, protocol_id)
    if taken_over:
        # another bridge of the device registers the same client id
        server.register_external_player(
            hello(SQUEEZELITE_CLIENT, BRIDGE_ROLE_ID), on_stream_start=MagicMock()
        )
        sendspin.hold_bridge_client(SQUEEZELITE_CLIENT, taker_id)

    await bridge.stop()

    assert server.is_external_player(SQUEEZELITE_CLIENT) is taken_over, (
        "the bridge removed the client another bridge holds"
        if taken_over
        else "the bridge left its own client behind"
    )


@pytest.mark.parametrize(
    "state_received", [True, False], ids=["receiver connected", "receiver awaits its state"]
)
async def test_airplay_gets_the_device_back_from_a_connected_cast_receiver(
    state_received: bool,
) -> None:
    """AirPlay switched on again bridges the device once a connected Cast receiver let go."""
    device = _device(
        airplay_id=SAME_ID_AIRPLAY_ID,
        cast=True,
        squeezelite=False,
        slow_teardown=True,
        cast_connected=True,
        cast_state_received=state_received,
    )
    await _bridged(device, "airplay", CAST_CLIENT)
    await device.switch(device.airplay, False)
    await _cast_carries(device, CAST_CLIENT)
    client = device.server.get_client(CAST_CLIENT)
    assert client is not None
    # the receiver's role took the place of the role of the Cast bridge
    assert [role.role_id for role in client.roles_by_family("player")] == ["player@v1"]
    assert client.is_connected is state_received

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None
    assert device.state() == ("airplay", CAST_CLIENT, "airplay", "on")


async def test_a_carrier_that_can_not_register_lets_the_evaluations_end() -> None:
    """The evaluations of a player end, also when the bridge that carries its output fails."""
    device = _device(cast=True)
    device.records()[SQUEEZELITE_ID]["enabled"] = False
    await device.attach(device.airplay)
    await device.attach(device.cast)
    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")
    # squeezelite can not register, and neither AirPlay nor Cast takes the place of a bridge
    # that comes before them
    squeezelite_starts = AsyncMock(side_effect=RuntimeError("the client is connected"))
    device.manager_of(device.squeezelite)._create_bridge = MagicMock(
        side_effect=lambda _: MagicMock(start=squeezelite_starts, stop=AsyncMock())
    )
    evaluations: Counter[str] = Counter()
    for manager in device.managers:

        async def _counted(protocol: Any, evaluate: Any = manager.evaluate_bridge) -> None:
            evaluations[protocol.provider.domain] += 1
            await evaluate(protocol)

        manager.evaluate_bridge = _counted

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)
    settled = dict(evaluations)
    for _ in range(20):
        await device.settle()

    assert dict(evaluations) == settled, "the evaluations do not come to an end"
    assert squeezelite_starts.await_count >= 1, "squeezelite did not try to register"
    assert device.state() == (None, AIRPLAY_CLIENT, "squeezelite", "on")


@pytest.mark.parametrize("warm", [True, False], ids=["warm disconnect", "cold disconnect"])
async def test_a_cast_bridge_whose_receiver_left_takes_its_client_along(warm: bool) -> None:
    """A Cast bridge whose receiver disconnected before it stopped is set up again later."""
    device = _device(cast=True, squeezelite=False, cast_connected=True)
    device.records()[AIRPLAY_ID]["enabled"] = False
    # the output the user switched on while Cast carried it
    device.store_output(
        CAST_CLIENT,
        CAST_ID,
        values={
            CONF_SENDSPIN_CAST_OPT_IN: True,
            CONF_PROTOCOL_EXPERIMENTAL_NOTE: SENDSPIN_CAST_EXPERIMENTAL_NOTE,
        },
    )
    await device.attach(device.cast)
    assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")
    client = device.server.get_client(CAST_CLIENT)
    assert client is not None

    # the device reboots: its receiver goes first, then its Cast protocol
    client.detach_connection(None if warm else GoodbyeReason.SHUTDOWN)
    await device.detach(device.cast)

    assert device.server.get_client(CAST_CLIENT) is None, "the Cast bridge left its client behind"

    await device.attach(device.cast)

    assert device.state() == ("chromecast", CAST_CLIENT, "chromecast", "on")


@pytest.mark.parametrize(
    ("airplay_id", "airplay_client"),
    [
        pytest.param(AIRPLAY_ID, AIRPLAY_CLIENT, id="other id form"),
        pytest.param(
            OTHER_INTERFACE_AIRPLAY_ID, OTHER_INTERFACE_AIRPLAY_CLIENT, id="other interface"
        ),
    ],
)
async def test_squeezelite_takes_the_device_back_from_an_airplay_player_that_is_away(
    airplay_id: str, airplay_client: str
) -> None:
    """Squeezelite switched on again carries the output AirPlay left while it is away."""
    device = _device(airplay_id=airplay_id)
    await _bridged(device, "squeezelite", airplay_client)
    await device.switch(device.squeezelite, False)
    assert device.state() == ("airplay", airplay_client, "airplay", "on")
    await device.detach(device.airplay)
    assert device.state() == (None, airplay_client, "airplay", "on")

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)

    assert device.state() == ("squeezelite", airplay_client, "squeezelite", "on")


@pytest.mark.parametrize(
    ("airplay_id", "airplay_client"),
    [
        pytest.param(AIRPLAY_ID, AIRPLAY_CLIENT, id="other id form"),
        pytest.param(
            OTHER_INTERFACE_AIRPLAY_ID, OTHER_INTERFACE_AIRPLAY_CLIENT, id="other interface"
        ),
    ],
)
async def test_squeezelite_starting_up_takes_the_output_of_an_airplay_player_that_is_away(
    airplay_id: str, airplay_client: str
) -> None:
    """Squeezelite that attaches first at startup carries the output AirPlay had."""
    device = _device(airplay_id=airplay_id)
    device.store_output(airplay_client, airplay_id)

    await device.attach(device.squeezelite)

    assert device.state() == ("squeezelite", airplay_client, "squeezelite", "on")

    await device.attach(device.airplay)

    assert device.state() == ("squeezelite", airplay_client, "squeezelite", "on")


async def test_a_speaker_with_two_interfaces_hands_the_output_over_both_ways() -> None:
    """AirPlay and squeezelite with unrelated MAC addresses hand the output over in turn."""
    device = _device(airplay_id=OTHER_INTERFACE_AIRPLAY_ID)
    await _bridged(device, "squeezelite", OTHER_INTERFACE_AIRPLAY_CLIENT)

    await device.switch(device.squeezelite, False)

    assert device.state() == ("airplay", OTHER_INTERFACE_AIRPLAY_CLIENT, "airplay", "on")

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)

    assert device.state() == ("squeezelite", OTHER_INTERFACE_AIRPLAY_CLIENT, "squeezelite", "on")


async def test_airplay_switched_on_while_the_cast_bridge_stops_carries_the_output() -> None:
    """AirPlay switched on while the Cast protocol goes away moves the output over at once."""
    device = _device(
        airplay_id=SAME_ID_AIRPLAY_ID, cast=True, squeezelite=False, slow_teardown=True
    )
    await _bridged(device, "airplay", CAST_CLIENT)
    await device.switch(device.airplay, False)
    await _cast_carries(device, CAST_CLIENT)

    # the Cast bridge is still stopping when AirPlay registers the client of the same id
    await device.detach(device.cast, settle=False)
    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None
    assert device.state() == ("airplay", CAST_CLIENT, "airplay", "on")


@pytest.mark.parametrize("at_start", [False, True])
async def test_an_airplay_player_that_plays_a_set_keeps_its_bridge(at_start: bool) -> None:
    """
    An Apple TV that is the player of a home theater set is bridged like any AirPlay player.

    Its bridge feeds the speakers of the set, whether the set is found while the bridge runs
    or before the protocol attaches.
    """
    device = _device(squeezelite=False)
    if at_start:
        device.airplay.carries_set = True
        await device.attach(device.airplay)
    else:
        await _bridged(device, "airplay")
        device.airplay.carries_set = True
        await device.manager_of(device.airplay).evaluate_bridge(device.airplay)
    await device.settle()

    assert device.manager_of(device.airplay).get_bridge(AIRPLAY_ID) is not None
    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")
