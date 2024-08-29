"""Implementation of a simple multi-client stream task/job."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress

from music_assistant.common.helpers.util import empty_queue
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.server.helpers.audio import get_ffmpeg_stream

LOGGER = logging.getLogger(__name__)


class MultiClientStream:
    """Implementation of a simple multi-client (audio) stream task/job."""

    def __init__(
        self,
        audio_source: AsyncGenerator[bytes, None],
        audio_format: AudioFormat,
        expected_clients: int = 0,
    ) -> None:
        """Initialize MultiClientStream."""
        self.audio_source = audio_source
        self.audio_format = audio_format
        self.subscribers: list[asyncio.Queue] = []
        self.expected_clients = expected_clients
        self.task = asyncio.create_task(self._runner())

    @property
    def done(self) -> bool:
        """Return if this stream is already done."""
        return self.task.done()

    async def stop(self) -> None:
        """Stop/cancel the stream."""
        if self.done:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        for sub_queue in list(self.subscribers):
            empty_queue(sub_queue)

    async def get_stream(
        self,
        output_format: AudioFormat,
        filter_params: list[str] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Get (client specific encoded) ffmpeg stream."""
        async for chunk in get_ffmpeg_stream(
            audio_input=self.subscribe_raw(),
            input_format=self.audio_format,
            output_format=output_format,
            filter_params=filter_params,
        ):
            yield chunk

    async def subscribe_raw(self) -> AsyncGenerator[bytes, None]:
        """Subscribe to the raw/unaltered audio stream."""
        try:
            queue = asyncio.Queue(2)
            self.subscribers.append(queue)
            while True:
                chunk = await queue.get()
                if chunk == b"":
                    break
                yield chunk
        finally:
            with suppress(ValueError):
                self.subscribers.remove(queue)

    async def _runner(self) -> None:
        """Run the stream for the given audio source."""
        expected_clients = self.expected_clients or 1
        # wait for first/all subscriber
        count = 0
        while count < 50:
            await asyncio.sleep(0.1)
            count += 1
            if len(self.subscribers) >= expected_clients:
                break
        LOGGER.debug(
            "Starting multi-client stream with %s/%s clients",
            len(self.subscribers),
            self.expected_clients,
        )
        async for chunk in self.audio_source:
            fail_count = 0
            while len(self.subscribers) == 0:
                await asyncio.sleep(0.1)
                fail_count += 1
                if fail_count > 50:
                    LOGGER.warning("No clients connected, stopping stream")
                    return
            await asyncio.gather(
                *[sub.put(chunk) for sub in self.subscribers], return_exceptions=True
            )
        # EOF: send empty chunk
        await asyncio.gather(*[sub.put(b"") for sub in self.subscribers], return_exceptions=True)
