"""
Contract tests for the provider <-> SpotifyConnectBackend boundary.

These tests drive the provider with a fake backend implementing the abstract
base contract: normalized events flow in through the provider's event callback
and commands flow out through the backend methods — no network, no processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    RepeatMode,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.providers.spotify_connect import provider as provider_mod
from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_HIGH,
    AUDIO_QUALITY_LOSSLESS,
    AUDIO_QUALITY_NORMAL,
    AUDIO_QUALITY_VERY_HIGH,
    SpotifyConnectBackend,
    spotify_source_audio_format,
)
from music_assistant.providers.spotify_connect.go_librespot.backend import (
    GoLibrespotBackend,
)
from music_assistant.providers.spotify_connect.models import (
    BackendEvent,
    BackendEventType,
    BackendPlaybackOptions,
    BackendQueueState,
    BackendStreamSource,
    BackendTrackMetadata,
)
from music_assistant.providers.spotify_connect.provider import (
    SpotifyConnectProvider,
    _PlayerDaemon,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.providers.spotify_connect.models import AudioChunkReader

_INSTANCE_ID = "spotify_connect_test"
# the connected player the tested daemon is bound to; doubles as the source item_id
_PLAYER_ID = "player1"
_SOURCE_URI = f"audiosource://{_INSTANCE_ID}/{_PLAYER_ID}"


class FakeBackend(SpotifyConnectBackend):
    """Minimal in-memory backend recording every command the provider dispatches."""

    def __init__(self) -> None:
        """Initialize the fake with an empty command log."""
        self.calls: list[tuple[str, Any]] = []
        # invoked from play/resume so tests can flip the provider into 'playing'
        # like a real backend would via a PLAYING event
        self.on_acquire: Callable[[], None] | None = None
        self._format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )

    @property
    def audio_format(self) -> AudioFormat:
        """Return the source audio format."""
        return self._format

    @property
    def decoded_audio_format(self) -> AudioFormat:
        """Return the decoded PCM format."""
        return self._format

    async def start(self) -> None:
        """Record the start call."""
        self.calls.append(("start", None))

    async def stop(self) -> None:
        """Record the stop call."""
        self.calls.append(("stop", None))

    async def get_stream_source(self) -> BackendStreamSource:
        """Return the same CUSTOM stream source the go-librespot backend uses."""
        return BackendStreamSource(
            stream_type=StreamType.CUSTOM,
            extra_input_args=["-fflags", "nobuffer"],
        )

    def get_audio_reader(self) -> AudioChunkReader | None:
        """Return no audio reader (audio is not exercised in these tests)."""
        return None

    async def play(self, uri: str, *, skip_to_uri: str | None = None) -> None:
        """Record a play command."""
        self.calls.append(("play", (uri, skip_to_uri)))
        if self.on_acquire:
            self.on_acquire()

    async def resume(self) -> None:
        """Record a resume command."""
        self.calls.append(("resume", None))
        if self.on_acquire:
            self.on_acquire()

    async def pause(self) -> None:
        """Record a pause command."""
        self.calls.append(("pause", None))

    async def deactivate(self) -> None:
        """Record a deactivate command."""
        self.calls.append(("deactivate", None))

    async def next(self) -> None:
        """Record a next command."""
        self.calls.append(("next", None))

    async def previous(self) -> None:
        """Record a previous command."""
        self.calls.append(("previous", None))

    async def seek(self, position_ms: int) -> None:
        """Record a seek command."""
        self.calls.append(("seek", position_ms))

    async def set_volume(self, volume: int) -> None:
        """Record a set_volume command."""
        self.calls.append(("set_volume", volume))


class QueueControlFakeBackend(FakeBackend):
    """Fake backend that additionally implements the queue-session verbs."""

    @property
    def supports_queue_control(self) -> bool:
        """The queue verbs below are implemented."""
        return True

    async def add_to_queue(self, uri: str) -> None:
        """Record an add_to_queue command."""
        self.calls.append(("add_to_queue", uri))

    async def set_shuffle(self, enabled: bool) -> None:
        """Record a set_shuffle command."""
        self.calls.append(("set_shuffle", enabled))

    async def set_repeat(self, repeat: RepeatMode) -> None:
        """Record a set_repeat command."""
        self.calls.append(("set_repeat", repeat))

    async def request_queue(self, limit: int = 10) -> None:
        """Record a request_queue command."""
        self.calls.append(("request_queue", limit))


def _create_task(coro_or_result: Any) -> Any:
    """Run coroutines as real tasks; pass anything else through as a mock."""
    if asyncio.iscoroutine(coro_or_result):
        return asyncio.get_running_loop().create_task(coro_or_result)
    return MagicMock()


def _make_provider(
    backend: FakeBackend,
    *,
    playing: bool = False,
    session_active: bool = False,
    active_player_id: str | None = None,
    in_use_by_queue: str | None = None,
    active_session_id: str | None = None,
    last_volume_sent: int | None = None,
    last_context_uri: str | None = None,
    last_track_uri: str | None = None,
) -> tuple[SpotifyConnectProvider, _PlayerDaemon, MagicMock]:
    """Build a provider with one daemon wired to the fake backend, mirroring the start defaults."""
    mass = MagicMock()
    mass.create_task.side_effect = _create_task
    # awaited inline by the deferred play_media trigger
    mass.player_queues.play_media = AsyncMock()
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = mass
    provider.logger = MagicMock()
    provider.config = MagicMock()
    provider.config.instance_id = _INSTANCE_ID
    provider.manifest = MagicMock()
    provider.manifest.domain = "spotify_connect"
    daemon = _PlayerDaemon(
        player_id=_PLAYER_ID,
        safe_player_id=_PLAYER_ID,
        publish_name="Test Device",
        stream_metadata=StreamMetadata(title="Spotify Connect | Test Device"),
    )
    daemon.backend = backend
    daemon.audio_source = MagicMock()
    daemon.audio_source.uri = _SOURCE_URI
    daemon.active_player_id = active_player_id
    daemon.in_use_by_player = in_use_by_queue
    daemon.active_session_id = active_session_id
    daemon.playing = playing
    daemon.spotify_session_active = session_active
    daemon.last_volume_sent = last_volume_sent
    daemon.last_context_uri = last_context_uri
    daemon.last_track_uri = last_track_uri
    provider._daemons = {_PLAYER_ID: daemon}
    return provider, daemon, mass


def _player_with_volume(mass: MagicMock, volume_level: int) -> MagicMock:
    """Make player lookups on the mass mock return a player with the given volume."""
    player = MagicMock()
    player.state.volume_level = volume_level
    # group_volume mirrors the player's own volume for anything that is not a group
    player.state.group_volume = volume_level
    mass.players.get_player.return_value = player
    return player


async def test_session_active_marks_session_and_syncs_volume() -> None:
    """A SESSION_ACTIVE event marks the session active and pushes the player volume."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(backend, active_player_id="player1")
    _player_with_volume(mass, 40)
    # a deferred play_media pending from a stale 'playing' must be superseded
    pending: Any = MagicMock()
    pending.done.return_value = False
    daemon.pending_play_media_task = pending

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.SESSION_ACTIVE))

    assert daemon.spotify_session_active is True
    assert daemon.last_session_active_time > 0
    pending.cancel.assert_called_once()
    assert daemon.pending_play_media_task is None
    assert backend.calls == [("set_volume", 40)]
    assert daemon.last_volume_sent == 40


