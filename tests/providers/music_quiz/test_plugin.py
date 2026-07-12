"""Tests for the Music Quiz plugin provider."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.enums import ConfigEntryType, MediaType, PlaybackState, QueueOption
from music_assistant_models.errors import AudioError, InvalidDataError, MediaNotFoundError

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    impersonated_user,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user as get_auth_current_user,
)
from music_assistant.helpers.api import APICommandHandler, parse_arguments
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz import (
    MUSIC_QUIZ_GUEST_USER,
    PLAYER_RECONNECT_GRACE_SECONDS,
    REPLAY_AUTO_START_SECONDS,
    MusicQuizPlugin,
    get_config_entries,
)
from music_assistant.providers.music_quiz.answer_types import get_answer_type
from music_assistant.providers.music_quiz.answer_types.base import QuizAnswerSubmissionPayload
from music_assistant.providers.music_quiz.errors import (
    MusicQuizGameActiveError,
    MusicQuizInvalidAnswerError,
    MusicQuizNoGameError,
    MusicQuizNoPlaybackTargetError,
    MusicQuizUnknownPlayerError,
)
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceRoundState,
    MultipleChoiceSuggestion,
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizRound,
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
from music_assistant.providers.music_quiz.quiz_types.trivia import TriviaQuizType

INSTANCE_ID = "music_quiz--test"

HOST_COMMANDS = (
    "music_quiz/available_quiz_types",
    "music_quiz/create",
    "music_quiz/get",
    "music_quiz/start",
    "music_quiz/reveal",
    "music_quiz/next",
    "music_quiz/reset",
    "music_quiz/delete",
)
GUEST_COMMANDS = (
    "music_quiz/info",
    "music_quiz/join",
    "music_quiz/state",
    "music_quiz/heartbeat",
    "music_quiz/submit_answer",
    "music_quiz/answer",
    "music_quiz/ready",
)
LISTEN_IN_COMMANDS = (
    "music_quiz/listen_in",
    "music_quiz/stop_listen_in",
    "music_quiz/can_listen_in",
)


def _make_round(round_index: int, suggestion_count: int = 4) -> MusicQuizRound:
    """Return a deterministic prepared round for the given index."""
    suggestions = [
        MultipleChoiceSuggestion(
            suggestion_id=f"correct_{round_index}",
            label=f"Artist - Correct {round_index}",
            is_correct=True,
        )
    ] + [
        MultipleChoiceSuggestion(
            suggestion_id=f"wrong_{round_index}_{index}",
            label=f"Artist - Wrong {round_index}.{index}",
        )
        for index in range(1, suggestion_count)
    ]
    return MusicQuizRound(
        round_index=round_index,
        track_uri=f"library://track/{round_index}",
        answer_label=f"Artist - Correct {round_index}",
        answer_state=MultipleChoiceRoundState(suggestions=suggestions),
        image_url=f"https://img/{round_index}",
        duration=180.0,
    )


def _make_hitster_round(
    round_index: int,
    previous_rounds: list[MusicQuizRound],
    bonus_definitions: list[TimelineBonusDefinition] | None = None,
) -> MusicQuizRound:
    """Return a deterministic Hitster round with a prefetch-safe snapshot."""
    if previous_rounds:
        previous_state = previous_rounds[-1].answer_state
        assert isinstance(previous_state, TimelineRoundState)
        timeline = sorted(
            [*previous_state.placement_snapshot, previous_state.candidate.entry],
            key=lambda entry: (entry.release_year, entry.entry_id),
        )
    else:
        timeline = [
            TimelineEntry(
                entry_id="anchor",
                release_year=1990,
                title="Anchor Song",
                artist="Anchor Artist",
                track_uri="library://track/anchor",
                image_url="https://img/anchor",
                is_anchor=True,
            )
        ]
    current_entry = TimelineEntry(
        entry_id=f"hitster-{round_index}",
        release_year=2000 + round_index,
        title=f"Secret Title {round_index}",
        artist=f"Secret Artist {round_index}",
        track_uri=f"library://track/hitster-{round_index}",
        image_url=f"https://img/hitster-{round_index}",
    )
    return MusicQuizRound(
        round_index=round_index,
        answer_label=f"Secret Artist {round_index} - Secret Title {round_index}",
        answer_state=TimelineRoundState(
            placement_snapshot=timeline,
            candidate=TimelineCandidate(
                entry=current_entry,
                artist_answers=[current_entry.artist],
                title_answers=[current_entry.title],
            ),
            bonus_definitions=list(bonus_definitions or []),
        ),
        track_uri=current_entry.track_uri,
        image_url=current_entry.image_url,
        duration=180.0,
    )


def _make_trivia_round(
    round_index: int,
    _previous_rounds: list[MusicQuizRound],
) -> MusicQuizRound:
    """Return a deterministic non-audio Trivia round."""
    return MusicQuizRound(
        round_index=round_index,
        question=f"Trivia question {round_index}?",
        answer_label=f"Correct answer {round_index}",
        answer_state=MultipleChoiceRoundState(
            suggestions=[
                MultipleChoiceSuggestion(
                    suggestion_id=f"correct_{round_index}",
                    label=f"Correct answer {round_index}",
                    uri=f"library://track/trivia-{round_index}",
                    is_correct=True,
                ),
                *[
                    MultipleChoiceSuggestion(
                        suggestion_id=f"wrong_{round_index}_{index}",
                        label=f"Wrong answer {round_index}.{index}",
                    )
                    for index in range(1, 4)
                ],
            ]
        ),
    )


def _create_plugin(
    mode: str = "venue",
    player: str | None = "venue_player",
    use_ai_distractors: bool = False,
) -> MusicQuizPlugin:
    """Create a minimally configured Music Quiz plugin for unit tests."""
    plugin = MusicQuizPlugin.__new__(MusicQuizPlugin)
    plugin.mass = MagicMock()
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin.config.instance_id = INSTANCE_ID
    plugin.config.get_value.side_effect = {
        "mode": mode,
        "player": player,
        "use_ai_distractors": use_ai_distractors,
    }.__getitem__
    plugin._game = None
    plugin._quiz_type = None
    plugin._answer_type = None
    plugin._game_lock = asyncio.Lock()
    plugin._game_generation = 0
    plugin._playback_session = None
    plugin._playback_lock = asyncio.Lock()
    plugin._next_round_task = None
    plugin._unregister_handles = []

    def _create_task(target: Any, *args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        if asyncio.iscoroutine(target):
            return asyncio.get_running_loop().create_task(target)
        return asyncio.get_running_loop().create_task(target(*args))

    plugin.mass.create_task.side_effect = _create_task
    source_item = MagicMock()
    source_item.name = "Test Playlist"
    source_item.media_type = MediaType.PLAYLIST
    plugin.mass.music.get_item = AsyncMock(return_value=source_item)
    plugin.signal_provider_event = MagicMock()  # type: ignore[method-assign, misc]
    plugin._play_track = AsyncMock()  # type: ignore[method-assign]
    plugin._stop_playback = AsyncMock()  # type: ignore[method-assign]
    plugin._get_join_url = AsyncMock(return_value="http://ma/join")  # type: ignore[method-assign]
    return plugin


def _fake_game() -> MusicQuizGame:
    """Return a minimal active-game stub for playback-session tests."""
    return cast("MusicQuizGame", SimpleNamespace(config=SimpleNamespace(name=None)))


def _guest_user() -> SimpleNamespace:
    """Return a fake authenticated Music Quiz guest user."""
    return SimpleNamespace(username=MUSIC_QUIZ_GUEST_USER, role=UserRole.GUEST)


def _phase(plugin: MusicQuizPlugin) -> MusicQuizPhase:
    """Return the current game phase."""
    game = plugin._game
    assert game is not None
    return game.phase


def _answer_state(game_round: MusicQuizRound) -> MultipleChoiceRoundState:
    """Return multiple-choice state from a test round."""
    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    return game_round.answer_state


def _presence_timer_call(plugin: MusicQuizPlugin) -> tuple[float, Any]:
    """Return the most recently scheduled player presence timer."""
    for timer_call in reversed(cast("MagicMock", plugin.mass.call_later).call_args_list):
        if timer_call.kwargs.get("task_id") == plugin._presence_timer_id:
            return cast("float", timer_call.args[0]), timer_call.args[1]
    raise AssertionError("No player presence timer was scheduled")


def _round_timer_call(plugin: MusicQuizPlugin, task_id: str) -> tuple[float, Any, int]:
    """Return the most recently scheduled timer for a game round."""
    for timer_call in reversed(cast("MagicMock", plugin.mass.call_later).call_args_list):
        if timer_call.kwargs.get("task_id") == task_id:
            return (
                cast("float", timer_call.args[0]),
                timer_call.args[1],
                cast("int", timer_call.args[2]),
            )
    raise AssertionError(f"No round timer was scheduled for {task_id}")


def _replay_timer_call(
    plugin: MusicQuizPlugin,
) -> tuple[float, Any, MusicQuizGame, int, float]:
    """Return the most recently scheduled replay auto-start timer."""
    for timer_call in reversed(cast("MagicMock", plugin.mass.call_later).call_args_list):
        if timer_call.kwargs.get("task_id") == plugin._replay_auto_start_timer_id:
            return (
                cast("float", timer_call.args[0]),
                timer_call.args[1],
                cast("MusicQuizGame", timer_call.args[2]),
                cast("int", timer_call.args[3]),
                cast("float", timer_call.args[4]),
            )
    raise AssertionError("No replay auto-start timer was scheduled")


def _timeline_answer_state(game_round: MusicQuizRound) -> TimelineRoundState:
    """Return timeline state from a test round."""
    assert isinstance(game_round.answer_state, TimelineRoundState)
    return game_round.answer_state


async def _create_started_game(
    plugin: MusicQuizPlugin,
    player_names: tuple[str, ...] = ("Alice", "Bob"),
    round_count: int = 2,
) -> dict[str, str]:
    """Create and start a game with joined players, returning name->player_id."""
    await plugin.create_game(
        round_count=round_count,
        source_uris=["library://playlist/1"],
        name="Test Quiz",
    )
    player_ids: dict[str, str] = {}
    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        for player_name in player_names:
            result = await plugin.join_game(player_name)
            player_ids[player_name] = result["player_id"]
    await plugin.start_game()
    return player_ids


async def _create_started_hitster_game(
    plugin: MusicQuizPlugin,
    player_names: tuple[str, ...] = ("Alice",),
    round_count: int = 1,
    *,
    artist_bonus_mode: str = TimelineBonusMode.OFF.value,
    title_bonus_mode: str = TimelineBonusMode.OFF.value,
) -> dict[str, str]:
    """Create and start a Hitster game, returning name-to-player credentials."""
    await plugin.create_game(
        quiz_type="hitster",
        round_count=round_count,
        source_uris=["library://playlist/1"],
        name="Timeline Quiz",
        artist_bonus_mode=artist_bonus_mode,
        title_bonus_mode=title_bonus_mode,
    )
    player_ids: dict[str, str] = {}
    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        for player_name in player_names:
            result = await plugin.join_game(player_name)
            player_ids[player_name] = result["player_id"]
    await plugin.start_game()
    return player_ids


@pytest.fixture(autouse=True)
def _deterministic_rounds() -> Any:
    """Patch round preparation to deterministic rounds without music lookups."""
    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song."
        "GuessTheSongQuizType.prepare_round",
        new=AsyncMock(side_effect=lambda round_index, _used: _make_round(round_index)),
    ):
        yield


@pytest.mark.asyncio
async def test_api_command_scopes_lock_out_guests_from_host_commands() -> None:
    """Host commands require USERS_INVITE, which guest users do not hold."""
    plugin = _create_plugin()
    registered: dict[str, Scope | None] = {}

    def _register(command: str, _handler: Any, **kwargs: Any) -> Any:
        registered[command] = kwargs.get("required_scope")
        return MagicMock()

    cast("MagicMock", plugin.mass).register_api_command.side_effect = _register
    await plugin.loaded_in_mass()

    for command in HOST_COMMANDS:
        assert registered[command] == Scope.USERS_INVITE
    for command in GUEST_COMMANDS:
        assert registered[command] is None
    for command in LISTEN_IN_COMMANDS:
        assert registered[command] == Scope.PLAYERS_CONTROL


@pytest.mark.parametrize(
    "username",
    [MUSIC_QUIZ_GUEST_USER, "party_guest", "temporary_guest"],
)
@pytest.mark.asyncio
async def test_guest_commands_accept_any_dedicated_guest(username: str) -> None:
    """Any authenticated guest role can use the active Music Quiz experience."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=SimpleNamespace(username=username, role=UserRole.GUEST),
    ):
        result = await plugin.join_game("Guest")

    assert result["player_id"]


