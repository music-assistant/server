"""A minimal in-process fake LIFX device listening on localhost UDP."""

from __future__ import annotations

import asyncio
import struct

from lifx_entertainment import protocol
from lifx_entertainment.constants import EXTENDED_ZONES_PER_MESSAGE, MessageType
from lifx_entertainment.models import HSBK
from lifx_entertainment.protocol import Header, decode_header, encode_message


class FakeLifxDevice(asyncio.DatagramProtocol):
    """Answers the subset of the LAN protocol the library uses and records every packet."""

    def __init__(
        self,
        serial: str,
        label: str,
        *,
        group: str = "Living room",
        group_id: bytes = b"\x11" * 16,
        zones: int = 0,
        power: int = 65535,
        color: HSBK | None = None,
    ) -> None:
        """Describe the device; call `start` to listen."""
        self.serial = serial
        self.label = label
        self.group = group
        self.group_id = group_id
        self.zone_count = zones
        self.power = power
        self.color = color or HSBK(1000, 2000, 3000, 3500)
        self.zones = [self.color] * zones
        self.received: list[tuple[Header, bytes]] = []
        self.port = 0
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        """Listen on a free localhost UDP port."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", 0)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def close(self) -> None:
        """Stop listening."""
        if self._transport is not None:
            self._transport.close()

    def of_type(self, msg_type: int) -> list[tuple[Header, bytes]]:
        """Return the received packets of one message type."""
        return [(h, p) for h, p in self.received if h.type == msg_type]

    def datagram_received(self, data: bytes, addr: tuple[str | int, int]) -> None:
        """Record a packet addressed to this device and answer it."""
        header = decode_header(data)
        if not header.tagged and header.serial != self.serial:
            return
        payload = data[36 : header.size]
        self.received.append((header, payload))
        for msg_type, reply in self._handle(header.type, payload):
            self._reply(header, addr, msg_type, reply)
        if header.ack_required:
            self._reply(header, addr, MessageType.ACKNOWLEDGEMENT, b"")

    def _reply(
        self, header: Header, addr: tuple[str | int, int], msg_type: int, payload: bytes
    ) -> None:
        assert self._transport is not None
        packet = encode_message(
            msg_type,
            payload,
            source=header.source,
            sequence=header.sequence,
            target=bytes.fromhex(self.serial) + b"\x00\x00",
        )
        self._transport.sendto(packet, (str(addr[0]), int(addr[1])))

    def _handle(self, msg_type: int, payload: bytes) -> list[tuple[int, bytes]]:
        label = self.label.encode().ljust(32, b"\x00")
        if msg_type == MessageType.GET_SERVICE:
            return [(MessageType.STATE_SERVICE, struct.pack("<BI", 1, self.port))]
        if msg_type == MessageType.GET_LABEL:
            return [(MessageType.STATE_LABEL, label)]
        if msg_type == MessageType.GET_GROUP:
            group = self.group.encode().ljust(32, b"\x00")
            return [(MessageType.STATE_GROUP, self.group_id + group + struct.pack("<Q", 0))]
        if msg_type == MessageType.GET_VERSION:
            product = 32 if self.zone_count else 27
            return [(MessageType.STATE_VERSION, struct.pack("<III", 1, product, 0))]
        if msg_type == MessageType.LIGHT_GET:
            body = protocol.encode_hsbk(self.color) + struct.pack("<hH", 0, self.power)
            return [(MessageType.LIGHT_STATE, body + label + struct.pack("<Q", 0))]
        if msg_type == MessageType.LIGHT_SET_POWER:
            self.power = struct.unpack_from("<H", payload)[0]
            return []
        if msg_type == MessageType.LIGHT_SET_COLOR:
            self.color = protocol.decode_hsbk(payload, 1)
            self.zones = [self.color] * self.zone_count
            return []
        if msg_type == MessageType.GET_EXTENDED_COLOR_ZONES:
            if not self.zone_count:
                return [(MessageType.STATE_UNHANDLED, struct.pack("<H", msg_type))]
            colors = self.zones + [HSBK()] * (EXTENDED_ZONES_PER_MESSAGE - self.zone_count)
            body = struct.pack("<HHB", self.zone_count, 0, self.zone_count)
            return [
                (
                    MessageType.STATE_EXTENDED_COLOR_ZONES,
                    body + b"".join(protocol.encode_hsbk(c) for c in colors),
                )
            ]
        if msg_type == MessageType.SET_EXTENDED_COLOR_ZONES:
            _duration, _apply, index, count = struct.unpack_from("<IBHB", payload)
            for i in range(count):
                self.zones[index + i] = protocol.decode_hsbk(payload, 8 + i * 8)
            return []
        if msg_type == MessageType.GET_COLOR_ZONES and not self.zone_count:
            return [(MessageType.STATE_UNHANDLED, struct.pack("<H", msg_type))]
        return []
