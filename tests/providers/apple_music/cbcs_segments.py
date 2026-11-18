"""Build cbcs encrypted live radio segments like those of Apple Music Chill, for the tests."""

import struct
import subprocess
from itertools import pairwise
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = bytes.fromhex("a" * 32)
IV = bytes(range(16))
_CONTAINERS = {b"moov": 0, b"trak": 0, b"mdia": 0, b"minf": 0, b"stbl": 0, b"stsd": 8}
# a saiz box with a default info size, and a saio box with one 32 bit offset
_SAIZ_SIZE = 17
_SAIO_SIZE = 20


def box(name: bytes, body: bytes) -> bytes:
    """Return a box of a type with a body."""
    return struct.pack(">I4s", 8 + len(body), name) + body


def children(data: bytes) -> list[tuple[bytes, bytes]]:
    """Return the type and the body of each box in data."""
    boxes: list[tuple[bytes, bytes]] = []
    position = 0
    while position < len(data):
        size, name = struct.unpack_from(">I4s", data, position)
        boxes.append((name, data[position + 8 : position + size]))
        position += size
    return boxes


def tenc(version: int = 1, pattern: int = 0, iv_size: int = 0) -> bytes:
    """Return a track encryption box, with the constant IV when there is no per-sample IV."""
    body = bytes([version, 0, 0, 0, 0, pattern, 1, iv_size]) + b"k" * 16
    if iv_size == 0:
        body += bytes([16]) + IV
    return box(b"tenc", body)


def encrypt_init(
    init: bytes, scheme: bytes = b"cbcs", track_encryption: bytes | None = None
) -> bytes:
    """Return a clear AAC init segment as an encrypted one of the given scheme."""
    sinf = box(
        b"sinf",
        box(b"frma", b"mp4a")
        + box(b"schm", b"\0\0\0\0" + scheme + b"\0\x01\0\0")
        + box(b"schi", track_encryption or tenc()),
    )

    def rewrite(data: bytes) -> bytes:
        out = b""
        for name, body in children(data):
            if name == b"mp4a":
                out += box(b"enca", body + sinf)
            elif (skip := _CONTAINERS.get(name)) is not None:
                out += box(name, body[:skip] + rewrite(body[skip:]))
            else:
                out += box(name, body)
        return out

    return rewrite(init)


def encrypt_segment(segment: bytes, clear_bytes: int = 0, extra_traf_boxes: bytes = b"") -> bytes:
    """
    Return a clear media segment as a cbcs encrypted one, with one subsample per sample.

    Each track fragment gets a senc box with the subsamples, and a saiz and a saio box that
    point at them, like the segments of Apple.

    :param segment: Pairs of a moof box and its mdat box.
    :param clear_bytes: The clear bytes each subsample declares in front of the encrypted ones.
    :param extra_traf_boxes: Boxes to add to each track fragment after the saio box.
    """
    boxes = children(segment)
    return b"".join(
        _encrypt_fragment(moof, mdat, clear_bytes, extra_traf_boxes)
        for (_, moof), (_, mdat) in zip(boxes[::2], boxes[1::2], strict=True)
    )


def fragmented_aac(tmp_path: Path) -> tuple[bytes, list[bytes]]:
    """Return the init segment and three segments of a 6 second clear AAC fMP4 from ffmpeg."""
    path = tmp_path / "clear.mp4"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-f",
            "mp4",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration",
            "2000000",
            str(path),
        ],
        check=True,
    )
    data = path.read_bytes()
    boxes: list[tuple[bytes, bytes]] = []
    position = 0
    while position < len(data):
        size, name = struct.unpack(">I4s", data[position : position + 8])
        boxes.append((name, data[position : position + size]))
        position += size
    init = b"".join(box for name, box in boxes if name in (b"ftyp", b"moov"))
    fragments = [
        moof + mdat
        for (first, moof), (second, mdat) in pairwise(boxes)
        if (first, second) == (b"moof", b"mdat")
    ]
    # ffmpeg may close with a short fragment, a segment can hold more than one
    return init, [*fragments[:2], b"".join(fragments[2:])]


def _encrypt_fragment(moof: bytes, mdat: bytes, clear_bytes: int, extra_traf_boxes: bytes) -> bytes:
    """Return the body of a moof and an mdat as an encrypted moof and mdat."""
    sizes: list[int] = []
    new_moof = b""
    for name, body in children(moof):
        if name != b"traf":
            new_moof += box(name, body)
            continue
        traf_children = children(body)
        found = dict(traf_children)
        sizes = _trun_sizes(found[b"trun"], _default_size(found[b"tfhd"]))
        senc = box(
            b"senc",
            b"\0\0\0\x02"
            + struct.pack(">I", len(sizes))
            + b"".join(struct.pack(">HHI", 1, clear_bytes, size - clear_bytes) for size in sizes),
        )
        added = len(senc) + _SAIZ_SIZE + _SAIO_SIZE + len(extra_traf_boxes)
        new_traf = b""
        for child_name, child in traf_children:
            if child_name == b"trun":
                # the audio moves back by the boxes that are added in front of it
                (offset,) = struct.unpack_from(">i", child, 8)
                new_traf += box(
                    child_name, child[:8] + struct.pack(">i", offset + added) + child[12:]
                )
            else:
                new_traf += box(child_name, child)
        # the offset of the senc entries from the start of the moof, its header included
        entries = 8 + len(new_moof) + 8 + len(new_traf) + 16
        saiz = box(b"saiz", b"\0\0\0\0" + bytes([8]) + struct.pack(">I", len(sizes)))
        saio = box(b"saio", b"\0\0\0\0" + struct.pack(">II", 1, entries))
        new_moof += box(b"traf", new_traf + senc + saiz + saio + extra_traf_boxes)
    return box(b"moof", new_moof) + box(b"mdat", _encrypt_samples(mdat, sizes))


def _encrypt_samples(mdat: bytes, sizes: list[int]) -> bytes:
    """Return the samples with their whole 16 byte blocks encrypted, the IV reset per sample."""
    out = b""
    position = 0
    for size in sizes:
        sample = mdat[position : position + size]
        whole = len(sample) // 16 * 16
        encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
        out += encryptor.update(sample[:whole]) + encryptor.finalize() + sample[whole:]
        position += size
    return out


def _default_size(body: bytes) -> int | None:
    """Return the default sample size of a track fragment header."""
    flags = int.from_bytes(body[1:4])
    position = 8 + (8 if flags & 0x1 else 0) + (4 if flags & 0x2 else 0) + (4 if flags & 0x8 else 0)
    return int(struct.unpack_from(">I", body, position)[0]) if flags & 0x10 else None


def _trun_sizes(body: bytes, default_size: int | None) -> list[int]:
    """Return the sample sizes of a track run as ffmpeg writes it, with a data offset."""
    flags = int.from_bytes(body[1:4])
    (count,) = struct.unpack_from(">I", body, 4)
    assert flags & 0x1
    if not flags & 0x200:
        assert default_size is not None
        return [default_size] * count
    position = 12 + (4 if flags & 0x4 else 0)
    fields = [flag for flag in (0x100, 0x200, 0x400, 0x800) if flags & flag]
    index = fields.index(0x200)
    return [
        int(struct.unpack_from(">I", body, position + (sample * len(fields) + index) * 4)[0])
        for sample in range(count)
    ]