@pytest.mark.parametrize(
    "user",
    [
        None,
        SimpleNamespace(username="user", role=UserRole.USER),
        SimpleNamespace(username="admin", role=UserRole.ADMIN),
        SimpleNamespace(username="service", role=UserRole.SERVICE),
    ],
)
@pytest.mark.asyncio
async def test_guest_commands_reject_non_guest_users(user: SimpleNamespace | None) -> None:
    """Guest game commands reject unauthenticated and non-guest users."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=user,
        ),
        pytest.raises(InvalidDataError, match="guests"),
    ):
        await plugin.join_game("Mallory")


@pytest.mark.asyncio
async def test_full_game_flow() -> None:
    """Play a full two-round game from create to finish."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)
    game = plugin._game
    assert game is not None
    assert _phase(plugin) == MusicQuizPhase.ANSWERING
    assert game.current_round_index == 0
    cast("AsyncMock", plugin._play_track).assert_awaited_with("library://track/0")

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        # the round auto-reveals when all active players answered
        await plugin.answer(player_ids["Alice"], "correct_0")
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        await plugin.answer(player_ids["Bob"], "wrong_0_1")
        assert _phase(plugin) == MusicQuizPhase.REVEAL
        assert game.players[player_ids["Alice"]].score == 1000
        assert game.players[player_ids["Bob"]].score == 0

        # all players ready advances to the next round
        await plugin.ready(player_ids["Alice"])
        assert _phase(plugin) == MusicQuizPhase.REVEAL
        await plugin.ready(player_ids["Bob"])
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        assert game.current_round_index == 1

        # final round: reveal via host command, then next finishes the game
        await plugin.answer(player_ids["Alice"], "wrong_1_1")
        await plugin.answer(player_ids["Bob"], "correct_1")

    assert _phase(plugin) == MusicQuizPhase.REVEAL
    await plugin.next_round()
    assert _phase(plugin) == MusicQuizPhase.FINISHED
    assert game.players[player_ids["Bob"]].score == 1000
    cast("AsyncMock", plugin._stop_playback).assert_awaited()


@pytest.mark.asyncio
async def test_guest_ready_advances_and_prefetches_in_system_context() -> None:
    """Keep provider playback and background preparation independent of the guest."""
    prepared_contexts: list[tuple[int, object | None]] = []

    async def _prepare_round(
        round_index: int,
        _previous_rounds: list[MusicQuizRound],
    ) -> MusicQuizRound:
        prepared_contexts.append((round_index, get_auth_current_user()))
        game_round = _make_round(round_index)
        game_round.track_uri = f"spotify://track/{round_index}"
        return game_round

    prepare_round = AsyncMock(side_effect=_prepare_round)
    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song."
        "GuessTheSongQuizType.prepare_round",
        new=prepare_round,
    ):
        plugin = _create_plugin()
        player_ids = await _create_started_game(plugin, round_count=3)
        game = plugin._game
        assert game is not None

        playback_contexts: list[object | None] = []

        async def _play_media(
            _queue_id: str,
            track_uri: str,
            *,
            option: QueueOption,
        ) -> None:
            playback_user = get_auth_current_user()
            playback_contexts.append(playback_user)
            if playback_user is not None:
                raise MediaNotFoundError("No playable items found")
            assert track_uri == "spotify://track/1"
            assert option == QueueOption.REPLACE

        plugin._play_track = MusicQuizPlugin._play_track.__get__(  # type: ignore[method-assign]
            plugin, MusicQuizPlugin
        )
        plugin._get_playback_session = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(queue_id="quiz_queue", player_id="quiz_player")
        )
        cast("MagicMock", plugin.mass.players.get_player).return_value = SimpleNamespace(
            extra_data={}
        )
        play_media = AsyncMock(side_effect=_play_media)
        cast("MagicMock", plugin.mass.player_queues).play_media = play_media
        requesting_user = cast("User", _guest_user())
        requesting_impersonation = cast("User", _guest_user())
        current_user_token = current_user.set(requesting_user)
        impersonated_user_token = impersonated_user.set(requesting_impersonation)
        try:
            await plugin.answer(player_ids["Alice"], "correct_0")
            await plugin.answer(player_ids["Bob"], "wrong_0_1")
            await plugin.ready(player_ids["Alice"])
            await plugin.ready(player_ids["Bob"])
            assert plugin._next_round_task is not None
            await plugin._next_round_task
            assert current_user.get() is requesting_user
            assert impersonated_user.get() is requesting_impersonation
        finally:
            impersonated_user.reset(impersonated_user_token)
            current_user.reset(current_user_token)

    assert playback_contexts == [None]
    assert (2, None) in prepared_contexts
    assert game.current_round_index == 1
    assert len(game.rounds) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "playback_error",
    [MediaNotFoundError("No playable items found"), AudioError("Stream is unavailable")],
)
async def test_unplayable_prefetch_is_replaced_before_round_start(
    playback_error: AudioError | MediaNotFoundError,
) -> None:
    """Reject an unplayable prefetch and start exactly one replacement round."""
    plugin = _create_plugin()
    await plugin.create_game(round_count=1, source_uris=["library://playlist/1"])
    game = plugin._game
    quiz_type = plugin._quiz_type
    assert game is not None
    assert quiz_type is not None
    plugin._cancel_next_round_task()
    rejected_round = _make_round(0)
    rejected_round.track_uri = "spotify://track/unplayable"
    replacement_round = _make_round(0)
    replacement_round.track_uri = "spotify://track/replacement"

    async def _prefetched_round() -> MusicQuizRound:
        return rejected_round

    plugin._next_round_task = asyncio.create_task(_prefetched_round())
    quiz_type.prepare_round = AsyncMock(  # type: ignore[method-assign]
        side_effect=[rejected_round, replacement_round]
    )
    quiz_type.reject_track = MagicMock(wraps=quiz_type.reject_track)  # type: ignore[method-assign]
    cast("AsyncMock", plugin._play_track).side_effect = [playback_error, None]

    await plugin.start_game()

    assert game.phase == MusicQuizPhase.ANSWERING
    assert game.rounds == [replacement_round]
    assert cast("AsyncMock", plugin._play_track).await_args_list[0].args == (
        "spotify://track/unplayable",
    )
    assert cast("AsyncMock", plugin._play_track).await_args_list[1].args == (
        "spotify://track/replacement",
    )
    assert quiz_type.reject_track.call_count == 2
    quiz_type.reject_track.assert_called_with("spotify://track/unplayable")
    cast("MagicMock", plugin.logger.warning).assert_called_once_with(
        "Could not play Music Quiz track %s; preparing a replacement: %s",
        "spotify://track/unplayable",
        playback_error,
    )


@pytest.mark.asyncio
async def test_rejected_uri_is_not_retried_and_replacement_attempts_are_bounded() -> None:
    """Never replay a rejected URI when a strategy keeps returning it."""
    plugin = _create_plugin()
    await plugin.create_game(round_count=1, source_uris=["library://playlist/1"])
    game = plugin._game
    quiz_type = plugin._quiz_type
    assert game is not None
    assert quiz_type is not None
    plugin._cancel_next_round_task()
    rejected_round = _make_round(0)
    rejected_round.track_uri = "spotify://track/unplayable"

    async def _prefetched_round() -> MusicQuizRound:
        return rejected_round

    plugin._next_round_task = asyncio.create_task(_prefetched_round())
    quiz_type.prepare_round = AsyncMock(return_value=rejected_round)  # type: ignore[method-assign]
    cast("AsyncMock", plugin._play_track).side_effect = MediaNotFoundError(
        "No playable items found"
    )

    with pytest.raises(MediaNotFoundError, match="after 5 attempts"):
        await plugin.start_game()

    cast("AsyncMock", plugin._play_track).assert_awaited_once_with("spotify://track/unplayable")
    assert quiz_type.prepare_round.await_count == 4
    assert game.phase == MusicQuizPhase.LOBBY
    assert game.rounds == []


@pytest.mark.asyncio
async def test_exhausted_replacements_leave_reveal_retryable() -> None:
    """Keep the revealed round unchanged when no replacement can be prepared."""
    plugin = _create_plugin()
    await _create_started_game(plugin, player_names=(), round_count=2)
    game = plugin._game
    quiz_type = plugin._quiz_type
    assert game is not None
    assert quiz_type is not None
    first_round = game.rounds[0]
    await plugin.reveal()
    plugin._cancel_next_round_task()
    rejected_round = _make_round(1)
    rejected_round.track_uri = "spotify://track/unplayable"

    async def _prefetched_round() -> MusicQuizRound:
        return rejected_round

    plugin._next_round_task = asyncio.create_task(_prefetched_round())
    quiz_type.prepare_round = AsyncMock(  # type: ignore[method-assign]
        side_effect=InvalidDataError("No unused source tracks are available")
    )
    cast("AsyncMock", plugin._play_track).reset_mock()
    cast("AsyncMock", plugin._play_track).side_effect = MediaNotFoundError(
        "No playable items found"
    )

    with pytest.raises(InvalidDataError, match="No unused source tracks"):
        await plugin.next_round()

    assert game.phase == MusicQuizPhase.REVEAL
    assert game.current_round_index == 0
    assert game.rounds == [first_round]

    replacement_round = _make_round(1)
    replacement_round.track_uri = "spotify://track/recovered"
    quiz_type.prepare_round = AsyncMock(return_value=replacement_round)  # type: ignore[method-assign]
    cast("AsyncMock", plugin._play_track).side_effect = None

    await plugin.next_round()

    assert _phase(plugin) == MusicQuizPhase.ANSWERING
    assert game.rounds == [first_round, replacement_round]


@pytest.mark.asyncio
async def test_system_context_is_restored_when_round_start_fails() -> None:
    """Restore requesting auth context when internal playback raises."""
    plugin = _create_plugin()
    await plugin.create_game(round_count=1, source_uris=["library://playlist/1"])
    game = plugin._game
    assert game is not None
    requesting_user = cast("User", _guest_user())
    requesting_impersonation = cast("User", _guest_user())
    current_user_token = current_user.set(requesting_user)
    impersonated_user_token = impersonated_user.set(requesting_impersonation)
    cast("AsyncMock", plugin._play_track).side_effect = MusicQuizNoPlaybackTargetError(
        "No playback target"
    )
    try:
        with pytest.raises(MusicQuizNoPlaybackTargetError):
            await plugin.start_game()
        assert current_user.get() is requesting_user
        assert impersonated_user.get() is requesting_impersonation
    finally:
        impersonated_user.reset(impersonated_user_token)
        current_user.reset(current_user_token)

    assert game.phase == MusicQuizPhase.LOBBY
    assert game.rounds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_error", [None, RuntimeError("Stop failed")])
async def test_final_guest_ready_stops_playback_in_system_context(
    stop_error: RuntimeError | None,
) -> None:
    """Finish from guest Ready while keeping queue stop provider-owned."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    stop_contexts: list[tuple[User | None, User | None]] = []

    async def _stop(_queue_id: str) -> None:
        stop_contexts.append((current_user.get(), impersonated_user.get()))
        if stop_error is not None:
            raise stop_error

    plugin._playback_session = cast(
        "Any",
        SimpleNamespace(queue_id="quiz_queue", player_id="quiz_player"),
    )
    plugin._stop_playback = MusicQuizPlugin._stop_playback.__get__(  # type: ignore[method-assign]
        plugin, MusicQuizPlugin
    )
    cast("MagicMock", plugin.mass.players.get_player).return_value = SimpleNamespace()
    stop = AsyncMock(side_effect=_stop)
    cast("MagicMock", plugin.mass.player_queues).stop = stop
    requesting_user = cast("User", _guest_user())
    requesting_impersonation = cast("User", _guest_user())
    current_user_token = current_user.set(requesting_user)
    impersonated_user_token = impersonated_user.set(requesting_impersonation)
    try:
        await plugin.answer(player_ids["Alice"], "correct_0")
        await plugin.ready(player_ids["Alice"])
        assert current_user.get() is requesting_user
        assert impersonated_user.get() is requesting_impersonation
    finally:
        impersonated_user.reset(impersonated_user_token)
        current_user.reset(current_user_token)

    assert stop_contexts == [(None, None)]
    stop.assert_awaited_once_with("quiz_queue")
    assert game.phase == MusicQuizPhase.FINISHED
    if stop_error is not None:
        cast("MagicMock", plugin.logger.warning).assert_called_once_with(
            "Could not stop Music Quiz playback: %s",
            stop_error,
        )


@pytest.mark.asyncio
async def test_play_track_rejects_busy_announcement_target() -> None:
    """Do not open an audio round when the target ignores playback during an announcement."""
    plugin = _create_plugin()
    plugin._game = _fake_game()
    plugin._quiz_type = MagicMock(uses_audio=True)
    plugin._get_playback_session = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(queue_id="quiz_queue", player_id="quiz_player")
    )
    cast("MagicMock", plugin.mass.players.get_player).return_value = SimpleNamespace(
        extra_data={"announcement_in_progress": True}
    )
    play_media = AsyncMock()
    cast("MagicMock", plugin.mass.player_queues).play_media = play_media

    with pytest.raises(MusicQuizNoPlaybackTargetError, match="announcement"):
        await MusicQuizPlugin._play_track(plugin, "spotify://track/test")

    play_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_guess_the_song_warms_lyrics_through_quiz_capability() -> None:
    """Keep lyrics warm-up enabled for guess-the-song rounds."""
    plugin = _create_plugin()
    plugin._warm_up_lyrics = MagicMock()  # type: ignore[method-assign]

    await _create_started_game(plugin, player_names=())

    plugin._warm_up_lyrics.assert_called_once_with("library://track/0")


@pytest.mark.asyncio
async def test_generic_submit_answer_uses_discriminated_payload() -> None:
    """The generic command accepts a strict typed multiple-choice submission."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=120.0),
    ):
        state = cast(
            "dict[str, Any]",
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "multiple_choice",
                    "suggestion_id": "correct_0",
                },
            ),
        )

    assert state["phase"] == "reveal"
    assert state["you"]["answer"]["correct"] is True
    assert state["you"]["answer"]["points"] == 1000
    assert game.players[player_ids["Alice"]].last_seen == 120.0


