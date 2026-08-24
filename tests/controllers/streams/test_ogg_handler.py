"""Tests for chained Ogg metadata handling."""

from __future__ import annotations

import struct
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant

from music_assistant.controllers.streams.ogg_handler import (
    OGG_FLAG_BOS,
    OGG_FLAG_EOS,
    get_chained_ogg_stream,
)

DEFAULT_VENDOR = "Music Assistant"
DEFAULT_SERIAL = 1
OGG_FLAC_MAPPING_PACKET = b"\x7fFLAC\x01\x00\x00\x01fLaC"
FLAC_METADATA_BLOCK_STREAMINFO = 0
FLAC_METADATA_BLOCK_VORBIS_COMMENT = 4
FLAC_METADATA_BLOCK_IS_LAST_FLAG = 0x80


class _FakeAudioStreamController:
    """Minimal audio controller stub for get_chained_ogg_stream tests."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def get_reconnecting_radio_stream(self, url: str) -> AsyncGenerator[bytes]:
        """Yield the provided Ogg chunks."""
        del url
        for chunk in self._chunks:
            yield chunk


def _pack_vorbis_comments(comments: dict[str, str], vendor: str = DEFAULT_VENDOR) -> bytes:
    """Build a Vorbis comments payload."""
    result = len(vendor).to_bytes(4, "little")
    result += vendor.encode("utf-8")
    result += len(comments).to_bytes(4, "little")
    for key, value in comments.items():
        comment = f"{key}={value}".encode()
        result += len(comment).to_bytes(4, "little")
        result += comment
    return result


def _pack_flac_metadata_block(block_type: int, payload: bytes, last: bool = False) -> bytes:
    """Build a FLAC metadata block."""
    block_header = (FLAC_METADATA_BLOCK_IS_LAST_FLAG if last else 0) | block_type
    block_length = len(payload).to_bytes(3, "big")
    return bytes([block_header]) + block_length + payload


def _build_ogg_page(
    segment_data: bytes,
    *,
    header_type: int = 0,
    granule_position: int = 0,
    serial_number: int = DEFAULT_SERIAL,
    page_sequence: int = 0,
) -> bytes:
    """Build a minimal raw Ogg page."""
    if len(segment_data) > 255:
        msg = "Test helper only supports single-segment pages"
        raise ValueError(msg)

    num_segments = 1 if segment_data else 0
    page = bytearray(27 + num_segments + len(segment_data))
    page[0:4] = b"OggS"
    page[4] = 0
    page[5] = header_type
    struct.pack_into("<Q", page, 6, granule_position)
    struct.pack_into("<I", page, 14, serial_number)
    struct.pack_into("<I", page, 18, page_sequence)
    page[22:26] = b"\x00\x00\x00\x00"
    page[26] = num_segments
    if segment_data:
        page[27] = len(segment_data)
        page[28:] = segment_data
    return bytes(page)


def _build_bos_page(segment_data: bytes = b"", page_sequence: int = 0) -> bytes:
    """Build a beginning-of-stream page."""
    return _build_ogg_page(segment_data, header_type=OGG_FLAG_BOS, page_sequence=page_sequence)


def _build_eos_page(
    page_sequence: int,
    *,
    granule_position: int = 0,
    serial_number: int = DEFAULT_SERIAL,
) -> bytes:
    """Build an end-of-stream page."""
    return _build_ogg_page(
        b"",
        header_type=OGG_FLAG_EOS,
        granule_position=granule_position,
        serial_number=serial_number,
        page_sequence=page_sequence,
    )


def _build_vorbis_comment_page(comments: dict[str, str], page_sequence: int = 1) -> bytes:
    """Build a Vorbis comment header page."""
    return _build_ogg_page(
        b"\x03vorbis" + _pack_vorbis_comments(comments),
        page_sequence=page_sequence,
    )


def _build_flac_streaminfo_page(page_sequence: int = 2) -> bytes:
    """Build a FLAC STREAMINFO metadata page."""
    streaminfo_payload = b"\x00" * 34
    streaminfo_block = _pack_flac_metadata_block(
        FLAC_METADATA_BLOCK_STREAMINFO, streaminfo_payload, last=True
    )
    return _build_ogg_page(streaminfo_block, page_sequence=page_sequence)


def _build_flac_vorbis_comment_page(comments: dict[str, str], page_sequence: int = 2) -> bytes:
    """Build a FLAC Vorbis comment metadata page."""
    payload = _pack_vorbis_comments(comments)
    block = _pack_flac_metadata_block(FLAC_METADATA_BLOCK_VORBIS_COMMENT, payload, last=True)
    return _build_ogg_page(block, page_sequence=page_sequence)


def _build_incomplete_flac_vorbis_comment_page(page_sequence: int = 2) -> bytes:
    """Build a truncated FLAC Vorbis comment metadata page."""
    payload = _pack_vorbis_comments({"TITLE": "Lossless Song"})
    block = _pack_flac_metadata_block(FLAC_METADATA_BLOCK_VORBIS_COMMENT, payload, last=True)
    truncated_length = len(block) - 1
    truncated_block = block[:truncated_length]
    return _build_ogg_page(truncated_block, page_sequence=page_sequence)


async def _collect_metadata(chunks: list[bytes]) -> list[dict[str, str]]:
    """Run a raw Ogg stream through the handler and collect metadata updates."""
    metadata_updates: list[dict[str, str]] = []
    mass = cast(
        "MusicAssistant",
        SimpleNamespace(
            streams=SimpleNamespace(audio=_FakeAudioStreamController(chunks)),
        ),
    )

    async for _ in get_chained_ogg_stream(
        mass,
        "https://example.invalid/test.ogg",
        metadata_updates.append,
    ):
        pass

    return metadata_updates


def _stream_details(*, opt_in: bool) -> StreamDetails:
    """Return stream details for testing the Ogg metadata handoff."""
    return StreamDetails(
        item_id="radio1",
        provider="test",
        audio_format=AudioFormat(content_type=ContentType.FLAC, channels=2),
        media_type=MediaType.RADIO,
        stream_type=StreamType.IN_BAND,
        path="https://example.invalid/test.ogg",
        duration=0,
        data={STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY: True} if opt_in else None,
        stream_metadata_update_callback=MagicMock(),
    )


def test_ogg_metadata_handoff_requires_explicit_opt_in() -> None:
    """Only opted-in providers receive chained-Ogg titles through StreamDetails.data."""
    audio = StreamsAudio(MagicMock())
    metadata = {"title": "Some Song", "artist": "Some Artist"}

    opted_in = _stream_details(opt_in=True)
    audio._handle_inband_metadata(opted_in, metadata)
    assert opted_in.data is not None
    assert opted_in.data[STREAMDETAILS_INBAND_TITLE_KEY] == "Some Artist - Some Song"
    assert opted_in.stream_metadata is None

    legacy = _stream_details(opt_in=False)
    legacy_mass = cast("Any", audio.mass)
    legacy_mass.metadata.normalize_radio_artist_name.side_effect = lambda artist: artist
    legacy_mass.metadata.get_radio_stream_station_image.return_value = None
    audio._handle_inband_metadata(legacy, metadata)
    assert legacy.stream_metadata is not None
    assert legacy.stream_metadata.title == "Some Song"
    assert legacy.stream_metadata.artist == "Some Artist"


async def test_vorbis_comment_page_emits_metadata() -> None:
    """Vorbis comment header pages should emit metadata."""
    # Setup
    comments = {"TITLE": "Song Title", "ARTIST": "Test Artist", "ALBUM": "Live Set"}
    chunks = [
        _build_bos_page(),
        _build_vorbis_comment_page(comments),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == [
        {
            "title": "Song Title",
            "artist": "Test Artist",
            "album": "Live Set",
        }
    ]


async def test_flac_vorbis_comment_page_emits_metadata_after_mapping_bos_page() -> None:
    """FLAC Vorbis comment pages should emit metadata after the mapping BOS page."""
    # Setup
    comments = {"TITLE": "Lossless Song", "ARTIST": "Studio Band", "ALBUM": "Broadcast"}
    chunks = [
        _build_bos_page(OGG_FLAC_MAPPING_PACKET),
        _build_flac_vorbis_comment_page(comments),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == [
        {
            "title": "Lossless Song",
            "artist": "Studio Band",
            "album": "Broadcast",
        }
    ]


async def test_flac_vorbis_comment_page_without_mapping_header_does_not_emit_metadata() -> None:
    """FLAC metadata pages without Ogg FLAC context should not emit metadata."""
    # Setup
    comments = {"TITLE": "Lossless Song", "ARTIST": "Studio Band", "ALBUM": "Broadcast"}
    chunks = [
        _build_bos_page(),
        _build_flac_vorbis_comment_page(comments),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == []


async def test_flac_vorbis_comment_page_emits_metadata_after_streaminfo_page() -> None:
    """FLAC Vorbis comment pages should still emit metadata after other header pages."""
    # Setup
    comments = {"TITLE": "Lossless Song", "ARTIST": "Studio Band", "ALBUM": "Broadcast"}
    chunks = [
        _build_bos_page(OGG_FLAC_MAPPING_PACKET),
        _build_flac_streaminfo_page(page_sequence=1),
        _build_flac_vorbis_comment_page(comments, page_sequence=2),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == [
        {
            "title": "Lossless Song",
            "artist": "Studio Band",
            "album": "Broadcast",
        }
    ]


async def test_flac_identification_pages_without_comments_do_not_emit_metadata() -> None:
    """Ogg FLAC mapping BOS pages and STREAMINFO pages should not emit metadata."""
    # Setup
    chunks = [
        _build_bos_page(OGG_FLAC_MAPPING_PACKET),
        _build_flac_streaminfo_page(),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == []


async def test_incomplete_flac_vorbis_comment_page_does_not_emit_metadata() -> None:
    """Incomplete FLAC Vorbis comment blocks should be skipped."""
    # Setup
    chunks = [
        _build_bos_page(OGG_FLAC_MAPPING_PACKET),
        _build_incomplete_flac_vorbis_comment_page(),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == []


async def test_flac_metadata_is_emitted_across_chained_streams() -> None:
    """FLAC metadata should be emitted again after an EOS/BOS chain boundary."""
    # Setup
    first_chain_comments = {"TITLE": "First Song", "ARTIST": "First Artist"}
    second_chain_comments = {"TITLE": "Second Song", "ARTIST": "Second Artist"}
    chunks = [
        _build_bos_page(OGG_FLAC_MAPPING_PACKET, page_sequence=0),
        _build_flac_vorbis_comment_page(first_chain_comments, page_sequence=1),
        _build_eos_page(page_sequence=2, granule_position=100),
        _build_bos_page(OGG_FLAC_MAPPING_PACKET, page_sequence=0),
        _build_flac_vorbis_comment_page(second_chain_comments, page_sequence=1),
    ]

    # Act
    metadata_updates = await _collect_metadata(chunks)

    # Assert
    assert metadata_updates == [
        {
            "title": "First Song",
            "artist": "First Artist",
        },
        {
            "title": "Second Song",
            "artist": "Second Artist",
        },
    ]
