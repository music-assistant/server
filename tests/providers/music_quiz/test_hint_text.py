"""Tests for the Music Quiz hint-text answer strategy."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.answer_types.hint_text import (
    ARTIST_BONUS_POINTS,
    FIRST_SOLVER_BONUS,
    MAX_ATTEMPTS_PER_ROUND,
    MAX_GUESS_LENGTH,
    HintTextAnswerType,
    HintTextArtistSubmission,
    HintTextSubmission,
)
from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizGuessTooFastError,
    MusicQuizInvalidAnswerError,
    MusicQuizTooManyAttemptsError,
)
from music_assistant.providers.music_quiz.models import (
    HintTextRoundState,
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizGame,
    MusicQuizPlayer,
    MusicQuizRound,
)

ANSWER_TYPE = HintTextAnswerType()
STARTED_AT = 100.0
AUDIO_STARTED_AT = STARTED_AT + 1.0
DEADLINE = STARTED_AT + 31.0


def _state(*, artist_bonus: bool = False) -> HintTextRoundState:
    """Return hint-text round state for "Hey Jude", optionally with the artist bonus."""
    return HintTextRoundState(
        solution="Hey Jude",
        accepted_answers=["heyjude"],
        allowed_typos=1,
        reveal_order=[0, 4, 1, 5, 2, 6, 7],
        # 5, 10 and 15 seconds into the 30 audible seconds
        hint_fractions=[1 / 6, 1 / 3, 1 / 2],
        artist_answers=["The Beatles"] if artist_bonus else [],
        artist_label="The Beatles" if artist_bonus else None,
    )


def _game(state: HintTextRoundState) -> MusicQuizGame:
    """Return a started one-round game with two players."""
    game = MusicQuizGame(
        config=MusicQuizConfig(answer_duration=31),
        quiz_type="title_reveal",
        answer_type=MusicQuizAnswerType.HINT_TEXT,
    )
    for name in ("alice", "bob"):
        game.players[name] = MusicQuizPlayer(
            player_id=name, name=name, joined_at=0, active_from_round=0
        )
    game.rounds.append(
        MusicQuizRound(
            round_index=0,
            answer_label="The Beatles - Hey Jude",
            answer_state=state,
            started_at=STARTED_AT,
            duration=180.0,
        )
    )
    game.current_round_index = 0
    ANSWER_TYPE.validate_round(game, state)
    ANSWER_TYPE.open_round(state, AUDIO_STARTED_AT, DEADLINE)
    return game


def _guess(game: MusicQuizGame, player_id: str, text: str, at: float) -> None:
    """Submit a guess for a player."""
    ANSWER_TYPE.submit(
        game,
        game.rounds[0].answer_state,
        game.players[player_id],
        HintTextSubmission(text=text),
        at,
    )


def _guess_artist(game: MusicQuizGame, player_id: str, text: str | None) -> None:
    """Submit an artist guess for a player, or skip the bonus when text is None."""
    ANSWER_TYPE.submit(
        game,
        game.rounds[0].answer_state,
        game.players[player_id],
        HintTextArtistSubmission(text=text),
        STARTED_AT + 20.0,
    )


def test_parse_submission_collapses_whitespace() -> None:
    """Parse the wire payload into a typed guess with normalized spacing."""
    submission = ANSWER_TYPE.parse_submission(
        {"answer_type": "hint_text", "action": "guess_title", "text": "  Hey   Jude "}
    )

    assert submission == HintTextSubmission(text="Hey Jude")


def test_parse_submission_reads_artist_guesses_and_skips() -> None:
    """Parse artist guesses and skipped artist bonuses into their own submission type."""
    guess = ANSWER_TYPE.parse_submission(
        {"answer_type": "hint_text", "action": "guess_artist", "text": "The Beatles"}
    )
    skip = ANSWER_TYPE.parse_submission({"answer_type": "hint_text", "action": "skip_artist"})

    assert guess == HintTextArtistSubmission(text="The Beatles")
    assert skip == HintTextArtistSubmission(text=None)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "guess_title", "text": "Hey Jude"},
        {"answer_type": "hint_text", "text": "Hey Jude"},
        {"answer_type": "hint_text", "action": "guess_title"},
        {"answer_type": "hint_text", "action": "dance", "text": "Hey Jude"},
        {"answer_type": "multiple_choice", "action": "guess_title", "text": "Hey Jude"},
        {"answer_type": "hint_text", "action": "guess_title", "text": 1},
        {"answer_type": "hint_text", "action": "guess_title", "text": "   "},
        {
            "answer_type": "hint_text",
            "action": "guess_title",
            "text": "a" * (MAX_GUESS_LENGTH + 1),
        },
        {"answer_type": "hint_text", "action": "guess_title", "text": "Hey Jude", "extra": 1},
        {"answer_type": "hint_text", "action": "skip_artist", "text": "The Beatles"},
    ],
)
def test_parse_submission_rejects_malformed_payload(payload: dict[str, object]) -> None:
    """Reject missing, mismatched, empty, oversized and extra fields."""
    with pytest.raises(MusicQuizInvalidAnswerError):
        ANSWER_TYPE.parse_submission(payload)


def test_hints_uncover_letters_in_reveal_order() -> None:
    """Anchor hints to the audible start and uncover one letter per hint."""
    state = _state()
    _game(state)

    hint_times = ANSWER_TYPE.open_round(state, AUDIO_STARTED_AT, DEADLINE)
    assert hint_times == pytest.approx([106.0, 111.0, 116.0])

    assert ANSWER_TYPE.reveal_hint(state, 1) is True
    assert ANSWER_TYPE.reveal_hint(state, 0) is False
    hidden = ANSWER_TYPE.serialize_round(state, revealed=False)

    assert hidden["mask"] == ["H", None, None, " ", "J", None, None, None]
    assert hidden["revealed_count"] == 2
    assert "solution" not in hidden


def test_round_serialization_reveals_the_solution_only_after_reveal() -> None:
    """Expose the whole title only once the round is revealed."""
    state = _state()

    revealed = ANSWER_TYPE.serialize_round(state, revealed=True)

    assert revealed["solution"] == "Hey Jude"
    assert revealed["mask"] == list("Hey Jude")


def test_wrong_guesses_stay_private_and_correct_guess_locks_the_player() -> None:
    """Keep guess texts out of public state and lock a player after a correct guess."""
    state = _state()
    game = _game(state)

    _guess(game, "alice", "Hey Joe", 102.0)
    _guess(game, "alice", "hey jud", 103.0)

    public = ANSWER_TYPE.serialize_public_player(state, "alice", revealed=False)
    personal = ANSWER_TYPE.serialize_personal_player(state, "alice", revealed=False)
    assert public == {"answered": True, "solved_rank": 1}
    assert personal["guesses"] == [
        {"text": "Hey Joe", "correct": False, "close": True},
        {"text": "hey jud", "correct": True, "close": False},
    ]
    assert personal["solution"] == "Hey Jude"
    assert personal["attempts_left"] == MAX_ATTEMPTS_PER_ROUND - 2
    assert ANSWER_TYPE.serialize_personal_player(state, "bob", revealed=False) == {
        "guesses": [],
        "attempts_left": MAX_ATTEMPTS_PER_ROUND,
        "solved": False,
    }
    with pytest.raises(MusicQuizAlreadyAnsweredError):
        _guess(game, "alice", "Hey Jude", 104.0)


def test_guesses_are_rate_limited_and_capped() -> None:
    """Reject rapid-fire guesses and guesses beyond the per-round limit."""
    state = _state()
    game = _game(state)

    _guess(game, "bob", "wrong", 101.0)
    with pytest.raises(MusicQuizGuessTooFastError):
        _guess(game, "bob", "wrong", 101.2)
    for index in range(1, MAX_ATTEMPTS_PER_ROUND):
        _guess(game, "bob", "wrong", 101.0 + index)
    with pytest.raises(MusicQuizTooManyAttemptsError):
        _guess(game, "bob", "Hey Jude", 200.0)


def test_earlier_correct_guesses_score_more_points() -> None:
    """Score correct guesses by time and complete the round once everyone solved it."""
    state = _state()
    game = _game(state)
    players = list(game.players.values())

    _guess(game, "alice", "Hey Jude", AUDIO_STARTED_AT)
    assert ANSWER_TYPE.is_round_complete(state, players) is False
    _guess(game, "bob", "Hey Jude", STARTED_AT + 16.0)
    assert ANSWER_TYPE.is_round_complete(state, players) is True

    assert ANSWER_TYPE.reveal(game, state) == STARTED_AT + 16.0
    assert game.players["alice"].score == 1000 + FIRST_SOLVER_BONUS
    assert game.players["bob"].score == 550
    assert ANSWER_TYPE.serialize_public_player(state, "bob", revealed=True) == {
        "answered": True,
        "solved_rank": 2,
        "last_answer": {"correct": True, "points": 550},
    }


def test_artist_bonus_is_one_guess_after_the_title() -> None:
    """Offer one artist guess after the title and keep the round open until it is answered."""
    state = _state(artist_bonus=True)
    game = _game(state)
    players = list(game.players.values())

    with pytest.raises(MusicQuizInvalidAnswerError):
        _guess_artist(game, "alice", "The Beatles")
    _guess(game, "alice", "Hey Jude", AUDIO_STARTED_AT)
    _guess(game, "bob", "Hey Jude", STARTED_AT + 16.0)

    personal = ANSWER_TYPE.serialize_personal_player(state, "alice", revealed=False)
    assert personal["artist_bonus"] == {"done": False}
    assert ANSWER_TYPE.is_round_complete(state, players) is False

    _guess_artist(game, "alice", "the beatls")
    _guess_artist(game, "bob", None)
    with pytest.raises(MusicQuizAlreadyAnsweredError):
        _guess_artist(game, "alice", "The Beatles")

    assert ANSWER_TYPE.is_round_complete(state, players) is True
    personal = ANSWER_TYPE.serialize_personal_player(state, "alice", revealed=False)
    assert personal["artist_bonus"] == {
        "done": True,
        "guess": "the beatls",
        "correct": True,
        "points": ARTIST_BONUS_POINTS,
        "artist": "The Beatles",
    }
    assert personal["points"] == 1000 + FIRST_SOLVER_BONUS + ARTIST_BONUS_POINTS
    ANSWER_TYPE.reveal(game, state)
    assert game.players["alice"].score == 1000 + FIRST_SOLVER_BONUS + ARTIST_BONUS_POINTS
    assert game.players["bob"].score == 550


def test_artist_guess_is_refused_without_artist_bonus() -> None:
    """Refuse artist guesses in rounds without the artist bonus."""
    state = _state()
    game = _game(state)
    _guess(game, "alice", "Hey Jude", 102.0)

    with pytest.raises(MusicQuizInvalidAnswerError):
        _guess_artist(game, "alice", "The Beatles")
    assert "artist_bonus" not in ANSWER_TYPE.serialize_personal_player(
        state, "alice", revealed=False
    )


def test_remove_player_drops_guesses() -> None:
    """Remove a leaving player's guesses and solve."""
    state = _state()
    game = _game(state)
    _guess(game, "alice", "Hey Jude", 102.0)

    ANSWER_TYPE.remove_player(state, "alice")

    assert state.attempts == {}
    assert state.solves == {}


def test_validate_round_rejects_hints_that_reveal_the_whole_title() -> None:
    """Refuse a round whose hints would uncover every letter."""
    state = _state()
    state.hint_fractions = [0.5] * len(state.reveal_order)

    with pytest.raises(InvalidDataError, match="never reveal"):
        _game(state)
