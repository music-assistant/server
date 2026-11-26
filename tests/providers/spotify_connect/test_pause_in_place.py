"""Tests for pausing the player in place on a Spotify Connect pause."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import (
    PlayerFeature,
    ProviderType,
    SourceControl,
)
from music_assistant_models.streamdetails import StreamMetadata

import music_assistant.providers.spotify_connect.provider as provider_mod
from music_assistant.models.media_capabilities import SourcePause
from music_assistant.providers.spotify_connect import SpotifyConnectProvider
from music_assistant.providers.spotify_connect.models import BackendEvent, BackendEventType
from music_assistant.providers.spotify_connect.provider import (
    _PlayerDaemon,
)

_PLAYER_ID = "player1"


class _Harness:
    """A provider with one daemon mid-playback on a pausable player."""

    def __init__(
        self,
        player_features: set[PlayerFeature] | None = None,
        stream_ends_on_pause: bool = False,
    ) -> None:
        """
        Build the provider, its daemon and the consuming player.

        :param player_features: Features the consuming player reports.
        :param stream_ends_on_pause: Whether the backend's stream ends on pause.
        """
        self.tasks: list[asyncio.Task[Any]] = []
        self.stopped: list[str] = []
        # the stopped players that show as paused on the source meanwhile
        self.shown_paused: list[str] = []
        self.play_media_scheduled = 0

        provider = object.__new__(SpotifyConnectProvider)
        provider.mass = MagicMock()
        provider.logger = MagicMock()
        provider.config = ProviderConfig(
            values={},
            type=ProviderType.PLUGIN,
            domain="spotify_connect",
            instance_id="spotify_connect--test",
            name="Spotify Connect",
        )
        mass = cast("Any", provider.mass)
        mass.create_task = self._create_task
        mass.loop.time = MagicMock(return_value=0.0)

        self.player = MagicMock()
        self.player.supported_features = (
            {PlayerFeature.PAUSE} if player_features is None else player_features
        )
        self.player.pause = AsyncMock()
        self.player.play = AsyncMock()
        # the player mock doubles as the output protocol that renders its audio
        self.player.resolve_output_protocol = MagicMock(return_value=self.player)
        mass.players.get_player = MagicMock(return_value=self.player)

        daemon = _PlayerDaemon(
            player_id=_PLAYER_ID,
            safe_player_id=_PLAYER_ID,
            publish_name="Test Speaker",
            stream_metadata=StreamMetadata(title="Spotify Connect | Test Speaker"),
        )
        daemon.backend = MagicMock()
        daemon.backend.stream_ends_on_pause = stream_ends_on_pause
        daemon.active_player_id = _PLAYER_ID
        daemon.in_use_by_player = _PLAYER_ID
        daemon.spotify_session_active = True
        daemon.playing = True
        provider._daemons = {_PLAYER_ID: daemon}

        provider._schedule_pause_stop = self._record_stop  # type: ignore[method-assign]
        provider._schedule_play_media = self._record_play_media  # type: ignore[method-assign]
        self.provider = provider
        self.daemon = daemon

    def _create_task(self, coro: Any, **_kwargs: Any) -> asyncio.Task[Any] | MagicMock:
        if not asyncio.iscoroutine(coro):
            return MagicMock()  # a mocked controller call (e.g. deselect_source)
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def _record_stop(self, daemon: _PlayerDaemon, player_id: str, *, paused: bool = False) -> None:
        del daemon
        self.stopped.append(player_id)
        if paused:
            self.shown_paused.append(player_id)

    def _record_play_media(self, daemon: _PlayerDaemon) -> None:
        del daemon
        self.play_media_scheduled += 1

    async def event(self, event_type: BackendEventType) -> None:
        """
        Feed one backend event and let the tasks it spawned settle.

        :param event_type: The backend event to dispatch.
        """
        await self.provider._handle_backend_event(self.daemon, BackendEvent(type=event_type))
        await self.settle()

    async def settle(self) -> None:
        """Run every spawned task that is not waiting on a timeout."""
        for _ in range(5):
            await asyncio.sleep(0)

    def cancel_tasks(self) -> None:
        """Cancel any task still pending (e.g. a release timeout)."""
        for task in self.tasks:
            task.cancel()


@pytest.fixture
def harness() -> Any:
    """Return a factory for harnesses that cleans up their pending tasks."""
    created: list[_Harness] = []

    def _factory(**kwargs: Any) -> _Harness:
        instance = _Harness(**kwargs)
        created.append(instance)
        return instance

    yield _factory
    for instance in created:
        instance.cancel_tasks()


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shorten the grace before a held pause reaches the player."""
    monkeypatch.setattr(provider_mod, "PAUSE_OUTPUT_GRACE_S", 0.01)
    return 0.05