async def test_playing_fires_play_media_after_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """An external 'playing' event starts play_media on the target player after the debounce."""
    monkeypatch.setattr(provider_mod, "PLAY_MEDIA_DEBOUNCE_S", 0.01)
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend, session_active=True, active_player_id="player1"
    )
    mass.players.get_player.return_value = MagicMock()

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))

    assert daemon.playing is True
    task = daemon.pending_play_media_task
    assert task is not None
    await task
    mass.player_queues.play_media.assert_called_once_with("player1", _SOURCE_URI)


async def test_stop_daemon_awaits_inflight_play_and_ignores_late_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown awaits an in-flight play_media; late backend events schedule nothing."""
    monkeypatch.setattr(provider_mod, "PLAY_MEDIA_DEBOUNCE_S", 0.01)
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend, session_active=True, active_player_id="player1"
    )
    mass.players.get_player.return_value = MagicMock()
    play_started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_play(_player_id: str, _uri: str) -> None:
        play_started.set()
        await release.wait()

    mass.player_queues.play_media = AsyncMock(side_effect=blocking_play)

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))
    await asyncio.wait_for(play_started.wait(), 1)
    task = daemon.pending_play_media_task
    assert task is not None
    assert not task.done()

    await provider._stop_daemon(daemon)

    # the in-flight play was cancelled and awaited (else _stop_daemon would hang on
    # the release event), and only then was the backend stopped
    assert task.cancelled()
    assert ("stop", None) in backend.calls
    # a late backend event on the stopped daemon schedules no replacement work
    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))
    assert daemon.pending_play_media_task is None
    mass.player_queues.play_media.assert_awaited_once()


async def test_playing_before_session_active_fires_play_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playback starts when 'playing' arrives before the session becomes active."""
    monkeypatch.setattr(provider_mod, "PLAY_MEDIA_DEBOUNCE_S", 0.01)
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(backend, active_player_id="player1")
    _player_with_volume(mass, 40)

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))

    pending_before_active = daemon.pending_play_media_task
    assert pending_before_active is None

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.SESSION_ACTIVE))

    task = daemon.pending_play_media_task
    assert task is not None
    await task
    mass.player_queues.play_media.assert_called_once_with("player1", _SOURCE_URI)


