"""Base model for a Music Quiz quiz type."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, ItemMapping, Playlist, Track

from music_assistant.helpers.datetime import utc
from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import MusicQuizAnswerType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import MusicQuizConfig, MusicQuizRound

LOGGER = logging.getLogger(__name__)

MAX_ROUND_COUNT = 100
MAX_ANSWER_DURATION = 300
MAX_SOURCE_COUNT = 200
MAX_SUGGESTION_COUNT = 12
MIN_RELEASE_YEAR = 1000


class QuizType(ABC):
    """
    Base for a Music Quiz quiz type.

    A quiz type generates the question material (rounds) for a game;
    the plugin itself drives phases, scoring and playback.
    """

    answer_type: ClassVar[MusicQuizAnswerType]
    uses_audio: ClassVar[bool] = True
    warm_up_lyrics: ClassVar[bool] = False

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
                media_item = await self.mass.music.get_item_by_uri(source_uri)
                if isinstance(media_item, Track):
                    if media_item.uri:
                        pool[media_item.uri] = media_item
                    continue
                if isinstance(media_item, Playlist):
                    async for track in self.mass.music.playlists.tracks(
                        item_id=media_item.item_id,
                        provider_instance_id_or_domain=media_item.provider,
                    ):
                        if isinstance(track, Track) and track.uri:
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
