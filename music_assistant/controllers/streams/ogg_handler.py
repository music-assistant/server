"""
Handler for chained OGG streams used in internet radio.

FFmpeg cannot handle chained OGG streams (multiple logical bitstreams). This module
stitches them into a single continuous stream by skipping EOS/BOS boundaries and
re-sequencing page numbers.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.constants import MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.ogg_handler")

# OGG constants
OGG_SYNC_PATTERN: bytes = b"OggS"
OGG_HEADER_SIZE: int = 27  # Fixed header size before segment table

# Header type flags (in header_type byte at offset 5)
OGG_FLAG_CONTINUATION: int = 0x01  # Continuation of previous page
OGG_FLAG_BOS: int = 0x02  # Beginning of stream
OGG_FLAG_EOS: int = 0x04  # End of stream

# Ogg FLAC constants
FLAC_METADATA_HEADER_SIZE: int = (
    4  # Size of FLAC metadata block header (1 byte type + 3 bytes length)
)
FLAC_METADATA_BLOCK_VORBIS_COMMENT: int = 4  # FLAC metadata block type for Vorbis comments


class OggPage:
    """Parsed OGG page with header fields and payload."""

    def __init__(
        self,
        raw_data: bytes,
        header_type: int,
        granule_position: int,
        serial_number: int,
        page_sequence: int,
        segment_data: bytes,
    ) -> None:
        """Initialize OGG page."""
        self.raw_data = raw_data
        self.header_type = header_type
        self.granule_position = granule_position
        self.serial_number = serial_number
        self.page_sequence = page_sequence
        self.segment_data = segment_data

    @property
    def is_bos(self) -> bool:
        """Return True if beginning of stream flag is set."""
        return bool(self.header_type & OGG_FLAG_BOS)

    @property
    def is_eos(self) -> bool:
        """Return True if end of stream flag is set."""
        return bool(self.header_type & OGG_FLAG_EOS)

    @property
    def is_continuation(self) -> bool:
        """Return True if continuation flag is set."""
        return bool(self.header_type & OGG_FLAG_CONTINUATION)

    @property
    def is_opus_head(self) -> bool:
        """Return True if page contains OpusHead header."""
        return self.segment_data.startswith(b"OpusHead")

    @property
    def is_opus_tags(self) -> bool:
        """Return True if page contains OpusTags header."""
        return self.segment_data.startswith(b"OpusTags")

    @property
    def is_vorbis_id(self) -> bool:
        """Return True if page contains Vorbis identification header."""
        return len(self.segment_data) > 7 and self.segment_data[0:7] == b"\x01vorbis"

    @property
    def is_vorbis_comment(self) -> bool:
        """Return True if page contains Vorbis comment header."""
        return len(self.segment_data) > 7 and self.segment_data[0:7] == b"\x03vorbis"

    @property
    def is_ogg_flac_mapping_page(self) -> bool:
        """Return True if page starts with the Ogg FLAC mapping header packet."""
        return len(self.segment_data) > 5 and self.segment_data[0:5] == b"\x7fFLAC"

    def is_header_page(self, is_ogg_flac_stream: bool = False) -> bool:
        """Return True if page is a header (not audio data)."""
        return (
            self.is_opus_head
            or self.is_opus_tags
            or self.is_vorbis_id
            or self.is_vorbis_comment
            or self.is_ogg_flac_mapping_page
            # In Ogg FLAC streams pages with granule position 0 contain header packets and not audio data
            or (is_ogg_flac_stream and self.granule_position == 0)
        )


def parse_ogg_page(data: bytes | bytearray, offset: int = 0) -> tuple[OggPage, int] | None:
    """Parse a single OGG page from buffer. Returns (OggPage, consumed) or None if incomplete."""
    if len(data) < offset + OGG_HEADER_SIZE:
        return None

    if data[offset : offset + 4] != OGG_SYNC_PATTERN:
        return None

    header_type = data[offset + 5]
    granule_position = struct.unpack_from("<Q", data, offset + 6)[0]
    serial_number = struct.unpack_from("<I", data, offset + 14)[0]
    page_sequence = struct.unpack_from("<I", data, offset + 18)[0]
    num_segments = data[offset + 26]

    header_size = OGG_HEADER_SIZE + num_segments
    if len(data) < offset + header_size:
        return None

    segment_table = data[offset + OGG_HEADER_SIZE : offset + header_size]
    segment_data_size = sum(segment_table)

    total_page_size = header_size + segment_data_size
    if len(data) < offset + total_page_size:
        return None

    segment_data = bytes(data[offset + header_size : offset + total_page_size])
    raw_data = bytes(data[offset : offset + total_page_size])

    page = OggPage(
        raw_data=raw_data,
        header_type=header_type,
        granule_position=granule_position,
        serial_number=serial_number,
        page_sequence=page_sequence,
        segment_data=segment_data,
    )

    return (page, offset + total_page_size)


_OGG_CRC_TABLE: list[int] = []


def _build_ogg_crc_table() -> list[int]:
    """Build OGG CRC32 lookup table (polynomial 0x04c11db7)."""
    table: list[int] = []
    poly = 0x04C11DB7
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


_OGG_CRC_TABLE = _build_ogg_crc_table()


def calculate_ogg_crc(data: bytes) -> int:
    """Calculate OGG CRC32 checksum for page data (with checksum field zeroed)."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _OGG_CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    return crc


