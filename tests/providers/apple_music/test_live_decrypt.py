"""Unit tests for the in-memory decryption of Apple Music live radio segments."""

import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from music_assistant.providers.apple_music.helpers.live_decrypt import (
    UnsupportedEncryptionError,
    decrypt_segment,
    parse_init_segment,
)

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
IV = bytes.fromhex("1bc6888e8a60c19e6b49aacba47dd32a")
KID = bytes.fromhex("aabbccddaabbccddaabbccddaabbccdd")


def _box(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), name) + payload


def _init(scheme: bytes = b"cbcs", pattern: int = 0) -> bytes:
    tenc = _box(b"tenc", b"\x01\0\0\0\0" + bytes([pattern]) + b"\x01\0" + KID + b"\x10" + IV)
    sinf = _box(
        b"sinf",
        _box(b"frma", b"mp4a")
        + _box(b"schm", b"\0\0\0\0" + scheme + b"\0\x01\0\0")
        + _box(b"schi", tenc),
    )
    enca = _box(b"enca", b"\0" * 28 + _box(b"esds", b"\0\0\0\0config") + sinf)
    stsd = _box(b"stsd", b"\0\0\0\0\0\0\0\x01" + enca)
    stbl = _box(b"stbl", stsd)
    trex = _box(b"trex", b"\0" * 4 + struct.pack(">IIIII", 1, 1, 1024, 0, 0))
    moov = _box(
        b"moov",
        _box(b"trak", _box(b"mdia", _box(b"minf", stbl))) + _box(b"mvex", trex),
    )
    return _box(b"ftyp", b"cmfc") + moov


def _encrypt(sample: bytes) -> bytes:
    whole = len(sample) - len(sample) % 16
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return encryptor.update(sample[:whole]) + sample[whole:]


def _segment(samples: list[bytes]) -> bytes:
    encrypted = b"".join(_encrypt(sample) for sample in samples)
    tfhd = _box(b"tfhd", b"\0\x02\0\0" + struct.pack(">I", 1))

    def moof(data_offset: int) -> bytes:
        rows = b"".join(struct.pack(">I", len(sample)) for sample in samples)
        run_body = b"\0\0\x02\x01" + struct.pack(">Ii", len(samples), data_offset) + rows
        track_run = _box(b"trun", run_body)  # codespell:ignore trun
        return _box(b"moof", _box(b"mfhd", b"\0" * 8) + _box(b"traf", tfhd + track_run))

    offset = len(moof(0)) + 8
    return _box(b"emsg", b"metadata") + moof(offset) + _box(b"mdat", encrypted)


def test_init_segment_gives_the_key_id_and_iv() -> None:
    """The settings needed to decrypt come from the init segment."""
    encryption = parse_init_segment(_init())
    assert encryption.key_id == KID.hex()
    assert encryption.constant_iv == IV
    assert encryption.audio_config.endswith(b"config")


@pytest.mark.parametrize(("scheme", "pattern"), [(b"cenc", 0), (b"cbcs", 0x19)])
def test_other_layouts_are_refused(scheme: bytes, pattern: int) -> None:
    """A layout the decryption does not handle fails instead of playing noise."""
    with pytest.raises(UnsupportedEncryptionError):
        parse_init_segment(_init(scheme, pattern))


def test_samples_are_decrypted_and_the_rest_stays_untouched() -> None:
    """Each sample starts over from the iv, and its last partial block stays clear."""
    samples = [bytes(range(40)), bytes(range(100, 132)), b"short"]
    segment = _segment(samples)
    result = decrypt_segment(segment, KEY, parse_init_segment(_init()))
    assert len(result) == len(segment)
    assert result.endswith(b"".join(samples))
    assert result[:16] == segment[:16]
    assert not segment.endswith(b"".join(samples))
