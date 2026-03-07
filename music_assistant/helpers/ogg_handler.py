"""OGG stream handler for chained OGG streams (radio broadcasts).

This module provides support for chained OGG streams commonly used in internet radio.
FFmpeg's OGG demuxer cannot handle chained streams (it treats the EOS + new headers
as an error and exits). This handler stitches chained OGG streams into a single
continuous stream that FFmpeg can decode.

Background:
- Chained OGG streams have multiple "logical bitstreams" concatenated together
- Each track/segment in a radio stream is a separate logical bitstream
- Each logical bitstream starts with BOS (beginning of stream) page containing headers
- Each logical bitstream ends with EOS (end of stream) marker
- VLC handles this correctly (see modules/demux/ogg.c), FFmpeg does not

Solution:
- Parse incoming OGG pages from the HTTP stream
- Forward the first logical bitstream's headers (OpusHead, OpusTags)
- On chain boundaries (EOS followed by BOS), skip the new headers
- Re-sequence page numbers to be continuous for FFmpeg
- Optionally extract metadata from new OpusTags headers for display
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


class OggPage:
    """Represents a parsed OGG page."""

    def __init__(
        self,
        raw_data: bytes,
        header_type: int,
        granule_position: int,
        serial_number: int,
        page_sequence: int,
        segment_data: bytes,
    ) -> None:
        """Initialize OGG page.

        :param raw_data: The complete raw page data.
        :param header_type: Header type flags (BOS, EOS, continuation).
        :param granule_position: Granule position (timestamp).
        :param serial_number: Stream serial number.
        :param page_sequence: Page sequence number within the stream.
        :param segment_data: The actual payload data.
        """
        self.raw_data = raw_data
        self.header_type = header_type
        self.granule_position = granule_position
        self.serial_number = serial_number
        self.page_sequence = page_sequence
        self.segment_data = segment_data

    @property
    def is_bos(self) -> bool:
        """Check if this is a beginning of stream page."""
        return bool(self.header_type & OGG_FLAG_BOS)

    @property
    def is_eos(self) -> bool:
        """Check if this is an end of stream page."""
        return bool(self.header_type & OGG_FLAG_EOS)

    @property
    def is_continuation(self) -> bool:
        """Check if this is a continuation page."""
        return bool(self.header_type & OGG_FLAG_CONTINUATION)

    @property
    def is_opus_head(self) -> bool:
        """Check if this page contains OpusHead header."""
        return self.segment_data.startswith(b"OpusHead")

    @property
    def is_opus_tags(self) -> bool:
        """Check if this page contains OpusTags (metadata)."""
        return self.segment_data.startswith(b"OpusTags")

    @property
    def is_vorbis_id(self) -> bool:
        """Check if this page contains Vorbis identification header."""
        return len(self.segment_data) > 7 and self.segment_data[0:7] == b"\x01vorbis"

    @property
    def is_vorbis_comment(self) -> bool:
        """Check if this page contains Vorbis comment header."""
        return len(self.segment_data) > 7 and self.segment_data[0:7] == b"\x03vorbis"

    @property
    def is_header_page(self) -> bool:
        """Check if this is a header page (not audio data)."""
        return self.is_opus_head or self.is_opus_tags or self.is_vorbis_id or self.is_vorbis_comment


def parse_ogg_page(data: bytes | bytearray, offset: int = 0) -> tuple[OggPage, int] | None:
    """Parse a single OGG page from the data buffer.

    :param data: Buffer containing OGG stream data (bytes or bytearray).
    :param offset: Offset in the buffer to start parsing from.
    :return: Tuple of (OggPage, new_offset) or None if incomplete data.
    """
    if len(data) < offset + OGG_HEADER_SIZE:
        return None

    # Check sync pattern
    if data[offset : offset + 4] != OGG_SYNC_PATTERN:
        return None

    # Parse header fields
    # Byte 4: version (must be 0)
    # Byte 5: header_type
    # Bytes 6-13: granule_position (8 bytes, little-endian)
    # Bytes 14-17: serial_number (4 bytes, little-endian)
    # Bytes 18-21: page_sequence (4 bytes, little-endian)
    # Bytes 22-25: checksum (4 bytes)
    # Byte 26: num_segments

    header_type = data[offset + 5]
    granule_position = struct.unpack_from("<Q", data, offset + 6)[0]
    serial_number = struct.unpack_from("<I", data, offset + 14)[0]
    page_sequence = struct.unpack_from("<I", data, offset + 18)[0]
    num_segments = data[offset + 26]

    header_size = OGG_HEADER_SIZE + num_segments
    if len(data) < offset + header_size:
        return None

    # Parse segment table to get total segment data size
    segment_table = data[offset + OGG_HEADER_SIZE : offset + header_size]
    segment_data_size = sum(segment_table)

    total_page_size = header_size + segment_data_size
    if len(data) < offset + total_page_size:
        return None

    segment_data = data[offset + header_size : offset + total_page_size]
    raw_data = data[offset : offset + total_page_size]

    page = OggPage(
        raw_data=raw_data,
        header_type=header_type,
        granule_position=granule_position,
        serial_number=serial_number,
        page_sequence=page_sequence,
        segment_data=segment_data,
    )

    return (page, offset + total_page_size)


# Pre-computed OGG CRC32 lookup table (polynomial 0x04c11db7, non-reflected)
# Generated once at module load time for efficiency
_OGG_CRC_TABLE: list[int] = []


def _build_ogg_crc_table() -> list[int]:
    """Build the OGG CRC32 lookup table."""
    table: list[int] = []
    poly = 0x04C11DB7
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


# Initialize table at module load
_OGG_CRC_TABLE = _build_ogg_crc_table()


def calculate_ogg_crc(data: bytes) -> int:
    """Calculate OGG CRC32 checksum.

    OGG uses a non-standard CRC32 polynomial (0x04c11db7), non-reflected.

    :param data: Data to calculate CRC for (with checksum field zeroed).
    :return: CRC32 value.
    """
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
    """Rewrite an OGG page with modified header fields.

    :param page: The original OGG page.
    :param new_serial: New serial number (None to keep original).
    :param new_sequence: New page sequence number (None to keep original).
    :param new_granule: New granule position (None to keep original).
    :param clear_bos: Clear the BOS flag.
    :return: New raw page data with updated CRC.
    """
    # Start with a copy of the raw data
    data = bytearray(page.raw_data)

    # Modify header type if needed
    if clear_bos:
        header_type = page.header_type
        header_type &= ~OGG_FLAG_BOS
        data[5] = header_type

    # Modify granule position if specified
    if new_granule is not None:
        struct.pack_into("<Q", data, 6, new_granule)

    # Modify serial number if specified
    if new_serial is not None:
        struct.pack_into("<I", data, 14, new_serial)

    # Modify sequence number if specified
    if new_sequence is not None:
        struct.pack_into("<I", data, 18, new_sequence)

    # Zero checksum field before calculating new CRC
    data[22:26] = b"\x00\x00\x00\x00"

    # Calculate and insert new CRC
    crc = calculate_ogg_crc(bytes(data))
    struct.pack_into("<I", data, 22, crc)

    return bytes(data)


def parse_vorbis_comments(data: bytes) -> dict[str, str]:
    """Parse Vorbis comments from OpusTags or Vorbis comment header.

    :param data: Comment data (after the magic header bytes).
    :return: Dictionary of metadata key-value pairs.
    """
    comments: dict[str, str] = {}

    try:
        offset = 0

        # Vendor string length (4 bytes, little-endian)
        if len(data) < offset + 4:
            return comments
        vendor_length = struct.unpack_from("<I", data, offset)[0]
        offset += 4 + vendor_length

        # Number of comments (4 bytes, little-endian)
        if len(data) < offset + 4:
            return comments
        num_comments = struct.unpack_from("<I", data, offset)[0]
        offset += 4

        # Parse each comment
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

    except (struct.error, IndexError):
        pass

    return comments


def extract_metadata_from_page(page: OggPage) -> dict[str, str] | None:
    """Extract metadata from an OGG page if it contains OpusTags or Vorbis comments.

    :param page: The OGG page to extract metadata from.
    :return: Dictionary of metadata or None if not a metadata page.
    """
    if page.is_opus_tags:
        # OpusTags: skip "OpusTags" (8 bytes)
        return parse_vorbis_comments(page.segment_data[8:])
    if page.is_vorbis_comment:
        # Vorbis comment: skip 0x03 + "vorbis" (7 bytes)
        return parse_vorbis_comments(page.segment_data[7:])
    return None


class _ChainedOggState:
    """State machine for processing chained OGG streams."""

    def __init__(self, metadata_callback: Callable[[dict[str, str]], Any] | None = None) -> None:
        """Initialize the chained OGG state."""
        self.metadata_callback = metadata_callback
        self.output_serial: int | None = None
        self.output_sequence: int = 0
        self.first_chain: bool = True
        self.seen_eos: bool = False
        self.header_pages_sent: int = 0
        self.last_granule: int = 0
        self.granule_offset: int = 0

    def _handle_metadata(self, page: OggPage) -> None:
        """Extract and callback metadata if page contains OpusTags."""
        if self.metadata_callback and page.is_opus_tags:
            metadata = extract_metadata_from_page(page)
            if metadata:
                LOGGER.debug("Extracted metadata: %s", metadata)
                self.metadata_callback(metadata)

    def _process_first_chain_page(self, page: OggPage) -> bytes | None:
        """Process a page from the first logical bitstream. Returns bytes to yield or None."""
        if page.is_bos:
            self.output_serial = page.serial_number
            LOGGER.debug("First chain BOS, serial=%d", self.output_serial)
            self.output_sequence = page.page_sequence
            self.header_pages_sent = 1
            return page.raw_data

        if page.is_header_page and self.header_pages_sent < 2:
            LOGGER.debug("First chain header page %d", self.header_pages_sent)
            self._handle_metadata(page)
            self.output_sequence = page.page_sequence
            self.header_pages_sent += 1
            return page.raw_data

        # Track granule position for timestamp continuity
        if page.granule_position != 0xFFFFFFFFFFFFFFFF:
            self.last_granule = page.granule_position

        if page.is_eos:
            # Skip EOS pages entirely - FFmpeg cannot handle them even with flag cleared
            LOGGER.debug(
                "First chain EOS at seq %d, granule %d (skipping)",
                page.page_sequence,
                self.last_granule,
            )
            self.granule_offset = self.last_granule
            self.first_chain = False
            self.seen_eos = True
            return None

        self.output_sequence += 1
        if page.page_sequence != self.output_sequence:
            return rewrite_ogg_page(page, new_sequence=self.output_sequence)
        return page.raw_data

    def _process_chain_page(self, page: OggPage) -> bytes | None:
        """Process a page from subsequent chains. Returns bytes to yield or None to skip."""
        if self.seen_eos and page.is_bos:
            LOGGER.debug("New chain BOS, serial=%d (skipping)", page.serial_number)
            self.seen_eos = False
            return None

        if page.is_header_page:
            LOGGER.debug("Chain header page (skipping)")
            self._handle_metadata(page)
            return None

        # Track granule position for timestamp continuity
        if page.granule_position != 0xFFFFFFFFFFFFFFFF:
            self.last_granule = page.granule_position + self.granule_offset

        if page.is_eos:
            # Skip EOS pages entirely - FFmpeg cannot handle them even with flag cleared
            # Update granule offset for the next chain
            LOGGER.debug(
                "Chain EOS at seq %d, new offset %d (skipping)",
                page.page_sequence,
                self.last_granule,
            )
            self.granule_offset = self.last_granule
            self.seen_eos = True
            return None

        # Calculate adjusted granule position for continuous timestamps
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

    def process_page(self, page: OggPage) -> bytes | None:
        """Process a single OGG page. Returns bytes to yield or None to skip."""
        if self.first_chain:
            return self._process_first_chain_page(page)
        return self._process_chain_page(page)


def _resync_ogg_buffer(buffer: bytearray) -> int:
    """Find the next OGG sync pattern in buffer, skipping corrupted data.

    :param buffer: Buffer to search in.
    :return: Number of bytes to skip to reach next sync pattern, or 0 if at sync.
    """
    # Look for next OggS marker after position 0
    idx = buffer.find(OGG_SYNC_PATTERN, 1)
    if idx > 0:
        LOGGER.warning("Skipping %d bytes of corrupted OGG data", idx)
        return idx
    return 0


# Maximum buffer size before forcing resync (64KB)
_MAX_BUFFER_SIZE = 65536


async def get_chained_ogg_stream(
    mass: MusicAssistant,
    url: str,
    metadata_callback: Callable[[dict[str, str]], Any] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Get a continuous OGG stream from a chained OGG radio source.

    This generator handles chained OGG streams by stitching multiple logical
    bitstreams into a single continuous stream that FFmpeg can decode.

    :param mass: MusicAssistant instance.
    :param url: URL of the OGG radio stream.
    :param metadata_callback: Optional callback for metadata changes.
    """
    # Import here to avoid circular dependency (audio.py imports from this module)
    from music_assistant.helpers.audio import get_reconnecting_radio_stream  # noqa: PLC0415

    state = _ChainedOggState(metadata_callback)
    buffer = bytearray()

    LOGGER.debug("Starting chained OGG stream handler for %s", url)

    try:
        async for chunk in get_reconnecting_radio_stream(mass, url):
            buffer.extend(chunk)

            while True:
                result = parse_ogg_page(buffer, 0)
                if result is None:
                    # Check if buffer is growing too large (corrupted data)
                    if len(buffer) > _MAX_BUFFER_SIZE:
                        skip = _resync_ogg_buffer(buffer)
                        if skip > 0:
                            buffer = buffer[skip:]
                            continue
                        # No sync found, discard half the buffer
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
