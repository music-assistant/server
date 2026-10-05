"""
Autoplay helper for the Player Queues controller.

Autoplay is the single "keep going" switch of a queue; what it appends depends on the media
type of the item that is ending. Music continues with a fresh batch of tracks, a podcast
episode or audiobook with its own successor, and a live source is left alone. This helper
resolves the configured Autoplay mode for a queue and produces the next batch of tracks for
the library- and playlist-based modes. The similar-tracks (radio) modes reuse the controller's
existing dynamic-radio machinery.
"""

from __future__ import annotations

import random
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.constants import CONF_PLAYER_QUEUES, CONF_VALUE_GLOBAL
from music_assistant.controllers.player_queues.constants import (
    CONF_AUTOPLAY_MODE,
    CONF_AUTOPLAY_PLAYLIST,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.player_queues.controller import PlayerQueuesController

# number of library tracks to append on each refill
AUTOPLAY_BATCH_SIZE = 25
# how many of the most recently enqueued items to derive a genre bias from
GENRE_SEED_ITEM_COUNT = 3
# media types that carry a useful genre signal for the library mix
GENRE_SEED_MEDIA_TYPES = (MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST)
# media types that end on their own but have a natural successor of their own: Autoplay
# continues these with the next episode/book instead of appending music
AUTOPLAY_SERIES_MEDIA_TYPES = (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE)
# media types Autoplay does not apply to: a live source has no natural end (stopping it means
# the source stopped, not that the queue ran out) and a sound effect is a one-off
AUTOPLAY_EXCLUDED_MEDIA_TYPES = (
    MediaType.RADIO,
    MediaType.AUDIO_SOURCE,
    MediaType.SOUND_EFFECT,
)


class AutoplayMode(StrEnum):
    """Enum with the available Autoplay (queue refill) strategies."""

    AUTO = "auto"
    SIMILAR = "similar"
    LIBRARY = "library"
    PLAYLIST = "playlist"


AUTOPLAY_MODE_DEFAULT_VALUE = AutoplayMode.AUTO.value


class Autoplay:
    """Resolve the Autoplay mode and produce the next batch of tracks for a queue."""

    def __init__(self, queues: PlayerQueuesController) -> None:
        """
        Initialize the Autoplay helper.

        :param queues: The owning player queues controller.
        """
        self.queues = queues
        self.mass = queues.mass
        self.logger = queues.logger.getChild("autoplay")

    def resolve_mode(self, queue_id: str) -> AutoplayMode:
        """
        Return the configured Autoplay mode for the given queue.

        Follows the global (queue controller) mode when the per-queue value is "global".

        :param queue_id: The queue to read the configured Autoplay mode for.
        """
        raw = self.mass.config.get_effective_player_queue_config_value(
            queue_id, CONF_AUTOPLAY_MODE, AUTOPLAY_MODE_DEFAULT_VALUE
        )
        try:
            return AutoplayMode(str(raw))
        except ValueError:
            return AutoplayMode.AUTO

    async def get_library_tracks(self, queue: PlayerQueue, exclude: set[Track]) -> list[Track]:
        """
        Return a fresh batch of library tracks for the 'infinite library mix' mode.

        Tracks are ordered by random/least-played and biased towards the genre(s) of what
        was recently played, falling back to a whole-library random mix when no genre match
        is available.

        :param queue: The queue being refilled.
        :param exclude: Tracks already present in the queue, to avoid immediate repeats.
        """
        candidates: list[Track] = []
        if genre_ids := await self._collect_genre_ids(queue):
            with suppress(MusicAssistantError):
                candidates += await self.mass.music.tracks.library_items(
                    genre=genre_ids,
                    limit=AUTOPLAY_BATCH_SIZE * 3,
                    order_by="random_play_count",
                    summary=False,
                )
        # top up with a whole-library random mix when the genre selection yields too few usable
        # tracks (gauge on the deduped result so unavailable/excluded matches don't mask a shortfall)
        result = self._dedupe(candidates, exclude)
        if len(result) < AUTOPLAY_BATCH_SIZE:
            with suppress(MusicAssistantError):
                candidates += await self.mass.music.tracks.library_items(
                    limit=AUTOPLAY_BATCH_SIZE * 3,
                    order_by="random_play_count",
                    summary=False,
                )
            result = self._dedupe(candidates, exclude)
        return result

    async def get_playlist_tracks(self, queue: PlayerQueue, exclude: set[Track]) -> list[Track]:
        """
        Return a random batch of tracks from the configured Autoplay playlist.

        :param queue: The queue being refilled.
        :param exclude: Tracks already present in the queue, to avoid immediate repeats.
        """
        uri = self._autoplay_playlist_uri(queue.queue_id)
        if not uri:
            self.logger.warning(
                "Autoplay playlist mode is selected for %s but no playlist is configured",
                queue.display_name,
            )
            return []
        try:
            playlist = await self.mass.music.get_item_by_uri(str(uri))
        except MusicAssistantError as err:
            self.logger.warning("Autoplay playlist %s is not available: %s", uri, err)
            return []
        if not isinstance(playlist, Playlist):
            return []
        tracks = [
            track
            for track in await self.queues.get_playlist_tracks(playlist, start_item=None)
            if isinstance(track, Track)
        ]
        random.shuffle(tracks)
        return self._dedupe(tracks, exclude)

    def _autoplay_playlist_uri(self, queue_id: str) -> ConfigValueType:
        """
        Return the configured Autoplay playlist, taken from the level the mode resolves from.

        A queue that follows the global Autoplay mode also follows the global playlist, so a
        leftover per-queue playlist can't override the global one.

        :param queue_id: The queue to read the Autoplay playlist for.
        """
        raw_mode = self.mass.config.get_raw_player_queue_config_value(
            queue_id, CONF_AUTOPLAY_MODE, CONF_VALUE_GLOBAL
        )
        if raw_mode in (CONF_VALUE_GLOBAL, None):
            return self.mass.config.get_raw_core_config_value(
                CONF_PLAYER_QUEUES, CONF_AUTOPLAY_PLAYLIST
            )
        return self.mass.config.get_raw_player_queue_config_value(queue_id, CONF_AUTOPLAY_PLAYLIST)

    async def _collect_genre_ids(self, queue: PlayerQueue) -> list[int]:
        """Collect library genre ids from the queue's most recently enqueued items."""
        genre_ids: set[int] = set()
        seeds = [
            item
            for item in reversed(self.queues.queue_data(queue.queue_id).enqueued_media_items)
            if item.media_type in GENRE_SEED_MEDIA_TYPES
        ][:GENRE_SEED_ITEM_COUNT]
        for seed in seeds:
            library_id = await self._resolve_library_id(
                seed.media_type, seed.item_id, seed.provider
            )
            if library_id is None:
                continue
            with suppress(MusicAssistantError):
                for genre in await self.mass.music.genres.get_genres_for_media_item(
                    seed.media_type, library_id
                ):
                    with suppress(ValueError, TypeError):
                        genre_ids.add(int(genre.item_id))
        return list(genre_ids)

    async def _resolve_library_id(
        self, media_type: MediaType, item_id: str, provider: str
    ) -> str | int | None:
        """Resolve a media item to its library (database) id, or None when not in the library."""
        if provider == "library":
            return item_id
        controller = self.mass.music.get_controller(media_type)
        with suppress(MusicAssistantError):
            if library_item := await controller.get_library_item_by_prov_id(item_id, provider):
                return library_item.item_id
        return None

    def _dedupe(self, tracks: list[Track], exclude: set[Track]) -> list[Track]:
        """Drop unavailable/duplicate/excluded tracks, capped to the batch size."""
        result: list[Track] = []
        seen: set[Track] = set()
        for track in tracks:
            if not track.available or track in exclude or track in seen:
                continue
            seen.add(track)
            result.append(track)
            if len(result) >= AUTOPLAY_BATCH_SIZE:
                break
        return result
