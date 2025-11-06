"""Metadata reader for shairport-sync metadata pipe."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import struct
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant.constants import VERBOSE_LOG_LEVEL

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger


class MetadataReader:
    """Read and parse metadata from shairport-sync metadata pipe."""

    def __init__(
        self,
        metadata_pipe: str,
        logger: Logger,
        on_metadata: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Initialize metadata reader.

        :param metadata_pipe: Path to the metadata pipe.
        :param logger: Logger instance.
        :param on_metadata: Callback function for metadata updates.
        """
        self.metadata_pipe = metadata_pipe
        self.logger = logger
        self.on_metadata = on_metadata
        self._reader_task: asyncio.Task[None] | None = None
        self._stop = False
        self._current_metadata: dict[str, Any] = {}
        self._fd: int | None = None
        self._buffer = ""
        self.cover_art_bytes: bytes | None = None

    async def start(self) -> None:
        """Start reading metadata from the pipe."""
        self._stop = False
        self._reader_task = asyncio.create_task(self._read_metadata())

    async def stop(self) -> None:
        """Stop reading metadata."""
        self._stop = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task

    async def _read_metadata(self) -> None:
        """Read metadata from the pipe using async file descriptor."""
        loop = asyncio.get_event_loop()
        try:
            # Open the metadata pipe in non-blocking mode
            # Use O_RDONLY | O_NONBLOCK to avoid blocking on open
            self._fd = await loop.run_in_executor(
                None, os.open, self.metadata_pipe, os.O_RDONLY | os.O_NONBLOCK
            )

            # Create an asyncio.Event to signal when data is available
            data_available = asyncio.Event()

            def on_readable() -> None:
                """Set data available flag when file descriptor is readable."""
                data_available.set()

            # Register the file descriptor with the event loop
            loop.add_reader(self._fd, on_readable)

            try:
                while not self._stop:
                    # Wait for data to be available
                    await data_available.wait()
                    data_available.clear()

                    # Read available data from the pipe
                    try:
                        chunk = os.read(self._fd, 4096)
                        if chunk:
                            # Decode as text and add to buffer
                            self._buffer += chunk.decode("utf-8", errors="ignore")
                            # Process all complete metadata items in the buffer
                            self._process_buffer()
                    except BlockingIOError:
                        # No data available right now, wait for next notification
                        continue
                    except OSError as err:
                        self.logger.debug("Error reading from pipe: %s", err)
                        await asyncio.sleep(0.1)

            finally:
                # Remove the reader callback
                loop.remove_reader(self._fd)

        except Exception as err:
            self.logger.error("Error reading metadata pipe: %s", err)
        finally:
            if self._fd is not None:
                with suppress(OSError):
                    os.close(self._fd)
                self._fd = None

    def _process_buffer(self) -> None:
        """Process all complete metadata items in the buffer (XML format or plain text markers)."""
        # First, check for plain text markers from sessioncontrol hooks
        while "\n" in self._buffer:
            # Check if we have a complete line before any XML
            line_end = self._buffer.index("\n")
            if "<item>" not in self._buffer or self._buffer.index("<item>") > line_end:
                # We have a plain text line before any XML
                line = self._buffer[:line_end].strip()
                self._buffer = self._buffer[line_end + 1 :]

                # Handle our custom markers
                if line == "MA_PLAY_BEGIN":
                    self.logger.info("Playback started (via sessioncontrol hook)")
                    if self.on_metadata:
                        self.on_metadata({"play_state": "playing"})
                elif line == "MA_PLAY_END":
                    self.logger.info("Playback ended (via sessioncontrol hook)")
                    if self.on_metadata:
                        self.on_metadata({"play_state": "stopped"})
                # Ignore other plain text lines
            else:
                # XML item comes first, stop looking for lines
                break

        # Look for complete <item>...</item> blocks
        while "<item>" in self._buffer and "</item>" in self._buffer:
            try:
                # Find the boundaries of the next item
                start_idx = self._buffer.index("<item>")
                end_idx = self._buffer.index("</item>") + len("</item>")

                # Extract the item
                item_xml = self._buffer[start_idx:end_idx]

                # Remove processed item from buffer
                self._buffer = self._buffer[end_idx:]

                # Parse the item
                self._parse_xml_item(item_xml)

            except (ValueError, IndexError) as err:
                self.logger.debug("Error processing buffer: %s", err)
                # Clear malformed data
                if "</item>" in self._buffer:
                    # Skip to after the next </item>
                    self._buffer = self._buffer[self._buffer.index("</item>") + len("</item>") :]
                else:
                    # Wait for more data
                    break
            except Exception as err:
                self.logger.error("Unexpected error processing buffer: %s", err)
                # Clear the buffer on unexpected error
                self._buffer = ""
                break

    def _parse_xml_item(self, item_xml: str) -> None:
        """Parse a single XML metadata item.

        :param item_xml: XML string containing a metadata item.
        """
        try:
            # Extract type (hex format)
            type_match = re.search(r"<type>([0-9a-fA-F]{8})</type>", item_xml)
            code_match = re.search(r"<code>([0-9a-fA-F]{8})</code>", item_xml)
            length_match = re.search(r"<length>(\d+)</length>", item_xml)

            if not type_match or not code_match or not length_match:
                return

            # Convert hex type and code to ASCII strings
            type_hex = int(type_match.group(1), 16)
            code_hex = int(code_match.group(1), 16)
            length = int(length_match.group(1))

            # Convert hex to 4-character ASCII codes
            type_str = type_hex.to_bytes(4, "big").decode("ascii", errors="ignore")
            code_str = code_hex.to_bytes(4, "big").decode("ascii", errors="ignore")

            # Extract data if present
            data: str | bytes | None = None
            if length > 0:
                data_match = re.search(r"<data encoding=\"base64\">([^<]+)</data>", item_xml)
                if data_match:
                    try:
                        # Decode base64 data
                        data_b64 = data_match.group(1).strip()
                        decoded_data = base64.b64decode(data_b64)

                        # For binary fields (PICT, astm), keep as raw bytes
                        # For text fields, decode to UTF-8
                        if code_str in ("PICT", "astm"):
                            # Cover art and duration: keep as raw bytes
                            data = decoded_data
                        else:
                            # Text metadata: decode to UTF-8
                            data = decoded_data.decode("utf-8", errors="ignore")
                    except Exception as err:
                        self.logger.debug("Error decoding base64 data: %s", err)

            # Process the metadata item
            asyncio.create_task(self._process_metadata_item(type_str, code_str, data))

        except Exception as err:
            self.logger.debug("Error parsing XML item: %s", err)

    async def _process_metadata_item(
        self, item_type: str, code: str, data: str | bytes | None
    ) -> None:
        """Process a metadata item and update current metadata.

        :param item_type: Type of metadata (e.g., 'core' or 'ssnc').
        :param code: Metadata code identifier.
        :param data: Optional metadata data (string, bytes, or None).
        """
        # Don't log binary data (like cover art)
        if code == "PICT":
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Metadata: type=%s, code=%s, data=<binary image data>",
                item_type,
                code,
            )
        else:
            self.logger.log(
                VERBOSE_LOG_LEVEL, "Metadata: type=%s, code=%s, data=%s", item_type, code, data
            )

        # Handle metadata start/end markers
        if item_type == "ssnc" and code == "mdst":
            self._current_metadata = {}
            # Note: We don't clear cover_art_bytes here because:
            # 1. Cover art may arrive before mdst (at playback start)
            # 2. New cover art will overwrite old bytes when it arrives
            # 3. Cache-busting timestamp ensures browser gets correct image
            if self.on_metadata:
                self.on_metadata({"metadata_start": True})
            return

        if item_type == "ssnc" and code == "mden":
            if self.on_metadata and self._current_metadata:
                self.on_metadata(dict(self._current_metadata))
            return

        # Parse core metadata (from iTunes/iOS)
        if item_type == "core" and data is not None:
            self._parse_core_metadata(code, data)

        # Parse shairport-sync metadata
        if item_type == "ssnc" and data is not None:
            self._parse_ssnc_metadata(code, data)

    def _parse_core_metadata(self, code: str, data: str | bytes) -> None:
        """Parse core metadata from iTunes/iOS.

        :param code: Metadata code identifier.
        :param data: Metadata data.
        """
        # Text metadata fields - expect string data
        if isinstance(data, str):
            if code == "asar":  # Artist
                self._current_metadata["artist"] = data
            elif code == "asal":  # Album
                self._current_metadata["album"] = data
            elif code == "minm":  # Title
                self._current_metadata["title"] = data

        # Binary metadata fields - expect bytes data
        elif isinstance(data, bytes):
            if code == "PICT":  # Cover art (raw bytes)
                # Store raw bytes for later retrieval via resolve_image
                self.cover_art_bytes = data
                self.logger.debug("Stored cover art: %d bytes", len(data))
                # Signal that cover art is available with timestamp for cache-busting
                timestamp = str(int(time.time() * 1000))
                self._current_metadata["cover_art_timestamp"] = timestamp
                # Send cover art update immediately (cover art often arrives in separate block)
                if self.on_metadata:
                    self.on_metadata({"cover_art_timestamp": timestamp})
            elif code == "astm":  # Track duration in milliseconds (stored as 32-bit big-endian int)
                try:
                    # Duration is sent as 4-byte big-endian integer
                    if len(data) >= 4:
                        duration_ms = struct.unpack(">I", data[:4])[0]
                        self._current_metadata["duration"] = duration_ms // 1000
                except (ValueError, TypeError, struct.error) as err:
                    self.logger.debug("Error parsing duration: %s", err)

    def _parse_ssnc_metadata(self, code: str, data: str | bytes) -> None:
        """Parse shairport-sync metadata.

        :param code: Metadata code identifier.
        :param data: Metadata data.
        """
        # Handle binary data (cover art can come as ssnc type)
        if isinstance(data, bytes):
            if code == "PICT":  # Cover art (raw bytes)
                # Store raw bytes for later retrieval via resolve_image
                self.cover_art_bytes = data
                self.logger.debug("Stored cover art: %d bytes", len(data))
                # Signal that cover art is available with timestamp for cache-busting
                timestamp = str(int(time.time() * 1000))
                self._current_metadata["cover_art_timestamp"] = timestamp
                # Send cover art update immediately (cover art often arrives in separate block)
                if self.on_metadata:
                    self.on_metadata({"cover_art_timestamp": timestamp})
            return

        # Process string data for ssnc metadata (volume/progress are text-based)
        if code == "pvol":  # Volume
            self._parse_volume(data)
            # Send volume updates immediately (not batched with mden)
            if self.on_metadata and "volume" in self._current_metadata:
                self.on_metadata({"volume": self._current_metadata["volume"]})
        elif code == "prgr":  # Progress
            self._parse_progress(data)
            # Send progress updates immediately (not batched with mden)
            if self.on_metadata and "elapsed_time" in self._current_metadata:
                self.on_metadata({"elapsed_time": self._current_metadata["elapsed_time"]})
        elif code == "paus":  # Paused
            self._current_metadata["paused"] = True
        elif code == "prsm":  # Playing/resumed
            self._current_metadata["paused"] = False

    def _parse_volume(self, data: str) -> None:
        """Parse volume metadata from shairport-sync.

        Format: airplay_volume,min_volume,max_volume,mute
        AirPlay volume is in dB, typically ranging from -30.0 (silent) to 0.0 (max).
        Special value -144.0 means muted.

        :param data: Volume data string (e.g., "-21.88,0.00,0.00,0.00").
        """
        try:
            parts = data.split(",")
            if len(parts) >= 1:
                airplay_volume = float(parts[0])
                # -144.0 means muted
                if airplay_volume <= -144.0:
                    volume_percent = 0
                else:
                    # Convert dB to percentage: -30dB = 0%, 0dB = 100%
                    volume_percent = int(((airplay_volume + 30.0) / 30.0) * 100)
                    volume_percent = max(0, min(100, volume_percent))
                self._current_metadata["volume"] = volume_percent
        except (ValueError, IndexError) as err:
            self.logger.debug("Error parsing volume: %s", err)

    def _parse_progress(self, data: str) -> None:
        """Parse progress metadata.

        :param data: Progress data string.
        """
        try:
            parts = data.split("/")
            if len(parts) >= 3:
                start_rtp = int(parts[0])
                current_rtp = int(parts[1])
                elapsed_frames = current_rtp - start_rtp
                elapsed_seconds = elapsed_frames / 44100
                self._current_metadata["elapsed_time"] = int(elapsed_seconds)
        except (ValueError, IndexError) as err:
            self.logger.debug("Error parsing progress: %s", err)