async def test_playing_without_active_session_fires_no_play_media() -> None:
    """A 'playing' from a daemon that is not the active Connect device grabs no player."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(backend, active_player_id="player1")

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))

    assert daemon.playing is True
    assert daemon.pending_play_media_task is None


async def test_paused_within_debounce_cancels_play_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'paused' during the debounce window cancels the deferred play_media."""
    monkeypatch.setattr(provider_mod, "PLAY_MEDIA_DEBOUNCE_S", 5.0)
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend, session_active=True, active_player_id="player1"
    )
    mass.players.get_player.return_value = MagicMock()

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))
    task = daemon.pending_play_media_task
    assert task is not None
    await asyncio.sleep(0)  # let the deferred fire enter its debounce sleep
    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PAUSED))

    assert daemon.playing is False
    assert daemon.pending_play_media_task is None
    await task  # the deferred fire swallows the cancellation and exits
    mass.player_queues.play_media.assert_not_called()


async def test_playing_while_claimed_does_not_fire_play_media() -> None:
    """No play_media is scheduled while a queue already claims the source."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(backend, in_use_by_queue="queue1")

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))

    assert daemon.playing is True
    assert daemon.pending_play_media_task is None


async def test_metadata_event_updates_stream_metadata_and_pushes() -> None:
    """A METADATA event updates the live StreamMetadata and pushes it to the active queue."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(backend, in_use_by_queue="queue1")

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.METADATA,
            context_uri="spotify:playlist:ctx",
            track_uri="spotify:track:tr",
            metadata=BackendTrackMetadata(
                track_uri="spotify:track:tr",
                title="Song",
                artist="Artist",
                album="Album",
                image_url="http://img",
                duration=200,
                position=5,
            ),
        ),
    )

    assert daemon.stream_metadata.uri == "spotify:track:tr"
    assert daemon.stream_metadata.title == "Song"
    assert daemon.stream_metadata.artist == "Artist"
    assert daemon.stream_metadata.album == "Album"
    assert daemon.stream_metadata.image_url == "http://img"
    assert daemon.stream_metadata.duration == 200
    assert daemon.stream_metadata.elapsed_time == 5
    assert daemon.last_context_uri == "spotify:playlist:ctx"
    assert daemon.last_track_uri == "spotify:track:tr"
    mass.players.update_source_metadata.assert_called_once_with(
        "queue1", _PLAYER_ID, _INSTANCE_ID, daemon.stream_metadata
    )

    # a metadata event without a title keeps the previous one
    await provider._handle_backend_event(
        daemon, BackendEvent(BackendEventType.METADATA, metadata=BackendTrackMetadata())
    )
    assert daemon.stream_metadata.title == "Song"


async def test_other_event_refreshes_context_uris() -> None:
    """Uncategorized backend events still refresh the cached context/track uris."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(backend)

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.OTHER,
            context_uri="spotify:album:ctx",
            track_uri="spotify:track:willplay",
        ),
    )

    assert daemon.last_context_uri == "spotify:album:ctx"
    assert daemon.last_track_uri == "spotify:track:willplay"


async def test_volume_event_applies_with_dedupe_and_grace() -> None:
    """Volume events are deduped against echoes and gated by the activation grace period."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(backend, in_use_by_queue="queue1", last_volume_sent=30)
    mass.players.cmd_volume_set = AsyncMock()

    # echo of the value we just pushed ourselves: ignored
    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.VOLUME, volume=30))
    mass.players.cmd_volume_set.assert_not_awaited()

    # within the session-activation grace period: ignored
    daemon.last_session_active_time = time.time()
    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.VOLUME, volume=45))
    mass.players.cmd_volume_set.assert_not_awaited()

    # past the grace period: applied to the claiming queue's player and cached
    daemon.last_session_active_time = time.time() - 10
    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.VOLUME, volume=45))
    mass.players.cmd_volume_set.assert_awaited_once_with("queue1", 45)
    assert daemon.last_volume_sent == 45


