"""Tests for the check of an AirPlay device that the players controller asks for."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator, Coroutine, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import ServiceStateChange

import music_assistant.providers.airplay.player as airplay_player_module
from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    CONF_STREAMING_MODE,
    DEVICE_CHECK_TIMEOUT,
    DEVICE_PROBE_MAX_PAUSE,
    DEVICE_PROBE_PAUSE,
    RAOP_DISCOVERY_TYPE,
    STREAMING_MODE_RAOP,
)
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.player import AirPlayPlayer, GenericAirPlayPlayer
from tests.providers.airplay.test_control_player import _make_control_player
from tests.providers.airplay.test_provider import _make_provider

# _airplay._tcp features bitmask with the AirPlay 2 feature bits set (bit 38/48).
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"
# the sleep of asyncio, which a test patches to record the pauses of the probe
_SLEEP = asyncio.sleep
DEVICE_ID = "AA:BB:CC:DD:EE:FF"
PROTOCOL_ID = "apaabbccddeeff"


def _service_info(service_type: str, port: int | None) -> MagicMock:
    """
    Create the mDNS record of a service of the device.

    :param service_type: The service type, _airplay or _raop.
    :param port: The port the record names.
    """
    info = MagicMock()
    info.type = service_type
    info.name = f"Den.{service_type}"
    info.port = port
    info.decoded_properties = {"deviceid": DEVICE_ID, "features": AP2_FEATURES}
    info.properties = {}
    # only loopback, which a record never moves the address of the device to
    info.ip_addresses_by_version.return_value = []
    return info


def _receiver(
    *,
    airplay_port: int | None = None,
    raop_port: int | None = None,
    streaming_mode: str | None = None,
) -> GenericAirPlayPlayer:
    """
    Create an AirPlay receiver on loopback, attached to a player, with the records it has.

    :param airplay_port: The port of its _airplay record, None for no such record.
    :param raop_port: The port of its _raop record, None for no such record.
    :param streaming_mode: The streaming mode it is set to, Automatic when omitted.
    """
    provider = MagicMock()
    provider.logger = logging.getLogger("test.airplay.device_check")
    config = MagicMock()
    values = {CONF_STREAMING_MODE: streaming_mode} if streaming_mode else {}
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    provider.mass.config.get_base_protocol_config.return_value = config
    provider.mass.config.get_protocol_value.side_effect = lambda _id, _key, default=None: default
    provider.mass.create_task.side_effect = _run_task
    receiver = GenericAirPlayPlayer(
        provider=provider,
        protocol_id=PROTOCOL_ID,
        raop_discovery_info=(
            _service_info(RAOP_DISCOVERY_TYPE, raop_port) if raop_port is not None else None
        ),
        airplay_discovery_info=(
            _service_info(AIRPLAY_DISCOVERY_TYPE, airplay_port)
            if airplay_port is not None
            else None
        ),
        address="127.0.0.1",
        display_name="Den",
        manufacturer="Test",
        model="Speaker",
    )
    receiver.set_player(MagicMock())
    receiver.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return receiver


async def _settle() -> None:
    """Let the tasks that are due run."""
    for _ in range(3):
        await _SLEEP(0)


def _run_task(coro: Coroutine[Any, Any, Any], **_kwargs: object) -> asyncio.Task[Any]:
    """Run a coroutine in a task of its own, as the server does."""
    return asyncio.get_running_loop().create_task(coro)


def _closed_port() -> int:
    """Return a loopback port that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def _listening() -> AsyncIterator[int]:
    """Accept connections on a loopback port, and return the port."""

    async def _accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


class _Device:
    """Answers the probes of the port of a device as a test tells it to."""

    def __init__(self, *answers: bool) -> None:
        """
        Answer the probes in turn, the last answer for every probe after those.

        :param answers: What the port answers to each probe.
        """
        self.answers = list(answers)
        self.probes: list[tuple[str, int, float]] = []

    async def probe(self, host: str, port: int, timeout: float) -> bool:
        """Answer a probe of a port."""
        self.probes.append((host, port, timeout))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@contextlib.contextmanager
