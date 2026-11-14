"""Reject oversized ID3 tags before parsing audio metadata."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers import tags


def _id3_header(size: int, version: int = 3) -> bytes:
    return (
        b"ID3" + bytes((version, 0, 0)) + bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    )


@pytest.fixture
def probe_process(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a probe that reports a file without audio streams."""
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b'{"format": {}, "streams": []}', b""))
    process.__aenter__ = AsyncMock(return_value=process)
    process.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=process)
    monkeypatch.setattr(tags, "AsyncProcess", factory)
    return factory


@pytest.mark.parametrize("version", [2, 3, 4])
async def test_oversized_id3_rejected_before_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_process: MagicMock, version: int
) -> None:
    """A huge declared tag must not reach either memory-intensive parser."""
    source = tmp_path / "oversized.mp3"
    source.write_bytes(_id3_header(161481653, version))
    mutagen = MagicMock()
    monkeypatch.setattr(tags, "parse_tags_mutagen", mutagen)

    with pytest.raises(InvalidDataError, match="ID3 metadata is too large"):
        await tags.async_parse_tags(str(source))

    probe_process.assert_not_called()
    mutagen.assert_not_called()


async def test_oversized_id3_closes_opener_descriptor(
    tmp_path: Path, probe_process: MagicMock
) -> None:
    """Rejected metadata still closes a provider's confined file descriptor."""
    source = tmp_path / "oversized.mp3"
    source.write_bytes(_id3_header(tags.MAX_ID3_TAG_SIZE + 1))
    descriptor = os.open(source, os.O_RDONLY)

    with pytest.raises(InvalidDataError, match="ID3 metadata is too large"):
        await tags.async_parse_tags("provider-file.mp3", opener=lambda _path, _flags: descriptor)

    probe_process.assert_not_called()
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


@pytest.mark.parametrize("header", [b"", b"RIFF123456", _id3_header(16 * 1024 * 1024)])
async def test_small_or_other_headers_continue_to_probe(
    tmp_path: Path, probe_process: MagicMock, header: bytes
) -> None:
    """The header guard leaves ordinary validation to the audio parser."""
    source = tmp_path / "audio"
    source.write_bytes(header)

    with pytest.raises(InvalidDataError, match="Not an audio file"):
        await tags.async_parse_tags(str(source))

    probe_process.assert_called_once()


async def test_invalid_id3_size_rejected(tmp_path: Path, probe_process: MagicMock) -> None:
    """Invalid synchsafe size bytes must not bypass the size guard."""
    source = tmp_path / "invalid.mp3"
    source.write_bytes(b"ID3\x03\x00\x00\xff\x00\x00\x00")

    with pytest.raises(InvalidDataError, match="Invalid ID3 tag size"):
        await tags.async_parse_tags(str(source))

    probe_process.assert_not_called()


async def test_header_check_preserves_descriptor_position(
    tmp_path: Path, probe_process: MagicMock
) -> None:
    """The probe still reads a provider's descriptor from the start of the file."""
    source = tmp_path / "audio"
    source.write_bytes(_id3_header(0))
    descriptor = os.open(source, os.O_RDONLY)
    positions: list[int] = []
    process: MagicMock = probe_process.return_value

    def start_probe(*_args: object, **_kwargs: object) -> MagicMock:
        positions.append(os.lseek(descriptor, 0, os.SEEK_CUR))
        return process

    probe_process.side_effect = start_probe

    with pytest.raises(InvalidDataError, match="Not an audio file"):
        await tags.async_parse_tags("provider-file.mp3", opener=lambda _path, _flags: descriptor)

    assert positions == [0]


@pytest.mark.parametrize("operation", ["open", "read"])
async def test_header_io_error_becomes_file_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_process: MagicMock, operation: str
) -> None:
    """Unreadable files are reported as a file error and never reach the probe."""
    source = tmp_path / "audio"
    source.write_bytes(_id3_header(0))
    failing_io = MagicMock(side_effect=OSError("Input/output error"))
    if operation == "open":
        monkeypatch.setattr(tags, "open", failing_io, raising=False)
        with pytest.raises(InvalidDataError, match="Input/output error"):
            await tags.async_parse_tags(str(source))
    else:
        monkeypatch.setattr(os, "pread", failing_io)
        descriptor = os.open(source, os.O_RDONLY)
        with pytest.raises(InvalidDataError, match="Input/output error"):
            await tags.async_parse_tags(
                "provider-file.mp3", opener=lambda _path, _flags: descriptor
            )
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)

    probe_process.assert_not_called()
