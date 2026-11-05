"""Reject oversized ID3 tags before parsing audio metadata."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers import tags


def _id3_header(size: int, version: int = 3) -> bytes:
    return (
        b"ID3" + bytes((version, 0, 0)) + bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    )


@pytest.mark.parametrize("version", [2, 3, 4])
def test_oversized_id3_rejected_before_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """A huge declared tag must not reach either memory-intensive parser."""
    source = tmp_path / "oversized.mp3"
    source.write_bytes(_id3_header(161481653, version))
    probe = MagicMock()
    mutagen = MagicMock()
    monkeypatch.setattr(subprocess, "check_output", probe)
    monkeypatch.setattr(tags, "parse_tags_mutagen", mutagen)

    with pytest.raises(InvalidDataError, match="ID3 metadata is too large"):
        tags.parse_tags(str(source))

    probe.assert_not_called()
    mutagen.assert_not_called()


def test_oversized_id3_closes_opener_descriptor(tmp_path: Path) -> None:
    """Rejected metadata still closes a provider's confined file descriptor."""
    source = tmp_path / "oversized.mp3"
    source.write_bytes(_id3_header(tags.MAX_ID3_TAG_SIZE + 1))
    descriptor = os.open(source, os.O_RDONLY)

    with pytest.raises(InvalidDataError, match="ID3 metadata is too large"):
        tags.parse_tags("provider-file.mp3", opener=lambda _path, _flags: descriptor)

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


@pytest.mark.parametrize("header", [b"", b"RIFF123456", _id3_header(16 * 1024 * 1024)])
def test_small_or_other_headers_continue_to_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: bytes
) -> None:
    """The header guard leaves ordinary validation to the audio parser."""
    source = tmp_path / "audio"
    source.write_bytes(header)
    probe = MagicMock(return_value=b'{"streams": []}')
    monkeypatch.setattr(subprocess, "check_output", probe)

    with pytest.raises(InvalidDataError, match="Not an audio file"):
        tags.parse_tags(str(source))

    probe.assert_called_once()


def test_invalid_id3_size_rejected(tmp_path: Path) -> None:
    """Invalid synchsafe size bytes must not bypass the size guard."""
    source = tmp_path / "invalid.mp3"
    source.write_bytes(b"ID3\x03\x00\x00\xff\x00\x00\x00")
    with pytest.raises(InvalidDataError, match="Invalid ID3 tag size"):
        tags.parse_tags(str(source))