def _pauses(*, wait: bool = False) -> Iterator[list[float]]:
    """
    Record the pauses of the probe, which pass at once.

    :param wait: Whether a pause longer than 0 lasts until the test ends instead.
    """
    pauses: list[float] = []

    async def _sleep(delay: float) -> None:
        pauses.append(delay)
        if wait and delay > 0:
            await asyncio.Event().wait()
        await _SLEEP(0)

    # the probe sleeps through asyncio.sleep
    with patch.object(asyncio, "sleep", _sleep):
        yield pauses


async def _probe_ended(protocol: AirPlayPlayer) -> None:
    """Wait until the probe of a protocol ended."""
    for _ in range(100):
        if protocol._device_probe is None:
            return
        await _SLEEP(0)
    pytest.fail("the probe did not end")


async def _gone(protocol: AirPlayPlayer) -> asyncio.Task[None]:
    """
    Let a check find the device of a protocol gone, and return the probe that started.

    :param protocol: The protocol to check, which probes through a patched probe.
    """
    assert await protocol.check_device() is False
    probe = protocol._device_probe
    assert probe is not None
    return probe


async def test_a_device_that_answers_reads_available() -> None:
    """A device that accepts a connection on its stream port reads available, and nothing more."""
    async with _listening() as port:
        receiver = _receiver(airplay_port=port)

        assert await receiver.check_device() is True

    assert receiver.available is True
    assert receiver._device_probe is None
    cast("MagicMock", receiver.update_state).assert_not_called()
    cast("MagicMock", receiver.provider).bridge_manager.stop_streaming.assert_not_called()


async def test_a_device_that_does_not_answer_reads_unavailable_and_stays_attached() -> None:
    """
    A device that refuses a connection on its stream port reads unavailable.

    The protocol stays attached to its player, so it keeps the say for its device, and it
    probes the device again.
    """
    receiver = _receiver(airplay_port=_closed_port())
    player = receiver.player

    with _pauses(wait=True) as pauses:
        assert await receiver.check_device() is False
        await _settle()

        assert receiver.available is False
        assert receiver.player is player
        cast("MagicMock", receiver.update_state).assert_called_once_with()
        assert receiver._device_probe is not None
        assert pauses == [DEVICE_PROBE_PAUSE]
        await receiver.on_detach()


@pytest.mark.parametrize(
    ("records", "streaming_mode", "port"),
    [
        ({"airplay_port": 7100, "raop_port": 5100}, None, 7100),
        ({"airplay_port": 0, "raop_port": 5100}, None, 7000),
        ({"airplay_port": 7100, "raop_port": 5100}, STREAMING_MODE_RAOP, 5100),
        ({"raop_port": 5100}, None, 5100),
        ({"raop_port": 0}, None, 5000),
    ],
    ids=[
        "AirPlay 2",
        "AirPlay 2 without a port",
        "RAOP streaming mode",
        "RAOP only",
        "RAOP without a port",
    ],
)
async def test_the_check_probes_the_port_the_stream_connects_to(
    records: dict[str, int], streaming_mode: str | None, port: int
) -> None:
    """The device is probed on the port of the record its stream connects to."""
    receiver = _receiver(**records, streaming_mode=streaming_mode)
    device = _Device(True)

    with patch.object(airplay_player_module, "port_accepts_connection", device.probe):
        assert await receiver.check_device() is True

    assert device.probes == [("127.0.0.1", port, DEVICE_CHECK_TIMEOUT)]


async def test_a_device_comes_back_by_the_repeated_probe() -> None:
    """
    A device that a check found gone is probed until it answers, with a growing pause.

    Its power is switched on outside Music Assistant and nobody plays: the probe finds it,
    and the protocol reads available again.
    """
    receiver = _receiver(airplay_port=7000)
    device = _Device(False, False, False, False, False, False, True)

    with (
        patch.object(airplay_player_module, "port_accepts_connection", device.probe),
        _pauses() as pauses,
    ):
        await _gone(receiver)
        await _probe_ended(receiver)

    assert pauses == [
        DEVICE_PROBE_PAUSE,
        DEVICE_PROBE_PAUSE * 2,
        DEVICE_PROBE_PAUSE * 4,
        DEVICE_PROBE_MAX_PAUSE,
        DEVICE_PROBE_MAX_PAUSE,
        DEVICE_PROBE_MAX_PAUSE,
    ]
    assert len(device.probes) == 7
    assert receiver.available is True
    assert cast("MagicMock", receiver.update_state).call_count == 2