async def test_session_inactive_stops_active_player() -> None:
    """A SESSION_INACTIVE event clears all claims and stops the active MA player."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend,
        playing=True,
        session_active=True,
        active_player_id="player1",
        in_use_by_queue="queue1",
        active_session_id="sess1",
    )

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.SESSION_INACTIVE))
    # the stop runs as a bounded background task; let it start
    await asyncio.sleep(0)

    assert daemon.spotify_session_active is False
    assert daemon.playing is False
    assert daemon.active_player_id is None
    assert daemon.in_use_by_player is None
    assert daemon.active_session_id is None
    mass.players.cmd_stop.assert_called_once_with("player1")
    # signing out is not a provider failure: it must never schedule an unload
    mass.call_later.assert_not_called()


async def test_stream_teardown_while_playing_releases_spotify() -> None:
    """An MA-side stop/queue-clear releases the Spotify session so the app drops the device."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(
        backend,
        playing=True,
        session_active=True,
        in_use_by_queue="queue1",
        active_session_id="sess1",
    )

    await provider.on_source_unselected(_PLAYER_ID, "queue1", "sess1")

    assert daemon.in_use_by_player is None
    assert daemon.active_session_id is None
    assert backend.calls == [("deactivate", None)]


async def test_stream_teardown_after_spotify_pause_does_not_pause_again() -> None:
    """A teardown that resulted from a Spotify-side pause sends no pause command."""
    backend = FakeBackend()
    provider, _daemon, _mass = _make_provider(
        backend,
        playing=False,
        session_active=True,
        in_use_by_queue="queue1",
        active_session_id="sess1",
    )

    await provider.on_source_unselected(_PLAYER_ID, "queue1", "sess1")

    assert backend.calls == []


async def test_stale_stream_teardown_is_ignored() -> None:
    """A late unselect from a superseded stream session releases and pauses nothing."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(
        backend,
        playing=True,
        session_active=True,
        in_use_by_queue="queue1",
        active_session_id="sess2",
    )

    await provider.on_source_unselected(_PLAYER_ID, "queue1", "sess1")

    assert daemon.in_use_by_player == "queue1"
    assert daemon.active_session_id == "sess2"
    assert backend.calls == []


async def test_connection_lost_resets_session_without_stopping_players() -> None:
    """CONNECTION_LOST silently resets session state; player claims are left alone."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend,
        playing=True,
        session_active=True,
        active_player_id="player1",
        in_use_by_queue="queue1",
    )

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.CONNECTION_LOST))

    assert daemon.spotify_session_active is False
    assert daemon.playing is False
    # the claim survives a backend restart; only session state resets
    assert daemon.active_player_id == "player1"
    assert daemon.in_use_by_player == "queue1"
    mass.players.cmd_stop.assert_not_called()


async def test_provider_wide_fatal_error_unloads_provider_with_error() -> None:
    """A provider-wide FATAL_ERROR unloads the provider with the backend's error message."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(backend)

    await provider._handle_backend_event(
        daemon,
        BackendEvent(BackendEventType.FATAL_ERROR, error="daemon kaput", provider_wide=True),
    )

    mass.call_later.assert_called_once_with(
        1, mass.unload_provider_with_error, _INSTANCE_ID, "daemon kaput"
    )


async def test_source_control_commands_dispatch_to_backend() -> None:
    """Transport commands on the source map onto the matching backend calls."""
    backend = FakeBackend()
    provider, _daemon, _mass = _make_provider(backend, playing=True)

    await provider.on_source_control(_PLAYER_ID, SourceControl.PLAY)
    await provider.on_source_control(_PLAYER_ID, SourceControl.PAUSE)
    await provider.on_source_control(_PLAYER_ID, SourceControl.NEXT)
    await provider.on_source_control(_PLAYER_ID, SourceControl.PREVIOUS)
    await provider.on_source_control(_PLAYER_ID, SourceControl.SEEK, 42)

    assert backend.calls == [
        ("resume", None),
        ("pause", None),
        ("next", None),
        ("previous", None),
        ("seek", 42000),
    ]


async def test_on_volume_change_pushes_deduped_volume_to_backend() -> None:
    """on_volume_change pushes new volumes to the backend and skips unchanged ones."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(backend, playing=True)

    await provider.on_volume_change(_PLAYER_ID, 60)
    assert backend.calls == [("set_volume", 60)]
    assert daemon.last_volume_sent == 60

    # unchanged value: deduped, nothing sent
    await provider.on_volume_change(_PLAYER_ID, 60)
    assert backend.calls == [("set_volume", 60)]


async def test_queue_control_contract_defaults() -> None:
    """A backend without queue control reports so and refuses the queue-session verbs."""
    backend = FakeBackend()

    assert backend.supports_queue_control is False
    with pytest.raises(NotImplementedError):
        await backend.add_to_queue("spotify:track:t1")
    with pytest.raises(NotImplementedError):
        await backend.set_shuffle(True)
    with pytest.raises(NotImplementedError):
        await backend.set_repeat(RepeatMode.OFF)
    with pytest.raises(NotImplementedError):
        await backend.request_queue()


