"""Tests for the decision which protocol of a player carries its one Sendspin output."""

from __future__ import annotations

import itertools
import logging
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.constants import (
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_OFF_BY_DEFAULT,
    CONF_UNDERLYING_PLAYER_ID,
)
from music_assistant.providers.chromecast.constants import SENDSPIN_CAST_EXPERIMENTAL_NOTE
from music_assistant.providers.sendspin import bridge_manager
from music_assistant.providers.sendspin.bridge_manager import (
    BRIDGE_ORDER,
    BridgeConnection,
    get_bridge_output_id,
    hand_static_delay_over,
    select_carrier,
)
from music_assistant.providers.sendspin.bridge_role import BRIDGE_ROLE_ID
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_BRIDGE_STATIC_DELAYS,
    CONF_SENDSPIN_CAST_OPT_IN,
    CONF_SENDSPIN_STATIC_DELAY,
)

from .bridge_harness import (
    AIRPLAY_CLIENT,
    AIRPLAY_ID,
    CAST_CLIENT,
    CAST_ID,
    OTHER_PLAYER_ID,
    PLAYER_ID,
    SAME_ID_AIRPLAY_ID,
    SQUEEZELITE_CLIENT,
    SQUEEZELITE_ID,
    BridgedDevice,
    hello,
)

# the devices of a test, whose Sendspin servers are closed after it
_DEVICES: list[BridgedDevice] = []


@pytest.fixture(autouse=True)
async def _close_devices() -> AsyncGenerator[None]:
    """Stop the bridges and close the Sendspin servers of the devices of a test."""
    yield
    while _DEVICES:
        await _DEVICES.pop().close()


def _device(**kwargs: Any) -> BridgedDevice:
    """
    Return a device that is closed after the test.

    :param kwargs: What the device has, see BridgedDevice.
    """
    device = BridgedDevice(**kwargs)
    _DEVICES.append(device)
    return device


# ---------------------------------------------------------------- the decision


def _there(domain: str, *, qualifies: bool = True) -> BridgeConnection:
    """Return a connection the user has switched on, whose device is there."""
    return BridgeConnection(domain, domain, switched_on=True, attached=True, qualifies=qualifies)


def _away(domain: str) -> BridgeConnection:
    """Return a connection the user has switched on: its device is away, or its provider loads."""
    return BridgeConnection(domain, domain, switched_on=True, attached=False)


def _off(domain: str) -> BridgeConnection:
    """Return a connection the user switched off, or whose provider the user disabled."""
    return BridgeConnection(domain, domain, switched_on=False, attached=False)


AIRPLAY, SQUEEZELITE, CAST = "airplay", "squeezelite", "chromecast"


def test_the_order_of_the_bridges() -> None:
    """AirPlay carries the output of a device before squeezelite, and Cast comes last."""
    assert BRIDGE_ORDER == (AIRPLAY, SQUEEZELITE, CAST)


