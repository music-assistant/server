"""Test how the cbcs segments of a live station are prepared for ffmpeg."""

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from music_assistant.providers.apple_music.cbcs import (
    drop_whole_sample_subsamples,
    encrypts_whole_samples,
)
from tests.providers.apple_music.cbcs_segments import (
    box,
    children,
    encrypt_init,
    encrypt_segment,
    fragmented_aac,
    tenc,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(name="clear")
def clear_fixture(tmp_path: Path) -> tuple[bytes, list[bytes]]:
    """Return the init segment and the segments of a clear AAC fMP4."""
    return fragmented_aac(tmp_path)


def _decode(data: bytes) -> tuple[bytes, str]:
    """Return the PCM and the errors of ffmpeg for an fMP4 with the test key."""
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-v",
            "error",
            "-decryption_key",
            "a" * 32,
            "-f",
            "mp4",
            "-i",
            "pipe:",
            "-f",
            "f32le",
            "pipe:",
        ],
        input=data,
        capture_output=True,
        check=True,
    )
    return result.stdout, result.stderr.decode()


def _box_names(segment: bytes) -> list[bytes]:
    """Return the types of the boxes in the track fragments of a segment."""
    names: list[bytes] = []
    for name, body in children(segment):
        if name == b"moof":
            for child, child_body in children(body):
                if child == b"traf":
                    names += [traf_child for traf_child, _ in children(child_body)]
    return names


def test_whole_sample_subsamples_decrypt_like_the_clear_audio(
    clear: tuple[bytes, list[bytes]],
) -> None:
    """The decoder refuses the subsamples of these segments, and decrypts them without them."""
    init, segments = clear
    encrypted_init = encrypt_init(init)
    encrypted = [encrypt_segment(segment) for segment in segments]
    clear_pcm, _ = _decode(init + b"".join(segments))

    refused_pcm, errors = _decode(encrypted_init + b"".join(encrypted))
    prepared_pcm, _ = _decode(
        encrypted_init + b"".join(drop_whole_sample_subsamples(s) for s in encrypted)
    )

    assert refused_pcm == b""
    assert "pattern encryption is not present in 'cbcs' scheme" in errors
    assert prepared_pcm == clear_pcm


def test_the_subsample_boxes_become_free_boxes_of_the_same_size(
    clear: tuple[bytes, list[bytes]],
) -> None:
    """The senc, saiz and saio boxes are renamed, so no offset in the segment moves."""
    _, segments = clear
    encrypted = encrypt_segment(segments[0])

    prepared = drop_whole_sample_subsamples(encrypted)

    assert len(prepared) == len(encrypted)
    assert {b"senc", b"saiz", b"saio"} <= set(_box_names(encrypted))
    assert _box_names(prepared).count(b"free") == 3
    assert not {b"senc", b"saiz", b"saio"} & set(_box_names(prepared))


def test_subsamples_with_clear_bytes_are_kept(clear: tuple[bytes, list[bytes]]) -> None:
    """A subsample with clear bytes in front is not a whole sample, so the segment stays."""
    _, segments = clear
    encrypted = encrypt_segment(segments[0], clear_bytes=16)

    assert drop_whole_sample_subsamples(encrypted) == encrypted


def test_a_sample_group_of_its_own_keeps_the_subsamples(clear: tuple[bytes, list[bytes]]) -> None:
    """A fragment with its own encryption settings in a sample group stays as it is."""
    _, segments = clear
    seig = box(b"sgpd", b"\x01\0\0\0seig" + b"\0" * 8)
    encrypted = encrypt_segment(segments[0], extra_traf_boxes=seig)

    assert drop_whole_sample_subsamples(encrypted) == encrypted


def test_a_cut_segment_never_fails(clear: tuple[bytes, list[bytes]]) -> None:
    """A segment cut anywhere in its boxes is passed on, ffmpeg reports what is wrong with it."""
    _, segments = clear
    encrypted = encrypt_segment(segments[0])
    moof_size = struct.unpack_from(">I", encrypted)[0]

    for cut in range(moof_size + 16):
        assert isinstance(drop_whole_sample_subsamples(encrypted[:cut]), bytes)


def _fragment(trun_count: int, senc: bytes) -> bytes:
    """Return a moof with a default sample size of 100, a run of samples and a senc box."""
    tfhd = box(b"tfhd", b"\0\0\0\x10" + struct.pack(">II", 1, 100))
    trun = box(b"trun", b"\0\0\0\0" + struct.pack(">I", trun_count))
    return box(b"moof", box(b"traf", tfhd + trun + senc))


def _senc_body(count: int, entries: int) -> bytes:
    """Return the body of a senc box with whole sample subsamples of 100 bytes."""
    return b"\0\0\0\x02" + struct.pack(">I", count) + struct.pack(">HHI", 1, 0, 100) * entries


def test_a_senc_box_with_a_64_bit_size_is_renamed_at_its_type() -> None:
    """The type of a box with a large size sits in front of the size, it is renamed there."""
    body = _senc_body(3, 3)
    senc = struct.pack(">I4sQ", 1, b"senc", 16 + len(body)) + body
    segment = _fragment(3, senc)

    assert drop_whole_sample_subsamples(segment) == segment.replace(b"senc", b"free")


@pytest.mark.parametrize(
    ("trun_count", "senc_count", "entries"),
    [(0x0FFFFFFF, 3, 3), (3, 0xFFFFFFFF, 3), (3, 4, 3)],
)
def test_counts_that_do_not_fit_keep_the_segment(
    trun_count: int, senc_count: int, entries: int
) -> None:
    """A sample count larger than its box is not believed, the segment stays as it is."""
    segment = _fragment(trun_count, box(b"senc", _senc_body(senc_count, entries)))

    assert drop_whole_sample_subsamples(segment) == segment


@pytest.mark.parametrize(
    ("scheme", "track_encryption", "whole"),
    [
        (b"cbcs", tenc(), True),
        (b"cbcs", tenc(version=0), True),
        (b"cbcs", tenc(pattern=0x19), False),
        (b"cbcs", tenc(iv_size=16), False),
        (b"cenc", tenc(iv_size=8), False),
    ],
)
def test_only_cbcs_with_a_constant_iv_and_no_pattern_encrypts_whole_samples(
    clear: tuple[bytes, list[bytes]], scheme: bytes, track_encryption: bytes, whole: bool
) -> None:
    """Without a constant IV the IVs are in the senc box, so it must stay."""
    init, _ = clear

    assert encrypts_whole_samples(encrypt_init(init, scheme, track_encryption)) is whole


def test_a_clear_init_segment_does_not_encrypt_whole_samples(
    clear: tuple[bytes, list[bytes]],
) -> None:
    """A track without encryption has nothing to prepare."""
    init, _ = clear

    assert encrypts_whole_samples(init) is False