async def test_pause_holds_the_stream_without_touching_the_player(harness: Any) -> None:
    """Within the grace a Spotify pause only holds the stream: the player plays on."""
    h = harness()

    await h.event(BackendEventType.PAUSED)

    h.player.pause.assert_not_awaited()
    assert h.stopped == []
    assert h.daemon.paused_in_place_player == _PLAYER_ID
    assert h.daemon.pause_output_task is not None


async def test_a_stream_that_ends_on_pause_is_not_held(harness: Any) -> None:
    """A backend whose stream ends on pause has nothing to hold: its player shows paused."""
    h = harness(stream_ends_on_pause=True)

    await h.event(BackendEventType.PAUSED)

    assert h.stopped == [_PLAYER_ID]
    assert h.shown_paused == [_PLAYER_ID]
    assert h.daemon.paused_in_place_player is None


async def test_a_spotify_stop_still_stops_the_player(harness: Any) -> None:
    """Only a pause keeps the player connected; a stop releases it."""
    h = harness()

    await h.event(BackendEventType.STOPPED)

    assert h.stopped == [_PLAYER_ID]
    assert h.shown_paused == []
    assert h.daemon.paused_in_place_player is None


async def test_resume_releases_the_hold(harness: Any) -> None:
    """A Spotify play after a pause releases the hold and cancels its pending pause."""
    h = harness()
    await h.event(BackendEventType.PAUSED)
    timeout_task = h.daemon.pause_output_task

    await h.event(BackendEventType.PLAYING)

    h.player.pause.assert_not_awaited()
    h.player.play.assert_not_awaited()
    assert h.daemon.paused_in_place_player is None
    assert h.daemon.pause_output_task is None
    assert timeout_task is not None
    assert timeout_task.cancelled()
    assert h.stopped == []


async def test_a_pause_past_the_grace_pauses_the_player(harness: Any, short_grace: float) -> None:
    """A pause that outlasts a track change pauses the player in place."""
    h = harness()

    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)

    h.player.pause.assert_awaited_once()
    assert h.daemon.output_paused is True
    assert h.daemon.paused_in_place_player == _PLAYER_ID
    assert h.stopped == []


async def test_a_pause_requested_on_the_player_pauses_it_straight_away(harness: Any) -> None:
    """A pause requested through Music Assistant cannot be a track change: no grace."""
    h = harness()
    h.daemon.backend.pause = AsyncMock()

    await h.provider.on_source_control(_PLAYER_ID, SourceControl.PAUSE)
    await h.event(BackendEventType.PAUSED)

    h.player.pause.assert_awaited_once()
    assert h.daemon.pause_requested_at is None


async def test_an_old_pause_request_does_not_skip_the_grace(harness: Any) -> None:
    """A pause request that Spotify never acted on soon does not skip a later grace."""
    h = harness()
    h.daemon.pause_requested_at = -provider_mod.REQUESTED_PAUSE_WINDOW_S - 1

    await h.event(BackendEventType.PAUSED)

    h.player.pause.assert_not_awaited()


async def test_resume_unpauses_a_paused_player(harness: Any, short_grace: float) -> None:
    """A play after the player was paused unpauses it on the same stream."""
    h = harness()
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)

    await h.event(BackendEventType.PLAYING)

    h.player.play.assert_awaited_once()
    assert h.daemon.paused_in_place_player is None
    assert h.daemon.output_paused is False
    assert h.stopped == []


async def test_resume_waits_for_a_pause_in_flight(harness: Any, short_grace: float) -> None:
    """A play that lands while the player is still pausing unpauses it after the pause."""
    h = harness()
    gate = asyncio.Event()
    order: list[str] = []

    async def _slow_pause() -> None:
        order.append("pause")
        await gate.wait()

    async def _play() -> None:
        order.append("play")

    h.player.pause.side_effect = _slow_pause
    h.player.play.side_effect = _play
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)

    await h.event(BackendEventType.PLAYING)
    assert order == ["pause"]
    gate.set()
    await h.settle()

    assert order == ["pause", "play"]


