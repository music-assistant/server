"""
Rendering of announcement audio (optional pre-announce chime + announcement).

A single announcement is regularly consumed more than once: by the player's own http
fetch, by a HEAD probe preceding it, and by every member of a group announcement. An
AnnouncementRender decodes the audio once and keeps the whole clip in memory, so every
one of those readers is served from there, each in the format it needs. Because the
clip is complete, its exact duration is known without probing the source again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import calculate_content_length
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream

if TYPE_CHECKING:
    from music_assistant.controllers.players.helpers import AnnounceData

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.announcements")

# Announcements are always rendered to this format; readers that need something else
# (an encoded http stream, a different sample rate) convert from it per reader.
ANNOUNCEMENT_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=44100,
    bit_depth=16,
    channels=2,
)

# Upper bounds on the audio taken from each source. Announcements are short by nature;
# these only guard against a source that never ends (e.g. a radio stream handed to the
# announcement api, or an endless custom chime), which would otherwise render forever.
MAX_CHIME_SECONDS = 30
MAX_ANNOUNCEMENT_SECONDS = 300

# The longest clip a render can produce, for callers that need to bound a wait on an
# announcement whose length is not known.
MAX_CLIP_SECONDS = MAX_CHIME_SECONDS + MAX_ANNOUNCEMENT_SECONDS

# Maximum time to wait for a (slow) announcement source.
DEFAULT_RENDER_TIMEOUT = 30


class AnnouncementRender:
    """
    A single announcement clip (pre-announce chime + announcement), held in memory.

    Announcement clips are short and are read by several consumers at once, each from
    the start, so the clip is kept whole until the render is closed.
    """

    def __init__(self, announcement_url: str, pre_announce: bool, pre_announce_url: str) -> None:
        """
        Initialize the render. Call start() to begin rendering.

        :param announcement_url: URL of the announcement audio.
        :param pre_announce: Whether to prepend the pre-announce chime.
        :param pre_announce_url: URL of the pre-announce chime.
        """
        self.announcement_url = announcement_url
        self.pre_announce = pre_announce
        self.pre_announce_url = pre_announce_url
        self.ref_count = 0
        self._chunks: list[bytes] = []
        self._total_bytes = 0
        self._closed = False
        self._task: asyncio.Task[None] | None = None
        self._audio_added = asyncio.Condition()
        self._ready = asyncio.Event()
        self._finished = asyncio.Event()

    @property
    def duration(self) -> float:
        """Return the duration (in seconds) of the audio rendered so far."""
        return self._total_bytes / ANNOUNCEMENT_PCM_FORMAT.pcm_sample_size

    @property
    def key(self) -> str:
        """Return the key that identifies the audio of this render."""
        return _render_key(self.announcement_url, self.pre_announce, self.pre_announce_url)

    def start(self) -> None:
        """Start rendering the announcement audio."""
        self._task = asyncio.get_running_loop().create_task(self._render())

    async def get_stream(self, output_format: AudioFormat) -> AsyncGenerator[bytes]:
        """
        Stream the announcement audio in the given output format.

        Starts at the beginning of the clip and waits for audio that is still being
        rendered, so any number of consumers can read it at the same time.

        :param output_format: The format to deliver the audio in.
        """
        if output_format == ANNOUNCEMENT_PCM_FORMAT:
            async for chunk in self._read():
                yield chunk
            return
        async for chunk in get_ffmpeg_stream(
            audio_input=self._read(),
            input_format=ANNOUNCEMENT_PCM_FORMAT,
            output_format=output_format,
        ):
            yield chunk

    async def wait_ready(self, timeout: float = DEFAULT_RENDER_TIMEOUT) -> bool:
        """
        Wait until there is audio available to start playback with.

        Returns False if the source did not deliver any audio within the timeout.

        :param timeout: Maximum time to wait for the first audio.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            LOGGER.warning("Timeout waiting for announcement audio from %s", self.announcement_url)
            return False
        return self._total_bytes > 0

    async def wait_finished(self, timeout: float = DEFAULT_RENDER_TIMEOUT) -> float | None:
        """
        Wait for the render to finish and return the exact duration in seconds.

        Returns None if the render did not finish within the timeout.

        :param timeout: Maximum time to wait for the render to finish.
        """
        try:
            await asyncio.wait_for(self._finished.wait(), timeout)
        except TimeoutError:
            LOGGER.warning(
                "Timeout waiting for announcement %s to be rendered", self.announcement_url
            )
            return None
        return self.duration

    async def close(self) -> None:
        """Discard the rendered audio and stop reading from the source."""
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        async with self._audio_added:
            self._closed = True
            self._chunks.clear()
            self._ready.set()
            self._finished.set()
            self._audio_added.notify_all()

    async def _render(self) -> None:
        """Decode the chime and the announcement into the clip."""
        try:
            if self.pre_announce:
                await self._decode(self.pre_announce_url, MAX_CHIME_SECONDS)
            # only the announcement itself marks the clip ready to play: a player
            # started on the chime alone would run dry waiting for a slow source
            await self._decode(self.announcement_url, MAX_ANNOUNCEMENT_SECONDS, signal_ready=True)
        except AudioError as err:
            # a failed source ends the clip instead of propagating to every reader:
            # they receive whatever was rendered so far (possibly nothing) and stop
            LOGGER.warning(
                "Failed to fetch announcement audio from %s: %s", self.announcement_url, err
            )
        except Exception:
            # same reasoning, for anything the ffmpeg helper did not wrap in an AudioError
            LOGGER.exception("Error rendering announcement audio from %s", self.announcement_url)
        finally:
            async with self._audio_added:
                self._ready.set()
                self._finished.set()
                self._audio_added.notify_all()
            LOGGER.log(
                VERBOSE_LOG_LEVEL,
                "Rendered %.2f seconds of announcement audio for %s",
                self.duration,
                self.announcement_url,
            )

    async def _decode(self, url: str, max_seconds: int, signal_ready: bool = False) -> None:
        """Decode one source url and add it to the clip."""
        fmt = url.rsplit(".", maxsplit=1)[-1]
        stream = get_ffmpeg_stream(
            audio_input=url,
            input_format=AudioFormat(content_type=ContentType.try_parse(fmt)),
            output_format=ANNOUNCEMENT_PCM_FORMAT,
            chunk_size=calculate_content_length(ANNOUNCEMENT_PCM_FORMAT, 1),
            extra_input_args=["-t", str(max_seconds)],
        )
        # aclosing guarantees the ffmpeg process is torn down immediately when the
        # render is cancelled, instead of lingering until garbage collection
        # finalizes the abandoned generator.
        async with aclosing(stream):
            async for chunk in stream:
                async with self._audio_added:
                    self._chunks.append(chunk)
                    self._total_bytes += len(chunk)
                    if signal_ready:
                        self._ready.set()
                    self._audio_added.notify_all()

    async def _read(self) -> AsyncGenerator[bytes]:
        """Yield the clip from the start, waiting for audio that is still rendering."""
        index = 0
        while True:
            async with self._audio_added:
                while index >= len(self._chunks):
                    if self._closed or self._finished.is_set():
                        return
                    await self._audio_added.wait()
                chunk = self._chunks[index]
            index += 1
            # yielded outside the lock, so a slow reader never holds up the render
            yield chunk