@pytest.mark.asyncio
async def test_available_quiz_types_reflect_ai_plugin_availability() -> None:
    """Expose Trivia only when a loaded AI_QUERY plugin can support it."""
    plugin = _create_plugin()
    providers = cast("MagicMock", plugin.mass.get_providers_supporting_feature)

    providers.return_value = []
    assert await plugin.available_quiz_types() == ["guess_the_song", "hitster"]
    providers.return_value = [MagicMock()]
    assert await plugin.available_quiz_types() == ["guess_the_song", "hitster"]
    providers.return_value = [MagicMock(spec=PluginProvider)]
    assert await plugin.available_quiz_types() == ["guess_the_song", "hitster", "trivia"]


@pytest.mark.parametrize(
    ("previous_entry_id", "next_entry_id"),
    [(None, "anchor"), ("anchor", None)],
)
@pytest.mark.asyncio
async def test_hitster_edge_placement_accepts_null_at_api_boundary(
    previous_entry_id: str | None,
    next_entry_id: str | None,
) -> None:
    """Accept either null timeline boundary through the registered API handler."""
    plugin = _create_plugin()
    registered: dict[str, APICommandHandler] = {}

    def _register(command: str, handler: Any, **kwargs: Any) -> Any:
        registered[command] = APICommandHandler.parse(command, handler, **kwargs)
        return MagicMock()

    cast("MagicMock", plugin.mass).register_api_command.side_effect = _register
    await plugin.loaded_in_mass()
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        player_ids = await _create_started_hitster_game(plugin)
        handler = registered["music_quiz/submit_answer"]
        arguments = parse_arguments(
            handler.signature,
            handler.type_hints,
            {
                "player_id": player_ids["Alice"],
                "submission": {
                    "answer_type": "timeline",
                    "action": "place",
                    "previous_entry_id": previous_entry_id,
                    "next_entry_id": next_entry_id,
                },
            },
        )
        invalid_arguments = parse_arguments(
            handler.signature,
            handler.type_hints,
            {
                "player_id": player_ids["Alice"],
                "submission": {
                    "answer_type": "timeline",
                    "action": "place",
                    "previous_entry_id": 1,
                    "next_entry_id": "anchor",
                },
            },
        )
        with patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ):
            with pytest.raises(MusicQuizInvalidAnswerError):
                await cast(
                    "Coroutine[Any, Any, Any]",
                    handler.target(**invalid_arguments),
                )
            await cast("Coroutine[Any, Any, Any]", handler.target(**arguments))

    game = plugin._game
    assert game is not None
    placement = _timeline_answer_state(game.rounds[0]).placements[player_ids["Alice"]]
    assert placement.previous_entry_id == previous_entry_id
    assert placement.next_entry_id == next_entry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("providers", [[], [MagicMock()]])
async def test_trivia_creation_rejects_without_ai_plugin(providers: list[object]) -> None:
    """Require a loaded AI_QUERY plugin before creating a Trivia game."""
    plugin = _create_plugin()
    cast("MagicMock", plugin.mass.get_providers_supporting_feature).return_value = providers

    with pytest.raises(InvalidDataError) as error:
        await plugin.create_game(
            quiz_type="trivia",
            source_uris=["library://playlist/1"],
        )

    assert error.value.translation_key == "music_quiz_trivia_ai_provider_required"
    assert plugin._game is None


@pytest.mark.asyncio
async def test_trivia_creation_initializes_with_ai_plugin() -> None:
    """Create Trivia when an AI plugin and enough selected metadata are available."""
    plugin = _create_plugin()
    ai_provider = MagicMock(spec=PluginProvider)
    ai_provider.instance_id = "ai--test"
    cast("MagicMock", plugin.mass.get_providers_supporting_feature).return_value = [ai_provider]
    eligible_tracks = AsyncMock(return_value={"library://track/1": MagicMock()})
    prepare_round = AsyncMock(side_effect=_make_trivia_round)
    with (
        patch.object(TriviaQuizType, "_get_eligible_tracks", new=eligible_tracks),
        patch.object(TriviaQuizType, "prepare_round", new=prepare_round),
    ):
        state = await plugin.create_game(
            quiz_type="trivia",
            round_count=1,
            source_uris=["library://playlist/1"],
        )
        await asyncio.sleep(0)

    assert state["quiz_type"] == "trivia"
    assert state["answer_type"] == "multiple_choice"
    eligible_tracks.assert_awaited_once()
    assert plugin._next_round_task is not None
    plugin._cancel_next_round_task()


@pytest.mark.asyncio
async def test_trivia_creation_closes_previous_audio_session() -> None:
    """Detach existing audio listeners when replacing an audio lobby with Trivia."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    previous_game = plugin._game
    playback_session = MagicMock()
    playback_session.close = AsyncMock()
    plugin._playback_session = playback_session
    with (
        patch.object(TriviaQuizType, "initialize", new=AsyncMock()),
        patch.object(
            TriviaQuizType,
            "prepare_round",
            new=AsyncMock(side_effect=_make_trivia_round),
        ),
    ):
        state = await plugin.create_game(
            quiz_type="trivia",
            round_count=1,
            source_uris=["library://playlist/1"],
        )

    assert state["quiz_type"] == "trivia"
    assert plugin._game is not previous_game
    assert vars(plugin)["_playback_session"] is None
    playback_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_trivia_replacement_blocks_concurrent_listen_in_recreation() -> None:
    """Commit non-audio state before a waiting listener can recreate a session."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    playback_session = MagicMock()
    playback_session.add_guest_listener = AsyncMock()

    async def _close_session() -> None:
        assert plugin._playback_lock.locked()
        close_started.set()
        await allow_close.wait()

    playback_session.close = AsyncMock(side_effect=_close_session)
    plugin._playback_session = playback_session
    with (
        patch.object(TriviaQuizType, "initialize", new=AsyncMock()),
        patch.object(
            TriviaQuizType,
            "prepare_round",
            new=AsyncMock(side_effect=_make_trivia_round),
        ),
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
    ):
        create_task = asyncio.create_task(
            plugin.create_game(
                quiz_type="trivia",
                round_count=1,
                source_uris=["library://playlist/1"],
            )
        )
        await close_started.wait()
        listen_task = asyncio.create_task(plugin.listen_in("web-player"))
        await asyncio.sleep(0)
        assert not listen_task.done()
        allow_close.set()
        state = await create_task
        with pytest.raises(MusicQuizNoPlaybackTargetError):
            await listen_task

    assert state["quiz_type"] == "trivia"
    assert vars(plugin)["_playback_session"] is None
    playback_session.add_guest_listener.assert_not_awaited()


@pytest.mark.asyncio
async def test_trivia_serialization_and_listen_in_remain_non_audio() -> None:
    """Expose flat redacted Trivia state without playback, lyrics, or listen-in."""
    plugin = _create_plugin(use_ai_distractors=True)
    plugin._warm_up_lyrics = MagicMock()  # type: ignore[method-assign]
    prepare_round = AsyncMock(side_effect=_make_trivia_round)
    with (
        patch.object(TriviaQuizType, "initialize", new=AsyncMock()),
        patch.object(TriviaQuizType, "prepare_round", new=prepare_round),
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
    ):
        await plugin.create_game(
            quiz_type="trivia",
            round_count=1,
            suggestion_count=4,
            answer_duration=20,
            source_uris=["library://playlist/1"],
            name="Music Trivia",
            difficulty="hard",
            artist_bonus_mode="free_text",
            title_bonus_mode="multiple_choice",
        )
        alice = (await plugin.join_game("Alice"))["player_id"]
        await plugin.start_game()

        game = plugin._game
        assert game is not None
        assert game.config.difficulty == "hard"
        assert game.config.use_ai_distractors is False
        assert game.config.artist_bonus_mode == "off"
        assert game.config.title_bonus_mode == "off"
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        cast("AsyncMock", plugin._play_track).assert_not_awaited()
        plugin._warm_up_lyrics.assert_not_called()

        public_state = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]["state"]
        assert public_state["quiz_type"] == "trivia"
        assert public_state["answer_type"] == "multiple_choice"
        assert public_state["current_round"]["question"] == "Trivia question 0?"
        assert set(public_state["current_round"]) == {
            "round_index",
            "started_at",
            "deadline",
            "auto_advance_at",
            "question",
            "suggestions",
        }
        assert all(
            set(suggestion) == {"suggestion_id", "label"}
            for suggestion in public_state["current_round"]["suggestions"]
        )
        assert "library://track/trivia-0" not in str(public_state)
        assert alice not in str(public_state)

        host_state = await plugin.get_game()
        assert host_state is not None
        assert host_state["rounds"][0]["question"] == "Trivia question 0?"
        assert host_state["rounds"][0]["track_uri"] is None
        assert host_state["rounds"][0]["duration"] is None
        assert host_state["rounds"][0]["image_url"] is None
        game_info = await plugin.get_game_info()
        assert game_info is not None
        assert game_info["quiz_type"] == "trivia"
        personal_state = await plugin.get_player_state(alice)
        assert personal_state["current_round"] == public_state["current_round"]
        assert alice not in str(personal_state)
        assert await plugin.can_listen_in("web-player") is False
        with pytest.raises(MusicQuizNoPlaybackTargetError):
            await plugin.listen_in("web-player")
        await plugin.answer(alice, "correct_0")
        revealed = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]["state"]
        assert revealed["current_round"]["answer_label"] == "Correct answer 0"
        assert revealed["current_round"]["track_uri"] is None
        assert revealed["current_round"]["correct_suggestion_id"] == "correct_0"
        assert all(
            set(suggestion) == {"suggestion_id", "label"}
            for suggestion in revealed["current_round"]["suggestions"]
        )
        await plugin.delete_game()

    cast("AsyncMock", plugin._stop_playback).assert_not_awaited()


@pytest.mark.asyncio
async def test_trivia_reuses_full_multiplayer_flow_and_persisted_prefetch() -> None:
    """Reuse early reveal, eligibility, presence, scoring, deadlines and prefetch."""
    plugin = _create_plugin()
    prepare_round = AsyncMock(side_effect=_make_trivia_round)
    with (
        patch.object(TriviaQuizType, "initialize", new=AsyncMock()),
        patch.object(TriviaQuizType, "prepare_round", new=prepare_round),
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
    ):
        await plugin.create_game(
            quiz_type="trivia",
            round_count=3,
            source_uris=["library://playlist/1"],
        )
        alice = (await plugin.join_game("Alice"))["player_id"]
        bob = (await plugin.join_game("Bob"))["player_id"]
        await plugin.start_game()
        game = plugin._game
        assert game is not None

        late = (await plugin.join_game("Late"))["player_id"]
        assert game.players[late].active_from_round == 1
        await plugin.answer(alice, "correct_0")
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        await plugin.answer(bob, "correct_0")
        assert _phase(plugin) == MusicQuizPhase.REVEAL
        assert (game.players[alice].score, game.players[bob].score) == (1000, 500)

        await plugin.ready(alice)
        await plugin.ready(bob)
        await plugin.ready(late)
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        assert game.current_round_index == 1

        game.players[bob].last_seen = 0
        game.players[alice].last_seen = 1000
        game.players[late].last_seen = 1000
        with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
            await plugin._expire_inactive_players()
        assert bob not in game.players
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        await plugin.answer(alice, "correct_1")
        await plugin.answer(late, "correct_1")
        assert _phase(plugin) == MusicQuizPhase.REVEAL
        assert (game.players[alice].score, game.players[late].score) == (2000, 500)

        await plugin.ready(alice)
        await plugin.ready(late)
        assert game.current_round_index == 2
        await plugin.answer(alice, "correct_2")
        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        _, deadline_callback, round_index = _round_timer_call(plugin, plugin._reveal_timer_id)
        await deadline_callback(round_index)
        assert _phase(plugin) == MusicQuizPhase.REVEAL
        assert (game.players[alice].score, game.players[late].score) == (3000, 500)
        await plugin.next_round()
        assert _phase(plugin) == MusicQuizPhase.FINISHED

        first_run_calls = prepare_round.await_args_list[:3]
        assert [call.args[0] for call in first_run_calls] == [0, 1, 2]
        assert [len(call.args[1]) for call in first_run_calls] == [0, 1, 2]
        cast("AsyncMock", plugin._stop_playback).assert_not_awaited()


@pytest.mark.asyncio
async def test_trivia_reset_delete_and_recreate_keep_lifecycle_non_audio() -> None:
    """Reset, delete and recreate Trivia transactionally without playback hooks."""
    plugin = _create_plugin()
    initialize = AsyncMock()
    prepare_round = AsyncMock(side_effect=_make_trivia_round)
    with (
        patch.object(TriviaQuizType, "initialize", new=initialize),
        patch.object(TriviaQuizType, "prepare_round", new=prepare_round),
    ):
        await plugin.create_game(
            quiz_type="trivia",
            round_count=1,
            source_uris=["library://playlist/1"],
        )
        await plugin.start_game()
        game = plugin._game
        assert game is not None
        reset_state = await plugin.reset()
        assert reset_state["phase"] == "lobby"
        assert reset_state["quiz_type"] == "trivia"
        assert reset_state["answer_type"] == "multiple_choice"
        assert game.rounds == []
        assert initialize.await_count == 2
        await plugin.delete_game()
        assert plugin._game is None
        cast("AsyncMock", plugin._stop_playback).assert_not_awaited()

        recreated = await plugin.create_game(
            quiz_type="trivia",
            round_count=1,
            source_uris=["library://playlist/1"],
        )
        assert recreated["phase"] == "lobby"
        assert recreated["quiz_type"] == "trivia"
        await plugin.delete_game()


