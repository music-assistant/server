"""Timeline answer strategy for Music Quiz."""

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
from music_assistant.providers.music_quiz.matching import compare_free_text_answer
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswerType,
    MusicQuizGame,
    MusicQuizPlayer,
    QuizRoundAnswerState,
    TimelineAnswerResult,
    TimelineBonusAnswer,
    TimelineBonusDefinition,
    TimelineBonusMode,
    TimelineBonusResult,
    TimelineBonusType,
    TimelineChoiceBonusAnswer,
    TimelineEntry,
    TimelineFreeTextBonusDefinition,
    TimelineMultipleChoiceBonusDefinition,
    TimelinePlacementAnswer,
    TimelinePlacementResult,
    TimelineRoundState,
    TimelineTextBonusAnswer,
)
from music_assistant.providers.music_quiz.scoring import calculate_linear_scores
from music_assistant.providers.music_quiz.suggestions import normalize_answer_label

MAX_BONUS_TEXT_LENGTH = 200
MAX_CLIENT_ID_LENGTH = 200
BONUS_POINTS = 250


@dataclass(frozen=True, slots=True)
class TimelinePlacementSubmission(QuizAnswerSubmission):
    """A validated placement against the current timeline snapshot."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.TIMELINE
    action: ClassVar[str] = "place"
    previous_entry_id: str | None
    next_entry_id: str | None


@dataclass(frozen=True, slots=True)
class TimelineBonusTextSubmission(QuizAnswerSubmission):
    """A validated free-text timeline bonus submission."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.TIMELINE
    action: ClassVar[str] = "bonus_text"
    bonus_type: TimelineBonusType
    value: str


@dataclass(frozen=True, slots=True)
class TimelineBonusChoiceSubmission(QuizAnswerSubmission):
    """A validated multiple-choice timeline bonus submission."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.TIMELINE
    action: ClassVar[str] = "bonus_choice"
    bonus_type: TimelineBonusType
    option_id: str


@dataclass(frozen=True, slots=True)
class TimelineFinishSubmission(QuizAnswerSubmission):
    """A validated request to finish a timeline answer."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.TIMELINE
    action: ClassVar[str] = "finish"


