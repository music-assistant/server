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


def _init(scheme: bytes = b"cbcs", pattern: int = 0, default_size: int = 0) -> bytes:
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
    trex = _box(b"trex", b"\0" * 4 + struct.pack(">IIIII", 1, 1, 1024, default_size, 0))
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


TFHD_BASE_OFFSET = 0x1
TFHD_DESCRIPTION = 0x2
TFHD_DURATION = 0x8
TFHD_SIZE = 0x10
TFHD_BASE_IS_MOOF = 0x020000
TRUN_DATA_OFFSET = 0x1
TRUN_FIRST_FLAGS = 0x4
TRUN_DURATION = 0x100
TRUN_SIZE = 0x200
TRUN_FLAGS = 0x400
TRUN_COMPOSITION = 0x800


def _flagged(flags: int) -> bytes:
    return b"\0" + flags.to_bytes(3)


def _tfhd(flags: int = TFHD_BASE_IS_MOOF, fields: bytes = b"") -> bytes:
    return _box(b"tfhd", _flagged(flags) + struct.pack(">I", 1) + fields)


def _trun(
    count: int,
    data_offset: int | None = None,
    rows: bytes = b"",
    flags: int = 0,
    first: bytes = b"",
) -> bytes:
    if data_offset is not None:
        flags |= TRUN_DATA_OFFSET
    offset = b"" if data_offset is None else struct.pack(">i", data_offset)
    body = _flagged(flags) + struct.pack(">I", count) + offset + first + rows
    return _box(b"trun", body)  # codespell:ignore trun


def _sizes(samples: list[bytes]) -> bytes:
    return b"".join(struct.pack(">I", len(sample)) for sample in samples)


def _senc(layouts: list[list[tuple[int, int]]], flags: int = 0x2) -> bytes:
    rows = b"".join(
        struct.pack(">H", len(layout))
        + b"".join(struct.pack(">HI", clear, encrypted) for clear, encrypted in layout)
        for layout in layouts
    )
    return _box(b"senc", _flagged(flags) + struct.pack(">I", len(layouts)) + rows)


def _moof(*boxes: bytes) -> bytes:
    return _box(b"moof", _box(b"mfhd", b"\0" * 8) + _box(b"traf", b"".join(boxes)))


def _sample_with_layout(parts: list[tuple[bytes, bytes]]) -> tuple[bytes, bytes]:
    """Return a sample of clear and protected parts, as plain bytes and as stored."""
    plain = b"".join(clear + protected for clear, protected in parts)
    stored = b"".join(clear + _encrypt(protected) for clear, protected in parts)
    return plain, stored


def _with_senc(stored: list[bytes], senc: bytes) -> bytes:
    """Return a segment whose moof describes the stored samples, with this senc box."""

    def moof(data_offset: int) -> bytes:
        return _moof(_tfhd(), senc, _trun(len(stored), data_offset, _sizes(stored), TRUN_SIZE))

    return moof(len(moof(0)) + 8) + _box(b"mdat", b"".join(stored))


def test_subsamples_keep_their_clear_bytes_and_start_over_from_the_iv() -> None:
    """Only the protected ranges of a sample change, each one decrypted from the iv."""
    first_clear, second_clear = b"CLEAR", b"more"
    first_protected, second_protected = bytes(range(32)) + b"xyz", bytes(range(50, 66))
    plain_one, stored_one = _sample_with_layout(
        [(first_clear, first_protected), (second_clear, second_protected)]
    )
    plain_two, stored_two = _sample_with_layout([(b"", bytes(range(100, 132)))])
    plain_three, stored_three = _sample_with_layout([(b"only clear", b"")])
    segment = _with_senc(
        [stored_one, stored_two, stored_three],
        _senc(
            [
                [(len(first_clear), len(first_protected)), (len(second_clear), 16)],
                [(0, 32)],
                [(10, 0)],
            ]
        ),
    )
    assert segment.endswith(stored_one + stored_two + stored_three)
    assert stored_one != plain_one

    result = decrypt_segment(segment, KEY, parse_init_segment(_init()))

    assert len(result) == len(segment)
    assert result.endswith(plain_one + plain_two + plain_three)
    mdat = result[-len(plain_one + plain_two + plain_three) :]
    assert mdat[: len(first_clear)] == first_clear
    # the part of a protected range after its last whole block is stored as it is
    assert mdat[len(first_clear) + 32 : len(first_clear) + 35] == b"xyz"
    assert mdat[len(first_clear) + 35 : len(first_clear) + 39] == second_clear


