"""Base model for a Music Quiz quiz type."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Iterable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, Final, cast

from music_assistant_models.enums import AlbumType, ExternalID, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    ItemMapping,
    Playlist,
    Track,
)

from music_assistant.constants import (
    DYNAMIC_PLAYLIST_SAMPLE_SIZE,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.controllers.music.constants import DYNAMIC_RADIO_DYNAMIC_TARGET
from music_assistant.controllers.music.recency import RecencyWindows
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.uri import parse_uri
from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import MusicQuizAnswerType

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.controllers.music.recency import RecencySnapshot
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import (
        MusicQuizConfig,
        MusicQuizGame,
        MusicQuizRound,
    )
    from music_assistant.providers.musicbrainz import MusicbrainzProvider
    from music_assistant.providers.radio_playlist import RadioPlaylistProvider

LOGGER = logging.getLogger(__name__)

MAX_ROUND_COUNT = 100
MAX_ANSWER_DURATION = 300
MAX_SOURCE_COUNT = 200
GENRE_TRACK_PAGE_SIZE = 100
MAX_GENRE_TRACK_COUNT = 500
MAX_SUGGESTION_COUNT = 12
PLAYBACK_REPLACEMENT_RESERVE = 4
QUIZ_TRACK_RECENCY_SECONDS = 24 * 60 * 60
MIN_RELEASE_YEAR = 1000
RELEASE_YEAR_LOOKUP_BUDGET_SECONDS = 2.0
SUPPORTED_SOURCE_MEDIA_TYPES: Final = frozenset(
    {
        MediaType.TRACK,
        MediaType.PLAYLIST,
        MediaType.ALBUM,
        MediaType.ARTIST,
        MediaType.GENRE,
    }
)
UNTRUSTED_RELEASE_ALBUM_TYPES: Final = frozenset(
    {
        AlbumType.COMPILATION,
        AlbumType.LIVE,
        AlbumType.SOUNDTRACK,
    }
)


def is_supported_source(media_type: MediaType, provider: str) -> bool:
    """Return whether a media item can supply Music Quiz tracks."""
    return media_type in SUPPORTED_SOURCE_MEDIA_TYPES and (
        media_type != MediaType.GENRE or provider == "library"
    )


def has_untrusted_release_year(album: Album | ItemMapping) -> bool:
    """
    Return whether an album's year is unusable as the release year of its tracks.

    An ``ItemMapping`` carries no album type or artists and is always trusted.

    :param album: Album attached to a selected track.
    """
    if not isinstance(album, Album):
        return False
    if album.album_type in UNTRUSTED_RELEASE_ALBUM_TYPES:
        return True
    return has_various_artists_credit(album)


def has_various_artists_credit(album: Album) -> bool:
    """
    Return whether an album is credited to Various Artists.

    :param album: Album whose artist credits should be inspected.
    """
    return any(
        artist.mbid == VARIOUS_ARTISTS_MBID
        or compare_strings(artist.name, VARIOUS_ARTISTS_NAME, strict=False)
        for artist in album.artists
    )


class QuizType(ABC):
    """
    Base for a Music Quiz quiz type.

    A quiz type generates the question material (rounds) for a game;
    the plugin itself drives phases, scoring and playback.
    """

    answer_type: ClassVar[MusicQuizAnswerType]
    prefetch_lyrics: ClassVar[bool] = False
    reveal_auto_advance_delay: ClassVar[float | None] = None
    completed_reveal_auto_advance_delay: ClassVar[float | None] = None

    def __init__(self, mass: MusicAssistant, config: MusicQuizConfig) -> None:
        """
        Initialize the quiz type for a single game.

        :param mass: MusicAssistant instance.
        :param config: Config of the game this quiz type generates rounds for.
        """
        self.mass = mass
        self.config = config
        self._source_track_pool: dict[str, Track] | None = None
        self._recency_snapshot: RecencySnapshot | None = None
        self._recent_track_uris: set[str] = set()

    @property
    def uses_audio(self) -> bool:
        """Return whether this quiz type uses a shared playback session."""
        return self.plays_track_before_answering or self.plays_track_on_reveal

    @property
    def plays_track_before_answering(self) -> bool:
        """Return whether a prepared track must start before answering opens."""
        return True

    @property
    def plays_track_on_reveal(self) -> bool:
        """Return whether a prepared track should start when its answer is revealed."""
        return False

    @classmethod
    async def is_available(cls, mass: MusicAssistant) -> bool:  # noqa: ARG003
        """
        Return whether this quiz type can be created.

        :param mass: MusicAssistant instance.
        """
        return True

    @classmethod
    def normalize_config(cls, config: MusicQuizConfig) -> MusicQuizConfig:
        """
        Normalize quiz-specific configuration before it is persisted.

        :param config: Raw typed game configuration.
        :return: Configuration to persist for this quiz type.
        """
        return config

    @classmethod
    def validate_config(cls, config: MusicQuizConfig) -> None:
        """
        Validate common defensive configuration limits.

        :param config: Configuration to validate.
        """
        if config.round_count < 1:
            raise InvalidDataError(
                "Music Quiz requires at least 1 round",
                translation_key="music_quiz_round_count_min",
                translation_owner=TRANSLATION_OWNER,
            )
        if config.round_count > MAX_ROUND_COUNT:
            raise InvalidDataError(
                f"Music Quiz supports at most {MAX_ROUND_COUNT} rounds",
                translation_key="music_quiz_round_count_max",
                translation_owner=TRANSLATION_OWNER,
                translation_args=[MAX_ROUND_COUNT],
            )
        if config.answer_duration < 1:
            raise InvalidDataError(
                "Answer duration must be at least 1 second",
                translation_key="music_quiz_answer_duration_min",
                translation_owner=TRANSLATION_OWNER,
            )
        if config.answer_duration > MAX_ANSWER_DURATION:
            raise InvalidDataError(
                f"Answer duration must be at most {MAX_ANSWER_DURATION} seconds",
                translation_key="music_quiz_answer_duration_max",
                translation_owner=TRANSLATION_OWNER,
                translation_args=[MAX_ANSWER_DURATION],
            )
        if len(config.source_uris) > MAX_SOURCE_COUNT:
            raise InvalidDataError(
                f"Music Quiz supports at most {MAX_SOURCE_COUNT} sources",
                translation_key="music_quiz_source_count_max",
                translation_owner=TRANSLATION_OWNER,
                translation_args=[MAX_SOURCE_COUNT],
            )

    async def initialize(self) -> None:
        """Prepare game-level content required before the game is created."""
        self._recency_snapshot = await self.mass.music.recency.snapshot(
            RecencyWindows(song_seconds=QUIZ_TRACK_RECENCY_SECONDS),
            include_partially_played=True,
        )

    def add_recent_track_uris(self, track_uris: Iterable[str]) -> None:
        """
        Treat tracks selected by an earlier game as recently played.

        :param track_uris: Track URIs to deprioritize during selection.
        """
        self._recent_track_uris.update(track_uris)

    def get_recent_track_uris(self, rounds: list[MusicQuizRound]) -> set[str]:
        """
        Return the source tracks represented by completed game rounds.

        :param rounds: Rounds from an earlier game.
        """
        return {game_round.track_uri for game_round in rounds if game_round.track_uri}

    @abstractmethod
    async def prepare_round(
        self, round_index: int, previous_rounds: list[MusicQuizRound]
    ) -> MusicQuizRound:
        """
        Prepare the round with the given index.

        :param round_index: Index of the round to prepare.
        :param previous_rounds: Rounds already prepared in earlier iterations.
        :raises InvalidDataError: If no suitable round material is available.
        :return: The prepared (not yet started) round.
        """

    @classmethod
    def serialize_game_config(
        cls,
        game: MusicQuizGame,
    ) -> dict[str, SerializableType]:
        """
        Serialize quiz-specific game configuration.

        :param game: Game whose configuration should be serialized.
        """
        return {"include_similar_music": game.config.include_similar_music}

    def reject_track(self, track_uri: str) -> None:
        """
        Remove a failed track from future round preparation.

        :param track_uri: URI of the track that failed playback.
        """
        if self._source_track_pool is not None:
            self._source_track_pool.pop(track_uri, None)

    async def _get_source_track_pool(self) -> dict[str, Track]:
        """Return all configured source tracks keyed by URI, fetched once per game."""
        if self._source_track_pool is not None:
            return self._source_track_pool
        pool: dict[str, Track] = {}
        seeds: list[MediaItemType] = []
        for source_uri in self.config.source_uris:
            # skip individual unavailable sources so one bad source does not
            # abort a round that other sources can still populate
            try:
                source_media_type, provider_instance, item_id = await parse_uri(source_uri)
                if not is_supported_source(source_media_type, provider_instance):
                    LOGGER.warning(
                        "Ignoring unsupported Music Quiz source %s (%s)",
                        source_uri,
                        source_media_type,
                    )
                    continue
                media_item = await self.mass.music.get_item(
                    media_type=source_media_type,
                    item_id=item_id,
                    provider_instance_id_or_domain=provider_instance,
                    allow_update_metadata=False,
                )
                if not isinstance(
                    media_item, Track | Playlist | Album | Artist | Genre
                ) or not is_supported_source(media_item.media_type, media_item.provider):
                    LOGGER.warning(
                        "Ignoring unsupported Music Quiz source %s (%s)",
                        source_uri,
                        media_item.media_type,
                    )
                    continue
                seeds.append(media_item)
                async for track in self._iter_source_tracks(media_item):
                    if track.uri:
                        pool[track.uri] = track
            except Exception as err:
                LOGGER.warning("Could not load Music Quiz source %s: %s", source_uri, err)
        if not pool:
            raise InvalidDataError(
                "None of the configured sources could be loaded",
                translation_key="music_quiz_sources_unavailable",
                translation_owner=TRANSLATION_OWNER,
            )
        if self.config.include_similar_music:
            await self._expand_source_track_pool(pool, seeds)
        self._source_track_pool = pool
        return pool

    async def _iter_source_tracks(
        self, media_item: Track | Playlist | Album | Artist | Genre
    ) -> AsyncGenerator[Track]:
        """
        Yield tracks from a supported Music Quiz source.

        :param media_item: Resolved source item.
        """
        if isinstance(media_item, Track):
            yield media_item
            return
        if isinstance(media_item, Playlist):
            async for track in self.mass.music.playlists.tracks(
                item_id=media_item.item_id,
                provider_instance_id_or_domain=media_item.provider,
            ):
                if isinstance(track, Track):
                    yield track
            return
        if isinstance(media_item, Album):
            tracks = await self.mass.music.albums.tracks(
                item_id=media_item.item_id,
                provider_instance_id_or_domain=media_item.provider,
            )
        elif isinstance(media_item, Artist):
            tracks = await self.mass.music.artists.tracks(
                item_id=media_item.item_id,
                provider_instance_id_or_domain=media_item.provider,
            )
        else:
            tracks = []
            offset = 0
            while offset < MAX_GENRE_TRACK_COUNT:
                page = await self.mass.music.genres.tracks(
                    item_id=media_item.item_id,
                    limit=min(GENRE_TRACK_PAGE_SIZE, MAX_GENRE_TRACK_COUNT - offset),
                    offset=offset,
                )
                tracks.extend(page)
                if len(page) < GENRE_TRACK_PAGE_SIZE:
                    break
                offset += len(page)
        for track in tracks:
            if isinstance(track, Track):
                yield track

    async def _expand_source_track_pool(
        self,
        pool: dict[str, Track],
        seeds: list[MediaItemType],
    ) -> None:
        """
        Add playable similar tracks to a resolved Music Quiz source pool.

        :param pool: Exact selected source tracks keyed by URI.
        :param seeds: Resolved selected media items used to find similar music.
        """
        radio_provider = self.mass.get_provider("radio_playlist")
        if radio_provider is None:
            LOGGER.debug(
                "Music Quiz similar-music expansion is unavailable without radio playlists"
            )
            return
        target_size = min(
            DYNAMIC_RADIO_DYNAMIC_TARGET,
            max(
                DYNAMIC_PLAYLIST_SAMPLE_SIZE,
                self.config.round_count
                + PLAYBACK_REPLACEMENT_RESERVE
                + max(self.config.suggestion_count - 1, 0),
            ),
        )
        preferred_provider_instances = sorted(
            {
                provider_instance
                for seed in seeds
                for provider_instance in (
                    seed.provider,
                    *(mapping.provider_instance for mapping in seed.provider_mappings),
                )
                if provider_instance != "library"
            }
        )
        try:
            similar_tracks = await cast("RadioPlaylistProvider", radio_provider).get_dynamic_tracks(
                seeds,
                include_base_tracks=False,
                target_size=target_size,
                preferred_provider_instances=preferred_provider_instances or None,
            )
        except Exception as err:
            LOGGER.warning("Could not expand Music Quiz sources with similar music: %s", err)
            return
        if not similar_tracks:
            LOGGER.debug("Radio playlists returned no similar tracks for Music Quiz")
            return
        initial_pool_size = len(pool)
        for track in similar_tracks:
            if isinstance(track, Track) and track.uri and track.available and track.is_playable:
                pool.setdefault(track.uri, track)
        if len(pool) == initial_pool_size:
            LOGGER.debug("Radio playlists returned no new playable tracks for Music Quiz")

    def _recency_candidates(
        self,
        tracks: list[Track],
        *,
        prefer_recent: bool = False,
    ) -> list[Track]:
        """Return the preferred recency tier, falling back to the complete candidate pool."""
        tracks_with_recency = [(track, self._track_is_recent(track)) for track in tracks]
        preferred = [
            track for track, is_recent in tracks_with_recency if is_recent is prefer_recent
        ]
        return preferred or tracks

    def _track_is_recent(self, track: Track) -> bool:
        """Return whether a track was selected or heard recently."""
        if track.uri and track.uri in self._recent_track_uris:
            return True
        return self._recency_snapshot is not None and self._recency_snapshot.track_recent(
            track, QUIZ_TRACK_RECENCY_SECONDS
        )

    async def _musicbrainz_dated_track(self, track: Track) -> tuple[Track, int | None]:
        """
        Return the track dated with the first release year MusicBrainz knows for the song.

        The track itself is returned unchanged when MusicBrainz has nothing better to offer.

        :param track: Track to date.
        :return: The dated track and the usable release year MusicBrainz knows for the song.
            The year is reported even when the track already carries that year or an earlier one,
            and is None when MusicBrainz knows no year or only an implausible one.
        """
        musicbrainz = self.mass.get_provider("musicbrainz")
        if musicbrainz is None:
            return track, None
        release_year: int | None = None
        # callers wait for this and MusicBrainz throttles to 10 requests per 10 seconds, so a
        # lookup that does not resolve within the budget leaves the track on its library year
        with suppress(TimeoutError):
            async with asyncio.timeout(RELEASE_YEAR_LOOKUP_BUDGET_SECONDS):
                try:
                    release_year = await self._musicbrainz_release_year(
                        cast("MusicbrainzProvider", musicbrainz), track
                    )
                except Exception as err:
                    LOGGER.debug("Could not date Music Quiz track %s: %s", track.uri, err)
        # a year outside this range is rejected by get_track_release_year anyway, and a year
        # below 1 cannot be expressed as a datetime at all
        if release_year is None or not MIN_RELEASE_YEAR <= release_year <= utc().year:
            return track, None
        release_date = track.metadata.release_date
        if release_date is not None and release_date.year <= release_year:
            return track, release_year
        # the track controller hands out objects that are shared with the library cache,
        # so the release date is written to a copy instead of the track itself
        dated_track = replace(
            track,
            metadata=replace(track.metadata, release_date=datetime(release_year, 1, 1, tzinfo=UTC)),
        )
        return dated_track, release_year

    @staticmethod
    async def _musicbrainz_release_year(
        musicbrainz: MusicbrainzProvider, track: Track
    ) -> int | None:
        """
        Return the first release year MusicBrainz knows for a track.

        :param musicbrainz: The loaded MusicBrainz provider.
        :param track: Track to date.
        """
        # an ISRC identifies the exact recording, so it is always the better evidence and
        # the search below is only reached when it is absent or MusicBrainz does not know it
        if isrc := track.get_external_id(ExternalID.ISRC):
            if (release_year := await musicbrainz.get_release_year_by_isrc(isrc)) is not None:
                return release_year
        artist_name = track.artists[0].name if track.artists else None
        if not artist_name or not track.name:
            return None
        return await musicbrainz.get_release_year_by_track_name(artist_name, track.name)


def get_track_release_year(track: Track) -> int | None:
    """
    Return a usable release year from a track's available metadata.

    :param track: Track whose release year should be resolved.
    """
    album = track.album
    album_year = (
        album.year
        if isinstance(album, Album | ItemMapping) and not has_untrusted_release_year(album)
        else None
    )
    track_year = (
        track.metadata.release_date.year if track.metadata.release_date is not None else None
    )
    current_year = utc().year
    valid_years = (
        year
        for year in (track_year, album_year)
        if year is not None and MIN_RELEASE_YEAR <= year <= current_year
    )
    return min(valid_years, default=None)
