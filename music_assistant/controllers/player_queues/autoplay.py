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
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType, ProviderFeature, SortField
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Album, Artist, ItemMapping, Playlist, Track

from music_assistant.constants import (
    CONF_PLAYER_QUEUES,
    CONF_VALUE_GLOBAL,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.controllers.music.favorites import without_disliked_tracks
from music_assistant.controllers.player_queues.constants import (
    CONF_AUTOPLAY_MODE,
    CONF_AUTOPLAY_PLAYLIST,
    CONF_DEFAULT_ENQUEUE_ORDER_ARTIST,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    ENQUEUE_ORDER_ARTIST_DEFAULT_VALUE,
    ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
    PROVIDER_ALBUM_SKIPPED_TYPES,
    ArtistOrder,
)
from music_assistant.controllers.player_queues.media_resolver import (
    ALBUM_LOAD_ERRORS,
    album_is_played,
    load_played_namesakes,
)
from music_assistant.helpers.compare import compare_strings
from music_assistant.models.music_provider import provider_fetch_log_level

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import ConfigValueType
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.player_queues.controller import PlayerQueuesController
    from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.mass import MusicAssistant

# number of library tracks to append on each refill
AUTOPLAY_BATCH_SIZE = 25
# how many of the most recently enqueued items to derive a genre bias from
GENRE_SEED_ITEM_COUNT = 3
# albums the whole-albums mode tries before it falls back to the Auto mode
ALBUM_PICK_ATTEMPTS = 3
SIMILAR_ARTISTS_LIMIT = 10
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
    ALBUMS = "albums"


AUTOPLAY_MODE_DEFAULT_VALUE = AutoplayMode.AUTO.value


def albums_mode_available(mass: MusicAssistant) -> bool:
    """
    Return whether the whole-albums mode can play with the artist settings.

    The artist order must play albums, and top tracks are tracks, not albums.

    :param mass: The MusicAssistant instance.
    """
    order = mass.config.get_raw_core_config_value(
        CONF_PLAYER_QUEUES, CONF_DEFAULT_ENQUEUE_ORDER_ARTIST, ENQUEUE_ORDER_ARTIST_DEFAULT_VALUE
    )
    selection = mass.config.get_raw_core_config_value(
        CONF_PLAYER_QUEUES, CONF_DEFAULT_ENQUEUE_SELECT_ARTIST, ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE
    )
    return (
        order in {item.value for item in ArtistOrder if item.plays_albums}
        and selection != "top_tracks"
    )


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
                    sort_field=SortField.RANDOM_PLAY_COUNT,
                    summary=False,
                )
        # top up with a whole-library random mix when the genre selection yields too few usable
        # tracks (gauge on the deduped result so unavailable/excluded matches don't mask a shortfall)
        result = self._dedupe(candidates, exclude)
        if len(result) < AUTOPLAY_BATCH_SIZE:
            with suppress(MusicAssistantError):
                candidates += await self.mass.music.tracks.library_items(
                    limit=AUTOPLAY_BATCH_SIZE * 3,
                    sort_field=SortField.RANDOM_PLAY_COUNT,
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

    async def get_album_tracks(self, queue: PlayerQueue) -> list[ResolvedItem]:
        """
        Return the next whole album of the last track's artist, for the whole-albums mode.

        The artist is the album artist, or the track's artist for a compilation or a track
        without an album. Returns an empty list when there is no album to play, so the caller
        lets Auto pick the next album.

        :param queue: The queue being refilled.
        """
        if not albums_mode_available(self.mass):
            self.logger.debug("Whole albums need an album artist order, using Auto instead")
            return []
        queue_data = self.queues.queue_data(queue.queue_id)
        tracks = [
            item.media_item for item in queue_data.items if isinstance(item.media_item, Track)
        ]
        if not tracks:
            return []
        last = tracks[-1]
        anchor: Album | None = None
        if last.album is not None:
            try:
                anchor = await self.mass.music.albums.get(
                    last.album.item_id, last.album.provider, allow_update_metadata=False
                )
            except ALBUM_LOAD_ERRORS as err:
                self.logger.debug("Unable to load album %s: %s", last.album.name, err)
        if anchor and anchor.artists and not _is_various_artists(anchor.artists[0]):
            seed: Artist | ItemMapping = anchor.artists[0]
        elif last.artists:
            seed = last.artists[0]
        else:
            self.logger.debug("Track %s has no artist, using Auto instead", last.name)
            return []
        try:
            artist = (
                seed
                if isinstance(seed, Artist)
                else await self.mass.music.artists.get(
                    seed.item_id, seed.provider, allow_update_metadata=False
                )
            )
        except ALBUM_LOAD_ERRORS as err:
            self.logger.debug("Unable to load artist %s, using Auto instead: %s", seed.name, err)
            return []
        played = _played_albums(tracks)
        if anchor is not None:
            played = [anchor if album is last.album else album for album in played]
        if album_tracks := await self._next_album_tracks(queue_data, artist, played, anchor):
            return album_tracks
        for similar in (await self._similar_artists(artist))[:ALBUM_PICK_ATTEMPTS]:
            if album_tracks := await self._next_album_tracks(queue_data, similar, played, None):
                return album_tracks
        return []

    async def get_album_of_tracks(
        self, queue: PlayerQueue, tracks: list[Track]
    ) -> list[ResolvedItem]:
        """
        Return the whole album of the first of Auto's tracks whose album hasn't played yet.

        Keeps the whole-albums mode in albums when the artist has none left: Auto picks, and its
        track's album plays. Returns an empty list when no album fits, so the caller keeps Auto's
        tracks.

        :param queue: The queue being refilled.
        :param tracks: Auto's tracks, in the order to try their albums.
        """
        if not albums_mode_available(self.mass):
            return []
        queue_data = self.queues.queue_data(queue.queue_id)
        skipped = _played_albums(
            [item.media_item for item in queue_data.items if isinstance(item.media_item, Track)]
        )
        attempts = 0
        for track in tracks:
            if attempts == ALBUM_PICK_ATTEMPTS:
                break
            if (mapping := track.album) is None or album_is_played([mapping], skipped):
                continue
            try:
                album = await self._auto_album(mapping)
            except ALBUM_LOAD_ERRORS as err:
                self.logger.log(
                    provider_fetch_log_level(err), "Unable to load album %s: %s", mapping.name, err
                )
                skipped.append(mapping)
                attempts += 1
                continue
            await load_played_namesakes(self.mass, self.logger, skipped, [album])
            played = album_is_played([album], skipped)
            # an album is looked at once, also when several of Auto's tracks come from it
            skipped.append(album)
            if played or album.album_type in PROVIDER_ALBUM_SKIPPED_TYPES:
                continue
            attempts += 1
            if album_tracks := await self._album_tracks_to_play(queue_data, [album]):
                return album_tracks
        self.logger.debug("Auto found no album to play, keeping its tracks")
        return []

    async def _auto_album(self, album: Album | ItemMapping) -> Album:
        """Return the library copy of an album of Auto's, or else the album itself, loaded."""
        # Auto's tracks come from providers, while the queue may hold the library copy
        if library_album := await self.mass.music.albums.get_library_item_by_prov_id(
            album.item_id, album.provider
        ):
            return library_album
        if isinstance(album, Album):
            return album
        # a mapping tells no album type, so load the album to know a single
        return await self.mass.music.albums.get(
            album.item_id, album.provider, allow_update_metadata=False
        )

    async def _next_album_tracks(
        self,
        queue_data: PlayerQueueData,
        artist: Artist,
        played: list[Album | ItemMapping],
        anchor: Album | None,
    ) -> list[ResolvedItem]:
        """Return the tracks of the next album of the artist that loads, or an empty list."""
        candidates = await self.queues.artist_album_candidates(artist, played, anchor)
        if candidates is None:
            self.logger.info("Unable to list the albums of %s", artist.name)
            return []
        for copies in candidates[:ALBUM_PICK_ATTEMPTS]:
            if album_tracks := await self._album_tracks_to_play(queue_data, copies):
                return album_tracks
        return []

    async def _similar_artists(self, artist: Artist) -> list[Artist]:
        """Return MA's similar artists of the artist, via its library copy when in the library."""
        library_artist = await self.queues.resolve_library_artist(artist)
        if (
            library_artist is not None
            and artist.provider != "library"
            and not any(mapping.in_library for mapping in library_artist.provider_mappings)
        ):
            library_artist = None
        if library_artist is None:
            provider = self.mass.get_provider(artist.provider)
            # MA logs a warning for every lookup a provider can't answer
            if (
                provider is None
                or ProviderFeature.SIMILAR_ARTISTS not in provider.supported_features
            ):
                return []
        artist = library_artist or artist
        try:
            return await self.mass.music.artists.similar_artists(
                artist.item_id, artist.provider, limit=SIMILAR_ARTISTS_LIMIT
            )
        except ALBUM_LOAD_ERRORS as err:
            self.logger.log(
                provider_fetch_log_level(err),
                "Unable to fetch the similar artists of %s: %s",
                artist.name,
                err,
            )
            return []

    async def _album_tracks_to_play(
        self, queue_data: PlayerQueueData, copies: Sequence[Album]
    ) -> list[ResolvedItem]:
        """
        Return the album's tracks without the disliked ones, or an empty list when it fails.

        Each track keeps the album as its origin, so it plays as the album does on its own.

        :param queue_data: The queue being refilled.
        :param copies: The album's copies, in the order to try them.
        """
        try:
            album_items = await self.queues.album_copy_items(
                copies, queue_data.userid, queue_data.queue.queue_id
            )
        except ALBUM_LOAD_ERRORS as err:
            self.logger.log(
                provider_fetch_log_level(err),
                "Unable to fetch the tracks of album %s: %s",
                copies[0].name,
                err,
            )
            return []
        kept = await without_disliked_tracks(
            self.mass, queue_data.userid, [cast("Track", entry.item) for entry in album_items]
        )
        kept_ids = {id(track) for track in kept}
        album_items = [entry for entry in album_items if id(entry.item) in kept_ids]
        if not album_items:
            self.logger.debug("Album %s has no tracks to play", copies[0].name)
        return album_items

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


def _is_various_artists(artist: Artist | ItemMapping) -> bool:
    """Return whether the artist is the Various Artists placeholder of a compilation."""
    return compare_strings(artist.name, VARIOUS_ARTISTS_NAME) or (
        isinstance(artist, Artist) and artist.mbid == VARIOUS_ARTISTS_MBID
    )


def _played_albums(tracks: list[Track]) -> list[Album | ItemMapping]:
    """Return the albums of the given tracks, once each."""
    return list({track.album.uri: track.album for track in tracks if track.album}.values())