@pytest.mark.asyncio
async def test_hitster_create_uses_flat_config_and_derived_answer_type() -> None:
    """Create Hitster from flattened fields without exposing GTS-only controls."""
    plugin = _create_plugin(use_ai_distractors=True)
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        created = await plugin.create_game(
            quiz_type="hitster",
            round_count=2,
            suggestion_count=9,
            source_uris=["library://playlist/1"],
            difficulty="not-used",
            artist_bonus_mode="free_text",
            title_bonus_mode="multiple_choice",
        )

    game = plugin._game
    assert game is not None
    assert game.quiz_type == "hitster"
    assert game.answer_type == "timeline"
    assert game.config.suggestion_count == 4
    assert game.config.difficulty == "normal"
    assert game.config.use_ai_distractors is True
    assert created["artist_bonus_mode"] == "free_text"
    assert created["title_bonus_mode"] == "multiple_choice"
    assert "suggestion_count" not in created


@pytest.mark.asyncio
async def test_hitster_placement_auto_reveals_without_bonuses_and_never_warms_lyrics() -> None:
    """Complete on placement, preserve playback, and skip lyrics warm-up for Hitster."""
    plugin = _create_plugin()
    plugin._warm_up_lyrics = MagicMock()  # type: ignore[method-assign]
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        player_ids = await _create_started_hitster_game(plugin)
        _, deadline_callback, round_index = _round_timer_call(plugin, plugin._reveal_timer_id)
        signal = cast("MagicMock", plugin.signal_provider_event)
        hidden_state = signal.call_args[0][0]["state"]
        hidden_round = hidden_state["current_round"]
        assert set(hidden_round) == {
            "round_index",
            "started_at",
            "deadline",
            "auto_advance_at",
            "question",
            "timeline",
            "bonus_definitions",
        }
        assert hidden_round["auto_advance_at"] is None
        assert hidden_round["timeline"][0]["is_anchor"] is True
        assert "Secret Title" not in str(hidden_state)
        assert player_ids["Alice"] not in str(hidden_state)

        with (
            patch(
                "music_assistant.providers.music_quiz.get_current_user",
                return_value=_guest_user(),
            ),
            patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
        ):
            state = cast(
                "dict[str, Any]",
                await plugin.submit_answer(
                    player_ids["Alice"],
                    {
                        "answer_type": "timeline",
                        "action": "place",
                        "previous_entry_id": "anchor",
                        "next_entry_id": None,
                    },
                ),
            )

        advance_delay, _, _ = _round_timer_call(plugin, plugin._advance_timer_id)
        await deadline_callback(round_index)
        game = plugin._game
        assert game is not None
        assert game.phase == MusicQuizPhase.REVEAL
        assert advance_delay == 30.0
        assert game.rounds[0].auto_advance_at == 130.0
        assert state["current_round"]["auto_advance_at"] == 130.0
        assert game.players[player_ids["Alice"]].score == 1000
        cast("AsyncMock", plugin._play_track).assert_awaited_with("library://track/hitster-0")
        plugin._warm_up_lyrics.assert_not_called()
        assert state["current_round"]["revealed_entry"]["release_year"] == 2000
        assert [entry["entry_id"] for entry in state["current_round"]["timeline"]] == [
            "anchor",
            "hitster-0",
        ]
        assert (
            sum(entry["entry_id"] == "hitster-0" for entry in state["current_round"]["timeline"])
            == 1
        )
        assert state["you"]["answer"]["previous_entry_id"] == "anchor"
        assert state["you"]["answer"]["correct"] is True
        assert state["you"]["answer"]["points"] == 1000
        cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
        with patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ):
            finished = await plugin.ready(player_ids["Alice"])
        assert _phase(plugin) == MusicQuizPhase.FINISHED
        assert finished["current_round"]["auto_advance_at"] is None
        cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._advance_timer_id)


@pytest.mark.asyncio
async def test_hitster_ready_cancels_intermediate_auto_advance() -> None:
    """Advance once on Ready and ignore the stale reveal timer."""
    plugin = _create_plugin()
    prepare_round = AsyncMock(
        side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
    )
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=prepare_round,
        ),
    ):
        player_ids = await _create_started_hitster_game(plugin, round_count=2)
        with (
            patch(
                "music_assistant.providers.music_quiz.get_current_user",
                return_value=_guest_user(),
            ),
            patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
        ):
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "timeline",
                    "action": "place",
                    "previous_entry_id": "anchor",
                    "next_entry_id": None,
                },
            )

        game = plugin._game
        assert game is not None
        first_round = game.rounds[0]
        advance_delay, stale_callback, stale_round_index = _round_timer_call(
            plugin, plugin._advance_timer_id
        )
        assert _phase(plugin) == MusicQuizPhase.REVEAL
        assert advance_delay == 30.0
        assert first_round.auto_advance_at == 130.0
        cast("AsyncMock", plugin._play_track).reset_mock()
        cast("MagicMock", plugin.mass.cancel_timer).reset_mock()

        with (
            patch(
                "music_assistant.providers.music_quiz.get_current_user",
                return_value=_guest_user(),
            ),
            patch("music_assistant.providers.music_quiz.time.time", return_value=110.0),
        ):
            state = await plugin.ready(player_ids["Alice"])

        assert _phase(plugin) == MusicQuizPhase.ANSWERING
        assert game.current_round_index == 1
        assert len(game.rounds) == 2
        assert first_round.to_dict()["auto_advance_at"] is None
        assert state["current_round"]["round_index"] == 1
        cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._advance_timer_id)
        cast("AsyncMock", plugin._play_track).assert_awaited_once_with("library://track/hitster-1")
        assert prepare_round.await_count == 2

        await stale_callback(stale_round_index)

    assert _phase(plugin) == MusicQuizPhase.ANSWERING
    assert game.current_round_index == 1
    assert len(game.rounds) == 2
    cast("AsyncMock", plugin._play_track).assert_awaited_once()
    assert prepare_round.await_count == 2


@pytest.mark.asyncio
async def test_hitster_finish_skips_unanswered_bonus() -> None:
    """Keep finish as a backward-compatible way to skip a bonus."""
    definitions: list[TimelineBonusDefinition] = [
        TimelineFreeTextBonusDefinition(
            bonus_type=TimelineBonusType.ARTIST,
        ),
        TimelineMultipleChoiceBonusDefinition(
            bonus_type=TimelineBonusType.TITLE,
            options=[
                TimelineBonusOption("correct-title", "Secret Title 0", True),
                TimelineBonusOption("wrong-a", "Wrong A"),
                TimelineBonusOption("wrong-b", "Wrong B"),
                TimelineBonusOption("wrong-c", "Wrong C"),
            ],
        ),
    ]
    plugin = _create_plugin()
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(
                    round_index, previous, definitions
                )
            ),
        ),
    ):
        player_ids = await _create_started_hitster_game(
            plugin,
            artist_bonus_mode="free_text",
            title_bonus_mode="multiple_choice",
        )
        with patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ):
            placed = cast(
                "dict[str, Any]",
                await plugin.submit_answer(
                    player_ids["Alice"],
                    {
                        "answer_type": "timeline",
                        "action": "place",
                        "previous_entry_id": "anchor",
                        "next_entry_id": None,
                    },
                ),
            )
            assert placed["phase"] == "answering"
            assert placed["you"]["answer"]["finished"] is False
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "timeline",
                    "action": "bonus_text",
                    "bonus_type": "artist",
                    "value": "secret artist 0",
                },
            )
            revealed = cast(
                "dict[str, Any]",
                await plugin.submit_answer(
                    player_ids["Alice"],
                    {"answer_type": "timeline", "action": "finish"},
                ),
            )

    game = plugin._game
    assert game is not None
    assert game.phase == MusicQuizPhase.REVEAL
    assert game.players[player_ids["Alice"]].score == 1250
    assert revealed["you"]["answer"]["finished"] is True
    assert revealed["you"]["answer"]["bonus_results"] == [
        {"bonus_type": "artist", "correct": True, "points": 250}
    ]


@pytest.mark.asyncio
async def test_hitster_deadline_scores_unfinished_placement_and_bonus() -> None:
    """Reveal and score submitted work at the deadline without a finish action."""
    definitions: list[TimelineBonusDefinition] = [
        TimelineFreeTextBonusDefinition(
            bonus_type=TimelineBonusType.ARTIST,
        ),
        TimelineMultipleChoiceBonusDefinition(
            bonus_type=TimelineBonusType.TITLE,
            options=[
                TimelineBonusOption("correct-title", "Secret Title 0", True),
                TimelineBonusOption("wrong-a", "Wrong A"),
                TimelineBonusOption("wrong-b", "Wrong B"),
                TimelineBonusOption("wrong-c", "Wrong C"),
            ],
        ),
    ]
    plugin = _create_plugin()
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(
                    round_index, previous, definitions
                )
            ),
        ),
    ):
        with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
            player_ids = await _create_started_hitster_game(
                plugin,
                artist_bonus_mode="free_text",
                title_bonus_mode="multiple_choice",
            )
        with (
            patch(
                "music_assistant.providers.music_quiz.get_current_user",
                return_value=_guest_user(),
            ),
            patch("music_assistant.providers.music_quiz.time.time", return_value=101.0),
        ):
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "timeline",
                    "action": "place",
                    "previous_entry_id": "anchor",
                    "next_entry_id": None,
                },
            )
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "timeline",
                    "action": "bonus_text",
                    "bonus_type": "artist",
                    "value": "Secret Artist 0",
                },
            )
        _, deadline_callback, round_index = _round_timer_call(plugin, plugin._reveal_timer_id)
        with patch("music_assistant.providers.music_quiz.time.time", return_value=130.0):
            await deadline_callback(round_index)

    game = plugin._game
    assert game is not None
    assert game.phase == MusicQuizPhase.REVEAL
    assert game.players[player_ids["Alice"]].score == 1250
    advance_delay, _, _ = _round_timer_call(plugin, plugin._advance_timer_id)
    assert advance_delay == 150.0
    assert game.rounds[0].auto_advance_at == 280.0
    public_state = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]["state"]
    player = public_state["players"][0]
    assert player["answered"] is False
    assert player["last_answer"]["placement"]["points"] == 1000
    assert player["last_answer"]["artist"]["points"] == 250


@pytest.mark.asyncio
async def test_hitster_manual_reveal_keeps_track_end_auto_advance() -> None:
    """Keep the existing track-end schedule for a manual Hitster reveal."""
    plugin = _create_plugin()
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
    ):
        await _create_started_hitster_game(plugin)

    with patch("music_assistant.providers.music_quiz.time.time", return_value=110.0):
        state = await plugin.reveal()

    game = plugin._game
    assert game is not None
    advance_delay, _, _ = _round_timer_call(plugin, plugin._advance_timer_id)
    assert advance_delay == 170.0
    assert game.rounds[0].auto_advance_at == 280.0
    assert state["current_round"]["auto_advance_at"] == 280.0


@pytest.mark.asyncio
async def test_hitster_host_public_and_personalized_rounds_remain_flat() -> None:
    """Compose strategy fragments without leaking nested persisted answer state."""
    definitions: list[TimelineBonusDefinition] = [
        TimelineFreeTextBonusDefinition(
            bonus_type=TimelineBonusType.ARTIST,
        )
    ]
    plugin = _create_plugin()
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(
                    round_index, previous, definitions
                )
            ),
        ),
    ):
        player_ids = await _create_started_hitster_game(
            plugin,
            artist_bonus_mode="free_text",
        )
        signal = cast("MagicMock", plugin.signal_provider_event)
        event_state = signal.call_args[0][0]["state"]
        public_player_keys = {
            "name",
            "score",
            "ready",
            "active_from_round",
            "answered",
            "placed",
            "artist_bonus_answered",
            "title_bonus_answered",
        }
        assert set(event_state["players"][0]) == public_player_keys
        assert event_state["players"][0]["active_from_round"] == 0
        host_state = await plugin.get_game()
        assert host_state is not None
        assert host_state["players"] == event_state["players"]
        host_round = host_state["rounds"][0]
        assert "answer_state" not in host_round
        assert host_round["candidate"]["entry"]["title"] == "Secret Title 0"
        assert host_round["placement_snapshot"][0]["is_anchor"] is True
        for player_id in player_ids.values():
            assert player_id not in str(event_state)
            assert player_id not in str(host_state["players"])

        with patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ):
            personal = cast(
                "dict[str, Any]",
                await plugin.submit_answer(
                    player_ids["Alice"],
                    {
                        "answer_type": "timeline",
                        "action": "place",
                        "previous_entry_id": "anchor",
                        "next_entry_id": None,
                    },
                ),
            )
            reconnected = await plugin.get_player_state(player_ids["Alice"])
            with pytest.raises(MusicQuizInvalidAnswerError, match="multiple_choice"):
                await plugin.answer(player_ids["Alice"], "not-supported")
        assert set(personal["players"][0]) == public_player_keys
        assert set(personal["you"]) == {
            "name",
            "score",
            "ready",
            "active_from_round",
            "answer",
        }
        assert personal["you"]["active_from_round"] == 0
        assert personal["you"]["answer"] == {
            "previous_entry_id": "anchor",
            "next_entry_id": None,
            "answered_at": personal["you"]["answer"]["answered_at"],
            "bonuses": [],
            "finished": False,
        }
        assert reconnected == personal
        for player_id in player_ids.values():
            assert player_id not in str(personal)


