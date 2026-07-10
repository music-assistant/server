"""Tests for Music Quiz game state helpers."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.answer_types.multiple_choice import (
    MultipleChoiceAnswerType,
    MultipleChoiceSubmission,
)
from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizNameTakenError,
    MusicQuizWrongPhaseError,
)
from music_assistant.providers.music_quiz.game import (
    active_players_for_round,
    add_player,
    all_active_players_complete,
    are_active_players_ready,
    finish_game,
    reset_game,
    reveal_round,
    start_round,
    submit_answer,
)
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizPlayer,
    MusicQuizRound,
    MusicQuizSuggestion,
)

ANSWER_TYPE = MultipleChoiceAnswerType()


def _player(player_id: str, name: str, active_from_round: int = 0) -> MusicQuizPlayer:
    """Return a test player."""
    return MusicQuizPlayer(
        player_id=player_id,
        name=name,
        joined_at=1,
        active_from_round=active_from_round,
    )


def _game() -> MusicQuizGame:
    """Return a test game with one active round."""
    return MusicQuizGame(
        config=MusicQuizConfig(),
        quiz_type="guess_the_song",
        answer_type=MusicQuizAnswerType.MULTIPLE_CHOICE,
        phase=MusicQuizPhase.ANSWERING,
        current_round_index=0,
        rounds=[
            MusicQuizRound(
                round_index=0,
                track_uri="library://track/1",
                answer_label="Daft Punk - One More Time",
                suggestions=[
                    MusicQuizSuggestion(
                        suggestion_id="correct",
                        label="Daft Punk - One More Time",
                        is_correct=True,
                    ),
                    MusicQuizSuggestion(
                        suggestion_id="wrong_1",
                        label="Justice - D.A.N.C.E.",
                    ),
                ],
            )
        ],
    )


def test_add_player_rejects_duplicate_name_case_insensitive() -> None:
    """Reject duplicate player names case-insensitively."""
    game = _game()
    add_player(game, _player("p1", "Alice"))

    with pytest.raises(MusicQuizNameTakenError, match="unique"):
        add_player(game, _player("p2", "alice"))


def test_add_player_rejects_duplicate_player_id() -> None:
    """Never silently overwrite an existing player on an ID collision."""
    game = _game()
    add_player(game, _player("p1", "Alice"))

    with pytest.raises(InvalidDataError, match="already exists"):
        add_player(game, _player("p1", "Bob"))

    assert game.players["p1"].name == "Alice"


def test_start_round_requires_exactly_one_correct_suggestion() -> None:
    """A round with zero or multiple correct suggestions is unplayable."""
    game = _game()
    game.phase = MusicQuizPhase.LOBBY
    game.rounds.clear()
    game.current_round_index = None

    def _round(*correct_flags: bool) -> MusicQuizRound:
        return MusicQuizRound(
            round_index=0,
            track_uri="library://track/1",
            answer_label="Daft Punk - One More Time",
            suggestions=[
                MusicQuizSuggestion(
                    suggestion_id=f"s{index}",
                    label=f"Suggestion {index}",
                    is_correct=flag,
                )
                for index, flag in enumerate(correct_flags)
            ],
        )

    with pytest.raises(InvalidDataError, match="exactly one correct"):
        start_round(game, _round(True, True, False, False), 1, ANSWER_TYPE)
    with pytest.raises(InvalidDataError, match="exactly one correct"):
        start_round(game, _round(False, False, False, False), 1, ANSWER_TYPE)

    start_round(game, _round(True, False, False, False), 1, ANSWER_TYPE)
    assert game.phase == MusicQuizPhase.ANSWERING


def test_finish_game_requires_reveal_phase() -> None:
    """A game can only finish from the reveal phase."""
    game = _game()

    with pytest.raises(InvalidDataError, match="only be finished"):
        finish_game(game)

    game.phase = MusicQuizPhase.REVEAL
    finish_game(game)
    assert game.phase == MusicQuizPhase.FINISHED


def test_ready_gate_includes_players_joining_next_round() -> None:
    """During reveal the upcoming round belongs to late joiners too."""
    game = _game()
    add_player(game, _player("p1", "Alice"))
    reveal_round(game, ANSWER_TYPE)
    add_player(game, _player("p2", "Bob", active_from_round=1))

    game.players["p1"].ready = True
    assert are_active_players_ready(game) is False

    game.players["p2"].ready = True
    assert are_active_players_ready(game) is True


def test_ready_gate_on_final_round_ignores_spectators_of_a_round_that_never_comes() -> None:
    """A player joining during the final reveal must not block the game finish."""
    game = _game()
    game.config.round_count = 1
    add_player(game, _player("p1", "Alice"))
    reveal_round(game, ANSWER_TYPE)
    # joins during the final reveal: their "first active round" will never play
    add_player(game, _player("p2", "Bob", active_from_round=1))

    game.players["p1"].ready = True
    assert are_active_players_ready(game) is True


def test_submit_answer_locks_first_answer() -> None:
    """Reject a second answer from the same player."""
    game = _game()
    add_player(game, _player("p1", "Alice"))

    submit_answer(
        game,
        "p1",
        MultipleChoiceSubmission(suggestion_id="wrong_1"),
        10,
        ANSWER_TYPE,
    )
    answer = game.rounds[0].answers["p1"]

    assert answer.suggestion_id == "wrong_1"
    assert answer.is_correct is False
    with pytest.raises(MusicQuizAlreadyAnsweredError, match="already answered"):
        submit_answer(
            game,
            "p1",
            MultipleChoiceSubmission(suggestion_id="correct"),
            11,
            ANSWER_TYPE,
        )


def test_submit_answer_rejects_unknown_suggestion() -> None:
    """Reject answers for suggestions that are not in the current round."""
    game = _game()
    add_player(game, _player("p1", "Alice"))

    with pytest.raises(InvalidDataError, match="Unknown suggestion"):
        submit_answer(
            game,
            "p1",
            MultipleChoiceSubmission(suggestion_id="missing"),
            10,
            ANSWER_TYPE,
        )


def test_submit_answer_rejects_outside_answering_phase() -> None:
    """Only accept answers during the answering phase."""
    game = _game()
    game.phase = MusicQuizPhase.REVEAL
    add_player(game, _player("p1", "Alice"))

    with pytest.raises(MusicQuizWrongPhaseError, match="answering phase"):
        submit_answer(
            game,
            "p1",
            MultipleChoiceSubmission(suggestion_id="correct"),
            10,
            ANSWER_TYPE,
        )


def test_all_active_players_complete_tracks_current_round() -> None:
    """Report completion only when every active player locked an answer."""
    game = _game()
    add_player(game, _player("p1", "Alice"))
    add_player(game, _player("p2", "Bob"))
    add_player(game, _player("p3", "Late", active_from_round=1))

    assert all_active_players_complete(game, ANSWER_TYPE) is False
    submit_answer(
        game,
        "p1",
        MultipleChoiceSubmission(suggestion_id="correct"),
        10,
        ANSWER_TYPE,
    )
    assert all_active_players_complete(game, ANSWER_TYPE) is False
    # the late joiner is not active this round and must not block completion
    submit_answer(
        game,
        "p2",
        MultipleChoiceSubmission(suggestion_id="wrong_1"),
        11,
        ANSWER_TYPE,
    )
    assert all_active_players_complete(game, ANSWER_TYPE) is True


def test_reveal_round_applies_scores_to_correct_answers_in_order() -> None:
    """Apply linear scores when a round is revealed."""
    game = _game()
    add_player(game, _player("p1", "Alice"))
    add_player(game, _player("p2", "Bob"))
    add_player(game, _player("p3", "Charlie"))
    submit_answer(
        game,
        "p2",
        MultipleChoiceSubmission(suggestion_id="correct"),
        10,
        ANSWER_TYPE,
    )
    submit_answer(
        game,
        "p1",
        MultipleChoiceSubmission(suggestion_id="wrong_1"),
        11,
        ANSWER_TYPE,
    )
    submit_answer(
        game,
        "p3",
        MultipleChoiceSubmission(suggestion_id="correct"),
        12,
        ANSWER_TYPE,
    )

    reveal_round(game, ANSWER_TYPE)

    current_round = game.rounds[0]
    assert game.phase == MusicQuizPhase.REVEAL
    assert current_round.answers["p2"].points == 1000
    assert current_round.answers["p3"].points == 500
    assert current_round.answers["p1"].points == 0
    assert game.players["p2"].score == 1000
    assert game.players["p3"].score == 500
    assert game.players["p1"].score == 0


def test_active_players_for_round_excludes_late_joiners_until_next_round() -> None:
    """Only include late joiners from their active round onward."""
    game = _game()
    add_player(game, _player("p1", "Alice", active_from_round=0))
    add_player(game, _player("p2", "Bob", active_from_round=1))

    assert [player.player_id for player in active_players_for_round(game, 0)] == ["p1"]
    assert [player.player_id for player in active_players_for_round(game, 1)] == [
        "p1",
        "p2",
    ]


def test_reset_game_keeps_players_and_config_for_new_game() -> None:
    """Reset transient state while preserving game settings and identity."""
    game = _game()
    add_player(game, _player("p1", "Alice", active_from_round=0))
    add_player(game, _player("p2", "Bob", active_from_round=1))
    game.players["p1"].score = 1000
    game.players["p1"].ready = True
    game.players["p2"].score = 500
    game.players["p2"].ready = True

    reset_game(game)

    assert game.phase == MusicQuizPhase.LOBBY
    assert game.quiz_type == "guess_the_song"
    assert game.answer_type == MusicQuizAnswerType.MULTIPLE_CHOICE
    assert game.current_round_index is None
    assert game.rounds == []
    assert {player.name for player in game.players.values()} == {"Alice", "Bob"}
    assert all(player.score == 0 for player in game.players.values())
    assert all(player.ready is False for player in game.players.values())
    assert all(player.active_from_round == 0 for player in game.players.values())
