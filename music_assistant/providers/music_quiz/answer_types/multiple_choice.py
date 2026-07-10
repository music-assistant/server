"""Multiple-choice answer strategy for Music Quiz."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import ClassVar

from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import SerializableType
from music_assistant.providers.music_quiz.answer_types.base import (
    QuizAnswerSubmission,
    QuizAnswerType,
)
from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizInvalidAnswerError,
    MusicQuizUnknownSuggestionError,
)
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswer,
    MusicQuizAnswerType,
    MusicQuizGame,
    MusicQuizPlayer,
    MusicQuizRound,
)
from music_assistant.providers.music_quiz.scoring import calculate_linear_scores


@dataclass(frozen=True, slots=True)
class MultipleChoiceSubmission(QuizAnswerSubmission):
    """A validated multiple-choice selection."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.MULTIPLE_CHOICE
    suggestion_id: str


class MultipleChoiceAnswerType(QuizAnswerType):
    """Multiple-choice answer strategy."""

    answer_type = MusicQuizAnswerType.MULTIPLE_CHOICE

    def parse_submission(self, payload: dict[str, object]) -> MultipleChoiceSubmission:
        """
        Parse a multiple-choice submission.

        :param payload: Raw JSON object submitted by a player.
        :return: A validated multiple-choice selection.
        """
        expected_keys = {"answer_type", "suggestion_id"}
        if payload.keys() != expected_keys:
            raise MusicQuizInvalidAnswerError(
                "Multiple-choice submissions require answer_type and suggestion_id"
            )
        answer_type = payload["answer_type"]
        if not isinstance(answer_type, str) or answer_type != self.answer_type:
            raise MusicQuizInvalidAnswerError("Answer type must be multiple_choice")
        suggestion_id = payload["suggestion_id"]
        if not isinstance(suggestion_id, str) or not suggestion_id:
            raise MusicQuizInvalidAnswerError("Suggestion ID must be a non-empty string")
        return MultipleChoiceSubmission(suggestion_id=suggestion_id)

    def validate_round(self, game: MusicQuizGame, game_round: MusicQuizRound) -> None:
        """
        Validate a multiple-choice round.

        :param game: Game the round belongs to.
        :param game_round: Prepared round to validate.
        """
        if sum(1 for suggestion in game_round.suggestions if suggestion.is_correct) != 1:
            raise InvalidDataError("Round requires exactly one correct suggestion")
        if len(game_round.suggestions) != game.config.suggestion_count:
            raise InvalidDataError("Round suggestion count does not match the game config")

    def submit(
        self,
        game: MusicQuizGame,
        game_round: MusicQuizRound,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Lock a player's first multiple-choice selection.

        :param game: Game receiving the submission.
        :param game_round: Current answering round.
        :param player: Player submitting the answer.
        :param submission: Validated multiple-choice selection.
        :param submitted_at: Server timestamp of the submission.
        """
        if not isinstance(submission, MultipleChoiceSubmission):
            raise MusicQuizInvalidAnswerError(
                "Submission does not match the multiple-choice answer type"
            )
        if player.player_id in game_round.answers:
            raise MusicQuizAlreadyAnsweredError("Player already answered this round")
        suggestion = next(
            (
                item
                for item in game_round.suggestions
                if item.suggestion_id == submission.suggestion_id
            ),
            None,
        )
        if suggestion is None:
            raise MusicQuizUnknownSuggestionError("Unknown suggestion")
        game_round.answers[player.player_id] = MusicQuizAnswer(
            player_id=player.player_id,
            suggestion_id=submission.suggestion_id,
            answered_at=submitted_at,
            is_correct=suggestion.is_correct,
        )

    def is_player_complete(self, game_round: MusicQuizRound, player_id: str) -> bool:
        """
        Return whether a player locked a selection.

        :param game_round: Round to inspect.
        :param player_id: Player to inspect.
        """
        return player_id in game_round.answers

    def is_round_complete(
        self,
        game_round: MusicQuizRound,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player locked a selection.

        :param game_round: Round to inspect.
        :param active_players: Players eligible for the round.
        """
        return bool(active_players) and all(
            self.is_player_complete(game_round, player.player_id) for player in active_players
        )

    def reveal(self, game: MusicQuizGame, game_round: MusicQuizRound) -> None:
        """
        Score correct selections by submission speed.

        :param game: Game being revealed.
        :param game_round: Round being revealed.
        """
        correct_answer_order = [
            answer.player_id
            for answer in sorted(game_round.answers.values(), key=lambda item: item.answered_at)
            if answer.is_correct
        ]
        scores = calculate_linear_scores(correct_answer_order)
        for answer in game_round.answers.values():
            answer.points = scores.get(answer.player_id, 0)
            game.players[answer.player_id].score += answer.points
        game_round.ended_at = max(
            [answer.answered_at for answer in game_round.answers.values()],
            default=game_round.started_at or 0,
        )

    def serialize_round(
        self, game_round: MusicQuizRound, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize multiple-choice round state.

        :param game_round: Round to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """
        state: dict[str, SerializableType] = {
            "suggestions": [
                {"suggestion_id": suggestion.suggestion_id, "label": suggestion.label}
                for suggestion in game_round.suggestions
            ]
        }
        if revealed:
            state["correct_suggestion_id"] = next(
                (
                    suggestion.suggestion_id
                    for suggestion in game_round.suggestions
                    if suggestion.is_correct
                ),
                None,
            )
        return state

    def serialize_public_player(
        self,
        game_round: MusicQuizRound | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's public multiple-choice progress.

        :param game_round: Current round, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """
        answer = game_round.answers.get(player_id) if game_round else None
        state: dict[str, SerializableType] = {"answered": answer is not None}
        if revealed and answer:
            state["last_answer"] = {
                "suggestion_id": answer.suggestion_id,
                "correct": answer.is_correct,
                "points": answer.points,
            }
        return state

    def serialize_personal_player(
        self,
        game_round: MusicQuizRound | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's personal multiple-choice answer.

        :param game_round: Current round, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
        answer = game_round.answers.get(player_id) if game_round else None
        if answer is None:
            return {}
        serialized_answer: dict[str, SerializableType] = {
            "suggestion_id": answer.suggestion_id,
            "answered_at": answer.answered_at,
        }
        if revealed:
            serialized_answer.update(correct=answer.is_correct, points=answer.points)
        return {"answer": serialized_answer}