class AnnouncementRenderer:
    """Registry of the announcement renders that are currently in use."""

    def __init__(self) -> None:
        """Initialize the renderer."""
        self._renders: dict[str, AnnouncementRender] = {}

    @property
    def active_renders(self) -> int:
        """Return the number of announcement renders currently in use."""
        return len(self._renders)

    def acquire(self, announce_data: AnnounceData) -> AnnouncementRender:
        """
        Get the render for the given announcement, starting it if it is not running yet.

        Every acquire must be paired with a release.

        :param announce_data: The announcement to render.
        """
        key = _announce_data_key(announce_data)
        if (render := self._renders.get(key)) is None:
            render = AnnouncementRender(
                announce_data["announcement_url"],
                announce_data["pre_announce"],
                announce_data["pre_announce_url"],
            )
            self._renders[key] = render
            render.start()
        render.ref_count += 1
        return render

    def get(self, announce_data: AnnounceData) -> AnnouncementRender | None:
        """
        Return the active render for the given announcement, if there is one.

        :param announce_data: The announcement to look up.
        """
        return self._renders.get(_announce_data_key(announce_data))

    async def release(self, render: AnnouncementRender) -> None:
        """
        Release a reference to a render, tearing it down when it was the last one.

        :param render: The render previously obtained from acquire().
        """
        render.ref_count -= 1
        if render.ref_count > 0:
            return
        if self._renders.get(render.key) is render:
            del self._renders[render.key]
        await render.close()


def _render_key(announcement_url: str, pre_announce: bool, pre_announce_url: str) -> str:
    """Return the key that identifies the audio of an announcement."""
    # The key covers the audio only - not which player fetches it - so all players
    # (and probes) that want this exact audio are served from a single render.
    return f"{announcement_url}|{pre_announce_url if pre_announce else ''}"


def _announce_data_key(announce_data: AnnounceData) -> str:
    """Return the render key for the given announcement."""
    return _render_key(
        announce_data["announcement_url"],
        announce_data["pre_announce"],
        announce_data["pre_announce_url"],
    )
