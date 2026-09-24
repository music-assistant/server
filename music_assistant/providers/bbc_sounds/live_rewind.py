"""Play a BBC programme that is on air now from the station's live rewind window."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.helpers.hls import HLSMediaPlaylistParser

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

type FetchText = Callable[[str], Awaitable[str]]
type FetchBytes = Callable[[str], Awaitable[bytes]]

LIVE_PROGRAMME_ID_PREFIX = "live_programme"
LIVE_PROGRAMME_ID_SEPARATOR = "~"
# BBC live HLS variants come as a short live-edge playlist; dropping this suffix
# gives the same stream with its multi-hour rewind window
NOREWIND_PLAYLIST_SUFFIX = ".norewind.m3u8"
# stay a few segments behind the live edge, as the BBC players do
LIVE_EDGE_SEGMENTS = 3
# a seek further than this past the live edge is snapped back to the live edge
LIVE_SEEK_TOLERANCE = 60
# BBC HLS programme dates run this far ahead of the broadcast: the BBC Sounds player
# starts a programme 20 seconds into the DASH timeline, which itself runs 16 seconds
# behind the HLS dates (availabilityStartTime 1969-12-31T23:59:44Z, same segment numbers)
PROGRAMME_DATE_OFFSET = 36
SEGMENT_FETCH_ATTEMPTS = 3
SEGMENT_FETCH_TIMEOUT = ClientTimeout(total=30)


@dataclass(frozen=True)
class LiveProgrammeId:
    """Identifies a programme on a station's live stream by its broadcast slot."""

    station_id: str
    pid: str
    start: int
    end: int

    @property
    def item_id(self) -> str:
        """Return the provider item id for this programme."""
        return LIVE_PROGRAMME_ID_SEPARATOR.join(
            (LIVE_PROGRAMME_ID_PREFIX, self.station_id, self.pid, str(self.start), str(self.end))
        )

    @property
    def duration(self) -> int:
        """Return the scheduled length of the programme in seconds."""
        return self.end - self.start

    @classmethod
    def parse(cls, item_id: str) -> LiveProgrammeId | None:
        """
        Parse a provider item id, returning None when it is not a live programme id.

        :param item_id: The provider item id.
        """
        parts = item_id.split(LIVE_PROGRAMME_ID_SEPARATOR)
        if len(parts) != 5 or parts[0] != LIVE_PROGRAMME_ID_PREFIX:
            return None
        try:
            return cls(station_id=parts[1], pid=parts[2], start=int(parts[3]), end=int(parts[4]))
        except ValueError:
            return None


@dataclass(frozen=True)
class TimedSegment:
    """A media segment of a live playlist with its wall-clock start time."""

    sequence: int
    start: float
    url: str


@dataclass(frozen=True)
class SegmentTimeline:
    """The segments of a live playlist, in order, with wall-clock timing."""

    segments: list[TimedSegment]
    target_duration: float
    ended: bool

    def index_at(self, timestamp: float) -> int:
        """
        Return the index of the segment playing at the given time.

        Times before the first segment give the first one, times at or past the live edge
        give the segment a few back from the edge.

        :param timestamp: UTC timestamp in seconds.
        """
        edge_index = max(0, len(self.segments) - LIVE_EDGE_SEGMENTS)
        index = 0
        for i, segment in enumerate(self.segments):
            if segment.start > timestamp:
                break
            index = i
        return min(index, edge_index)


def rewind_playlist_url(variant_url: str) -> str:
    """
    Return the rewind window playlist for a live BBC HLS variant playlist.

    :param variant_url: The URL of the live-edge variant playlist.
    """
    path, sep, query = variant_url.partition("?")
    if not path.endswith(NOREWIND_PLAYLIST_SUFFIX):
        raise MediaNotFoundError("This station's live stream can not be rewound")
    return f"{path.removesuffix(NOREWIND_PLAYLIST_SUFFIX)}.m3u8{sep}{query}"