@pytest.mark.asyncio
async def test_hitster_reset_preserves_config_and_reinitializes_strategy() -> None:
    """Reset Hitster to a fresh lobby while preserving its typed game config."""
    plugin = _create_plugin()
    pending_task = MagicMock()
    pending_task.cancelled.return_value = False
    initialize_call_count = 0

    def _initialize() -> None:
        nonlocal initialize_call_count
        initialize_call_count += 1
        if initialize_call_count == 2:
            pending_task.cancel.assert_not_called()
            assert plugin._next_round_task is pending_task
            cast("MagicMock", plugin.mass.cancel_timer).assert_not_called()
            cast("MagicMock", plugin.mass.cancel_task).assert_not_called()
            cast("AsyncMock", plugin._stop_playback).assert_not_awaited()

    initialize = AsyncMock(side_effect=_initialize)
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=initialize,
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        await _create_started_hitster_game(plugin)
        plugin._next_round_task = pending_task
        cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
        cast("MagicMock", plugin.mass.cancel_task).reset_mock()
        cast("AsyncMock", plugin._stop_playback).reset_mock()
        state = await plugin.reset()

    game = plugin._game
    assert game is not None
    assert initialize.await_count == 2
    pending_task.cancel.assert_called_once()
    cancel_timer = cast("MagicMock", plugin.mass.cancel_timer)
    cancel_timer.assert_any_call(plugin._reveal_timer_id)
    cancel_timer.assert_any_call(plugin._advance_timer_id)
    cancel_timer.assert_any_call(plugin._presence_timer_id)
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(plugin._presence_timer_id)
    cast("AsyncMock", plugin._stop_playback).assert_awaited_once()
    assert game.rounds == []
    assert state["phase"] == "lobby"
    assert state["quiz_type"] == "hitster"
    assert state["answer_type"] == "timeline"
    assert state["artist_bonus_mode"] == "off"
    assert state["title_bonus_mode"] == "off"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_phase",
    [MusicQuizPhase.ANSWERING, MusicQuizPhase.REVEAL],
)
async def test_hitster_failed_reset_preserves_active_game(
    initial_phase: MusicQuizPhase,
) -> None:
    """Keep the current game and its background work intact when initialization fails."""
    plugin = _create_plugin()
    initialize = AsyncMock(
        side_effect=[
            None,
            InvalidDataError("Sources changed"),
        ]
    )
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=initialize,
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        await _create_started_hitster_game(plugin, round_count=2)
        game = plugin._game
        assert game is not None
        if initial_phase == MusicQuizPhase.REVEAL:
            await plugin.reveal()
        old_quiz_type = plugin._quiz_type
        old_answer_type = plugin._answer_type
        old_rounds = list(game.rounds)
        pending_prefetch = plugin._next_round_task
        assert pending_prefetch is not None
        timer_id = (
            plugin._reveal_timer_id
            if initial_phase == MusicQuizPhase.ANSWERING
            else plugin._advance_timer_id
        )
        _, timer_callback, round_index = _round_timer_call(plugin, timer_id)
        cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
        cast("MagicMock", plugin.mass.cancel_task).reset_mock()
        cast("AsyncMock", plugin._stop_playback).reset_mock()

        with pytest.raises(InvalidDataError, match="Sources changed"):
            await plugin.reset()

        assert game.phase == initial_phase
        assert game.rounds == old_rounds
        assert plugin._game is game
        assert plugin._quiz_type is old_quiz_type
        assert plugin._answer_type is old_answer_type
        assert plugin._next_round_task is pending_prefetch
        assert pending_prefetch.cancelled() is False
        cast("MagicMock", plugin.mass.cancel_timer).assert_not_called()
        cast("MagicMock", plugin.mass.cancel_task).assert_not_called()
        cast("AsyncMock", plugin._stop_playback).assert_not_awaited()

        await timer_callback(round_index)
        expected_phase = (
            MusicQuizPhase.REVEAL
            if initial_phase == MusicQuizPhase.ANSWERING
            else MusicQuizPhase.ANSWERING
        )
        assert game.phase == expected_phase
        if plugin._next_round_task is pending_prefetch:
            plugin._cancel_next_round_task()


@pytest.mark.asyncio
async def test_hitster_late_joiner_starts_on_prefetched_next_round() -> None:
    """Exclude a late joiner from the active round and include them in the next snapshot."""
    plugin = _create_plugin()
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=AsyncMock(),
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        player_ids = await _create_started_hitster_game(plugin, round_count=2)
        with patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ):
            joined = await plugin.join_game("Late")
            player_ids["Late"] = joined["player_id"]
            assert joined["state"]["you"]["active_from_round"] == 1
            public_state = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]["state"]
            late_player = next(
                player for player in public_state["players"] if player["name"] == "Late"
            )
            assert late_player == {
                "name": "Late",
                "score": 0,
                "ready": False,
                "active_from_round": 1,
                "answered": False,
                "placed": False,
                "artist_bonus_answered": False,
                "title_bonus_answered": False,
            }
            for player_id in player_ids.values():
                assert player_id not in str(public_state)
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "timeline",
                    "action": "place",
                    "previous_entry_id": "anchor",
                    "next_entry_id": None,
                },
            )
            game = plugin._game
            assert game is not None
            assert _phase(plugin) == MusicQuizPhase.REVEAL
            await plugin.ready(player_ids["Alice"])
            assert _phase(plugin) == MusicQuizPhase.REVEAL
            await plugin.ready(player_ids["Late"])
            assert _phase(plugin) == MusicQuizPhase.ANSWERING
            assert game.current_round_index == 1
            second_state = _timeline_answer_state(game.rounds[1])
            assert [entry.entry_id for entry in second_state.placement_snapshot] == [
                "anchor",
                "hitster-0",
            ]
            await plugin.submit_answer(
                player_ids["Alice"],
                {
                    "answer_type": "timeline",
                    "action": "place",
                    "previous_entry_id": "hitster-0",
                    "next_entry_id": None,
                },
            )
            late_state = cast(
                "dict[str, Any]",
                await plugin.submit_answer(
                    player_ids["Late"],
                    {
                        "answer_type": "timeline",
                        "action": "place",
                        "previous_entry_id": "hitster-0",
                        "next_entry_id": None,
                    },
                ),
            )
            assert late_state["you"]["answer"]["correct"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submission",
    [
        {"suggestion_id": "correct_0"},
        {"answer_type": "unknown", "suggestion_id": "correct_0"},
        {"answer_type": "multiple_choice", "suggestion_id": 1},
        {
            "answer_type": "multiple_choice",
            "suggestion_id": "correct_0",
            "extra": True,
        },
    ],
)
async def test_generic_submit_answer_rejects_invalid_payload(
    submission: QuizAnswerSubmissionPayload,
) -> None:
    """Malformed generic submissions fail without mutating round state."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        pytest.raises(MusicQuizInvalidAnswerError),
    ):
        await plugin.submit_answer(player_ids["Alice"], submission)

    game = plugin._game
    assert game is not None
    assert _answer_state(game.rounds[0]).answers == {}
    assert game.phase == MusicQuizPhase.ANSWERING


@pytest.mark.asyncio
async def test_generic_submit_answer_rejects_game_type_mismatch() -> None:
    """Reject a known discriminator that does not match persisted game identity."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    mismatched_type = SimpleNamespace(answer_type="other")

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch(
            "music_assistant.providers.music_quiz.get_answer_type",
            side_effect=lambda answer_type: (
                mismatched_type if answer_type == "other" else get_answer_type(answer_type)
            ),
        ),
        pytest.raises(MusicQuizInvalidAnswerError, match="does not match"),
    ):
        await plugin.submit_answer(
            player_ids["Alice"],
            {
                "answer_type": "other",
                "suggestion_id": "correct_0",
            },
        )


@pytest.mark.asyncio
async def test_answer_with_unknown_player_is_rejected() -> None:
    """A guest answering with an unknown player_id gets a localized Music Quiz error."""
    plugin = _create_plugin()
    await _create_started_game(plugin)

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        pytest.raises(MusicQuizUnknownPlayerError),
    ):
        await plugin.answer("not-a-real-player", "correct_0")


@pytest.mark.asyncio
async def test_public_state_redacts_answer_data_before_reveal() -> None:
    """Broadcast payloads never leak player IDs or the correct answer early."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)
    signal = cast("MagicMock", plugin.signal_provider_event)

    payload = signal.call_args[0][0]
    assert payload["event"] == "game_updated"
    state = payload["state"]
    assert set(state) == {
        "phase",
        "name",
        "quiz_type",
        "answer_type",
        "mode",
        "round_count",
        "suggestion_count",
        "answer_duration",
        "auto_start_at",
        "players",
        "current_round",
    }
    assert state["phase"] == "answering"
    assert state["quiz_type"] == "guess_the_song"
    assert state["answer_type"] == "multiple_choice"
    assert state["mode"] == "venue"
    public_player_keys = {
        "name",
        "score",
        "ready",
        "active_from_round",
        "answered",
    }
    assert all(
        set(player) == public_player_keys and player["active_from_round"] == 0
        for player in state["players"]
    )
    host_state = await plugin.get_game()
    assert host_state is not None
    assert host_state["players"] == state["players"]
    current_round = state["current_round"]
    assert set(current_round) == {
        "round_index",
        "started_at",
        "deadline",
        "auto_advance_at",
        "question",
        "suggestions",
    }
    for suggestion in current_round["suggestions"]:
        assert set(suggestion) == {"suggestion_id", "label"}
    serialized = str(payload)
    for player_id in player_ids.values():
        assert player_id not in serialized
    assert "is_correct" not in serialized
    assert "track_uri" not in serialized

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        personal_state = await plugin.answer(player_ids["Alice"], "correct_0")

    own_answer = personal_state["you"]["answer"]
    assert set(personal_state) == {*state, "you"}
    assert set(personal_state["you"]) == {
        "name",
        "score",
        "ready",
        "active_from_round",
        "answer",
    }
    assert set(own_answer) == {"suggestion_id", "answered_at"}
    assert own_answer["suggestion_id"] == "correct_0"
    assert "correct" not in own_answer
    assert "points" not in own_answer
    assert "track_uri" not in str(personal_state)
    public_state = signal.call_args[0][0]["state"]
    alice = next(player for player in public_state["players"] if player["name"] == "Alice")
    assert set(alice) == public_player_keys
    assert alice["answered"] is True
    assert "last_answer" not in alice
    for player_id in player_ids.values():
        assert player_id not in str(personal_state)

    # after the reveal the same payload exposes the answer
    await plugin.reveal()
    state = signal.call_args[0][0]["state"]
    current_round = state["current_round"]
    assert set(current_round) == {
        "round_index",
        "started_at",
        "deadline",
        "auto_advance_at",
        "question",
        "suggestions",
        "correct_suggestion_id",
        "answer_label",
        "track_uri",
        "image_url",
        "duration",
        "ended_at",
    }
    assert current_round["correct_suggestion_id"] == "correct_0"
    assert current_round["track_uri"] == "library://track/0"
    assert current_round["answer_label"] == "Artist - Correct 0"
    alice = next(player for player in state["players"] if player["name"] == "Alice")
    assert set(alice) == {*public_player_keys, "last_answer"}
    assert set(alice["last_answer"]) == {"suggestion_id", "correct", "points"}


@pytest.mark.asyncio
async def test_game_info_exposes_game_identity_and_playback_mode() -> None:
    """The join-screen info includes the game identity and playback mode."""
    plugin = _create_plugin(mode="remote", player=None)
    assert await plugin.get_game_info() is None
    await plugin.create_game(source_uris=["library://playlist/1"], name="Test Quiz")

    info = await plugin.get_game_info()
    assert info == {
        "name": "Test Quiz",
        "quiz_type": "guess_the_song",
        "answer_type": "multiple_choice",
        "phase": "lobby",
        "mode": "remote",
        "player_count": 0,
        "round_count": 5,
        "auto_start_at": None,
    }


@pytest.mark.asyncio
async def test_get_game_returns_none_without_active_game() -> None:
    """The host getter exposes an empty state without raising an error."""
    plugin = _create_plugin()

    assert await plugin.get_game() is None


@pytest.mark.asyncio
async def test_host_actions_still_require_active_game() -> None:
    """Host lifecycle actions still reject requests without an active game."""
    plugin = _create_plugin()

    for action in (
        plugin.start_game,
        plugin.reveal,
        plugin.next_round,
        plugin.reset,
        plugin.delete_game,
    ):
        with pytest.raises(MusicQuizNoGameError):
            await action()


@pytest.mark.asyncio
async def test_create_and_get_expose_persisted_quiz_type() -> None:
    """Create and host state use the quiz type persisted on the game."""
    plugin = _create_plugin()

    created = await plugin.create_game(
        quiz_type="guess_the_song",
        source_uris=["library://playlist/1"],
    )
    game = plugin._game
    assert game is not None
    assert game.quiz_type == "guess_the_song"
    assert game.answer_type == "multiple_choice"
    assert created["quiz_type"] == game.quiz_type
    assert created["answer_type"] == game.answer_type
    host_state = await plugin.get_game()
    assert host_state is not None
    assert host_state["quiz_type"] == game.quiz_type
    assert host_state["answer_type"] == game.answer_type


@pytest.mark.asyncio
async def test_host_rounds_preserve_flat_wire_shape() -> None:
    """Keep nested persisted answer state flat in host round payloads."""
    plugin = _create_plugin()
    await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None
    game_round = game.rounds[0]
    answer_state = _answer_state(game_round)

    host_state = await plugin.get_game()

    assert host_state is not None
    assert host_state["rounds"] == [
        {
            "round_index": game_round.round_index,
            "answer_label": game_round.answer_label,
            "suggestions": [
                {
                    "suggestion_id": suggestion.suggestion_id,
                    "label": suggestion.label,
                    "uri": suggestion.uri,
                    "is_correct": suggestion.is_correct,
                }
                for suggestion in answer_state.suggestions
            ],
            "answers": {},
            "track_uri": game_round.track_uri,
            "question": game_round.question,
            "image_url": game_round.image_url,
            "duration": game_round.duration,
            "started_at": game_round.started_at,
            "ended_at": game_round.ended_at,
            "auto_advance_at": game_round.auto_advance_at,
        }
    ]
    assert "answer_state" not in host_state["rounds"][0]


@pytest.mark.asyncio
async def test_cached_strategy_mismatch_is_rejected() -> None:
    """Strategy caches cannot diverge from persisted game identity."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    plugin._answer_type = MagicMock()

    with pytest.raises(InvalidDataError, match="identity mismatch"):
        await plugin.get_game()


