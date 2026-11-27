"""
The stream of a track that is mixed with its neighbours, as audio that can be read again.

In a queue that fades its tracks into each other, the stream of a track is more than the
track. It starts with the end of the mix that the track before it began (the lead-in), and
it ends with the first part of the mix into the track after it. Each mix is made once,
while it plays. A player that fetches a track as a file asks for parts of that stream
again, so what the mixes yield is kept here, and the track's own audio between them is
read from its buffer. Several players can read the stream beside each other.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING, Final

from music_assistant_models.errors import AudioError

from music_assistant.controllers.streams.file_stream import RUN_IN_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from music_assistant_models.enums import VolumeNormalizationMode
    from music_assistant_models.media_items import AudioFormat

# Seconds the stream of a track can get longer than the track: a smart fade slows the end
# of a track down to the tempo of the next one, by 8 percent at most over its 45 seconds.
SMART_FADE_STRETCH: Final[int] = 4

# Seconds a reader waits for more of the stream before it looks whether its player is
# still there.
HUNG_UP_CHECK_INTERVAL: Final[float] = 0.5


class _Pass:
    """One run of the stream of a mixed track, for the reader that drives it or for none."""

    def __init__(self, origin: int, connected: Callable[[], bool], unpaced: bool = False) -> None:
        """
        Initialize the pass.

        :param origin: Offset in the stream of the first byte the run yields.
        :param connected: Reports whether its reader is still there, or any reader of the
            stream for a pass that none drives.
        :param unpaced: Whether the pass runs by itself, at the pace of no reader.
        """
        self.origin = origin
        self.unpaced = unpaced
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
        """Return whether a reader is still there for what the pass makes."""
        return self.connected is not None and self.connected()


class MixedTrack:
    """
    The stream of one track, with the mixes it starts and ends with.

    The stream is made by passes. A pass that plays the track from its start brings the
    lead-in. The track's own audio goes to the reader that drives the pass, at the pace of
    that reader. From the moment the track is read to its end a pass runs on by itself and
    brings the end of the stream. The lead-in and the end are kept, so a later reader gets
    them from memory, and the audio between them from the track's buffer.

    One pass runs at a time. A reader beside the one that drives it reads the track's own
    audio from the buffer by itself, and when it needs the end first, a pass that waits for
    no reader makes it.
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
            consumer that ``consumer_connected`` tells about, with the volume normalization
            ``normalization_override`` pins it to.
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
        # the volume normalization the fade into the track pins its own audio to
        self._normalization: VolumeNormalizationMode | None = None
        # the offset in the stream where the kept end starts, once the track is read out
        self._end_offset: int | None = None
        self._end = bytearray()
        # the size of the stream, once its end is made
        self.size: int | None = None
        self._pass: _Pass | None = None
        # the pass that opened its stream last: one pass has the track at a time
        self._opened: asyncio.Task[None] | None = None
        # reports for every reader of the stream whether it is still there
        self._readers: list[Callable[[], bool]] = []
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

    def lead_in_ended(
        self, size: int, position_ms: int, normalization: VolumeNormalizationMode | None
    ) -> None:
        """
        Take the point where the fade into the track ends and its own audio goes on.

        :param size: The bytes the running pass has yielded up to this point.
        :param position_ms: The position in the track its own audio goes on from.
        :param normalization: The volume normalization the fade pins the track's own audio
            to, if it pins one.
        """
        assert self._pass is not None
        self._lead_in_size, self._track_ms = size, position_ms
        self._normalization = normalization
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
        self._readers.append(connected)
        try:
            while not self._closed and connected():
                if self._lead_in_size is None or offset < self._lead_in_size:
                    if self._pass is None:
                        own = self._start(0, None, connected)
                    if chunk := bytes(self._lead_in[offset : offset + self._second]):
                        offset += len(chunk)
                        yield chunk
                    elif self._lead_in_size is None:
                        await self._more()
                elif self._end_offset is None and (own is self._pass or not self._pass_runs):
                    # the end is still to be made, by a pass this reader drives: its own
                    # for as long as no other took its place
                    origin, position_ms = self._track_position(offset)
                    if own is not None and own is self._pass and own.over:
                        if own.error is not None:
                            raise own.error
                        return
                    if own is None or own is not self._pass or own.offset < origin:
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
                elif offset < self._track_end:
                    # from the track's buffer, as it needs no pass of this reader
                    async with aclosing(self._track_audio(offset, connected)) as track_audio:
                        async for chunk in track_audio:
                            offset += len(chunk)
                            yield chunk
                elif self._end_offset is None:
                    await self._end_for_all(offset)
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
            self._readers.remove(connected)
            if own is not None:
                own.connected = None
                if own is self._pass and self._reads_track and own.task is not None:
                    own.task.cancel()

    def close(self) -> None:
        """Give up what is kept, and end the pass that runs."""
        self._closed = True
        self._end_pass()
        self._lead_in = bytearray()
        self._end = bytearray()
        self._wake()

    @property
    def _reads_track(self) -> bool:
        """Return whether a pass that runs is in the track's own audio, between its mixes."""
        return self._lead_in_size is not None and self._end_offset is None

    @property
    def _pass_runs(self) -> bool:
        """Return whether a pass runs that a reader is still there for."""
        return self._pass is not None and not self._pass.over and self._pass.wanted()

    @property
    def _track_end(self) -> int:
        """
        Return the offset up to where the track's own audio is read without a pass.

        That is where the kept end starts. Before the end is made it is where a pass starts
        at the latest: the end is made from the audio that follows.
        """
        if self._end_offset is not None:
            return self._end_offset
        longest = self.max_size
        assert longest is not None
        return self._track_position(longest)[0]

    def _has_reader(self) -> bool:
        """Return whether a reader of the stream is still there."""
        return any(connected() for connected in self._readers)

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
        Yield the track's own audio from an offset in the stream, as far as no pass is needed.

        :param offset: Offset in the stream of the first byte, past the lead-in.
        :param connected: Reports whether whoever reads is still there.
        :raises AudioError: If the audio of the track ends before that.
        """
        origin, position_ms = self._track_position(offset)
        skip = offset - origin
        async with aclosing(
            self._open_track(
                seek_position_ms=position_ms,
                consumer_connected=connected,
                # at the level of the pass it is read beside, or in the place of
                normalization_override=self._normalization,
            )
        ) as audio:
            async for chunk in audio:
                # the end can get made while the track is read, which moves where this ends
                data = chunk[skip : skip + max(self._track_end - offset, 0)]
                skip = max(skip - len(chunk), 0)
                if data:
                    offset += len(data)
                    yield data
                if offset >= self._track_end:
                    return
        if not self._closed and connected():
            raise AudioError("The audio of the track ended before its mix")

    def _start(
        self,
        origin: int,
        start_ms: int | None,
        connected: Callable[[], bool],
        unpaced: bool = False,
    ) -> _Pass:
        """
        Start a pass, in place of one that is in the track's own audio.

        :param origin: Offset in the stream of the first byte the pass yields.
        :param start_ms: The position in the track the pass starts at, or None for the pass
            that plays the track from its start, with the fade into it.
        :param connected: Reports whether the reader that drives the pass is still there,
            or any reader of the stream for a pass that none drives.
        :param unpaced: Whether the pass runs by itself, at the pace of no reader.
        """
        self._end_pass()
        run = self._pass = _Pass(origin, connected, unpaced)
        stream = self._open_stream(start_ms=start_ms, marks=self, consumer_connected=run.wanted)
        run.task = asyncio.create_task(self._run(run, stream))
        return run

    def _end_pass(self) -> None:
        """End the pass that runs, and tell its reader."""
        if self._pass is None or self._pass.task is None:
            return
        self._pass.task.cancel()
        # a pass that is ended before its first step never gets to tell its reader
        self._pass.audio.put_nowait(None)

    async def _run(self, run: _Pass, stream: AsyncGenerator[bytes]) -> None:
        """
        Run a pass: keep what the mixes yield, and hand the track's own audio to its reader.

        :param run: The pass.
        :param stream: The stream of the track with its mixes.
        """
        try:
            if self._opened is not None:
                # the pass that had the track lets go of it first, whatever was started
                # and ended between the two
                await asyncio.wait({self._opened})
            self._opened = run.task
            async with aclosing(stream):
                async for chunk in stream:
                    if self._end_offset is not None:
                        self._end += chunk
                    elif self._lead_in_size is None:
                        self._lead_in += chunk
                    elif run.unpaced:
                        # the readers that wait for the end read the track by themselves
                        continue
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

    async def _end_for_all(self, offset: int) -> None:
        """
        Wait for the end, for a reader that needs it before the one that drives the pass.

        A pass that waits for no reader makes it, so no reader waits for another to read on.

        :param offset: Offset in the stream the reader is at.
        """
        assert self._pass is not None
        if not self._pass.unpaced:
            origin, position_ms = self._track_position(offset)
            self._start(origin, position_ms, self._has_reader, unpaced=True)
        await self._more()

    async def _more(self) -> None:
        """
        Wait until a pass has kept more of the stream, or raise what kept it from that.

        Returns without more after `HUNG_UP_CHECK_INTERVAL` seconds.
        """
        if self._error is not None:
            raise self._error
        with suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), HUNG_UP_CHECK_INTERVAL)

    def _wake(self) -> None:
        """Wake the readers that wait for more of the stream."""
        self._changed.set()
        self._changed = asyncio.Event()