@pytest.mark.parametrize(
    ("connections", "stored_carrier", "carrier"),
    [
        # one connection
        pytest.param([_there(AIRPLAY)], None, AIRPLAY, id="airplay only"),
        pytest.param([_there(SQUEEZELITE)], None, SQUEEZELITE, id="squeezelite only"),
        pytest.param([_there(CAST)], None, CAST, id="cast only"),
        pytest.param([_away(CAST)], None, None, id="cast only, away, never carried"),
        pytest.param([_away(CAST)], CAST, CAST, id="cast only, away, carried"),
        pytest.param([_off(CAST)], CAST, None, id="cast only, switched off"),
        pytest.param([_there(CAST, qualifies=False)], CAST, None, id="cast only, blocked"),
        # airplay and squeezelite
        pytest.param([_there(AIRPLAY), _there(SQUEEZELITE)], None, AIRPLAY, id="ap+sq"),
        pytest.param([_there(AIRPLAY), _there(SQUEEZELITE)], SQUEEZELITE, AIRPLAY, id="ap+sq, sq"),
        pytest.param([_off(AIRPLAY), _there(SQUEEZELITE)], AIRPLAY, SQUEEZELITE, id="ap off, sq"),
        pytest.param([_away(AIRPLAY), _there(SQUEEZELITE)], AIRPLAY, AIRPLAY, id="ap away, sq"),
        pytest.param(
            [_away(AIRPLAY), _there(SQUEEZELITE)], None, SQUEEZELITE, id="ap away new, sq"
        ),
        pytest.param(
            [_away(AIRPLAY), _there(SQUEEZELITE)], SQUEEZELITE, SQUEEZELITE, id="ap away, sq had it"
        ),
        pytest.param([_there(AIRPLAY), _away(SQUEEZELITE)], SQUEEZELITE, AIRPLAY, id="ap, sq away"),
        pytest.param([_there(AIRPLAY), _off(SQUEEZELITE)], SQUEEZELITE, AIRPLAY, id="ap, sq off"),
        pytest.param(
            [_there(AIRPLAY, qualifies=False), _there(SQUEEZELITE)],
            AIRPLAY,
            SQUEEZELITE,
            id="ap does not qualify, sq",
        ),
        pytest.param(
            [_away(AIRPLAY), _away(SQUEEZELITE)], SQUEEZELITE, SQUEEZELITE, id="both away"
        ),
        pytest.param([_away(AIRPLAY), _away(SQUEEZELITE)], None, None, id="both away, no output"),
        pytest.param([_off(AIRPLAY), _off(SQUEEZELITE)], AIRPLAY, None, id="both off"),
        # airplay and cast
        pytest.param([_there(AIRPLAY), _there(CAST)], CAST, AIRPLAY, id="ap+cc, cc had it"),
        pytest.param([_off(AIRPLAY), _there(CAST)], AIRPLAY, CAST, id="ap off, cc"),
        pytest.param([_away(AIRPLAY), _there(CAST)], AIRPLAY, AIRPLAY, id="ap away, cc"),
        pytest.param([_away(AIRPLAY), _there(CAST)], CAST, CAST, id="ap away, cc had it"),
        pytest.param([_there(AIRPLAY), _away(CAST)], CAST, AIRPLAY, id="ap, cc away"),
        # squeezelite and cast
        pytest.param([_there(SQUEEZELITE), _there(CAST)], CAST, SQUEEZELITE, id="sq+cc, cc had it"),
        pytest.param([_off(SQUEEZELITE), _there(CAST)], SQUEEZELITE, CAST, id="sq off, cc"),
        pytest.param(
            [_away(SQUEEZELITE), _there(CAST)], SQUEEZELITE, SQUEEZELITE, id="sq away, cc"
        ),
        pytest.param([_away(SQUEEZELITE), _there(CAST)], None, CAST, id="sq away new, cc"),
        pytest.param(
            [_there(SQUEEZELITE, qualifies=False), _there(CAST)], None, CAST, id="sq tool, cc"
        ),
        # all three
        pytest.param(
            [_there(AIRPLAY), _there(SQUEEZELITE), _there(CAST)], None, AIRPLAY, id="triple"
        ),
        pytest.param(
            [_off(AIRPLAY), _there(SQUEEZELITE), _there(CAST)], AIRPLAY, SQUEEZELITE, id="ap off"
        ),
        pytest.param(
            [_off(AIRPLAY), _off(SQUEEZELITE), _there(CAST)], SQUEEZELITE, CAST, id="ap, sq off"
        ),
        pytest.param(
            [_off(AIRPLAY), _away(SQUEEZELITE), _there(CAST)],
            SQUEEZELITE,
            SQUEEZELITE,
            id="ap off, sq away",
        ),
        pytest.param(
            [_off(AIRPLAY), _away(SQUEEZELITE), _there(CAST)], CAST, CAST, id="ap off, sq away new"
        ),
        pytest.param(
            [_away(AIRPLAY), _there(SQUEEZELITE), _there(CAST)], AIRPLAY, AIRPLAY, id="ap away"
        ),
        pytest.param(
            [_away(AIRPLAY), _there(SQUEEZELITE), _there(CAST)],
            CAST,
            SQUEEZELITE,
            id="ap away new, cc had it",
        ),
        pytest.param(
            [_away(AIRPLAY), _away(SQUEEZELITE), _there(CAST)], AIRPLAY, AIRPLAY, id="ap, sq away"
        ),
        pytest.param(
            [_away(AIRPLAY), _away(SQUEEZELITE), _away(CAST)], CAST, CAST, id="all away, cc had it"
        ),
        pytest.param([_off(AIRPLAY), _off(SQUEEZELITE), _off(CAST)], CAST, None, id="all off"),
        pytest.param([], AIRPLAY, None, id="no connection"),
    ],
)
def test_the_carrier_is_the_first_connection_that_holds_the_output(
    connections: list[BridgeConnection], stored_carrier: str | None, carrier: str | None
) -> None:
    """A connection holds the output when it qualifies while there, or carried it while away."""
    for order in itertools.permutations(connections):
        assert select_carrier(order, stored_carrier) == carrier


