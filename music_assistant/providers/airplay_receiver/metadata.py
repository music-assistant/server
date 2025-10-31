"""Metadata reader for shairport-sync metadata pipe."""

from __future__ import annotations

import asyncio
import base64
import os
import struct
from contextlib import suppress
from typing import TYPE_CHECKING, Any

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
        """Initialize metadata reader."""
        self.metadata_pipe = metadata_pipe
        self.logger = logger
        self.on_metadata = on_metadata
        self._reader_task: asyncio.Task[None] | None = None
        self._stop = False
        self._current_metadata: dict[str, Any] = {}
        self._fd: int | None = None
        self._buffer = bytearray()

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
                            self._buffer.extend(chunk)
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
        """Process all complete metadata items in the buffer."""
        while len(self._buffer) >= 12:  # Minimum header size (type + code + length)
            try:
                # Read header (12 bytes: 4 for type, 4 for code, 4 for length)
                type_bytes = bytes(self._buffer[0:4])
                code_bytes = bytes(self._buffer[4:8])
                length_bytes = bytes(self._buffer[8:12])

                # Unpack length
                length = struct.unpack(">I", length_bytes)[0]

                # Check if we have the complete item (header + data)
                total_size = 12 + length
                if len(self._buffer) < total_size:
                    # Not enough data yet, wait for more
                    break

                # Extract data if present
                data: str | None = None
                if length > 0:
                    data_bytes = bytes(self._buffer[12:total_size])
                    # Data is base64 encoded
                    try:
                        data = base64.b64decode(data_bytes).decode("utf-8")
                    except Exception:
                        # If decoding fails, store raw bytes as string representation
                        data = str(data_bytes)

                # Convert type and code to strings
                type_str = type_bytes.decode("latin-1")
                code_str = code_bytes.decode("latin-1")

                # Remove processed item from buffer
                del self._buffer[:total_size]

                # Process the metadata item (schedule as task to avoid blocking)
                asyncio.create_task(self._process_metadata_item(type_str, code_str, data))

            except Exception as err:
                self.logger.debug("Error processing buffer: %s", err)
                # Clear the buffer on error to avoid getting stuck
                self._buffer.clear()
                break

    async def _process_metadata_item(self, item_type: str, code: str, data: str | None) -> None:
        """Process a metadata item and update current metadata."""
        self.logger.debug("Metadata: type=%s, code=%s, data=%s", item_type, code, data)

        # Handle metadata start/end markers
        if item_type == "ssnc" and code == "mdst":
            # Metadata sequence start
            self._current_metadata = {}
            return

        if item_type == "ssnc" and code == "mden":
            # Metadata sequence end - trigger callback
            if self.on_metadata and self._current_metadata:
                self.on_metadata(dict(self._current_metadata))
            return

        # Parse core metadata (from iTunes/iOS)
        if item_type == "core" and data:
            if code == "asar":  # Artist
                self._current_metadata["artist"] = data
            elif code == "asal":  # Album
                self._current_metadata["album"] = data
            elif code == "minm":  # Title
                self._current_metadata["title"] = data
            elif code == "PICT":  # Cover art
                self._current_metadata["cover_art"] = data

        # Parse shairport-sync metadata
        if item_type == "ssnc" and data:
            if code == "pvol":  # Volume
                # Format: "airplay_volume,volume,lowest_volume,highest_volume"
                self._current_metadata["volume_info"] = data
            elif code == "prgr":  # Progress
                # RTP timestamps for start, current, end
                self._current_metadata["progress"] = data
            elif code == "paus":  # Paused
                self._current_metadata["paused"] = True
            elif code == "prsm":  # Playing/resumed
                self._current_metadata["paused"] = False
