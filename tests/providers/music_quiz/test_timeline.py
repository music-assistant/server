"""Tests for the Music Quiz timeline answer strategy."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.answer_types.timeline import (
    MAX_BONUS_TEXT_LENGTH,
    TimelineAnswerType,
    TimelineBonusChoiceSubmission,
    TimelineBonusTextSubmission,
    TimelineFinishSubmission,
    TimelinePlacementSubmission,
)
from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizInvalidAnswerError,
    MusicQuizUnknownSuggestionError,
)
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizPlayer,
    MusicQuizRound,
    QuizRoundAnswerState,
    TimelineBonusDefinition,
    TimelineBonusMode,
    TimelineBonusOption,
    TimelineBonusType,
    TimelineCandidate,
    TimelineEntry,
    TimelineFreeTextBonusDefinition,
    TimelineMultipleChoiceBonusDefinition,
    TimelineRoundState,
)

ANSWER_TYPE = TimelineAnswerType()


def _entry(entry_id: str, year: int, *, anchor: bool = False) -> TimelineEntry:
    """Return a deterministic timeline entry."""
    return TimelineEntry(
        entry_id=entry_id,
        release_year=year,
        title=f"Title {entry_id}",
        artist=f"Artist {entry_id}",
        track_uri=f"library://track/{entry_id}",
        image_url=f"https://img/{entry_id}",
        is_anchor=anchor,
    )


def _state(
    current_year: int = 2005,
    *,
    bonus_definitions: list[TimelineBonusDefinition] | None = None,
    artist_answers: list[str] | None = None,
    title_answers: list[str] | None = None,
) -> TimelineRoundState:
    """Return timeline state with an equal-year run."""
    current = _entry("current-secret", current_year)
    current.title = "Current Secret Title"
    current.artist = "Current Secret Artist"
    return TimelineRoundState(
        placement_snapshot=[
            _entry("anchor", 1990, anchor=True),
            _entry("same-a", 2000),
            _entry("same-b", 2000),
            _entry("newer", 2010),
        ],
        candidate=TimelineCandidate(
            entry=current,
            artist_answers=artist_answers or [current.artist],
            title_answers=title_answers or [current.title],
        ),
        bonus_definitions=list(bonus_definitions or []),
    )


def _game(
    state: TimelineRoundState,
    player_ids: Iterable[str] = ("p1",),
    *,
    artist_mode: TimelineBonusMode = TimelineBonusMode.OFF,
    title_mode: TimelineBonusMode = TimelineBonusMode.OFF,
) -> MusicQuizGame:
    """Return an active timeline game."""
    players = {
        player_id: MusicQuizPlayer(
            player_id=player_id,
            name=f"Player {player_id}",
            joined_at=float(index),
            active_from_round=0,
        )
        for index, player_id in enumerate(player_ids)
    }
    return MusicQuizGame(
        config=MusicQuizConfig(
            round_count=1,
            source_uris=["library://playlist/1"],
            artist_bonus_mode=artist_mode,
            title_bonus_mode=title_mode,
        ),
        quiz_type="music_timeline",
        answer_type=MusicQuizAnswerType.TIMELINE,
        phase=MusicQuizPhase.ANSWERING,
        players=players,
        rounds=[
            MusicQuizRound(
                round_index=0,
                answer_label="Current Secret Artist - Current Secret Title",
                answer_state=state,
                track_uri=state.candidate.entry.track_uri,
            )
        ],
        current_round_index=0,
    )


def _choice_definition(
    bonus_type: TimelineBonusType = TimelineBonusType.TITLE,
) -> TimelineMultipleChoiceBonusDefinition:
    """Return a four-option timeline bonus definition."""
    return TimelineMultipleChoiceBonusDefinition(
        bonus_type=bonus_type,
        options=[
            TimelineBonusOption("correct-option", "Correct", True),
            TimelineBonusOption("wrong-a", "Wrong A"),
            TimelineBonusOption("wrong-b", "Wrong B"),
            TimelineBonusOption("wrong-c", "Wrong C"),
        ],
    )


def _correct_placement() -> TimelinePlacementSubmission:
    """Return the correct boundary for the default current entry."""
    return TimelinePlacementSubmission("same-b", "newer")


def test_parse_submission_returns_strict_typed_actions() -> None:
    """Parse every timeline wire variant into its concrete typed action."""
    assert ANSWER_TYPE.parse_submission(
        {
            "answer_type": "timeline",
            "action": "place",
            "previous_entry_id": None,
            "next_entry_id": "anchor",
        }
    ) == TimelinePlacementSubmission(None, "anchor")
    assert ANSWER_TYPE.parse_submission(
        {
            "answer_type": "timeline",
            "action": "bonus_text",
            "bonus_type": "artist",
            "value": "  Beyoncé  ",
        }
    ) == TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Beyoncé")
    assert ANSWER_TYPE.parse_submission(
        {
            "answer_type": "timeline",
            "action": "bonus_choice",
            "bonus_type": "title",
            "option_id": "opaque",
        }
    ) == TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "opaque")
    assert (
        ANSWER_TYPE.parse_submission({"answer_type": "timeline", "action": "finish"})
        == TimelineFinishSubmission()
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "finish"},
        {"answer_type": "multiple_choice", "action": "finish"},
        {"answer_type": "timeline"},
        {"answer_type": "timeline", "action": "unknown"},
        {"answer_type": "timeline", "action": "finish", "extra": True},
        {
            "answer_type": "timeline",
            "action": "place",
            "previous_entry_id": None,
        },
        {
            "answer_type": "timeline",
            "action": "place",
            "previous_entry_id": 1,
            "next_entry_id": "anchor",
        },
        {
            "answer_type": "timeline",
            "action": "bonus_text",
            "bonus_type": "artist",
            "value": " ",
        },
        {
            "answer_type": "timeline",
            "action": "bonus_text",
            "bonus_type": "artist",
            "value": "x" * (MAX_BONUS_TEXT_LENGTH + 1),
        },
        {
            "answer_type": "timeline",
            "action": "bonus_text",
            "bonus_type": "year",
            "value": "2000",
        },
        {
            "answer_type": "timeline",
            "action": "bonus_choice",
            "bonus_type": "title",
            "option_id": "",
        },
        {
            "answer_type": "timeline",
            "action": "bonus_choice",
            "bonus_type": "title",
            "option_id": "opaque",
            "extra": True,
        },
    ],
)
def test_parse_submission_rejects_malformed_variants(payload: dict[str, object]) -> None:
    """Reject wrong discriminators, key sets, types and bounded text violations."""
    with pytest.raises(MusicQuizInvalidAnswerError):
        ANSWER_TYPE.parse_submission(payload)


def test_validate_round_rejects_invalid_timeline_and_bonus_state() -> None:
    """Reject unsorted snapshots, duplicate IDs and mismatched bonus definitions."""
    state = _state()
    game = _game(state)
    ANSWER_TYPE.validate_round(game, state)

    state.placement_snapshot.reverse()
    with pytest.raises(InvalidDataError, match="ordered"):
        ANSWER_TYPE.validate_round(game, state)

    state = _state()
    state.candidate.entry.entry_id = "anchor"
    with pytest.raises(InvalidDataError, match="unique"):
        ANSWER_TYPE.validate_round(_game(state), state)

    state = _state(
        bonus_definitions=[
            TimelineFreeTextBonusDefinition(
                bonus_type=TimelineBonusType.ARTIST,
            )
        ]
    )
    with pytest.raises(InvalidDataError, match="definition"):
        ANSWER_TYPE.validate_round(_game(state), state)

    choice_definition = _choice_definition()
    choice_definition.options[1].label = "Correct!"
    state = _state(bonus_definitions=[choice_definition])
    with pytest.raises(InvalidDataError, match="labels"):
        ANSWER_TYPE.validate_round(
            _game(state, title_mode=TimelineBonusMode.MULTIPLE_CHOICE),
            state,
        )

    state = _state(
        bonus_definitions=[
            _choice_definition(),
            TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST),
        ]
    )
    with pytest.raises(InvalidDataError, match="configured order"):
        ANSWER_TYPE.validate_round(
            _game(
                state,
                artist_mode=TimelineBonusMode.FREE_TEXT,
                title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
            ),
            state,
        )


def test_all_adjacent_boundaries_are_valid_but_stale_boundaries_do_not_lock() -> None:
    """Accept every adjacent snapshot boundary and reject stale or non-adjacent IDs."""
    state = _state()
    valid_boundaries = [
        (None, "anchor"),
        ("anchor", "same-a"),
        ("same-a", "same-b"),
        ("same-b", "newer"),
        ("newer", None),
    ]
    game = _game(state, [f"p{index}" for index in range(len(valid_boundaries) + 5)])
    for index, (previous_id, next_id) in enumerate(valid_boundaries):
        ANSWER_TYPE.submit(
            game,
            state,
            game.players[f"p{index}"],
            TimelinePlacementSubmission(previous_id, next_id),
            float(index),
        )

    for player_id, submission in (
        ("p5", TimelinePlacementSubmission("missing", "anchor")),
        ("p6", TimelinePlacementSubmission("anchor", "newer")),
        ("p7", TimelinePlacementSubmission(None, None)),
        ("p8", TimelinePlacementSubmission(None, "same-a")),
        ("p9", TimelinePlacementSubmission("same-b", None)),
    ):
        with pytest.raises(MusicQuizInvalidAnswerError):
            ANSWER_TYPE.submit(game, state, game.players[player_id], submission, 10)
        assert player_id not in state.placements


@pytest.mark.parametrize(
    ("previous_id", "next_id"),
    [
        ("anchor", "same-a"),
        ("same-a", "same-b"),
        ("same-b", "newer"),
    ],
)
def test_equal_year_accepts_every_boundary_around_the_equal_year_run(
    previous_id: str,
    next_id: str,
) -> None:
    """Treat every adjacent insertion point around equal-year entries as correct."""
    state = _state(current_year=2000)
    game = _game(state)
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p1"],
        TimelinePlacementSubmission(previous_id, next_id),
        1,
    )

    ANSWER_TYPE.reveal(game, state)

    assert state.results["p1"].placement.is_correct is True
    assert state.results["p1"].placement.points == 1000


@pytest.mark.parametrize(
    ("current_year", "previous_id", "next_id"),
    [
        (1980, None, "anchor"),
        (2020, "newer", None),
    ],
)
def test_chronological_boundary_ends_score_correctly(
    current_year: int,
    previous_id: str | None,
    next_id: str | None,
) -> None:
    """Score songs older or newer than the entire snapshot at its boundary ends."""
    state = _state(current_year=current_year)
    game = _game(state)
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p1"],
        TimelinePlacementSubmission(previous_id, next_id),
        1,
    )

    ANSWER_TYPE.reveal(game, state)

    assert state.results["p1"].placement.is_correct is True


def test_reveal_always_inserts_entry_in_deterministic_order() -> None:
    """Insert the revealed song by year and entry ID even when its placement is wrong."""
    state = _state(current_year=2000)
    state.candidate.entry.entry_id = "same-aa"
    game = _game(state)
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p1"],
        TimelinePlacementSubmission("newer", None),
        1,
    )

    ANSWER_TYPE.reveal(game, state)
    revealed = cast("dict[str, Any]", ANSWER_TYPE.serialize_round(state, revealed=True))
    revealed_again = cast("dict[str, Any]", ANSWER_TYPE.serialize_round(state, revealed=True))

    assert state.results["p1"].placement.is_correct is False
    assert state.results["p1"].placement.points == 0
    assert [entry["entry_id"] for entry in revealed["timeline"]] == [
        "anchor",
        "same-a",
        "same-aa",
        "same-b",
        "newer",
    ]
    assert revealed["revealed_entry"]["entry_id"] == "same-aa"
    assert revealed_again["timeline"] == revealed["timeline"]
    assert sum(entry["entry_id"] == "same-aa" for entry in revealed["timeline"]) == 1
    assert "same-aa" not in {entry.entry_id for entry in state.placement_snapshot}


@pytest.mark.parametrize(
    "placement",
    [_correct_placement(), TimelinePlacementSubmission(None, "anchor")],
    ids=["correct", "incorrect"],
)
def test_placement_locks_and_completes_immediately_without_bonuses(
    placement: TimelinePlacementSubmission,
) -> None:
    """Lock any valid placement and complete immediately when bonuses are off."""
    state = _state()
    game = _game(state)
    player = game.players["p1"]

    ANSWER_TYPE.submit(game, state, player, placement, 12)

    assert state.placements["p1"].answered_at == 12
    assert ANSWER_TYPE.is_player_complete(state, "p1") is True
    with pytest.raises(MusicQuizInvalidAnswerError, match="after finishing"):
        ANSWER_TYPE.submit(game, state, player, _correct_placement(), 13)


def test_incorrect_placement_accepts_serializes_and_scores_bonuses() -> None:
    """Offer and independently score every configured bonus after a wrong placement."""
    definitions = [
        TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST),
        _choice_definition(),
    ]
    state = _state(bonus_definitions=definitions)
    game = _game(
        state,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    player = game.players["p1"]
    wrong_placement = TimelinePlacementSubmission(None, "anchor")
    ANSWER_TYPE.submit(game, state, player, wrong_placement, 1)

    personal = cast(
        "dict[str, Any]",
        ANSWER_TYPE.serialize_personal_player(state, "p1", revealed=False),
    )
    assert ANSWER_TYPE.is_player_complete(state, "p1") is False
    assert personal["answer"] == {
        "previous_entry_id": None,
        "next_entry_id": "anchor",
        "answered_at": 1,
        "bonuses": [],
        "finished": False,
    }

    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Wrong Artist"),
        2,
    )
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "correct-option"),
        3,
    )

    assert ANSWER_TYPE.serialize_public_player(state, "p1", revealed=False) == {
        "answered": True,
        "placed": True,
        "artist_bonus_answered": True,
        "title_bonus_answered": True,
    }
    ended_at = ANSWER_TYPE.reveal(game, state)

    assert ended_at == 3
    result = state.results["p1"]
    assert result.placement.is_correct is False
    assert result.placement.points == 0
    assert [(bonus.bonus_type, bonus.is_correct, bonus.points) for bonus in result.bonuses] == [
        (TimelineBonusType.ARTIST, False, 0),
        (TimelineBonusType.TITLE, True, 250),
    ]
    assert player.score == 250
    public = cast(
        "dict[str, Any]",
        ANSWER_TYPE.serialize_public_player(state, "p1", revealed=True),
    )
    assert public["last_answer"] == {
        "placement": {
            "previous_entry_id": None,
            "next_entry_id": "anchor",
            "correct": False,
            "points": 0,
        },
        "artist": {"correct": False, "points": 0},
        "title": {"correct": True, "points": 250},
    }
    personal = cast(
        "dict[str, Any]",
        ANSWER_TYPE.serialize_personal_player(state, "p1", revealed=True),
    )
    assert personal["answer"]["correct"] is False
    assert personal["answer"]["points"] == 0
    assert personal["answer"]["bonus_results"] == [
        {"bonus_type": "artist", "correct": False, "points": 0},
        {"bonus_type": "title", "correct": True, "points": 250},
    ]


def test_final_configured_bonus_completes_at_submission_timestamp() -> None:
    """Complete only when the final configured bonus is submitted."""
    definitions = [
        TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST),
        _choice_definition(),
    ]
    state = _state(bonus_definitions=definitions)
    game = _game(
        state,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    player = game.players["p1"]

    ANSWER_TYPE.submit(game, state, player, _correct_placement(), 1)
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Artist"),
        2,
    )
    assert ANSWER_TYPE.is_player_complete(state, "p1") is False

    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "correct-option"),
        3,
    )

    assert state.finished_at["p1"] == 3


def test_remove_player_clears_all_timeline_answer_state() -> None:
    """Remove placement, bonus, finish and result state for an expired player."""
    state = _state()
    game = _game(state)
    ANSWER_TYPE.submit(game, state, game.players["p1"], _correct_placement(), 1)
    state.bonus_answers["p1"] = []
    ANSWER_TYPE.reveal(game, state)

    ANSWER_TYPE.remove_player(state, "p1")
    ANSWER_TYPE.remove_player(state, "missing")

    assert state.placements == {}
    assert state.bonus_answers == {}
    assert state.finished_at == {}
    assert state.results == {}


def test_removed_player_neither_blocks_completion_nor_scores() -> None:
    """Exclude an expired player's locked placement, bonus and finish from reveal."""
    state = _state(
        bonus_definitions=[TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST)],
        artist_answers=["Massive Attack"],
    )
    game = _game(
        state,
        ("departed", "active"),
        artist_mode=TimelineBonusMode.FREE_TEXT,
    )
    for index, player in enumerate(game.players.values(), start=1):
        ANSWER_TYPE.submit(game, state, player, _correct_placement(), index)
        ANSWER_TYPE.submit(
            game,
            state,
            player,
            TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Massive Attack"),
            index + 2,
        )
    departed = game.players.pop("departed")

    ANSWER_TYPE.remove_player(state, departed.player_id)

    assert ANSWER_TYPE.is_round_complete(state, game.players.values()) is True
    ANSWER_TYPE.reveal(game, state)
    assert departed.score == 0
    assert game.players["active"].score == 1250
    assert "departed" not in state.placements
    assert "departed" not in state.bonus_answers
    assert "departed" not in state.finished_at
    assert "departed" not in state.results


