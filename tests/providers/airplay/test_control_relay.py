"""Regression tests for native HomePod volume relay and media passthrough."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyatv.protocols.mrp import messages, protobuf
from pyatv.protocols.mrp.protobuf.ProtocolMessage_pb2 import ProtocolMessage
from pyatv.scripts.atvproxy import AirPlayChannelAppleTVProxy
from pyatv.support.http import HttpRequest, HttpResponse, format_response, parse_response

from music_assistant.providers.airplay.control_relay import (
    HomePodControlRelay,
    RelayConnection,
    RelayDataChannel,
)


@pytest.fixture
def relay() -> HomePodControlRelay:
    """Create an isolated relay without opening network sockets."""
    return HomePodControlRelay(
        name="MA test Control",
        address="192.0.2.1",
        port=7000,
        properties={"gid": "owned-group", "psi": "physical-output", "flags": "0x4"},
        publish_ip="192.0.2.2",
        zeroconf=MagicMock(async_unregister_service=AsyncMock()),
        volume_confirmed=MagicMock(),
        volume_level=29,
    )


def _channel(relay: HomePodControlRelay) -> RelayDataChannel:
    """Create a protocol handler without creating encrypted transports."""
    channel = object.__new__(RelayDataChannel)
    channel.owner = relay
    return channel


def _notification(output: str, volume: float) -> Any:
    message = ProtocolMessage(type=protobuf.VOLUME_DID_CHANGE_MESSAGE)
    inner = protobuf.extract_inner(message)
    inner.outputDeviceUID = output
    inner.volume = volume
    return message


@pytest.mark.parametrize("output", ["", "physical-output", "owned-group"])
def test_volume_routes_directly_without_replacing_request(
    relay: HomePodControlRelay, output: str
) -> None:
    """The receiver gets the original request identifier, and owns its reply."""
    message = messages.set_volume(output, 0.29)
    message.identifier = "request-correlation"
    result = _channel(relay)._rewrite_client_protobuf(message)
    assert result is message
    assert result.identifier == "request-correlation"
    assert protobuf.extract_inner(result).outputDeviceUID == "physical-output"
    cast("MagicMock", relay.volume_confirmed).assert_not_called()


@pytest.mark.parametrize(
    ("output", "volume"),
    [("other-room", 0.29), ("", -0.01), ("", 1.01), ("", float("nan")), ("", float("inf"))],
)
def test_invalid_volume_never_reaches_receiver(
    relay: HomePodControlRelay, output: str, volume: float
) -> None:
    """Foreign outputs and invalid values cannot become device commands."""
    channel = _channel(relay)
    with pytest.raises(channel.IgnoreMessage):
        channel._rewrite_client_protobuf(messages.set_volume(output, volume))
    assert relay.pending_volume is None


def test_adopt_only_matching_fresh_receiver_confirmation(relay: HomePodControlRelay) -> None:
    """Unsolicited, stale, foreign, duplicate and superseded updates do not alter MA."""
    callback = cast("MagicMock", relay.volume_confirmed)
    relay.observe_volume(_notification("physical-output", 0.0))
    assert relay.accept_volume(messages.set_volume("", 0.29))
    relay.observe_volume(_notification("other-room", 0.29))
    relay.observe_volume(_notification("physical-output", 0.28))
    callback.assert_not_called()
    relay.observe_volume(_notification("physical-output", 0.29))
    callback.assert_called_once_with(29)
    relay.observe_volume(_notification("physical-output", 0.29))
    callback.assert_called_once()
    callback.reset_mock()
    assert relay.accept_volume(messages.set_volume("", 0.3))
    assert relay.accept_volume(messages.set_volume("", 0.31))
    relay.observe_volume(_notification("physical-output", 0.3))
    callback.assert_not_called()
    with patch("music_assistant.providers.airplay.control_relay.time.monotonic", return_value=1e20):
        relay.observe_volume(_notification("physical-output", 0.31))
    callback.assert_not_called()
    assert relay.pending_volume is None


def test_artwork_and_real_receiver_replies_are_unchanged(relay: HomePodControlRelay) -> None:
    """Never replace artwork, media state or the correlated receiver response."""
    channel = _channel(relay)
    request = ProtocolMessage(type=protobuf.PLAYBACK_QUEUE_REQUEST_MESSAGE)
    protobuf.extract_inner(request).contentItemIdentifiers.append("track")
    original = request.SerializeToString()
    assert channel._rewrite_client_protobuf(request) is None
    assert request.SerializeToString() == original
    state = ProtocolMessage(type=protobuf.SET_STATE_MESSAGE)
    item = protobuf.extract_inner(state).playbackQueue.contentItems.add()
    item.identifier = "track"
    item.metadata.title = "Original title"
    item.artworkData = b"original-jpeg"
    reply = ProtocolMessage(type=ProtocolMessage.UNKNOWN_MESSAGE)
    reply.identifier = "real-receiver-reply"
    for message in (state, reply):
        original = message.SerializeToString()
        assert channel._rewrite_remote_protobuf(message) is None
        assert message.SerializeToString() == original


async def test_failed_discovery_withdrawal_still_closes_connections(
    relay: HomePodControlRelay,
) -> None:
    """Loss of mDNS cannot leave a listener or client connection alive."""
    client = MagicMock()

    async def wait_closed() -> None:
        client.close.assert_called_once()

    server = MagicMock(wait_closed=AsyncMock(side_effect=wait_closed))
    relay.server = server
    relay.clients.add(client)
    relay.service = MagicMock()
    cast("AsyncMock", relay.zeroconf.async_unregister_service).side_effect = RuntimeError(
        "mDNS unavailable"
    )
    await relay.close()
    server.close.assert_called_once()
    client.close.assert_called_once()
    assert relay.closed
    assert not relay.clients
    assert not relay.accept_volume(messages.set_volume("", 0.3))
    await relay.close()
    client.close.assert_called_once()


async def test_disconnect_cancels_channel_connection(relay: HomePodControlRelay) -> None:
    """An in-flight remote connection must not survive the local client."""
    channel = _channel(relay)
    channel.loop = asyncio.get_running_loop()
    channel.remote_transport = None
    channel.remote_protocol = None
    channel.transport = None
    transport = MagicMock()
    transport.get_extra_info.return_value.getpeername.return_value = ("192.0.2.2", 1234)
    started = asyncio.Event()

    async def connect() -> None:
        started.set()
        await asyncio.Future()

    with patch.object(AirPlayChannelAppleTVProxy, "_connect_to_remote", side_effect=connect):
        channel.connection_made(transport)
        await started.wait()
        channel.connection_lost(None)
        assert channel.connect_task is not None
        with pytest.raises(asyncio.CancelledError):
            await channel.connect_task
    transport.close.assert_called_once()


@pytest.mark.parametrize("target", ["", "controller", "physical-output", "owned-group"])
def test_volume_read_routes_to_same_physical_output(
    relay: HomePodControlRelay, target: str
) -> None:
    """A controller's volume read cannot leak its synthetic UID to the receiver."""
    message = ProtocolMessage(type=protobuf.GET_VOLUME_MESSAGE, identifier="volume-read")
    protobuf.extract_inner(message).outputDeviceUID = (
        relay.identifier if target == "controller" else target
    )
    result = _channel(relay)._rewrite_client_protobuf(message)
    assert result is message
    assert result.identifier == "volume-read"
    assert protobuf.extract_inner(result).outputDeviceUID == relay.receiver_id
    assert relay.pending_volume is None
    cast("MagicMock", relay.volume_confirmed).assert_not_called()