def test_every_combination_of_connections_keeps_to_the_rules() -> None:
    """Whatever the connections of a player are at, the carrier is one that may carry."""
    states = {
        "there": _there,
        "blocked": lambda domain: _there(domain, qualifies=False),
        "away": _away,
        "off": _off,
        "absent": None,
    }
    cases = 0
    for kinds in itertools.product(states, repeat=len(BRIDGE_ORDER)):
        connections = [
            make(domain)
            for domain, kind in zip(BRIDGE_ORDER, kinds, strict=True)
            if (make := states[kind]) is not None
        ]
        for stored_carrier in (None, *BRIDGE_ORDER):
            cases += 1
            carrier = select_carrier(connections, stored_carrier)
            by_id = {connection.protocol_id: connection for connection in connections}
            context = f"{dict(zip(BRIDGE_ORDER, kinds, strict=True))}, stored {stored_carrier}"
            if carrier is None:
                assert not any(
                    connection.switched_on and connection.attached and connection.qualifies
                    for connection in connections
                ), f"a connection that qualifies does not carry: {context}"
                continue
            chosen = by_id[carrier]
            assert chosen.switched_on, f"a connection that is switched off carries: {context}"
            if chosen.attached:
                assert chosen.qualifies, f"a connection that does not qualify carries: {context}"
            else:
                assert carrier == stored_carrier, f"one that never carried holds a place: {context}"
            for earlier in BRIDGE_ORDER[: BRIDGE_ORDER.index(carrier)]:
                other = by_id.get(earlier)
                if other is None or not other.switched_on:
                    continue
                holds = other.qualifies if other.attached else earlier == stored_carrier
                assert not holds, f"{earlier} holds the output, yet {carrier} carries: {context}"
    assert cases == 5**3 * 4


def test_a_connection_of_an_unlisted_provider_comes_last() -> None:
    """A bridge of a provider that is not in the order only carries when none of the others does."""
    other = BridgeConnection("x1", "other", switched_on=True, attached=True, qualifies=True)

    assert select_carrier([other, _there(CAST)], None) == CAST
    assert select_carrier([other, _off(CAST)], None) == "x1"


# ---------------------------------------------------------------- the connections of a player


async def test_the_connections_follow_the_config_and_what_is_attached() -> None:
    """A connection is switched on by the config alone, and qualifies only while it is attached."""
    device = _device(cast=True)
    await device.unload_provider("chromecast")
    device.squeezelite.client.device_model = "CastBridge"
    await device.attach(device.airplay)
    await device.attach(device.squeezelite)
    device.config.set("providers/squeezelite/enabled", False)

    connections = {
        connection.domain: connection
        for connection in bridge_manager._get_connections(device.mass, cast("Any", device.player))
    }

    assert connections == {
        "airplay": BridgeConnection(AIRPLAY_ID, "airplay", True, attached=True, qualifies=True),
        # a bridge tool, of a provider the user disabled
        "squeezelite": BridgeConnection(
            SQUEEZELITE_ID, "squeezelite", False, attached=True, qualifies=False
        ),
        # its provider is not loaded
        "chromecast": BridgeConnection(CAST_ID, "chromecast", True, attached=False),
    }


# ---------------------------------------------------------------- the delay of each carrier


def _delays(device: BridgedDevice) -> tuple[str | None, Any, Any]:
    """Return what the one output stores: its carrier, its delay and the delays put aside."""
    values = device.output()["values"]
    return (
        values.get(CONF_UNDERLYING_PLAYER_ID),
        values.get(CONF_SENDSPIN_STATIC_DELAY),
        values.get(CONF_SENDSPIN_BRIDGE_STATIC_DELAYS),
    )