def test_bonus_and_finish_rules_reject_invalid_action_order_and_modes() -> None:
    """Enforce placement-first, mode matching, uniqueness, finish and post-finish locks."""
    definitions = [
        TimelineFreeTextBonusDefinition(
            bonus_type=TimelineBonusType.ARTIST,
        ),
        _choice_definition(),
    ]
    state = _state(bonus_definitions=definitions, artist_answers=["Beyoncé"])
    game = _game(
        state,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    player = game.players["p1"]

    with pytest.raises(MusicQuizInvalidAnswerError, match="Place"):
        ANSWER_TYPE.submit(
            game,
            state,
            player,
            TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Beyoncé"),
            1,
        )
    with pytest.raises(MusicQuizInvalidAnswerError, match="Place"):
        ANSWER_TYPE.submit(game, state, player, TimelineFinishSubmission(), 1)

    ANSWER_TYPE.submit(game, state, player, _correct_placement(), 2)
    assert ANSWER_TYPE.is_player_complete(state, "p1") is False
    with pytest.raises(MusicQuizAlreadyAnsweredError):
        ANSWER_TYPE.submit(game, state, player, _correct_placement(), 3)
    with pytest.raises(MusicQuizInvalidAnswerError, match="does not accept"):
        ANSWER_TYPE.submit(
            game,
            state,
            player,
            TimelineBonusChoiceSubmission(TimelineBonusType.ARTIST, "correct-option"),
            3,
        )
    with pytest.raises(MusicQuizInvalidAnswerError, match="artist bonus first"):
        ANSWER_TYPE.submit(
            game,
            state,
            player,
            TimelineBonusTextSubmission(TimelineBonusType.TITLE, "Correct"),
            3,
        )
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Beyoncé"),
        4,
    )
    with pytest.raises(MusicQuizUnknownSuggestionError):
        ANSWER_TYPE.submit(
            game,
            state,
            player,
            TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "unknown"),
            5,
        )
    with pytest.raises(MusicQuizInvalidAnswerError, match="already answered"):
        ANSWER_TYPE.submit(
            game,
            state,
            player,
            TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Beyoncé"),
            5,
        )
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "correct-option"),
        6,
    )
    assert ANSWER_TYPE.is_player_complete(state, "p1") is True
    assert state.finished_at["p1"] == 6
    for submission in (
        TimelineFinishSubmission(),
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Beyoncé"),
        _correct_placement(),
    ):
        with pytest.raises(MusicQuizInvalidAnswerError, match="after finishing"):
            ANSWER_TYPE.submit(game, state, player, submission, 8)