@pytest.mark.asyncio
async def test_get_game_rejects_unsupported_active_game() -> None:
    """An active game with an unsupported quiz type still raises its validation error."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    game = plugin._game
    assert game is not None
    game.quiz_type = "unsupported"

    with pytest.raises(InvalidDataError, match="Unknown quiz type"):
        await plugin.get_game()


@pytest.mark.asyncio
async def test_join_and_player_state_expose_persisted_quiz_type() -> None:
    """Join and personalized state include the persisted quiz type."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        joined = await plugin.join_game("Alice")
        assert joined["state"]["quiz_type"] == "guess_the_song"
        assert joined["state"]["answer_type"] == "multiple_choice"
        state = await plugin.get_player_state(joined["player_id"])

    assert state["quiz_type"] == "guess_the_song"
    assert state["answer_type"] == "multiple_choice"


@pytest.mark.asyncio
async def test_presence_expiry_honors_reconnect_grace_boundary() -> None:
    """Keep a player just before the grace deadline and remove them just after it."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
    ):
        joined = await plugin.join_game("Alice")

    game = plugin._game
    assert game is not None
    player_id = joined["player_id"]
    assert game.players[player_id].last_seen == 100.0

    with patch(
        "music_assistant.providers.music_quiz.time.time",
        return_value=100.0 + PLAYER_RECONNECT_GRACE_SECONDS - 0.001,
    ):
        await plugin._expire_inactive_players()

    assert player_id in game.players
    delay, _ = _presence_timer_call(plugin)
    assert delay == pytest.approx(0.001)

    with patch(
        "music_assistant.providers.music_quiz.time.time",
        return_value=100.0 + PLAYER_RECONNECT_GRACE_SECONDS + 0.001,
    ):
        await plugin._expire_inactive_players()

    assert player_id not in game.players
    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=161.0),
    ):
        assert await plugin.heartbeat(player_id) is False
        rejoined = await plugin.join_game("Alice")
    assert rejoined["player_id"] != player_id
    assert rejoined["state"]["you"]["active_from_round"] == 0


@pytest.mark.asyncio
async def test_heartbeat_reschedules_player_expiry() -> None:
    """A heartbeat gives an existing player a fresh reconnect grace period."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
    ):
        joined = await plugin.join_game("Alice")

    player_id = joined["player_id"]
    game = plugin._game
    assert game is not None
    cast("MagicMock", plugin.mass.call_later).reset_mock()

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=130.0),
    ):
        assert await plugin.heartbeat(player_id) is True

    assert game.players[player_id].last_seen == 130.0
    delay, expiry_callback = _presence_timer_call(plugin)
    assert delay == PLAYER_RECONNECT_GRACE_SECONDS

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await expiry_callback()
    assert player_id in game.players
    delay, _ = _presence_timer_call(plugin)
    assert delay == 30.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=190.001):
        await expiry_callback()
    assert player_id not in game.players


@pytest.mark.asyncio
async def test_heartbeat_returns_false_for_missing_game_or_player() -> None:
    """Expected reconnect misses return false instead of raising an API error."""
    plugin = _create_plugin()

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        assert await plugin.heartbeat("missing") is False
        await plugin.create_game(source_uris=["library://playlist/1"])
        assert await plugin.heartbeat("missing") is False
        joined = await plugin.join_game("Alice")
        await plugin.delete_game()
        assert await plugin.heartbeat(joined["player_id"]) is False


@pytest.mark.asyncio
async def test_player_state_fetch_refreshes_presence() -> None:
    """A successful personalized state fetch refreshes reconnect presence."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
    ):
        joined = await plugin.join_game("Alice")

    player_id = joined["player_id"]
    game = plugin._game
    assert game is not None
    cast("MagicMock", plugin.mass.call_later).reset_mock()

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=125.0),
    ):
        state = await plugin.get_player_state(player_id)

    assert game.players[player_id].last_seen == 125.0
    assert "last_seen" not in str(state)
    delay, _ = _presence_timer_call(plugin)
    assert delay == PLAYER_RECONNECT_GRACE_SECONDS


@pytest.mark.asyncio
async def test_answer_and_ready_refresh_presence() -> None:
    """Successful answer and ready actions refresh player presence."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)
    game = plugin._game
    assert game is not None
    alice = game.players[player_ids["Alice"]]

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=120.0),
    ):
        await plugin.answer(alice.player_id, "correct_0")
    assert alice.last_seen == 120.0

    await plugin.reveal()
    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=130.0),
    ):
        await plugin.ready(alice.player_id)
    assert alice.last_seen == 130.0

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=140.0),
    ):
        await plugin.ready(alice.player_id)
    assert alice.last_seen == 140.0


@pytest.mark.asyncio
async def test_expiry_removes_player_answer_state_from_every_round() -> None:
    """Removing a player clears their answer-owned state from all played rounds."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        await plugin.answer(player_ids["Alice"], "correct_0")
        await plugin.answer(player_ids["Bob"], "wrong_0_1")
        await plugin.ready(player_ids["Alice"])
        await plugin.ready(player_ids["Bob"])
        await plugin.answer(player_ids["Alice"], "correct_1")

    game = plugin._game
    assert game is not None
    assert all(
        player_ids["Alice"] in _answer_state(game_round).answers for game_round in game.rounds
    )
    game.players[player_ids["Alice"]].last_seen = 100.0
    game.players[player_ids["Bob"]].last_seen = 200.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await plugin._expire_inactive_players()

    assert player_ids["Alice"] not in game.players
    assert all(
        player_ids["Alice"] not in _answer_state(game_round).answers for game_round in game.rounds
    )
    assert game.phase == MusicQuizPhase.ANSWERING

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        await plugin.answer(player_ids["Bob"], "correct_1")
    assert game.players[player_ids["Bob"]].score == 1000


@pytest.mark.asyncio
async def test_expiry_unblocks_answer_completion_and_keeps_events_private() -> None:
    """Removing an inactive holdout reveals without leaking private presence state."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        await plugin.answer(player_ids["Alice"], "correct_0")

    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 200.0
    game.players[player_ids["Bob"]].last_seen = 100.0
    cast("MagicMock", plugin.signal_provider_event).reset_mock()

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await plugin._expire_inactive_players()

    assert game.phase == MusicQuizPhase.REVEAL
    assert player_ids["Bob"] not in game.players
    assert game.players[player_ids["Alice"]].score == 1000
    payload = cast("MagicMock", plugin.signal_provider_event).call_args.args[0]
    serialized = str(payload)
    assert payload["state"]["players"][0]["name"] == "Alice"
    assert "last_seen" not in serialized
    for player_id in player_ids.values():
        assert player_id not in serialized


