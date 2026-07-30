"""Multiple-choice answer strategy for Music Quiz."""

from __future__ import annotations

from collections.abc import Collection, Mapping
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
    MultipleChoiceAnswer,
    MultipleChoiceRoundState,
    MusicQuizAnswerType,
    MusicQuizGame,
    MusicQuizPlayer,
    QuizRoundAnswerState,
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

    def parse_submission(self, payload: Mapping[str, object]) -> QuizAnswerSubmission:
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

    def validate_round(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> None:
        """
        Validate a multiple-choice round.

        :param game: Game the round belongs to.
        :param state: Prepared answer state to validate.
        """
        multiple_choice_state = self._get_state(state)
        if sum(1 for suggestion in multiple_choice_state.suggestions if suggestion.is_correct) != 1:
            raise InvalidDataError("Round requires exactly one correct suggestion")
        if len(multiple_choice_state.suggestions) != game.config.suggestion_count:
            raise InvalidDataError("Round suggestion count does not match the game config")

    def submit(
        self,
        game: MusicQuizGame,
        state: QuizRoundAnswerState,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Lock a player's first multiple-choice selection.

        :param game: Game receiving the submission.
        :param state: Current round answer state.
        :param player: Player submitting the answer.
        :param submission: Validated multiple-choice selection.
        :param submitted_at: Server timestamp of the submission.
        """
        multiple_choice_state = self._get_state(state)
        if not isinstance(submission, MultipleChoiceSubmission):
            raise MusicQuizInvalidAnswerError(
                "Submission does not match the multiple-choice answer type"
            )
        if player.player_id in multiple_choice_state.answers:
            raise MusicQuizAlreadyAnsweredError("Player already answered this round")
        suggestion = next(
            (
                item
                for item in multiple_choice_state.suggestions
                if item.suggestion_id == submission.suggestion_id
            ),
            None,
        )
        if suggestion is None:
            raise MusicQuizUnknownSuggestionError("Unknown suggestion")
        multiple_choice_state.answers[player.player_id] = MultipleChoiceAnswer(
            player_id=player.player_id,
            suggestion_id=submission.suggestion_id,
            answered_at=submitted_at,
            is_correct=suggestion.is_correct,
        )

    def remove_player(self, state: QuizRoundAnswerState, player_id: str) -> None:
        """
        Remove a player's multiple-choice answer.

        :param state: Round answer state to mutate.
        :param player_id: Player whose answer should be removed.
        """
        multiple_choice_state = self._get_state(state)
        multiple_choice_state.answers.pop(player_id, None)

    def is_player_complete(self, state: QuizRoundAnswerState, player_id: str) -> bool:
        """
        Return whether a player locked a selection.

        :param state: Round answer state to inspect.
        :param player_id: Player to inspect.
        """
        multiple_choice_state = self._get_state(state)
        return player_id in multiple_choice_state.answers

    def is_round_complete(
        self,
        state: QuizRoundAnswerState,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player locked a selection.

        :param state: Round answer state to inspect.
        :param active_players: Players eligible for the round.
        """
        multiple_choice_state = self._get_state(state)
        return bool(active_players) and all(
            player.player_id in multiple_choice_state.answers for player in active_players
        )

    def reveal(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> float | None:
        """
        Score correct selections by submission speed.

        :param game: Game being revealed.
        :param state: Round answer state being revealed.
        :return: Timestamp of the latest submitted answer, if any.
        """
        multiple_choice_state = self._get_state(state)
        correct_answer_order = [
            answer.player_id
            for answer in sorted(
                multiple_choice_state.answers.values(), key=lambda item: item.answered_at
            )
            if answer.is_correct
        ]
        scores = calculate_linear_scores(correct_answer_order)
        for answer in multiple_choice_state.answers.values():
            answer.points = scores.get(answer.player_id, 0)
            game.players[answer.player_id].score += answer.points
        return max(
            (answer.answered_at for answer in multiple_choice_state.answers.values()),
            default=None,
        )

    def serialize_game_config(self, game: MusicQuizGame) -> dict[str, SerializableType]:
        """
        Serialize multiple-choice game configuration.

        :param game: Game whose configuration should be serialized.
        """
        return {"suggestion_count": game.config.suggestion_count}

    def serialize_host_round(self, state: QuizRoundAnswerState) -> dict[str, SerializableType]:
        """
        Serialize multiple-choice state for the host's flat round payload.

        :param state: Round answer state to serialize.
        """
        multiple_choice_state = self._get_state(state)
        return {
            "suggestions": [
                {
                    "suggestion_id": suggestion.suggestion_id,
                    "label": suggestion.label,
                    "uri": suggestion.uri,
                    "is_correct": suggestion.is_correct,
                }
                for suggestion in multiple_choice_state.suggestions
            ],
            "answers": {
                player_id: {
                    "player_id": answer.player_id,
                    "suggestion_id": answer.suggestion_id,
                    "answered_at": answer.answered_at,
                    "is_correct": answer.is_correct,
                    "points": answer.points,
                }
                for player_id, answer in multiple_choice_state.answers.items()
            },
        }

    def serialize_round(
        self, state: QuizRoundAnswerState, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize multiple-choice round state.

        :param state: Round answer state to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """
        multiple_choice_state = self._get_state(state)
        serialized_state: dict[str, SerializableType] = {
            "suggestions": [
                {"suggestion_id": suggestion.suggestion_id, "label": suggestion.label}
                for suggestion in multiple_choice_state.suggestions
            ]
        }
        if revealed:
            serialized_state["correct_suggestion_id"] = next(
                (
                    suggestion.suggestion_id
                    for suggestion in multiple_choice_state.suggestions
                    if suggestion.is_correct
                ),
                None,
            )
        return serialized_state

    def serialize_public_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's public multiple-choice progress.

        :param state: Current round answer state, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {"answered": False}
        multiple_choice_state = self._get_state(state)
        answer = multiple_choice_state.answers.get(player_id)
        serialized_state: dict[str, SerializableType] = {"answered": answer is not None}
        if revealed and answer:
            serialized_state["last_answer"] = {
                "suggestion_id": answer.suggestion_id,
                "correct": answer.is_correct,
                "points": answer.points,
            }
        return serialized_state

    def serialize_personal_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's personal multiple-choice answer.

        :param state: Current round answer state, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {}
        multiple_choice_state = self._get_state(state)
        answer = multiple_choice_state.answers.get(player_id)
        if answer is None:
            return {}
        serialized_answer: dict[str, SerializableType] = {
            "suggestion_id": answer.suggestion_id,
            "answered_at": answer.answered_at,
        }
        if revealed:
            serialized_answer.update(correct=answer.is_correct, points=answer.points)
        return {"answer": serialized_answer}

    def _get_state(self, state: QuizRoundAnswerState) -> MultipleChoiceRoundState:
        """Return validated multiple-choice round state."""
        if state.answer_type != self.answer_type or not isinstance(state, MultipleChoiceRoundState):
            raise InvalidDataError("Round state does not match the multiple-choice answer type")
        return state