def parse_timeline(
    playlist_text: str, playlist_url: str, from_sequence: int = 0
) -> SegmentTimeline:
    """
    Parse a live media playlist into a timeline of segments.

    :param playlist_text: The media playlist body.
    :param playlist_url: The URL the playlist was fetched from, to resolve segment URLs.
    :param from_sequence: Leave out the segments before this media sequence number.
    """
    playlist = HLSMediaPlaylistParser(playlist_text).parse()
    segments: list[TimedSegment] = []
    clock: float | None = None
    for sequence, segment in enumerate(playlist.segments, start=playlist.media_sequence):
        if start_time := segment.start_time:
            clock = start_time.timestamp() - PROGRAMME_DATE_OFFSET
        if clock is None:
            raise InvalidDataError("Live playlist carries no programme date and time")
        if sequence >= from_sequence:
            segments.append(
                TimedSegment(
                    sequence=sequence,
                    # summing durations drifts a little, which would move a start that
                    # falls exactly on a segment boundary into the segment before
                    start=round(clock, 3),
                    url=urljoin(playlist_url, segment.segment_url),
                )
            )
        clock += segment.duration
    return SegmentTimeline(
        segments=segments,
        target_duration=playlist.target_duration or 6,
        ended=playlist.ended,
    )


async def stream_programme(
    fetch_text: FetchText,
    fetch_bytes: FetchBytes,
    rewind_url: str,
    live_url: str,
    start: float,
    end: float,
    logger: logging.Logger,
    on_start: Callable[[float], None] | None = None,
    is_superseded: Callable[[], bool] | None = None,
) -> AsyncGenerator[bytes]:
    """
    Yield the MPEG-TS segments of a live stream from the given time to the given end time.

    Follows the live edge when it catches up with it, and stops at the end time.

    :param fetch_text: Callable that fetches a playlist body.
    :param fetch_bytes: Callable that fetches a media segment.
    :param rewind_url: The playlist holding the full rewind window.
    :param live_url: The short playlist holding only the live edge.
    :param start: UTC timestamp to start playback from.
    :param end: UTC timestamp to stop playback at.
    :param logger: Logger to report on the stream with.
    :param on_start: Called with the UTC timestamp playback actually starts from, which is
        earlier than the requested start when that lies past the live edge.
    :param is_superseded: Returns True once nothing reads this stream any more, e.g. after a
        seek started a new one; the stream then stops instead of following the live edge.
    """

    async def fetch_rewind_timeline(from_sequence: int = 0) -> SegmentTimeline:
        # the rewind window holds thousands of segments, so parse it off the event loop
        text = await fetch_text(rewind_url)
        return await asyncio.to_thread(parse_timeline, text, rewind_url, from_sequence)

    timeline = await fetch_rewind_timeline()
    if not timeline.segments:
        raise MediaNotFoundError("The station's rewind window is empty")
    # a slot boundary may fall a moment before the first segment of the window
    if start < timeline.segments[0].start - timeline.target_duration:
        raise MediaNotFoundError("This programme started too long ago to rewind to")
    first_segment = timeline.segments[timeline.index_at(start)]
    next_sequence = first_segment.sequence
    if on_start:
        on_start(first_segment.start)

    while not (is_superseded and is_superseded()):
        if timeline.segments and next_sequence < timeline.segments[0].sequence:
            logger.warning(
                "Rewind window moved past the playback position, skipping %s segments",
                timeline.segments[0].sequence - next_sequence,
            )
            next_sequence = timeline.segments[0].sequence
        pending = [s for s in timeline.segments if s.sequence >= next_sequence]
        for segment in pending:
            if segment.start >= end or (is_superseded and is_superseded()):
                return
            yield await fetch_bytes(segment.url)
            next_sequence = segment.sequence + 1
        if timeline.ended:
            return
        if not pending:
            # at the live edge: new segments arrive once per target duration
            await asyncio.sleep(timeline.target_duration / 2)
        timeline = parse_timeline(await fetch_text(live_url), live_url)
        if timeline.segments and next_sequence < timeline.segments[0].sequence:
            # still behind the short live playlist, so pick up from the rewind window
            timeline = await fetch_rewind_timeline(next_sequence)


def http_fetchers(session: ClientSession) -> tuple[FetchText, FetchBytes]:
    """
    Return playlist and segment fetchers using the given HTTP session.

    :param session: The HTTP session to fetch with.
    """

    async def fetch_once(url: str) -> bytes:
        async with session.get(url, timeout=SEGMENT_FETCH_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def fetch_bytes(url: str) -> bytes:
        for attempt in range(1, SEGMENT_FETCH_ATTEMPTS):
            try:
                return await fetch_once(url)
            except ClientError, TimeoutError:
                await asyncio.sleep(attempt)
        try:
            return await fetch_once(url)
        except (ClientError, TimeoutError) as err:
            raise MediaNotFoundError(f"Failed to fetch {url}: {err}") from err

    async def fetch_text(url: str) -> str:
        return (await fetch_bytes(url)).decode("utf-8", errors="replace")

    return fetch_text, fetch_bytes
