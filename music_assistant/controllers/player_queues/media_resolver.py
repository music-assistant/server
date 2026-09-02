"""
Media resolution for the Player Queues controller.

Resolves source media items (artist, album, genre, playlist, audiobook, podcast, browse folder)
into the concrete tracks / playable items that enqueueing them produces, honoring the user's
per-type selection preferences. Pure media->tracks logic dispatched out of the controller; it reads
config and the music controller via its owning controller, and holds no per-queue state.
"""

from __future__ import annotations

import asyncio
import random
from types import NoneType
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ArtistType, MediaType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    Genre,
    ItemMapping,
    MediaCollection,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    Radio,
    Track,
    UniqueList,
)

from music_assistant.constants import PlaylistPlayableItem
from music_assistant.controllers.player_queues.constants import (
    CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
    ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
)
from music_assistant.controllers.player_queues.helpers import sort_tracks
from music_assistant.controllers.webserver.helpers.auth_middleware import ImpersonatedUser
from music_assistant.helpers.collections import (
    get_collection_item_id,
    get_collection_item_media_type_from_item_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant.controllers.player_queues.controller import PlayerQueuesController

_LATEST_EPISODE_KEYWORDS = frozenset({"latest", "newest"})
_START_ITEM_SUBSTRING_MIN_LEN = 3


def _start_item_matches(start_item: str, item: Any) -> bool:
    """
    Return whether `item` satisfies a `start_item` directive.

    :param start_item: Exact `item_id` / `uri`, or a case-insensitive
        substring of the item's name.
    :param item: Candidate media item.
    """
    if start_item in (getattr(item, "item_id", None), getattr(item, "uri", None)):
        return True
    if len(start_item) < _START_ITEM_SUBSTRING_MIN_LEN:
        return False
    name = getattr(item, "name", None)
    return bool(name and start_item.lower() in name.lower())


class MediaResolver:
    """Resolve source media items into the concrete tracks/playable items to enqueue."""

    def __init__(self, queues: PlayerQueuesController) -> None:
        """
        Initialize the media resolver.

        :param queues: The owning player queues controller.
        """
        self.queues = queues
        self.mass = queues.mass
        self.logger = queues.logger.getChild("media_resolver")

    async def get_tracks_for_playback(self, media_item: MediaItemType) -> list[Track]:
        """
        Return the playable tracks for a media item, honoring the user's selection preferences.

        Resolves an umbrella media item (artist, album, genre, playlist) to the tracks that
        playing it would enqueue; a track resolves to itself, other types to an empty list.

        :param media_item: The media item to resolve to playable tracks.
        """
        if media_item.media_type == MediaType.TRACK:
            return [cast("Track", media_item)]
        if media_item.media_type == MediaType.ALBUM:
            return await self.get_album_tracks(cast("Album", media_item), None)
        if media_item.media_type == MediaType.ARTIST:
            return await self.get_artist_tracks(cast("Artist", media_item))
        if media_item.media_type == MediaType.GENRE:
            return await self.get_genre_tracks(cast("Genre", media_item), None)
        if media_item.media_type == MediaType.PLAYLIST:
            return [
                track
                for track in await self.get_playlist_tracks(cast("Playlist", media_item), None)
                if isinstance(track, Track)
            ]
        return []

    async def get_artist_tracks(self, artist: Artist) -> list[Track]:
        """Return the tracks to play for the given artist, based on user preference."""
        artist_items_conf = self.mass.config.get_raw_core_config_value(
            self.queues.domain,
            CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
            ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
        )
        self.logger.info(
            "Fetching tracks to play for artist %s (selection: %s)", artist.name, artist_items_conf
        )
        if artist_items_conf == "top_tracks":
            tracks = await self.mass.music.artists.top_tracks(artist.item_id, artist.provider)
            random.shuffle(tracks)
            return tracks
        # legacy "library_album_tracks" also resolves to the in-library tracks
        if artist_items_conf in ("library_tracks", "library_album_tracks"):
            tracks = await self._library_artist_tracks(artist)
            random.shuffle(tracks)
            return tracks
        if artist_items_conf == "prefer_library":
            tracks = await self._library_artist_tracks(artist)
            if not tracks:
                tracks = await self.mass.music.artists.top_tracks(artist.item_id, artist.provider)
            random.shuffle(tracks)
            return tracks
        result: list[Track] = []
        seen: set[str] = set()
        sources = await asyncio.gather(
            self._library_artist_tracks(artist),
            self._provider_artist_tracks(artist),
            return_exceptions=True,
        )
        for source in sources:
            if isinstance(source, BaseException):
                self.logger.warning(
                    "Error resolving some tracks for artist %s", artist.name, exc_info=source
                )
                continue
            for track in source:
                unique_id = f"{track.name}.{track.version}"
                if unique_id in seen:
                    continue
                seen.add(unique_id)
                result.append(track)
        random.shuffle(result)
        return result

    async def get_album_tracks(
        self,
        album: Album,
        start_item: str | None,
        sort_by: str | None = None,
        keep_preceding_items: bool = False,
    ) -> list[Track]:
        """
        Return tracks for given album, based on user preference.

        :param album: The album to fetch the tracks for.
        :param start_item: Optional item_id/uri of the track to start from.
        :param sort_by: Optional sort key to order the tracks by before applying start_item.
        :param keep_preceding_items: Move the tracks before start_item behind the rest instead
            of dropping them, so the full album is returned with start_item first.
        """
        album_items_conf = self.mass.config.get_raw_core_config_value(
            self.queues.domain,
            CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
            ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
        )
        result: list[Track] = []
        self.logger.info(
            "Fetching tracks to play for album %s",
            album.name,
        )
        for album_track in await self.mass.music.albums.tracks(
            item_id=album.item_id,
            provider_instance_id_or_domain=album.provider,
            in_library_only=album_items_conf == "library_tracks",
        ):
            if not album_track.available:
                continue
            result.append(album_track)
        if sort_by and sort_by != "track_number":
            result = sort_tracks(result, sort_by)
        if start_item is not None:
            for idx, track in enumerate(result):
                if start_item in (track.item_id, track.uri):
                    return result[idx:] + (result[:idx] if keep_preceding_items else [])
            return []
        return result

    async def get_genre_tracks(self, genre: Genre, start_item: str | None) -> list[Track]:
        """
        Return tracks for given genre, based on alias mappings.

        Limits results to avoid loading thousands of tracks for broad genres.
        Directly mapped tracks are fetched with random ordering, then supplemented
        with tracks from a limited set of mapped albums and artists.
        """
        result: list[Track] = []
        start_item_found = False
        self.logger.info(
            "Fetching tracks to play for genre %s",
            genre.name,
        )
        tracks, albums, artists = await self.mass.music.genres.mapped_media(
            genre,
            track_limit=25,
            album_limit=5,
            artist_limit=5,
            order_by="random",
        )

        for genre_track in tracks:
            if not genre_track.available:
                continue
            if start_item in (genre_track.item_id, genre_track.uri):
                start_item_found = True
            if start_item is not None and not start_item_found:
                continue
            result.append(genre_track)

        for album in albums:
            album_tracks = await self.get_album_tracks(album, None)
            result.extend(album_tracks[:5])

        for artist in artists:
            artist_tracks = await self.get_artist_tracks(artist)
            result.extend(artist_tracks[:5])
        return result

    async def get_dynamic_source_tracks(self, item: MediaItemType) -> list[Track]:
        """
        Return a fresh batch of tracks for a dynamic playlist or radio station.

        :param item: The dynamic source to fetch the next batch for.
        """
        if isinstance(item, Radio):
            return await self.mass.music.radio.dynamic_tracks(item)
        if isinstance(item, Playlist):
            tracks = await self.get_playlist_tracks(item, start_item=None)
            return [track for track in tracks if isinstance(track, Track)]
        return []

    async def get_playlist_tracks(
        self,
        playlist: Playlist,
        start_item: str | None,
        sort_by: str | None = None,
        keep_preceding_items: bool = False,
    ) -> list[PlaylistPlayableItem]:
        """
        Return tracks for given playlist, based on user preference.

        :param playlist: The playlist to fetch the tracks for.
        :param start_item: Optional item_id/uri/name of the track to start from.
        :param sort_by: Optional sort key to order the tracks by before applying start_item.
        :param keep_preceding_items: Move the tracks before start_item behind the rest instead
            of dropping them, so the full playlist is returned with start_item first.
        """
        result: list[PlaylistPlayableItem] = []
        self.logger.info(
            "Fetching tracks to play for playlist %s",
            playlist.name,
        )
        force_refresh = playlist.is_dynamic
        needs_sort = sort_by is not None and sort_by != "position"
        # Fast path: no re-sort needed and the preceding tracks are dropped anyway, so
        # skip-until-found in a single pass and never materialize huge playlists when
        # starting near the end.
        if not needs_sort and not keep_preceding_items:
            start_item_found = False
            async for playlist_track in self.mass.music.playlists.tracks(
                playlist.item_id,
                playlist.provider,
                force_refresh=force_refresh,
                allow_dynamic_tracks=playlist.is_dynamic,
            ):
                if not playlist_track.available:
                    continue
                if start_item is not None and _start_item_matches(start_item, playlist_track):
                    start_item_found = True
                if start_item is not None and not start_item_found:
                    continue
                result.append(playlist_track)
            return result
        # Sort/rotate path: must materialize all tracks before sorting or rotating, then slice.
        async for playlist_track in self.mass.music.playlists.tracks(
            playlist.item_id,
            playlist.provider,
            force_refresh=force_refresh,
            allow_dynamic_tracks=playlist.is_dynamic,
        ):
            if not playlist_track.available:
                continue
            result.append(playlist_track)
        if needs_sort:
            result = sort_tracks(result, cast("str", sort_by))
        if start_item is not None:
            for idx, track in enumerate(result):
                if _start_item_matches(start_item, track):
                    return result[idx:] + (result[:idx] if keep_preceding_items else [])
            return []
        return result

    async def get_audiobook_resume_point(
        self, audio_book: Audiobook, chapter: str | int | None = None, userid: str | None = None
    ) -> int:
        """Return resume point (in milliseconds) for given audio book."""
        self.logger.debug(
            "Fetching resume point to play for audio book %s",
            audio_book.name,
        )
        if chapter is not None:
            # user explicitly selected a chapter to play
            start_chapter = int(chapter) if isinstance(chapter, str) else chapter
            if chapters := audio_book.metadata.chapters:
                if _chapter := next((x for x in chapters if x.position == start_chapter), None):
                    return int(_chapter.start * 1000)
            raise InvalidDataError(
                f"Unable to resolve chapter to play for Audiobook {audio_book.name}"
            )
        full_played, resume_position_ms = await self.mass.music.get_resume_position(
            audio_book, userid=userid
        )
        return 0 if full_played else resume_position_ms

    async def get_next_podcast_episodes(
        self,
        podcast: Podcast | None,
        episode: PodcastEpisode | str | None,
        userid: str | None = None,
        start_from_beginning: bool = False,
    ) -> UniqueList[PodcastEpisode]:
        """
        Return the next episode(s) and resume point for the given podcast.

        :param podcast: Podcast to enqueue, or `None` if `episode` is a
            concrete `PodcastEpisode`.
        :param episode: A concrete `PodcastEpisode`, an `item_id` / `uri`,
            a case-insensitive substring of an episode name, or one of the
            reserved lowercase keywords `"latest"` / `"newest"`.
        :param userid: User whose resume position should be applied.
        :param start_from_beginning: When True, the resolved episode starts at position 0,
            ignoring any saved resume position. The stored progress itself is left untouched.
        """
        if podcast is None and isinstance(episode, str | NoneType):
            raise InvalidDataError("Either podcast or episode must be provided")
        if podcast is None:
            # single podcast episode requested
            assert isinstance(episode, PodcastEpisode)  # checked above
            self.logger.debug(
                "Fetching resume point to play for Podcast episode %s",
                episode.name,
            )
            await self._set_episode_resume_point(episode, userid, start_from_beginning)
            return UniqueList([episode])
        # podcast with optional start episode requested
        self.logger.debug(
            "Fetching episode(s) and resume point to play for Podcast %s",
            podcast.name,
        )
        all_episodes = [
            x async for x in self.mass.music.podcasts.episodes(podcast.item_id, podcast.provider)
        ]
        all_episodes.sort(key=lambda x: x.position)
        # Require exact case and keyword match to minimise false positives.
        if isinstance(episode, str) and episode in _LATEST_EPISODE_KEYWORDS:
            # the newest episode holds the highest position, whatever order the provider
            # lists its episodes in. A tie at the top resolves to the first one listed
            latest = max(all_episodes, key=lambda x: x.position, default=None)
            if latest is None:
                raise InvalidDataError(
                    f"Unable to resolve episode to play for Podcast {podcast.name}"
                )
            await self._set_episode_resume_point(latest, userid, start_from_beginning)
            return UniqueList([latest])
        # if a episode was provided, a user explicitly selected a episode to play
        # so we need to find the index of the episode in the list
        resolved_episode: PodcastEpisode | None = None
        if isinstance(episode, PodcastEpisode):
            resolved_episode = next((x for x in all_episodes if x.uri == episode.uri), None)
            if resolved_episode:
                # ensure we have accurate resume info
                (
                    fully_played,
                    resume_position_ms,
                ) = await self.mass.music.get_resume_position(resolved_episode, userid=userid)
                resolved_episode.resume_position_ms = 0 if fully_played else resume_position_ms
        elif isinstance(episode, str):
            resolved_episode = next(
                (x for x in all_episodes if _start_item_matches(episode, x)), None
            )
            if resolved_episode:
                # ensure we have accurate resume info
                (
                    fully_played,
                    resume_position_ms,
                ) = await self.mass.music.get_resume_position(resolved_episode, userid=userid)
                resolved_episode.resume_position_ms = 0 if fully_played else resume_position_ms
        else:
            # get first episode that is not fully played
            for ep in all_episodes:
                if ep.fully_played:
                    continue
                # ensure we have accurate resume info
                (
                    fully_played,
                    resume_position_ms,
                ) = await self.mass.music.get_resume_position(ep, userid=userid)
                if fully_played:
                    continue
                ep.resume_position_ms = resume_position_ms
                resolved_episode = ep
                break
            else:
                # no episodes found that are not fully played, so we start at the beginning
                resolved_episode = next((x for x in all_episodes), None)
        if resolved_episode is None:
            raise InvalidDataError(f"Unable to resolve episode to play for Podcast {podcast.name}")
        if start_from_beginning:
            # play the resolved episode from position 0 without touching stored progress
            resolved_episode.fully_played = False
            resolved_episode.resume_position_ms = 0
        # get the index of the episode
        episode_index = all_episodes.index(resolved_episode)
        # return the (remaining) episode(s) to play
        return UniqueList(all_episodes[episode_index:])

    async def get_next_podcast_episode(
        self, episode: PodcastEpisode, userid: str | None = None
    ) -> PodcastEpisode | None:
        """
        Return the episode to play after the given one, or None if there is none left.

        Episodes are walked in the same order a full podcast enqueue produces, skipping the
        ones that were already fully played.

        :param episode: The episode that is being continued.
        :param userid: User whose resume position should be applied.
        """
        podcast = episode.podcast
        all_episodes = [
            x async for x in self.mass.music.podcasts.episodes(podcast.item_id, podcast.provider)
        ]
        all_episodes.sort(key=lambda x: x.position)
        current_index = next(
            (idx for idx, x in enumerate(all_episodes) if x.uri == episode.uri), None
        )
        if current_index is None:
            # the episode is no longer part of the feed, so we have nothing to continue from
            return None
        for candidate in all_episodes[current_index + 1 :]:
            if candidate.fully_played:
                continue
            # ensure we have accurate resume info
            fully_played, resume_position_ms = await self.mass.music.get_resume_position(
                candidate, userid=userid
            )
            if fully_played:
                continue
            candidate.resume_position_ms = resume_position_ms
            return candidate
        return None

    async def get_next_audiobook(
        self, audiobook: Audiobook, userid: str | None = None
    ) -> Audiobook | None:
        """
        Return the next book in the collection(s) the given book belongs to, if there is one.

        Returns None for a standalone book and for a book whose collection has no not-fully-played
        book left after it.

        :param audiobook: The audiobook that is being continued.
        :param userid: User whose resume position should be applied.
        """
        # collections are built from the library metadata, so a book that is not in the
        # library has no series to continue with
        library_item = (
            audiobook
            if audiobook.provider == "library"
            else await self.mass.music.audiobooks.get_library_item_by_prov_id(
                audiobook.item_id, audiobook.provider
            )
        )
        if library_item is None:
            return None
        for collection in library_item.metadata.collections or []:
            try:
                media_collection = await self.mass.music.audiobooks.get_collection(
                    get_collection_item_id(collection.title, MediaType.AUDIOBOOK)
                )
            except MediaNotFoundError:
                continue
            if next_book := await self._next_unplayed_book(media_collection, library_item, userid):
                return next_book
        return None

    async def get_author_narrator_audiobooks(
        self, author_narrator: Artist, userid: str | None
    ) -> list[Audiobook]:
        """
        Return audiobooks to play of a given artist.

        If all books are played, enqueue all of them. If not, enqueue books in a collection's order
        if they are part of a collection.
        """
        audiobooks: UniqueList[Audiobook] = UniqueList([])
        async with ImpersonatedUser(self.mass, user=userid):
            # ensure we get the position status on the current user
            all_audiobooks = await self.mass.music.artists.audiobooks(
                author_narrator.item_id, author_narrator.provider, author_narrator.artist_type
            )
        for book in all_audiobooks:
            # do not use get_resume_position here, as an artist may potentially have a lot of audiobooks,
            # resulting in many API calls.
            if book.fully_played:
                continue
            audiobooks.append(book)
        if len(audiobooks) == 0:
            audiobooks = UniqueList(all_audiobooks)

        # treat books part of a collection separately by keeping the collections order
        collections: list[MediaCollection[Audiobook]] = []
        collection_item_ids: list[str] = []

        books_with_collection: dict[str, set[str]] = {}  # book_item_id: {collection_ids}
        for book in audiobooks:
            for media_item_collection in book.metadata.collections or []:
                collection_item_id = get_collection_item_id(
                    media_item_collection.title, MediaType.AUDIOBOOK
                )
                if collection_item_id not in collection_item_ids:
                    collection_item_ids.append(collection_item_id)
                entry = books_with_collection.get(book.item_id, set())
                entry.add(collection_item_id)
                books_with_collection[book.item_id] = entry
        async with ImpersonatedUser(self.mass, user=userid):
            for collection_item_id in collection_item_ids:
                try:
                    collection = await self.mass.music.audiobooks.get_collection(collection_item_id)
                    collections.append(collection)
                except MediaNotFoundError:
                    # Remove invalid collection everywhere
                    for book_collections in books_with_collection.values():
                        book_collections.discard(collection_item_id)
                    continue
        # ensure, that books with collection only holds books which have a verified collection
        books_with_collection = {
            book_item_id: collection_ids
            for book_item_id, collection_ids in books_with_collection.items()
            if collection_ids
        }

        # remove books which are part of a collection
        audiobooks = UniqueList(
            [book for book in audiobooks if book.item_id not in books_with_collection]
        )
        # enqueue books which are part of a collection in the collection's order, however, as a collection
        # may have books of different artists, only enqueue the books which belong to the artist.
        # if a book happens to be part of multiple collections, only enqueue once
        books_with_collection_sorted: list[Audiobook] = []
        for collection in collections:
            for book in collection.items:
                if (
                    book.item_id in books_with_collection
                    and book not in books_with_collection_sorted
                ):
                    books_with_collection_sorted.append(book)

        return list(audiobooks) + books_with_collection_sorted

    async def _set_episode_resume_point(
        self, episode: PodcastEpisode, userid: str | None, start_from_beginning: bool
    ) -> None:
        """
        Apply the resume point to a resolved podcast episode.

        When start_from_beginning is set the episode starts at position 0 and the resume
        lookup is skipped; the stored progress itself is left untouched.
        """
        if start_from_beginning:
            episode.fully_played = False
            episode.resume_position_ms = 0
            return
        fully_played, resume_position_ms = await self.mass.music.get_resume_position(
            episode, userid=userid
        )
        episode.fully_played = fully_played
        episode.resume_position_ms = 0 if fully_played else resume_position_ms

    async def _next_unplayed_book(
        self,
        collection: MediaCollection[Audiobook],
        current: Audiobook,
        userid: str | None,
    ) -> Audiobook | None:
        """Return the first not fully played book after `current` in the given collection."""
        books = [x for x in collection.items if isinstance(x, Audiobook)]
        current_index = next(
            (idx for idx, x in enumerate(books) if x.item_id == current.item_id), None
        )
        if current_index is None:
            return None
        for candidate in books[current_index + 1 :]:
            fully_played, resume_position_ms = await self.mass.music.get_resume_position(
                candidate, userid=userid
            )
            if fully_played:
                continue
            candidate.resume_position_ms = resume_position_ms
            return candidate
        return None

    async def _resolve_library_artist(self, artist: Artist) -> Artist | None:
        """
        Resolve the in-library artist for the given (possibly provider) artist item.

        :param artist: The artist item, which may be a library or a provider item.
        """
        if artist.provider == "library":
            return artist
        return await self.mass.music.artists.get_library_item_by_prov_id(
            artist.item_id, artist.provider
        )

    async def _library_artist_tracks(self, artist: Artist) -> list[Track]:
        """
        Return the in-library tracks for the given artist (empty if it is not saved).

        :param artist: The artist to resolve in-library tracks for.
        """
        if (library_artist := await self._resolve_library_artist(artist)) is None:
            return []
        return await self.mass.music.artists.tracks(library_artist.item_id, "library")

    async def _provider_artist_tracks(self, artist: Artist) -> list[Track]:
        """
        Return all of the artist's tracks across its (streaming) providers.

        :param artist: The artist to resolve provider tracks for.
        """
        unique_providers = self.mass.music.get_unique_providers()
        tracks: list[Track] = []
        for mapping in artist.provider_mappings:
            if mapping.provider_instance not in unique_providers:
                continue
            tracks.extend(
                await self.mass.music.artists.tracks(mapping.item_id, mapping.provider_instance)
            )
        return tracks

    async def _resolve_media_items(
        self,
        media_item: MediaItemType | ItemMapping | BrowseFolder,
        start_item: str | None = None,
        userid: str | None = None,
        queue_id: str | None = None,
        sort_by: str | None = None,
        start_from_beginning: bool = False,
        keep_preceding_items: bool = False,
    ) -> list[MediaItemType]:
        """
        Resolve/unwrap media items to enqueue.

        :param media_item: The media item to resolve into playable items.
        :param start_item: Optional item to start a playlist/album/genre from, or the chapter
            to start an audiobook/podcast episode at.
        :param userid: Optional user the playback is attributed to.
        :param queue_id: Optional queue the playback is requested for.
        :param sort_by: Optional sort key to order tracks by before applying start_item.
        :param start_from_beginning: Ignore any saved resume position for a podcast episode.
        :param keep_preceding_items: For a playlist/album, move the tracks before start_item
            behind the rest instead of dropping them, so the full item is returned with
            start_item first.
        """
        # resolve Itemmapping to full media item
        if isinstance(media_item, ItemMapping):
            if media_item.uri is None:
                raise InvalidDataError("ItemMapping has no URI")
            media_item = await self.mass.music.get_item_by_uri(media_item.uri)
        if media_item.media_type == MediaType.PLAYLIST:
            media_item = cast("Playlist", media_item)
            playlist_tracks = await self.get_playlist_tracks(
                media_item,
                start_item,
                sort_by=sort_by,
                keep_preceding_items=keep_preceding_items,
            )
            self._mark_container_played(media_item, playlist_tracks, userid, queue_id)
            return list(playlist_tracks)
        if media_item.media_type == MediaType.ARTIST:
            media_item = cast("Artist", media_item)
            artist_items: list[Audiobook] | list[Track]
            if media_item.artist_type in [ArtistType.AUTHOR, ArtistType.NARRATOR]:
                artist_items = await self.get_author_narrator_audiobooks(media_item, userid)
            else:
                artist_items = await self.get_artist_tracks(media_item)
            self._mark_container_played(media_item, artist_items, userid, queue_id)
            return list(artist_items)
        if media_item.media_type == MediaType.ALBUM:
            media_item = cast("Album", media_item)
            return list(
                await self.get_album_tracks(
                    media_item,
                    start_item,
                    sort_by=sort_by,
                    keep_preceding_items=keep_preceding_items,
                )
            )
        if media_item.media_type == MediaType.GENRE:
            media_item = cast("Genre", media_item)
            genre_tracks = await self.get_genre_tracks(media_item, start_item)
            self._mark_container_played(media_item, genre_tracks, userid, queue_id)
            return list(genre_tracks)
        if media_item.media_type == MediaType.AUDIOBOOK:
            media_item = cast("Audiobook", media_item)
            # ensure we grab the correct/latest resume point info
            media_item.resume_position_ms = await self.get_audiobook_resume_point(
                media_item, start_item, userid=userid
            )
            return [media_item]
        if media_item.media_type == MediaType.COLLECTION:
            collection_item_media_type = get_collection_item_media_type_from_item_id(
                media_item.item_id
            )
            if collection_item_media_type != MediaType.AUDIOBOOK:
                self.logger.error("Collections are only available for audiobooks.")
                return []
            if TYPE_CHECKING:
                assert isinstance(media_item, MediaCollection)
            book: Audiobook | None = None
            for item in media_item.items:
                if TYPE_CHECKING:
                    assert isinstance(item, Audiobook)
                # enqueue the first not fully finished audiobook
                fully_played, resume_position_ms = await self.mass.music.get_resume_position(
                    item, userid=userid
                )
                if not fully_played:
                    item.resume_position_ms = resume_position_ms
                    book = item
                    break
            if book is None:
                if len(media_item.items) > 0:
                    return [media_item.items[0]]
                return []
            return [book]

        if media_item.media_type == MediaType.PODCAST:
            media_item = cast("Podcast", media_item)
            episodes = await self.get_next_podcast_episodes(
                media_item, start_item, userid=userid, start_from_beginning=start_from_beginning
            )
            self._mark_container_played(media_item, episodes, userid, queue_id)
            return list(episodes)
        if media_item.media_type == MediaType.PODCAST_EPISODE:
            media_item = cast("PodcastEpisode", media_item)
            return list(
                await self.get_next_podcast_episodes(
                    None, media_item, userid=userid, start_from_beginning=start_from_beginning
                )
            )
        if media_item.media_type == MediaType.FOLDER:
            media_item = cast("BrowseFolder", media_item)
            return list(await self._get_folder_tracks(media_item))
        # all other: single track or radio item
        return [cast("MediaItemType", media_item)]

    async def _get_folder_tracks(self, folder: BrowseFolder) -> list[Track]:
        """Fetch (playable) tracks for given browse folder."""
        self.logger.info(
            "Fetching tracks to play for folder %s",
            folder.name,
        )
        try:
            folder_items = await self.mass.music.browse(folder.path)
        except OSError as err:
            # e.g. the (top-level) folder URI points at a path that no longer exists
            raise MediaNotFoundError(f"Folder '{folder.path}' could not be found") from err
        tracks: list[Track] = []
        for item in folder_items:
            if not item.is_playable:
                continue
            try:
                # recursively fetch tracks from all media types
                resolved = await self._resolve_media_items(item)
            except MediaNotFoundError:
                # best-effort: skip child items/subfolders that are empty or unreachable
                # so a single bad entry does not abort playback of the whole folder
                continue
            tracks += [x for x in resolved if isinstance(x, Track)]

        return tracks

    def _mark_container_played(
        self,
        container: MediaItemType,
        resolved_items: Sequence[MediaItemType],
        userid: str | None,
        queue_id: str | None,
    ) -> None:
        """
        Credit a container the user asked to play with an explicit play.

        Only credits when the container actually resolved to something, so an empty
        playlist/artist/genre/podcast never lands in the play history.

        :param container: The playlist, artist, genre or podcast that was asked for.
        :param resolved_items: The items the container resolved to.
        :param userid: Optional user the playback is attributed to.
        :param queue_id: Optional queue the playback is requested for.
        """
        if not resolved_items:
            return
        self.mass.create_task(
            self.mass.music.mark_item_played(
                container, userid=userid, queue_id=queue_id, user_initiated=True
            )
        )
