"""Base model for a Music Quiz quiz type."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, ClassVar, Final

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    ItemMapping,
    Playlist,
    Track,
)

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.uri import parse_uri
from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import MusicQuizAnswerType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import MusicQuizConfig, MusicQuizRound

LOGGER = logging.getLogger(__name__)

MAX_ROUND_COUNT = 100
MAX_ANSWER_DURATION = 300
MAX_SOURCE_COUNT = 200
GENRE_TRACK_PAGE_SIZE = 100
MAX_GENRE_TRACK_COUNT = 500
MAX_SUGGESTION_COUNT = 12
MIN_RELEASE_YEAR = 1000
SUPPORTED_SOURCE_MEDIA_TYPES: Final = frozenset(
    {
        MediaType.TRACK,
        MediaType.PLAYLIST,
        MediaType.ALBUM,
        MediaType.ARTIST,
        MediaType.GENRE,
    }
)


def is_supported_source(media_type: MediaType, provider: str) -> bool:
    """Return whether a media item can supply Music Quiz tracks."""
    return media_type in SUPPORTED_SOURCE_MEDIA_TYPES and (
        media_type != MediaType.GENRE or provider == "library"
    )


class QuizType(ABC):
    """
    Base for a Music Quiz quiz type.

    A quiz type generates the question material (rounds) for a game;
    the plugin itself drives phases, scoring and playback.
    """

    answer_type: ClassVar[MusicQuizAnswerType]
    uses_audio: ClassVar[bool] = True
    warm_up_lyrics: ClassVar[bool] = False
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

    @classmethod
    def is_available(cls, mass: MusicAssistant) -> bool:  # noqa: ARG003
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
        return

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

    async def _get_source_track_pool(self) -> dict[str, Track]:
        """Return all configured source tracks keyed by URI, fetched once per game."""
        if self._source_track_pool is not None:
            return self._source_track_pool
        pool: dict[str, Track] = {}
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


def get_track_release_year(track: Track) -> int | None:
    """
    Return a usable release year from a track's available metadata.

    :param track: Track whose release year should be resolved.
    """
    album = track.album
    year = album.year if isinstance(album, Album | ItemMapping) else None
    current_year = utc().year
    if year is not None and MIN_RELEASE_YEAR <= year <= current_year:
        return year
    if track.metadata.release_date is not None:
        year = track.metadata.release_date.year
        if MIN_RELEASE_YEAR <= year <= current_year:
            return year
    return None
