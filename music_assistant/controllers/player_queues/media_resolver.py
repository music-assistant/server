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
from dataclasses import dataclass
from types import NoneType
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from music_assistant_models.enums import (
    ArtistType,
    ListingType,
    MediaType,
    ProviderFeature,
    SortDirection,
    SortField,
)
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
from music_assistant_models.queue_item import QueueItemOrigin

from music_assistant.constants import PlaylistPlayableItem
from music_assistant.controllers.music.listing import resolve_listing_sort
from music_assistant.controllers.player_queues.constants import (
    CONF_DEFAULT_ENQUEUE_ORDER_ARTIST,
    CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    ENQUEUE_ORDER_ARTIST_DEFAULT_VALUE,
    ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
    ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
    LIBRARY_ALBUM_SKIPPED_TYPES,
    PROVIDER_ALBUM_SKIPPED_TYPES,
    ArtistOrder,
)
from music_assistant.controllers.player_queues.helpers import (
    is_finite_radio,
    order_albums,
    origin_for,
)
from music_assistant.controllers.streams.stream_sources import (
    ContainerListing,
    SourceCandidate,
    StreamSourcePolicy,
    rank_stream_sources,
    read_stream_source_policy,
    select_container_source,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import ImpersonatedUser
from music_assistant.helpers.collections import (
    get_collection_item_id,
    get_collection_item_media_type_from_item_id,
)
from music_assistant.helpers.compare import compare_album
from music_assistant.helpers.provider_access import playback_instance_for, playback_sources_for
from music_assistant.helpers.util import is_disc_dir
from music_assistant.models.music_provider import (
    PROVIDER_FETCH_ERRORS,
    MusicProvider,
    provider_fetch_log_level,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from music_assistant_models.media_items import ProviderMapping

    from music_assistant.controllers.music.media.album_tracks import AlbumListing
    from music_assistant.controllers.player_queues.controller import PlayerQueuesController

_LATEST_EPISODE_KEYWORDS = frozenset({"latest", "newest"})
_START_ITEM_SUBSTRING_MIN_LEN = 3
# the artist selections that play only the library's albums
_LIBRARY_SELECTIONS = frozenset({"library_tracks", "library_album_tracks", "prefer_library"})
# caps the album track lookups in flight, so a large discography does not flood a provider
ALBUM_LOOKUP_BATCH = 5


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    """A playable item a media item resolved to, and where it was played from."""

    item: MediaItemType
    origin: QueueItemOrigin | None = None


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


class ArtistTracks(NamedTuple):
    """The tracks to play for an artist."""

    tracks: list[Track]
    # the tracks are the artist's albums one after the other, so they play in order
    plays_albums: bool


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

        Resolves an umbrella media item (artist, album, genre, playlist, finite radio) to the
        tracks that playing it would enqueue; a track resolves to itself, other types to an
        empty list.

        :param media_item: The media item to resolve to playable tracks.
        """
        if media_item.media_type == MediaType.TRACK:
            return [cast("Track", media_item)]
        if media_item.media_type == MediaType.RADIO:
            if is_finite_radio(media_item):
                radio_tracks = await self.mass.music.radio.tracks(media_item)
                return [track for track in radio_tracks if track.available]
            return []
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
        """
        Return the tracks to play for the given artist.

        :param artist: The artist to play.
        """
        return (await self.resolve_artist_tracks(artist)).tracks

    async def resolve_artist_tracks(self, artist: Artist) -> ArtistTracks:
        """
        Return the tracks to play for the given artist, and whether they are its albums.

        An artist in the library follows the user preference, any other artist plays all of
        its tracks across the providers. With an album order, these are the artist's albums one
        after the other, each as it plays on its own. An artist with no albums gets the selected
        tracks shuffled. One whose albums can't be listed or loaded plays nothing.

        :param artist: The artist to play.
        """
        library_artist = await self._resolve_library_artist(artist)
        if library_artist is None or not any(
            mapping.in_library for mapping in library_artist.provider_mappings
        ):
            artist_items_conf = "all_tracks"
        else:
            artist_items_conf = str(
                self.mass.config.get_raw_core_config_value(
                    self.queues.domain,
                    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
                    ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
                )
            )
        order = self.get_artist_order()
        self.logger.info(
            "Fetching tracks to play for artist %s (selection: %s, order: %s)",
            artist.name,
            artist_items_conf,
            order,
        )
        if self._album_order_applies(artist, artist_items_conf):
            albums = await self._artist_albums(artist, artist_items_conf)
            if albums is None:
                # no provider could list the albums; shuffled songs would ignore the album order
                return ArtistTracks([], plays_albums=True)
            if albums:
                copies = {album_copies[0].uri: album_copies for album_copies in albums}
                ordered = order_albums([album_copies[0] for album_copies in albums], order)
                return ArtistTracks(
                    await self._album_tracks_in_turn([copies[album.uri] for album in ordered]),
                    plays_albums=True,
                )
        return ArtistTracks(
            await self._shuffled_artist_tracks(artist, artist_items_conf), plays_albums=False
        )

    def get_artist_order(self) -> ArtistOrder:
        """Return the configured order to play an artist's tracks in."""
        raw = self.mass.config.get_raw_core_config_value(
            self.queues.domain,
            CONF_DEFAULT_ENQUEUE_ORDER_ARTIST,
            ENQUEUE_ORDER_ARTIST_DEFAULT_VALUE,
        )
        try:
            return ArtistOrder(str(raw))
        except ValueError:
            return ArtistOrder(ENQUEUE_ORDER_ARTIST_DEFAULT_VALUE)

    async def get_album_tracks(
        self,
        album: Album,
        start_item: str | None,
        keep_preceding_items: bool = False,
    ) -> list[Track]:
        """
        Return tracks for given album, based on user preference.

        :param album: The album to fetch the tracks for.
        :param start_item: Optional item_id/uri of the track to start from.
        :param keep_preceding_items: Move the tracks before start_item behind the rest instead
            of dropping them, so the full album is returned with start_item first.
        """
        self.logger.info(
            "Fetching tracks to play for album %s",
            album.name,
        )
        return self._album_order(await self._album_tracks(album), start_item, keep_preceding_items)

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
            sort_field=SortField.RANDOM,
        )

        for genre_track in tracks:
            if not genre_track.available:
                continue
            if start_item in (genre_track.item_id, genre_track.uri):
                start_item_found = True
            if start_item is not None and not start_item_found:
                continue
            result.append(genre_track)

        provider_error: Exception | None = None
        for album in albums:
            try:
                album_tracks = await self.get_album_tracks(album, None)
            except PROVIDER_FETCH_ERRORS as err:
                # the genre is sampled best-effort: one failing album must not abort it
                provider_error = err
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch tracks for album %s: %s",
                    album.name,
                    err,
                )
                continue
            result.extend(album_tracks[:5])

        for artist in artists:
            try:
                top_tracks = await self.mass.music.artists.top_tracks(
                    artist.item_id, artist.provider, limit=None
                )
                # a provider can list top tracks that are all unplayable here, which must
                # not stand in for the artist's playable tracks
                artist_tracks = [track for track in top_tracks if track.available]
                if not artist_tracks:
                    # not get_artist_tracks: a top_tracks preference would repeat the empty lookup
                    all_tracks = await self.mass.music.artists.tracks(
                        artist.item_id, artist.provider, limit=None
                    )
                    artist_tracks = [track for track in all_tracks if track.available]
            except PROVIDER_FETCH_ERRORS as err:
                provider_error = err
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch tracks for artist %s: %s",
                    artist.name,
                    err,
                )
                continue
            random.shuffle(artist_tracks)
            result.extend(artist_tracks[:5])
        if provider_error is not None and not result:
            # nothing could be played at all, so surface the reason instead of an empty list
            raise provider_error
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
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        keep_preceding_items: bool = False,
    ) -> list[PlaylistPlayableItem]:
        """
        Return tracks for given playlist, based on user preference.

        The tracks come in the order the playlist's tracks listing sorts them in.

        :param playlist: The playlist to fetch the tracks for.
        :param start_item: Optional item_id/uri/name of the track to start from.
        :param sort_field: Sort field, the playlist order (POSITION) when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param keep_preceding_items: Move the tracks before start_item behind the rest instead
            of dropping them, so the full playlist is returned with start_item first.
        :raises InvalidDataError: When the sort field is not offered for playlist tracks.
        """
        result: list[PlaylistPlayableItem] = []
        self.logger.info(
            "Fetching tracks to play for playlist %s",
            playlist.name,
        )
        # an unsupported sort is rejected before the playlist is fetched
        sort_field, sort_direction = resolve_listing_sort(
            ListingType.PLAYLIST_TRACKS, sort_field, sort_direction
        )
        force_refresh = playlist.is_dynamic
        # the order the provider lists the playlist in
        in_playlist_order = sort_field == SortField.POSITION and sort_direction == SortDirection.ASC
        # Fast path: no re-sort needed and the preceding tracks are dropped anyway, so
        # skip-until-found in a single pass and never materialize huge playlists when
        # starting near the end.
        if in_playlist_order and not keep_preceding_items:
            start_item_found = False
            async for playlist_track in self.mass.music.playlists.iter_tracks(
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
        if in_playlist_order:
            # Rotate path: must materialize all tracks before rotating, then slice.
            async for playlist_track in self.mass.music.playlists.iter_tracks(
                playlist.item_id,
                playlist.provider,
                force_refresh=force_refresh,
                allow_dynamic_tracks=playlist.is_dynamic,
            ):
                if not playlist_track.available:
                    continue
                result.append(playlist_track)
        else:
            # any other order is the one the playlist's tracks listing shows
            result = [
                playlist_track
                for playlist_track in await self.mass.music.playlists.tracks(
                    playlist.item_id,
                    playlist.provider,
                    force_refresh=force_refresh,
                    allow_dynamic_tracks=playlist.is_dynamic,
                    sort_field=sort_field,
                    sort_direction=sort_direction,
                    limit=None,
                )
                if playlist_track.available
            ]
        if start_item is not None:
            for idx, track in enumerate(result):
                if _start_item_matches(start_item, track):
                    return result[idx:] + (result[:idx] if keep_preceding_items else [])
            return []
        return result

    async def get_audiobook_resume_point(
        self,
        audio_book: Audiobook,
        chapter: str | int | None = None,
        userid: str | None = None,
        pinned: tuple[str, str] | None = None,
    ) -> int:
        """
        Return resume point (in milliseconds) for given audio book.

        :param audio_book: The audiobook to resume.
        :param chapter: The chapter the user picked to start at, if any.
        :param userid: The user whose progress to resume from.
        :param pinned: The (provider instance, item id) of the copy the book plays from, whose
            progress is read first.
        """
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
            audio_book, userid=userid, pinned=pinned
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
            x
            async for x in self.mass.music.podcasts.iter_episodes(podcast.item_id, podcast.provider)
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
            x
            async for x in self.mass.music.podcasts.iter_episodes(podcast.item_id, podcast.provider)
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
                author_narrator.item_id,
                author_narrator.provider,
                author_narrator.artist_type,
                limit=None,
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

    def _album_order_applies(self, artist: Artist, selection: str) -> bool:
        """
        Return whether the artist order's album setting applies to the artist.

        :param artist: The artist to play.
        :param selection: The artist selection setting.
        """
        # top tracks are tracks, and an author or narrator plays audiobooks
        return (
            self.get_artist_order().plays_albums
            and selection != "top_tracks"
            and artist.artist_type not in (ArtistType.AUTHOR, ArtistType.NARRATOR)
        )

    async def _artist_albums(self, artist: Artist, selection: str) -> list[list[Album]] | None:
        """
        Return the artist's albums that the selection setting covers.

        Each album is a list of its copies, in the order to try them. Returns None when there is
        no library album and no provider could list the artist's albums.

        :param artist: The artist to fetch the albums of.
        :param selection: The artist selection setting.
        """
        library_albums: list[Album] = []
        if (library_artist := await self._resolve_library_artist(artist)) is not None:
            library_albums = await self.mass.music.artists.albums(
                library_artist.item_id, "library", limit=None
            )
        # singles are left out; library compilations stay, as a local collection may have no
        # albums otherwise
        albums = [
            [album]
            for album in library_albums
            if album.album_type not in LIBRARY_ALBUM_SKIPPED_TYPES
        ]
        if selection not in _LIBRARY_SELECTIONS:
            # add the albums found only on a provider, leaving out singles and compilations, as
            # their songs are on the albums already; a skipped library single is still in the
            # library, so its provider copy is skipped too, but it doesn't hide a provider album
            # of the same name
            in_library = {
                (mapping.provider_instance, mapping.item_id)
                for album in library_albums
                for mapping in album.provider_mappings
            }
            if (provider_albums := await self._provider_artist_albums(artist)) is None:
                return albums or None
            for album in provider_albums:
                if album.album_type in PROVIDER_ALBUM_SKIPPED_TYPES or any(
                    (mapping.provider_instance, mapping.item_id) in in_library
                    for mapping in album.provider_mappings
                ):
                    continue
                # an album plays once, even when the library or another provider has it unlinked;
                # the other copy plays when this one has nothing to play
                if album_copies := next(
                    (copies for copies in albums if compare_album(album, copies[0])), None
                ):
                    album_copies.append(album)
                else:
                    albums.append([album])
        return albums

    async def _album_tracks(self, album: Album) -> list[Track]:
        """
        Return the available tracks of the given album, following the album selection setting.

        :param album: The album to fetch the tracks for.
        """
        return [
            album_track
            for album_track in await self.mass.music.albums.tracks(
                item_id=album.item_id,
                provider_instance_id_or_domain=album.provider,
                in_library_only=self._album_library_tracks_only(),
                limit=None,
            )
            if album_track.available
        ]

    def _album_library_tracks_only(self) -> bool:
        """Return whether an album plays only its tracks that are in the library."""
        return (
            self.mass.config.get_raw_core_config_value(
                self.queues.domain,
                CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
                ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
            )
            == "library_tracks"
        )

    async def _shuffled_artist_tracks(self, artist: Artist, artist_items_conf: str) -> list[Track]:
        """
        Return the artist's selected tracks, shuffled.

        :param artist: The artist to play.
        :param artist_items_conf: The artist selection setting.
        """
        if artist_items_conf == "top_tracks":
            tracks = await self.mass.music.artists.top_tracks(
                artist.item_id, artist.provider, limit=None
            )
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
                tracks = await self.mass.music.artists.top_tracks(
                    artist.item_id, artist.provider, limit=None
                )
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

    async def _album_tracks_in_turn(self, albums: list[list[Album]]) -> list[Track]:
        """
        Return the tracks of the given albums, one album after the other.

        Each album plays as it does on its own. An album is left out when no copy it may play
        from has tracks to play.

        :param albums: The albums to play, in order, each a list of its copies to try in turn.
        """
        tracks: list[Track] = []
        for start in range(0, len(albums), ALBUM_LOOKUP_BATCH):
            batch = albums[start : start + ALBUM_LOOKUP_BATCH]
            results = await asyncio.gather(
                *(self._album_copy_tracks(copies) for copies in batch),
                return_exceptions=True,
            )
            for (album, *_), result in zip(batch, results, strict=True):
                if isinstance(result, PROVIDER_FETCH_ERRORS):
                    self.logger.log(
                        provider_fetch_log_level(result),
                        "Unable to fetch the tracks of album %s: %s",
                        album.name,
                        result,
                    )
                    continue
                if isinstance(result, BaseException):
                    raise result
                self.logger.debug("Playing %s tracks of album %s", len(result), album.name)
                tracks.extend(result)
        return tracks

    async def _album_copy_tracks(self, copies: list[Album]) -> list[Track]:
        """
        Return the tracks of the first copy of an album with tracks to play.

        The last copy's result is returned as it is, even when empty or failing.

        :param copies: The album's copies, in the order to try them.
        """
        library_tracks = self._album_library_tracks_only()
        for album in copies[:-1]:
            # with the library tracks album setting, a copy in the library plays only its saved
            # tracks; another copy outside the library would play the album in full
            if library_tracks and (
                album.provider == "library"
                or await self.mass.music.albums.get_library_item_by_prov_id(
                    album.item_id, album.provider
                )
            ):
                return await self._album_tracks(album)
            try:
                tracks = await self._album_tracks(album)
            except PROVIDER_FETCH_ERRORS as err:
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch the tracks of album %s from %s, trying another copy: %s",
                    album.name,
                    album.provider,
                    err,
                )
                continue
            if tracks:
                return tracks
            self.logger.debug(
                "Album %s from %s has no playable tracks, trying another copy",
                album.name,
                album.provider,
            )
        return await self._album_tracks(copies[-1])

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
        return await self.mass.music.artists.tracks(library_artist.item_id, "library", limit=None)

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
            try:
                tracks.extend(
                    await self.mass.music.artists.tracks(
                        mapping.item_id, mapping.provider_instance, limit=None
                    )
                )
            except PROVIDER_FETCH_ERRORS as err:
                # one failing provider must not drop the tracks of the artist's other providers
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch tracks for artist %s from provider %s: %s",
                    artist.name,
                    mapping.provider_instance,
                    err,
                )
        return tracks

    async def _provider_artist_albums(self, artist: Artist) -> list[Album] | None:
        """
        Return all of the artist's albums across its (streaming) providers.

        Returns None when no provider listed them and at least one was unavailable or failed.
        A provider that can't list artist albums at all is left out.

        :param artist: The artist to resolve provider albums for.
        """
        unique_providers = self.mass.music.get_unique_providers()
        albums: list[Album] = []
        listed = failed = False
        # a fixed order makes the first copy of an album, and so its release year, deterministic
        for mapping in sorted(
            artist.provider_mappings,
            key=lambda mapping: (not mapping.available, mapping.provider_instance, mapping.item_id),
        ):
            if mapping.provider_instance not in unique_providers:
                continue
            # the albums lookup answers an empty list for these, so they are told apart first
            provider = self.mass.get_provider(
                mapping.provider_instance, provider_type=MusicProvider
            )
            if provider is None:
                failed = True
                self.logger.warning(
                    "Unable to fetch albums for artist %s from provider %s: provider unavailable",
                    artist.name,
                    mapping.provider_instance,
                )
                continue
            if not provider.supports_feature(ProviderFeature.ARTIST_ALBUMS):
                continue
            try:
                albums.extend(
                    await self.mass.music.artists.albums(
                        mapping.item_id, mapping.provider_instance, limit=None
                    )
                )
                listed = True
            except PROVIDER_FETCH_ERRORS as err:
                failed = True
                # one failing provider must not drop the albums of the artist's other providers
                self.logger.log(
                    provider_fetch_log_level(err),
                    "Unable to fetch albums for artist %s from provider %s: %s",
                    artist.name,
                    mapping.provider_instance,
                    err,
                )
        return None if failed and not listed else albums

    async def _resolve_media_items(  # noqa: PLR0915
        self,
        media_item: MediaItemType | ItemMapping | BrowseFolder,
        start_item: str | None = None,
        userid: str | None = None,
        queue_id: str | None = None,
        sort_field: SortField | None = None,
        sort_direction: SortDirection | None = None,
        start_from_beginning: bool = False,
        keep_preceding_items: bool = False,
        artist_tracks: list[Track] | None = None,
        played_copy: tuple[str, str] | None = None,
    ) -> list[ResolvedItem]:
        """
        Resolve/unwrap media items to enqueue, each with the origin it was played from.

        An item played from a container records that container; one listed by a playlist,
        podcast or folder is also pinned to the copy that listing had for it, an album track to
        its copy on the one source the album plays from. An audiobook or podcast episode played
        on its own keeps the provider copy it was started from, a single track or radio played
        on its own has no origin.

        :param media_item: The media item to resolve into playable items.
        :param start_item: Optional item to start a playlist/album/genre from, or the chapter
            to start an audiobook/podcast episode at.
        :param userid: Optional user the playback is attributed to.
        :param queue_id: Optional queue the playback is requested for.
        :param sort_field: For a playlist/album, the sort field of its tracks listing to play
            the tracks in before applying start_item; the listing's default when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param start_from_beginning: Ignore any saved resume position for a podcast episode.
        :param keep_preceding_items: For a playlist/album, move the tracks before start_item
            behind the rest instead of dropping them, so the full item is returned with
            start_item first.
        :param artist_tracks: The tracks already resolved for an artist, so they are not
            fetched again.
        :param played_copy: For an audiobook played on its own, the (provider instance, item id)
            of the copy the play request named, which the library item it resolved to no longer
            names.
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
                sort_field=sort_field,
                sort_direction=sort_direction,
                keep_preceding_items=keep_preceding_items,
            )
            self._mark_container_played(media_item, playlist_tracks, userid, queue_id)
            return self._played_from(media_item, playlist_tracks)
        if media_item.media_type == MediaType.ARTIST:
            media_item = cast("Artist", media_item)
            artist_items: list[Audiobook] | list[Track]
            if media_item.artist_type in [ArtistType.AUTHOR, ArtistType.NARRATOR]:
                artist_items = await self.get_author_narrator_audiobooks(media_item, userid)
            else:
                artist_items = (
                    artist_tracks
                    if artist_tracks is not None
                    else await self.get_artist_tracks(media_item)
                )
            self._mark_container_played(media_item, artist_items, userid, queue_id)
            return self._played_from(media_item, artist_items, pin=False)
        if media_item.media_type == MediaType.ALBUM:
            media_item = cast("Album", media_item)
            return await self._resolve_album(
                media_item,
                start_item,
                userid,
                queue_id,
                sort_field,
                sort_direction,
                keep_preceding_items,
            )
        if media_item.media_type == MediaType.GENRE:
            media_item = cast("Genre", media_item)
            genre_tracks = await self.get_genre_tracks(media_item, start_item)
            self._mark_container_played(media_item, genre_tracks, userid, queue_id)
            return self._played_from(media_item, genre_tracks, pin=False)
        if media_item.media_type == MediaType.AUDIOBOOK:
            media_item = cast("Audiobook", media_item)
            # ensure we grab the correct/latest resume point info
            media_item.resume_position_ms = await self.get_audiobook_resume_point(
                media_item, start_item, userid=userid, pinned=played_copy
            )
            return [ResolvedItem(media_item, self._pinned_origin(played_copy))]
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
                    return [ResolvedItem(media_item.items[0])]
                return []
            return [ResolvedItem(book)]

        if media_item.media_type == MediaType.PODCAST:
            media_item = cast("Podcast", media_item)
            episodes = await self.get_next_podcast_episodes(
                media_item, start_item, userid=userid, start_from_beginning=start_from_beginning
            )
            self._mark_container_played(media_item, episodes, userid, queue_id)
            return self._played_from(media_item, episodes)
        if media_item.media_type == MediaType.PODCAST_EPISODE:
            media_item = cast("PodcastEpisode", media_item)
            return [
                ResolvedItem(episode, self._pinned_origin(self._own_copy(episode)))
                for episode in await self.get_next_podcast_episodes(
                    None, media_item, userid=userid, start_from_beginning=start_from_beginning
                )
            ]
        if media_item.media_type == MediaType.FOLDER:
            media_item = cast("BrowseFolder", media_item)
            return await self._get_folder_items(media_item, userid, queue_id, start_from_beginning)
        if is_finite_radio(media_item):
            # resolved up front like a playlist instead of played as a live stream
            radio_tracks = await self.get_tracks_for_playback(media_item)
            self._mark_container_played(media_item, radio_tracks, userid, queue_id)
            # a station's tracklist can come from a builtin playlist, whose entries name an
            # arbitrary copy, so the station is recorded without pinning one
            return self._played_from(media_item, radio_tracks, pin=False)
        # all other: a single track, or a live/dynamic radio item played as itself
        return [ResolvedItem(cast("MediaItemType", media_item))]

    async def _get_folder_items(
        self,
        folder: BrowseFolder,
        userid: str | None = None,
        queue_id: str | None = None,
        start_from_beginning: bool = False,
    ) -> list[ResolvedItem]:
        """Fetch the playable items for the given browse folder, each played from that folder."""
        self.logger.info(
            "Fetching items to play for folder %s",
            folder.name,
        )
        try:
            folder_items = await self.mass.music.browse(folder.path, limit=None)
        except OSError as err:
            # e.g. the (top-level) folder URI points at a path that no longer exists
            raise MediaNotFoundError(f"Folder '{folder.path}' could not be found") from err
        container = await self._folder_container(folder)
        items: list[ResolvedItem] = []
        for item in folder_items:
            if not item.is_playable:
                continue
            try:
                # recursively resolve every child, so a folder of podcast episodes or
                # radio stations plays just like a folder of tracks
                resolved = await self._resolve_media_items(
                    item,
                    userid=userid,
                    queue_id=queue_id,
                    start_from_beginning=start_from_beginning,
                )
            except MediaNotFoundError:
                # best-effort: skip child items/subfolders that are empty or unreachable
                # so a single bad entry does not abort playback of the whole folder
                continue
            for entry in resolved:
                # a dynamic station supplies its tracks on demand through the managed pool;
                # queued as a plain item it cannot be streamed, so leave it out here. This is
                # checked after resolving, as an ItemMapping child only reveals it once resolved.
                if isinstance(entry.item, Radio) and entry.item.is_dynamic:
                    continue
                # a subfolder or a playlist in the folder already recorded itself as the
                # container of its own entries; the folder's direct entries are played from the
                # folder
                items.append(
                    entry
                    if entry.origin is not None and entry.origin.container is not None
                    else ResolvedItem(entry.item, self._folder_origin(folder, container, item))
                )
        return items

    async def _resolve_album(
        self,
        album: Album,
        start_item: str | None,
        userid: str | None,
        queue_id: str | None,
        sort_field: SortField | None,
        sort_direction: SortDirection | None,
        keep_preceding_items: bool,
    ) -> list[ResolvedItem]:
        """
        Resolve an album to its tracks, each pinned to its copy on the one source it plays from.

        A track the album lists as another source's entry plays as the chosen source's own
        entry, when it has one. A track the chosen source does not have plays as the album
        lists it. When every track is ranked on its own, no source is chosen and nothing is
        pinned; a track that is not in the library then plays the best entry a source the
        playback user may play from serves.

        :param album: The album to play.
        :param start_item: Optional item_id/uri of the track to start from.
        :param userid: Optional user the playback is for.
        :param queue_id: Optional queue the playback is requested for.
        :param sort_field: The sort field of the album's tracks listing to play the tracks in
            before applying start_item; the disc and track order when omitted.
        :param sort_direction: Sort direction, the field's default when omitted.
        :param keep_preceding_items: Move the tracks before start_item behind the rest instead
            of dropping them, so the full album is returned with start_item first.
        """
        self.logger.info(
            "Fetching tracks to play for album %s",
            album.name,
        )
        listing = await self.mass.music.albums.playback_listing(
            album.item_id,
            album.provider,
            in_library_only=self._album_library_tracks_only(),
            sort_field=sort_field,
            sort_direction=sort_direction,
        )
        tracks = [track for track in listing.tracks if track.available]
        source, served = await self._album_source(listing, tracks, userid, queue_id)
        if source is not None:
            self.logger.debug("Playing album %s from %s", album.name, source)
        container = ItemMapping.from_item(album)
        resolved: list[ResolvedItem] = []
        for track in self._album_order(tracks, start_item, keep_preceding_items):
            if (played := served.get(cast("str", track.uri))) is not None:
                item, copy = played
                resolved.append(
                    ResolvedItem(
                        item,
                        # nothing is pinned without a source
                        QueueItemOrigin(
                            container=container,
                            provider_instance=source,
                            item_id=copy.item_id if source is not None else None,
                        ),
                    )
                )
                continue
            if source is not None:
                self.logger.debug(
                    "%s has no copy on %s, the source of album %s; it plays as the album lists it",
                    track.name,
                    source,
                    album.name,
                )
            # nothing is pinned without a source
            resolved.append(ResolvedItem(track, origin_for(album, track, pin=source is not None)))
        return resolved

    async def _album_source(
        self,
        listing: AlbumListing,
        tracks: list[Track],
        userid: str | None,
        queue_id: str | None,
    ) -> tuple[str | None, dict[str, tuple[Track, ProviderMapping]]]:
        """
        Choose the one source an album plays from, with the copy it serves of each track.

        Only the sources the playback user may play from count, each with the copies of the
        listed tracks it can serve. An account of a streaming service also serves the listings
        of that service's accounts the user may not play from. No source is chosen when
        the source selection settings pick the best copy for every track on its own; a track
        that is not in the library then plays the entry whose copy ranks best among the
        sources.

        :param listing: The album's tracks, and the entry each source lists for them.
        :param tracks: The album's tracks to play.
        :param userid: Optional user the playback is for.
        :param queue_id: Optional queue the playback is requested for.
        :return: The provider instance chosen, if any, and per track uri the item to queue for
            the track with the copy chosen for it.
        """
        # not the queue's user: the queue takes on the playback user once the play is resolved
        user = await self.mass.webserver.auth.get_user(userid) if userid else None
        allowed, preferred = playback_sources_for(self.mass, user)
        providers: dict[str, MusicProvider] = {}
        served_by: dict[str, dict[str, tuple[Track, ProviderMapping]]] = {}
        # an account's own listing counts first, then the listings it plays in place of
        # the accounts of its streaming service the user may not play from
        for instance_id, listed_ids in sorted(
            listing.listed_ids.items(),
            key=lambda entry: (allowed is not None and entry[0] not in allowed, entry[0]),
        ):
            if (serving_id := playback_instance_for(self.mass, instance_id, allowed)) is None:
                continue
            provider = self.mass.get_provider(serving_id, provider_type=MusicProvider)
            if (
                provider is None
                # an instance that went away hands its lookups to another account
                or provider.instance_id != serving_id
            ):
                continue
            providers[serving_id] = provider
            own_entries = listing.own_entries.get(instance_id, {})
            served = served_by.setdefault(serving_id, {})
            for track in tracks:
                uri = cast("str", track.uri)
                if uri in served or (item_id := listed_ids.get(uri)) is None:
                    continue
                # a track listed as another source's entry plays as this source's own entry
                for item in (track, own_entries.get(uri)):
                    if (
                        item is not None
                        and (copy := self._served_copy(item, provider, item_id)) is not None
                    ):
                        served[uri] = (item, copy)
                        break
        listed = [
            ContainerListing(
                instance_id=instance_id,
                is_streaming=providers[instance_id].is_streaming_provider,
                copies=[copy for _, copy in served.values()],
                complete=all(
                    track.uri in served for track in tracks if track.provider == "library"
                ),
            )
            for instance_id, served in served_by.items()
        ]
        policy = read_stream_source_policy(self.mass, queue_id)
        source = select_container_source(listed, preferred, policy)
        if source is None:
            return None, self._best_entries(tracks, served_by, providers, preferred, policy)
        return source, served_by[source]

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
        playlist/artist/genre/podcast/radio never lands in the play history.

        :param container: The playlist, artist, genre, podcast or radio that was asked for.
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

    def _played_from(
        self, container: MediaItemType, items: Iterable[MediaItemType], *, pin: bool = True
    ) -> list[ResolvedItem]:
        """
        Wrap the items a container resolved to with the origin that records it.

        :param container: The playlist, artist, genre, podcast or radio station that listed
            the items.
        :param items: The playable items the container resolved to.
        :param pin: Whether to pin each item to the copy the container listed for it.
        """
        return [ResolvedItem(item, origin_for(container, item, pin=pin)) for item in items]

    async def _folder_container(self, folder: BrowseFolder) -> ItemMapping:
        """
        Return the container a folder play is recorded under.

        An album folder, or a disc subfolder of one, is recorded as the library album it holds,
        so its tracks play as that album's. Any other folder is recorded as itself.

        :param folder: The browse folder being played.
        """
        if album := await self._folder_album(folder):
            return ItemMapping.from_item(album)
        # a browse folder carries no metadata to build a mapping from
        return ItemMapping(
            media_type=MediaType.FOLDER,
            item_id=folder.item_id,
            provider=folder.provider,
            name=folder.name,
        )

    async def _folder_album(self, folder: BrowseFolder) -> Album | None:
        """
        Return the library album of an album folder or of a disc subfolder, if the folder is one.

        :param folder: The browse folder being played.
        """
        if not folder.item_id:
            # a provider's root folder
            return None
        albums = self.mass.music.albums
        # a filesystem album is mapped on its folder path
        if album := await albums.get_library_item_by_prov_id(folder.item_id, folder.provider):
            return album
        parent_path, _, folder_name = folder.item_id.rpartition("/")
        # the path's last segment, as a folder played by URI is named after its whole path
        if parent_path and is_disc_dir(folder_name):
            return await albums.get_library_item_by_prov_id(parent_path, folder.provider)
        return None

    @staticmethod
    def _folder_origin(
        folder: BrowseFolder,
        container: ItemMapping,
        entry: MediaItemType | ItemMapping | BrowseFolder,
    ) -> QueueItemOrigin:
        """
        Return the origin of a folder's direct entry, pinned to the entry's own file.

        A library item stands in for its file in the listing, so it is pinned to its copy under
        the folder on the folder's provider, or to its first copy on that provider.

        :param folder: The browse folder being played.
        :param container: The container the folder play is recorded under.
        :param entry: The entry as the folder listed it.
        """
        if entry.provider != "library":
            return QueueItemOrigin(
                container=container, provider_instance=entry.provider, item_id=entry.item_id
            )
        copies = sorted(
            (
                mapping
                for mapping in getattr(entry, "provider_mappings", ())
                if mapping.provider_instance == folder.provider
            ),
            key=lambda mapping: (
                not mapping.item_id.startswith(f"{folder.item_id}/"),
                mapping.item_id,
            ),
        )
        if not copies:
            return QueueItemOrigin(container=container)
        return QueueItemOrigin(
            container=container,
            provider_instance=copies[0].provider_instance,
            item_id=copies[0].item_id,
        )

    @staticmethod
    def _own_copy(item: MediaItemType) -> tuple[str, str] | None:
        """
        Return the (provider instance, item id) of an item's own provider copy.

        A library item names no copy.

        :param item: The item to return the copy of.
        """
        if item.provider == "library":
            return None
        return (item.provider, item.item_id)

    @staticmethod
    def _pinned_origin(copy: tuple[str, str] | None) -> QueueItemOrigin | None:
        """
        Return the origin pinning an item played on its own to the given copy.

        :param copy: The (provider instance, item id) to pin to, or None for no pin.
        """
        if copy is None:
            return None
        return QueueItemOrigin(provider_instance=copy[0], item_id=copy[1])

    @staticmethod
    def _album_order(
        tracks: list[Track],
        start_item: str | None,
        keep_preceding_items: bool,
    ) -> list[Track]:
        """
        Return the tracks of an album in the order to play them, from the start item on.

        :param tracks: The album's tracks, in the order to play them.
        :param start_item: Optional item_id/uri of the track to start from; nothing plays
            when the album does not have it.
        :param keep_preceding_items: Move the tracks before start_item behind the rest instead
            of dropping them.
        """
        if start_item is None:
            return tracks
        for idx, track in enumerate(tracks):
            if start_item in (track.item_id, track.uri):
                return tracks[idx:] + (tracks[:idx] if keep_preceding_items else [])
        return []

    @staticmethod
    def _served_copy(track: Track, provider: MusicProvider, item_id: str) -> ProviderMapping | None:
        """
        Return the mapping of a track that a provider instance plays under the given item id.

        :param track: The track to play.
        :param provider: The instance to play it from.
        :param item_id: The item id the instance lists the track under.
        """
        copies = [mapping for mapping in track.provider_mappings if mapping.item_id == item_id]
        if any(
            not mapping.available and mapping.provider_instance == provider.instance_id
            for mapping in copies
        ):
            # the instance no longer finds this id, so no other account stands in for it either
            return None
        served = [
            mapping
            for mapping in copies
            if mapping.available
            and (
                mapping.provider_instance == provider.instance_id
                # another account of a streaming service plays the very same item ids
                or (provider.is_streaming_provider and mapping.provider_domain == provider.domain)
            )
        ]
        return min(
            served,
            key=lambda mapping: (
                mapping.provider_instance != provider.instance_id,
                mapping.provider_instance,
            ),
            default=None,
        )

    @staticmethod
    def _best_entries(
        tracks: list[Track],
        served_by: dict[str, dict[str, tuple[Track, ProviderMapping]]],
        providers: dict[str, MusicProvider],
        preferred: list[str],
        policy: StreamSourcePolicy,
    ) -> dict[str, tuple[Track, ProviderMapping]]:
        """
        Return the entry to play, with its copy, of each album track outside the library.

        Keyed by track uri. That is the entry whose copy would rank first at playback among
        the sources serving the track; a track no source serves is left out.

        :param tracks: The album's tracks to play.
        :param served_by: Per source, the item and copy it serves of each track uri.
        :param providers: The sources, by instance id.
        :param preferred: The provider instances the playback user owns.
        :param policy: The source selection settings to rank under.
        """
        best: dict[str, tuple[Track, ProviderMapping]] = {}
        for track in tracks:
            uri = cast("str", track.uri)
            if track.provider == "library":
                continue
            ranked = rank_stream_sources(
                [
                    SourceCandidate(
                        mapping=served[uri][1],
                        provider=providers[instance_id],
                        is_streaming=providers[instance_id].is_streaming_provider,
                    )
                    for instance_id, served in served_by.items()
                    if uri in served
                ],
                preferred=preferred,
                policy=policy,
            )
            if ranked:
                best[uri] = served_by[ranked[0].provider.instance_id][uri]
        return best