@pytest.mark.asyncio
async def test_expiry_reveals_when_only_late_joiners_remain() -> None:
    """Reveal immediately when expiry leaves no player eligible for the current round."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        late_join = await plugin.join_game("Late")

    game = plugin._game
    assert game is not None
    late_player_id = late_join["player_id"]
    assert game.players[late_player_id].active_from_round == 1
    game.players[player_ids["Alice"]].last_seen = 100.0
    game.players[late_player_id].last_seen = 200.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await plugin._expire_inactive_players()

    assert set(game.players) == {late_player_id}
    assert game.phase == MusicQuizPhase.REVEAL


@pytest.mark.asyncio
async def test_expiry_unblocks_reveal_readiness() -> None:
    """Removing an inactive holdout advances when every remaining player is ready."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)
    await plugin.reveal()

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        await plugin.ready(player_ids["Alice"])

    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 200.0
    game.players[player_ids["Bob"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await plugin._expire_inactive_players()

    assert player_ids["Bob"] not in game.players
    assert game.phase == MusicQuizPhase.ANSWERING
    assert game.current_round_index == 1


@pytest.mark.asyncio
async def test_expiry_propagates_reveal_advance_failure() -> None:
    """Surface unexpected expiry-driven advance failures to task error handling."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin)
    await plugin.reveal()

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        await plugin.ready(player_ids["Alice"])

    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 200.0
    game.players[player_ids["Bob"]].last_seen = 100.0

    with (
        patch(
            "music_assistant.providers.music_quiz.time.time",
            return_value=160.0,
        ),
        patch.object(
            plugin,
            "_advance_from_reveal",
            new=AsyncMock(side_effect=RuntimeError("advance failed")),
        ),
        pytest.raises(RuntimeError, match="advance failed"),
    ):
        await plugin._expire_inactive_players()

    assert player_ids["Bob"] not in game.players


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [MusicQuizPhase.ANSWERING, MusicQuizPhase.REVEAL])
async def test_expiry_with_zero_remaining_players_does_not_auto_transition(
    phase: MusicQuizPhase,
) -> None:
    """Removing the final player leaves the current phase stable."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    if phase == MusicQuizPhase.REVEAL:
        await plugin.reveal()

    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await plugin._expire_inactive_players()

    assert game.players == {}
    assert game.phase == phase
    payload = cast("MagicMock", plugin.signal_provider_event).call_args.args[0]
    assert payload["state"]["players"] == []


@pytest.mark.asyncio
async def test_finished_game_stops_presence_expiry() -> None:
    """Finished standings and answer history remain intact without presence expiry."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(
        plugin,
        player_names=("Alice",),
        round_count=1,
    )
    game = plugin._game
    assert game is not None

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        await plugin.answer(player_ids["Alice"], "correct_0")
    await plugin.next_round()

    assert game.phase == MusicQuizPhase.FINISHED
    assert game.players[player_ids["Alice"]].score == 1000
    cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._presence_timer_id)
    game.players[player_ids["Alice"]].last_seen = 0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=10_000.0):
        await plugin._expire_inactive_players()

    assert player_ids["Alice"] in game.players
    assert player_ids["Alice"] in _answer_state(game.rounds[0]).answers


@pytest.mark.asyncio
async def test_reset_preserves_quiz_type_in_state_and_events() -> None:
    """Reset preserves the selected quiz type in returned and broadcast state."""
    plugin = _create_plugin()
    await _create_started_game(plugin)

    state = await plugin.reset()

    assert state["phase"] == "lobby"
    assert state["quiz_type"] == "guess_the_song"
    assert state["answer_type"] == "multiple_choice"
    payload = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]
    assert payload["state"]["quiz_type"] == "guess_the_song"
    assert payload["state"]["answer_type"] == "multiple_choice"


@pytest.mark.asyncio
async def test_replay_reset_schedules_and_serializes_authoritative_deadline() -> None:
    """Expose one 30-second replay deadline in host, public and personal state."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        host_state = await plugin.reset(auto_start=True)
        with patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ):
            personal_state = await plugin.get_player_state(player_ids["Alice"])
        game_info = await plugin.get_game_info()

    delay, _, scheduled_game, generation, deadline = _replay_timer_call(plugin)
    public_state = cast("MagicMock", plugin.signal_provider_event).call_args.args[0]["state"]
    assert delay == REPLAY_AUTO_START_SECONDS
    assert deadline == 100.0 + REPLAY_AUTO_START_SECONDS
    assert scheduled_game is game
    assert generation == plugin._game_generation
    assert game.auto_start_at == deadline
    assert host_state["auto_start_at"] == deadline
    assert public_state["auto_start_at"] == deadline
    assert personal_state["auto_start_at"] == deadline
    assert game_info is not None
    assert game_info["auto_start_at"] == deadline


@pytest.mark.asyncio
async def test_manual_or_inactive_replay_reset_does_not_schedule() -> None:
    """Keep reset manual by default and ignore stale player dictionary entries."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None
    player = game.players[player_ids["Alice"]]
    player.last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        manual_state = await plugin.reset()
    assert manual_state["auto_start_at"] is None
    assert not any(
        call.kwargs.get("task_id") == plugin._replay_auto_start_timer_id
        for call in cast("MagicMock", plugin.mass.call_later).call_args_list
    )

    cast("MagicMock", plugin.mass.call_later).reset_mock()
    player.last_seen = 40.0
    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        inactive_state = await plugin.reset(auto_start=True)

    assert player.player_id in game.players
    assert inactive_state["phase"] == "lobby"
    assert inactive_state["auto_start_at"] is None
    assert not any(
        call.kwargs.get("task_id") == plugin._replay_auto_start_timer_id
        for call in cast("MagicMock", plugin.mass.call_later).call_args_list
    )


@pytest.mark.asyncio
async def test_manual_start_cancels_replay_countdown_and_starts_once() -> None:
    """Let the host start immediately without a later timer duplicating the round."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        await plugin.reset(auto_start=True)
    _, callback, scheduled_game, generation, deadline = _replay_timer_call(plugin)
    cast("AsyncMock", plugin._play_track).reset_mock()
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()

    state = await plugin.start_game()

    assert state["phase"] == "answering"
    assert state["auto_start_at"] is None
    assert game.auto_start_at is None
    assert len(game.rounds) == 1
    cast("AsyncMock", plugin._play_track).assert_awaited_once()
    cast("MagicMock", plugin.mass.cancel_timer).assert_called_once_with(
        plugin._replay_auto_start_timer_id
    )
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(
        plugin._replay_auto_start_timer_id
    )

    await callback(scheduled_game, generation, deadline)

    assert len(game.rounds) == 1
    cast("AsyncMock", plugin._play_track).assert_awaited_once()


@pytest.mark.asyncio
async def test_replay_deadline_starts_once_in_system_context() -> None:
    """Run timed preparation and playback without inheriting the host request."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0
    prepare_contexts: list[tuple[User | None, User | None]] = []
    playback_contexts: list[tuple[User | None, User | None]] = []

    async def _prepare_round(
        round_index: int,
        _previous_rounds: list[MusicQuizRound],
    ) -> MusicQuizRound:
        prepare_contexts.append((current_user.get(), impersonated_user.get()))
        return _make_round(round_index)

    async def _play_track(_track_uri: str) -> None:
        playback_contexts.append((current_user.get(), impersonated_user.get()))

    prepare_round = AsyncMock(side_effect=_prepare_round)
    plugin._play_track = AsyncMock(side_effect=_play_track)  # type: ignore[method-assign]
    requesting_user = cast("User", _guest_user())
    requesting_impersonation = cast("User", _guest_user())
    current_user_token = current_user.set(requesting_user)
    impersonated_user_token = impersonated_user.set(requesting_impersonation)
    try:
        with (
            patch(
                "music_assistant.providers.music_quiz.quiz_types.guess_the_song."
                "GuessTheSongQuizType.prepare_round",
                new=prepare_round,
            ),
            patch("music_assistant.providers.music_quiz.time.time", return_value=100.0),
        ):
            await plugin.reset(auto_start=True)
        _, callback, scheduled_game, generation, deadline = _replay_timer_call(plugin)

        with patch("music_assistant.providers.music_quiz.time.time", return_value=130.0):
            await callback(scheduled_game, generation, deadline)
            await callback(scheduled_game, generation, deadline)

        assert current_user.get() is requesting_user
        assert impersonated_user.get() is requesting_impersonation
    finally:
        impersonated_user.reset(impersonated_user_token)
        current_user.reset(current_user_token)

    prepare_round.assert_awaited_once_with(0, [])
    assert prepare_contexts == [(None, None)]
    assert playback_contexts == [(None, None)]
    assert game.phase == MusicQuizPhase.ANSWERING
    assert len(game.rounds) == 1
    assert game.auto_start_at is None


@pytest.mark.asyncio
async def test_all_players_expiring_cancels_replay_without_rescheduling_on_join() -> None:
    """Cancel an empty replay lobby and require another explicit reset to rearm it."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=140.0):
        await plugin.reset(auto_start=True)
    _, replay_callback, scheduled_game, generation, deadline = _replay_timer_call(plugin)
    presence_delay, presence_callback = _presence_timer_call(plugin)
    assert presence_delay == 20.0
    signal = cast("MagicMock", plugin.signal_provider_event)
    signal.reset_mock()
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()

    with patch("music_assistant.providers.music_quiz.time.time", return_value=160.0):
        await presence_callback()

    assert game.players == {}
    assert game.phase == MusicQuizPhase.LOBBY
    assert game.auto_start_at is None
    cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._replay_auto_start_timer_id)
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(
        plugin._replay_auto_start_timer_id
    )
    cancelled_state = signal.call_args.args[0]["state"]
    assert cancelled_state["auto_start_at"] is None
    assert cancelled_state["players"] == []

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=_guest_user(),
        ),
        patch("music_assistant.providers.music_quiz.time.time", return_value=165.0),
    ):
        joined = await plugin.join_game("Bob")

    assert joined["state"]["auto_start_at"] is None
    assert (
        sum(
            call.kwargs.get("task_id") == plugin._replay_auto_start_timer_id
            for call in cast("MagicMock", plugin.mass.call_later).call_args_list
        )
        == 1
    )

    with patch("music_assistant.providers.music_quiz.time.time", return_value=170.0):
        await replay_callback(scheduled_game, generation, deadline)
    assert game.phase == MusicQuizPhase.LOBBY
    assert game.rounds == []


@pytest.mark.asyncio
async def test_stale_replay_generation_cannot_reuse_identical_deadline() -> None:
    """Reject an old reset callback even when a new reset chose the same epoch deadline."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        await plugin.reset(auto_start=True)
        old_timer = _replay_timer_call(plugin)
        await plugin.reset(auto_start=True)
        current_timer = _replay_timer_call(plugin)

    assert old_timer[2] is current_timer[2] is game
    assert old_timer[3] != current_timer[3]
    assert old_timer[4] == current_timer[4]
    cast("AsyncMock", plugin._play_track).reset_mock()

    with patch("music_assistant.providers.music_quiz.time.time", return_value=130.0):
        await old_timer[1](*old_timer[2:])
        assert _phase(plugin) == MusicQuizPhase.LOBBY
        assert game.rounds == []
        await current_timer[1](*current_timer[2:])

    assert _phase(plugin) == MusicQuizPhase.ANSWERING
    assert len(game.rounds) == 1
    cast("AsyncMock", plugin._play_track).assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_action", ["reset", "delete", "unload", "replace"])
async def test_stale_replay_callbacks_ignore_lifecycle_changes(lifecycle_action: str) -> None:
    """Keep callbacks harmless after every lifecycle path that invalidates a game."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        await plugin.reset(auto_start=True)
    timer = _replay_timer_call(plugin)
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()

    if lifecycle_action == "reset":
        await plugin.reset()
    elif lifecycle_action == "delete":
        await plugin.delete_game()
    elif lifecycle_action == "unload":
        await plugin.unload()
    else:
        await plugin.create_game(source_uris=["library://playlist/replacement"])

    cast("MagicMock", plugin.mass.cancel_task).assert_any_call(plugin._replay_auto_start_timer_id)
    cast("AsyncMock", plugin._play_track).reset_mock()
    with patch("music_assistant.providers.music_quiz.time.time", return_value=130.0):
        await timer[1](*timer[2:])

    cast("AsyncMock", plugin._play_track).assert_not_awaited()
    if plugin._game is not None:
        assert plugin._game.phase == MusicQuizPhase.LOBBY
        assert plugin._game.rounds == []
        assert plugin._game.auto_start_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["preparation", "playback"])
async def test_failed_timed_start_remains_retryable(failure_stage: str) -> None:
    """Clear a failed timed start and let the host retry the unchanged lobby."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",), round_count=1)
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0

    with patch("music_assistant.providers.music_quiz.time.time", return_value=100.0):
        await plugin.reset(auto_start=True)
    timer = _replay_timer_call(plugin)
    plugin._cancel_next_round_task()
    quiz_type = plugin._quiz_type
    assert quiz_type is not None
    prepared_round = _make_round(0)
    prepare_round = AsyncMock()
    quiz_type.prepare_round = prepare_round  # type: ignore[method-assign]
    if failure_stage == "preparation":
        failure = InvalidDataError("Round preparation failed")
        prepare_round.side_effect = failure
    else:
        failure = MusicQuizNoPlaybackTargetError("Playback unavailable")
        prepare_round.return_value = prepared_round
        cast("AsyncMock", plugin._play_track).side_effect = failure
    signal = cast("MagicMock", plugin.signal_provider_event)
    signal.reset_mock()
    cast("MagicMock", plugin.logger.error).reset_mock()

    with patch("music_assistant.providers.music_quiz.time.time", return_value=130.0):
        await timer[1](*timer[2:])

    assert game.phase == MusicQuizPhase.LOBBY
    assert game.rounds == []
    assert game.auto_start_at is None
    assert signal.call_args.args[0]["state"]["auto_start_at"] is None
    cast("MagicMock", plugin.logger.error).assert_called_once_with(
        "Could not automatically start Music Quiz replay: %s",
        failure,
        exc_info=failure,
    )

    recovered_round = _make_round(0)
    prepare_round.side_effect = None
    prepare_round.return_value = recovered_round
    cast("AsyncMock", plugin._play_track).side_effect = None

    state = await plugin.start_game()

    assert state["phase"] == "answering"
    assert game.rounds == [recovered_round]
    assert game.auto_start_at is None


@pytest.mark.asyncio
async def test_reset_restarts_presence_timer_without_refreshing_players() -> None:
    """Reset replaces presence work while preserving each player's last activity."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None
    game.players[player_ids["Alice"]].last_seen = 100.0
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()
    cast("MagicMock", plugin.mass.call_later).reset_mock()

    with patch("music_assistant.providers.music_quiz.time.time", return_value=120.0):
        await plugin.reset()

    cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._presence_timer_id)
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(plugin._presence_timer_id)
    delay, _ = _presence_timer_call(plugin)
    assert delay == 40.0
    assert game.players[player_ids["Alice"]].last_seen == 100.0


@pytest.mark.asyncio
async def test_answer_deadline_timer_reveals_round() -> None:
    """The scheduled answering deadline reveals the round, once."""
    plugin = _create_plugin()
    await _create_started_game(plugin)
    game = plugin._game
    assert game is not None

    delay, target, round_index = cast("MagicMock", plugin.mass.call_later).call_args[0]
    assert delay == 30  # configured answer_duration capped by track duration
    await target(round_index)
    assert game.phase == MusicQuizPhase.REVEAL

    # a stale timer for an old round is a no-op
    await target(round_index)
    assert game.phase == MusicQuizPhase.REVEAL


@pytest.mark.asyncio
async def test_reveal_schedules_track_end_advance() -> None:
    """After reveal the game auto-advances when the round track finished."""
    plugin = _create_plugin()
    await _create_started_game(plugin)
    game = plugin._game
    assert game is not None

    await plugin.reveal()
    delay, target, round_index = cast("MagicMock", plugin.mass.call_later).call_args[0]
    assert delay > 0
    await target(round_index)
    assert game.phase == MusicQuizPhase.ANSWERING
    assert game.current_round_index == 1


@pytest.mark.asyncio
async def test_join_during_round_activates_player_next_round() -> None:
    """A player joining mid-round only participates from the next round."""
    plugin = _create_plugin()
    player_ids = await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        result = await plugin.join_game("Late")
        assert result["state"]["you"]["active_from_round"] == 1
    public_state = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]["state"]
    late_player = next(player for player in public_state["players"] if player["name"] == "Late")
    assert late_player == {
        "name": "Late",
        "score": 0,
        "ready": False,
        "active_from_round": 1,
        "answered": False,
    }
    for player_id in (*player_ids.values(), result["player_id"]):
        assert player_id not in str(public_state)


@pytest.mark.asyncio
async def test_create_game_rejected_while_game_active() -> None:
    """A running game must be finished or deleted before creating a new one."""
    plugin = _create_plugin()
    await _create_started_game(plugin)

    with pytest.raises(MusicQuizGameActiveError):
        await plugin.create_game(source_uris=["library://playlist/2"])


@pytest.mark.asyncio
async def test_create_game_replaces_finished_game() -> None:
    """Creating a new game over a finished one starts fresh."""
    plugin = _create_plugin()
    await _create_started_game(plugin, round_count=1)
    game = plugin._game
    assert game is not None
    await plugin.reveal()
    await plugin.next_round()
    assert game.phase == MusicQuizPhase.FINISHED

    cast("MagicMock", plugin.mass.cancel_task).reset_mock()
    result = await plugin.create_game(source_uris=["library://playlist/2"])
    assert result["phase"] == "lobby"
    assert plugin._game is not game
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(plugin._presence_timer_id)


