"""Guess the song quiz type: guess the currently playing track."""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import MusicQuizRound
from music_assistant.providers.music_quiz.quiz_types.base import QuizType
from music_assistant.providers.music_quiz.suggestions import (
    SuggestionCandidate,
    build_answer_label,
    build_suggestions,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import MusicQuizConfig

LOGGER = logging.getLogger(__name__)


class GuessTheSongQuizType(QuizType):
    """Quiz type where players guess the currently playing track."""

    def __init__(self, mass: MusicAssistant, config: MusicQuizConfig) -> None:
        """
        Initialize the guess-the-song quiz type for a single game.

        :param mass: MusicAssistant instance.
        :param config: Config of the game this quiz type generates rounds for.
        """
        super().__init__(mass, config)
        self._source_track_pool: dict[str, Track] | None = None

    async def prepare_round(
        self, round_index: int, previous_rounds: list[MusicQuizRound]
    ) -> MusicQuizRound:
        """
        Prepare a guess-the-song round from the configured sources.

        :param round_index: Index of the round to prepare.
        :param previous_rounds: Rounds already prepared in earlier iterations.
        :raises InvalidDataError: If no unused source track or not enough
            distractors are available.
        :return: The prepared (not yet started) round.
        """
        used_track_uris = {
            previous_round.track_uri
            for previous_round in previous_rounds
            if previous_round.track_uri
        }
        track = await self._get_next_source_track(used_track_uris)
        correct = _track_to_candidate(track)
        search_results = await self.mass.music.search(
            search_query=correct.label,
            media_types=[MediaType.TRACK],
            limit=max(self.config.suggestion_count * 8, 24),
            library_only=False,
        )
        distractors = [
            _track_to_candidate(item) for item in search_results.tracks if isinstance(item, Track)
        ]
        try:
            suggestions = build_suggestions(
                correct,
                distractors,
                self.config.suggestion_count,
            )
        except ValueError as err:
            raise InvalidDataError(
                str(err),
                translation_key="music_quiz_not_enough_distractors",
                translation_owner=TRANSLATION_OWNER,
            ) from err
        assert track.uri is not None  # guaranteed by _get_next_source_track
        return MusicQuizRound(
            round_index=round_index,
            track_uri=track.uri,
            answer_label=correct.label,
            suggestions=suggestions,
            image_url=await self.mass.metadata.get_image_url_for_item(track),
            duration=track.duration,
        )

    async def _get_next_source_track(self, used_track_uris: set[str]) -> Track:
        """Return a random unused track from the configured sources."""
        pool = await self._get_source_track_pool()
        available = [track for uri, track in pool.items() if uri not in used_track_uris]
        if not available:
            raise InvalidDataError(
                "No unused source tracks are available",
                translation_key="music_quiz_no_unused_source_tracks",
                translation_owner=TRANSLATION_OWNER,
            )
        return secrets.choice(available)

    async def _get_source_track_pool(self) -> dict[str, Track]:
        """Return all configured source tracks keyed by URI, fetched once per game."""
        if self._source_track_pool is not None:
            return self._source_track_pool
        if not self.config.source_uris:
            raise InvalidDataError(
                "At least one source URI is required",
                translation_key="music_quiz_source_required",
                translation_owner=TRANSLATION_OWNER,
            )
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


def _track_to_candidate(track: Track) -> SuggestionCandidate:
    """Convert a track to an answer suggestion candidate."""
    return SuggestionCandidate(
        label=build_answer_label(track.artist_str or None, track.name),
        uri=track.uri,
        title=track.name,
    )
