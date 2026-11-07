"""Metadata reader for shairport-sync metadata pipe."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.shairport_metadata import ShairportMetadataDecoder

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
        """
        Initialize metadata reader.

        :param metadata_pipe: Path to the metadata pipe.
        :param logger: Logger instance.
        :param on_metadata: Callback function for metadata updates.
        """
        self.metadata_pipe = metadata_pipe
        self.logger = logger
        self.on_metadata = on_metadata
        self._reader_task: asyncio.Task[None] | None = None
        self._stop = False
        self._decoder = ShairportMetadataDecoder(self._on_decoder_update, logger)
        self._fd: int | None = None
        self._buffer = ""
        self.cover_art_bytes: bytes | None = None

    async def start(self) -> None:
        """Start reading metadata from the pipe."""
        self._stop = False
        # Open the FIFO up front so hook writers never block on a missing reader.
        try:
            self._fd = await self._open_pipe()
        except OSError as err:
            # _run() retries the open; raising here would take down the whole daemon.
            self.logger.error("Unable to open metadata pipe %s: %s", self.metadata_pipe, err)
        self._reader_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop reading metadata."""
        self._stop = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
        # The fd stays open for the reader's whole lifetime, so stop() owns closing it.
        if self._fd is not None:
            with suppress(OSError):
                os.close(self._fd)
            self._fd = None

    async def _run(self) -> None:
        """
        Keep the pipe reader alive until stopped.

        A dead reader would silently drop all sessioncontrol markers and leave
        hook writers blocked on the FIFO, so unexpected errors must not be fatal.
        """
        backoff = 1
        while not self._stop:
            try:
                await self._read_metadata()
            except Exception as err:
                # Only the first failure of a streak is worth an error, retries are noise.
                log = self.logger.error if backoff == 1 else self.logger.debug
                log("Metadata reader failed, retrying in %ss: %s", backoff, err)
            if not self._stop:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _open_pipe(self) -> int:
        """Open the metadata pipe in non-blocking mode and return its file descriptor."""
        # O_RDWR keeps a writer attached so the read end never reaches EOF: epoll reports a
        # writerless FIFO readable forever, which spins the event loop at 100% CPU.
        return await asyncio.to_thread(os.open, self.metadata_pipe, os.O_RDWR | os.O_NONBLOCK)

    async def _read_metadata(self) -> None:
        """Read metadata from the pipe using async file descriptor."""
        loop = asyncio.get_running_loop()
        # start() already opened the pipe; only open here when start() could not.
        if self._fd is None:
            self._fd = await self._open_pipe()
        fd = self._fd

        # Create an asyncio.Event to signal when data is available
        data_available = asyncio.Event()

        def on_readable() -> None:
            """Set data available flag when file descriptor is readable."""
            data_available.set()

        # Register the file descriptor with the event loop
        loop.add_reader(fd, on_readable)

        try:
            while not self._stop:
                # Wait for data to be available
                await data_available.wait()
                data_available.clear()

                # Read available data from the pipe
                try:
                    chunk = os.read(fd, 4096)
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
            # Remove the reader callback, but keep the fd open so hook writers
            # never find the FIFO readerless while _run() restarts the loop.
            loop.remove_reader(fd)

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
        """
        Parse a single XML metadata item and pass it to the decoder.

        :param item_xml: XML string containing a metadata item.
        """
        type_match = re.search(r"<type>([0-9a-fA-F]{8})</type>", item_xml)
        code_match = re.search(r"<code>([0-9a-fA-F]{8})</code>", item_xml)
        length_match = re.search(r"<length>(\d+)</length>", item_xml)
        if not type_match or not code_match or not length_match:
            return

        # Type and code are four ASCII characters, sent as hex
        item_type = bytes.fromhex(type_match.group(1)).decode("ascii", errors="ignore")
        code = bytes.fromhex(code_match.group(1)).decode("ascii", errors="ignore")

        data = b""
        if int(length_match.group(1)) > 0 and (
            data_match := re.search(r"<data encoding=\"base64\">([^<]+)</data>", item_xml)
        ):
            try:
                data = base64.b64decode(data_match.group(1).strip())
            except binascii.Error as err:
                self.logger.debug("Error decoding base64 data of %s/%s: %s", item_type, code, err)
                return

        if code in ("PICT", "copl"):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Metadata: type=%s, code=%s, data=<%d bytes>",
                item_type,
                code,
                len(data),
            )
        else:
            self.logger.log(
                VERBOSE_LOG_LEVEL, "Metadata: type=%s, code=%s, data=%s", item_type, code, data
            )
        self._decoder.process_item(item_type, code, data)

    def _on_decoder_update(self, update: dict[str, Any]) -> None:
        """
        Forward a decoded metadata update, keeping cover art bytes for resolve_image.

        :param update: Metadata update from the decoder.
        """
        if (artwork := update.pop("artwork", None)) is not None:
            self.cover_art_bytes = artwork
            self.logger.debug("Stored cover art: %d bytes", len(artwork))
            # A new timestamp changes the cover art URL, so clients fetch the new image
            update["cover_art_timestamp"] = str(int(time.time() * 1000))
        if update and self.on_metadata:
            self.on_metadata(update)
