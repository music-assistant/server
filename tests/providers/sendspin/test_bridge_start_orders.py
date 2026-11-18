"""Tests that the Sendspin output of a device ends the same in every order of events."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator, Iterator
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from music_assistant.constants import (
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.controllers.config.migrations import _migrate_sendspin_one_output
from music_assistant.providers.chromecast.constants import SENDSPIN_CAST_EXPERIMENTAL_NOTE
from music_assistant.providers.sendspin.bridge_manager import BRIDGE_ORDER
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_BRIDGE_STATIC_DELAYS,
    CONF_SENDSPIN_STATIC_DELAY,
)

from .bridge_harness import (
    AIRPLAY_CLIENT,
    AIRPLAY_ID,
    CAST_CLIENT,
    CAST_ID,
    DOMAINS,
    SAME_ID_AIRPLAY_ID,
    SQUEEZELITE_CLIENT,
    SQUEEZELITE_ID,
    BridgedDevice,
)

Event = tuple[str, str]
SUBSETS = [subset for size in (1, 2, 3) for subset in itertools.combinations(DOMAINS, size)]
PAIRS_AND_TRIPLE = [subset for subset in SUBSETS if len(subset) > 1]

# the devices of a test, which are closed after it
_DEVICES: list[BridgedDevice] = []


@pytest.fixture(autouse=True)
async def _close_devices() -> AsyncGenerator[None]:
    """Stop the bridges and close the Sendspin servers of the devices a test left open."""
    yield
    while _DEVICES:
        await _DEVICES.pop().close()


def _device(
    subset: tuple[str, ...] = DOMAINS, *, shared: bool = False, **kwargs: Any
) -> BridgedDevice:
    """
    Return a device with a protocol of each of the given providers.

    :param subset: The domains of the providers.
    :param shared: Whether its AirPlay protocol reports the MAC address squeezelite and Cast
        report, so the bridges of all three would register under the same client id.
    :param kwargs: What else the device has, see BridgedDevice.
    """
    device = BridgedDevice(
        airplay_id=SAME_ID_AIRPLAY_ID if shared else AIRPLAY_ID,
        airplay="airplay" in subset,
        squeezelite="squeezelite" in subset,
        cast="chromecast" in subset,
        **kwargs,
    )
    _DEVICES.append(device)
    return device


async def _done(device: BridgedDevice) -> None:
    """Close a device of a test that goes through many of them."""
    _DEVICES.remove(device)
    await device.close()


def _first(domains: tuple[str, ...] | list[str]) -> str:
    """Return the provider whose bridge comes first in the order, among the given ones."""
    return min(domains, key=BRIDGE_ORDER.index)


def _schedules(domains: tuple[str, ...], attached: tuple[str, ...]) -> Iterator[list[Event]]:
    """
    Yield every order in which providers load and their devices are found at a start.

    :param domains: The domains of the providers that load.
    :param attached: The domains whose protocol attaches, each after its provider loaded.
    """
    events = [("load", domain) for domain in domains] + [("attach", d) for d in attached]
    for order in itertools.permutations(events):
        loaded: set[str] = set()
        for kind, domain in order:
            if kind == "load":
                loaded.add(domain)
            elif domain not in loaded:
                break
        else:
            yield list(order)


async def _restarted(device: BridgedDevice, settings: dict[str, Any] | None = None) -> None:
    """
    Bring a device to where a server is before its providers load.

    :param device: A device none of whose protocols is attached.
    :param settings: The stored settings the server starts with; those of the device by default.
    """
    if settings is not None:
        device.config.data = deepcopy(settings)
    for domain in list(device.protocols):
        await device.unload_provider(domain)


async def _play(device: BridgedDevice, event: Event) -> None:
    """Let a provider load, or a protocol of the device attach."""
    kind, domain = event
    if kind == "load":
        await device.load_provider(domain)
    else:
        await device.attach(device.protocols[domain])


def _kind(device: BridgedDevice) -> str | None:
    """Return whether the one output of a device is on, off by its user or off by default."""
    return device.state()[3]


# ---------------------------------------------------------------- a new device


@pytest.mark.parametrize("subset", SUBSETS, ids="+".join)
@pytest.mark.parametrize("shared", [False, True], ids=["own ids", "one id"])
async def test_a_new_device_gets_one_output_whatever_the_start_order(
    subset: tuple[str, ...], shared: bool
) -> None:
    """The first bridge in the order carries a new device, whichever provider finds it first."""
    first = _first(subset)
    # Sendspin over Cast is left switched off until the user opts in
    expected = (
        (None, "chromecast", "off-default") if first == "chromecast" else (first, first, "on")
    )
    orders = 0
    for schedule in _schedules(subset, subset):
        device = _device(subset, shared=shared)
        await _restarted(device)
        for event in schedule:
            await _play(device, event)
        registered, _client_id, carrier, kind = device.state()
        assert (registered, carrier, kind) == expected, f"after {schedule}"
        await _done(device)
        orders += 1
    assert orders == {1: 1, 2: 6, 3: 90}[len(subset)]


@pytest.mark.parametrize("domain", DOMAINS)
async def test_a_device_with_one_bridge_behaves_as_before(domain: str) -> None:
    """One bridge: its output follows its protocol and its own switch, under its own client id."""
    device = _device((domain,))
    protocol = device.protocols[domain]
    client_id = {"airplay": AIRPLAY_CLIENT}.get(domain, SQUEEZELITE_CLIENT)
    on = (domain, client_id, domain, "on")
    away = (None, client_id, domain, "on")

    await device.attach(protocol)
    if domain == "chromecast":
        assert device.state() == (None, client_id, domain, "off-default")
        await device.switch_output(True)
    assert device.state() == on

    await device.detach(protocol)
    assert device.state() == away
    await device.attach(protocol)
    assert device.state() == on

    await device.switch_output(False)
    assert device.state() == (None, client_id, domain, "off-user")
    await device.detach(protocol)
    await device.attach(protocol)
    assert device.state() == (None, client_id, domain, "off-user")
    await device.switch_output(True)
    assert device.state() == on

    await device.switch(protocol, False)
    assert device.state() == away
    await device.switch(protocol, True)
    await device.attach(protocol)
    assert device.state() == on
    # one client, registered once per time its protocol or its output came back
    assert {name for _action, name in device.timeline} == {domain}


# ---------------------------------------------------------------- a restart


async def _settled(
    switched_off: tuple[str, ...], output: str
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """
    Return the settings and the state of a device with three bridges that has settled.

    :param switched_off: The domains of the protocols its user switched off.
    :param output: Where its Sendspin output is at: "on", "off-user" (switched off by its
        user, before the protocols were) or "off-default" (left off by Cast, which carries it).
    """
    device = _device()
    for protocol in device.protocols.values():
        await device.attach(protocol)
    if output == "off-user":
        await device.switch_output(False)
    for domain in switched_off:
        await device.switch(device.protocols[domain], False)
    if output == "on" and _kind(device) == "off-default":
        # the opt-in of an output that Cast carries and left switched off
        await device.switch_output(True)
    assert _kind(device) == output
    settings, state = deepcopy(device.config.data), device.state()
    await _done(device)
    return settings, state


def _settled_states() -> Iterator[Any]:
    """Yield where a device with three bridges can have settled, as parameters of a test."""
    for size in range(len(DOMAINS) + 1):
        for switched_off in itertools.combinations(DOMAINS, size):
            cast_carries = {"airplay", "squeezelite"} <= set(switched_off)
            for output in ("on", "off-user", "off-default"):
                # an output is only off by default while Cast carries it, and can only be
                # switched on while one of its protocols is
                if (output == "off-default" and not cast_carries) or (
                    output == "on" and size == len(DOMAINS)
                ):
                    continue
                yield pytest.param(
                    switched_off, output, id=f"{output}, off: {'+'.join(switched_off) or 'none'}"
                )


@pytest.mark.parametrize(("switched_off", "output"), list(_settled_states()))
async def test_a_restart_never_moves_a_settled_output(
    switched_off: tuple[str, ...], output: str
) -> None:
    """
    After a restart the output is where it was, whatever loads and attaches in which order.

    Its carrier keeps its place while it is away or its provider still loads: no other
    bridge registers, and the stored output is not written to.
    """
    switched_on = tuple(domain for domain in DOMAINS if domain not in switched_off)
    settings, (_registered, client_id, carrier, kind) = await _settled(switched_off, output)
    output_on = output == "on"
    stored = deepcopy(settings["player_protocols"][client_id])
    assert carrier == (_first(switched_on) if switched_on else DOMAINS[-1])
    restarts = 0
    for size in range(len(switched_on) + 1):
        for attached in itertools.combinations(switched_on, size):
            for schedule in _schedules(DOMAINS, attached):
                device = _device()
                await _restarted(device, settings)
                for index, event in enumerate(schedule):
                    await _play(device, event)
                    context = f"after {schedule[: index + 1]}"
                    there = carrier in switched_on and ("attach", carrier) in schedule[: index + 1]
                    assert device.state() == (
                        carrier if output_on and there else None,
                        client_id,
                        carrier,
                        kind,
                    ), context
                    assert device.output() == stored, f"the stored output changed {context}"
                assert {name for _action, name in device.timeline} <= {carrier}, (
                    f"another bridge than the carrier registered in {schedule}"
                )
                await _done(device)
                restarts += 1
    assert restarts == {0: 6, 1: 18, 2: 60, 3: 222}[len(switched_on)]


@pytest.mark.parametrize("subset", SUBSETS, ids="+".join)
@pytest.mark.parametrize("shared", [False, True], ids=["own ids", "one id"])
@pytest.mark.parametrize("output_on", [True, False], ids=["output on", "output off"])
async def test_a_device_that_went_away_comes_back_as_it_was(
    subset: tuple[str, ...], shared: bool, output_on: bool
) -> None:
    """Protocols that go and come back in any order leave the output and its settings alone."""
    for detach_order in itertools.permutations(subset):
        for attach_order in itertools.permutations(subset):
            device = _device(subset, shared=shared)
            for domain in subset:
                await device.attach(device.protocols[domain])
            if _kind(device) != ("on" if output_on else "off-user"):
                await device.switch_output(output_on)
            device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 123
            before, state = deepcopy(device.output()), device.state()

            for domain in detach_order:
                await device.detach(device.protocols[domain])
            assert device.state() == (None, *state[1:])
            for domain in attach_order:
                await device.attach(device.protocols[domain])

            context = f"away {detach_order}, back {attach_order}"
            assert device.state() == state, context
            assert device.output() == before, context
            await _done(device)


@pytest.mark.parametrize("subset", SUBSETS, ids="+".join)
@pytest.mark.parametrize("output_on", [True, False], ids=["output on", "output off"])
async def test_a_provider_that_is_reloaded_leaves_the_output_as_it_was(
    subset: tuple[str, ...], output_on: bool
) -> None:
    """A provider that unloads and loads again takes its bridge along and brings it back."""
    for reloaded in subset:
        device = _device(subset)
        for domain in subset:
            await device.attach(device.protocols[domain])
        if _kind(device) != ("on" if output_on else "off-user"):
            await device.switch_output(output_on)
        device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 123
        before, state = deepcopy(device.output()), device.state()
        carrier = state[2]

        await device.unload_provider(reloaded)

        # the provider is enabled: its protocol keeps its place while it loads
        gone = (None, *state[1:]) if reloaded == carrier else state
        assert device.state() == gone, f"{reloaded} unloaded"
        await device.load_provider(reloaded)
        assert device.state() == gone, f"{reloaded} loaded"
        await device.attach(device.protocols[reloaded])

        assert device.state() == state, f"{reloaded} attached again"
        assert device.output() == before
        await _done(device)


async def test_a_restart_with_the_provider_of_the_carrier_disabled_hands_the_output_over() -> None:
    """A carrier whose provider the user disabled does not keep its place."""
    settings, state = await _settled((), "on")
    assert state == ("squeezelite", AIRPLAY_CLIENT, "squeezelite", "on")
    settings["providers"]["squeezelite"]["enabled"] = False

    for schedule in _schedules(("airplay", "chromecast"), ("airplay", "chromecast")):
        device = _device()
        await _restarted(device, settings)
        for event in schedule:
            await _play(device, event)
        assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on"), schedule
        await _done(device)


# ---------------------------------------------------------------- an upgrade


async def _upgraded(device: BridgedDevice) -> dict[str, Any]:
    """Migrate the settings of a device as stored by the version before, and return them."""
    assert _migrate_sendspin_one_output(device.config.data) is True
    settings = deepcopy(device.config.data)
    await _done(device)
    return settings


async def _assert_in_every_start_order(
    settings: dict[str, Any],
    subset: tuple[str, ...],
    attached: tuple[str, ...],
    expected: tuple[Any, ...],
    *,
    shared: bool = False,
) -> None:
    """
    Assert a device ends in one state, in every order its providers load and it is found.

    An output that is switched off is never registered on the way.

    :param settings: The stored settings the server starts with.
    :param subset: The domains of the providers of the device.
    :param attached: The domains whose protocol attaches.
    :param expected: The state the device ends in.
    :param shared: Whether the bridges of the device would register under one client id.
    """
    for schedule in _schedules(subset, attached):
        device = _device(subset, shared=shared)
        await _restarted(device, settings)
        for event in schedule:
            await _play(device, event)
        assert device.state() == expected, f"after {schedule}"
        if expected[3] != "on":
            assert device.timeline == [], f"an output that is off was registered in {schedule}"
        await _done(device)


@pytest.mark.parametrize("shared", [False, True], ids=["own ids", "one id"])
@pytest.mark.parametrize("enabled", [True, False], ids=["output on", "output off"])
async def test_an_upgrade_leaves_the_one_output_of_a_device_as_it_is(
    shared: bool, enabled: bool
) -> None:
    """A device with AirPlay and squeezelite keeps the output AirPlay carried, on or off."""
    subset = ("airplay", "squeezelite")
    device = _device(subset, shared=shared)
    client_id = SQUEEZELITE_CLIENT if shared else AIRPLAY_CLIENT
    device.store_output(
        client_id,
        device.airplay.protocol_id,
        enabled=enabled,
        values={CONF_SENDSPIN_STATIC_DELAY: 77},
    )
    before = deepcopy(device.output())
    settings = await _upgraded(device)

    assert settings["player_protocols"][client_id] == before
    # squeezelite comes first, and carries it from the first start
    expected = (
        ("squeezelite", client_id, "squeezelite", "on")
        if enabled
        else (None, client_id, "squeezelite", "off-user")
    )
    await _assert_in_every_start_order(settings, subset, subset, expected, shared=shared)


@pytest.mark.parametrize("enabled", [True, False], ids=["output on", "output off"])
@pytest.mark.parametrize(
    "squeezelite_output", [False, True], ids=["airplay output", "squeezelite output too"]
)
async def test_the_first_start_after_an_upgrade_hands_the_airplay_output_to_squeezelite(
    enabled: bool, squeezelite_output: bool
) -> None:
    """The one output moves from AirPlay to squeezelite as it was, and each keeps its delay."""
    subset = ("airplay", "squeezelite")
    device = _device(subset)
    device.store_output(
        AIRPLAY_CLIENT, AIRPLAY_ID, enabled=enabled, values={CONF_SENDSPIN_STATIC_DELAY: 77}
    )
    if squeezelite_output:
        # an output squeezelite had of its own before the update
        device.store_output(
            SQUEEZELITE_CLIENT,
            SQUEEZELITE_ID,
            enabled=enabled,
            values={CONF_SENDSPIN_STATIC_DELAY: 40},
        )
    settings = await _upgraded(device)
    kind = "on" if enabled else "off-user"
    over_squeezelite: dict[str, Any] = {
        CONF_UNDERLYING_PLAYER_ID: SQUEEZELITE_ID,
        CONF_SENDSPIN_BRIDGE_STATIC_DELAYS: {AIRPLAY_ID: 77},
    }
    over_airplay: dict[str, Any] = {
        CONF_UNDERLYING_PLAYER_ID: AIRPLAY_ID,
        CONF_SENDSPIN_STATIC_DELAY: 77,
    }
    if squeezelite_output:
        over_squeezelite[CONF_SENDSPIN_STATIC_DELAY] = 40
        over_airplay[CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] = {SQUEEZELITE_ID: 40}

    for schedule in _schedules(subset, subset):
        device = _device(subset)
        await _restarted(device, settings)
        for event in schedule:
            await _play(device, event)

        assert list(device.outputs()) == [AIRPLAY_CLIENT], schedule
        assert device.state() == (
            "squeezelite" if enabled else None,
            AIRPLAY_CLIENT,
            "squeezelite",
            kind,
        ), schedule
        assert device.output()["values"] == over_squeezelite, schedule
        if not enabled:
            assert device.timeline == [], f"an output that is off was registered in {schedule}"

        await device.switch(device.squeezelite, False)

        assert device.state() == (
            "airplay" if enabled else None,
            AIRPLAY_CLIENT,
            "airplay",
            kind,
        ), schedule
        assert device.output()["values"] == over_airplay, schedule
        await _done(device)


def _store_cast_default(device: BridgedDevice, client_id: str = CAST_CLIENT) -> None:
    """Store the output a Cast bridge registered and left switched off, as the version before did."""
    device.store_output(
        client_id,
        CAST_ID,
        enabled=False,
        values={
            CONF_SENDSPIN_STATIC_DELAY: 330,
            CONF_PROTOCOL_EXPERIMENTAL_NOTE: SENDSPIN_CAST_EXPERIMENTAL_NOTE,
        },
    )


@pytest.mark.parametrize("cast_there", [True, False], ids=["cast there", "cast away"])
async def test_an_upgrade_keeps_the_airplay_output_in_use_next_to_a_cast_default(
    cast_there: bool,
) -> None:
    """Two outputs become the one AirPlay carried, in every start order, with Cast away or not."""
    device = _device()
    device.store_output(AIRPLAY_CLIENT, AIRPLAY_ID, values={CONF_SENDSPIN_STATIC_DELAY: 77})
    _store_cast_default(device)
    settings = await _upgraded(device)

    assert list(settings["player_protocols"]) == [
        AIRPLAY_ID,
        SQUEEZELITE_ID,
        CAST_ID,
        AIRPLAY_CLIENT,
    ]
    values = settings["player_protocols"][AIRPLAY_CLIENT]["values"]
    assert values[CONF_SENDSPIN_STATIC_DELAY] == 77
    assert values[CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {CAST_ID: 330}
    attached = DOMAINS if cast_there else ("airplay", "squeezelite")
    await _assert_in_every_start_order(
        settings, DOMAINS, attached, ("squeezelite", AIRPLAY_CLIENT, "squeezelite", "on")
    )


@pytest.mark.parametrize("cast_there", [True, False], ids=["cast there", "cast away"])
async def test_an_upgrade_keeps_an_airplay_output_its_user_switched_off(cast_there: bool) -> None:
    """A device without Sendspin by the choice of its user stays without, in every start order."""
    device = _device()
    device.store_output(
        AIRPLAY_CLIENT, AIRPLAY_ID, enabled=False, values={CONF_SENDSPIN_STATIC_DELAY: 77}
    )
    _store_cast_default(device)
    settings = await _upgraded(device)

    attached = DOMAINS if cast_there else ("airplay", "squeezelite")
    await _assert_in_every_start_order(
        settings, DOMAINS, attached, (None, AIRPLAY_CLIENT, "squeezelite", "off-user")
    )


@pytest.mark.parametrize("other", ["airplay", "squeezelite"])
async def test_an_upgrade_lets_another_bridge_take_a_cast_default_of_its_client_id(
    other: str,
) -> None:
    """The output Cast left off under the client id of both is carried by the other bridge."""
    subset = (other, "chromecast")
    device = _device(subset, shared=True)
    _store_cast_default(device)
    settings = await _upgraded(device)

    assert settings["player_protocols"][CAST_CLIENT]["values"][CONF_PROTOCOL_OFF_BY_DEFAULT] is True
    for attached in (subset, (other,)):
        await _assert_in_every_start_order(
            settings, subset, attached, (other, CAST_CLIENT, other, "on"), shared=True
        )


@pytest.mark.parametrize("enabled", [True, False], ids=["output on", "output off"])
async def test_an_upgrade_hands_an_output_whose_carrier_is_gone_to_the_first_bridge(
    enabled: bool,
) -> None:
    """An output of a protocol that is not stored anymore is taken over as it is stored."""
    subset = ("airplay", "squeezelite")
    device = _device(subset)
    device.store_output(
        "spb_112233445566", "gone", enabled=enabled, values={CONF_SENDSPIN_STATIC_DELAY: 40}
    )
    settings = await _upgraded(device)

    expected = (
        ("squeezelite", "spb_112233445566", "squeezelite", "on")
        if enabled
        else (None, "spb_112233445566", "squeezelite", "off-user")
    )
    await _assert_in_every_start_order(settings, subset, subset, expected)


# ---------------------------------------------------------------- sequences that went wrong before


@pytest.mark.parametrize("shared", [False, True], ids=["own ids", "one id"])
async def test_switching_the_output_off_only_stops_the_bridge(shared: bool) -> None:
    """The output switched off keeps its record and its settings, and no bridge takes it."""
    device = _device(shared=shared)
    for protocol in device.protocols.values():
        await device.attach(protocol)
    client_id = device.state()[1]
    device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 120
    device.timeline.clear()

    await device.switch_output(False)

    assert device.timeline == [("stopped", "squeezelite")]
    assert device.state() == (None, client_id, "squeezelite", "off-user")
    assert device.output()["values"] == {
        CONF_UNDERLYING_PLAYER_ID: SQUEEZELITE_ID,
        CONF_SENDSPIN_STATIC_DELAY: 120,
    }


@pytest.mark.parametrize("shared", [False, True], ids=["own ids", "one id"])
@pytest.mark.parametrize("cast_there", [True, False], ids=["cast there", "cast away"])
async def test_airplay_switched_on_takes_an_output_cast_has_switched_off(
    shared: bool, cast_there: bool
) -> None:
    """AirPlay switched on carries the one output: no second one, and the settings stay."""
    device = _device(("airplay", "chromecast"), shared=shared)
    device.records()[device.airplay.protocol_id]["enabled"] = False
    # switched off by its user while Cast carried it
    device.store_output(
        CAST_CLIENT, CAST_ID, enabled=False, values={CONF_SENDSPIN_STATIC_DELAY: 123}
    )
    if cast_there:
        await device.attach(device.cast)

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert device.state() == (None, CAST_CLIENT, "airplay", "off-user")
    assert device.output()["values"][CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {CAST_ID: 123}
    assert device.timeline == []


@pytest.mark.parametrize("shared", [False, True], ids=["own ids", "one id"])
async def test_an_output_that_is_off_stays_with_squeezelite_while_its_provider_is_not_loaded(
    shared: bool,
) -> None:
    """AirPlay does not register an output of its own next to one squeezelite has, switched off."""
    device = _device(("airplay", "squeezelite"), shared=shared)
    device.store_output(
        SQUEEZELITE_CLIENT,
        SQUEEZELITE_ID,
        enabled=False,
        values={CONF_SENDSPIN_STATIC_DELAY: 77},
    )
    before = deepcopy(device.output())
    await device.unload_provider("squeezelite")

    await device.attach(device.airplay)

    assert device.state() == (None, SQUEEZELITE_CLIENT, "squeezelite", "off-user")
    assert device.output() == before
    assert device.timeline == []


async def test_the_output_stays_stored_while_the_set_up_of_its_new_carrier_waits() -> None:
    """A hand-over never leaves a moment without the output, however long the new bridge takes."""
    device = _device(("airplay", "squeezelite"), shared=True)
    await device.attach(device.airplay)
    device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 77
    # the squeezelite bridge reads what it learned about its player before it registers
    gate = asyncio.Event()

    async def _read_later(*_args: Any, **_kwargs: Any) -> None:
        await gate.wait()

    device.mass.cache.get = AsyncMock(side_effect=_read_later)
    device.timeline.clear()

    await device.attach(device.squeezelite)

    assert device.timeline == [("stopped", "airplay")]
    assert device.output()["enabled"] is True
    assert device.output()["values"] == {
        CONF_UNDERLYING_PLAYER_ID: SQUEEZELITE_ID,
        CONF_SENDSPIN_BRIDGE_STATIC_DELAYS: {SAME_ID_AIRPLAY_ID: 77},
    }

    gate.set()
    await device.settle()

    assert device.state() == ("squeezelite", SQUEEZELITE_CLIENT, "squeezelite", "on")


async def test_switching_off_a_carrier_that_is_away_hands_the_output_over_at_once() -> None:
    """A protocol switched off while its device is away gives its place up without a restart."""
    device = _device()
    for protocol in device.protocols.values():
        await device.attach(protocol)
    await device.detach(device.squeezelite)
    assert device.state() == (None, AIRPLAY_CLIENT, "squeezelite", "on")

    await device.switch(device.squeezelite, False)

    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")

    await device.detach(device.airplay)
    await device.switch(device.airplay, False)

    assert device.state() == (None, AIRPLAY_CLIENT, "chromecast", "off-default")


@pytest.mark.parametrize("other", ["airplay", "squeezelite"])
async def test_an_output_cast_carried_and_its_user_switched_off_is_not_stuck_with_cast(
    other: str,
) -> None:
    """Another bridge takes the output of a Cast protocol that is away, and leaves it off."""
    device = _device((other, "chromecast"), shared=True)
    device.store_output(
        CAST_CLIENT, CAST_ID, enabled=False, values={CONF_SENDSPIN_STATIC_DELAY: 123}
    )

    await device.attach(device.protocols[other])

    assert device.state() == (None, CAST_CLIENT, other, "off-user")

    await device.switch_output(True)

    assert device.state() == (other, CAST_CLIENT, other, "on")
    assert device.output()["values"][CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {CAST_ID: 123}
