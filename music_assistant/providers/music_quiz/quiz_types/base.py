"""Base model for a Music Quiz quiz type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from music_assistant.providers.music_quiz.models import MusicQuizAnswerType

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import MusicQuizConfig, MusicQuizRound


class QuizType(ABC):
    """
    Base for a Music Quiz quiz type.

    A quiz type generates the question material (rounds) for a game;
    the plugin itself drives phases, scoring and playback.
    """

    answer_type: ClassVar[MusicQuizAnswerType]

    def __init__(self, mass: MusicAssistant, config: MusicQuizConfig) -> None:
        """
        Initialize the quiz type for a single game.

        :param mass: MusicAssistant instance.
        :param config: Config of the game this quiz type generates rounds for.
        """
        self.mass = mass
        self.config = config

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