def test_explicit_finish_skips_remaining_bonuses() -> None:
    """Keep finish as an explicit way to skip configured bonuses."""
    definitions = [
        TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST),
        _choice_definition(),
    ]
    state = _state(bonus_definitions=definitions)
    game = _game(
        state,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    player = game.players["p1"]

    ANSWER_TYPE.submit(game, state, player, _correct_placement(), 1)
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Artist"),
        2,
    )
    ANSWER_TYPE.submit(game, state, player, TimelineFinishSubmission(), 3)

    assert state.finished_at["p1"] == 3
    assert len(state.bonus_answers["p1"]) == 1


def test_artist_choice_and_title_text_modes_score_independently() -> None:
    """Accept either action mode independently for artist and title bonuses."""
    cases = [
        (
            _choice_definition(TimelineBonusType.ARTIST),
            TimelineBonusChoiceSubmission(TimelineBonusType.ARTIST, "correct-option"),
            TimelineBonusMode.MULTIPLE_CHOICE,
            TimelineBonusMode.OFF,
        ),
        (
            TimelineFreeTextBonusDefinition(
                bonus_type=TimelineBonusType.TITLE,
            ),
            TimelineBonusTextSubmission(TimelineBonusType.TITLE, "Around the World"),
            TimelineBonusMode.OFF,
            TimelineBonusMode.FREE_TEXT,
        ),
    ]
    for definition, submission, artist_mode, title_mode in cases:
        state = _state(
            bonus_definitions=[definition],
            title_answers=(
                ["Around the World (Radio Edit)"]
                if definition.bonus_type == TimelineBonusType.TITLE
                else None
            ),
        )
        game = _game(
            state,
            artist_mode=artist_mode,
            title_mode=title_mode,
        )
        player = game.players["p1"]
        ANSWER_TYPE.submit(game, state, player, _correct_placement(), 1)
        assert ANSWER_TYPE.is_player_complete(state, "p1") is False
        ANSWER_TYPE.submit(game, state, player, submission, 2)
        assert state.finished_at["p1"] == 2

        ANSWER_TYPE.reveal(game, state)

        assert state.results["p1"].bonuses[0].points == 250


