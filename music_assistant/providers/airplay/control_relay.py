"""
Session-scoped HomePod control relay using pyatv's AirPlay transport.

Keep the receiver's media/artwork messages intact. Only discovery, endpoint
identity and volume routing belong to the sender. In particular, SET_VOLUME
must reach the receiver and its real reply must reach iOS; synthesizing an ACK
in the sender caused multi-second stalls on HomePod OS 27.2.

The transport adapter uses internals of the pinned pyatv version. It deliberately
does not change pyatv globals or use its diagnostic server identity and keys.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from io import BytesIO
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pyatv.auth.hap_channel import AbstractHAPChannel
from pyatv.const import Protocol
from pyatv.core import MutableService
from pyatv.protocols.airplay.server_auth import BaseAirPlayServerAuth, generate_keys
from pyatv.protocols.airplay.utils import decode_plist_body, encode_plist_body
from pyatv.protocols.mrp import protobuf
from pyatv.protocols.mrp.protobuf.VolumeControlAvailabilityMessage_pb2 import VolumeCapabilities
from pyatv.scripts.atvproxy import (
    AirPlayAppleTVProxy,
    AirPlayChannelAppleTVProxy,
    AirPlayDataStreamChannelAppleTVProxy,
    AirPlayEventChannelAppleTVProxy,
)
from pyatv.support import shift_hex_identifier
from pyatv.support.dns import format_txt_dict, parse_txt_dict
from pyatv.support.http import BasicHttpServer, HttpRequest, HttpResponse
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

if TYPE_CHECKING:
    from pyatv.auth.hap_session import HAPSession

LOGGER = logging.getLogger(__name__)
SERVICE_TYPE = "_airplay._tcp.local."
RELAY_MARKER = "maControlRelay"
MAX_CLIENTS = 8


class HomePodControlRelay:
    """Advertise a controller for one active MA HomePod stream."""

    def __init__(
        self,
        *,
        name: str,
        address: str,
        port: int,
        properties: Mapping[str, str],
        publish_ip: str,
        zeroconf: AsyncZeroconf,
        volume_confirmed: Callable[[int], None],
        volume_level: int,
    ) -> None:
        """Initialize the relay without opening sockets or publishing discovery."""
        self.name = name
        self.address = address
        self.port = port
        self.properties = dict(properties)
        self.group_id = properties["gid"]
        self.receiver_id = properties["psi"]
        self.publish_ip = publish_ip
        self.zeroconf = zeroconf
        self.volume_confirmed = volume_confirmed
        self.volume = max(0, min(100, volume_level)) / 100
        self.identifier = str(uuid4()).upper()
        self.mac = ":".join(f"{value:02X}" for value in b"\x02" + secrets.token_bytes(5))
        self.keys = generate_keys(secrets.token_bytes(32))  # type: ignore[no-untyped-call]
        self.pin = secrets.randbelow(9000) + 1000
        self.clients: set[RelayConnection] = set()
        self.server: asyncio.Server | None = None
        self.service: AsyncServiceInfo | None = None
        self.closed = False
        self.pending_volume: tuple[float, float] | None = None

    async def start(self) -> None:
        """Open the control listener and advertise it after it is listening."""
        if self.closed or self.server is not None:
            raise RuntimeError("Relay already started or closed")
        loop = asyncio.get_running_loop()
        try:
            self.server = await loop.create_server(
                lambda: RelayConnection(self), self.publish_ip, 0
            )
            port = self.server.sockets[0].getsockname()[1]
            self.service = AsyncServiceInfo(
                SERVICE_TYPE,
                f"{self.name}.{SERVICE_TYPE}",
                addresses=[ip_address(self.publish_ip).packed],
                port=port,
                properties=self.discovery_properties(self.properties),
                server=f"ma-control-{self.identifier.lower()}.local.",
            )
            await self.zeroconf.async_register_service(self.service)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Withdraw discovery and close every connection owned by this stream."""
        self.closed = True
        self.pending_volume = None
        # Closing sockets must not depend on mDNS still being available during shutdown.
        server, self.server = self.server, None
        if server is not None:
            server.close()
        for client in tuple(self.clients):
            client.close()
        self.clients.clear()
        if server is not None:
            await server.wait_closed()
        if self.service is not None:
            service, self.service = self.service, None
            try:
                await self.zeroconf.async_unregister_service(service)
            except Exception as err:
                LOGGER.debug("Cannot withdraw HomePod control relay: %s", type(err).__name__)

    def discovery_properties(self, source: Mapping[str, str]) -> dict[str, str]:
        """Return controller discovery without changing the receiver's advertisement."""
        result = dict(source)
        result.update(
            deviceid=self.mac,
            pi=self.identifier,
            psi=self.identifier,
            pk=self.keys.auth_pub.hex(),
            gid=self.group_id,
            igl="1",
            gcgl="1",
        )
        result[RELAY_MARKER] = "1"
        result["flags"] = hex(int(result.get("flags", "0"), 16) | (1 << 11))
        if "btaddr" in result:
            result["btaddr"] = self.mac
        return result

    def accept_volume(self, message: Any) -> bool:
        """Route only this output's finite, bounded volume commands."""
        inner = protobuf.extract_inner(message)
        if self.closed or inner.outputDeviceUID not in (
            "",
            self.identifier,
            self.receiver_id,
            self.group_id,
        ):
            return False
        if not inner.HasField("volume") or not math.isfinite(inner.volume):
            return False
        if not 0 <= inner.volume <= 1:
            return False
        inner.outputDeviceUID = self.receiver_id
        self.pending_volume = inner.volume, time.monotonic()
        return True

    def observe_volume(self, message: Any) -> None:
        """Adopt a receiver confirmation only for a command owned by this relay."""
        inner = protobuf.extract_inner(message)
        if (
            self.closed
            or inner.outputDeviceUID != self.receiver_id
            or not math.isfinite(inner.volume)
            or not 0 <= inner.volume <= 1
        ):
            return
        self.volume = inner.volume
        pending = self.pending_volume
        if pending is None:
            return
        if time.monotonic() - pending[1] > 3:
            self.pending_volume = None
            return
        if not math.isfinite(inner.volume) or abs(inner.volume - pending[0]) > 0.0001:
            return
        self.pending_volume = None
        self.volume_confirmed(max(0, min(100, round(inner.volume * 100))))


