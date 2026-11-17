"""
Hint-text answer strategy for Music Quiz: free-text guesses against a revealing mask.

Players guess the title as often as they like until it is right. The first correct guess
earns a bonus, and rounds with an artist bonus then offer a single artist guess.
"""

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
    MusicQuizGuessTooFastError,
    MusicQuizInvalidAnswerError,
    MusicQuizTooManyAttemptsError,
)
from music_assistant.providers.music_quiz.hints import build_mask, hidden_positions
from music_assistant.providers.music_quiz.matching import (
    compare_free_text_answer,
    compare_hint_guess,
)
from music_assistant.providers.music_quiz.models import (
    HintTextAttempt,
    HintTextRoundState,
    HintTextSolve,
    MusicQuizAnswerType,
    MusicQuizGame,
    MusicQuizPlayer,
    QuizRoundAnswerState,
)
from music_assistant.providers.music_quiz.scoring import calculate_time_score

MAX_GUESS_LENGTH = 200
MAX_ATTEMPTS_PER_ROUND = 30
MIN_GUESS_INTERVAL_SECONDS = 0.5
FIRST_SOLVER_BONUS = 200
# matches the Music Timeline artist bonus
ARTIST_BONUS_POINTS = 250

ACTION_GUESS_TITLE = "guess_title"
ACTION_GUESS_ARTIST = "guess_artist"
ACTION_SKIP_ARTIST = "skip_artist"