def test_a_senc_box_before_or_after_the_track_run_gives_the_same_result() -> None:
    """The order of the boxes in a track fragment does not change which bytes are protected."""
    plain, stored = _sample_with_layout([(b"CLEAR", bytes(range(32)))])
    senc = _senc([[(5, 32)]])

    def moof(data_offset: int) -> bytes:
        return _moof(_tfhd(), _trun(1, data_offset, _sizes([stored]), TRUN_SIZE), senc)

    segment = moof(len(moof(0)) + 8) + _box(b"mdat", stored)
    assert decrypt_segment(segment, KEY, parse_init_segment(_init())).endswith(plain)


def test_a_senc_box_without_subsamples_means_whole_samples() -> None:
    """Without the subsample flag, or for a sample it does not list, a sample is protected whole."""
    samples = [bytes(range(32)), bytes(range(32, 64))]
    stored = [_encrypt(sample) for sample in samples]
    encryption = parse_init_segment(_init())

    no_flag = _with_senc(stored, _box(b"senc", _flagged(0) + struct.pack(">I", 2)))
    assert decrypt_segment(no_flag, KEY, encryption).endswith(b"".join(samples))

    first_listed = _with_senc([stored[0], stored[1]], _senc([[(0, 32)]]))
    assert decrypt_segment(first_listed, KEY, encryption).endswith(b"".join(samples))


def test_the_base_data_offset_of_a_track_fragment_is_used() -> None:
    """An absolute base offset counts from the segment, past the optional fields around it."""
    samples = [bytes(range(32)), bytes(range(32, 64))]
    stored = b"".join(_encrypt(sample) for sample in samples)
    emsg = _box(b"emsg", b"metadata")

    def moof(data_offset: int) -> bytes:
        flags = TFHD_BASE_OFFSET | TFHD_DESCRIPTION | TFHD_DURATION | TFHD_SIZE
        fields = struct.pack(">QIII", len(emsg), 1, 1024, 32)
        return _moof(_tfhd(flags, fields), _trun(2, data_offset))

    segment = emsg + moof(len(moof(0)) + 8) + _box(b"mdat", stored)
    result = decrypt_segment(segment, KEY, parse_init_segment(_init()))
    assert result.endswith(b"".join(samples))
    assert result.startswith(emsg)


@pytest.mark.parametrize(
    ("tfhd_size", "trex_size"),
    [(32, 0), (0, 32), (32, 16)],
    ids=["from-tfhd", "from-trex", "tfhd-before-trex"],
)
def test_a_track_run_without_sizes_uses_the_default_sample_size(
    tfhd_size: int, trex_size: int
) -> None:
    """The track fragment's default comes first, then the one of the init segment."""
    samples = [bytes(range(32)), bytes(range(32, 64))]
    stored = b"".join(_encrypt(sample) for sample in samples)
    flags = TFHD_BASE_IS_MOOF | (TFHD_SIZE if tfhd_size else 0)
    tfhd = _tfhd(flags, struct.pack(">I", tfhd_size) if tfhd_size else b"")

    def moof(data_offset: int) -> bytes:
        return _moof(tfhd, _trun(2, data_offset))

    segment = moof(len(moof(0)) + 8) + _box(b"mdat", stored)
    encryption = parse_init_segment(_init(default_size=trex_size))
    assert decrypt_segment(segment, KEY, encryption).endswith(b"".join(samples))