@pytest.mark.asyncio
async def test_create_game_cancels_previous_background_work_after_initialize() -> None:
    """Initialize a replacement before stopping the current game's background work."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    game = plugin._game
    assert game is not None
    game_state = game.to_dict()
    quiz_strategy = plugin._quiz_type
    answer_strategy = plugin._answer_type
    plugin._cancel_next_round_task()
    pending_task = MagicMock()
    plugin._next_round_task = pending_task
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()

    async def _initialize(_strategy: Any) -> None:
        assert plugin._game is game
        assert game.to_dict() == game_state
        assert plugin._quiz_type is quiz_strategy
        assert plugin._answer_type is answer_strategy
        assert plugin._next_round_task is pending_task
        pending_task.cancel.assert_not_called()
        cast("MagicMock", plugin.mass.cancel_timer).assert_not_called()
        cast("MagicMock", plugin.mass.cancel_task).assert_not_called()

    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=_initialize,
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.prepare_round",
            new=AsyncMock(
                side_effect=lambda round_index, previous: _make_hitster_round(round_index, previous)
            ),
        ),
    ):
        await plugin.create_game(
            quiz_type="hitster",
            source_uris=["library://playlist/2"],
        )

    assert plugin._game is not game
    pending_task.cancel.assert_called_once()
    cancel_timer = cast("MagicMock", plugin.mass.cancel_timer)
    cancel_timer.assert_any_call(plugin._reveal_timer_id)
    cancel_timer.assert_any_call(plugin._advance_timer_id)
    cancel_timer.assert_any_call(plugin._presence_timer_id)
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(plugin._presence_timer_id)


@pytest.mark.asyncio
async def test_create_game_source_failure_preserves_existing_lobby() -> None:
    """Keep an existing lobby when replacement source preparation fails."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    game = plugin._game
    assert game is not None
    game_state = game.to_dict()
    quiz_strategy = plugin._quiz_type
    answer_strategy = plugin._answer_type
    plugin._cancel_next_round_task()
    pending_task = MagicMock()
    plugin._next_round_task = pending_task
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()
    cast("MagicMock", plugin.mass.call_later).reset_mock()

    resolve_sources = AsyncMock(side_effect=InvalidDataError("Source preparation failed"))
    with (
        patch.object(plugin, "_resolve_sources", new=resolve_sources),
        pytest.raises(InvalidDataError, match="Source preparation failed"),
    ):
        await plugin.create_game(source_uris=["library://playlist/2"])

    assert plugin._game is game
    assert game.to_dict() == game_state
    assert plugin._quiz_type is quiz_strategy
    assert plugin._answer_type is answer_strategy
    assert plugin._next_round_task is pending_task
    pending_task.cancel.assert_not_called()
    cast("MagicMock", plugin.mass.cancel_timer).assert_not_called()
    cast("MagicMock", plugin.mass.cancel_task).assert_not_called()
    cast("MagicMock", plugin.mass.call_later).assert_not_called()


@pytest.mark.asyncio
async def test_create_game_initialize_failure_preserves_existing_finished_game() -> None:
    """Keep an existing finished game when replacement initialization fails."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])
    game = plugin._game
    assert game is not None
    game.phase = MusicQuizPhase.FINISHED
    game_state = game.to_dict()
    quiz_strategy = plugin._quiz_type
    answer_strategy = plugin._answer_type
    plugin._cancel_next_round_task()
    pending_task = MagicMock()
    plugin._next_round_task = pending_task
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()
    cast("MagicMock", plugin.mass.call_later).reset_mock()

    initialize = AsyncMock(side_effect=InvalidDataError("Initialization failed"))
    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.hitster.HitsterQuizType.initialize",
            new=initialize,
        ),
        pytest.raises(InvalidDataError, match="Initialization failed"),
    ):
        await plugin.create_game(
            quiz_type="hitster",
            source_uris=["library://playlist/2"],
        )

    initialize.assert_awaited_once()
    assert plugin._game is game
    assert game.to_dict() == game_state
    assert plugin._quiz_type is quiz_strategy
    assert plugin._answer_type is answer_strategy
    assert plugin._next_round_task is pending_task
    pending_task.cancel.assert_not_called()
    cast("MagicMock", plugin.mass.cancel_timer).assert_not_called()
    cast("MagicMock", plugin.mass.cancel_task).assert_not_called()
    cast("MagicMock", plugin.mass.call_later).assert_not_called()


@pytest.mark.asyncio
async def test_get_game_serializes_with_create_and_delete() -> None:
    """The host getter sees complete states while create and delete are in progress."""
    plugin = _create_plugin()
    async with plugin._game_lock:
        create_task = asyncio.create_task(plugin.create_game(source_uris=["library://playlist/1"]))
        await asyncio.sleep(0)
        get_task = asyncio.create_task(plugin.get_game())
    created, fetched = await asyncio.gather(create_task, get_task)
    assert fetched == created

    async with plugin._game_lock:
        get_task = asyncio.create_task(plugin.get_game())
        await asyncio.sleep(0)
        delete_task = asyncio.create_task(plugin.delete_game())
    fetched, _ = await asyncio.gather(get_task, delete_task)
    assert fetched == created
    assert await plugin.get_game() is None


@pytest.mark.asyncio
async def test_delete_game_signals_removal() -> None:
    """Deleting the game stops playback, closes the session and broadcasts game_removed."""
    plugin = _create_plugin()
    await _create_started_game(plugin)
    session = MagicMock()
    session.close = AsyncMock()
    plugin._playback_session = session
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()

    await plugin.delete_game()
    assert plugin._game is None
    assert plugin._quiz_type is None
    assert plugin._answer_type is None
    cast("AsyncMock", plugin._stop_playback).assert_awaited()
    session.close.assert_awaited_once()
    cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._presence_timer_id)
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(plugin._presence_timer_id)
    payload = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]
    assert payload == {"event": "game_removed"}
    assert await plugin.get_game() is None


def test_cancel_prefetch_task_consumes_exception() -> None:
    """Cancelling a prefetch task retrieves its exception so asyncio does not warn."""
    plugin = _create_plugin()
    task = MagicMock()
    task.cancelled.return_value = False
    plugin._next_round_task = task

    plugin._cancel_next_round_task()

    task.cancel.assert_called_once()
    task.add_done_callback.assert_called_once()
    remaining: Any = plugin._next_round_task
    assert remaining is None
    # the registered callback retrieves the exception from the settled task
    callback = task.add_done_callback.call_args.args[0]
    callback(task)
    task.exception.assert_called_once()


@pytest.mark.asyncio
async def test_config_validation() -> None:
    """Reject invalid game configurations."""
    plugin = _create_plugin()
    with pytest.raises(InvalidDataError, match="source"):
        await plugin.create_game(source_uris=[])
    with pytest.raises(InvalidDataError, match="round"):
        await plugin.create_game(round_count=0, source_uris=["library://playlist/1"])
    with pytest.raises(InvalidDataError, match=r"(?i)suggestion"):
        await plugin.create_game(suggestion_count=1, source_uris=["library://playlist/1"])
    with pytest.raises(InvalidDataError, match="duration"):
        await plugin.create_game(answer_duration=0, source_uris=["library://playlist/1"])
    with pytest.raises(InvalidDataError, match="quiz type"):
        await plugin.create_game(quiz_type="unsupported", source_uris=["library://playlist/1"])


@pytest.mark.asyncio
async def test_playback_session_remote_mode() -> None:
    """Remote mode creates a virtual-player session keyed to the instance."""
    plugin = _create_plugin(mode="remote")
    plugin._game = _fake_game()
    cast("MagicMock", plugin.mass).players.get_player.return_value = None
    session = MagicMock()
    with patch(
        "music_assistant.providers.music_quiz.SharedPlaybackSession.create_remote",
        new=AsyncMock(return_value=session),
    ) as create_remote:
        assert await plugin._get_playback_session() is session
    create_remote.assert_awaited_once_with(
        plugin.mass,
        owner_instance_id=INSTANCE_ID,
        display_name="Music Quiz",
        session_id=INSTANCE_ID,
    )


@pytest.mark.asyncio
async def test_playback_session_recreated_when_player_vanished() -> None:
    """A session whose player disappeared (e.g. sendspin reload) is recreated."""
    plugin = _create_plugin(mode="remote")
    plugin._game = _fake_game()
    stale_session = MagicMock()
    stale_session.player_id = "gone"
    stale_session.close = AsyncMock()
    plugin._playback_session = stale_session
    cast("MagicMock", plugin.mass).players.get_player.return_value = None
    fresh_session = MagicMock()
    with patch(
        "music_assistant.providers.music_quiz.SharedPlaybackSession.create_remote",
        new=AsyncMock(return_value=fresh_session),
    ):
        assert await plugin._get_playback_session() is fresh_session
    stale_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_playback_session_venue_mode() -> None:
    """Venue mode creates a session on the configured player."""
    plugin = _create_plugin(mode="venue", player="venue_player")
    plugin._game = _fake_game()
    session = MagicMock()
    with patch(
        "music_assistant.providers.music_quiz.SharedPlaybackSession.create_venue",
        new=AsyncMock(return_value=session),
    ) as create_venue:
        assert await plugin._get_playback_session() is session
    create_venue.assert_awaited_once_with(plugin.mass, "venue_player")


@pytest.mark.asyncio
async def test_playback_session_venue_mode_auto_prefers_playing_player() -> None:
    """Venue mode with the auto player picks a currently playing player."""
    plugin = _create_plugin(mode="venue", player="__auto__")
    plugin._game = _fake_game()
    paused = SimpleNamespace(player_id="paused_player", playback_state=PlaybackState.PAUSED)
    playing = SimpleNamespace(player_id="playing_player", playback_state=PlaybackState.PLAYING)
    cast("MagicMock", plugin.mass).players.all_players.return_value = [paused, playing]
    session = MagicMock()
    with patch(
        "music_assistant.providers.music_quiz.SharedPlaybackSession.create_venue",
        new=AsyncMock(return_value=session),
    ) as create_venue:
        assert await plugin._get_playback_session() is session
    create_venue.assert_awaited_once_with(plugin.mass, "playing_player")


@pytest.mark.asyncio
async def test_playback_session_venue_mode_auto_without_players() -> None:
    """Venue mode with the auto player yields no session when no player is available."""
    plugin = _create_plugin(mode="venue", player="__auto__")
    plugin._game = _fake_game()
    cast("MagicMock", plugin.mass).players.all_players.return_value = []
    with patch(
        "music_assistant.providers.music_quiz.SharedPlaybackSession.create_venue",
        new=AsyncMock(),
    ) as create_venue:
        assert await plugin._get_playback_session() is None
    create_venue.assert_not_awaited()


@pytest.mark.asyncio
async def test_playback_session_requires_active_game() -> None:
    """No playback session is created without an active game (guards the listen-in race)."""
    plugin = _create_plugin(mode="remote")
    plugin._game = None
    with patch(
        "music_assistant.providers.music_quiz.SharedPlaybackSession.create_remote",
        new=AsyncMock(),
    ) as create_remote:
        assert await plugin._get_playback_session() is None
    create_remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_listen_in_joins_session_under_playback_lock() -> None:
    """listen_in joins the guest while holding the playback lock so teardown cannot race it."""
    plugin = _create_plugin(mode="remote")
    plugin._game = _fake_game()
    session = MagicMock()

    async def _assert_locked(_web_player_id: str) -> None:
        assert plugin._playback_lock.locked()

    session.add_guest_listener = AsyncMock(side_effect=_assert_locked)
    plugin._playback_session = session
    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=SimpleNamespace(username=MUSIC_QUIZ_GUEST_USER, role=UserRole.GUEST),
    ):
        await plugin.listen_in("web-1")
    session.add_guest_listener.assert_awaited_once_with("web-1")


@pytest.mark.asyncio
async def test_unload_cleans_up() -> None:
    """Unload unregisters commands, closes the session and revokes guest access."""
    plugin = _create_plugin()
    unregister = MagicMock()
    plugin._unregister_handles = [unregister]
    session = MagicMock()
    session.close = AsyncMock()
    plugin._playback_session = session
    cast("MagicMock", plugin.mass.cancel_timer).reset_mock()
    cast("MagicMock", plugin.mass.cancel_task).reset_mock()

    with patch(
        "music_assistant.helpers.guest_access.revoke_guest_access",
        new=AsyncMock(return_value=(0, 0)),
    ) as revoke:
        await plugin.unload(is_removed=True)

    unregister.assert_called_once()
    session.close.assert_awaited_once()
    assert plugin._game is None
    cast("MagicMock", plugin.mass.cancel_timer).assert_any_call(plugin._presence_timer_id)
    cast("MagicMock", plugin.mass.cancel_task).assert_called_once_with(plugin._presence_timer_id)
    revoke.assert_awaited_once_with(plugin.mass, MUSIC_QUIZ_GUEST_USER)


@pytest.mark.asyncio
async def test_create_game_rejects_invalid_difficulty() -> None:
    """An unknown difficulty is rejected before a game is created."""
    plugin = _create_plugin()
    with pytest.raises(InvalidDataError, match="difficulty"):
        await plugin.create_game(source_uris=["library://playlist/1"], difficulty="impossible")
    assert plugin._game is None


@pytest.mark.asyncio
async def test_get_config_entries_includes_ai_distractor_toggle() -> None:
    """The provider exposes an off-by-default AI distractor toggle."""
    mass = MagicMock()
    mass.players.all_players.return_value = []

    entries = await get_config_entries(mass)

    ai_entry = next(entry for entry in entries if entry.key == "use_ai_distractors")
    assert ai_entry.type == ConfigEntryType.BOOLEAN
    assert ai_entry.default_value is False
    assert ai_entry.required is False
