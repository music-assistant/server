"""Answer types for the Music Quiz provider."""

from __future__ import annotations

from music_assistant.providers.music_quiz.answer_types.base import QuizAnswerType
from music_assistant.providers.music_quiz.answer_types.multiple_choice import (
    MultipleChoiceAnswerType,
)
from music_assistant.providers.music_quiz.answer_types.timeline import TimelineAnswerType
from music_assistant.providers.music_quiz.errors import MusicQuizInvalidAnswerError
from music_assistant.providers.music_quiz.models import MusicQuizAnswerType

ANSWER_TYPES: dict[MusicQuizAnswerType, type[QuizAnswerType]] = {
    MusicQuizAnswerType.MULTIPLE_CHOICE: MultipleChoiceAnswerType,
    MusicQuizAnswerType.TIMELINE: TimelineAnswerType,
}


def get_answer_type(answer_type: MusicQuizAnswerType | str) -> type[QuizAnswerType]:
    """
    Return the registered answer strategy class.

    :param answer_type: Answer type identity to resolve.
    """
    try:
        answer_type_id = MusicQuizAnswerType(answer_type)
        return ANSWER_TYPES[answer_type_id]
    except (KeyError, ValueError) as err:
        raise MusicQuizInvalidAnswerError(f"Unknown answer type: {answer_type}") from err
