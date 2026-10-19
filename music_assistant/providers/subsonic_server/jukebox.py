"""
Subsonic jukebox mode, played on the queue of a Music Assistant player.

The jukebox playlist is a view on the tracks of the player queue: a list of queue item ids.
Subsonic indexes refer to that list, so removing a played track from the jukebox playlist shifts
the indexes the client sees while the queue keeps it as history. When the queue no longer plays
an item of the view (it was changed from another client), the view mirrors the queue again.
Queue items that are not tracks, such as radio stations, are left out of the view, since
Subsonic clients can only show tracks.

Subsonic clients take ``playing: false`` as the end of the track, while the queue briefly stops
playing whenever it (re)starts an item. For a few seconds after the jukebox starts or skips,
the status therefore reports the requested state rather than the momentary one.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.media_items import Track

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    has_player_access,
    has_scope,
)

from . import mappers
from .constants import ErrorCode
from .ids import parse_subsonic_id
from .response import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import ItemMapping, MediaItemType

    from . import SubsonicServerProvider
    from .request import EndpointHandler, SubsonicRequest

QUEUE_PAGE_SIZE = 500
START_GRACE_SECONDS = 5.0
SEEK_TOLERANCE_SECONDS = 2


class JukeboxEndpoints:
    """The ``jukeboxControl`` endpoint."""

    def __init__(self, provider: SubsonicServerProvider) -> None:
        """
        Initialize the endpoint.

        :param provider: The plugin instance, which knows the jukebox player.
        """
        self.provider = provider
        self.mass = provider.mass
        self._entries: list[str] = []
        self._entries_queue_id: str | None = None
        self._started_at: float | None = None
        # one jukebox request at a time: each one reads the queue, changes it and reads it back
        self._lock = asyncio.Lock()

    def endpoints(self) -> dict[str, EndpointHandler]:
        """Return the handlers of this group, keyed by endpoint name."""
        return {"jukeboxControl": self._jukebox_control}

    async def _jukebox_control(self, request: SubsonicRequest) -> dict[str, Any]:
        queue_id = self.provider.jukebox_player_id
        if queue_id is None:
            raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "Jukebox mode is not enabled")
        if not has_scope(request.user, Scope.QUEUES_CONTROL):
            raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "User may not control the jukebox")
        action = request.require("action")
        player = self.mass.players.get_player(queue_id)
        if player is None or not player.available:
            # clients copy the reported gain into their volume, so there must be a real one
            raise SubsonicError(ErrorCode.GENERIC, "The jukebox player is not available")
        if not has_player_access(request.user, queue_id, player):
            raise SubsonicError(ErrorCode.NOT_AUTHORIZED, "User may not use the jukebox player")
        async with self._lock:
            self._sync_entries(queue_id)
            await self._run_action(queue_id, action, request)
            status = self._status(queue_id)
            if action == "get":
                status["entry"] = self._entry_songs(queue_id)
                return {"jukeboxPlaylist": status}
            return {"jukeboxStatus": status}

    async def _run_action(self, queue_id: str, action: str, request: SubsonicRequest) -> None:
        match action:
            case "get" | "status":
                return
            case "set":
                uris = _track_uris(request.get_all("id"))
                self.mass.player_queues.clear(queue_id)
                self._entries = []
                self._started_at = None
                await self._add(queue_id, uris)
            case "add":
                await self._add(queue_id, _track_uris(request.get_all("id")))
            case "clear":
                self.mass.player_queues.clear(queue_id)
                self._entries = []
                self._started_at = None
            case "remove":
                self._remove(queue_id, self._entry_index(request.get_int("index", -1)))
            case "skip":
                index = self._entry_index(request.get_int("index", -1))
                offset = request.get_int("offset", 0)
                if not self._is_playing_at(queue_id, self._entries[index], offset):
                    await self.mass.player_queues.play_index(
                        queue_id, self._entries[index], seek_position=offset
                    )
                    self._started_at = time.monotonic()
            case "start":
                await self.mass.player_queues.play(queue_id)
                self._started_at = time.monotonic()
            case "stop":
                await self.mass.player_queues.pause(queue_id)
                self._started_at = None
            case "setGain":
                gain = _parse_gain(request.require("gain"))
                await self.mass.players.cmd_volume_set(queue_id, round(gain * 100))
            case _:
                raise SubsonicError(ErrorCode.GENERIC, f"Unsupported jukebox action: {action}")

    async def _add(self, queue_id: str, uris: list[str]) -> None:
        if not uris:
            return
        known = set(self._track_item_ids(queue_id))
        media: list[MediaItemType | ItemMapping | str] = list(uris)
        await self.mass.player_queues.play_media(queue_id, media, option=QueueOption.ADD)
        self._entries.extend(
            item_id for item_id in self._track_item_ids(queue_id) if item_id not in known
        )

    def _remove(self, queue_id: str, index: int) -> None:
        queue_item_id = self._entries[index]
        queue = self.mass.player_queues.get(queue_id)
        queue_index = self.mass.player_queues.index_by_id(queue_id, queue_item_id)
        current_index = queue.current_index if queue else None
        if queue_index is not None and (current_index is None or queue_index >= current_index):
            # the queue refuses to delete an item it already loaded for playback
            self.mass.player_queues.delete_item(queue_id, queue_item_id)
            if self.mass.player_queues.index_by_id(queue_id, queue_item_id) is not None:
                raise SubsonicError(ErrorCode.GENERIC, "The track is already loaded for playback")
        # a played track stays in the queue history and only leaves the jukebox playlist
        self._entries.pop(index)

    def _sync_entries(self, queue_id: str) -> None:
        item_ids = self._track_item_ids(queue_id)
        if self._entries_queue_id != queue_id:
            self._entries_queue_id = queue_id
            self._entries = item_ids
            return
        queued = set(item_ids)
        self._entries = [entry for entry in self._entries if entry in queued]
        queue = self.mass.player_queues.get(queue_id)
        current = queue.current_item if queue else None
        if current is not None and current.queue_item_id not in self._entries:
            self._entries = item_ids

    def _status(self, queue_id: str) -> dict[str, Any]:
        queue = self.mass.player_queues.get(queue_id)
        player = self.mass.players.get_player(queue_id)
        current = queue.current_item if queue else None
        current_index = -1
        if current is not None and current.queue_item_id in self._entries:
            current_index = self._entries.index(current.queue_item_id)
        volume = player.volume_level if player else None
        starting = (
            self._started_at is not None
            and time.monotonic() - self._started_at < START_GRACE_SECONDS
        )
        return {
            "currentIndex": current_index,
            "playing": bool(queue and queue.state == PlaybackState.PLAYING) or starting,
            "gain": round(volume / 100, 2) if volume is not None else 0.0,
            "position": int(queue.corrected_elapsed_time) if queue and current else 0,
        }

    def _entry_songs(self, queue_id: str) -> list[dict[str, Any]]:
        songs = []
        for queue_item_id in self._entries:
            queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
            if queue_item is not None and isinstance(queue_item.media_item, Track):
                songs.append(mappers.song(queue_item.media_item))
        return songs

    def _is_playing_at(self, queue_id: str, queue_item_id: str, offset: int) -> bool:
        queue = self.mass.player_queues.get(queue_id)
        return bool(
            queue
            and queue.state == PlaybackState.PLAYING
            and queue.current_item
            and queue.current_item.queue_item_id == queue_item_id
            and abs(queue.corrected_elapsed_time - offset) <= SEEK_TOLERANCE_SECONDS
        )

    def _entry_index(self, index: int) -> int:
        if not 0 <= index < len(self._entries):
            raise SubsonicError(ErrorCode.GENERIC, f"Invalid jukebox index: {index}")
        return index

    def _track_item_ids(self, queue_id: str) -> list[str]:
        item_ids: list[str] = []
        offset = 0
        while True:
            page = self.mass.player_queues.items(queue_id, limit=QUEUE_PAGE_SIZE, offset=offset)
            item_ids.extend(
                item.queue_item_id for item in page if isinstance(item.media_item, Track)
            )
            if len(page) < QUEUE_PAGE_SIZE:
                return item_ids
            offset += QUEUE_PAGE_SIZE


def _track_uris(subsonic_ids: list[str]) -> list[str]:
    return [parse_subsonic_id(value, MediaType.TRACK).uri for value in subsonic_ids]


def _parse_gain(value: str) -> float:
    try:
        gain = float(value)
    except ValueError as err:
        raise SubsonicError(ErrorCode.GENERIC, f"Invalid gain: {value}") from err
    return min(max(gain, 0.0), 1.0)