def test_the_per_sample_fields_of_a_track_run_are_skipped() -> None:
    """Only the size is read from a row, whatever else the row holds."""
    samples = [bytes(range(32)), bytes(range(32, 80))]
    stored = b"".join(_encrypt(sample) for sample in samples)
    rows = b"".join(struct.pack(">IIII", 1024, len(sample), 7, 9) for sample in samples)
    flags = TRUN_FIRST_FLAGS | TRUN_DURATION | TRUN_SIZE | TRUN_FLAGS | TRUN_COMPOSITION

    def moof(data_offset: int) -> bytes:
        return _moof(_tfhd(), _trun(2, data_offset, rows, flags, first=b"\0\0\0\0"))

    segment = moof(len(moof(0)) + 8) + _box(b"mdat", stored)
    assert decrypt_segment(segment, KEY, parse_init_segment(_init())).endswith(b"".join(samples))


def test_a_track_run_without_an_offset_continues_after_the_previous_one() -> None:
    """Two runs of one fragment describe samples right behind each other."""
    samples = [bytes(range(32)), bytes(range(32, 64))]
    stored = [_encrypt(sample) for sample in samples]

    def moof(data_offset: int) -> bytes:
        return _moof(
            _tfhd(),
            _trun(1, data_offset, _sizes(stored[:1]), TRUN_SIZE),
            _trun(1, None, _sizes(stored[1:]), TRUN_SIZE),
        )

    segment = moof(len(moof(0)) + 8) + _box(b"mdat", b"".join(stored))
    assert decrypt_segment(segment, KEY, parse_init_segment(_init())).endswith(b"".join(samples))


def test_each_fragment_of_a_segment_counts_from_its_own_moof() -> None:
    """A second fragment is not read at the offsets of the first one."""
    first_sample, second_sample = bytes(range(32)), bytes(range(32, 64))

    def fragment(sample: bytes) -> bytes:
        stored = _encrypt(sample)

        def moof(data_offset: int) -> bytes:
            return _moof(_tfhd(), _trun(1, data_offset, _sizes([stored]), TRUN_SIZE))

        return moof(len(moof(0)) + 8) + _box(b"mdat", stored)

    one, two = fragment(first_sample), fragment(second_sample)
    result = decrypt_segment(one + two, KEY, parse_init_segment(_init()))
    assert result[len(one) - 32 : len(one)] == first_sample
    assert result[-32:] == second_sample


def test_a_track_run_shorter_than_its_sample_count_is_refused() -> None:
    """Reading on into the next box would give a wrong size, so this fails instead."""
    stored = _encrypt(bytes(range(32)))

    def moof(data_offset: int) -> bytes:
        return _moof(
            _tfhd(), _trun(2, data_offset, _sizes([stored]), TRUN_SIZE), _box(b"free", b"\0" * 8)
        )

    segment = moof(len(moof(0)) + 8) + _box(b"mdat", stored)
    with pytest.raises(UnsupportedEncryptionError):
        decrypt_segment(segment, KEY, parse_init_segment(_init()))


@pytest.mark.parametrize(
    ("data_offset", "size"),
    [(10_000, 32), (-10_000, 32), (0, 10_000)],
    ids=["offset", "negative-offset", "size"],
)
def test_a_sample_past_the_end_of_the_segment_is_refused(data_offset: int, size: int) -> None:
    """A wrong offset or size fails instead of damaging the stream."""
    stored = _encrypt(bytes(range(32)))
    moof = _moof(_tfhd(), _trun(1, data_offset, struct.pack(">I", size), TRUN_SIZE))
    with pytest.raises(UnsupportedEncryptionError):
        decrypt_segment(moof + _box(b"mdat", stored), KEY, parse_init_segment(_init()))


def test_a_subsample_layout_larger_than_its_sample_is_refused() -> None:
    """A wrong layout would decrypt the sample behind it, so this fails instead."""
    samples = [bytes(range(32)), bytes(range(32, 64))]
    segment = _with_senc([_encrypt(sample) for sample in samples], _senc([[(0, 64)]]))
    with pytest.raises(UnsupportedEncryptionError):
        decrypt_segment(segment, KEY, parse_init_segment(_init()))