@pytest.mark.parametrize("switched_on", [True, False], ids=["output on", "output off"])
async def test_the_static_delay_follows_the_carrier_there_and_back(switched_on: bool) -> None:
    """Each carrier has the delay the user set for it, also while the output is switched off."""
    device = _device(airplay_id=SAME_ID_AIRPLAY_ID)
    await device.attach(device.airplay)
    await device.attach(device.squeezelite)
    device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 77
    if not switched_on:
        await device.switch_output(False)

    await device.switch(device.airplay, False)

    assert _delays(device) == (SQUEEZELITE_ID, None, {SAME_ID_AIRPLAY_ID: 77})
    device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 15

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert _delays(device) == (SAME_ID_AIRPLAY_ID, 77, {SQUEEZELITE_ID: 15})

    await device.switch(device.airplay, False)

    assert _delays(device) == (SQUEEZELITE_ID, 15, {SAME_ID_AIRPLAY_ID: 77})
    assert device.state()[3] == ("on" if switched_on else "off-user")


async def test_the_delay_of_a_cast_output_is_set_aside_for_airplay_and_back() -> None:
    """The delay Cast had comes back when Cast carries the output again."""
    device = _device(airplay_id=SAME_ID_AIRPLAY_ID, squeezelite=False, cast=True)
    device.records()[SAME_ID_AIRPLAY_ID]["enabled"] = False
    device.store_output(
        CAST_CLIENT,
        CAST_ID,
        enabled=False,
        values={CONF_SENDSPIN_STATIC_DELAY: 123, CONF_PROTOCOL_OFF_BY_DEFAULT: True},
    )
    await device.attach(device.cast)

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert _delays(device) == (SAME_ID_AIRPLAY_ID, None, {CAST_ID: 123})
    device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 25

    await device.switch(device.airplay, False)

    assert _delays(device) == (CAST_ID, 123, {SAME_ID_AIRPLAY_ID: 25})

    await device.switch(device.airplay, True)
    await device.attach(device.airplay)

    assert _delays(device) == (SAME_ID_AIRPLAY_ID, 25, {CAST_ID: 123})


async def test_the_delay_hand_over_of_an_output_that_is_not_stored_does_nothing() -> None:
    """An output without a record, or that rides on the protocol already, is left alone."""
    device = _device()
    hand_static_delay_over(device.mass, "spb_unknown", SQUEEZELITE_ID)
    assert not device.outputs()
    device.store_output(AIRPLAY_CLIENT, AIRPLAY_ID, values={CONF_SENDSPIN_STATIC_DELAY: 77})
    values = device.output()["values"]

    hand_static_delay_over(device.mass, AIRPLAY_CLIENT, AIRPLAY_ID)

    assert device.output()["values"] is values
    assert _delays(device) == (AIRPLAY_ID, 77, None)


# ---------------------------------------------------------------- rule 4: the off of the user


@pytest.mark.parametrize("cast", [False, True], ids=["airplay+squeezelite", "all three"])
async def test_an_output_the_user_switched_off_stays_off_whatever_the_connections_do(
    cast: bool,
) -> None:
    """No bridge registers an output its user switched off, and its settings stay."""
    device = _device(cast=cast)
    for protocol in device.protocols.values():
        await device.attach(protocol)
    device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] = 120
    device.timeline.clear()

    await device.switch_output(False)

    assert device.timeline == [("stopped", "airplay")]
    assert device.state() == (None, AIRPLAY_CLIENT, "airplay", "off-user")
    device.timeline.clear()
    carriers: list[str | None] = []
    for protocol in device.protocols.values():
        # each connection is switched off in turn, away and back, and on again
        await device.switch(protocol, False)
        carriers.append(device.state()[2])
        await device.switch(protocol, True)
        await device.attach(protocol)
        await device.detach(protocol)
        await device.attach(protocol)
        assert device.state()[3] == "off-user"

    assert device.timeline == [], "a bridge registered an output its user switched off"
    # the output keeps naming a connection that is switched on, so its switch can be used
    assert carriers == ["squeezelite", "airplay", "airplay"][: len(device.protocols)]
    assert device.state() == (None, AIRPLAY_CLIENT, "airplay", "off-user")
    assert device.output()["values"][CONF_SENDSPIN_STATIC_DELAY] == 120

    await device.switch_output(True)

    assert device.state() == ("airplay", AIRPLAY_CLIENT, "airplay", "on")


async def test_an_output_the_user_switched_off_stays_off_when_cast_is_left_to_carry_it() -> None:
    """The off of the user is no default of Cast: another bridge does not switch it on later."""
    device = _device(cast=True)
    for protocol in device.protocols.values():
        await device.attach(protocol)
    await device.switch_output(False)

    await device.switch(device.airplay, False)
    await device.switch(device.squeezelite, False)

    assert device.state() == (None, AIRPLAY_CLIENT, "chromecast", "off-user")
    assert CONF_PROTOCOL_OFF_BY_DEFAULT not in device.output()["values"]

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)

    assert device.state() == (None, AIRPLAY_CLIENT, "squeezelite", "off-user")
    assert device.timeline[-1] == ("stopped", "airplay")