def test_reveal_scores_speed_ranked_placements_and_fixed_bonuses() -> None:
    """Rank only correct placements by speed and score bonuses independently."""
    definitions = [
        TimelineFreeTextBonusDefinition(
            bonus_type=TimelineBonusType.ARTIST,
        ),
        _choice_definition(),
    ]
    state = _state(
        bonus_definitions=definitions,
        artist_answers=["Beyoncé feat. Jay-Z"],
    )
    game = _game(
        state,
        ("p1", "p2", "p3"),
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    for player_id, placement, answered_at in (
        ("p1", _correct_placement(), 3),
        ("p2", TimelinePlacementSubmission(None, "anchor"), 1),
        ("p3", _correct_placement(), 2),
    ):
        ANSWER_TYPE.submit(game, state, game.players[player_id], placement, answered_at)
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p1"],
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "beyonce"),
        4,
    )
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p1"],
        TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "correct-option"),
        5,
    )
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p2"],
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "wrong"),
        2,
    )
    ANSWER_TYPE.submit(
        game,
        state,
        game.players["p2"],
        TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "correct-option"),
        3,
    )
    ANSWER_TYPE.submit(game, state, game.players["p3"], TimelineFinishSubmission(), 6)

    ended_at = ANSWER_TYPE.reveal(game, state)

    assert ended_at == 6
    assert state.results["p3"].placement.points == 1000
    assert state.results["p1"].placement.points == 500
    assert state.results["p2"].placement.points == 0
    assert [result.points for result in state.results["p1"].bonuses] == [250, 250]
    assert [result.points for result in state.results["p2"].bonuses] == [0, 250]
    assert game.players["p1"].score == 1000
    assert game.players["p2"].score == 250
    assert game.players["p3"].score == 1000