async def test_a_device_comes_back_by_its_mdns_announcement() -> None:
    """
    A device that a check found gone is probed at once when it announces itself again.

    The provider passes a new record of the device on to its protocol, which by itself
    leaves the protocol as it is; the protocol probes the device at once, and reads
    available as it answers.
    """
    receiver = _receiver(airplay_port=7000)
    provider = _make_provider()
    cast("MagicMock", provider.mass).players.get_protocol.return_value = receiver
    device = _Device(False, True)

    with (
        patch.object(airplay_player_module, "port_accepts_connection", device.probe),
        _pauses(wait=True) as pauses,
    ):
        await _gone(receiver)
        await _settle()
        assert pauses == [DEVICE_PROBE_PAUSE]

        record = _service_info(AIRPLAY_DISCOVERY_TYPE, 7000)
        await provider.on_mdns_service_state_change(record.name, ServiceStateChange.Updated, record)
        await _probe_ended(receiver)

    assert pauses == [DEVICE_PROBE_PAUSE, 0]
    assert receiver.airplay_discovery_info is record
    assert receiver.available is True


async def test_an_announcement_of_a_device_that_reads_available_probes_nothing() -> None:
    """A record of a device that no check found gone starts no probe."""
    receiver = _receiver(airplay_port=7000)
    provider = _make_provider()
    cast("MagicMock", provider.mass).players.get_protocol.return_value = receiver
    device = _Device(True)
    record = _service_info(AIRPLAY_DISCOVERY_TYPE, 7000)

    with patch.object(airplay_player_module, "port_accepts_connection", device.probe):
        await provider.on_mdns_service_state_change(record.name, ServiceStateChange.Updated, record)

    assert receiver._device_probe is None
    assert device.probes == []
    assert receiver.available is True


async def test_a_later_check_that_answers_brings_the_device_back() -> None:
    """A check that the device answers, while a probe waits, ends the probe."""
    receiver = _receiver(airplay_port=7000)
    device = _Device(False)

    with (
        patch.object(airplay_player_module, "port_accepts_connection", device.probe),
        _pauses(wait=True),
    ):
        probe = await _gone(receiver)
        device.answers = [True]

        assert await receiver.check_device() is True
        await _settle()

    assert probe.cancelled()
    assert receiver._device_probe is None
    assert receiver.available is True


@pytest.mark.parametrize("meanwhile", ["detached", "another address", "another port"])
async def test_a_check_changes_nothing_once_its_answer_is_stale(meanwhile: str) -> None:
    """A check whose device was detached or got another record during the probe changes nothing."""
    receiver = _receiver(airplay_port=7000)
    probed = asyncio.Event()
    deliver = asyncio.Event()

    async def _probe(_host: str, _port: int, _timeout: float) -> bool:
        probed.set()
        await deliver.wait()
        return False

    with patch.object(airplay_player_module, "port_accepts_connection", _probe):
        checking = asyncio.create_task(receiver.check_device())
        await probed.wait()
        if meanwhile == "detached":
            receiver.set_player(None)
        elif meanwhile == "another address":
            receiver.address = "127.0.0.2"
        else:
            receiver.set_discovery_info(_service_info(AIRPLAY_DISCOVERY_TYPE, 7100), "Den")
        cast("MagicMock", receiver.update_state).reset_mock()
        deliver.set()

        assert await checking is False

    assert receiver.available is True
    assert receiver._device_probe is None
    cast("MagicMock", receiver.update_state).assert_not_called()


async def test_a_probe_that_answers_once_detached_changes_nothing() -> None:
    """A probe whose device answers after its protocol was detached does not bring it back."""
    receiver = _receiver(airplay_port=7000)
    device = _Device(False)
    probed = asyncio.Event()

    async def _probe(host: str, port: int, timeout: float) -> bool:
        answer = await device.probe(host, port, timeout)
        probed.set()
        return answer

    with (
        patch.object(airplay_player_module, "port_accepts_connection", _probe),
        _pauses(),
    ):
        await _gone(receiver)
        probed.clear()
        receiver.set_player(None)
        device.answers = [True]
        await probed.wait()
        await _settle()

        assert receiver._attr_available is False
        await receiver.on_detach()