def rewrite_ogg_page(
    page: OggPage,
    new_serial: int | None = None,
    new_sequence: int | None = None,
    new_granule: int | None = None,
    clear_bos: bool = False,
) -> bytes:
    """Rewrite an OGG page with modified header fields and recalculated CRC."""
    data = bytearray(page.raw_data)

    if clear_bos:
        data[5] = page.header_type & ~OGG_FLAG_BOS
    if new_granule is not None:
        struct.pack_into("<Q", data, 6, new_granule)
    if new_serial is not None:
        struct.pack_into("<I", data, 14, new_serial)
    if new_sequence is not None:
        struct.pack_into("<I", data, 18, new_sequence)

    data[22:26] = b"\x00\x00\x00\x00"
    crc = calculate_ogg_crc(bytes(data))
    struct.pack_into("<I", data, 22, crc)

    return bytes(data)


def parse_vorbis_comments(data: bytes) -> dict[str, str]:
    """Parse Vorbis comments structure. Data should exclude magic header bytes."""
    comments: dict[str, str] = {}
    try:
        offset = 0
        if len(data) < 4:
            return comments
        vendor_length = struct.unpack_from("<I", data, offset)[0]
        offset += 4 + vendor_length

        if len(data) < offset + 4:
            return comments
        num_comments = struct.unpack_from("<I", data, offset)[0]
        offset += 4

        for _ in range(num_comments):
            if len(data) < offset + 4:
                break
            comment_length = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            if len(data) < offset + comment_length:
                break
            comment_bytes = data[offset : offset + comment_length]
            offset += comment_length
            try:
                comment_str = comment_bytes.decode("utf-8")
                if "=" in comment_str:
                    key, value = comment_str.split("=", 1)
                    comments[key.lower()] = value
            except UnicodeDecodeError:
                continue
    except struct.error, IndexError:
        pass
    return comments


def parse_flac_vorbis_comment_block(data: bytes) -> dict[str, str]:
    """Parse Vorbis comments from a native FLAC metadata block."""
    if len(data) < FLAC_METADATA_HEADER_SIZE:
        return {}
    block_length = int.from_bytes(data[1:4], byteorder="big")
    if len(data) < FLAC_METADATA_HEADER_SIZE + block_length:
        LOGGER.debug(
            "Skipping FLAC Vorbis comment block spanning multiple OGG pages: "
            "need %d bytes, have %d",
            FLAC_METADATA_HEADER_SIZE + block_length,
            len(data),
        )
        return {}
    comment_data = data[FLAC_METADATA_HEADER_SIZE : FLAC_METADATA_HEADER_SIZE + block_length]
    return parse_vorbis_comments(comment_data)


def _is_flac_vorbis_comment_block(data: bytes) -> bool:
    """Return True if Ogg FLAC packet data contains a FLAC Vorbis comment block."""
    if len(data) < FLAC_METADATA_HEADER_SIZE:
        return False
    block_type = data[0] & 0x7F
    return block_type == FLAC_METADATA_BLOCK_VORBIS_COMMENT


def extract_metadata_from_page(
    page: OggPage, is_ogg_flac_stream: bool = False
) -> dict[str, str] | None:
    """Extract metadata from page if it contains supported comment metadata."""
    if page.is_opus_tags:
        return parse_vorbis_comments(page.segment_data[8:])
    if page.is_vorbis_comment:
        return parse_vorbis_comments(page.segment_data[7:])
    if is_ogg_flac_stream and _is_flac_vorbis_comment_block(page.segment_data):
        return parse_flac_vorbis_comment_block(page.segment_data)
    return None


