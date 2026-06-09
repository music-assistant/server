"""
DTLS 1.2 PSK streaming client for the Hue Entertainment API.

Pure-Python implementation of the minimal DTLS 1.2 handshake needed for
the Hue Entertainment API. Only supports TLS_PSK_WITH_AES_128_GCM_SHA256.

Uses the `cryptography` library (already a MA dependency) for AES-GCM
and HMAC-SHA256. No ctypes, no C bindings, no external dependencies.

All socket operations run on a dedicated sender thread so the asyncio
event loop is never blocked.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import queue
import socket
import struct
import threading
import time
import uuid
from contextlib import suppress

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .constants import (
    COLOR_SPACE_RGB,
    HUE_ENTERTAINMENT_PORT,
    HUESTREAM_HEADER,
    HUESTREAM_VERSION,
    KEEPALIVE_INTERVAL_S,
)
from .models import LightColorCommand

LOGGER = logging.getLogger(__name__)

_STOP = object()
HANDSHAKE_TIMEOUT = 5.0

# DTLS constants
_DTLS_VERSION = b"\xfe\xfd"  # DTLS 1.2
_CT_HANDSHAKE = 0x16
_CT_CHANGE_CIPHER_SPEC = 0x14
_CT_APPLICATION_DATA = 0x17

_HT_CLIENT_HELLO = 0x01
_HT_SERVER_HELLO = 0x02
_HT_HELLO_VERIFY_REQUEST = 0x03
_HT_SERVER_HELLO_DONE = 0x0E
_HT_CLIENT_KEY_EXCHANGE = 0x10
_HT_FINISHED = 0x14

# TLS_PSK_WITH_AES_128_GCM_SHA256
_CIPHER_SUITE = b"\x00\xa8"


class HueDtlsStreamer:
    """
    Streams light color commands to a Hue bridge over DTLS.

    Pure-Python DTLS 1.2 PSK implementation. All blocking socket I/O
    is confined to a dedicated sender thread.
    """

    def __init__(self) -> None:
        """Initialize the DTLS streamer."""
        self._area_uuid_bytes: bytes = b""
        self._sequence: int = 0
        self._connected = False
        self._send_queue: queue.Queue[bytes | object] = queue.Queue(maxsize=64)
        self._sender_thread: threading.Thread | None = None
        self._last_message: bytes | None = None

    @property
    def is_connected(self) -> bool:
        """Return True if the DTLS connection is active."""
        return self._connected

    def connect(self, host: str, username: str, clientkey: str, area_id: str) -> None:
        """
        Establish a DTLS connection and start the sender thread.

        Must be called from a thread executor (blocking).
        """
        self._sequence = 0
        self._last_message = None
        # Try all 3 UUID formats — log which one we're using for debugging
        # Format: 36-byte ASCII with dashes (Q42 reference)
        self._area_uuid_bytes = str(uuid.UUID(area_id)).encode("ascii")
        LOGGER.debug("Area UUID bytes (%d): %s", len(self._area_uuid_bytes), self._area_uuid_bytes)

        while not self._send_queue.empty():
            with suppress(queue.Empty):
                self._send_queue.get_nowait()

        # Perform the DTLS handshake (blocking)
        psk = bytes.fromhex(clientkey)
        dtls_conn = _DtlsConnection(host, HUE_ENTERTAINMENT_PORT, username, psk)
        dtls_conn.handshake()

        self._connected = True
        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            args=(dtls_conn,),
            name="hue-dtls-sender",
            daemon=True,
        )
        self._sender_thread.start()
        LOGGER.info("DTLS connected to Hue bridge at %s:%d", host, HUE_ENTERTAINMENT_PORT)

    def disconnect(self) -> None:
        """Close the DTLS connection and stop the sender thread."""
        self._connected = False
        self._last_message = None
        with suppress(queue.Full):
            self._send_queue.put_nowait(_STOP)
        if self._sender_thread is not None:
            self._sender_thread.join(timeout=5.0)
            self._sender_thread = None

    def send_colors(self, commands: list[LightColorCommand]) -> None:
        """Queue light color commands for sending (non-blocking, event-loop safe)."""
        if not self._connected:
            return
        if not commands:
            return
        message = self._build_huestream_message(commands)
        # Frame-size invariant: 16-byte header + 36-byte UUID + 7 bytes per channel.
        expected = 16 + 36 + 7 * len(commands)
        if len(message) != expected:
            LOGGER.warning(
                "HueStream frame size mismatch: got %d expected %d (commands=%d)",
                len(message),
                expected,
                len(commands),
            )
        self._last_message = message
        try:
            self._send_queue.put_nowait(message)
        except queue.Full:
            with suppress(queue.Empty):
                self._send_queue.get_nowait()
            with suppress(queue.Full):
                self._send_queue.put_nowait(message)

    def _sender_loop(self, dtls_conn: _DtlsConnection) -> None:
        """Dedicated thread: dequeue messages and send over the DTLS connection."""
        try:
            while self._connected:
                try:
                    item = self._send_queue.get(timeout=KEEPALIVE_INTERVAL_S)
                except queue.Empty:
                    # Keepalive: resend last frame to prevent entertainment timeout
                    if self._connected and self._last_message is not None:
                        try:
                            dtls_conn.send(self._last_message)
                        except Exception:
                            LOGGER.warning("DTLS keepalive failed")
                            self._connected = False
                            break
                    continue

                if item is _STOP:
                    break

                if isinstance(item, bytes):
                    try:
                        dtls_conn.send(item)
                    except Exception as err:
                        LOGGER.warning("DTLS send failed: %s", err)
                        self._connected = False
                        break
        finally:
            with suppress(Exception):
                dtls_conn.close()
            LOGGER.debug("DTLS sender thread stopped")

    def _build_huestream_message(self, commands: list[LightColorCommand]) -> bytes:
        """Build a HueStream v2 message for the Hue V2 bridge.

        Format: 16-byte header + 36-byte ASCII UUID + 7 bytes per channel.
        Per-channel: channel_id(1) + R(2) + G(2) + B(2)
        """
        header = bytearray()
        header.extend(HUESTREAM_HEADER)  # 9 bytes protocol name
        header.extend(HUESTREAM_VERSION)  # version 2.0
        header.append(self._sequence & 0xFF)  # sequence id
        header.extend(b"\x00\x00")  # reserved
        header.append(COLOR_SPACE_RGB)  # color space (0x00 = RGB)
        header.append(0x00)  # reserved
        self._sequence = (self._sequence + 1) & 0xFF

        # Entertainment area ID as 36-byte ASCII UUID string (with dashes)
        header.extend(self._area_uuid_bytes)

        for cmd in commands:
            # Per-channel: channel_id(1) + R(1,1) + G(1,1) + B(1,1) = 7 bytes
            # Color bytes are 0-255, each duplicated (Q42.HueApi convention)
            r = min(255, cmd.red >> 8) if cmd.red > 255 else cmd.red
            g = min(255, cmd.green >> 8) if cmd.green > 255 else cmd.green
            b = min(255, cmd.blue >> 8) if cmd.blue > 255 else cmd.blue
            header.extend(bytes([cmd.channel_id & 0xFF, r, r, g, g, b, b]))

        return bytes(header)


# ---------------------------------------------------------------------------
# Pure-Python DTLS 1.2 PSK implementation
# ---------------------------------------------------------------------------


class _DtlsConnection:
    """Minimal DTLS 1.2 PSK connection over UDP."""

    def __init__(self, host: str, port: int, identity: str, psk: bytes) -> None:
        self._host = host
        self._port = port
        self._identity = identity.encode("utf-8")
        self._psk = psk
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(HANDSHAKE_TIMEOUT)
        self._sock.connect((host, port))

        self._client_random = b""
        self._server_random = b""
        self._epoch = 0
        self._send_seq = 0
        self._client_write_key: bytes = b""
        self._client_write_iv: bytes = b""
        self._aesgcm: AESGCM | None = None
        self._handshake_messages: bytearray = bytearray()
        self._msg_seq = 0

    def handshake(self) -> None:
        """Perform the DTLS 1.2 PSK handshake."""
        # Flight 1: ClientHello (no cookie)
        self._client_random = _make_random()
        hello1 = self._build_client_hello(cookie=b"")
        self._send_handshake(hello1, msg_type=_HT_CLIENT_HELLO)

        # Flight 2: HelloVerifyRequest
        data = self._recv()
        cookie = self._parse_hello_verify_request(data)

        # Flight 3: ClientHello (with cookie)
        self._msg_seq = 0
        self._send_seq = 0
        self._handshake_messages = bytearray()
        hello2 = self._build_client_hello(cookie=cookie)
        self._send_handshake(hello2, msg_type=_HT_CLIENT_HELLO)

        # Flight 4: ServerHello + ServerHelloDone
        data = self._recv()
        self._parse_server_flight(data)

        # Derive keys from PSK
        self._derive_keys()

        # Flight 5: ClientKeyExchange + ChangeCipherSpec + Finished
        cke = self._build_client_key_exchange()
        self._send_handshake(cke, msg_type=_HT_CLIENT_KEY_EXCHANGE)

        # ChangeCipherSpec
        self._send_record(_CT_CHANGE_CIPHER_SPEC, b"\x01")
        self._epoch = 1
        self._send_seq = 0

        # Finished (encrypted)
        finished = self._build_finished()
        self._send_encrypted_handshake(finished)

        # Flight 6: Server ChangeCipherSpec + Finished
        # Read and discard — we just need to confirm the server accepted
        try:
            self._sock.settimeout(3.0)
            self._recv()  # ChangeCipherSpec
            self._recv()  # Finished (encrypted, we don't verify)
        except TimeoutError:
            pass  # Some bridges don't send server Finished promptly
        self._sock.settimeout(5.0)

    def send(self, plaintext: bytes) -> None:
        """Send encrypted application data."""
        self._send_encrypted(_CT_APPLICATION_DATA, plaintext)

    def close(self) -> None:
        """Close the UDP socket."""
        with suppress(Exception):
            self._sock.close()

    # -- Record layer --

    def _send_record(self, content_type: int, fragment: bytes) -> None:
        """Send a DTLS plaintext record."""
        header = struct.pack(
            "!BHH6sH",
            content_type,
            0xFEFD,  # DTLS 1.2
            self._epoch,
            self._send_seq.to_bytes(6, "big"),
            len(fragment),
        )
        self._sock.send(header + fragment)
        self._send_seq += 1

    def _send_encrypted(self, content_type: int, plaintext: bytes) -> None:
        """Send an encrypted DTLS record using AES-128-GCM."""
        assert self._aesgcm is not None
        # Explicit nonce = epoch(2) + seq(6) = 8 bytes
        explicit_nonce = struct.pack("!H", self._epoch) + self._send_seq.to_bytes(6, "big")
        # Full nonce = implicit_iv(4) + explicit_nonce(8) = 12 bytes
        nonce = self._client_write_iv + explicit_nonce

        # AAD: seq_num(8) + content_type(1) + version(2) + length(2)
        aad = explicit_nonce + struct.pack("!BHH", content_type, 0xFEFD, len(plaintext))

        ciphertext_and_tag = self._aesgcm.encrypt(nonce, plaintext, aad)

        # Record fragment: explicit_nonce(8) + ciphertext + tag(16)
        fragment = explicit_nonce + ciphertext_and_tag
        header = struct.pack(
            "!BHH6sH",
            content_type,
            0xFEFD,
            self._epoch,
            self._send_seq.to_bytes(6, "big"),
            len(fragment),
        )
        self._sock.send(header + fragment)
        self._send_seq += 1

    def _recv(self) -> bytes:
        """Receive a UDP datagram from the bridge."""
        return self._sock.recv(4096)

    # -- Handshake messages --

    def _build_client_hello(self, cookie: bytes) -> bytes:
        """Build a ClientHello handshake message body."""
        body = bytearray()
        body.extend(_DTLS_VERSION)  # client_version
        body.extend(self._client_random)  # random (32 bytes)
        body.append(0)  # session_id length = 0
        body.append(len(cookie))  # cookie length
        body.extend(cookie)  # cookie
        body.extend(struct.pack("!H", 2))  # cipher_suites length
        body.extend(_CIPHER_SUITE)  # TLS_PSK_WITH_AES_128_GCM_SHA256
        body.append(1)  # compression_methods length
        body.append(0)  # null compression
        return bytes(body)

    def _build_client_key_exchange(self) -> bytes:
        """Build a ClientKeyExchange message with PSK identity."""
        return struct.pack("!H", len(self._identity)) + self._identity

    def _build_finished(self) -> bytes:
        """Build the Finished message verify_data."""
        # Hash all handshake messages so far
        h = hashlib.sha256(self._handshake_messages).digest()
        return _prf(self._master_secret, b"client finished", h, 12)

    def _send_handshake(self, body: bytes, msg_type: int) -> None:
        """Wrap a handshake body in a handshake header and send as a record."""
        hs_header = struct.pack(
            "!B3sH3s3s",
            msg_type,
            len(body).to_bytes(3, "big"),  # length
            self._msg_seq,  # message_seq
            (0).to_bytes(3, "big"),  # fragment_offset
            len(body).to_bytes(3, "big"),  # fragment_length
        )
        full_msg = hs_header + body
        # Record handshake messages for Finished hash (exclude HelloVerifyRequest retransmits)
        self._handshake_messages.extend(full_msg)
        self._msg_seq += 1
        self._send_record(_CT_HANDSHAKE, full_msg)

    def _send_encrypted_handshake(self, body: bytes) -> None:
        """Send an encrypted Finished handshake message."""
        hs_header = struct.pack(
            "!B3sH3s3s",
            _HT_FINISHED,
            len(body).to_bytes(3, "big"),
            self._msg_seq,
            (0).to_bytes(3, "big"),
            len(body).to_bytes(3, "big"),
        )
        full_msg = hs_header + body
        self._msg_seq += 1
        self._send_encrypted(_CT_HANDSHAKE, full_msg)

    # -- Parsing --

    def _parse_hello_verify_request(self, data: bytes) -> bytes:
        """Extract the cookie from a HelloVerifyRequest record."""
        # Skip DTLS record header (13 bytes) + handshake header (12 bytes)
        # Then: version(2) + cookie_length(1) + cookie
        offset = 13 + 12 + 2  # record(13) + handshake(12) + version(2)
        cookie_len = data[offset]
        return data[offset + 1 : offset + 1 + cookie_len]

    def _parse_server_flight(self, data: bytes) -> None:
        """
        Parse the server's Flight 4: ServerHello + ServerHelloDone.

        These may arrive as multiple DTLS records in a single UDP packet.
        Each record has a 13-byte header, and each handshake message inside
        the record has a 12-byte handshake header.
        """
        got_server_hello = False
        got_server_hello_done = False
        offset = 0

        while offset < len(data):
            if offset + 13 > len(data):
                break
            # Parse DTLS record header
            record_len = int.from_bytes(data[offset + 11 : offset + 13], "big")
            record_payload = data[offset + 13 : offset + 13 + record_len]
            offset += 13 + record_len

            # Parse handshake messages inside the record
            hs_offset = 0
            while hs_offset < len(record_payload):
                if hs_offset + 12 > len(record_payload):
                    break
                hs_type = record_payload[hs_offset]
                hs_len = int.from_bytes(record_payload[hs_offset + 1 : hs_offset + 4], "big")
                hs_msg = record_payload[hs_offset : hs_offset + 12 + hs_len]

                if hs_type == _HT_SERVER_HELLO:
                    # Extract server_random: after handshake header(12) + version(2)
                    self._server_random = hs_msg[14 : 14 + 32]
                    self._handshake_messages.extend(hs_msg)
                    got_server_hello = True
                elif hs_type == _HT_SERVER_HELLO_DONE:
                    self._handshake_messages.extend(hs_msg)
                    got_server_hello_done = True

                hs_offset += 12 + hs_len

        # ServerHelloDone might arrive in a separate UDP packet
        if got_server_hello and not got_server_hello_done:
            try:
                data2 = self._recv()
                self._parse_server_flight(data2)
            except TimeoutError:
                pass

    # -- Key derivation --

    def _derive_keys(self) -> None:
        """Derive encryption keys from the PSK."""
        n = len(self._psk)
        # Pre-master secret for PSK: len(N) + zeros(N) + len(N) + psk(N)
        pre_master = struct.pack("!H", n) + b"\x00" * n + struct.pack("!H", n) + self._psk

        # Master secret
        self._master_secret = _prf(
            pre_master,
            b"master secret",
            self._client_random + self._server_random,
            48,
        )

        # Key block: client_write_key(16) + server_write_key(16) + client_write_iv(4) + server_write_iv(4)
        key_block = _prf(
            self._master_secret,
            b"key expansion",
            self._server_random + self._client_random,
            40,
        )
        self._client_write_key = key_block[0:16]
        # server_write_key = key_block[16:32]  # not needed for sending
        self._client_write_iv = key_block[32:36]
        # server_write_iv = key_block[36:40]  # not needed for sending

        self._aesgcm = AESGCM(self._client_write_key)


def _make_random() -> bytes:
    """Generate a 32-byte TLS random (4-byte time + 28 random bytes)."""
    return struct.pack("!I", int(time.time())) + os.urandom(28)


def _prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    """TLS 1.2 PRF with SHA-256."""
    result = b""
    a = hmac.new(secret, label + seed, hashlib.sha256).digest()  # A(1)
    while len(result) < length:
        result += hmac.new(secret, a + label + seed, hashlib.sha256).digest()
        a = hmac.new(secret, a, hashlib.sha256).digest()  # A(i+1)
    return result[:length]