async def test_shuffle_and_repeat_controls_dispatch_to_backend() -> None:
    """SHUFFLE and REPEAT source controls map onto the backend's queue-session verbs."""
    backend = QueueControlFakeBackend()
    provider, _daemon, _mass = _make_provider(backend, playing=True)

    await provider.on_source_control(_PLAYER_ID, SourceControl.SHUFFLE, True)
    await provider.on_source_control(_PLAYER_ID, SourceControl.SHUFFLE, False)
    await provider.on_source_control(_PLAYER_ID, SourceControl.REPEAT, RepeatMode.ALL)

    assert backend.calls == [
        ("set_shuffle", True),
        ("set_shuffle", False),
        ("set_repeat", RepeatMode.ALL),
    ]
    # the shuffle payload is passed through strictly typed, never coerced
    assert all(type(value) is bool for name, value in backend.calls if name == "set_shuffle")


async def test_shuffle_control_ignores_non_bool_payloads() -> None:
    """A None (or misrouted enum) payload never silently toggles shuffle."""
    backend = QueueControlFakeBackend()
    provider, _daemon, _mass = _make_provider(backend, playing=True)

    await provider.on_source_control(_PLAYER_ID, SourceControl.SHUFFLE, None)
    await provider.on_source_control(_PLAYER_ID, SourceControl.SHUFFLE, RepeatMode.OFF)

    assert backend.calls == []


async def test_seek_control_accepts_floats_but_not_bools() -> None:
    """A float seek position still works; a bool must not become a 1-second seek."""
    backend = FakeBackend()
    provider, _daemon, _mass = _make_provider(backend, playing=True)

    await provider.on_source_control(_PLAYER_ID, SourceControl.SEEK, 42.7)  # type: ignore[arg-type]
    await provider.on_source_control(_PLAYER_ID, SourceControl.SEEK, True)

    assert backend.calls == [("seek", 42000)]


async def test_shuffle_control_refused_without_active_session() -> None:
    """The existing not-active gate also covers the new queue controls."""
    backend = QueueControlFakeBackend()
    provider, _daemon, _mass = _make_provider(backend)

    with pytest.raises(AudioError, match="not the active Spotify playback device"):
        await provider.on_source_control(_PLAYER_ID, SourceControl.SHUFFLE, True)

    assert backend.calls == []


async def test_options_changed_pushes_options_to_the_claiming_player() -> None:
    """An OPTIONS_CHANGED event mirrors the session's shuffle/repeat to the active queue."""
    backend = QueueControlFakeBackend()
    provider, daemon, mass = _make_provider(backend, in_use_by_queue="queue1")

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.OPTIONS_CHANGED,
            options=BackendPlaybackOptions(shuffle=True, repeat=RepeatMode.ALL),
        ),
    )

    mass.players.update_source_options.assert_called_once_with(
        "queue1",
        _PLAYER_ID,
        _INSTANCE_ID,
        shuffle_enabled=True,
        repeat_mode=RepeatMode.ALL,
    )
    # an options report is no reason to re-push the (unchanged) stream metadata
    mass.players.update_source_metadata.assert_not_called()


async def test_options_changed_without_claiming_queue_pushes_nothing() -> None:
    """Session options are only mirrored while a queue is streaming the source."""
    backend = QueueControlFakeBackend()
    provider, daemon, mass = _make_provider(backend)

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.OPTIONS_CHANGED,
            options=BackendPlaybackOptions(shuffle=True, repeat=RepeatMode.ALL),
        ),
    )

    mass.players.update_source_options.assert_not_called()
    # the options are cached for the push that follows once a queue claims the source
    assert daemon.last_playback_options == BackendPlaybackOptions(
        shuffle=True, repeat=RepeatMode.ALL
    )


async def test_options_cached_before_claim_are_pushed_on_claim() -> None:
    """Options reported before the queue claim exists are pushed once the claim is set."""
    backend = QueueControlFakeBackend()
    provider, daemon, mass = _make_provider(backend, playing=True, session_active=True)
    _player_with_volume(mass, 25)

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.OPTIONS_CHANGED,
            options=BackendPlaybackOptions(shuffle=True, repeat=RepeatMode.ALL),
        ),
    )
    mass.players.update_source_options.assert_not_called()

    await provider.on_source_selected(_PLAYER_ID, "proto1", "queue1", "sess1")

    mass.players.update_source_options.assert_called_once_with(
        "queue1",
        _PLAYER_ID,
        _INSTANCE_ID,
        shuffle_enabled=True,
        repeat_mode=RepeatMode.ALL,
    )