@pytest.mark.usefixtures("short_grace")
@pytest.mark.parametrize("failure", ["no_pause_feature", "pause_fails"])
async def test_a_player_that_cannot_pause_shows_as_paused(harness: Any, failure: str) -> None:
    """
    A player that cannot pause is stopped and shows as paused on the source.

    :param failure: How the pause fails.
    """
    h = harness(player_features=set() if failure == "no_pause_feature" else None)
    if failure == "pause_fails":
        h.player.pause.side_effect = RuntimeError("boom")

    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(0.05)

    assert h.stopped == [_PLAYER_ID]
    assert h.shown_paused == [_PLAYER_ID]
    assert h.daemon.paused_in_place_player is None


async def test_a_track_change_never_touches_the_player(harness: Any) -> None:
    """The short pauses the engine reports around a skip leave the player playing."""
    h = harness()

    for event in (
        BackendEventType.PAUSED,
        BackendEventType.PLAYING,
        BackendEventType.PAUSED,
        BackendEventType.PLAYING,
    ):
        await h.event(event)

    h.player.pause.assert_not_awaited()
    h.player.play.assert_not_awaited()
    assert h.stopped == []
    assert h.daemon.paused_in_place_player is None
    assert all(task.done() for task in h.tasks)


async def test_play_without_a_hold_starts_playback_as_before(harness: Any) -> None:
    """A play that does not follow a held pause schedules play_media as before."""
    h = harness()
    h.daemon.playing = False

    await h.event(BackendEventType.PLAYING)

    assert h.play_media_scheduled == 1


async def test_session_end_while_held_clears_the_hold(harness: Any) -> None:
    """Picking another device while held cancels the pending pause and stops the player."""
    h = harness()
    await h.event(BackendEventType.PAUSED)
    timeout_task = h.daemon.pause_output_task

    await h.event(BackendEventType.SESSION_INACTIVE)

    assert h.daemon.paused_in_place_player is None
    assert timeout_task is not None
    assert timeout_task.cancelled()
    assert h.stopped == [_PLAYER_ID]


async def test_the_source_reports_how_its_pause_is_held(harness: Any, short_grace: float) -> None:
    """The stream is fed silence until the player is paused, then only held."""
    h = harness()
    details = MagicMock(item_id=_PLAYER_ID)
    assert h.provider.get_source_pause(details) is SourcePause.NONE

    await h.event(BackendEventType.PAUSED)
    assert h.provider.get_source_pause(details) is SourcePause.FILL_SILENCE

    await asyncio.sleep(short_grace)
    assert h.provider.get_source_pause(details) is SourcePause.HOLD

    await h.event(BackendEventType.PLAYING)
    assert h.provider.get_source_pause(details) is SourcePause.NONE


def test_an_unknown_source_is_not_paused() -> None:
    """A source the provider does not know is never reported paused."""
    h = _Harness()

    assert h.provider.get_source_pause(MagicMock(item_id="other")) is SourcePause.NONE


async def test_stopping_the_daemon_while_held_cancels_the_hold(harness: Any) -> None:
    """A daemon stopped mid-hold (rename, unload) leaves no pause behind to reach the player."""
    h = harness()
    h.daemon.backend.stop = AsyncMock()
    await h.event(BackendEventType.PAUSED)
    timeout_task = h.daemon.pause_output_task
    pause_task = h.daemon.pause_output_task

    await h.provider._stop_daemon(h.daemon)

    assert h.daemon.paused_in_place_player is None
    assert timeout_task is not None
    assert timeout_task.cancelled()
    assert pause_task is not None
    assert pause_task.cancelled()
    h.player.pause.assert_not_awaited()
    assert h.stopped == []


@pytest.mark.parametrize("event_type", [BackendEventType.STOPPED, BackendEventType.CONNECTION_LOST])
async def test_spotify_ending_while_held_stops_the_player(
    harness: Any, event_type: BackendEventType
) -> None:
    """A stop or a lost session ends a hold that nothing would resume."""
    h = harness()
    await h.event(BackendEventType.PAUSED)

    await h.event(event_type)

    assert h.daemon.paused_in_place_player is None
    assert h.stopped == [_PLAYER_ID]


