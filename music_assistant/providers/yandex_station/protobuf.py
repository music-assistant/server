"""
Lightweight protobuf encoder/decoder for Yandex externalCommandBypass.

Adapted from AlexxIT/YandexStation (MIT license).
"""

from __future__ import annotations

import base64


class Protobuf:
    """Minimal protobuf wire-format parser."""

    def __init__(self, raw: str | bytes) -> None:
        """Initialize from base64 string or raw bytes."""
        self.raw = base64.b64decode(raw) if isinstance(raw, str) else raw
        self.pos = 0

    def read(self, length: int) -> bytes:
        """Read exactly `length` bytes from buffer."""
        if self.pos + length > len(self.raw):
            msg = "Read past end of buffer"
            raise IndexError(msg)
        self.pos += length
        return self.raw[self.pos - length : self.pos]

    def read_byte(self) -> int:
        """Read a single byte."""
        res = self.raw[self.pos]
        self.pos += 1
        return res

    def read_varint(self) -> int:
        """Read a variable-length integer."""
        res = 0
        shift = 0
        while True:
            b = self.read_byte()
            res += (b & 0x7F) << shift
            if b & 0x80 == 0:
                break
            shift += 7
        return res

    def read_bytes(self) -> bytes:
        """Read a length-prefixed byte string."""
        length = self.read_varint()
        return self.read(length)

    def read_dict(self) -> dict[int, object]:
        """Parse the buffer into a tag→value dict."""
        res: dict[int, object] = {}
        while self.pos < len(self.raw):
            b = self.read_varint()
            typ = b & 0b111
            tag = b >> 3

            if typ == 0:  # VARINT
                v: object = self.read_varint()
            elif typ == 1:  # I64
                v = self.read(8)
            elif typ == 2:  # LEN
                raw_bytes = self.read_bytes()
                try:
                    nested = Protobuf(raw_bytes)
                    parsed = nested.read_dict()
                    # Only accept as nested message if ALL bytes were consumed
                    v = parsed if nested.pos == len(nested.raw) and parsed else raw_bytes
                except Exception:
                    v = raw_bytes
            elif typ == 5:  # I32
                v = self.read(4)
            else:
                msg = f"Unsupported protobuf wire type: {typ}"
                raise NotImplementedError(msg)

            if tag in res:
                existing = res[tag]
                if isinstance(existing, list):
                    existing.append(v)
                else:
                    res[tag] = [existing, v]
            else:
                res[tag] = v

        return res


def _append_varint(b: bytearray, i: int) -> None:
    while i >= 0x80:
        b.append(0x80 | (i & 0x7F))
        i >>= 7
    b.append(i)


def loads(raw: str | bytes) -> dict[int, object]:
    """Decode protobuf wire format to dict."""
    return Protobuf(raw).read_dict()


def dumps(data: dict[int, str]) -> bytes:
    """Encode dict to protobuf wire format (string values only)."""
    b = bytearray()
    for tag, value in data.items():
        _append_varint(b, tag << 3 | 2)
        encoded = value.encode()
        _append_varint(b, len(encoded))
        b.extend(encoded)
    return bytes(b)
