"""
Helper module for sending timeline updates to Plex when playing Plex media.

This module provides functionality to notify Plex servers about the playback status
of Plex media content played through Music Assistant, allowing for playback state
synchronization in the Plex interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import EventType, PlayerState

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant


class PlexTimelineReporter:
    """
    Reporter that sends timeline updates to Plex for played media.

    This class monitors player and queue updates to detect when a Plex media item
    is being played, paused, or stopped, and sends appropriate updates to the Plex API
    to reflect the playback state in the Plex interface.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the timeline reporter.

        Args:
            mass: The MusicAssistant instance.
        """
        self.mass = mass
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self._session = None
        self._active_tracks: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._client_info: dict[str, dict[str, str]] = {}
        self.logger.info("PlexTimelineReporter initialized")

    async def setup(self) -> None:
        """
        Set up the timeline reporter and start listening for events.

        Creates an HTTP session and subscribes to relevant events to monitor
        Plex content playback.
        """
        self.logger.debug("Setting up Plex timeline reporter...")
        self._session = aiohttp.ClientSession()
        self.mass.subscribe(self._handle_queue_update, EventType.QUEUE_UPDATED)
        self.mass.subscribe(self._handle_queue_time_update, EventType.QUEUE_TIME_UPDATED)
        self.mass.subscribe(self._handle_player_update, EventType.PLAYER_UPDATED)
        self.logger.info("Plex timeline reporter setup complete - listening for events")

    async def close(self) -> None:
        """
        Clean up resources and close connections.

        Terminates all running tasks and closes the HTTP session.
        """
        self.logger.debug("Closing Plex timeline reporter...")
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self._session:
            await self._session.close()
            self._session = None
        self.logger.info("Plex timeline reporter closed")

    async def _handle_queue_update(self, event) -> None:
        """
        Handle queue update events.

        Called when a queue is updated (e.g., new track, state changed, etc.).
        Determines if the current track is from Plex and starts or updates reporting
        of playback state.

        Args:
            event: The queue update event.
        """
        queue_id = event.object_id
        queue = event.data
        if not queue or not queue_id:
            return

        self.logger.debug("Queue update for %s, state: %s", queue_id, queue.state)

        current_item = queue.current_item
        if not current_item or not current_item.streamdetails:
            self.logger.debug("Queue %s has no current item or streamdetails", queue_id)
            self._stop_reporting(queue_id)
            return

        if not self._is_plex_item(current_item):
            self.logger.debug("Current item is not from Plex provider")
            self._stop_reporting(queue_id)
            return

        plex_data = current_item.streamdetails.data
        if not self._validate_plex_data(plex_data):
            self.logger.debug("Missing required Plex data in streamdetails")
            return

        if queue.state == PlayerState.PLAYING:
            self._handle_playing_state(queue_id, queue, current_item, plex_data)
        elif queue.state == PlayerState.PAUSED:
            self._handle_paused_state(queue_id, queue, current_item)
        elif queue.state == PlayerState.IDLE:
            await self._handle_idle_state(queue_id, queue, current_item)

    def _is_plex_item(self, current_item: QueueItem) -> bool:
        """Check if the current item is a Plex item."""
        if not current_item.streamdetails:
            return False
        provider = current_item.streamdetails.provider
        if not provider or not provider.startswith(("plex:", "plex--")):
            return False
        return current_item.streamdetails.data is not None and isinstance(
            current_item.streamdetails.data, dict
        )

    def _validate_plex_data(self, plex_data: dict) -> bool:
        """Validate that the Plex data dictionary contains the required keys."""
        required_keys = ["rating_key", "server_url", "token", "machine_identifier"]
        if not all(k in plex_data for k in required_keys):
            missing_keys = ", ".join(k for k in required_keys if k not in plex_data)
            self.logger.debug("Missing required Plex data in streamdetails: %s", missing_keys)
            return False
        return True

    def _handle_playing_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem, plex_data: dict
    ) -> None:
        """Handle the playing state of the queue."""
        self.logger.info(
            "Queue %s is now playing Plex track: %s (rating_key: %s)",
            queue_id,
            current_item.name,
            plex_data["rating_key"],
        )
        self._start_reporting(queue_id, queue, current_item)

    def _handle_paused_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem
    ) -> None:
        """Handle the paused state of the queue."""
        self.logger.info("Queue %s is now paused", queue_id)
        if (
            current_item.duration
            and queue.elapsed_time >= current_item.duration - 2
            and not queue.next_item
        ):
            self.logger.info(
                "Track %s ended naturally with no next track, stopping reporting",
                current_item.name,
            )
            self._update_state(queue_id, "stopped")
            self._stop_reporting(queue_id)
        else:
            self._update_state(queue_id, "paused")

    async def _handle_idle_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem
    ) -> None:
        """Handle the idle state of the queue."""
        is_pause_action = (
            current_item.duration
            and queue.elapsed_time > 0
            and queue.elapsed_time < (current_item.duration - 5)
        )

        is_track_ended = current_item.duration and queue.elapsed_time >= (current_item.duration - 2)

        if is_pause_action:
            self._handle_idle_paused(queue_id, queue, current_item)
        elif is_track_ended:
            await self._handle_idle_ended(queue_id, queue, current_item)
        else:
            self._handle_idle_stopped(queue_id)

    def _handle_idle_paused(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem
    ) -> None:
        """Handle the idle state when it appears to be a paused action."""
        self.logger.info(
            "Queue %s is idle but appears to be paused (time: %s/%s), sending paused state to Plex",
            queue_id,
            queue.elapsed_time,
            current_item.duration,
        )
        if queue_id in self._active_tracks:
            self._update_state(queue_id, "paused")
        else:
            self._start_reporting(queue_id, queue, current_item)
            self._update_state(queue_id, "paused")

    async def _handle_idle_ended(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem
    ) -> None:
        """Handle the idle state when the track appears to have ended."""
        self.logger.info(
            "Track %s appears to have completed naturally "
            "(time: %s/%s), sending stopped state to Plex",
            current_item.name,
            queue.elapsed_time,
            current_item.duration,
        )
        self.logger.info("Arresto riproduzione per fine brano...")
        final_position = int(current_item.duration * 1000)
        await self._send_stopped_state(queue_id, queue, current_item, final_position)

    def _handle_idle_stopped(self, queue_id: str) -> None:
        """Handle the idle state when the queue is simply stopped."""
        self.logger.info("Queue %s is now stopped", queue_id)
        if queue_id in self._active_tracks:
            self._update_state(queue_id, "stopped")
            self._stop_reporting(queue_id)
        else:
            self._stop_reporting_in_idle(queue_id)

    async def _send_stopped_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem, final_position: int
    ) -> None:
        """Send the stopped state to Plex and handle reporting."""
        if queue_id in self._active_tracks:
            self.mass.create_task(self._send_timeline_update(queue_id, "stopped", final_position))
            self._stop_reporting(queue_id)
        else:
            self._start_reporting(queue_id, queue, current_item)
            self.mass.create_task(self._send_timeline_update(queue_id, "stopped", final_position))
            self._stop_reporting(queue_id)

    def _stop_reporting_in_idle(self, queue_id: str) -> None:
        """Start and stop reporting in stopped state when queue is idle."""
        queue = next((q for q in self.mass.players.all() if q.queue_id == queue_id), None)
        current_item = queue.current_item if queue else None

        if current_item and current_item.streamdetails:
            self.logger.debug(
                "Queue is idle but has a current item - starting reporting in stopped state"
            )
            self._start_reporting(queue_id, queue, current_item)
            self._update_state(queue_id, "stopped")
            self._stop_reporting(queue_id)

    async def _handle_queue_time_update(self, event) -> None:
        """
        Handle queue time update events.

        Called frequently when a track is playing to keep track of the current position.

        Args:
            event: The queue time update event.
        """
        queue_id = event.object_id
        elapsed_time = event.data
        if not queue_id or queue_id not in self._active_tracks or elapsed_time is None:
            return

        previous_elapsed = self._active_tracks[queue_id].get("elapsed_time", 0)
        self._active_tracks[queue_id]["elapsed_time"] = elapsed_time

        if int(elapsed_time) % 5 == 0:
            self.logger.debug("Time update for queue %s: %s seconds", queue_id, int(elapsed_time))
            self.logger.debug(
                "Progress update for '%s': %s ms into playback",
                self._active_tracks[queue_id]["item"].name,
                int(elapsed_time * 1000),
            )

        track_duration = self._active_tracks[queue_id]["duration"] / 1000

        if previous_elapsed > (track_duration - 5) and elapsed_time < 2:
            self.logger.info(
                "Detected track completion or restart - "
                "sending stopped state to Plex before continuing"
            )
            final_position = int(track_duration * 1000)
            self.logger.info("Arresto riproduzione per fine brano...")

            for _ in range(3):
                await self._send_timeline_update(queue_id, "stopped", final_position)
                await asyncio.sleep(0.5)

            await asyncio.sleep(1)

    async def _handle_player_update(self, event) -> None:
        """
        Handle player update events.

        Ensures we detect when a player is powered off or becomes unavailable
        to properly stop reporting.

        Args:
            event: The player update event.
        """
        player = event.data
        if not player:
            return

        player_id = player.player_id
        for queue_id, track_data in list(self._active_tracks.items()):
            if track_data.get("player_id") == player_id:
                if not player.available or player.powered is False:
                    self.logger.info("Player %s is no longer available or powered off", player_id)
                    pause_position = int(track_data["elapsed_time"] * 1000)
                    self.logger.info("Arresto riproduzione per spegnimento player...")
                    await self._send_timeline_update(queue_id, "stopped", pause_position)
                    await asyncio.sleep(1)
                    self._stop_reporting(queue_id)

    def _start_reporting(self, queue_id: str, queue: PlayerQueue, item: QueueItem) -> None:
        """
        Start sending timeline updates for the given queue and item.

        Sets up tracking data and creates a background task that will regularly
        send state updates to the Plex server.

        Args:
            queue_id: The queue ID.
            queue: The PlayerQueue object.
            item: The current QueueItem.
        """
        if queue_id in self._active_tracks:
            if self._active_tracks[queue_id].get("item_id") == item.queue_item_id:
                self.logger.debug("Updating state to 'playing' for existing track %s", item.name)
                self._update_state(queue_id, "playing")
                return
            self.logger.debug("New track detected, stopping previous timeline reporting")
            self._stop_reporting(queue_id)

        plex_data = item.streamdetails.data

        track_data = {
            "player_id": queue.queue_id,
            "queue": queue,
            "item": item,
            "item_id": item.queue_item_id,
            "rating_key": plex_data["rating_key"],
            "key": plex_data.get("key", f"/library/metadata/{plex_data['rating_key']}"),
            "duration": plex_data["duration"],
            "machine_identifier": plex_data["machine_identifier"],
            "server_url": plex_data["server_url"],
            "token": plex_data["token"],
            "state": "playing",
            "elapsed_time": queue.elapsed_time or 0,
        }

        self._active_tracks[queue_id] = track_data
        self._tasks[queue_id] = asyncio.create_task(self._timeline_update_task(queue_id))

        self.logger.info(
            "Started Plex timeline reporting for %s (rating_key: %s, server: %s)",
            item.name,
            plex_data["rating_key"],
            plex_data["server_url"],
        )

    def _update_state(self, queue_id: str, state: str) -> None:
        """
        Update the state of an active track.

        Changes the current playback state and sends multiple updates to ensure
        Plex detects the change.

        Args:
            queue_id: The queue ID.
            state: The new state ('playing', 'paused', or 'stopped').
        """
        if queue_id not in self._active_tracks:
            return

        if self._active_tracks[queue_id]["state"] != state:
            self._active_tracks[queue_id]["state"] = state
            self.logger.info(
                "Updated Plex timeline state to %s for %s",
                state,
                self._active_tracks[queue_id]["item"].name,
            )

            elapsed_time = int(self._active_tracks[queue_id]["elapsed_time"] * 1000)

            if state == "stopped":
                self.mass.create_task(self._force_stop_state_update(queue_id, elapsed_time))
            else:
                self.mass.create_task(self._send_repeated_updates(queue_id, state, elapsed_time))

    def _stop_reporting(self, queue_id: str) -> None:
        """
        Stop sending timeline updates for the given queue.

        Sends a final 'stopped' state to ensure Plex registers the end of playback,
        cancels the update task, and removes tracking data.

        Args:
            queue_id: The queue ID.
        """
        if queue_id not in self._active_tracks:
            return

        track_data = self._active_tracks[queue_id]
        if track_data["state"] != "stopped":
            self.logger.debug("Sending final 'stopped' state to Plex")
            self.mass.create_task(self._force_send_stopped_state(queue_id, track_data))

        if queue_id in self._tasks and not self._tasks[queue_id].done():
            self._tasks[queue_id].cancel()

        track_name = track_data["item"].name if "item" in track_data else "unknown track"
        del self._active_tracks[queue_id]
        self._tasks.pop(queue_id, None)

        self.logger.info("Stopped Plex timeline reporting for %s (queue %s)", track_name, queue_id)

    async def _force_stop_state_update(self, queue_id: str, elapsed_time: int) -> None:
        """
        Force update of the stopped state to ensure Plex registers it.

        Sends multiple consecutive 'stopped' updates before terminating reporting.

        Args:
            queue_id: The queue ID.
            elapsed_time: The elapsed time in milliseconds.
        """
        for _ in range(3):
            await self._send_timeline_update(queue_id, "stopped", elapsed_time)
            await asyncio.sleep(0.5)

        self._stop_reporting(queue_id)

    async def _send_repeated_updates(self, queue_id: str, state: str, elapsed_time: int) -> None:
        """
        Send repeated timeline updates to ensure Plex registers the state change.

        Args:
            queue_id: The queue ID.
            state: The state to send.
            elapsed_time: The elapsed time in milliseconds.
        """
        for _ in range(3):
            await self._send_timeline_update(queue_id, state, elapsed_time)
            await asyncio.sleep(0.5)

    async def _force_send_stopped_state(self, queue_id: str, track_data: dict) -> None:
        """
        Send the 'stopped' state multiple times to ensure Plex receives it.

        Args:
            queue_id: The queue ID.
            track_data: The active track data.
        """
        elapsed_time = int(track_data["elapsed_time"] * 1000)

        self.logger.info("Arresto riproduzione...")

        for _ in range(3):
            try:
                await self._send_timeline_update(queue_id, "stopped", elapsed_time)
                await asyncio.sleep(0.5)
            except Exception as exc:
                self.logger.warning("Error sending final stopped state: %s", exc)

    async def _timeline_update_task(self, queue_id: str) -> None:
        """
        Background task that sends timeline updates at regular intervals.

        This task handles sending initial updates (buffering, playing) and continues
        to send periodic updates as long as the track is active.

        Args:
            queue_id: The queue ID.
        """
        try:
            self.logger.debug("Sending initial buffering state to Plex")
            await self._send_timeline_update(queue_id, "buffering", 0)
            await asyncio.sleep(0.5)
            self.logger.debug("Sending initial playing state to Plex")
            await self._send_timeline_update(queue_id, "playing", 0)

            last_update_time = time.time()
            last_state = "playing"
            last_elapsed = 0

            while queue_id in self._active_tracks:
                now = time.time()
                elapsed_time = int(self._active_tracks[queue_id]["elapsed_time"] * 1000)
                current_state = self._active_tracks[queue_id]["state"]

                send_update = False
                if current_state != last_state:
                    self.logger.debug(
                        "State changed from %s to %s, sending update", last_state, current_state
                    )
                    send_update = True
                elif current_state == "playing" and (now - last_update_time) >= 5:
                    self.logger.debug("5 seconds elapsed during playback, sending update")
                    send_update = True
                elif current_state == "paused" and (now - last_update_time) >= 30:
                    self.logger.debug("30 seconds elapsed during pause, sending update")
                    send_update = True
                elif abs(elapsed_time - last_elapsed) > 10000:
                    self.logger.debug("Detected seek/jump in playback position, sending update")
                    send_update = True

                if send_update:
                    await self._send_timeline_update(queue_id, current_state, elapsed_time)
                    last_update_time = now
                    last_state = current_state
                    last_elapsed = elapsed_time

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.logger.debug("Timeline update task for queue %s was cancelled", queue_id)
        except Exception as exc:
            self.logger.exception("Error in Plex timeline update task: %s", exc)

    async def _send_timeline_update(self, queue_id: str, state: str, position: int) -> None:
        """
        Send a timeline update to the Plex server.

        Builds the necessary parameters and headers and sends a POST request to
        the Plex server's timeline endpoint.

        Args:
            queue_id: The queue ID.
            state: The playback state ('buffering', 'playing', 'paused', or 'stopped').
            position: The current position in milliseconds.
        """
        if queue_id not in self._active_tracks or not self._session:
            return

        track_data = self._active_tracks[queue_id]
        player_id = track_data["player_id"]

        headers = {
            **self._get_client_info(player_id),
            "X-Plex-Token": track_data["token"],
        }

        params = {
            "ratingKey": track_data["rating_key"],
            "key": track_data["key"],
            "state": state,
            "time": position,
            "duration": track_data["duration"],
            "machineIdentifier": track_data["machine_identifier"],
            "protocol": "https",
            "containerKey": f"/library/metadata/{track_data['rating_key']}",
            "commandID": str(uuid.uuid4()),
        }

        url = f"{track_data['server_url']}/:/timeline"

        try:
            self.logger.debug(
                "Sending timeline update to %s: state=%s, time=%s ms, player=%s",
                url,
                state,
                position,
                player_id,
            )
            async with self._session.post(
                url, params=params, headers=headers, timeout=5
            ) as response:
                if response.status != 200:
                    self.logger.warning(
                        "Failed to send Plex timeline update: HTTP %s", response.status
                    )
                    response_text = await response.text()
                    self.logger.debug("Response body: %s", response_text[:200])
                else:
                    self.logger.debug(
                        "Successfully sent Plex timeline update: "
                        "state=%s, time=%s ms, item=%s, player=%s",
                        state,
                        position,
                        track_data["item"].name,
                        player_id,
                    )
        except aiohttp.ClientConnectorError as exc:
            self.logger.warning(
                "Connection error sending Plex timeline update: %s - server: %s",
                exc,
                track_data["server_url"],
            )
        except Exception as exc:
            self.logger.warning("Error sending Plex timeline update: %s", exc)

    def _get_client_info(self, player_id: str) -> dict[str, str]:
        """
        Get client info for a specific player, creating it if needed.

        Ensures each player appears as a unique client in Plex with a stable identifier.

        Args:
            player_id: The player ID.

        Returns:
            A dictionary with X-Plex-* headers for client identification.
        """
        if player_id not in self._client_info:
            player = self.mass.players.get(player_id)
            player_name = player.display_name if player else player_id

            client_id = f"music-assistant-{player_id}-{uuid.uuid4().hex[:8]}"

            self._client_info[player_id] = {
                "X-Plex-Client-Identifier": client_id,
                "X-Plex-Product": "Music Assistant",
                "X-Plex-Device": "MusicAssistant",
                "X-Plex-Platform": "Linux",
                "X-Plex-Device-Name": player_name,
                "X-Plex-Model": f"MA-Player-{player_id[:8]}",
                "X-Plex-Platform-Version": "1.0",
                "X-Plex-Product-Version": "1.0",
                "X-Plex-Provides": "player",
            }
            self.logger.info(
                "Created Plex client for player %s with ID: %s", player_name, client_id
            )

        return self._client_info[player_id]


async def setup(mass: MusicAssistant) -> PlexTimelineReporter:
    """
    Set up the Plex timeline reporter.

    Creates and initializes a PlexTimelineReporter instance.

    Args:
        mass: The MusicAssistant instance.

    Returns:
        The configured PlexTimelineReporter instance.
    """
    reporter = PlexTimelineReporter(mass)
    await reporter.setup()
    return reporter
