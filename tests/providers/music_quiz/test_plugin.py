"""Tests for the Music Quiz plugin provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz import (
    MUSIC_QUIZ_GUEST_USER,
    MusicQuizPlugin,
)
from music_assistant.providers.music_quiz.errors import (
    MusicQuizGameActiveError,
    MusicQuizNoGameError,
    MusicQuizUnknownPlayerError,
)
from music_assistant.providers.music_quiz.models import (
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizRound,
    MusicQuizSuggestion,
)

INSTANCE_ID = "music_quiz--test"

HOST_COMMANDS = (
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
        MusicQuizSuggestion(
            suggestion_id=f"correct_{round_index}",
            label=f"Artist - Correct {round_index}",
            is_correct=True,
        )
    ] + [
        MusicQuizSuggestion(
            suggestion_id=f"wrong_{round_index}_{index}",
            label=f"Artist - Wrong {round_index}.{index}",
        )
        for index in range(1, suggestion_count)
    ]
    return MusicQuizRound(
        round_index=round_index,
        track_uri=f"library://track/{round_index}",
        answer_label=f"Artist - Correct {round_index}",
        suggestions=suggestions,
        image_url=f"https://img/{round_index}",
        duration=180.0,
    )


def _create_plugin(mode: str = "venue", player: str | None = "venue_player") -> MusicQuizPlugin:
    """Create a minimally configured Music Quiz plugin for unit tests."""
    plugin = MusicQuizPlugin.__new__(MusicQuizPlugin)
    plugin.mass = MagicMock()
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin.config.instance_id = INSTANCE_ID
    plugin.config.get_value.side_effect = {"mode": mode, "player": player}.__getitem__
    plugin._game = None
    plugin._quiz_type = None
    plugin._game_lock = asyncio.Lock()
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
    source_item.media_type.value = "playlist"
    plugin.mass.music.get_item_by_uri = AsyncMock(return_value=source_item)
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
    return SimpleNamespace(username=MUSIC_QUIZ_GUEST_USER)


def _phase(plugin: MusicQuizPlugin) -> MusicQuizPhase:
    """Return the current game phase."""
    game = plugin._game
    assert game is not None
    return game.phase


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


@pytest.fixture(autouse=True)
def _deterministic_rounds() -> Any:
    """Patch round preparation to deterministic rounds without music lookups."""
    with patch(
        "music_assistant.providers.music_quiz.GuessTheSongQuizType.prepare_round",
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


@pytest.mark.asyncio
async def test_guest_commands_reject_non_guest_users() -> None:
    """Guest game commands are only available to the Music Quiz guest user."""
    plugin = _create_plugin()
    await plugin.create_game(source_uris=["library://playlist/1"])

    with (
        patch(
            "music_assistant.providers.music_quiz.get_current_user",
            return_value=SimpleNamespace(username="admin"),
        ),
        pytest.raises(InvalidDataError, match="guests"),
    ):
        await plugin.join_game("Mallory")

    with (
        patch("music_assistant.providers.music_quiz.get_current_user", return_value=None),
        pytest.raises(InvalidDataError, match="guests"),
    ):
        await plugin.answer("some_player", "some_suggestion")


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
    assert state["phase"] == "answering"
    assert state["mode"] == "venue"
    current_round = state["current_round"]
    assert set(current_round) == {
        "round_index",
        "started_at",
        "deadline",
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

    # after the reveal the same payload exposes the answer
    await plugin.reveal()
    state = signal.call_args[0][0]["state"]
    current_round = state["current_round"]
    assert current_round["correct_suggestion_id"] == "correct_0"
    assert current_round["track_uri"] == "library://track/0"
    assert current_round["answer_label"] == "Artist - Correct 0"


@pytest.mark.asyncio
async def test_game_info_exposes_playback_mode() -> None:
    """The join-screen info includes the configured playback mode."""
    plugin = _create_plugin(mode="remote", player=None)
    assert await plugin.get_game_info() is None
    await plugin.create_game(source_uris=["library://playlist/1"], name="Test Quiz")

    info = await plugin.get_game_info()
    assert info == {
        "name": "Test Quiz",
        "phase": "lobby",
        "mode": "remote",
        "player_count": 0,
        "round_count": 5,
    }


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
    await _create_started_game(plugin, player_names=("Alice",))
    game = plugin._game
    assert game is not None

    with patch(
        "music_assistant.providers.music_quiz.get_current_user",
        return_value=_guest_user(),
    ):
        result = await plugin.join_game("Late")
        assert result["state"]["you"]["active_from_round"] == 1


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

    result = await plugin.create_game(source_uris=["library://playlist/2"])
    assert result["phase"] == "lobby"
    assert plugin._game is not game


@pytest.mark.asyncio
async def test_delete_game_signals_removal() -> None:
    """Deleting the game stops playback, closes the session and broadcasts game_removed."""
    plugin = _create_plugin()
    await _create_started_game(plugin)
    session = MagicMock()
    session.close = AsyncMock()
    plugin._playback_session = session

    await plugin.delete_game()
    assert plugin._game is None
    cast("AsyncMock", plugin._stop_playback).assert_awaited()
    session.close.assert_awaited_once()
    payload = cast("MagicMock", plugin.signal_provider_event).call_args[0][0]
    assert payload == {"event": "game_removed"}
    with pytest.raises(MusicQuizNoGameError):
        await plugin.get_game()


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
        await plugin.create_game(quiz_type="trivia", source_uris=["library://playlist/1"])


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
        return_value=SimpleNamespace(username=MUSIC_QUIZ_GUEST_USER),
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

    with patch(
        "music_assistant.helpers.guest_access.revoke_guest_access",
        new=AsyncMock(return_value=(0, 0)),
    ) as revoke:
        await plugin.unload(is_removed=True)

    unregister.assert_called_once()
    session.close.assert_awaited_once()
    assert plugin._game is None
    revoke.assert_awaited_once_with(plugin.mass, MUSIC_QUIZ_GUEST_USER)