def test_deadline_reveal_scores_unfinished_submissions() -> None:
    """Score a placement and bonus at reveal even when finish was not submitted."""
    state = _state(
        bonus_definitions=[
            TimelineFreeTextBonusDefinition(
                bonus_type=TimelineBonusType.ARTIST,
            ),
            _choice_definition(),
        ],
        artist_answers=["Massive Attack"],
    )
    game = _game(
        state,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    player = game.players["p1"]
    ANSWER_TYPE.submit(game, state, player, _correct_placement(), 1)
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "Massive Attack"),
        2,
    )

    assert ANSWER_TYPE.is_player_complete(state, "p1") is False
    ANSWER_TYPE.reveal(game, state)

    assert state.results["p1"].placement.points == 1000
    assert state.results["p1"].bonuses[0].points == 250
    assert game.players["p1"].score == 1250
    with pytest.raises(MusicQuizInvalidAnswerError, match="revealing"):
        ANSWER_TYPE.submit(game, state, player, TimelineFinishSubmission(), 3)


@pytest.mark.parametrize(
    ("submitted_artist", "expected_points"),
    [
        ("Daft Punk feat. Pharrell Williams", 250),
        ("Daft Punk", 250),
        ("Pharrell Williams", 250),
        ("Punk, Daft", 250),
        ("Random Artist", 0),
    ],
)
def test_artist_free_text_accepts_persisted_contributors_and_aliases(
    submitted_artist: str,
    expected_points: int,
) -> None:
    """Accept only artist truths justified by the persisted candidate metadata."""
    state = _state(
        bonus_definitions=[TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST)],
        artist_answers=[
            "Daft Punk feat. Pharrell Williams",
            "Daft Punk",
            "Pharrell Williams",
            "Punk, Daft",
        ],
    )
    state.candidate.entry.artist = "Daft Punk feat. Pharrell Williams"
    game = _game(state, artist_mode=TimelineBonusMode.FREE_TEXT)
    player = game.players["p1"]
    ANSWER_TYPE.submit(game, state, player, _correct_placement(), 1)
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, submitted_artist),
        2,
    )

    ANSWER_TYPE.reveal(game, state)

    assert state.results["p1"].bonuses[0].points == expected_points
    assert state.candidate.entry.artist == "Daft Punk feat. Pharrell Williams"