async def test_session_inactive_clears_cached_options() -> None:
    """Cached options do not outlive the session they were reported by."""
    backend = QueueControlFakeBackend()
    provider, daemon, _mass = _make_provider(backend, session_active=True)

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.OPTIONS_CHANGED,
            options=BackendPlaybackOptions(shuffle=True, repeat=RepeatMode.ALL),
        ),
    )
    assert daemon.last_playback_options is not None

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.SESSION_INACTIVE))

    assert daemon.last_playback_options is None


async def test_queue_changed_event_is_ignored_for_now() -> None:
    """QUEUE_CHANGED snapshots are not consumed yet and skip the metadata push tail."""
    backend = QueueControlFakeBackend()
    provider, daemon, mass = _make_provider(backend, in_use_by_queue="queue1")

    await provider._handle_backend_event(
        daemon,
        BackendEvent(
            BackendEventType.QUEUE_CHANGED,
            queue=BackendQueueState(),
            context_uri="spotify:playlist:ctx",
        ),
    )

    mass.players.update_source_metadata.assert_not_called()
    mass.players.update_source_options.assert_not_called()
    # the context memo still applies: it piggybacks on every event type
    assert daemon.last_context_uri == "spotify:playlist:ctx"


def test_audio_source_declares_ordering_with_queue_control() -> None:
    """A queue-control backend makes the AudioSource declare it orders its session."""
    backend = QueueControlFakeBackend()
    provider, daemon, _mass = _make_provider(backend)
    provider.config.name = "Spotify Connect Test"

    source = provider._build_audio_source(daemon, "Player 1")

    assert source.item_id == _PLAYER_ID
    assert source.name == "Spotify Connect Test (Player 1)"
    assert source.can_shuffle is True
    assert source.can_repeat is True
    # account verification arrives with the play redirect
    assert source.account_id is None


def test_audio_source_stays_transport_only_without_queue_control() -> None:
    """A transport-only backend leaves the AudioSource unable to order itself."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(backend)
    provider.config.name = "Spotify Connect Test"

    source = provider._build_audio_source(daemon, "Player 1")

    assert source.can_shuffle is False
    assert source.can_repeat is False


async def test_source_selected_takes_playback_back_via_backend() -> None:
    """Selecting the source without an active session replays the last context (take-back)."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend,
        last_context_uri="spotify:playlist:ctx",
        last_track_uri="spotify:track:tr",
    )
    _player_with_volume(mass, 25)

    def _mark_playing() -> None:
        daemon.playing = True

    backend.on_acquire = _mark_playing

    await provider.on_source_selected(_PLAYER_ID, "proto1", "queue1", "sess1")

    assert daemon.in_use_by_player == "queue1"
    assert daemon.active_player_id == "queue1"
    assert daemon.active_session_id == "sess1"
    assert backend.calls == [
        ("play", ("spotify:playlist:ctx", "spotify:track:tr")),
        ("set_volume", 25),
    ]


async def test_source_selected_resumes_active_session_via_backend() -> None:
    """Selecting the source with a paused-but-active session resumes it on the backend."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(backend, session_active=True)
    _player_with_volume(mass, 25)

    def _mark_playing() -> None:
        daemon.playing = True

    backend.on_acquire = _mark_playing

    await provider.on_source_selected(_PLAYER_ID, "proto1", "queue1", "sess1")

    assert backend.calls == [("resume", None), ("set_volume", 25)]


async def test_get_stream_details_built_from_backend_stream_source() -> None:
    """StreamDetails mirror the backend's stream source, formats and live metadata."""
    backend = FakeBackend()
    provider, daemon, _mass = _make_provider(backend, playing=True)

    details = await provider.get_stream_details(_PLAYER_ID, MediaType.AUDIO_SOURCE)

    assert details.provider == _INSTANCE_ID
    assert details.item_id == _PLAYER_ID
    assert details.media_type is MediaType.AUDIO_SOURCE
    assert details.stream_type is StreamType.CUSTOM
    assert details.path is None
    assert details.extra_input_args == ["-fflags", "nobuffer"]
    assert details.audio_format is backend.audio_format
    assert details.decoded_audio_format is backend.decoded_audio_format
    assert details.stream_metadata is daemon.stream_metadata
    assert details.expiration == 0


async def test_go_librespot_stream_source_is_custom_with_nobuffer() -> None:
    """The go-librespot backend delivers audio as a CUSTOM source with unbuffered input."""
    source = await object.__new__(GoLibrespotBackend).get_stream_source()

    assert source.stream_type is StreamType.CUSTOM
    assert source.path is None
    assert source.extra_input_args == ["-fflags", "nobuffer"]