async def test_a_detach_stops_the_probe_and_forgets_the_check() -> None:
    """
    A protocol taken off its player stops probing, and reads available when attached again.

    The provider unload takes every protocol off its player the same way.
    """
    receiver = _receiver(airplay_port=7000)
    device = _Device(False)

    with (
        patch.object(airplay_player_module, "port_accepts_connection", device.probe),
        _pauses(wait=True),
    ):
        probe = await _gone(receiver)
        receiver.set_player(None)
        await receiver.on_detach()
        await _settle()

    assert probe.cancelled()
    assert receiver._device_probe is None
    assert receiver.available is True


async def test_a_cancelled_check_changes_nothing() -> None:
    """A check that is cancelled during its probe leaves the protocol as it was."""
    receiver = _receiver(airplay_port=7000)
    probed = asyncio.Event()

    async def _probe(_host: str, _port: int, _timeout: float) -> bool:
        probed.set()
        await asyncio.Event().wait()
        return False

    with patch.object(airplay_player_module, "port_accepts_connection", _probe):
        checking = asyncio.create_task(receiver.check_device())
        await probed.wait()
        checking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await checking

    assert receiver.available is True
    assert receiver._device_probe is None
    cast("MagicMock", receiver.update_state).assert_not_called()


async def test_a_check_that_fails_leaves_the_stream_session_to_the_others() -> None:
    """
    A device that a check found gone leaves the stream session it plays in.

    The server sends no stop to a player that is switched off at its power control, so the
    session would otherwise keep streaming to a device without power; the other members of
    a group play on.
    """
    receiver = _receiver(airplay_port=7000)
    session = MagicMock(remove_client=AsyncMock())
    receiver.stream = MagicMock(running=True, session=session)
    bridge_manager = cast("MagicMock", receiver.provider).bridge_manager

    with (
        patch.object(airplay_player_module, "port_accepts_connection", _Device(False).probe),
        _pauses(wait=True),
    ):
        await _gone(receiver)
        await _settle()

        session.remove_client.assert_awaited_once_with(receiver, reason="device does not answer")
        session.stop.assert_not_called()
        bridge_manager.stop_streaming.assert_not_called()

        # a later check that fails again leaves nothing anew
        assert await receiver.check_device() is False
        await _settle()
        session.remove_client.assert_awaited_once()
        await receiver.on_detach()


async def test_a_check_that_fails_stops_the_sendspin_bridge_that_feeds_the_device() -> None:
    """A device that a check found gone and that plays through its Sendspin bridge is let go."""
    receiver = _receiver(airplay_port=7000)
    bridge_manager = cast("MagicMock", receiver.provider).bridge_manager

    with (
        patch.object(airplay_player_module, "port_accepts_connection", _Device(False).probe),
        _pauses(wait=True),
    ):
        await _gone(receiver)
        await _settle()

        bridge_manager.stop_streaming.assert_called_once_with(PROTOCOL_ID)
        await receiver.on_detach()


async def test_an_apple_device_that_is_back_reaches_its_control_services_at_once() -> None:
    """
    An Apple device that a check found gone reconnects its control services as it is back.

    Its own reconnects may wait out a backoff of minutes.
    """
    apple_tv = _make_control_player()
    apple_tv.address = "127.0.0.1"
    apple_tv.set_player(MagicMock())
    apple_tv.update_state = MagicMock()  # type: ignore[misc, method-assign]
    cast("MagicMock", apple_tv.mass).create_task.side_effect = _run_task
    schedule_connection = MagicMock()
    device = _Device(False, True)

    with (
        patch.object(airplay_player_module, "port_accepts_connection", device.probe),
        patch.object(AirPlayControlPlayer, "_schedule_connection", schedule_connection),
        _pauses(),
    ):
        await _gone(apple_tv)
        schedule_connection.assert_not_called()
        await _probe_ended(apple_tv)

    assert device.probes == [
        ("127.0.0.1", 7000, DEVICE_CHECK_TIMEOUT),
        ("127.0.0.1", 7000, DEVICE_CHECK_TIMEOUT),
    ]
    assert apple_tv.available is True
    schedule_connection.assert_called_once_with()