def test_reveal_rejects_progress_without_a_locked_placement() -> None:
    """Reject corrupted finish or bonus progress that has no placement."""
    state = _state()
    state.finished_at["p1"] = 1

    with pytest.raises(InvalidDataError, match="locked placement"):
        ANSWER_TYPE.reveal(_game(state), state)


def test_serialization_redacts_answer_material_and_restores_personal_progress() -> None:
    """Keep current content and correctness secret while restoring only the player's answer."""
    definitions = [
        TimelineFreeTextBonusDefinition(
            bonus_type=TimelineBonusType.ARTIST,
        ),
        _choice_definition(),
    ]
    state = _state(
        bonus_definitions=definitions,
        artist_answers=["Current Secret Artist", "Hidden Artist Alias"],
    )
    game = _game(
        state,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    player = game.players["p1"]
    ANSWER_TYPE.submit(game, state, player, _correct_placement(), 10)
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusTextSubmission(TimelineBonusType.ARTIST, "my artist answer"),
        11,
    )
    ANSWER_TYPE.submit(
        game,
        state,
        player,
        TimelineBonusChoiceSubmission(TimelineBonusType.TITLE, "wrong-a"),
        12,
    )

    public_round = cast("dict[str, Any]", ANSWER_TYPE.serialize_round(state, revealed=False))
    serialized_public = str(public_round)
    assert set(public_round) == {"timeline", "bonus_definitions"}
    assert "Current Secret" not in serialized_public
    assert "current-secret" not in serialized_public
    assert "Hidden Artist Alias" not in serialized_public
    assert "candidate" not in serialized_public
    assert "is_correct" not in serialized_public
    assert public_round["bonus_definitions"][0] == {
        "bonus_type": "artist",
        "mode": "free_text",
    }
    for option in public_round["bonus_definitions"][1]["options"]:
        assert set(option) == {"option_id", "label"}

    assert ANSWER_TYPE.serialize_public_player(state, "p1", revealed=False) == {
        "answered": True,
        "placed": True,
        "artist_bonus_answered": True,
        "title_bonus_answered": True,
    }
    assert ANSWER_TYPE.serialize_personal_player(state, "p1", revealed=False) == {
        "answer": {
            "previous_entry_id": "same-b",
            "next_entry_id": "newer",
            "answered_at": 10,
            "bonuses": [
                {
                    "action": "bonus_text",
                    "bonus_type": "artist",
                    "value": "my artist answer",
                },
                {
                    "action": "bonus_choice",
                    "bonus_type": "title",
                    "option_id": "wrong-a",
                },
            ],
            "finished": True,
        }
    }
    assert "p1" not in str(ANSWER_TYPE.serialize_public_player(state, "p1", revealed=False))

    ANSWER_TYPE.reveal(game, state)
    revealed_round = cast("dict[str, Any]", ANSWER_TYPE.serialize_round(state, revealed=True))
    public_player = cast(
        "dict[str, Any]",
        ANSWER_TYPE.serialize_public_player(state, "p1", revealed=True),
    )
    personal_player = cast(
        "dict[str, Any]",
        ANSWER_TYPE.serialize_personal_player(state, "p1", revealed=True),
    )
    assert revealed_round["revealed_entry"]["release_year"] == 2005
    assert public_player["last_answer"]["placement"] == {
        "previous_entry_id": "same-b",
        "next_entry_id": "newer",
        "correct": True,
        "points": 1000,
    }
    assert personal_player["answer"]["correct"] is True
    assert personal_player["answer"]["points"] == 1000
    assert personal_player["answer"]["bonus_results"] == [
        {"bonus_type": "artist", "correct": False, "points": 0},
        {"bonus_type": "title", "correct": False, "points": 0},
    ]


def test_host_serialization_is_flat_and_complete() -> None:
    """Expose full typed timeline answer data in the host round fragment."""
    state = _state()
    game = _game(state)
    ANSWER_TYPE.submit(game, state, game.players["p1"], _correct_placement(), 1)
    ANSWER_TYPE.reveal(game, state)

    host = cast("dict[str, Any]", ANSWER_TYPE.serialize_host_round(state))

    assert set(host) == {
        "placement_snapshot",
        "candidate",
        "bonus_definitions",
        "placements",
        "bonus_answers",
        "finished_at",
        "results",
        "revealed",
    }
    assert host["candidate"]["entry"]["entry_id"] == "current-secret"
    assert host["placements"]["p1"]["answered_at"] == 1
    assert host["results"]["p1"]["placement"]["is_correct"] is True


def test_strategy_rejects_matching_discriminator_on_wrong_state_class() -> None:
    """Reject state whose discriminator matches but concrete model does not."""
    state = QuizRoundAnswerState(answer_type=MusicQuizAnswerType.TIMELINE)

    with pytest.raises(InvalidDataError, match="does not match"):
        ANSWER_TYPE.serialize_round(state, revealed=False)
