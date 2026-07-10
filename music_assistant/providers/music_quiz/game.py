"""Game state helpers for the Music Quiz provider."""

from __future__ import annotations

from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.errors import (
    MusicQuizAlreadyAnsweredError,
    MusicQuizNameTakenError,
    MusicQuizNotActiveThisRoundError,
    MusicQuizUnknownPlayerError,
    MusicQuizUnknownSuggestionError,
    MusicQuizWrongPhaseError,
)
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswer,
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizPlayer,
    MusicQuizRound,
)
from music_assistant.providers.music_quiz.scoring import calculate_linear_scores


def add_player(
    game: MusicQuizGame,
    player: MusicQuizPlayer,
) -> None:
    """
    Add a player to a game.

    :param game: Game to mutate.
    :param player: Player to add.
    """
    if player.player_id in game.players:
        raise InvalidDataError("Player already exists")
    normalized_name = player.name.casefold()
    if any(existing.name.casefold() == normalized_name for existing in game.players.values()):
        raise MusicQuizNameTakenError("Player name must be unique")
    game.players[player.player_id] = player


def submit_answer(
    game: MusicQuizGame,
    player_id: str,
    suggestion_id: str,
    answered_at: float,
) -> MusicQuizAnswer:
    """
    Submit and lock a player answer for the current round.

    :param game: Game to mutate.
    :param player_id: Player submitting the answer.
    :param suggestion_id: Selected suggestion ID.
    :param answered_at: Answer timestamp.
    """
    if game.phase != MusicQuizPhase.ANSWERING:
        raise MusicQuizWrongPhaseError("Answers can only be submitted during the answering phase")
    current_round = get_current_round(game)
    if player_id in current_round.answers:
        raise MusicQuizAlreadyAnsweredError("Player already answered this round")
    if player_id not in game.players:
        raise MusicQuizUnknownPlayerError("Unknown player")
    if game.players[player_id].active_from_round > current_round.round_index:
        raise MusicQuizNotActiveThisRoundError("Player is not active for this round")
    suggestion = next(
        (item for item in current_round.suggestions if item.suggestion_id == suggestion_id),
        None,
    )
    if suggestion is None:
        raise MusicQuizUnknownSuggestionError("Unknown suggestion")
    answer = MusicQuizAnswer(
        player_id=player_id,
        suggestion_id=suggestion_id,
        answered_at=answered_at,
        is_correct=suggestion.is_correct,
    )
    current_round.answers[player_id] = answer
    return answer


def reveal_round(game: MusicQuizGame) -> None:
    """
    Reveal the current round and apply scores.

    :param game: Game to mutate.
    """
    current_round = get_current_round(game)
    if game.phase != MusicQuizPhase.ANSWERING:
        raise MusicQuizWrongPhaseError("Round can only be revealed during the answering phase")
    correct_answer_order = [
        answer.player_id
        for answer in sorted(current_round.answers.values(), key=lambda item: item.answered_at)
        if answer.is_correct
    ]
    scores = calculate_linear_scores(correct_answer_order)
    for answer in current_round.answers.values():
        answer.points = scores.get(answer.player_id, 0)
        game.players[answer.player_id].score += answer.points
    current_round.ended_at = max(
        [answer.answered_at for answer in current_round.answers.values()],
        default=current_round.started_at or 0,
    )
    game.phase = MusicQuizPhase.REVEAL
    for player in game.players.values():
        player.ready = False


def start_round(
    game: MusicQuizGame,
    music_quiz_round: MusicQuizRound,
    started_at: float,
) -> None:
    """
    Start a new answering round.

    :param game: Game to mutate.
    :param music_quiz_round: Round to append and make current.
    :param started_at: Round start timestamp.
    """
    if game.phase not in (MusicQuizPhase.LOBBY, MusicQuizPhase.REVEAL):
        raise InvalidDataError("A round cannot be started from the current phase")
    if len(game.rounds) >= game.config.round_count:
        raise InvalidDataError("All configured rounds have already been played")
    expected_index = len(game.rounds)
    if music_quiz_round.round_index != expected_index:
        raise InvalidDataError("Round index does not match the game state")
    if sum(1 for suggestion in music_quiz_round.suggestions if suggestion.is_correct) != 1:
        raise InvalidDataError("Round requires exactly one correct suggestion")
    if len(music_quiz_round.suggestions) != game.config.suggestion_count:
        raise InvalidDataError("Round suggestion count does not match the game config")
    music_quiz_round.started_at = started_at
    music_quiz_round.ended_at = None
    game.rounds.append(music_quiz_round)
    game.current_round_index = music_quiz_round.round_index
    game.phase = MusicQuizPhase.ANSWERING
    for player in game.players.values():
        player.ready = False


def mark_player_ready(game: MusicQuizGame, player_id: str) -> None:
    """
    Mark a player ready during the reveal/listening phase.

    :param game: Game to mutate.
    :param player_id: Player ID.
    """
    if game.phase != MusicQuizPhase.REVEAL:
        raise MusicQuizWrongPhaseError("Players can only become ready during reveal")
    if player_id not in game.players:
        raise MusicQuizUnknownPlayerError("Unknown player")
    game.players[player_id].ready = True


def are_active_players_ready(game: MusicQuizGame) -> bool:
    """
    Return whether every player of the upcoming round is ready.

    :param game: Game to inspect.
    """
    current_round = get_current_round(game)
    # +1: during reveal the next round belongs to late joiners too, so the
    # game must not advance from under them — capped at the last real round,
    # because a final-reveal joiner will never play and must not block finish
    gate_round_index = min(current_round.round_index + 1, game.config.round_count - 1)
    players = active_players_for_round(game, gate_round_index)
    return bool(players) and all(player.ready for player in players)


def all_active_players_answered(game: MusicQuizGame) -> bool:
    """
    Return whether every player of the current round has locked an answer.

    :param game: Game to inspect.
    """
    if game.phase != MusicQuizPhase.ANSWERING:
        return False
    current_round = get_current_round(game)
    players = active_players_for_round(game, current_round.round_index)
    return bool(players) and all(player.player_id in current_round.answers for player in players)


def finish_game(game: MusicQuizGame) -> None:
    """
    Mark a game finished.

    :param game: Game to mutate.
    """
    if game.phase != MusicQuizPhase.REVEAL:
        raise InvalidDataError("A game can only be finished from the reveal phase")
    game.phase = MusicQuizPhase.FINISHED


def reset_game(game: MusicQuizGame) -> None:
    """
    Reset a game for a new run with the same config and players.

    :param game: Game to mutate.
    """
    game.phase = MusicQuizPhase.LOBBY
    game.rounds.clear()
    game.current_round_index = None
    for player in game.players.values():
        player.score = 0
        player.ready = False
        player.active_from_round = 0


def active_players_for_round(game: MusicQuizGame, round_index: int) -> list[MusicQuizPlayer]:
    """
    Return players active for a round.

    :param game: Game to inspect.
    :param round_index: Round index.
    """
    return [player for player in game.players.values() if player.active_from_round <= round_index]


def get_current_round(game: MusicQuizGame) -> MusicQuizRound:
    """
    Return the current round.

    :param game: Game to inspect.
    """
    if game.current_round_index is None:
        raise InvalidDataError("No current round")
    try:
        return game.rounds[game.current_round_index]
    except IndexError as err:
        raise InvalidDataError("Current round does not exist") from err