async def test_a_pause_waits_for_an_unpause_still_on_its_way(
    harness: Any, short_grace: float
) -> None:
    """A pause right after a resume lands after it, so the player ends up paused."""
    h = harness()
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)
    assert h.daemon.output_paused is True
    order: list[str] = []

    async def _slow_play() -> None:
        await asyncio.sleep(0.02)
        order.append("play")

    async def _pause() -> None:
        order.append("pause")

    h.player.play = AsyncMock(side_effect=_slow_play)
    h.player.pause = AsyncMock(side_effect=_pause)
    await h.event(BackendEventType.PLAYING)
    h.daemon.pause_requested_at = 0.0

    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(0.05)

    assert order == ["play", "pause"]


async def test_handing_the_source_to_another_player_clears_the_hold(harness: Any) -> None:
    """A resume after a handoff plays on the new player, not the one held before."""
    h = harness()
    await h.event(BackendEventType.PAUSED)
    h.provider.mass.players.cmd_stop = AsyncMock()
    h.daemon.backend.resume = AsyncMock()

    with (
        patch.object(SpotifyConnectProvider, "_wait_for_playing", AsyncMock(return_value=True)),
        patch.object(SpotifyConnectProvider, "_sync_player_volume_to_spotify", AsyncMock()),
    ):
        await h.provider.on_source_selected(_PLAYER_ID, "proto2", "player2", "sess2")

    assert h.daemon.paused_in_place_player is None
    await h.event(BackendEventType.PLAYING)
    h.player.play.assert_not_awaited()


async def test_a_stream_ended_by_music_assistant_clears_the_hold(harness: Any) -> None:
    """A held stream stopped from Music Assistant leaves no pause to reach the player later."""
    h = harness()
    h.daemon.active_session_id = "sess1"
    await h.event(BackendEventType.PAUSED)
    timeout_task = h.daemon.pause_output_task

    await h.provider.on_source_unselected(_PLAYER_ID, _PLAYER_ID, "sess1")
    await h.settle()

    assert h.daemon.paused_in_place_player is None
    assert timeout_task is not None
    assert timeout_task.cancelled()


async def test_a_player_stopped_at_the_pause_timeout_keeps_the_spotify_session(
    harness: Any,
) -> None:
    """A held player stopped at the pause timeout keeps the device, so play starts it again."""
    h = harness()
    h.daemon.active_session_id = "sess1"
    h.daemon.spotify_session_active = True
    h.daemon.backend.deactivate = AsyncMock()
    await h.event(BackendEventType.PAUSED)

    # the stop at the pause timeout ends the stream, as any stop of the player does
    await h.provider.on_source_unselected(_PLAYER_ID, _PLAYER_ID, "sess1")
    await h.settle()
    await h.event(BackendEventType.PLAYING)

    h.daemon.backend.deactivate.assert_not_awaited()
    assert h.daemon.spotify_session_active
    assert h.play_media_scheduled == 1


async def test_a_failed_unpause_restarts_the_player(harness: Any, short_grace: float) -> None:
    """A player that cannot be resumed in place is stopped and started on a fresh stream."""
    h = harness()
    h.daemon.active_session_id = "sess1"
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)
    h.player.play = AsyncMock(side_effect=RuntimeError("gone"))
    h.provider.mass.players.cmd_stop = AsyncMock()

    await h.event(BackendEventType.PLAYING)
    await h.settle()

    h.provider.mass.players.cmd_stop.assert_awaited_once_with(_PLAYER_ID)
    # the claim is given up first, so the teardown cannot release the Spotify session
    assert h.daemon.active_session_id is None
    assert h.daemon.in_use_by_player is None
    assert h.play_media_scheduled == 1


async def test_a_failed_unpause_starts_play_media_for_real(
    harness: Any, short_grace: float
) -> None:
    """The restart passes play_media's own guards: the claim is free and Spotify plays."""
    h = harness()
    h.daemon.active_session_id = "sess1"
    h.daemon.spotify_session_active = True
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)
    h.player.play = AsyncMock(side_effect=RuntimeError("gone"))
    h.provider.mass.players.cmd_stop = AsyncMock()
    # the real scheduler, with only its deferred fire stubbed
    del h.provider._schedule_play_media
    fire = AsyncMock()

    with patch.object(SpotifyConnectProvider, "_deferred_play_media_fire", fire):
        await h.event(BackendEventType.PLAYING)
        await h.settle()

    fire.assert_awaited_once_with(h.daemon)