class _ChainedOggState:
    """State machine for stitching chained OGG streams."""

    def __init__(self, metadata_callback: Callable[[dict[str, str]], Any] | None = None) -> None:
        self.metadata_callback = metadata_callback
        self.output_serial: int | None = None
        self.output_sequence: int = 0
        self.first_chain: bool = True
        self.seen_eos: bool = False
        self.is_ogg_flac_chain: bool = False
        self.header_pages_sent: int = 0
        self.last_granule: int = 0
        self.granule_offset: int = 0

    def process_page(self, page: OggPage) -> bytes | None:
        """Process page. Returns data to yield or None to skip."""
        if self.first_chain:
            return self._process_first_chain_page(page)
        return self._process_chain_page(page)

    def _handle_metadata(self, page: OggPage) -> None:
        """Extract and invoke callback for supported in-band metadata pages."""
        if self.metadata_callback:
            metadata = extract_metadata_from_page(page, is_ogg_flac_stream=self.is_ogg_flac_chain)
            if metadata:
                LOGGER.debug("Extracted metadata: %s", metadata)
                self.metadata_callback(metadata)

    def _process_first_chain_page(self, page: OggPage) -> bytes | None:
        """Process page from first chain. Returns data to yield or None to skip."""
        if page.is_bos:
            self.output_serial = page.serial_number
            LOGGER.debug("First chain BOS, serial=%d", self.output_serial)
            if page.is_ogg_flac_mapping_page:
                self.is_ogg_flac_chain = True
            self.output_sequence = page.page_sequence
            self.header_pages_sent = 1
            return page.raw_data

        # Ogg FLAC chains can carry additional header pages after the mapping header,
        # so do not stop at the two-page limit used for Opus/Vorbis setup headers.
        if page.is_header_page(self.is_ogg_flac_chain) and (
            self.header_pages_sent < 2 or self.is_ogg_flac_chain
        ):
            LOGGER.debug("First chain header page %d", self.header_pages_sent)
            self._handle_metadata(page)
            self.output_sequence = page.page_sequence
            self.header_pages_sent += 1
            return page.raw_data

        if page.granule_position != 0xFFFFFFFFFFFFFFFF:
            self.last_granule = page.granule_position

        if page.is_eos:
            # Skip EOS - FFmpeg cannot handle them
            LOGGER.debug(
                "First chain EOS at seq %d, granule %d (skipping)",
                page.page_sequence,
                self.last_granule,
            )
            self.granule_offset = self.last_granule
            self.first_chain = False
            self.seen_eos = True
            self.is_ogg_flac_chain = False
            return None

        self.output_sequence += 1
        if page.page_sequence != self.output_sequence:
            return rewrite_ogg_page(page, new_sequence=self.output_sequence)
        return page.raw_data

    def _process_chain_page(self, page: OggPage) -> bytes | None:
        """Process page from subsequent chains. Returns data to yield or None to skip."""
        if self.seen_eos and page.is_bos:
            LOGGER.debug("New chain BOS, serial=%d (skipping)", page.serial_number)
            self.seen_eos = False
            self.is_ogg_flac_chain = False
            if page.is_ogg_flac_mapping_page:
                self.is_ogg_flac_chain = True
            return None

        if page.is_header_page(self.is_ogg_flac_chain):
            LOGGER.debug("Chain header page (skipping)")
            self._handle_metadata(page)
            return None

        if page.granule_position != 0xFFFFFFFFFFFFFFFF:
            self.last_granule = page.granule_position + self.granule_offset

        if page.is_eos:
            # Skip EOS - FFmpeg cannot handle them
            LOGGER.debug(
                "Chain EOS at seq %d, new offset %d (skipping)",
                page.page_sequence,
                self.last_granule,
            )
            self.granule_offset = self.last_granule
            self.seen_eos = True
            return None

        new_granule: int | None = None
        if page.granule_position != 0xFFFFFFFFFFFFFFFF:
            new_granule = page.granule_position + self.granule_offset

        self.output_sequence += 1
        return rewrite_ogg_page(
            page,
            new_serial=self.output_serial,
            new_sequence=self.output_sequence,
            new_granule=new_granule,
            clear_bos=page.is_bos,
        )


def _resync_ogg_buffer(buffer: bytearray) -> int:
    """Find next OGG sync pattern, returning bytes to skip (0 if already synced)."""
    idx = buffer.find(OGG_SYNC_PATTERN, 1)
    if idx > 0:
        LOGGER.warning("Skipping %d bytes of corrupted OGG data", idx)
        return idx
    return 0


_MAX_BUFFER_SIZE = 65536


async def get_chained_ogg_stream(
    mass: MusicAssistant,
    url: str,
    metadata_callback: Callable[[dict[str, str]], Any] | None = None,
) -> AsyncGenerator[bytes]:
    """
    Yield continuous OGG data from a chained stream, stitching chain boundaries.

    :param mass: MusicAssistant instance.
    :param url: URL of the OGG radio stream.
    :param metadata_callback: Optional callback invoked on metadata changes.
    """
    state = _ChainedOggState(metadata_callback)
    buffer = bytearray()

    LOGGER.debug("Starting chained OGG stream handler for %s", url)

    try:
        async for chunk in mass.streams.audio.get_reconnecting_radio_stream(url):
            buffer.extend(chunk)

            while True:
                result = parse_ogg_page(buffer, 0)
                if result is None:
                    if len(buffer) > _MAX_BUFFER_SIZE:
                        skip = _resync_ogg_buffer(buffer)
                        if skip > 0:
                            buffer = buffer[skip:]
                            continue
                        discard = len(buffer) // 2
                        LOGGER.warning("Buffer overflow, discarding %d bytes", discard)
                        buffer = buffer[discard:]
                    break

                page, consumed = result
                buffer = buffer[consumed:]

                output = state.process_page(page)
                if output is not None:
                    yield output
    except aiohttp.ClientError as err:
        raise ProviderUnavailableError(f"Failed to fetch OGG stream: {err}") from err

    LOGGER.debug("Chained OGG stream handler ended for %s", url)