@dataclass(frozen=True, slots=True)
class HintTextSubmission(QuizAnswerSubmission):
    """A validated free-text title guess."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.HINT_TEXT
    text: str


@dataclass(frozen=True, slots=True)
class HintTextArtistSubmission(QuizAnswerSubmission):
    """A validated artist bonus guess, or a skipped bonus when text is None."""

    answer_type: ClassVar[MusicQuizAnswerType] = MusicQuizAnswerType.HINT_TEXT
    text: str | None


class HintTextAnswerType(QuizAnswerType):
    """Free-text answer strategy with progressively revealed letter hints."""

    answer_type = MusicQuizAnswerType.HINT_TEXT

    def parse_submission(self, payload: Mapping[str, object]) -> QuizAnswerSubmission:
        """
        Parse a title guess, an artist guess or a skipped artist bonus.

        :param payload: Raw JSON object submitted by a player.
        :return: A validated hint-text submission.
        """
        answer_type = payload.get("answer_type")
        if not isinstance(answer_type, str) or answer_type != self.answer_type:
            raise MusicQuizInvalidAnswerError("Answer type must be hint_text")
        action = payload.get("action")
        if action == ACTION_SKIP_ARTIST:
            if payload.keys() != {"answer_type", "action"}:
                raise MusicQuizInvalidAnswerError("Skipping the artist bonus takes no text")
            return HintTextArtistSubmission(text=None)
        if action not in (ACTION_GUESS_TITLE, ACTION_GUESS_ARTIST):
            raise MusicQuizInvalidAnswerError("Unknown hint-text action")
        if payload.keys() != {"answer_type", "action", "text"}:
            raise MusicQuizInvalidAnswerError(
                "Hint-text guesses require answer_type, action and text"
            )
        text = payload["text"]
        if not isinstance(text, str):
            raise MusicQuizInvalidAnswerError("Guess must be a string")
        text = " ".join(text.split())
        if not text or len(text) > MAX_GUESS_LENGTH:
            raise MusicQuizInvalidAnswerError(
                f"Guess must contain between 1 and {MAX_GUESS_LENGTH} characters"
            )
        if action == ACTION_GUESS_ARTIST:
            return HintTextArtistSubmission(text=text)
        return HintTextSubmission(text=text)

    def validate_round(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> None:
        """
        Validate a hint-text round.

        :param game: Game the round belongs to.
        :param state: Prepared answer state to validate.
        """
        hint_state = self._get_state(state)
        hidden = set(hidden_positions(hint_state.solution))
        if not hidden or not hint_state.accepted_answers:
            raise InvalidDataError("Round requires a solution with letters to guess")
        if set(hint_state.reveal_order) != hidden or len(hint_state.reveal_order) != len(hidden):
            raise InvalidDataError("Round reveal order does not match the solution")
        if len(hint_state.hint_fractions) >= len(hidden):
            raise InvalidDataError("Round hints must never reveal the whole solution")

    def open_round(
        self, state: QuizRoundAnswerState, audio_started_at: float, deadline: float
    ) -> list[float]:
        """
        Spread the round's hints over the time its track is audible.

        :param state: Answer state of the round that started.
        :param audio_started_at: Expected timestamp at which the round's track became audible.
        :param deadline: Timestamp at which the round stops accepting answers.
        """
        hint_state = self._get_state(state)
        hint_state.audio_started_at = audio_started_at
        hint_state.deadline = deadline
        hint_state.revealed_count = 0
        audible_window = max(deadline - audio_started_at, 0.0)
        hint_state.hint_times = [
            audio_started_at + audible_window * fraction for fraction in hint_state.hint_fractions
        ]
        return list(hint_state.hint_times)

    def reveal_hint(self, state: QuizRoundAnswerState, hint_index: int) -> bool:
        """
        Uncover the letters of all hints up to and including the given one.

        :param state: Current round answer state.
        :param hint_index: Index of the hint in the list returned by open_round.
        """
        hint_state = self._get_state(state)
        revealed_count = min(hint_index + 1, len(hint_state.hint_fractions))
        if revealed_count <= hint_state.revealed_count:
            return False
        hint_state.revealed_count = revealed_count
        return True

    def submit(
        self,
        game: MusicQuizGame,
        state: QuizRoundAnswerState,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        submitted_at: float,
    ) -> None:
        """
        Check a player's title or artist guess.

        :param game: Game receiving the submission.
        :param state: Current round answer state.
        :param player: Player submitting the answer.
        :param submission: Validated hint-text submission.
        :param submitted_at: Server timestamp of the submission.
        """
        hint_state = self._get_state(state)
        if isinstance(submission, HintTextArtistSubmission):
            self._submit_artist(hint_state, player, submission)
            return
        if not isinstance(submission, HintTextSubmission):
            raise MusicQuizInvalidAnswerError("Submission does not match the hint-text answer type")
        if player.player_id in hint_state.solves:
            raise MusicQuizAlreadyAnsweredError("Player already guessed this round")
        attempts = hint_state.attempts.setdefault(player.player_id, [])
        if len(attempts) >= MAX_ATTEMPTS_PER_ROUND:
            raise MusicQuizTooManyAttemptsError("Player used all guesses for this round")
        if attempts and submitted_at - attempts[-1].submitted_at < MIN_GUESS_INTERVAL_SECONDS:
            raise MusicQuizGuessTooFastError("Player guessed again too quickly")
        is_correct, is_close = compare_hint_guess(
            submission.text,
            hint_state.accepted_answers,
            hint_state.allowed_typos,
        )
        attempts.append(
            HintTextAttempt(
                text=submission.text,
                submitted_at=submitted_at,
                is_correct=is_correct,
                is_close=is_close,
            )
        )
        if is_correct:
            hint_state.solves[player.player_id] = HintTextSolve(
                solved_at=submitted_at,
                points=self._score(hint_state, submitted_at),
                first_bonus=0 if hint_state.solves else FIRST_SOLVER_BONUS,
                # without an artist bonus there is nothing left to answer
                artist_done=not hint_state.artist_answers,
            )

    def remove_player(self, state: QuizRoundAnswerState, player_id: str) -> None:
        """
        Remove a player's guesses.

        :param state: Round answer state to mutate.
        :param player_id: Player whose guesses should be removed.
        """
        hint_state = self._get_state(state)
        hint_state.attempts.pop(player_id, None)
        hint_state.solves.pop(player_id, None)

    def is_player_complete(self, state: QuizRoundAnswerState, player_id: str) -> bool:
        """
        Return whether a player guessed the title and answered or skipped the artist bonus.

        :param state: Round answer state to inspect.
        :param player_id: Player to inspect.
        """
        solve = self._get_state(state).solves.get(player_id)
        return solve is not None and solve.artist_done

    def is_round_complete(
        self,
        state: QuizRoundAnswerState,
        active_players: Collection[MusicQuizPlayer],
    ) -> bool:
        """
        Return whether every active player guessed the title and finished the artist bonus.

        :param state: Round answer state to inspect.
        :param active_players: Players eligible for the round.
        """
        return bool(active_players) and all(
            self.is_player_complete(state, player.player_id) for player in active_players
        )

    def reveal(self, game: MusicQuizGame, state: QuizRoundAnswerState) -> float | None:
        """
        Apply the points earned by correct guesses.

        :param game: Game being revealed.
        :param state: Round answer state being revealed.
        :return: Timestamp of the latest submitted guess, if any.
        """
        hint_state = self._get_state(state)
        for player_id, solve in hint_state.solves.items():
            if player := game.players.get(player_id):
                player.score += _total_points(solve)
        return max(
            (
                attempt.submitted_at
                for attempts in hint_state.attempts.values()
                for attempt in attempts
            ),
            default=None,
        )

    def serialize_game_config(self, game: MusicQuizGame) -> dict[str, SerializableType]:
        """
        Serialize hint-text game configuration.

        :param game: Game whose configuration should be serialized.
        """
        return {}

    def serialize_host_round(self, state: QuizRoundAnswerState) -> dict[str, SerializableType]:
        """
        Serialize hint-text state for the host's flat round payload.

        :param state: Round answer state to serialize.
        """
        hint_state = self._get_state(state)
        return {
            "solution": hint_state.solution,
            "allowed_typos": hint_state.allowed_typos,
            "mask": self._mask(hint_state, revealed=False),
            "revealed_count": hint_state.revealed_count,
            "hint_count": len(hint_state.hint_fractions),
            "hint_times": list(hint_state.hint_times),
            "attempts": {
                player_id: [
                    {
                        "text": attempt.text,
                        "submitted_at": attempt.submitted_at,
                        "correct": attempt.is_correct,
                        "close": attempt.is_close,
                    }
                    for attempt in attempts
                ]
                for player_id, attempts in hint_state.attempts.items()
            },
            "artist_answers": list(hint_state.artist_answers),
            "solves": {
                player_id: {
                    "solved_at": solve.solved_at,
                    "points": solve.points,
                    "first_bonus": solve.first_bonus,
                    "artist_done": solve.artist_done,
                    "artist_guess": solve.artist_guess,
                    "artist_correct": solve.artist_correct,
                    "artist_points": solve.artist_points,
                    "total_points": _total_points(solve),
                }
                for player_id, solve in hint_state.solves.items()
            },
        }

    def serialize_round(
        self, state: QuizRoundAnswerState, *, revealed: bool
    ) -> dict[str, SerializableType]:
        """
        Serialize hint-text round state.

        :param state: Round answer state to serialize.
        :param revealed: Whether protected answer data may be exposed.
        """
        hint_state = self._get_state(state)
        serialized_state: dict[str, SerializableType] = {
            "mask": self._mask(hint_state, revealed=revealed),
            "revealed_count": hint_state.revealed_count,
            "hint_count": len(hint_state.hint_fractions),
            "allowed_typos": hint_state.allowed_typos,
            "artist_bonus": bool(hint_state.artist_answers),
        }
        if revealed:
            serialized_state["solution"] = hint_state.solution
        return serialized_state

    def serialize_public_player(
        self,
        state: QuizRoundAnswerState | None,
        player_id: str,
        *,
        revealed: bool,
    ) -> dict[str, SerializableType]:
        """
        Serialize a player's public guessing progress.

        :param state: Current round answer state, if one exists.
        :param player_id: Player represented by the public entry.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {"answered": False, "solved_rank": None}
        hint_state = self._get_state(state)
        solve = hint_state.solves.get(player_id)
        serialized_state: dict[str, SerializableType] = {
            "answered": solve is not None,
            "solved_rank": self._solved_rank(hint_state, player_id),
        }
        if revealed:
            serialized_state["last_answer"] = {
                "correct": solve is not None,
                "points": _total_points(solve) if solve else 0,
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
        Serialize a player's own guesses.

        :param state: Current round answer state, if one exists.
        :param player_id: Player receiving the state.
        :param revealed: Whether protected answer data may be exposed.
        """
        if state is None:
            return {}
        hint_state = self._get_state(state)
        attempts = hint_state.attempts.get(player_id, [])
        solve = hint_state.solves.get(player_id)
        serialized_state: dict[str, SerializableType] = {
            "guesses": [
                {"text": attempt.text, "correct": attempt.is_correct, "close": attempt.is_close}
                for attempt in attempts
            ],
            "attempts_left": max(MAX_ATTEMPTS_PER_ROUND - len(attempts), 0),
            "solved": solve is not None,
        }
        if solve is not None:
            # a player who guessed correctly already knows the title
            serialized_state["solution"] = hint_state.solution
            serialized_state["points"] = _total_points(solve)
            serialized_state["title_points"] = solve.points
            serialized_state["first_bonus"] = solve.first_bonus
            if hint_state.artist_answers:
                artist_bonus: dict[str, SerializableType] = {"done": solve.artist_done}
                if solve.artist_done:
                    # the artist is only shown once the player can no longer change the guess
                    artist_bonus.update(
                        guess=solve.artist_guess,
                        correct=solve.artist_correct,
                        points=solve.artist_points,
                        artist=hint_state.artist_label,
                    )
                serialized_state["artist_bonus"] = artist_bonus
        return serialized_state

    @staticmethod
    def _submit_artist(
        hint_state: HintTextRoundState,
        player: MusicQuizPlayer,
        submission: HintTextArtistSubmission,
    ) -> None:
        """Apply a player's single artist guess, or a skipped artist bonus."""
        if not hint_state.artist_answers:
            raise MusicQuizInvalidAnswerError("This round has no artist bonus")
        solve = hint_state.solves.get(player.player_id)
        if solve is None:
            raise MusicQuizInvalidAnswerError("Guess the title before the artist")
        if solve.artist_done:
            raise MusicQuizAlreadyAnsweredError("Player already answered the artist bonus")
        solve.artist_done = True
        if submission.text is None:
            return
        solve.artist_guess = submission.text
        solve.artist_correct = any(
            compare_free_text_answer(submission.text, answer)
            for answer in hint_state.artist_answers
        )
        solve.artist_points = ARTIST_BONUS_POINTS if solve.artist_correct else 0

    @staticmethod
    def _mask(hint_state: HintTextRoundState, *, revealed: bool) -> list[SerializableType]:
        """Return the masked solution, or the whole solution once revealed."""
        if revealed:
            return list(hint_state.solution)
        return list(
            build_mask(
                hint_state.solution,
                set(hint_state.reveal_order[: hint_state.revealed_count]),
            )
        )

    @staticmethod
    def _solved_rank(hint_state: HintTextRoundState, player_id: str) -> int | None:
        """Return the 1-based position of a player among the correct guessers."""
        if player_id not in hint_state.solves:
            return None
        ordered = sorted(hint_state.solves, key=lambda item: hint_state.solves[item].solved_at)
        return ordered.index(player_id) + 1

    @staticmethod
    def _score(hint_state: HintTextRoundState, submitted_at: float) -> int:
        """Return the points of a correct guess, measured from when the track became audible."""
        if hint_state.audio_started_at is None or hint_state.deadline is None:
            raise InvalidDataError("Hint-text round was not opened")
        return calculate_time_score(
            submitted_at - hint_state.audio_started_at,
            hint_state.deadline - hint_state.audio_started_at,
        )

    def _get_state(self, state: QuizRoundAnswerState) -> HintTextRoundState:
        """Return validated hint-text round state."""
        if state.answer_type != self.answer_type or not isinstance(state, HintTextRoundState):
            raise InvalidDataError("Round state does not match the hint-text answer type")
        return state


def _total_points(solve: HintTextSolve) -> int:
    """Return the points of a correct title guess including all bonuses."""
    return solve.points + solve.first_bonus + solve.artist_points
