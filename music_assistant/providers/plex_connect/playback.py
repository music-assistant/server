"""Playback control command handlers for Plex remote control."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.enums import RepeatMode

from .parsing import plex_key_for_item

if TYPE_CHECKING:
    from music_assistant.providers.plex import PlexProvider

LOGGER = logging.getLogger(__name__)


class PlaybackMixin:
    """Mixin providing playback control command handlers."""

    if TYPE_CHECKING:
        provider: PlexProvider
        _ma_player_id: str | None
        _updating_from_plex: bool
        play_queue_id: str | None
        play_queue_item_ids: dict[int, int]
        _last_synced_ma_queue_length: int
        _last_synced_ma_queue_keys: list[str]

        async def _broadcast_timeline(self) -> None: ...

        async def _create_plex_playqueue_from_ma(self) -> None: ...

        def _remember_synced_queue(
            self, player_id: str, keys: list[str] | None = None
        ) -> list[str]: ...

    async def _seek_to_offset_after_playback(self, player_id: str, offset: int) -> None:
        """
        Seek to the specified offset after playback starts.

        :param player_id: The player ID to seek on.
        :param offset: The offset in milliseconds.
        """
        queue = self.provider.mass.player_queues.get(player_id)
        if queue and queue.current_item:
            try:
                await self.provider.mass.players.cmd_seek(player_id, offset // 1000)
            except Exception as e:
                LOGGER.debug(f"Could not seek to offset {offset}ms: {e}")
        else:
            LOGGER.warning("Queue not ready for seeking after timeout")

    async def handle_pause(self, request: web.Request) -> web.Response:
        """Handle pause command."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.players.cmd_pause(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_play(self, request: web.Request) -> web.Response:
        """Handle play/resume command."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.players.cmd_play(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_stop(self, request: web.Request) -> web.Response:
        """Handle stop command - stops playback and clears the queue."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                self.provider.mass.player_queues.clear(self._ma_player_id)
                self.play_queue_id = None
                self.play_queue_item_ids = {}
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_skip_next(self, request: web.Request) -> web.Response:
        """Handle skip next command."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.player_queues.next(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_skip_previous(self, request: web.Request) -> web.Response:
        """Handle skip previous command."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.player_queues.previous(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_step_forward(self, request: web.Request) -> web.Response:
        """Handle step forward command (small skip forward, 30 seconds)."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                queue = self.provider.mass.player_queues.get(self._ma_player_id)
                if queue:
                    new_position = queue.corrected_elapsed_time + 30
                    if queue.current_item and queue.current_item.media_item:
                        max_duration = queue.current_item.media_item.duration or new_position
                        new_position = min(new_position, max_duration)
                    await self.provider.mass.players.cmd_seek(self._ma_player_id, int(new_position))
                    await asyncio.sleep(0.1)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_step_back(self, request: web.Request) -> web.Response:
        """Handle step back command (small skip backward, 10 seconds)."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                queue = self.provider.mass.player_queues.get(self._ma_player_id)
                if queue:
                    new_position = max(0, queue.corrected_elapsed_time - 10)
                    await self.provider.mass.players.cmd_seek(self._ma_player_id, int(new_position))
                    await asyncio.sleep(0.1)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_seek_to(self, request: web.Request) -> web.Response:
        """Handle seek command."""
        self._updating_from_plex = True
        try:
            offset_ms = int(request.query.get("offset", 0))
            if self._ma_player_id:
                await self.provider.mass.players.cmd_seek(self._ma_player_id, int(offset_ms / 1000))
                await asyncio.sleep(0.1)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_skip_to(self, request: web.Request) -> web.Response:
        """Handle skip to specific queue item."""
        key = request.query.get("key")
        if not self._ma_player_id or not key:
            return web.Response(status=400, text="Missing player ID or key")

        self._updating_from_plex = True
        try:
            ma_index = None

            if key.isdigit():
                # Key is a play queue item ID — look up MA queue index
                play_queue_item_id = int(key)
                for idx, pq_item_id in self.play_queue_item_ids.items():
                    if pq_item_id == play_queue_item_id:
                        ma_index = idx
                        break

                if ma_index is None:
                    LOGGER.warning(
                        f"Could not find MA queue index for play queue item ID: "
                        f"{play_queue_item_id}"
                    )
                    return web.Response(status=404, text="Queue item not found")

                LOGGER.info(
                    f"Skipping to queue index {ma_index} (play queue item ID: {play_queue_item_id})"
                )
            else:
                # Key is a library path — find track in MA queue by Plex key
                queue_items = self.provider.mass.player_queues.items(self._ma_player_id)
                if not queue_items:
                    return web.Response(status=404, text="Queue is empty")

                for idx, item in enumerate(queue_items):
                    if plex_key_for_item(item.media_item, self.provider.instance_id) == key:
                        ma_index = idx
                        break

                if ma_index is None:
                    LOGGER.warning(f"Could not find track with key {key} in MA queue")
                    return web.Response(status=404, text="Track not found in queue")

                LOGGER.info(f"Skipping to queue index {ma_index} (track key: {key})")

            await self.provider.mass.player_queues.play_index(self._ma_player_id, ma_index)
            await self._broadcast_timeline()
            return web.Response(status=200)

        except Exception:
            LOGGER.exception("Error handling skipTo")
            return web.Response(status=500, text="Internal error")
        finally:
            self._updating_from_plex = False

    async def handle_set_parameters(self, request: web.Request) -> web.Response:
        """Handle parameter changes (volume, shuffle, repeat)."""
        if not self._ma_player_id:
            return web.Response(status=200)

        self._updating_from_plex = True
        shuffle_changed = False
        try:
            if "volume" in request.query:
                volume = int(request.query["volume"])
                # Use group_volume so a group/syncgroup adjusts its members relatively
                # (matching the group-aware volume reported in the timeline). For a solo
                # player this falls back to a normal volume set.
                await self.provider.mass.players.cmd_group_volume(self._ma_player_id, volume)

            if "shuffle" in request.query:
                shuffle = request.query["shuffle"] == "1"
                await self.provider.mass.player_queues.set_shuffle(self._ma_player_id, shuffle)
                shuffle_changed = True

            if "repeat" in request.query:
                repeat_value = int(request.query["repeat"])
                if repeat_value == 0:
                    await self.provider.mass.player_queues.set_repeat(
                        self._ma_player_id, RepeatMode.OFF
                    )
                elif repeat_value == 1:
                    await self.provider.mass.player_queues.set_repeat(
                        self._ma_player_id, RepeatMode.ONE
                    )
                elif repeat_value == 2:
                    await self.provider.mass.player_queues.set_repeat(
                        self._ma_player_id, RepeatMode.ALL
                    )

            await self._broadcast_timeline()
        finally:
            self._updating_from_plex = False

        if shuffle_changed:
            # MA shuffles the queue asynchronously; give it a moment then sync the
            # new order back to Plex so Plexamp sees the updated play queue.
            await asyncio.sleep(0.2)
            await self._create_plex_playqueue_from_ma()
            self._remember_synced_queue(self._ma_player_id)

        return web.Response(status=200)