class TimelineAnswerType(QuizAnswerType):
    """Timeline placement and optional bonus answer strategy."""

    answer_type = MusicQuizAnswerType.TIMELINE

    def parse_submission(self, payload: dict[str, object]) -> QuizAnswerSubmission:
        """
        Parse a strict timeline submission.

        :param payload: Raw JSON object submitted by a player.
        :return: A validated timeline action.
        """
        answer_type = payload.get("answer_type")
        if not isinstance(answer_type, str) or answer_type != self.answer_type:
            raise MusicQuizInvalidAnswerError("Answer type must be timeline")
        action = payload.get("action")
        if not isinstance(action, str):
            raise MusicQuizInvalidAnswerError("Timeline submissions require an action string")
        if action == TimelinePlacementSubmission.action:
            return self._parse_placement(payload)
        if action == TimelineBonusTextSubmission.action:
            return self._parse_bonus_text(payload)
        if action == TimelineBonusChoiceSubmission.action:
            return self._parse_bonus_choice(payload)
        if action == TimelineFinishSubmission.action:
            if payload.keys() != {"answer_type", "action"}:
                raise MusicQuizInvalidAnswerError(
                    "Timeline finish submissions require only answer_type and action"
                )
            return TimelineFinishSubmission()
        raise MusicQuizInvalidAnswerError(f"Unknown timeline action: {action}")

    def validate_round(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> None:
        """
        Validate a timeline round and its protected answer material.

        :param game: Game the round belongs to.
        :param state: Prepared answer state to validate.
        """
        timeline_state = self._get_state(state)
        if not timeline_state.placement_snapshot:
            raise InvalidDataError("Timeline rounds require a non-empty placement snapshot")
        if timeline_state.placement_snapshot != self._sorted_timeline(
            timeline_state.placement_snapshot
        ):
            raise InvalidDataError("Timeline placement snapshot must be deterministically ordered")
        entries = [*timeline_state.placement_snapshot, timeline_state.current_entry]
        entry_ids = [entry.entry_id for entry in entries]
        track_uris = [entry.track_uri for entry in entries]
        if any(not entry_id for entry_id in entry_ids) or len(entry_ids) != len(set(entry_ids)):
            raise InvalidDataError("Timeline entry IDs must be non-empty and unique")
        if any(not track_uri for track_uri in track_uris) or len(track_uris) != len(
            set(track_uris)
        ):
            raise InvalidDataError("Timeline track URIs must be non-empty and unique")
        if any(entry.release_year < 1 for entry in entries):
            raise InvalidDataError("Timeline entries require a release year")
        if sum(entry.is_anchor for entry in timeline_state.placement_snapshot) != 1:
            raise InvalidDataError("Timeline placement snapshot requires exactly one anchor")
        if timeline_state.current_entry.is_anchor:
            raise InvalidDataError("The scored timeline entry cannot be the anchor")
        if (
            timeline_state.placements
            or timeline_state.bonus_answers
            or timeline_state.finished_at
            or timeline_state.results
            or timeline_state.revealed
        ):
            raise InvalidDataError("A prepared timeline round must not contain player answers")
        self._validate_bonus_definitions(game, timeline_state.bonus_definitions)

    def submit(
        self,
        game: MusicQuizGame,
        state: QuizRoundAnswerState,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Apply one timeline action to a player's locked answer.

        :param game: Game receiving the submission.
        :param state: Current round answer state.
        :param player: Player submitting the answer.
        :param submission: Validated timeline action.
        :param submitted_at: Server timestamp of the submission.
        """
        timeline_state = self._get_state(state)
        player_id = player.player_id
        if player_id in timeline_state.finished_at:
            raise MusicQuizInvalidAnswerError("No actions are allowed after finishing the round")
        if isinstance(submission, TimelinePlacementSubmission):
            self._submit_placement(game, timeline_state, player_id, submission, submitted_at)
            return
        if isinstance(submission, TimelineBonusTextSubmission):
            self._submit_bonus_text(timeline_state, player_id, submission, submitted_at)
            return
        if isinstance(submission, TimelineBonusChoiceSubmission):
            self._submit_bonus_choice(timeline_state, player_id, submission, submitted_at)
            return
        if isinstance(submission, TimelineFinishSubmission):
            if player_id not in timeline_state.placements:
                raise MusicQuizInvalidAnswerError("Place the song before finishing the round")
            timeline_state.finished_at[player_id] = submitted_at
            return
        raise MusicQuizInvalidAnswerError("Submission does not match the timeline answer type")

    def is_player_complete(self, state: QuizRoundAnswerState, player_id: str) -> bool:
        """
        Return whether a player completed the timeline round.

        :param state: Round answer state to inspect.
        :param player_id: Player to inspect.
        """
        return player_id in self._get_state(state).finished_at

    def is_round_complete(
        self,
        state: QuizRoundAnswerState,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player completed the timeline round.

        :param state: Round answer state to inspect.
        :param active_players: Players eligible for the round.
        """
        timeline_state = self._get_state(state)
        return bool(active_players) and all(
            player.player_id in timeline_state.finished_at for player in active_players
        )

    def reveal(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> float | None:
        """
        Score placements and submitted bonuses, then reveal the timeline entry.

        :param game: Game being revealed.
        :param state: Round answer state being revealed.
        :return: Timestamp of the latest submitted timeline action, if any.
        """
        timeline_state = self._get_state(state)
        if timeline_state.revealed or timeline_state.results:
            raise InvalidDataError("Timeline round was already revealed")
        placement_player_ids = set(timeline_state.placements)
        if (
            set(timeline_state.bonus_answers) - placement_player_ids
            or set(timeline_state.finished_at) - placement_player_ids
        ):
            raise InvalidDataError("Timeline progress exists without a locked placement")
        placement_correctness = {
            player_id: self._is_correct_placement(
                timeline_state.placement_snapshot,
                timeline_state.current_entry.release_year,
                placement,
            )
            for player_id, placement in timeline_state.placements.items()
        }
        correct_order = [
            player_id
            for player_id, placement in sorted(
                timeline_state.placements.items(),
                key=lambda item: (item[1].answered_at, item[0]),
            )
            if placement_correctness[player_id]
        ]
        placement_scores = calculate_linear_scores(correct_order)
        definitions = self._definitions_by_type(timeline_state.bonus_definitions)
        pending_results: dict[str, TimelineAnswerResult] = {}
        score_increments: dict[str, int] = {}
        timestamps: list[float] = []
        for player_id, placement in timeline_state.placements.items():
            if player_id not in game.players:
                raise InvalidDataError("Timeline placement belongs to an unknown player")
            timestamps.append(placement.answered_at)
            placement_result = TimelinePlacementResult(
                previous_entry_id=placement.previous_entry_id,
                next_entry_id=placement.next_entry_id,
                is_correct=placement_correctness[player_id],
                points=placement_scores.get(player_id, 0),
            )
            bonus_results: list[TimelineBonusResult] = []
            for bonus_answer in self._bonus_answers_by_type(timeline_state, player_id).values():
                timestamps.append(bonus_answer.submitted_at)
                definition = definitions.get(bonus_answer.bonus_type)
                if definition is None:
                    raise InvalidDataError("Timeline bonus answer has no matching definition")
                is_correct = self._is_bonus_correct(definition, bonus_answer)
                bonus_results.append(
                    TimelineBonusResult(
                        bonus_type=bonus_answer.bonus_type,
                        is_correct=is_correct,
                        points=BONUS_POINTS if is_correct else 0,
                    )
                )
            pending_results[player_id] = TimelineAnswerResult(
                placement=placement_result,
                bonuses=sorted(bonus_results, key=lambda item: item.bonus_type.value),
            )
            score_increments[player_id] = placement_result.points + sum(
                result.points for result in bonus_results
            )
        timestamps.extend(timeline_state.finished_at.values())
        timeline_state.results = pending_results
        timeline_state.revealed = True
        for player_id, points in score_increments.items():
            game.players[player_id].score += points
        return max(timestamps, default=None)

    def serialize_game_config(self, game: MusicQuizGame) -> dict[str, SerializableType]:
        """
        Serialize timeline bonus configuration.

        :param game: Game whose configuration should be serialized.
        """
        return {
            "artist_bonus_mode": game.config.artist_bonus_mode,
            "title_bonus_mode": game.config.title_bonus_mode,
        }

    def serialize_host_round(self, state: QuizRoundAnswerState) -> dict[str, SerializableType]:
        """
        Serialize full timeline state for the host's flat round payload.

        :param state: Round answer state to serialize.
        """
        timeline_state = self._get_state(state)
        return {
            "placement_snapshot": [
                self._serialize_entry(entry) for entry in timeline_state.placement_snapshot
            ],
            "current_entry": self._serialize_entry(timeline_state.current_entry),
            "bonus_definitions": [
                self._serialize_host_bonus_definition(definition)
                for definition in timeline_state.bonus_definitions
            ],
            "placements": {
                player_id: self._serialize_placement(placement)
                for player_id, placement in timeline_state.placements.items()
            },
            "bonus_answers": {
                player_id: [self._serialize_host_bonus_answer(answer) for answer in answers]
                for player_id, answers in timeline_state.bonus_answers.items()
            },
            "finished_at": dict(timeline_state.finished_at),
            "results": {
                player_id: self._serialize_result(result)
                for player_id, result in timeline_state.results.items()
            },
            "revealed": timeline_state.revealed,
        }

    def serialize_round(
        self, state: QuizRoundAnswerState, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize a redacted or revealed timeline round.

        :param state: Round answer state to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """
        timeline_state = self._get_state(state)
        if revealed != timeline_state.revealed:
            raise InvalidDataError("Timeline reveal state does not match the game phase")
        serialized_state: dict[str, SerializableType] = {
            "timeline": [
                self._serialize_entry(entry)
                for entry in (
                    self._timeline_with_current(timeline_state)
                    if revealed
                    else timeline_state.placement_snapshot
                )
            ],
            "bonus_definitions": [
                self._serialize_public_bonus_definition(definition)
                for definition in timeline_state.bonus_definitions
            ],
        }
        if revealed:
            serialized_state["revealed_entry"] = self._serialize_entry(timeline_state.current_entry)
        return serialized_state

    def serialize_public_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's public timeline progress and revealed result.

        :param state: Current round answer state, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {
                "answered": False,
                "placed": False,
                "artist_bonus_answered": False,
                "title_bonus_answered": False,
            }
        timeline_state = self._get_state(state)
        bonus_types = set(self._bonus_answers_by_type(timeline_state, player_id))
        serialized_state: dict[str, SerializableType] = {
            "answered": player_id in timeline_state.finished_at,
            "placed": player_id in timeline_state.placements,
            "artist_bonus_answered": TimelineBonusType.ARTIST in bonus_types,
            "title_bonus_answered": TimelineBonusType.TITLE in bonus_types,
        }
        if revealed and (result := timeline_state.results.get(player_id)):
            last_answer: dict[str, SerializableType] = {
                "placement": {
                    "previous_entry_id": result.placement.previous_entry_id,
                    "next_entry_id": result.placement.next_entry_id,
                    "correct": result.placement.is_correct,
                    "points": result.placement.points,
                }
            }
            for bonus_result in result.bonuses:
                last_answer[bonus_result.bonus_type.value] = {
                    "correct": bonus_result.is_correct,
                    "points": bonus_result.points,
                }
            serialized_state["last_answer"] = last_answer
        return serialized_state

    def serialize_personal_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's personal timeline answer.

        :param state: Current round answer state, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {}
        timeline_state = self._get_state(state)
        placement = timeline_state.placements.get(player_id)
        if placement is None:
            return {}
        serialized_answer: dict[str, SerializableType] = {
            "previous_entry_id": placement.previous_entry_id,
            "next_entry_id": placement.next_entry_id,
            "answered_at": placement.answered_at,
            "bonuses": [
                self._serialize_personal_bonus_answer(answer)
                for answer in timeline_state.bonus_answers.get(player_id, [])
            ],
            "finished": player_id in timeline_state.finished_at,
        }
        if revealed and (result := timeline_state.results.get(player_id)):
            serialized_answer.update(
                correct=result.placement.is_correct,
                points=result.placement.points,
                bonus_results=[
                    {
                        "bonus_type": bonus_result.bonus_type.value,
                        "correct": bonus_result.is_correct,
                        "points": bonus_result.points,
                    }
                    for bonus_result in result.bonuses
                ],
            )
        return {"answer": serialized_answer}

    def _parse_placement(self, payload: dict[str, object]) -> TimelinePlacementSubmission:
        """Parse a strict timeline placement submission."""
        expected_keys = {
            "answer_type",
            "action",
            "previous_entry_id",
            "next_entry_id",
        }
        if payload.keys() != expected_keys:
            raise MusicQuizInvalidAnswerError(
                "Timeline placements require answer_type, action and both neighboring entry IDs"
            )
        previous_entry_id = self._parse_client_id(
            payload["previous_entry_id"], "Previous entry ID", nullable=True
        )
        next_entry_id = self._parse_client_id(
            payload["next_entry_id"], "Next entry ID", nullable=True
        )
        return TimelinePlacementSubmission(previous_entry_id, next_entry_id)

    def _parse_bonus_text(self, payload: dict[str, object]) -> TimelineBonusTextSubmission:
        """Parse a strict free-text timeline bonus submission."""
        expected_keys = {"answer_type", "action", "bonus_type", "value"}
        if payload.keys() != expected_keys:
            raise MusicQuizInvalidAnswerError(
                "Timeline text bonuses require answer_type, action, bonus_type and value"
            )
        bonus_type = self._parse_bonus_type(payload["bonus_type"])
        value = payload["value"]
        if not isinstance(value, str):
            raise MusicQuizInvalidAnswerError("Timeline bonus text must be a string")
        value = value.strip()
        if not value:
            raise MusicQuizInvalidAnswerError("Timeline bonus text cannot be empty")
        if len(value) > MAX_BONUS_TEXT_LENGTH:
            raise MusicQuizInvalidAnswerError(
                f"Timeline bonus text cannot exceed {MAX_BONUS_TEXT_LENGTH} characters"
            )
        return TimelineBonusTextSubmission(bonus_type, value)

    def _parse_bonus_choice(self, payload: dict[str, object]) -> TimelineBonusChoiceSubmission:
        """Parse a strict multiple-choice timeline bonus submission."""
        expected_keys = {"answer_type", "action", "bonus_type", "option_id"}
        if payload.keys() != expected_keys:
            raise MusicQuizInvalidAnswerError(
                "Timeline choice bonuses require answer_type, action, bonus_type and option_id"
            )
        bonus_type = self._parse_bonus_type(payload["bonus_type"])
        option_id = self._parse_client_id(payload["option_id"], "Option ID", nullable=False)
        assert option_id is not None
        return TimelineBonusChoiceSubmission(bonus_type, option_id)

    def _submit_placement(
        self,
        game: MusicQuizGame,
        state: TimelineRoundState,
        player_id: str,
        submission: TimelinePlacementSubmission,
        submitted_at: float,
    ) -> None:
        """Validate and lock a player's first timeline placement."""
        if player_id in state.placements:
            raise MusicQuizAlreadyAnsweredError("Player already placed this round")
        self._validate_boundary(
            state.placement_snapshot,
            submission.previous_entry_id,
            submission.next_entry_id,
        )
        state.placements[player_id] = TimelinePlacementAnswer(
            previous_entry_id=submission.previous_entry_id,
            next_entry_id=submission.next_entry_id,
            answered_at=submitted_at,
        )
        if all(
            self._configured_bonus_mode(game, bonus_type) == TimelineBonusMode.OFF
            for bonus_type in TimelineBonusType
        ):
            state.finished_at[player_id] = submitted_at

    def _submit_bonus_text(
        self,
        state: TimelineRoundState,
        player_id: str,
        submission: TimelineBonusTextSubmission,
        submitted_at: float,
    ) -> None:
        """Validate and store a free-text timeline bonus answer."""
        self._require_placement(state, player_id)
        definition = self._definitions_by_type(state.bonus_definitions).get(submission.bonus_type)
        if not isinstance(definition, TimelineFreeTextBonusDefinition):
            raise MusicQuizInvalidAnswerError(
                f"{submission.bonus_type.value} bonus does not accept free text"
            )
        self._ensure_bonus_not_answered(state, player_id, submission.bonus_type)
        state.bonus_answers.setdefault(player_id, []).append(
            TimelineTextBonusAnswer(
                bonus_type=submission.bonus_type,
                submitted_at=submitted_at,
                value=submission.value,
            )
        )

    def _submit_bonus_choice(
        self,
        state: TimelineRoundState,
        player_id: str,
        submission: TimelineBonusChoiceSubmission,
        submitted_at: float,
    ) -> None:
        """Validate and store a multiple-choice timeline bonus answer."""
        self._require_placement(state, player_id)
        definition = self._definitions_by_type(state.bonus_definitions).get(submission.bonus_type)
        if not isinstance(definition, TimelineMultipleChoiceBonusDefinition):
            raise MusicQuizInvalidAnswerError(
                f"{submission.bonus_type.value} bonus does not accept an option"
            )
        self._ensure_bonus_not_answered(state, player_id, submission.bonus_type)
        if not any(option.option_id == submission.option_id for option in definition.options):
            raise MusicQuizUnknownSuggestionError("Unknown timeline bonus option")
        state.bonus_answers.setdefault(player_id, []).append(
            TimelineChoiceBonusAnswer(
                bonus_type=submission.bonus_type,
                submitted_at=submitted_at,
                option_id=submission.option_id,
            )
        )

    def _validate_bonus_definitions(
        self,
        game: MusicQuizGame,
        definitions: list[TimelineBonusDefinition],
    ) -> None:
        """Validate bonus definitions against the persisted game config."""
        definitions_by_type = self._definitions_by_type(definitions)
        for bonus_type in TimelineBonusType:
            configured_mode = self._configured_bonus_mode(game, bonus_type)
            definition = definitions_by_type.get(bonus_type)
            if configured_mode == TimelineBonusMode.OFF:
                if definition is not None:
                    raise InvalidDataError("Disabled timeline bonuses cannot have a definition")
                continue
            if definition is None or definition.mode != configured_mode:
                raise InvalidDataError("Timeline bonus definition does not match the game config")
            if isinstance(definition, TimelineFreeTextBonusDefinition):
                if not definition.correct_value.strip():
                    raise InvalidDataError("Free-text timeline bonus requires a correct value")
                continue
            if not isinstance(definition, TimelineMultipleChoiceBonusDefinition):
                raise InvalidDataError("Unknown timeline bonus definition type")
            if len(definition.options) != 4:
                raise InvalidDataError("Multiple-choice timeline bonuses require four options")
            option_ids = [option.option_id for option in definition.options]
            if any(not option_id for option_id in option_ids) or len(option_ids) != len(
                set(option_ids)
            ):
                raise InvalidDataError("Timeline bonus option IDs must be non-empty and unique")
            option_labels = [normalize_answer_label(option.label) for option in definition.options]
            if any(not label for label in option_labels) or len(option_labels) != len(
                set(option_labels)
            ):
                raise InvalidDataError("Timeline bonus option labels must be non-empty and unique")
            if sum(option.is_correct for option in definition.options) != 1:
                raise InvalidDataError(
                    "Multiple-choice timeline bonus requires exactly one correct option"
                )

    def _validate_boundary(
        self,
        timeline: list[TimelineEntry],
        previous_entry_id: str | None,
        next_entry_id: str | None,
    ) -> None:
        """Validate neighboring entry IDs against an immutable timeline snapshot."""
        entry_ids = [entry.entry_id for entry in timeline]
        if previous_entry_id is None and next_entry_id is None:
            raise MusicQuizInvalidAnswerError("Timeline placement requires a neighboring entry")
        if previous_entry_id is not None and previous_entry_id not in entry_ids:
            raise MusicQuizInvalidAnswerError("Previous timeline entry is unknown or stale")
        if next_entry_id is not None and next_entry_id not in entry_ids:
            raise MusicQuizInvalidAnswerError("Next timeline entry is unknown or stale")
        if previous_entry_id is None:
            if next_entry_id != entry_ids[0]:
                raise MusicQuizInvalidAnswerError("Timeline placement boundary is not adjacent")
            return
        if next_entry_id is None:
            if previous_entry_id != entry_ids[-1]:
                raise MusicQuizInvalidAnswerError("Timeline placement boundary is not adjacent")
            return
        if entry_ids.index(next_entry_id) != entry_ids.index(previous_entry_id) + 1:
            raise MusicQuizInvalidAnswerError("Timeline placement boundary is not adjacent")

    def _is_correct_placement(
        self,
        timeline: list[TimelineEntry],
        release_year: int,
        placement: TimelinePlacementAnswer,
    ) -> bool:
        """Return whether a placement preserves chronological year ordering."""
        self._validate_boundary(
            timeline,
            placement.previous_entry_id,
            placement.next_entry_id,
        )
        previous_year = next(
            (
                entry.release_year
                for entry in timeline
                if entry.entry_id == placement.previous_entry_id
            ),
            None,
        )
        next_year = next(
            (entry.release_year for entry in timeline if entry.entry_id == placement.next_entry_id),
            None,
        )
        return (previous_year is None or previous_year <= release_year) and (
            next_year is None or release_year <= next_year
        )

    def _is_bonus_correct(
        self,
        definition: TimelineBonusDefinition,
        answer: TimelineBonusAnswer,
    ) -> bool:
        """Return whether a persisted timeline bonus answer is correct."""
        if isinstance(definition, TimelineFreeTextBonusDefinition) and isinstance(
            answer, TimelineTextBonusAnswer
        ):
            return compare_free_text_answer(answer.value, definition.correct_value)
        if isinstance(definition, TimelineMultipleChoiceBonusDefinition) and isinstance(
            answer, TimelineChoiceBonusAnswer
        ):
            return any(
                option.option_id == answer.option_id and option.is_correct
                for option in definition.options
            )
        raise InvalidDataError("Timeline bonus answer does not match its definition")

    def _definitions_by_type(
        self, definitions: list[TimelineBonusDefinition]
    ) -> dict[TimelineBonusType, TimelineBonusDefinition]:
        """Return bonus definitions keyed by their unique bonus type."""
        result: dict[TimelineBonusType, TimelineBonusDefinition] = {}
        for definition in definitions:
            if definition.bonus_type in result:
                raise InvalidDataError("Timeline bonus types must be unique")
            result[definition.bonus_type] = definition
        return result

    def _bonus_answers_by_type(
        self,
        state: TimelineRoundState,
        player_id: str,
    ) -> dict[TimelineBonusType, TimelineBonusAnswer]:
        """Return one player's unique bonus answers keyed by bonus type."""
        result: dict[TimelineBonusType, TimelineBonusAnswer] = {}
        for answer in state.bonus_answers.get(player_id, []):
            if answer.bonus_type in result:
                raise InvalidDataError("Player has duplicate timeline bonus answers")
            result[answer.bonus_type] = answer
        return result

    def _ensure_bonus_not_answered(
        self,
        state: TimelineRoundState,
        player_id: str,
        bonus_type: TimelineBonusType,
    ) -> None:
        """Reject a second answer for the same timeline bonus."""
        if bonus_type in self._bonus_answers_by_type(state, player_id):
            raise MusicQuizInvalidAnswerError(
                f"Player already answered the {bonus_type.value} bonus"
            )

    @staticmethod
    def _require_placement(state: TimelineRoundState, player_id: str) -> None:
        """Require a locked placement before accepting a bonus."""
        if player_id not in state.placements:
            raise MusicQuizInvalidAnswerError("Place the song before answering bonuses")

    @staticmethod
    def _configured_bonus_mode(
        game: MusicQuizGame,
        bonus_type: TimelineBonusType,
    ) -> TimelineBonusMode:
        """Return the configured mode for a timeline bonus."""
        raw_mode = (
            game.config.artist_bonus_mode
            if bonus_type == TimelineBonusType.ARTIST
            else game.config.title_bonus_mode
        )
        try:
            return TimelineBonusMode(raw_mode)
        except ValueError as err:
            raise InvalidDataError("Unknown timeline bonus mode in game config") from err

    @staticmethod
    def _parse_bonus_type(value: object) -> TimelineBonusType:
        """Return a validated timeline bonus type."""
        if not isinstance(value, str):
            raise MusicQuizInvalidAnswerError("Timeline bonus type must be a string")
        try:
            return TimelineBonusType(value)
        except ValueError as err:
            raise MusicQuizInvalidAnswerError(f"Unknown timeline bonus type: {value}") from err

    @staticmethod
    def _parse_client_id(
        value: object,
        label: str,
        *,
        nullable: bool,
    ) -> str | None:
        """Return a validated opaque client-provided ID."""
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not value or len(value) > MAX_CLIENT_ID_LENGTH:
            raise MusicQuizInvalidAnswerError(
                f"{label} must be a non-empty string" + (" or null" if nullable else "")
            )
        return value

    @staticmethod
    def _sorted_timeline(entries: Collection[TimelineEntry]) -> list[TimelineEntry]:
        """Return entries in deterministic chronological order."""
        return sorted(entries, key=lambda entry: (entry.release_year, entry.entry_id))

    def _timeline_with_current(self, state: TimelineRoundState) -> list[TimelineEntry]:
        """Return the revealed timeline with the current entry inserted."""
        return self._sorted_timeline([*state.placement_snapshot, state.current_entry])

    @staticmethod
    def _serialize_entry(entry: TimelineEntry) -> dict[str, SerializableType]:
        """Serialize a revealed timeline entry."""
        return {
            "entry_id": entry.entry_id,
            "release_year": entry.release_year,
            "title": entry.title,
            "artist": entry.artist,
            "track_uri": entry.track_uri,
            "image_url": entry.image_url,
            "is_anchor": entry.is_anchor,
        }

    @staticmethod
    def _serialize_placement(
        placement: TimelinePlacementAnswer,
    ) -> dict[str, SerializableType]:
        """Serialize a locked timeline placement."""
        return {
            "previous_entry_id": placement.previous_entry_id,
            "next_entry_id": placement.next_entry_id,
            "answered_at": placement.answered_at,
        }

    def _serialize_host_bonus_definition(
        self,
        definition: TimelineBonusDefinition,
    ) -> dict[str, SerializableType]:
        """Serialize a protected timeline bonus definition for the host."""
        serialized = self._serialize_public_bonus_definition(definition)
        if isinstance(definition, TimelineFreeTextBonusDefinition):
            serialized["correct_value"] = definition.correct_value
        elif isinstance(definition, TimelineMultipleChoiceBonusDefinition):
            serialized["options"] = [
                {
                    "option_id": option.option_id,
                    "label": option.label,
                    "is_correct": option.is_correct,
                }
                for option in definition.options
            ]
        return serialized

    @staticmethod
    def _serialize_public_bonus_definition(
        definition: TimelineBonusDefinition,
    ) -> dict[str, SerializableType]:
        """Serialize a timeline bonus definition without its correct answer."""
        serialized: dict[str, SerializableType] = {
            "bonus_type": definition.bonus_type.value,
            "mode": definition.mode.value,
        }
        if isinstance(definition, TimelineMultipleChoiceBonusDefinition):
            serialized["options"] = [
                {"option_id": option.option_id, "label": option.label}
                for option in definition.options
            ]
        return serialized

    @staticmethod
    def _serialize_host_bonus_answer(
        answer: TimelineBonusAnswer,
    ) -> dict[str, SerializableType]:
        """Serialize a persisted timeline bonus answer for the host."""
        serialized: dict[str, SerializableType] = {
            "action": answer.action,
            "bonus_type": answer.bonus_type.value,
            "submitted_at": answer.submitted_at,
        }
        if isinstance(answer, TimelineTextBonusAnswer):
            serialized["value"] = answer.value
        elif isinstance(answer, TimelineChoiceBonusAnswer):
            serialized["option_id"] = answer.option_id
        return serialized

    @staticmethod
    def _serialize_personal_bonus_answer(
        answer: TimelineBonusAnswer,
    ) -> dict[str, SerializableType]:
        """Serialize a player's own timeline bonus answer."""
        serialized: dict[str, SerializableType] = {
            "action": answer.action,
            "bonus_type": answer.bonus_type.value,
        }
        if isinstance(answer, TimelineTextBonusAnswer):
            serialized["value"] = answer.value
        elif isinstance(answer, TimelineChoiceBonusAnswer):
            serialized["option_id"] = answer.option_id
        return serialized

    @staticmethod
    def _serialize_result(result: TimelineAnswerResult) -> dict[str, SerializableType]:
        """Serialize a revealed timeline answer result."""
        return {
            "placement": {
                "previous_entry_id": result.placement.previous_entry_id,
                "next_entry_id": result.placement.next_entry_id,
                "is_correct": result.placement.is_correct,
                "points": result.placement.points,
            },
            "bonuses": [
                {
                    "bonus_type": bonus.bonus_type.value,
                    "is_correct": bonus.is_correct,
                    "points": bonus.points,
                }
                for bonus in result.bonuses
            ],
        }

    def _get_state(self, state: QuizRoundAnswerState) -> TimelineRoundState:
        """Return validated timeline round state."""
        if state.answer_type != self.answer_type or not isinstance(state, TimelineRoundState):
            raise InvalidDataError("Round state does not match the timeline answer type")
        return state
