"""Report the tracks the Plex provider streams to the Plex server, per player."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import plexapi.exceptions
import requests
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.plex.helpers import compute_client_id, player_client_headers

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

    from music_assistant.providers.plex import PlexProvider

# seconds between the attempts to send a stop the Plex server did not take
STOP_RETRY_DELAY = 10
# times a stop the Plex server did not take is sent again
STOP_RETRIES = 3

PLEX_STATE_PLAYING = "playing"
PLEX_STATE_PAUSED = "paused"
PLEX_STATE_STOPPED = "stopped"


@dataclass
class _Timeline:
    """A timeline update of a track, as sent to the Plex server."""

    key: str
    state: str
    position_ms: int
    duration_ms: int
    headers: dict[str, str]
    play_queue_params: dict[str, str]
    delivered: bool = False


class PlexPlaybackReporter:
    """
    Report the tracks the Plex provider streams to its Plex server, as the player plays them.

    Each player shows up in the Now Playing view and the listening history of Plex as a
    device of its own. A player that Plex Connect mirrors to a Plex play queue is reported
    within that play queue.
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
        self._play_queues: dict[str, Callable[[], dict[str, str]]] = {}
        self._retry_timers: set[str] = set()
        # reports are sent one at a time, in the order the player made them
        self._lock = asyncio.Lock()
        self._stopped = False

    def add_play_queue(
        self, player_id: str, play_queue_params: Callable[[], dict[str, str]]
    ) -> Callable[[], None]:
        """
        Report the tracks of a player within the Plex play queue its queue is mirrored to.

        Returns a callable that stops reporting the player within the play queue.

        :param player_id: The player whose queue is mirrored to a Plex play queue.
        :param play_queue_params: Returns the timeline params that place the current track
            of the player's queue in its Plex play queue.
        """
        self._play_queues[player_id] = play_queue_params

        def remove() -> None:
            if self._play_queues.get(player_id) is play_queue_params:
                del self._play_queues[player_id]

        return remove

    async def report(
        self, report: MediaItemPlaybackProgressReport, key: str, state: PlaybackState
    ) -> None:
        """
        Report the playback of a track this provider streamed to the Plex server.

        :param report: The playback progress report of the track.
        :param key: The Plex key of the track.
        :param state: The playback state of the track on the player.
        """
        if not (player_id := report.player_id):
            return
        if state == PlaybackState.PLAYING:
            plex_state = PLEX_STATE_PLAYING
        elif state == PlaybackState.PAUSED:
            plex_state = PLEX_STATE_PAUSED
        else:
            plex_state = PLEX_STATE_STOPPED
        position_ms = report.seconds_played * 1000
        # the play queue can move on to the next track while this report waits for the lock
        get_params = self._play_queues.get(player_id)
        current_play_queue_params = get_params() if get_params else {}
        async with self._lock:
            if self._stopped:
                return
            previous = self._timelines.get(player_id)
            if previous and previous.key != key:
                previous = None
            if (
                previous
                and previous.delivered
                and plex_state != PLEX_STATE_STOPPED
                and previous.state == plex_state
                and (plex_state == PLEX_STATE_PAUSED or previous.position_ms == position_ms)
            ):
                # the same report again, as a paused track does not move
                return
            if plex_state == PLEX_STATE_STOPPED and previous:
                # a stop ends the play within the play queue item it started in
                play_queue_params = previous.play_queue_params
            elif plex_state != PLEX_STATE_STOPPED:
                play_queue_params = current_play_queue_params
            else:
                play_queue_params = {}
            timeline = _Timeline(
                key=key,
                state=plex_state,
                position_ms=position_ms,
                duration_ms=report.duration * 1000,
                headers=self._headers(player_id),
                play_queue_params=play_queue_params,
            )
            if plex_state != PLEX_STATE_STOPPED:
                self._timelines[player_id] = timeline
            elif previous:
                del self._timelines[player_id]
            timeline.delivered = await self._send(timeline)
        if not timeline.delivered and plex_state == PLEX_STATE_STOPPED:
            self._retry_stop(player_id, timeline, 1)

    def stop(self) -> None:
        """Stop reporting playback to the Plex server."""
        self._stopped = True
        for task_id in self._retry_timers:
            self.mass.cancel_timer(task_id)
        self._retry_timers.clear()

    def _headers(self, player_id: str) -> dict[str, str]:
        """
        Return the headers that present a player to the Plex server.

        :param player_id: The player to present.
        """
        player = self.mass.players.get_player(player_id)
        return player_client_headers(
            compute_client_id(self.provider.instance_id, player_id),
            player.name if player else "Music Assistant",
        )

    def _retry_stop(self, player_id: str, timeline: _Timeline, tries: int) -> None:
        """
        Send a stop the Plex server did not take again, after a delay.

        :param player_id: The player the stop was reported for.
        :param timeline: The stop to send again.
        :param tries: The number of times the stop was sent before.
        """
        if self._stopped:
            return
        task_id = f"plex_stop_retry_{self.provider.instance_id}_{id(timeline)}"

        async def retry() -> None:
            self._retry_timers.discard(task_id)
            async with self._lock:
                current = self._timelines.get(player_id)
                if self._stopped or (current and current.key == timeline.key):
                    # the player plays the track again, a stop now would end that new play
                    return
                delivered = await self._send(timeline)
            if not delivered and tries < STOP_RETRIES:
                self._retry_stop(player_id, timeline, tries + 1)

        self._retry_timers.add(task_id)
        self.mass.call_later(STOP_RETRY_DELAY, retry, task_id=task_id)

    async def _send(self, timeline: _Timeline) -> bool:
        """
        Send a timeline update to the Plex server, and return whether it took it.

        :param timeline: The timeline update to send.
        """
        params = {
            "ratingKey": timeline.key.rsplit("/", 1)[-1],
            "key": timeline.key,
            "state": timeline.state,
            "time": str(timeline.position_ms),
            "duration": str(timeline.duration_ms),
            **timeline.play_queue_params,
        }
        try:
            await asyncio.to_thread(
                self.provider._plex_server.query,
                "/:/timeline",
                params=params,
                headers=timeline.headers,
            )
        except (plexapi.exceptions.PlexApiException, requests.exceptions.RequestException) as err:
            self.logger.debug(
                "Failed to report %s of %s to Plex: %s", timeline.state, timeline.key, err
            )
            return False
        return True
