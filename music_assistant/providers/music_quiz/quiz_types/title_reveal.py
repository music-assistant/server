"""Title reveal quiz type: guess the playing song's title while its letters are uncovered."""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.util import parse_title_and_version
from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.hints import (
    build_hint_fractions,
    build_reveal_order,
    clean_hint_title,
    hint_count,
    is_hint_title_usable,
    strip_emoji,
)
from music_assistant.providers.music_quiz.matching import allowed_typos, normalize_guess
from music_assistant.providers.music_quiz.models import (
    DEFAULT_TRIVIA_LANGUAGE,
    HintTextRoundState,
    MusicQuizAnswerType,
    MusicQuizDifficulty,
    MusicQuizRound,
    TimelineBonusMode,
)
from music_assistant.providers.music_quiz.quiz_types.base import (
    QuizType,
    get_track_artist_answers,
)
from music_assistant.providers.music_quiz.suggestions import build_answer_label

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.helpers.json import SerializableType
    from music_assistant.providers.music_quiz.models import MusicQuizConfig, MusicQuizGame

SYSTEM_RANDOM = secrets.SystemRandom()


class TitleRevealQuizType(QuizType):
    """Quiz type where players type the playing song's title as its letters are revealed."""

    answer_type = MusicQuizAnswerType.HINT_TEXT

    @classmethod
    def normalize_config(cls, config: MusicQuizConfig) -> MusicQuizConfig:
        """
        Remove configuration fields that do not apply to title reveal.

        The artist bonus mode is kept, title reveal supports it as a free-text bonus.

        :param config: Raw typed game configuration.
        :return: Configuration to persist for this quiz type.
        """
        return replace(
            config,
            difficulty=MusicQuizDifficulty.NORMAL.value,
            language=DEFAULT_TRIVIA_LANGUAGE,
            play_reveal_audio=True,
            title_bonus_mode=TimelineBonusMode.OFF,
        )

    @classmethod
    def validate_config(cls, config: MusicQuizConfig) -> None:
        """
        Validate title-reveal configuration.

        :param config: Configuration to validate.
        """
        super().validate_config(config)
        if config.artist_bonus_mode not in (TimelineBonusMode.OFF, TimelineBonusMode.FREE_TEXT):
            raise InvalidDataError(
                "Title Reveal supports only a free-text artist bonus",
                translation_key="music_quiz_invalid_bonus_mode",
                translation_owner=TRANSLATION_OWNER,
            )
        if not config.source_uris:
            raise InvalidDataError(
                "At least one source URI is required",
                translation_key="music_quiz_source_required",
                translation_owner=TRANSLATION_OWNER,
            )

    @classmethod
    def serialize_game_config(cls, game: MusicQuizGame) -> dict[str, SerializableType]:
        """
        Serialize title-reveal game configuration.

        :param game: Game whose configuration should be serialized.
        """
        return {
            **super().serialize_game_config(game),
            "hint_speed": game.config.hint_speed.value,
            "typo_tolerance": game.config.typo_tolerance.value,
            "artist_bonus_mode": game.config.artist_bonus_mode.value,
        }

    async def prepare_round(
        self, round_index: int, previous_rounds: list[MusicQuizRound]
    ) -> MusicQuizRound:
        """
        Prepare a title-reveal round from the configured sources.

        :param round_index: Index of the round to prepare.
        :param previous_rounds: Rounds already prepared in earlier iterations.
        :raises InvalidDataError: If no unused source track has a guessable title.
        :return: The prepared (not yet started) round.
        """
        track = await self._get_next_source_track(previous_rounds)
        assert track.uri is not None  # guaranteed by _get_next_source_track
        solution = clean_hint_title(track.name)
        reveal_order = build_reveal_order(solution, SYSTEM_RANDOM)
        artist_name = track.artists[0].name if track.artists else None
        artist_answers = (
            get_track_artist_answers(track)
            if self.config.artist_bonus_mode == TimelineBonusMode.FREE_TEXT
            else []
        )
        return MusicQuizRound(
            round_index=round_index,
            track_uri=track.uri,
            answer_label=build_answer_label(artist_name, solution),
            answer_state=HintTextRoundState(
                solution=solution,
                accepted_answers=_accepted_answers(track, solution),
                allowed_typos=allowed_typos(
                    len(normalize_guess(solution)), self.config.typo_tolerance
                ),
                reveal_order=reveal_order,
                hint_fractions=build_hint_fractions(
                    hint_count(len(reveal_order), self.config.hint_speed)
                ),
                artist_answers=artist_answers,
                artist_label=track.artist_str if artist_answers else None,
            ),
            image_url=await self.mass.metadata.get_image_url_for_item(track),
            duration=track.duration,
        )

    async def _get_next_source_track(self, previous_rounds: list[MusicQuizRound]) -> Track:
        """Return a random unused source track whose title makes a guessable round."""
        used_track_uris = {
            previous_round.track_uri
            for previous_round in previous_rounds
            if previous_round.track_uri
        }
        # two recordings of one song would make the second round a giveaway
        used_titles = {
            normalize_guess(previous_round.answer_state.solution)
            for previous_round in previous_rounds
            if isinstance(previous_round.answer_state, HintTextRoundState)
        }
        pool = await self._get_source_track_pool()
        available: list[Track] = []
        for uri, track in pool.items():
            if uri in used_track_uris:
                continue
            solution = clean_hint_title(track.name)
            normalized = normalize_guess(solution)
            if is_hint_title_usable(solution) and normalized and normalized not in used_titles:
                available.append(track)
        if not available:
            raise InvalidDataError(
                "No unused source tracks are available",
                translation_key="music_quiz_no_unused_source_tracks",
                translation_owner=TRANSLATION_OWNER,
            )
        return secrets.choice(self._recency_candidates(available))


def _accepted_answers(track: Track, solution: str) -> list[str]:
    """Return the normalized answers accepted for a track, the shown solution first."""
    search_title, _ = parse_title_and_version(strip_emoji(track.name), strip_for_search=True)
    return [
        answer
        for answer in dict.fromkeys(normalize_guess(title) for title in (solution, search_title))
        if answer
    ]