class RelayChannel(AirPlayChannelAppleTVProxy):
    """Keep channel connection attempts within the owning control session."""

    connect_task: asyncio.Task[None] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connect the encrypted channel with a cancellable, bounded task."""
        AbstractHAPChannel.connection_made(self, transport)
        self.connect_task = self.loop.create_task(self._connect_channel())

    def connection_lost(self, exc: Exception | None) -> None:
        """Cancel pending upstream work when the controller disconnects."""
        self.close()

    def close(self) -> None:
        """Release both transports and any pending upstream connection."""
        if self.connect_task is not None and self.connect_task is not asyncio.current_task():
            self.connect_task.cancel()
        super().close()

    async def _connect_channel(self) -> None:
        try:
            async with asyncio.timeout(10):
                await self._connect_to_remote()  # type: ignore[no-untyped-call]
            self.handle_received()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.debug("HomePod control channel failed: %s", type(err).__name__)
            self.close()


class RelayEventChannel(RelayChannel, AirPlayEventChannelAppleTVProxy):
    """Relay event updates with session-owned connection cleanup."""


class RelayDataChannel(RelayChannel, AirPlayDataStreamChannelAppleTVProxy):
    """Forward media unchanged and map volume to the physical HomePod output."""

    def __init__(self, owner: HomePodControlRelay, *args: Any) -> None:
        """Initialize a transport channel owned by one stream."""
        self.owner = owner
        super().__init__(*args)

    def process_buffer(self, data: bytes) -> tuple[bytes, bytes]:
        """Drain ignored messages so subsequent frames do not wait for more TCP data."""
        while data:
            forwarded, rest = super().process_buffer(data)
            if forwarded or len(rest) == len(data):
                return forwarded, rest
            data = rest
        return b"", b""

    def _rewrite_client_protobuf(self, message: Any) -> Any:
        if message.type == protobuf.SET_VOLUME_MESSAGE:
            if not self.owner.accept_volume(message):
                raise self.IgnoreMessage
            return message
        if message.type == protobuf.GET_VOLUME_MESSAGE:
            inner = protobuf.extract_inner(message)
            if inner.outputDeviceUID not in (
                "",
                self.owner.identifier,
                self.owner.receiver_id,
                self.owner.group_id,
            ):
                raise self.IgnoreMessage
            inner.outputDeviceUID = self.owner.receiver_id
            return message
        if message.type == protobuf.CONFIGURE_CONNECTION_MESSAGE:
            # The receiver owns its group. Applying the controller's group
            # configuration back to it invalidates the existing control tunnel.
            if protobuf.extract_inner(message).groupID:
                raise self.IgnoreMessage
        if message.type == protobuf.DEVICE_INFO_MESSAGE:
            return super()._rewrite_client_protobuf(message)
        return None

    def _rewrite_remote_protobuf(self, message: Any) -> Any:
        if message.type not in {
            protobuf.DEVICE_INFO_MESSAGE,
            protobuf.DEVICE_INFO_UPDATE_MESSAGE,
            protobuf.UPDATE_OUTPUT_DEVICE_MESSAGE,
            protobuf.VOLUME_DID_CHANGE_MESSAGE,
            protobuf.VOLUME_CONTROL_AVAILABILITY_MESSAGE,
            protobuf.VOLUME_CONTROL_CAPABILITIES_DID_CHANGE_MESSAGE,
        }:
            return None
        owner = self.owner
        inner = protobuf.extract_inner(message)
        if message.type in (protobuf.DEVICE_INFO_MESSAGE, protobuf.DEVICE_INFO_UPDATE_MESSAGE):
            inner.uniqueIdentifier = owner.identifier
            inner.deviceUID = owner.identifier
            inner.name = owner.name
            # Keep the virtual controller separate from the physical receiver's
            # routing context, as in pyatv's identity rewrite.
            if inner.managedConfigDeviceID:
                inner.managedConfigDeviceID = shift_hex_identifier(inner.managedConfigDeviceID)
            if inner.routingContextID:
                inner.routingContextID = shift_hex_identifier(inner.routingContextID)
            inner.groupUID = owner.group_id
            inner.senderDefaultGroupUID = owner.group_id
            inner.airPlayGroupID = owner.group_id
            inner.isGroupLeader = True
            inner.groupContainsDiscoverableGroupLeader = 1
            inner.parentGroupContainsDiscoverableGroupLeader = 1
            return message
        if message.type == protobuf.UPDATE_OUTPUT_DEVICE_MESSAGE:
            inner.endpointUID = owner.group_id
            for device in list(inner.outputDevices) + list(inner.clusterAwareOutputDevices):
                if device.uniqueIdentifier != owner.receiver_id:
                    continue
                device.groupID = owner.group_id
                device.parentGroupIdentifier = owner.group_id
                device.airPlayGroupID = owner.group_id
                device.isGroupLeader = False
                device.groupContainsGroupLeader = True
                device.parentGroupContainsDiscoverableLeader = True
                device.isVolumeControlAvailable = True
                device.volumeCapabilities = VolumeCapabilities.Absolute
                device.volume = owner.volume
            return message
        if message.type == protobuf.VOLUME_DID_CHANGE_MESSAGE:
            owner.observe_volume(message)
            if inner.outputDeviceUID == owner.receiver_id:
                inner.outputDeviceUID = owner.identifier
                inner.endpointUID = owner.group_id
            return message
        if message.type == protobuf.VOLUME_CONTROL_AVAILABILITY_MESSAGE:
            inner.volumeControlAvailable = True
            inner.volumeCapabilities = VolumeCapabilities.Absolute
            return message
        if message.type == protobuf.VOLUME_CONTROL_CAPABILITIES_DID_CHANGE_MESSAGE:
            if inner.outputDeviceUID == owner.receiver_id:
                inner.outputDeviceUID = owner.identifier
                inner.endpointUID = owner.group_id
                inner.capabilities.volumeControlAvailable = True
                inner.capabilities.volumeCapabilities = VolumeCapabilities.Absolute
            return message
        return None


class RelayConnection(AirPlayAppleTVProxy):
    """Use generated server credentials and own all per-client resources."""

    def __init__(self, owner: HomePodControlRelay) -> None:
        """Initialize pyatv's transport with this relay's identity and keys."""
        self.owner = owner
        self.transport: asyncio.Transport | None = None  # type: ignore[assignment]
        self.keys = owner.keys
        BaseAirPlayServerAuth.__init__(self, owner.name, owner.identifier, owner.pin)
        BasicHttpServer.__init__(self, self)
        self.loop = asyncio.get_running_loop()
        self.address = owner.address
        self.port = owner.port
        self.target_identifier = owner.receiver_id
        self.target_group = owner.group_id
        self.credentials = None
        self.client_hap_session: HAPSession | None = None
        self.client_encryption = False
        self.service = MutableService(None, Protocol.AirPlay, owner.port, owner.properties)
        self.connection = None
        self.verifier = None
        self._channel_servers = {}
        self._connected_event = asyncio.Event()
        self.channels: list[Any] = []
        self.start_task: asyncio.Task[None] | None = None
        self.request_tasks: set[asyncio.Task[Any]] = set()
        self.add_route("GET", "/info", self.handle_info)
        self.add_route("SETUP", ".*", self.handle_setup)
        self.add_route("TEARDOWN", ".*", self.handle_teardown)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connect an admitted controller to its existing HomePod session."""
        self.transport = cast("asyncio.Transport", transport)
        if self.owner.closed or len(self.owner.clients) >= MAX_CLIENTS:
            transport.close()
            return
        self.owner.clients.add(self)
        self.start_task = self.loop.create_task(self._connect())

    def connection_lost(self, exc: Exception | None) -> None:
        """Release the upstream session when the controller leaves."""
        self.close()

    def close(self) -> None:
        """Close listeners, channels and the upstream control connection."""
        self.owner.clients.discard(self)
        if self.start_task is not None and self.start_task is not asyncio.current_task():
            self.start_task.cancel()
        for task in self.request_tasks:
            task.cancel()
        self.request_tasks.clear()
        for server in self._channel_servers.values():
            server.close()
        self._channel_servers.clear()
        for channel in self.channels:
            channel.close()
        self.channels.clear()
        if self.connection is not None:
            self.connection.close()
        if self.transport is not None:
            self.transport.close()

    def handle_request(self, request: HttpRequest) -> Any:
        """Turn failed or cancelled upstream requests into bounded HTTP responses."""
        result = super().handle_request(request)
        if not isinstance(result, asyncio.Task):
            return result
        self.request_tasks.add(result)
        result.add_done_callback(self.request_tasks.discard)
        return self.loop.create_task(self._finish_request(result, request))

    async def send_to_atv(self, request: HttpRequest) -> HttpResponse:
        """Forward control requests and preserve the receiver's response headers."""
        async with asyncio.timeout(10):
            await self._connected_event.wait()
        if self.connection is None or self.owner.closed:
            raise ConnectionError("HomePod control relay is closed")
        response = await self.connection.send_and_receive(
            method=request.method,
            uri=request.path,
            protocol=f"{request.protocol}/{request.version}",
            headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
            body=request.body,
        )
        # pyatv's response formatter adds the final body length. Keeping the
        # receiver's length would duplicate it, and rewrites can change the body.
        return response._replace(
            headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        )

    async def _connect(self) -> None:
        try:
            async with asyncio.timeout(10):
                await self.start()
        except Exception as err:
            LOGGER.debug("HomePod control relay connection failed: %s", type(err).__name__)
            self.close()

    async def _finish_request(self, task: asyncio.Task[Any], request: HttpRequest) -> Any:
        try:
            return await task
        except asyncio.CancelledError:
            return None
        except Exception as err:
            LOGGER.debug("HomePod control request failed: %s", type(err).__name__)
            return HttpResponse(
                request.protocol,
                request.version,
                503,
                "Control unavailable",
                {"CSeq": request.headers.get("CSeq", "1")},
                b"",
            )

    def _rewrite_info(self, info: Mapping[str, Any]) -> Mapping[str, Any]:
        result = dict(info)
        owner = self.owner
        result.update(
            psi=owner.identifier, name=owner.name, pi=owner.identifier, pk=owner.keys.auth_pub
        )
        for key in ("deviceID", "macAddress"):
            if key in result:
                result[key] = owner.mac
        result["statusFlags"] = result.get("statusFlags", 0) | (1 << 11)
        if "senderAddress" in result:
            result["senderAddress"] = f"{self.client_ip}:{self.client_port}"
        if "txtAirPlay" in result:
            raw = result["txtAirPlay"]
            props = {k: v.decode() for k, v in parse_txt_dict(BytesIO(raw), len(raw)).items()}
            result["txtAirPlay"] = format_txt_dict(owner.discovery_properties(props))
        return result

    async def _handle_setup_event_channel(
        self, request: HttpRequest, request_data: dict[str, Any]
    ) -> HttpResponse:
        response = cast(
            "HttpResponse", await super()._handle_setup_event_channel(request, request_data)
        )
        data = decode_plist_body(response.body) or {}
        if isinstance(data.get("info"), dict):
            data["info"] = self._rewrite_info(data["info"])
            response = response._replace(body=encode_plist_body(data))
        return response

    async def _handle_setup_data_stream_channel(
        self, request: HttpRequest, request_data: dict[str, Any]
    ) -> HttpResponse:
        if any(stream.get("type") != 130 for stream in request_data["streams"]):
            return HttpResponse(
                request.protocol,
                request.version,
                400,
                "Control streams only",
                {"CSeq": request.headers.get("CSeq", "1")},
                b"",
            )
        return cast(
            "HttpResponse", await super()._handle_setup_data_stream_channel(request, request_data)
        )

    def _create_event_channel(self, port: int, rewrite_info: Any) -> Any:
        assert self.connection is not None
        assert self.verifier is not None
        assert self.shared_key is not None
        channel = RelayEventChannel(
            self.loop,
            self.verifier,
            self.connection.remote_ip,
            port,
            self.shared_key,
            rewrite_info,
        )
        self.channels.append(channel)
        return channel

    def _create_data_stream_channel(self, port: int, stream_id: int, seed: int) -> RelayDataChannel:
        assert self.connection is not None
        assert self.verifier is not None
        assert self.shared_key is not None
        channel = RelayDataChannel(
            self.owner,
            stream_id,
            self.loop,
            self.verifier,
            self.connection.remote_ip,
            port,
            self.shared_key,
            seed,
            self.target_identifier,
            self.target_group,
        )
        self.channels.append(channel)
        return channel