def test_volume_read_rejects_another_room(relay: HomePodControlRelay) -> None:
    """Volume reads obey the same output ownership restriction as writes."""
    message = ProtocolMessage(type=protobuf.GET_VOLUME_MESSAGE)
    protobuf.extract_inner(message).outputDeviceUID = "another-room"
    channel = _channel(relay)
    with pytest.raises(channel.IgnoreMessage):
        channel._rewrite_client_protobuf(message)


def test_output_descriptor_includes_initial_volume(relay: HomePodControlRelay) -> None:
    """Home needs a volume value on the physical output before enabling its slider."""
    channel = _channel(relay)
    message = ProtocolMessage(type=protobuf.UPDATE_OUTPUT_DEVICE_MESSAGE)
    device = protobuf.extract_inner(message).clusterAwareOutputDevices.add()
    device.uniqueIdentifier = relay.receiver_id
    assert not device.HasField("volume")
    channel._rewrite_remote_protobuf(message)
    assert device.HasField("volume")
    assert device.volume == pytest.approx(0.29)
    assert device.uniqueIdentifier == relay.receiver_id
    relay.observe_volume(_notification(relay.receiver_id, 0.31))
    cast("MagicMock", relay.volume_confirmed).assert_not_called()
    channel._rewrite_remote_protobuf(message)
    assert device.volume == pytest.approx(0.31)


def test_controller_has_distinct_routing_identity(relay: HomePodControlRelay) -> None:
    """Keep controller discovery distinct from the physical receiver's routing graph."""
    message = ProtocolMessage(type=protobuf.DEVICE_INFO_MESSAGE)
    inner = protobuf.extract_inner(message)
    inner.managedConfigDeviceID = "aa:bb:cc:dd:ee:ff"
    inner.routingContextID = "00112233-4455-6677-8899-AABBCCDDEEFF"
    inner.deviceUID = relay.receiver_id
    _channel(relay)._rewrite_remote_protobuf(message)
    assert inner.managedConfigDeviceID == "ab:bb:cc:dd:ee:ff"
    assert inner.routingContextID == "01112233-4455-6677-8899-AABBCCDDEEFF"
    assert inner.deviceUID == relay.identifier
    assert inner.groupUID == relay.group_id


@pytest.mark.parametrize("length_header", ["Content-Length", "content-length"])
async def test_rewritten_response_has_one_correct_body_length(
    relay: HomePodControlRelay, length_header: str
) -> None:
    """Rewriting receiver bodies must not retain their old HTTP framing length."""
    connection = RelayConnection(relay)
    connection._connected_event.set()
    connection.connection = MagicMock(
        send_and_receive=AsyncMock(
            return_value=HttpResponse(
                "RTSP",
                "1.0",
                200,
                "OK",
                {
                    length_header: "3",
                    "CSeq": "42",
                    "Content-Type": "application/x-apple-binary-plist",
                },
                b"old",
            )
        )
    )
    request = HttpRequest("GET", "/info", "RTSP", "1.0", {"CSeq": "42"}, b"")
    response = await connection.send_to_atv(request)
    body = b"rewritten response with a different length"
    encoded = format_response(response._replace(body=body))
    assert encoded.lower().count(b"content-length:") == 1
    parsed, remaining = parse_response(encoded)
    assert parsed is not None
    assert parsed.body == body
    assert parsed.headers["CSeq"] == "42"
    assert parsed.headers["Content-Type"] == "application/x-apple-binary-plist"
    assert not remaining