async def test_an_output_switched_on_over_cast_and_off_again_is_the_off_of_the_user() -> None:
    """What the user switched is no default of Cast anymore: no other bridge switches it on."""
    device = _device(airplay=False, cast=True)
    device.records()[SQUEEZELITE_ID]["enabled"] = False
    await device.attach(device.cast)
    assert device.state() == (None, CAST_CLIENT, "chromecast", "off-default")

    await device.switch_output(True)
    await device.switch_output(False)

    assert device.state() == (None, CAST_CLIENT, "chromecast", "off-user")

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)

    assert device.state() == (None, CAST_CLIENT, "squeezelite", "off-user"), (
        "squeezelite switched on an output its user switched off"
    )


# ---------------------------------------------------------------- rule 5: the default of Cast


@pytest.mark.parametrize("taker", ["airplay", "squeezelite"])
@pytest.mark.parametrize("cast_there", [True, False], ids=["cast there", "cast away"])
async def test_a_cast_default_does_not_keep_another_bridge_from_the_output(
    taker: str, cast_there: bool
) -> None:
    """A bridge before Cast takes the output Cast left off by default, and switches it on."""
    device = _device(airplay=taker == "airplay", squeezelite=taker == "squeezelite", cast=True)
    device.store_output(
        CAST_CLIENT,
        CAST_ID,
        enabled=False,
        values={
            CONF_SENDSPIN_STATIC_DELAY: 123,
            CONF_PROTOCOL_OFF_BY_DEFAULT: True,
            CONF_PROTOCOL_EXPERIMENTAL_NOTE: SENDSPIN_CAST_EXPERIMENTAL_NOTE,
        },
    )
    if cast_there:
        await device.attach(device.cast)
        assert device.state() == (None, CAST_CLIENT, "chromecast", "off-default")

    await device.attach(device.protocols[taker])

    assert device.state() == (taker, CAST_CLIENT, taker, "on")
    values = device.output()["values"]
    assert CONF_PROTOCOL_OFF_BY_DEFAULT not in values
    assert CONF_PROTOCOL_EXPERIMENTAL_NOTE not in values
    assert values[CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {CAST_ID: 123}


async def test_cast_leaves_an_output_it_takes_over_off_until_the_user_opts_in() -> None:
    """Sendspin over Cast is opt-in, also for an output that was switched on before."""
    device = _device(cast=True)
    for protocol in device.protocols.values():
        await device.attach(protocol)

    await device.switch(device.airplay, False)
    await device.switch(device.squeezelite, False)

    assert device.state() == (None, AIRPLAY_CLIENT, "chromecast", "off-default")

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)

    # the default of Cast was no choice of the user: the output is on again
    assert device.state() == ("squeezelite", AIRPLAY_CLIENT, "squeezelite", "on")

    await device.switch(device.squeezelite, False)
    await device.switch_output(True)

    assert device.state() == ("chromecast", AIRPLAY_CLIENT, "chromecast", "on")
    assert device.output()["values"][CONF_SENDSPIN_CAST_OPT_IN] is True

    await device.switch(device.squeezelite, True)
    await device.attach(device.squeezelite)
    await device.switch(device.squeezelite, False)

    # the opt-in is remembered: Cast carries the output at once the next time
    assert device.state() == ("chromecast", AIRPLAY_CLIENT, "chromecast", "on")


async def test_an_output_cast_switches_off_is_detached_also_without_a_bridge() -> None:
    """The default of Cast takes an attached output away that no bridge holds the client of."""
    device = _device(airplay=False, squeezelite=False, cast=True)
    # stored switched on, and never offered over Cast
    device.store_output(CAST_CLIENT, CAST_ID)
    # its client is there before the Cast protocol is, as a receiver that connected by itself
    device.server.register_external_player(
        hello(CAST_CLIENT, BRIDGE_ROLE_ID), on_stream_start=MagicMock()
    )
    await device.settle()
    assert device.sendspin_protocols == {CAST_CLIENT: CAST_ID}

    await device.attach(device.cast)

    assert device.output()["enabled"] is False
    assert device.output()["values"][CONF_PROTOCOL_OFF_BY_DEFAULT] is True
    assert CAST_CLIENT not in device.sendspin_protocols, "the output is still attached"
    assert device.manager_of(device.cast).get_bridge(CAST_ID) is None