async def test_a_handoff_during_an_unpause_leaves_the_new_player_alone(
    harness: Any, short_grace: float
) -> None:
    """An unpause that fails after the source moved on does not restart the new player."""
    h = harness()
    h.daemon.active_session_id = "sess1"
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)
    h.provider.mass.players.cmd_stop = AsyncMock()

    async def _handed_off_then_failed() -> None:
        h.daemon.active_player_id = "player2"
        h.daemon.in_use_by_player = "player2"
        h.daemon.active_session_id = "sess2"
        raise RuntimeError("stopped by the handoff")

    h.player.play = AsyncMock(side_effect=_handed_off_then_failed)
    await h.event(BackendEventType.PLAYING)
    await h.settle()

    h.provider.mass.players.cmd_stop.assert_not_awaited()
    assert h.daemon.active_session_id == "sess2"
    assert h.daemon.in_use_by_player == "player2"


async def test_a_resume_racing_the_release_starts_a_fresh_stream(harness: Any) -> None:
    """A resume that cancels the release stop still plays: the claim was given up first."""
    h = harness()
    h.daemon.active_session_id = "sess1"
    h.daemon.spotify_session_active = True
    await h.event(BackendEventType.PAUSED)
    h.provider._release_held_player(h.daemon)
    await h.settle()
    assert h.stopped == [_PLAYER_ID]
    assert h.daemon.in_use_by_player is None
    assert h.daemon.active_session_id is None
    # the real scheduler, with only its deferred fire stubbed
    del h.provider._schedule_play_media
    fire = AsyncMock()

    with patch.object(SpotifyConnectProvider, "_deferred_play_media_fire", fire):
        await h.event(BackendEventType.PLAYING)
        await h.settle()

    fire.assert_awaited_once_with(h.daemon)


async def test_a_stop_in_music_assistant_after_a_resume_releases_spotify(
    harness: Any, monkeypatch: pytest.MonkeyPatch, short_grace: float
) -> None:
    """A stop pressed right after an in-place resume is honoured, not undone."""
    monkeypatch.setattr(provider_mod, "STREAM_REOPEN_GRACE_S", 0)
    h = harness()
    h.daemon.active_session_id = "sess1"
    h.daemon.backend.deactivate = AsyncMock()
    await h.event(BackendEventType.PAUSED)
    await asyncio.sleep(short_grace)
    await h.event(BackendEventType.PLAYING)
    plays_before = h.play_media_scheduled

    await h.provider.on_source_unselected(_PLAYER_ID, _PLAYER_ID, "sess1")
    await h.settle()

    h.daemon.backend.deactivate.assert_awaited_once()
    assert h.play_media_scheduled == plays_before


async def test_a_stop_in_music_assistant_during_a_pending_resume_is_honoured(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpause still waiting on the pause is dropped when its stream is stopped."""
    monkeypatch.setattr(provider_mod, "STREAM_REOPEN_GRACE_S", 0)
    h = harness()
    h.daemon.active_session_id = "sess1"
    h.daemon.backend.deactivate = AsyncMock()
    h.daemon.pause_requested_at = 0.0
    pause_gate = asyncio.Event()

    async def _slow_pause() -> None:
        await pause_gate.wait()

    h.player.pause = AsyncMock(side_effect=_slow_pause)
    await h.event(BackendEventType.PAUSED)
    # the resume waits for the pause still on its way to the player
    await h.event(BackendEventType.PLAYING)
    del h.provider._schedule_play_media
    fire = AsyncMock()

    with patch.object(SpotifyConnectProvider, "_deferred_play_media_fire", fire):
        await h.provider.on_source_unselected(_PLAYER_ID, _PLAYER_ID, "sess1")
        pause_gate.set()
        await h.settle()
        await asyncio.sleep(0.05)

    h.daemon.backend.deactivate.assert_awaited_once()
    h.player.play.assert_not_awaited()
    fire.assert_not_awaited()