@pytest.mark.parametrize(
    ("quality", "lossless", "codec", "bit_depth", "bit_rate"),
    [
        (AUDIO_QUALITY_LOSSLESS, True, ContentType.FLAC, 24, None),
        # an engine or item that cannot be served losslessly keeps the tier's ceiling
        (AUDIO_QUALITY_LOSSLESS, False, ContentType.VORBIS, 16, 320),
        (AUDIO_QUALITY_VERY_HIGH, False, ContentType.VORBIS, 16, 320),
        (AUDIO_QUALITY_HIGH, False, ContentType.VORBIS, 16, 160),
        (AUDIO_QUALITY_NORMAL, False, ContentType.VORBIS, 16, 96),
        ("future_tier", False, ContentType.VORBIS, 16, 320),
    ],
)
def test_the_advertised_source_format_follows_the_tier(
    quality: str,
    lossless: bool,
    codec: ContentType,
    bit_depth: int,
    bit_rate: int | None,
) -> None:
    """Every soloist/librespot engine advertises the tier it was configured with."""
    fmt = spotify_source_audio_format(quality, lossless=lossless)

    assert fmt.codec_type is codec
    assert fmt.sample_rate == 44100
    assert fmt.bit_depth == bit_depth
    assert fmt.channels == 2
    assert fmt.bit_rate == bit_rate


def _make_backend() -> GoLibrespotBackend:
    """Build a bare backend instance, enough to exercise event translation."""
    return object.__new__(GoLibrespotBackend)


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("active", BackendEventType.SESSION_ACTIVE),
        ("inactive", BackendEventType.SESSION_INACTIVE),
        ("playing", BackendEventType.PLAYING),
        ("paused", BackendEventType.PAUSED),
        ("stopped", BackendEventType.STOPPED),
        ("will_play", BackendEventType.OTHER),
        ("some_future_event", BackendEventType.OTHER),
    ],
)
def test_translate_event_maps_lifecycle_types(raw_type: str, expected: BackendEventType) -> None:
    """Raw lifecycle events map to their normalized type, unknown types to OTHER."""
    event = _make_backend()._translate_event(
        raw_type, {"context_uri": "spotify:playlist:ctx", "uri": "spotify:track:t1"}
    )
    assert event.type is expected
    assert event.context_uri == "spotify:playlist:ctx"
    assert event.track_uri == "spotify:track:t1"


def test_translate_event_maps_metadata_fields() -> None:
    """Metadata fields map 1:1 with millisecond durations converted to seconds."""
    event = _make_backend()._translate_event(
        "metadata",
        {
            "uri": "spotify:track:t1",
            "name": "Track Title",
            "artist_names": ["Main Artist", "Feat Artist"],
            "album_name": "The Album",
            "album_cover_url": "http://img.invalid/c.jpg",
            "duration": 215500,
            "position": 32900,
        },
    )
    assert event.type is BackendEventType.METADATA
    metadata = event.metadata
    assert metadata is not None
    assert metadata.track_uri == "spotify:track:t1"
    assert metadata.title == "Track Title"
    assert metadata.artist == "Main Artist"
    assert metadata.album == "The Album"
    assert metadata.image_url == "http://img.invalid/c.jpg"
    assert metadata.duration == 215
    assert metadata.position == 32


def test_translate_event_maps_seek_position_ms_to_seconds() -> None:
    """Seek events carry the position converted from milliseconds to whole seconds."""
    event = _make_backend()._translate_event("seek", {"position": 61999})
    assert event.type is BackendEventType.POSITION
    assert event.position == 61


@pytest.mark.parametrize(
    ("value", "max_value", "expected_pct"),
    [
        (0, 100, 0),
        (50, 100, 50),
        (100, 100, 100),
        (33, 100, 33),
        (10, 16, 62),  # non-default scale, truncating conversion
    ],
)
def test_translate_event_scales_volume_to_percentage(
    value: int, max_value: int, expected_pct: int
) -> None:
    """Volume events translate the daemon's 0..max scale to a 0-100 percentage."""
    event = _make_backend()._translate_event("volume", {"value": value, "max": max_value})
    assert event.type is BackendEventType.VOLUME
    assert event.volume == expected_pct


def test_translate_event_volume_without_value_is_other() -> None:
    """A volume event without a value carries no volume and degrades to OTHER."""
    event = _make_backend()._translate_event("volume", {"max": 100})
    assert event.type is BackendEventType.OTHER
    assert event.volume is None


