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
    QuizRoundAnswerState,
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
    def validate_round(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> None:
        """
        Validate answer-specific round requirements.

        :param game: Game the round belongs to.
        :param state: Prepared answer state to validate.
        """

    @abstractmethod
    def submit(
        self,
        game: MusicQuizGame,
        state: QuizRoundAnswerState,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Apply one validated player submission.

        :param game: Game receiving the submission.
        :param state: Current round answer state.
        :param player: Player submitting the answer.
        :param submission: Validated answer submission.
        :param submitted_at: Server timestamp of the submission.
        """

    @abstractmethod
    def remove_player(self, state: QuizRoundAnswerState, player_id: str) -> None:
        """
        Remove answer-specific state owned by a player.

        :param state: Round answer state to mutate.
        :param player_id: Player whose state should be removed.
        """

    @abstractmethod
    def is_player_complete(self, state: QuizRoundAnswerState, player_id: str) -> bool:
        """
        Return whether a player completed the answer requirements.

        :param state: Round answer state to inspect.
        :param player_id: Player to inspect.
        """

    @abstractmethod
    def is_round_complete(
        self,
        state: QuizRoundAnswerState,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player completed the round.

        :param state: Round answer state to inspect.
        :param active_players: Players eligible for the round.
        """

    @abstractmethod
    def reveal(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> float | None:
        """
        Finalize answer state and apply scoring.

        :param game: Game being revealed.
        :param state: Round answer state being revealed.
        :return: Timestamp of the latest submitted answer, if any.
        """

    @abstractmethod
    def serialize_game_config(self, game: MusicQuizGame) -> dict[str, SerializableType]:
        """
        Serialize answer-specific game configuration.

        :param game: Game whose configuration should be serialized.
        """

    @abstractmethod
    def serialize_host_round(self, state: QuizRoundAnswerState) -> dict[str, SerializableType]:
        """
        Serialize answer state for the host's flat round payload.

        :param state: Round answer state to serialize.
        """

    @abstractmethod
    def serialize_round(
        self, state: QuizRoundAnswerState, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize answer-specific round state.

        :param state: Round answer state to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """

    @abstractmethod
    def serialize_public_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize answer progress safe for every guest.

        :param state: Current round answer state, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """

    @abstractmethod
    def serialize_personal_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize answer state visible to its submitting player.

        :param state: Current round answer state, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
