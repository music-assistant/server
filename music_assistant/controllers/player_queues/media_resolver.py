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

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    Genre,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
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

if TYPE_CHECKING:
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
        self, album: Album, start_item: str | None, sort_by: str | None = None
    ) -> list[Track]:
        """Return tracks for given album, based on user preference."""
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
                    return result[idx:]
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

    async def get_playlist_tracks(
        self,
        playlist: Playlist,
        start_item: str | None,
        sort_by: str | None = None,
    ) -> list[PlaylistPlayableItem]:
        """Return tracks for given playlist, based on user preference."""
        result: list[PlaylistPlayableItem] = []
        self.logger.info(
            "Fetching tracks to play for playlist %s",
            playlist.name,
        )
        force_refresh = playlist.is_dynamic
        needs_sort = sort_by is not None and sort_by != "position"
        # Fast path: no re-sort needed, skip-until-found in a single pass
        # so we don't materialize huge playlists when starting near the end.
        if not needs_sort:
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
        # Sort path: must materialize all tracks before sorting, then slice.
        async for playlist_track in self.mass.music.playlists.tracks(
            playlist.item_id,
            playlist.provider,
            force_refresh=force_refresh,
            allow_dynamic_tracks=playlist.is_dynamic,
        ):
            if not playlist_track.available:
                continue
            result.append(playlist_track)
        result = sort_tracks(result, cast("str", sort_by))
        if start_item is not None:
            for idx, track in enumerate(result):
                if _start_item_matches(start_item, track):
                    return result[idx:]
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
    ) -> UniqueList[PodcastEpisode]:
        """
        Return the next episode(s) and resume point for the given podcast.

        :param podcast: Podcast to enqueue, or `None` if `episode` is a
            concrete `PodcastEpisode`.
        :param episode: A concrete `PodcastEpisode`, an `item_id` / `uri`,
            a case-insensitive substring of an episode name, or one of the
            reserved lowercase keywords `"latest"` / `"newest"`.
        :param userid: User whose resume position should be applied.
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
            (
                fully_played,
                resume_position_ms,
            ) = await self.mass.music.get_resume_position(episode, userid=userid)
            episode.fully_played = fully_played
            episode.resume_position_ms = 0 if fully_played else resume_position_ms
            return UniqueList([episode])
        # podcast with optional start episode requested
        self.logger.debug(
            "Fetching episode(s) and resume point to play for Podcast %s",
            podcast.name,
        )
        # Require exact case and keyword match to minimise false positives.
        if isinstance(episode, str) and episode in _LATEST_EPISODE_KEYWORDS:
            # provider yields newest-first, so only pull the first episode here and skip
            # materialising the rest, which avoids a per-episode resume lookup on each one
            latest = await anext(
                self.mass.music.podcasts.episodes(podcast.item_id, podcast.provider), None
            )
            if latest is None:
                raise InvalidDataError(
                    f"Unable to resolve episode to play for Podcast {podcast.name}"
                )
            (
                fully_played,
                resume_position_ms,
            ) = await self.mass.music.get_resume_position(latest, userid=userid)
            latest.fully_played = fully_played
            latest.resume_position_ms = 0 if fully_played else resume_position_ms
            return UniqueList([latest])
        all_episodes = [
            x async for x in self.mass.music.podcasts.episodes(podcast.item_id, podcast.provider)
        ]
        all_episodes.sort(key=lambda x: x.position)
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
        # get the index of the episode
        episode_index = all_episodes.index(resolved_episode)
        # return the (remaining) episode(s) to play
        return UniqueList(all_episodes[episode_index:])

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
    ) -> list[MediaItemType]:
        """Resolve/unwrap media items to enqueue."""
        # resolve Itemmapping to full media item
        if isinstance(media_item, ItemMapping):
            if media_item.uri is None:
                raise InvalidDataError("ItemMapping has no URI")
            media_item = await self.mass.music.get_item_by_uri(media_item.uri)
        if media_item.media_type == MediaType.PLAYLIST:
            media_item = cast("Playlist", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_playlist_tracks(media_item, start_item, sort_by=sort_by))
        if media_item.media_type == MediaType.ARTIST:
            media_item = cast("Artist", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_artist_tracks(media_item))
        if media_item.media_type == MediaType.ALBUM:
            media_item = cast("Album", media_item)
            return list(await self.get_album_tracks(media_item, start_item, sort_by=sort_by))
        if media_item.media_type == MediaType.GENRE:
            media_item = cast("Genre", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_genre_tracks(media_item, start_item))
        if media_item.media_type == MediaType.AUDIOBOOK:
            media_item = cast("Audiobook", media_item)
            # ensure we grab the correct/latest resume point info
            media_item.resume_position_ms = await self.get_audiobook_resume_point(
                media_item, start_item, userid=userid
            )
            return [media_item]
        if media_item.media_type == MediaType.PODCAST:
            media_item = cast("Podcast", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_next_podcast_episodes(media_item, start_item, userid=userid))
        if media_item.media_type == MediaType.PODCAST_EPISODE:
            media_item = cast("PodcastEpisode", media_item)
            return list(await self.get_next_podcast_episodes(None, media_item, userid=userid))
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
