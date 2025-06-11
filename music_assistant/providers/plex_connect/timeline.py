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
        self._session: aiohttp.ClientSession | None = None
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
        if not current_item.streamdetails:
            return False
        provider = current_item.streamdetails.provider
        if not provider or not provider.startswith(("plex:", "plex--")):
            return False
        return isinstance(current_item.streamdetails.data, dict)

    def _validate_plex_data(self, plex_data: dict) -> bool:
        required_keys = ["rating_key", "server_url", "token", "machine_identifier"]
        if not all(k in plex_data for k in required_keys):
            missing = ", ".join(k for k in required_keys if k not in plex_data)
            self.logger.debug("Missing required Plex data in streamdetails: %s", missing)
            return False
        return True

    def _handle_playing_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem, plex_data: dict
    ) -> None:
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
        self.logger.info("Queue %s is now paused", queue_id)
        if (
            current_item.duration
            and queue.elapsed_time >= current_item.duration - 2
            and not queue.next_item
        ):
            self.logger.info(
                "Track %s ended naturally with no next track, stopping reporting", current_item.name
            )
            self._update_state(queue_id, "stopped")
            self._stop_reporting(queue_id)
        else:
            self._update_state(queue_id, "paused")

    async def _handle_idle_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem
    ) -> None:
        is_pause = (
            current_item.duration
            and queue.elapsed_time > 0
            and queue.elapsed_time < (current_item.duration - 5)
        )
        is_ended = current_item.duration and queue.elapsed_time >= (current_item.duration - 2)
        if is_pause:
            self._handle_idle_paused(queue_id, queue, current_item)
        elif is_ended:
            await self._handle_idle_ended(queue_id, queue, current_item)
        else:
            self._handle_idle_stopped(queue_id)

    def _handle_idle_paused(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem
    ) -> None:
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
        self.logger.info(
            "Track %s appears to have completed naturally (time: %s/%s), "
            "sending stopped state to Plex",
            current_item.name,
            queue.elapsed_time,
            current_item.duration,
        )
        self.logger.info("Arresto riproduzione per fine brano...")
        final_pos = int(current_item.duration * 1000)
        await self._send_stopped_state(queue_id, queue, current_item, final_pos)

    def _handle_idle_stopped(self, queue_id: str) -> None:
        self.logger.info("Queue %s is now stopped", queue_id)
        if queue_id in self._active_tracks:
            self._update_state(queue_id, "stopped")
            self._stop_reporting(queue_id)
        else:
            self._stop_reporting_in_idle(queue_id)

    async def _send_stopped_state(
        self, queue_id: str, queue: PlayerQueue, current_item: QueueItem, final_position: int
    ) -> None:
        if queue_id in self._active_tracks:
            self.mass.create_task(self._send_timeline_update(queue_id, "stopped", final_position))
            self._stop_reporting(queue_id)
        else:
            self._start_reporting(queue_id, queue, current_item)
            self.mass.create_task(self._send_timeline_update(queue_id, "stopped", final_position))
            self._stop_reporting(queue_id)

    def _stop_reporting_in_idle(self, queue_id: str) -> None:
        # No-op for idle without active track: avoid crashing by not looking up queue
        self.logger.debug(
            "Idle stop received for queue %s with no active track, skipping", queue_id
        )

    async def _handle_queue_time_update(self, event) -> None:
        queue_id = event.object_id
        elapsed = event.data
        if not queue_id or queue_id not in self._active_tracks or elapsed is None:
            return
        prev = self._active_tracks[queue_id].get("elapsed_time", 0)
        self._active_tracks[queue_id]["elapsed_time"] = elapsed
        if int(elapsed) % 5 == 0:
            self.logger.debug("Time update for queue %s: %s seconds", queue_id, int(elapsed))
        track_dur = self._active_tracks[queue_id]["duration"] / 1000
        if prev > (track_dur - 5) and elapsed < 2:
            self.logger.info("Detected track completion or restart - sending stopped state to Plex")
            final_pos = int(track_dur * 1000)
            self.logger.info("Arresto riproduzione per fine brano...")
            for _ in range(3):
                await self._send_timeline_update(queue_id, "stopped", final_pos)
                await asyncio.sleep(0.5)
            await asyncio.sleep(1)

    async def _handle_player_update(self, event) -> None:
        player = event.data
        if not player:
            return
        pid = player.player_id
        for qid, data in list(self._active_tracks.items()):
            if data.get("player_id") == pid:
                if not player.available or player.powered is False:
                    self.logger.info("Player %s is no longer available or powered off", pid)
                    pause_pos = int(data["elapsed_time"] * 1000)
                    self.logger.info("Arresto riproduzione per spegnimento player...")
                    await self._send_timeline_update(qid, "stopped", pause_pos)
                    await asyncio.sleep(1)
                    self._stop_reporting(qid)

    def _start_reporting(self, queue_id: str, queue: PlayerQueue, item: QueueItem) -> None:
        if (
            queue_id in self._active_tracks
            and self._active_tracks[queue_id].get("item_id") == item.queue_item_id
        ):
            self.logger.debug("Updating state to 'playing' for existing track %s", item.name)
            self._update_state(queue_id, "playing")
            return
        if queue_id in self._active_tracks:
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
        if queue_id not in self._active_tracks:
            return
        current = self._active_tracks[queue_id]
        if current["state"] != state:
            current["state"] = state
            self.logger.info(
                "Updated Plex timeline state to %s for %s", state, current["item"].name
            )
            elapsed = int(current["elapsed_time"] * 1000)
            if state == "stopped":
                self.mass.create_task(self._force_stop_state_update(queue_id, elapsed))
            else:
                self.mass.create_task(self._send_repeated_updates(queue_id, state, elapsed))

    def _stop_reporting(self, queue_id: str) -> None:
        if queue_id not in self._active_tracks:
            return
        data = self._active_tracks[queue_id]
        if data["state"] != "stopped":
            self.logger.debug("Sending final 'stopped' state to Plex")
            self.mass.create_task(self._force_send_stopped_state(queue_id, data))
        if queue_id in self._tasks and not self._tasks[queue_id].done():
            self._tasks[queue_id].cancel()
        name = data.get("item").name if "item" in data else "unknown track"
        del self._active_tracks[queue_id]
        self._tasks.pop(queue_id, None)
        self.logger.info("Stopped Plex timeline reporting for %s (queue %s)", name, queue_id)

    async def _force_stop_state_update(self, queue_id: str, elapsed: int) -> None:
        for _ in range(3):
            await self._send_timeline_update(queue_id, "stopped", elapsed)
            await asyncio.sleep(0.5)
        self._stop_reporting(queue_id)

    async def _send_repeated_updates(self, queue_id: str, state: str, elapsed: int) -> None:
        for _ in range(3):
            await self._send_timeline_update(queue_id, state, elapsed)
            await asyncio.sleep(0.5)

    async def _force_send_stopped_state(self, queue_id: str, track_data: dict) -> None:
        elapsed = int(track_data["elapsed_time"] * 1000)
        self.logger.info("Arresto riproduzione...")
        for _ in range(3):
            try:
                await self._send_timeline_update(queue_id, "stopped", elapsed)
                await asyncio.sleep(0.5)
            except Exception as exc:
                self.logger.warning("Error sending final stopped state: %s", exc)

    async def _timeline_update_task(self, queue_id: str) -> None:
        try:
            self.logger.debug("Sending initial buffering state to Plex")
            await self._send_timeline_update(queue_id, "buffering", 0)
            await asyncio.sleep(0.5)
            self.logger.debug("Sending initial playing state to Plex")
            await self._send_timeline_update(queue_id, "playing", 0)
            last_update = time.time()
            last_state = "playing"
            last_elapsed = 0
            while queue_id in self._active_tracks:
                now = time.time()
                elapsed = int(self._active_tracks[queue_id]["elapsed_time"] * 1000)
                state = self._active_tracks[queue_id]["state"]
                send = (
                    state != last_state
                    or (state == "playing" and now - last_update >= 5)
                    or (state == "paused" and now - last_update >= 30)
                    or abs(elapsed - last_elapsed) > 10000
                )
                if send:
                    await self._send_timeline_update(queue_id, state, elapsed)
                    last_update = now
                    last_state = state
                    last_elapsed = elapsed
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.logger.debug("Timeline update task for queue %s was cancelled", queue_id)
        except Exception as exc:
            self.logger.exception("Error in Plex timeline update task: %s", exc)

    async def _send_timeline_update(self, queue_id: str, state: str, position: int) -> None:
        if queue_id not in self._active_tracks or not self._session:
            return
        data = self._active_tracks[queue_id]
        headers = {**self._get_client_info(data["player_id"]), "X-Plex-Token": data["token"]}
        params = {
            "ratingKey": data["rating_key"],
            "key": data["key"],
            "state": state,
            "time": position,
            "duration": data["duration"],
            "machineIdentifier": data["machine_identifier"],
            "protocol": "https",
            "containerKey": f"/library/metadata/{data['rating_key']}",
            "commandID": str(uuid.uuid4()),
        }
        url = f"{data['server_url']}/:/timeline"
        try:
            self.logger.debug(
                "Sending timeline update to %s: state=%s, time=%s ms, player=%s",
                url,
                state,
                position,
                data["player_id"],
            )
            async with self._session.post(
                url, params=params, headers=headers, timeout=5
            ) as response:
                if response.status != 200:
                    self.logger.warning(
                        "Failed to send Plex timeline update: HTTP %s", response.status
                    )
                    text = await response.text()
                    self.logger.debug("Response body: %s", text[:200])
                else:
                    self.logger.debug(
                        "Successfully sent Plex timeline update: state=%s, time=%s ms, "
                        "item=%s, player=%s",
                        state,
                        position,
                        data["item"].name,
                        data["player_id"],
                    )
        except aiohttp.ClientConnectorError as exc:
            self.logger.warning(
                "Connection error sending Plex timeline update: %s - server: %s",
                exc,
                data["server_url"],
            )
        except Exception as exc:
            self.logger.warning("Error sending Plex timeline update: %s", exc)

    def _get_client_info(self, player_id: str) -> dict[str, str]:
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
