"""Report the tracks the Plex provider streams to the Plex server, per player."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import plexapi.exceptions
import requests
from music_assistant_models.enums import EventType, MediaType, PlaybackState

from music_assistant.providers.plex.helpers import compute_client_id, player_client_headers

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.event import MassEvent
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.providers.plex import PlexProvider

# seconds between the position reports of a playing track, in line with the Plex apps
TIMELINE_INTERVAL = 10

PLEX_STATE_PLAYING = "playing"
PLEX_STATE_PAUSED = "paused"
PLEX_STATE_STOPPED = "stopped"


@dataclass
class _Timeline:
    """A track of the provider as last seen on a queue."""

    queue_item_id: str
    key: str
    state: str
    position_ms: int
    duration_ms: int
    headers: dict[str, str]
    seen_at: float
    reported_at: float = 0.0
    reported_state: str | None = None

    def position_at(self, now: float) -> int:
        """
        Return the position of the track at the given time.

        :param now: Monotonic time to return the position at.
        """
        if self.state != PLEX_STATE_PLAYING:
            return self.position_ms
        position = self.position_ms + round((now - self.seen_at) * 1000)
        return min(position, self.duration_ms) if self.duration_ms else position


class PlexPlaybackReporter:
    """
    Report the tracks the Plex provider streams to its Plex server, as the player plays them.

    Each player shows up in the Now Playing view and the listening history of Plex as a
    device of its own. A player that Plex Connect exposes is reported by Plex Connect.
    """

    def __init__(self, provider: PlexProvider) -> None:
        """
        Initialize the playback reporter.

        :param provider: The Plex provider whose streamed tracks are reported.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger
        self._timelines: dict[str, _Timeline] = {}
        self._skipped_players: list[str] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def start(self) -> None:
        """Start reporting playback to the Plex server."""
        self._unsubscribe = self.mass.subscribe(
            self._on_queue_event, (EventType.QUEUE_UPDATED, EventType.QUEUE_TIME_UPDATED)
        )
        self._task = self.mass.create_task(
            self._run(), task_name=f"plex_playback_reporter_{self.provider.instance_id}"
        )

    def stop(self) -> None:
        """Stop reporting playback to the Plex server."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task:
            self._task.cancel()
            self._task = None

    def skip_player(self, player_id: str) -> Callable[[], None]:
        """
        Leave reporting the playback of a player to the caller.

        Returns a callable that hands the reporting back.

        :param player_id: The player whose playback the caller reports to the Plex server.
        """
        self._skipped_players.append(player_id)
        self._wake.set()

        def resume() -> None:
            with suppress(ValueError):
                self._skipped_players.remove(player_id)
            self._wake.set()

        return resume

    def _on_queue_event(self, _event: MassEvent) -> None:
        """Report a changed queue state without waiting for the next position report."""
        self._wake.set()

    async def _run(self) -> None:
        """Report the queues on every queue change and at the position report interval."""
        while True:
            await self._report_queues()
            with suppress(TimeoutError):
                async with asyncio.timeout(self._next_report_delay()):
                    await self._wake.wait()
            self._wake.clear()

    def _next_report_delay(self) -> float:
        """Return the seconds until the position of a playing track is due to be reported."""
        now = time.monotonic()
        return max(
            0.0,
            min(
                (
                    timeline.reported_at + TIMELINE_INTERVAL - now
                    for timeline in self._timelines.values()
                    if timeline.state == PLEX_STATE_PLAYING
                ),
                default=TIMELINE_INTERVAL,
            ),
        )

    async def _report_queues(self) -> None:
        """Report the track each queue plays to the Plex server, where it changed or is due."""
        now = time.monotonic()
        skipped_queue_ids = self._skipped_queue_ids()
        queue_ids: set[str] = set()
        for queue in self.mass.player_queues.all():
            queue_ids.add(queue.queue_id)
            seen = None if queue.queue_id in skipped_queue_ids else self._seen_timeline(queue, now)
            await self._report_queue(queue.queue_id, seen, now)
        for queue_id in set(self._timelines) - queue_ids:
            await self._report_queue(queue_id, None, now)

    async def _report_queue(self, queue_id: str, seen: _Timeline | None, now: float) -> None:
        """
        Report the track a queue plays, ending the report of the track it played before.

        :param queue_id: The queue to report.
        :param seen: The track of this provider the queue plays now, if any.
        :param now: Monotonic time the queue was seen at.
        """
        previous = self._timelines.pop(queue_id, None)
        if previous and (
            seen is None or (seen.queue_item_id, seen.key) != (previous.queue_item_id, previous.key)
        ):
            await self._send(previous, PLEX_STATE_STOPPED, previous.position_at(now))
            previous = None
        if seen is None:
            return
        if previous and previous.reported_state == seen.state:
            seen.reported_at = previous.reported_at
            seen.reported_state = previous.reported_state
            if seen.state != PLEX_STATE_PLAYING or now - seen.reported_at < TIMELINE_INTERVAL:
                self._timelines[queue_id] = seen
                return
        # a failed report is not retried before the next interval either
        seen.reported_at = now
        seen.reported_state = seen.state
        self._timelines[queue_id] = seen
        await self._send(seen, seen.state, seen.position_ms)

    def _seen_timeline(self, queue: PlayerQueue, now: float) -> _Timeline | None:
        """
        Return the track of this provider the queue plays, if any.

        :param queue: The queue to look at.
        :param now: Monotonic time the queue is seen at.
        """
        if not queue.active or queue.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return None
        item = queue.current_item
        if item is None or item.media_type != MediaType.TRACK:
            return None
        streamdetails = item.streamdetails
        # only a track this provider streamed was played from this Plex server
        if streamdetails is None or streamdetails.provider != self.provider.instance_id:
            return None
        duration = streamdetails.duration or item.duration or 0
        client_id = compute_client_id(self.provider.instance_id, queue.queue_id)
        return _Timeline(
            queue_item_id=item.queue_item_id,
            key=streamdetails.item_id,
            state=PLEX_STATE_PLAYING if queue.state == PlaybackState.PLAYING else PLEX_STATE_PAUSED,
            position_ms=round(queue.corrected_elapsed_time * 1000),
            duration_ms=round(duration * 1000),
            headers=player_client_headers(client_id, queue.display_name),
            seen_at=now,
        )

    def _skipped_queue_ids(self) -> set[str]:
        """Return the queues that play on a player whose playback is reported elsewhere."""
        queue_ids: set[str] = set()
        for player_id in self._skipped_players:
            if (player := self.mass.players.get_player(player_id)) and (
                queue := self.mass.players.get_active_queue(player)
            ):
                queue_ids.add(queue.queue_id)
        return queue_ids

    async def _send(self, timeline: _Timeline, state: str, position_ms: int) -> None:
        """
        Send a timeline update of a track to the Plex server.

        :param timeline: The track to report.
        :param state: The Plex playback state to report.
        :param position_ms: The position to report, in milliseconds.
        """
        params = {
            "ratingKey": timeline.key.rsplit("/", 1)[-1],
            "key": timeline.key,
            "state": state,
            "time": str(position_ms),
            "duration": str(timeline.duration_ms),
        }
        try:
            await asyncio.to_thread(
                self.provider._plex_server.query,
                "/:/timeline",
                params=params,
                headers=timeline.headers,
            )
        except (plexapi.exceptions.PlexApiException, requests.exceptions.RequestException) as err:
            self.logger.debug("Failed to report %s of %s to Plex: %s", state, timeline.key, err)
