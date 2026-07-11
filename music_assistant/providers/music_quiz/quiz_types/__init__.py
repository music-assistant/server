"""Quiz types for the Music Quiz provider."""

from __future__ import annotations

from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.quiz_types.base import QuizType
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import GuessTheSongQuizType
from music_assistant.providers.music_quiz.quiz_types.hitster import HitsterQuizType
from music_assistant.providers.music_quiz.quiz_types.trivia import TriviaQuizType

QUIZ_TYPES: dict[str, type[QuizType]] = {
    "guess_the_song": GuessTheSongQuizType,
    "hitster": HitsterQuizType,
    "trivia": TriviaQuizType,
}


def get_quiz_type(quiz_type: str) -> type[QuizType]:
    """
    Return the registered quiz strategy class.

    :param quiz_type: Quiz type identity to resolve.
    """
    try:
        return QUIZ_TYPES[quiz_type]
    except KeyError as err:
        raise InvalidDataError(
            f"Unknown quiz type: {quiz_type}",
            translation_key="music_quiz_unknown_quiz_type",
            translation_owner=TRANSLATION_OWNER,
        ) from err