# ---------------------------------------------------------------- one output per player


async def test_bridges_on_players_of_their_own_each_have_an_output() -> None:
    """Protocols on two players are two speakers, each with its own output."""
    device = _device()
    await device.attach(device.airplay)
    await device.attach(device.squeezelite, device.other_player)

    assert set(device.outputs()) == {AIRPLAY_CLIENT, SQUEEZELITE_CLIENT}
    assert device.outputs()[SQUEEZELITE_CLIENT]["player_id"] == OTHER_PLAYER_ID
    assert device.manager_of(device.squeezelite).get_bridge(SQUEEZELITE_ID) is not None


async def test_a_bridge_does_not_register_the_output_of_another_player() -> None:
    """A bridge whose id is that of the output of another player waits until it is linked."""
    device = _device(airplay_id=SAME_ID_AIRPLAY_ID)
    await device.attach(device.airplay)
    assert device.state() == ("airplay", SQUEEZELITE_CLIENT, "airplay", "on")
    device.timeline.clear()

    await device.attach(device.squeezelite, device.other_player)

    assert device.timeline == [], "the bridge registered the output of another player"
    assert device.manager_of(device.squeezelite).bridge_client_id(device.squeezelite) is None
    assert device.outputs()[SQUEEZELITE_CLIENT]["player_id"] == PLAYER_ID

    await device.link(device.squeezelite)

    # one speaker now: its output stays with the bridge that comes first
    assert device.state() == ("airplay", SQUEEZELITE_CLIENT, "airplay", "on")
    assert device.manager_of(device.squeezelite).bridge_client_id(device.squeezelite) == (
        SQUEEZELITE_CLIENT
    )


async def test_a_bridge_adopts_an_output_of_its_id_that_belongs_to_no_player() -> None:
    """An output without a player that still exists is taken by the bridge that meets it."""
    device = _device(airplay=False)
    device.store_output(SQUEEZELITE_CLIENT, "gone", values={CONF_SENDSPIN_STATIC_DELAY: 40})
    device.records()[SQUEEZELITE_CLIENT]["player_id"] = "pl_removed"

    await device.attach(device.squeezelite)

    assert device.state() == ("squeezelite", SQUEEZELITE_CLIENT, "squeezelite", "on")
    assert device.output()["player_id"] == PLAYER_ID
    assert device.output()["values"][CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {"gone": 40}


async def test_a_second_output_that_is_stored_is_reported_and_one_is_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Of several stored outputs the attached one is used, else one that is on, else the first."""
    device = _device()
    device.store_output("spb_2", AIRPLAY_ID, enabled=False)
    device.store_output("spb_1", AIRPLAY_ID, enabled=False)
    with caplog.at_level(logging.WARNING, logger=bridge_manager.LOGGER.name):
        assert get_bridge_output_id(device.mass, PLAYER_ID) == "spb_1"
        assert get_bridge_output_id(device.mass, PLAYER_ID) == "spb_1"
    assert len([record for record in caplog.records if "spb_1, spb_2" in record.message]) == 1

    device.records()["spb_2"]["enabled"] = True
    assert get_bridge_output_id(device.mass, PLAYER_ID) == "spb_2"

    device.records()["spb_1"]["enabled"] = True
    device.sendspin_protocols["spb_1"] = AIRPLAY_ID
    device.player.attached_protocols[AIRPLAY_ID] = device.airplay
    assert get_bridge_output_id(device.mass, PLAYER_ID) == "spb_1"
    del device.player.attached_protocols[AIRPLAY_ID]


async def test_an_output_registered_by_a_bridge_is_the_output_of_its_player_before_it_is_stored() -> (
    None
):
    """A bridge that takes over before the output is stored registers under the same client id."""
    device = _device(squeezelite=False, cast=True)
    # the client of the Cast bridge is registered, its Sendspin protocol not attached yet
    with patch.object(device, "_client_added", AsyncMock()):
        await device.attach(device.cast)
    assert device.manager_of(device.cast).get_bridge(CAST_ID) is not None
    assert not device.outputs()

    await device.attach(device.airplay)

    assert set(device.outputs()) == {CAST_CLIENT}, "the bridge registered an output of its own"
    assert device.state() == ("airplay", CAST_CLIENT, "airplay", "on")
