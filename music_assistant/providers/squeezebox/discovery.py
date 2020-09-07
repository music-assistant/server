"""Squeezebox emulation discovery implementation."""

from collections import OrderedDict
import socket
import logging
import struct

from music_assistant.utils import (
    get_hostname,
    get_ip
)

LOGGER = logging.getLogger("squeezebox")

class Datagram():
    """Description of a discovery datagram."""
    @classmethod
    def decode(self, data):
        if data[0] == "e":
            return TLVDiscoveryRequestDatagram(data)
        elif data[0] == "E":
            return TLVDiscoveryResponseDatagram(data)
        elif data[0] == "d":
            return ClientDiscoveryDatagram(data)
        elif data[0] == "h":
            pass  # Hello!
        elif data[0] == "i":
            pass  # IR
        elif data[0] == "2":
            pass  # i2c?
        elif data[0] == "a":
            pass  # ack!


class ClientDiscoveryDatagram(Datagram):
    """Description of a client discovery datagram."""

    device = None
    firmware = None
    client = None

    def __init__(self, data):
        s = struct.unpack("!cxBB8x6B", data.encode())
        assert s[0] == "d"
        self.device = s[1]
        self.firmware = hex(s[2])
        self.client = ":".join(["%02x" % (x,) for x in s[3:]])

    def __repr__(self):
        return "<%s device=%r firmware=%r client=%r>" % (
            self.__class__.__name__,
            self.device,
            self.firmware,
            self.client,
        )


class DiscoveryResponseDatagram(Datagram):
    """Description of a discovery response datagram."""
    def __init__(self, hostname, port):
        hostname = hostname[:16].encode("UTF-8")
        hostname += (16 - len(hostname)) * "\x00"
        self.packet = struct.pack("!c16s", "D", hostname).decode()


class TLVDiscoveryRequestDatagram(Datagram):
    """Description of a discovery request datagram."""
    def __init__(self, data):
        requestdata = OrderedDict()
        assert data[0] == "e"
        idx = 1
        length = len(data) - 5
        while idx <= length:
            typ, l = struct.unpack_from("4sB", data.encode(), idx)
            if l:
                val = data[idx + 5 : idx + 5 + l]
                idx += 5 + l
            else:
                val = None
                idx += 5
            typ = typ.decode()
            requestdata[typ] = val
        self.data = requestdata

    def __repr__(self):
        return "<%s data=%r>" % (self.__class__.__name__, self.data.items())


class TLVDiscoveryResponseDatagram(Datagram):
    """Description of a TLV discovery response datagram."""
    def __init__(self, responsedata):
        parts = ["E"]  # new discovery format
        for typ, value in responsedata.items():
            if value is None:
                value = ""
            elif len(value) > 255:
                # Response too long, truncating to 255 bytes
                value = value[:255]
            parts.extend((typ, chr(len(value)), value))
        self.packet = "".join(parts)


class DiscoveryProtocol:
    """Description of a discovery protocol."""
    def __init__(self, web_port):
        self.web_port = web_port

    def connection_made(self, transport):
        self.transport = transport
        # Allow receiving multicast broadcasts
        sock = self.transport.get_extra_info("socket")
        group = socket.inet_aton("239.255.255.250")
        mreq = struct.pack("4sL", group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    def error_received(self, exc):
        LOGGER.error(exc)

    def connection_lost(self, *args, **kwargs):
        LOGGER.debug("Connection lost to discovery")

    def build_TLV_response(self, requestdata):
        responsedata = OrderedDict()
        for typ, value in requestdata.items():
            if typ == "NAME":
                # send full host name - no truncation
                value = get_hostname()
            elif typ == "IPAD":
                # send ipaddress as a string only if it is set
                value = get_ip()
                # :todo: IPv6
                if value == "0.0.0.0":
                    # do not send back an ip address
                    typ = None
            elif typ == "JSON":
                # send port as a string
                json_port = self.web_port
                value = str(json_port)
            elif typ == "VERS":
                # send server version
                value = "7.9"
            elif typ == "UUID":
                # send server uuid
                value = "musicassistant"
            else:
                LOGGER.debug("Unexpected information request: %r", typ)
                typ = None
            if typ:
                responsedata[typ] = value
        return responsedata

    def datagram_received(self, data, addr):
        try:
            data = data.decode()
            dgram = Datagram.decode(data)
            if isinstance(dgram, ClientDiscoveryDatagram):
                self.sendDiscoveryResponse(addr)
            elif isinstance(dgram, TLVDiscoveryRequestDatagram):
                resonsedata = self.build_TLV_response(dgram.data)
                self.sendTLVDiscoveryResponse(resonsedata, addr)
        except Exception as exc:
            LOGGER.exception(exc)

    def sendDiscoveryResponse(self, addr):
        dgram = DiscoveryResponseDatagram(get_hostname(), 3483)
        self.transport.sendto(dgram.packet.encode(), addr)

    def sendTLVDiscoveryResponse(self, resonsedata, addr):
        dgram = TLVDiscoveryResponseDatagram(resonsedata)
        self.transport.sendto(dgram.packet.encode(), addr)
