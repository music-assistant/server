"""Logic for slimproto clients to discover our (emulated) server on the local network."""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections import OrderedDict

LOGGER = logging.getLogger(__name__)

# pylint:disable=consider-using-f-string


async def start_discovery(
    ip_address: str,
    control_port: int,
    cli_port: int | None,
    cli_port_json: int | None,
    name: str = "Slimproto",
    uuid: str = "slimproto",
) -> asyncio.BaseTransport:
    """Start discovery for players."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(ip_address, control_port, cli_port, cli_port_json, name, uuid),
        local_addr=("0.0.0.0", control_port),
    )
    return transport


class ClientDiscoveryDatagram:
    """Description of a client discovery datagram."""

    device = None
    firmware = None
    client = None

    def __init__(self, data):
        """Initialize class."""
        msg = struct.unpack("!cxBB8x6B", data)
        self.device = msg[1]
        self.firmware = hex(msg[2])
        self.client = ":".join([f"{x:02x}" for x in msg[3:]])

    def __repr__(self):
        """Print the class contents."""
        return "<{} device={!r} firmware={!r} client={!r}>".format(
            self.__class__.__name__,
            self.device,
            self.firmware,
            self.client,
        )


class TLVDiscoveryRequestDatagram:
    """Description of a discovery request datagram."""

    def __init__(self, data: str):
        """Initialize class."""
        requestdata = OrderedDict()
        idx = 0
        length = len(data) - 5
        while idx <= length:
            key, _len = struct.unpack_from("4sB", data.encode(), idx)
            if _len:
                val = data[idx + 5 : idx + 5 + _len]
                idx += 5 + _len
            else:
                val = None
                idx += 5
            key = key.decode()
            requestdata[key] = val
        self.data = requestdata

    def __repr__(self):
        """Pretty print class."""
        return f"<{self.__class__.__name__} data={self.data.items()!r}>"


class DiscoveryProtocol:
    """Description of a discovery protocol."""

    def __init__(
        self,
        ip_address: str,
        control_port: int,
        cli_port: int | None,
        cli_port_json: int | None,
        name: str,
        uuid: str,
    ):
        """Initialze class."""
        self.ip_address = ip_address
        self.control_port = control_port
        self.cli_port = cli_port
        self.cli_port_json = cli_port_json
        self.name = name
        self.uuid = uuid
        self.transport = None

    def connection_made(self, transport):
        """Call on connection."""
        self.transport = transport
        # Allow receiving multicast broadcasts
        sock = self.transport.get_extra_info("socket")
        group = socket.inet_aton("239.255.255.250")
        mreq = struct.pack("4sL", group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    @classmethod
    def error_received(cls, exc):
        """Call on Error."""
        LOGGER.error(exc)

    @classmethod
    def connection_lost(cls, *args, **kwargs):
        """Call on Connection lost."""
        # pylint: disable=unused-argument
        LOGGER.debug("Connection lost to discovery")

    def build_tlv_response(self, requestdata: OrderedDict[str, str]) -> OrderedDict[str, str]:
        """Build TLV Response message."""
        responsedata = OrderedDict()
        for key, value in requestdata.items():
            if key == "NAME":
                responsedata[key] = self.name
            elif key == "IPAD":
                responsedata[key] = self.ip_address
            elif key == "JSON" and self.cli_port_json is not None:
                # send port as a string
                responsedata[key] = str(self.cli_port_json)
            elif key == "CLIP" and self.cli_port is not None:
                # send port as a string
                responsedata[key] = str(self.cli_port)
            elif key == "VERS":
                # send server version
                responsedata[key] = "7.999.999"
            elif key == "UUID":
                # send server uuid
                responsedata[key] = self.uuid
        return responsedata

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle Datagram received callback."""
        # pylint: disable=broad-except
        try:
            # tlv discovery request
            if data.startswith(b"e"):
                # Discovery request and responses contain TLVs of the format:
                # T (4 bytes), L (1 byte unsigned), V (0-255 bytes)
                # To escape from previous discovery format,
                # request are prepended by 'e', responses by 'E'
                # strip leading char of the datagram message
                decoded_data = data.decode("utf-8")[1:]
                dgram = TLVDiscoveryRequestDatagram(decoded_data)
                requestdata = self.build_tlv_response(dgram.data)
                self.send_tlv_discovery_response(requestdata, addr)
            # udp/legacy discovery request
            if data.startswith(b"d"):
                # Discovery request: note that SliMP3 sends deviceid and revision in the discovery
                # request, but the revision is wrong (v 2.2 sends revision 1.1). Oops.
                # also, it does not send the MAC address until the [h]ello packet.
                # Squeezebox sends all fields correctly.
                dgram = ClientDiscoveryDatagram(data)
                self.send_discovery_response(addr)
            # NOTE: ignore all other such as slimp3 - that is simply too old
        except Exception:
            LOGGER.exception(
                "Error occured while trying to parse a datagram from %s - data: %s",
                addr,
                data,
            )

    def send_discovery_response(self, addr: tuple[str, int]) -> None:
        """Send discovery response message."""
        # prefer ip over hostname because its truncated to 16 chars
        hostname = self.ip_address[:16].encode("iso-8859-1")
        hostname += (16 - len(hostname)) * b"\x00"
        dgram = struct.pack("!c16s", b"D", hostname).decode()
        self.transport.sendto(dgram.encode(), addr)

    def send_tlv_discovery_response(
        self, requestdata: OrderedDict[str, str], addr: tuple[str, int]
    ) -> None:
        """Send TLV discovery response message."""
        parts = ["E"]  # new discovery format
        for key, value in requestdata.items():
            if value is None:
                value = ""  # noqa: PLW2901
            elif len(value) > 255:
                # Response too long, truncating to 255 bytes
                value = value[:255]  # noqa: PLW2901
            parts.extend((key, chr(len(value)), value))
        dgram = "".join(parts)
        self.transport.sendto(dgram.encode(), addr)
