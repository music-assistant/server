"""
The stream of a track that is mixed with its neighbours, as audio that can be read again.

In a queue that fades its tracks into each other, the stream of a track is more than the
track. It starts with the end of the mix that the track before it began (the lead-in), and
it ends with the first part of the mix into the track after it. Each mix is made once,
while it plays. A player that fetches a track as a file asks for parts of that stream
again, so what the mixes yield is kept here, and the track's own audio between them is
read from its buffer.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING, Final

from music_assistant_models.errors import AudioError

from music_assistant.controllers.streams.file_stream import RUN_IN_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from music_assistant_models.media_items import AudioFormat

# Seconds the stream of a track can get longer than the track: a smart fade slows the end
# of a track down to the tempo of the next one, by 8 percent at most over its 45 seconds.
SMART_FADE_STRETCH: Final[int] = 4


class _Pass:
    """One run of the stream of a mixed track, for the reader that drives it."""

    def __init__(self, origin: int, connected: Callable[[], bool]) -> None:
        """
        Initialize the pass.

        :param origin: Offset in the stream of the first byte the run yields.
        :param connected: Reports whether its reader is still there.
        """
        self.origin = origin
        # offset in the stream of the next audio of the track its reader gets
        self.offset = origin
        # None once its reader has left
        self.connected: Callable[[], bool] | None = connected
        self.audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None

    @property
    def over(self) -> bool:
        """Return whether the run has ended."""
        return self.task is None or self.task.done()

    def wanted(self) -> bool:
        """Return whether its reader is still there to take the audio of the track."""
        return self.connected is not None and self.connected()


class MixedTrack:
    """
    The stream of one track, with the mixes it starts and ends with.

    The stream is made by passes. A pass that plays the track from its start brings the
    lead-in. The track's own audio goes to the reader that drives the pass, at the pace of
    that reader. From the moment the track is read to its end a pass runs on by itself and
    brings the end of the stream. The lead-in and the end are kept, so a later reader gets
    them from memory, and the audio between them from the track's buffer.
    """

    def __init__(
        self,
        pcm_format: AudioFormat,
        end_ms: int,
        tail_ms: int,
        open_stream: Callable[..., AsyncGenerator[bytes]],
        open_track: Callable[..., AsyncGenerator[bytes]],
    ) -> None:
        """
        Initialize the stream.

        :param pcm_format: PCM format of the stream.
        :param end_ms: The position in the track where its stream ends at the latest.
        :param tail_ms: The position in the track from where its audio can be part of the
            mix into the next track.
        :param open_stream: Opens the stream of the track with its mixes, from the position
            ``start_ms``, reporting to ``marks`` and for a consumer that ``consumer_connected``
            tells about.
        :param open_track: Opens the track's own audio at ``seek_position_ms``, for a
            consumer that ``consumer_connected`` tells about.
        """
        self._second = pcm_format.pcm_sample_size
        self._sample_rate = pcm_format.sample_rate
        self._end_ms = end_ms
        self._tail_ms = tail_ms
        self._open_stream = open_stream
        self._open_track = open_track
        self._lead_in = bytearray()
        # the size of the lead-in and the position in the track that follows it, once known
        self._lead_in_size: int | None = None
        self._track_ms = 0
        # the offset in the stream where the kept end starts, once the track is read out
        self._end_offset: int | None = None
        self._end = bytearray()
        # the size of the stream, once its end is made
        self.size: int | None = None
        self._pass: _Pass | None = None
        # what kept the lead-in or the end from being made: neither can be made again
        self._error: Exception | None = None
        self._changed = asyncio.Event()
        self._closed = False

    @property
    def max_size(self) -> int | None:
        """Return the size the stream has at most, or None before its lead-in is known."""
        if self.size is not None or self._lead_in_size is None:
            return self.size
        frames = -(-max(self._end_ms - self._track_ms, 0) * self._sample_rate // 1000)
        return self._lead_in_size + frames * (self._second // self._sample_rate)

    def lead_in_ended(self, size: int, position_ms: int) -> None:
        """
        Take the point where the fade into the track ends and its own audio goes on.

        :param size: The bytes the running pass has yielded up to this point.
        :param position_ms: The position in the track its own audio goes on from.
        """
        assert self._pass is not None
        self._lead_in_size, self._track_ms = size, position_ms
        self._pass.offset = size
        self._wake()

    def source_read_out(self, size: int) -> None:
        """
        Take the point from where the running pass yields the end of the stream.

        :param size: The bytes the running pass has yielded up to this point.
        """
        assert self._pass is not None
        self._end_offset = self._pass.origin + size
        # its reader goes on with what is kept
        self._pass.audio.put_nowait(None)
        self._wake()

    async def read(self, offset: int, connected: Callable[[], bool]) -> AsyncGenerator[bytes]:
        """
        Yield the stream from an offset to its end.

        :param offset: Offset in the stream of the first byte, on a whole frame.
        :param connected: Reports whether whoever reads is still there.
        """
        own: _Pass | None = None
        try:
            while not self._closed:
                if self._lead_in_size is None or offset < self._lead_in_size:
                    if self._pass is None:
                        own = self._start(0, None, connected)
                    if chunk := bytes(self._lead_in[offset : offset + self._second]):
                        offset += len(chunk)
                        yield chunk
                    elif self._lead_in_size is None:
                        await self._more()
                elif self._end_offset is None:
                    # the end is still to be made, by a pass this reader drives
                    origin, position_ms = self._track_position(offset)
                    if own is not None and own.over:
                        if own.error is not None:
                            raise own.error
                        return
                    if own is None or own.offset < origin:
                        await self._stop()
                        own = self._start(origin, position_ms, connected)
                    audio = await own.audio.get()
                    own.audio.task_done()
                    if audio is None:
                        continue
                    start, own.offset = own.offset, own.offset + len(audio)
                    # a pass starts before the audio that is asked, to let its filters settle
                    if chunk := audio[offset - start :]:
                        offset += len(chunk)
                        yield chunk
                elif offset < self._end_offset:
                    async with aclosing(self._track_audio(offset, connected)) as track_audio:
                        async for chunk in track_audio:
                            offset += len(chunk)
                            yield chunk
                    if offset < self._end_offset:
                        if self._closed or not connected():
                            return
                        raise AudioError("The audio of the track ended before its mix")
                else:
                    start = offset - self._end_offset
                    if chunk := bytes(self._end[start : start + self._second]):
                        offset += len(chunk)
                        yield chunk
                    elif self.size is not None:
                        return
                    else:
                        await self._more()
        finally:
            if own is not None:
                own.connected = None
                if own is self._pass and self._reads_track and own.task is not None:
                    own.task.cancel()

    def close(self) -> None:
        """Give up what is kept, and end the pass that runs."""
        self._closed = True
        if self._pass is not None and self._pass.task is not None:
            self._pass.task.cancel()
        self._lead_in = bytearray()
        self._end = bytearray()
        self._wake()

    @property
    def _reads_track(self) -> bool:
        """Return whether a pass that runs is in the track's own audio, between its mixes."""
        return self._lead_in_size is not None and self._end_offset is None

    def _track_position(self, offset: int) -> tuple[int, int]:
        """
        Return where to read the track for its own audio at an offset in the stream.

        The track is read from a whole number of seconds after its lead-in: that is a
        whole frame in every format its audio passes through, so it comes out as the same
        samples as in a pass from the start.

        :param offset: Offset in the stream, past the lead-in.
        :return: The offset in the stream to read from, and the position in the track
            that belongs to it.
        """
        assert self._lead_in_size is not None
        seconds = (offset - self._lead_in_size) // self._second
        # a mix is made from the whole tail of the track, wherever its reader starts
        seconds = min(seconds, (self._tail_ms - self._track_ms) // 1000)
        seconds = max(seconds - RUN_IN_SECONDS, 0)
        return self._lead_in_size + seconds * self._second, self._track_ms + seconds * 1000

    async def _track_audio(
        self, offset: int, connected: Callable[[], bool]
    ) -> AsyncGenerator[bytes]:
        """
        Yield the track's own audio from an offset in the stream up to the kept end.

        :param offset: Offset in the stream of the first byte, past the lead-in.
        :param connected: Reports whether whoever reads is still there.
        """
        assert self._end_offset is not None
        origin, position_ms = self._track_position(offset)
        skip = offset - origin
        async with aclosing(
            self._open_track(seek_position_ms=position_ms, consumer_connected=connected)
        ) as audio:
            async for chunk in audio:
                data = chunk[skip : skip + self._end_offset - offset]
                skip = max(skip - len(chunk), 0)
                if data:
                    offset += len(data)
                    yield data
                if offset == self._end_offset:
                    return

    def _start(self, origin: int, start_ms: int | None, connected: Callable[[], bool]) -> _Pass:
        """
        Start a pass for a reader.

        :param origin: Offset in the stream of the first byte the pass yields.
        :param start_ms: The position in the track the pass starts at, or None for the pass
            that plays the track from its start, with the fade into it.
        :param connected: Reports whether the reader is still there.
        """
        run = self._pass = _Pass(origin, connected)
        stream = self._open_stream(start_ms=start_ms, marks=self, consumer_connected=run.wanted)
        run.task = asyncio.create_task(self._run(run, stream))
        return run

    async def _stop(self) -> None:
        """End a pass that is in the track's own audio, and wait until it has gone."""
        if self._pass is None or self._pass.task is None or self._pass.over:
            return
        self._pass.task.cancel()
        await asyncio.wait({self._pass.task})

    async def _run(self, run: _Pass, stream: AsyncGenerator[bytes]) -> None:
        """
        Run a pass: keep what the mixes yield, and hand the track's own audio to its reader.

        :param run: The pass.
        :param stream: The stream of the track with its mixes.
        """
        try:
            async with aclosing(stream):
                async for chunk in stream:
                    if self._end_offset is not None:
                        self._end += chunk
                    elif self._lead_in_size is None:
                        self._lead_in += chunk
                    elif run.connected is None:
                        # the track's own audio is only read for a reader
                        return
                    else:
                        # at the pace of its reader: the track is read on once this is taken
                        run.audio.put_nowait(chunk)
                        await run.audio.join()
                        continue
                    self._wake()
            if self._end_offset is not None:
                self.size = self._end_offset + len(self._end)
        except Exception as err:  # noqa: BLE001 - raised to whoever reads the stream
            run.error = err
            if not self._reads_track:
                self._error = err
        finally:
            run.audio.put_nowait(None)
            self._wake()

    async def _more(self) -> None:
        """Wait until a pass has kept more of the stream, or raise what kept it from that."""
        if self._error is not None:
            raise self._error
        await self._changed.wait()

    def _wake(self) -> None:
        """Wake the readers that wait for more of the stream."""
        self._changed.set()
        self._changed = asyncio.Event()
