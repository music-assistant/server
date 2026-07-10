"""Base contract for Music Quiz answer types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass
from typing import ClassVar

from music_assistant.helpers.json import SerializableType
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswerType,
    MusicQuizGame,
    MusicQuizPlayer,
    MusicQuizRound,
)


@dataclass(frozen=True, slots=True)
class QuizAnswerSubmission(ABC):
    """A validated Music Quiz answer submission."""

    answer_type: ClassVar[MusicQuizAnswerType]


class QuizAnswerType(ABC):
    """Strategy for a reusable Music Quiz answer type."""

    answer_type: ClassVar[MusicQuizAnswerType]

    @abstractmethod
    def parse_submission(self, payload: dict[str, object]) -> QuizAnswerSubmission:
        """
        Parse an answer submission received from the API.

        :param payload: Raw JSON object submitted by a player.
        :return: A strictly validated answer submission.
        """

    @abstractmethod
    def validate_round(self, game: MusicQuizGame, game_round: MusicQuizRound) -> None:
        """
        Validate answer-specific round requirements.

        :param game: Game the round belongs to.
        :param game_round: Prepared round to validate.
        """

    @abstractmethod
    def submit(
        self,
        game: MusicQuizGame,
        game_round: MusicQuizRound,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Apply one validated player submission.

        :param game: Game receiving the submission.
        :param game_round: Current answering round.
        :param player: Player submitting the answer.
        :param submission: Validated answer submission.
        :param submitted_at: Server timestamp of the submission.
        """

    @abstractmethod
    def is_player_complete(self, game_round: MusicQuizRound, player_id: str) -> bool:
        """
        Return whether a player completed the answer requirements.

        :param game_round: Round to inspect.
        :param player_id: Player to inspect.
        """

    @abstractmethod
    def is_round_complete(
        self,
        game_round: MusicQuizRound,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player completed the round.

        :param game_round: Round to inspect.
        :param active_players: Players eligible for the round.
        """

    @abstractmethod
    def reveal(self, game: MusicQuizGame, game_round: MusicQuizRound) -> None:
        """
        Finalize answer state and apply scoring.

        :param game: Game being revealed.
        :param game_round: Round being revealed.
        """

    @abstractmethod
    def serialize_round(
        self, game_round: MusicQuizRound, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize answer-specific round state.

        :param game_round: Round to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """

    @abstractmethod
    def serialize_public_player(
        self,
        game_round: MusicQuizRound | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize answer progress safe for every guest.

        :param game_round: Current round, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """

    @abstractmethod
    def serialize_personal_player(
        self,
        game_round: MusicQuizRound | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize answer state visible to its submitting player.

        :param game_round: Current round, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