async def test_paused_stops_player_when_stream_does_not_end() -> None:
    """A pipe-fed backend (no EOF on pause) gets its player actively stopped."""

    class _PipeFedBackend(FakeBackend):
        @property
        def stream_ends_on_pause(self) -> bool:
            return False

    backend = _PipeFedBackend()
    provider, daemon, mass = _make_provider(backend, playing=True, active_player_id="player-1")
    stopped: list[str] = []

    async def _cmd_stop(player_id: str) -> None:
        stopped.append(player_id)

    mass.players.cmd_stop = _cmd_stop

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PAUSED))
    await asyncio.sleep(0)

    assert daemon.playing is False
    assert stopped == ["player-1"]
    # the claim survives so the next 'playing' event can resume playback
    assert daemon.active_player_id == "player-1"
    # the finished stop task's handle is dropped again
    await asyncio.sleep(0)
    assert daemon.pending_pause_stop_task is None


async def test_duplicate_paused_events_stop_player_once() -> None:
    """The backend reports a pause via multiple events; only one stop is issued."""

    class _PipeFedBackend(FakeBackend):
        @property
        def stream_ends_on_pause(self) -> bool:
            return False

    backend = _PipeFedBackend()
    provider, daemon, mass = _make_provider(backend, playing=True, active_player_id="player-1")
    stopped: list[str] = []

    async def _cmd_stop(player_id: str) -> None:
        stopped.append(player_id)

    mass.players.cmd_stop = _cmd_stop

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PAUSED))
    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PAUSED))
    await asyncio.sleep(0)

    assert stopped == ["player-1"]


async def test_playing_cancels_pending_pause_stop() -> None:
    """A quick resume cancels the in-flight pause-stop so it can't kill the new stream."""

    class _PipeFedBackend(FakeBackend):
        @property
        def stream_ends_on_pause(self) -> bool:
            return False

    backend = _PipeFedBackend()
    provider, daemon, mass = _make_provider(
        backend, playing=True, active_player_id="player-1", in_use_by_queue="queue1"
    )
    stop_started = asyncio.Event()
    stopped: list[str] = []

    async def _cmd_stop(player_id: str) -> None:
        stop_started.set()
        await asyncio.sleep(3600)  # an unresponsive player holds the stop in flight
        stopped.append(player_id)

    mass.players.cmd_stop = _cmd_stop

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PAUSED))
    task = daemon.pending_pause_stop_task
    assert task is not None
    async with asyncio.timeout(1.0):
        await stop_started.wait()

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PLAYING))

    assert daemon.pending_pause_stop_task is None
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert stopped == []  # the stale stop never reached the player


async def test_paused_replaces_pending_pause_stop() -> None:
    """A new pause-stop supersedes a still-pending one instead of piling up."""

    class _PipeFedBackend(FakeBackend):
        @property
        def stream_ends_on_pause(self) -> bool:
            return False

    backend = _PipeFedBackend()
    provider, daemon, mass = _make_provider(backend, playing=True, active_player_id="player-1")
    mass.players.cmd_stop = AsyncMock()
    stale: Any = MagicMock()
    stale.done.return_value = False
    daemon.pending_pause_stop_task = stale

    await provider._handle_backend_event(daemon, BackendEvent(BackendEventType.PAUSED))

    stale.cancel.assert_called_once()
    assert daemon.pending_pause_stop_task is not stale
    # let the replacement stop task run to completion (and drop its handle)
    for _ in range(3):
        await asyncio.sleep(0)
    assert daemon.pending_pause_stop_task is None


async def test_handoff_kick_cannot_release_the_session() -> None:
    """An old stream's teardown during the handoff kick must not deactivate the session."""
    backend = FakeBackend()
    provider, daemon, mass = _make_provider(
        backend,
        playing=True,
        session_active=True,
        active_player_id="queue_old",
        in_use_by_queue="queue_old",
        active_session_id="sess_old",
    )
    _player_with_volume(mass, 25)

    async def _stop_old_player(_player_id: str) -> None:
        # a fast player: its stream teardown completes within the awaited stop, so
        # only the already-replaced session id keeps this from releasing the session
        await provider.on_source_unselected(_PLAYER_ID, "queue_old", "sess_old")

    mass.players.cmd_stop = AsyncMock(side_effect=_stop_old_player)

    await provider.on_source_selected(_PLAYER_ID, "proto_new", "queue_new", "sess_new")

    assert ("deactivate", None) not in backend.calls
    assert daemon.in_use_by_player == "queue_new"
    assert daemon.active_session_id == "sess_new"
    assert daemon.active_player_id == "queue_new"
    mass.players.cmd_stop.assert_awaited_once_with("queue_old")
