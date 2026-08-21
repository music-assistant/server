"""Tests for the Spotify Soloist backend (all fakes: no processes, network or pulse)."""

from __future__ import annotations

import asyncio
import logging
import stat
from contextlib import suppress
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, RepeatMode, StreamType
from music_assistant_models.errors import AudioError

from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_HIGH,
    AUDIO_QUALITY_LOSSLESS,
    AUDIO_QUALITY_NORMAL,
    AUDIO_QUALITY_VERY_HIGH,
)
from music_assistant.providers.spotify_connect.models import (
    BackendEvent,
    BackendEventType,
    QueueEntrySource,
)
from music_assistant.providers.spotify_connect.soloist import backend as soloist_backend
from music_assistant.providers.spotify_connect.soloist.backend import (
    CACHE_SIZE_MB,
    VOLUME_MODE_PLAYER_ONLY,
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
)
from music_assistant.providers.spotify_connect.soloist.runtime import (
    BuildExpiredError,
    ConsentRequiredError,
    SoloistAuthState,
    SoloistCommandResult,
    SoloistDeviceChanged,
    SoloistEntity,
    SoloistErrorMessage,
    SoloistEvent,
    SoloistOptionsChanged,
    SoloistPlaybackOptions,
    SoloistPlaybackState,
    SoloistPosition,
    SoloistPositionSync,
    SoloistQueueChanged,
    SoloistQueueEntry,
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

_API_KEY = "sk-super-secret-key-123"
_INSTANCE_ID = "spotify_connect--test1"


class _FakeServer:
    """Fake PulseCaptureServer with a settable generation."""

    def __init__(self) -> None:
        self.generation = 1
        self.released = 0

    async def acquire(self) -> _FakeServer:
        """Return self, like the real refcounted acquire."""
        return self

    async def release(self) -> None:
        """Record the release."""
        self.released += 1

    def child_env(self, sink_name: str) -> dict[str, str]:
        """Return a minimal audio-client environment."""
        return {"PULSE_SERVER": "unix:/fake/native", "PULSE_SINK": sink_name}


class _FakeSink:
    """Fake PipeSink recording volume changes and unloads."""

    def __init__(self, name: str = "sink1") -> None:
        self.sink_name = name
        self.fifo_path = Path(f"/fake/{name}.pcm")
        self.volumes: list[float] = []
        self.unloaded = 0
        # when set (and not yet signalled), set_volume blocks on this gate
        self.gate: asyncio.Event | None = None
        # when set, set_volume raises this instead of recording the change
        self.set_volume_error: Exception | None = None

    async def set_volume(self, volume_pct: float) -> None:
        """Record a volume change, optionally blocking on the gate first."""
        if self.gate is not None:
            await self.gate.wait()
        if self.set_volume_error is not None:
            raise self.set_volume_error
        self.volumes.append(volume_pct)

    async def unload(self) -> None:
        """Record the unload."""
        self.unloaded += 1


class _FakeProc:
    """Stand-in for AsyncProcess recording its lifecycle."""

    def __init__(
        self,
        exit_code: int = 0,
        start_error: Exception | None = None,
        *,
        stdout_lines: list[str] | None = None,
        block_stdout: bool = False,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.returncode: int | None = None
        self.closed = 0
        self._exit_code = exit_code
        self._start_error = start_error
        self._stdout_lines = stdout_lines or []
        self._block_stdout = block_stdout
        self._on_close = on_close
        self._closed_event = asyncio.Event()
        # a real daemon's stdout reaches EOF exactly when the process exits,
        # so the fake ties its wait() to its output ending
        self._output_done = asyncio.Event()

    async def start(self) -> None:
        """Start the fake process, failing when configured to."""
        if self._start_error is not None:
            raise self._start_error

    async def close(self) -> None:
        """Mark the process closed and expose its exit code."""
        self.closed += 1
        self.returncode = self._exit_code
        self._closed_event.set()
        if self._on_close is not None:
            self._on_close()

    async def iter_stdout(self) -> AsyncGenerator[str]:
        """Yield the configured stdout lines, optionally blocking until closed."""
        try:
            for line in self._stdout_lines:
                yield line
            if self._block_stdout:
                await self._closed_event.wait()
        finally:
            self._output_done.set()

    async def wait(self) -> int:
        """Return the exit code once the fake daemon's output ended."""
        await self._output_done.wait()
        return self._exit_code


def _make_backend(
    *,
    volume_mode: str = VOLUME_MODE_PLAYER_ONLY,
    instance_id: str = _INSTANCE_ID,
    base_dir: Path | None = None,
    consent: bool = True,
) -> tuple[SoloistBackend, list[BackendEvent]]:
    """Build a backend on a mocked mass, capturing every emitted BackendEvent."""
    mass = MagicMock()
    mass.storage_path = str(base_dir / "storage") if base_dir else "/fake/storage"
    mass.cache_path = str(base_dir / "cache") if base_dir else "/fake/cache"
    events: list[BackendEvent] = []

    async def _capture(event: BackendEvent) -> None:
        events.append(event)

    backend = SoloistBackend(
        mass,
        instance_id=instance_id,
        publish_name="Test Device",
        name="Spotify Test",
        logger=logging.getLogger("test.soloist_backend"),
        event_callback=_capture,
        api_key=_API_KEY,
        consent=consent,
        volume_mode=volume_mode,
    )
    return backend, events


def _runner_backend(
    *, volume_mode: str = VOLUME_MODE_PLAYER_ONLY
) -> tuple[SoloistBackend, list[BackendEvent]]:
    """Build a backend primed to run its daemon supervisor with fakes."""
    backend, events = _make_backend(volume_mode=volume_mode)
    backend._binary = Path("/fake/bin/soloist")
    server: Any = _FakeServer()
    sink: Any = _FakeSink()
    backend._server = server
    backend._sink = sink
    backend._sink_generation = server.generation
    return backend, events


def _patch_spawn(
    monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc]
) -> list[tuple[list[str], dict[str, Any]]]:
    """Replace AsyncProcess with a factory serving the given fakes, recording spawns."""
    spawned: list[tuple[list[str], dict[str, Any]]] = []

    def _spawn(args: list[str], **kwargs: Any) -> _FakeProc:
        spawned.append((args, kwargs))
        return procs[len(spawned) - 1]

    monkeypatch.setattr(soloist_backend, "AsyncProcess", _spawn)
    return spawned


def _event(event_type: str, data: Any) -> SoloistEvent:
    """Wrap a decoded payload in a SoloistEvent."""
    return SoloistEvent(type=event_type, data=data, raw={"type": event_type})


def _volume_event(volume: int) -> SoloistEvent:
    """Wrap a volume value in a decoded volume_changed event."""
    return _event("volume_changed", SoloistVolumeChanged(volume=volume))


async def test_start_wires_binary_capture_and_supervisors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() installs the binary, acquires capture and launches both supervisors."""
    backend, _events = _make_backend(base_dir=tmp_path)
    tasks: list[str] = []

    def _fake_create_task(coro: Any) -> MagicMock:
        tasks.append(coro.__name__)
        coro.close()
        return MagicMock()

    mass_mock: Any = backend.mass
    mass_mock.create_task.side_effect = _fake_create_task
    consents: list[bool] = []

    class _FakeManager:
        """Fake binary manager recording the consent flag."""

        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        def diagnostics(self) -> dict[str, Any]:
            """Report the installed build's digest."""
            return {"installed": True, "sha256": "sha-1"}

        async def ensure_fresh(self, consent: bool) -> Path:
            """Record the consent flag and hand out a fake binary path."""
            consents.append(consent)
            return Path("/fake/bin/soloist")

    server: Any = _FakeServer()
    sink: Any = _FakeSink()
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _FakeManager)
    monkeypatch.setattr(soloist_backend, "get_pulse_capture_server", lambda _mass: server)
    monkeypatch.setattr(
        soloist_backend, "PipeSink", SimpleNamespace(create=AsyncMock(return_value=sink))
    )

    await backend.start()

    assert consents == [True]
    assert backend._binary == Path("/fake/bin/soloist")
    assert backend._sink_generation == server.generation
    assert backend._server is server
    assert backend._sink is sink
    assert backend._client is not None
    assert backend._client.data_dir == backend._data_dir
    assert backend._data_dir.is_dir()
    # the data dir holds the Spotify device identity/session: owner-only
    assert stat.S_IMODE(backend._data_dir.stat().st_mode) == 0o700
    assert backend._cache_dir.is_dir()
    assert tasks == [
        "_daemon_runner",
        "_events_runner",
        "_binary_refresh_loop",
        "_generation_watcher",
    ]


async def test_start_setup_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary setup error fails start() before any capture resource is acquired."""
    backend, _events = _make_backend(consent=False)

    class _RefusingManager:
        """Fake binary manager that refuses without consent."""

        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        async def ensure_fresh(self, consent: bool) -> Path:
            """Refuse the download."""
            raise ConsentRequiredError("consent required")

    capture = MagicMock()
    capture.acquire = AsyncMock()
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _RefusingManager)
    monkeypatch.setattr(soloist_backend, "get_pulse_capture_server", lambda _mass: capture)

    with pytest.raises(ConsentRequiredError):
        await backend.start()
    capture.acquire.assert_not_awaited()


async def test_daemon_argv_and_key_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The daemon argv carries the exact documented flags; the api key never hits a log."""
    backend, _events = _runner_backend()
    # a single supervisor iteration: stop once the spawned process is closed
    proc = _FakeProc(on_close=lambda: setattr(backend, "_stop_called", True))
    spawned = _patch_spawn(monkeypatch, [proc])

    with caplog.at_level(logging.DEBUG):
        await backend._daemon_runner()

    args, kwargs = spawned[0]
    assert args == [
        "/fake/bin/soloist",
        "--device-name",
        "Test Device",
        "--api-key",
        _API_KEY,
        "--data-dir",
        f"/fake/storage/spotify_connect/{_INSTANCE_ID}/soloist-data",
        "--cache-dir",
        f"/fake/cache/{_INSTANCE_ID}/soloist-cache",
        "--cache-size",
        str(CACHE_SIZE_MB),
        "--initial-volume",
        "100",
        "--ws",
        "127.0.0.1:0",
    ]
    assert kwargs["name"] == "soloist[Spotify Test]"
    # the daemon logs to stdout; stderr is merged in so nothing is left uncaptured
    assert kwargs["stdout"] is True
    assert kwargs["stderr"] is asyncio.subprocess.STDOUT
    assert kwargs["env"] == {"PULSE_SERVER": "unix:/fake/native", "PULSE_SINK": "sink1"}
    assert all(_API_KEY not in record.getMessage() for record in caplog.records)


async def test_exit_code_10_refreshes_binary_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build-expired exit (code 10) refreshes the binary and restarts with it."""
    backend, _events = _runner_backend()
    monkeypatch.setattr(soloist_backend, "RESTART_DELAY_S", 0)
    refreshed: list[bool] = []

    class _FakeManager:
        """Fake binary manager serving a replacement build."""

        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        def diagnostics(self) -> dict[str, Any]:
            """Report the replacement build's digest."""
            return {"installed": True, "sha256": "sha-v2"}

        async def ensure_fresh(self, consent: bool, *, force: bool = False) -> Path:
            """Serve the replacement build."""
            refreshed.append(force)
            return Path("/fake/bin/soloist-v2")

    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _FakeManager)
    # stop the supervisor once the restarted (second) daemon has run
    spawned = _patch_spawn(
        monkeypatch,
        [
            _FakeProc(exit_code=10),
            _FakeProc(exit_code=0, on_close=lambda: setattr(backend, "_stop_called", True)),
        ],
    )

    await backend._daemon_runner()

    assert refreshed == [True]
    assert len(spawned) == 2
    # the restarted daemon runs the freshly installed binary
    assert spawned[1][0][0] == "/fake/bin/soloist-v2"


async def test_exit_code_10_with_failed_refresh_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no replacement build exists for an expired one, the backend fails fatally."""
    backend, events = _runner_backend()
    monkeypatch.setattr(soloist_backend, "RESTART_DELAY_S", 0)

    class _ExpiredManager:
        """Fake binary manager that cannot replace the expired build."""

        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        async def ensure_fresh(self, consent: bool, *, force: bool = False) -> Path:
            """Fail the refresh."""
            raise BuildExpiredError("expired")

    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _ExpiredManager)
    spawned = _patch_spawn(monkeypatch, [_FakeProc(exit_code=10)])

    await backend._daemon_runner()

    assert len(spawned) == 1  # the expired build is never restarted
    assert events[-1].type is BackendEventType.FATAL_ERROR
    assert "expired" in (events[-1].error or "")


async def test_five_daemon_failures_report_fatal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After five consecutive daemon failures the backend reports a fatal error."""
    backend, events = _runner_backend()
    monkeypatch.setattr(soloist_backend, "RESTART_DELAY_S", 0)
    procs = [_FakeProc(exit_code=1, start_error=RuntimeError("spawn failed")) for _ in range(5)]
    spawned = _patch_spawn(monkeypatch, procs)

    await backend._daemon_runner()

    assert len(spawned) == 5
    assert sum(1 for e in events if e.type is BackendEventType.CONNECTION_LOST) == 5
    assert events[-1].type is BackendEventType.FATAL_ERROR


@pytest.mark.parametrize(
    ("data", "expected_type"),
    [
        pytest.param(
            SoloistAuthState(logged_in=False, is_active=False),
            BackendEventType.SESSION_INACTIVE,
            id="auth_state-initial-logged_out",
        ),
        pytest.param(
            SoloistAuthState(logged_in=True, is_active=True),
            BackendEventType.SESSION_ACTIVE,
            id="auth_state-active",
        ),
        pytest.param(
            SoloistAuthState(logged_in=True, is_active=False),
            BackendEventType.SESSION_INACTIVE,
            id="auth_state-inactive",
        ),
        pytest.param(
            SoloistDeviceChanged(is_active=True),
            BackendEventType.SESSION_ACTIVE,
            id="device_changed-active",
        ),
        pytest.param(
            SoloistDeviceChanged(is_active=False),
            BackendEventType.SESSION_INACTIVE,
            id="device_changed-inactive",
        ),
        pytest.param(
            SoloistPlaybackState(status="playing"), BackendEventType.PLAYING, id="status-playing"
        ),
        pytest.param(
            SoloistPlaybackState(status="paused"), BackendEventType.PAUSED, id="status-paused"
        ),
        pytest.param(
            SoloistPlaybackState(status="buffering"),
            BackendEventType.BUFFERING,
            id="status-buffering",
        ),
        pytest.param(
            SoloistPlaybackState(status="idle"), BackendEventType.STOPPED, id="status-idle"
        ),
        pytest.param(
            SoloistPlaybackState(status="stopped"), BackendEventType.STOPPED, id="status-stopped"
        ),
        pytest.param(
            SoloistPlaybackState(status="warping"), BackendEventType.OTHER, id="status-unknown"
        ),
        pytest.param(
            SoloistErrorMessage(message="boom"), BackendEventType.ERROR, id="error-message"
        ),
        pytest.param(
            SoloistOptionsChanged(options=SoloistPlaybackOptions()),
            BackendEventType.OPTIONS_CHANGED,
            id="options_changed",
        ),
        pytest.param(SoloistQueueChanged(), BackendEventType.QUEUE_CHANGED, id="queue_changed"),
        pytest.param(
            SoloistCommandResult(command="pause"), BackendEventType.OTHER, id="command_result"
        ),
        pytest.param(None, BackendEventType.OTHER, id="unknown-event"),
    ],
)
async def test_event_adaptation(data: Any, expected_type: BackendEventType) -> None:
    """Every documented soloist event maps onto its normalized counterpart."""
    backend, events = _make_backend()

    await backend._handle_event(_event("test_event", data))

    assert [event.type for event in events] == [expected_type]


async def test_auth_required_only_after_login_loss() -> None:
    """AUTH_REQUIRED is only emitted when an established login is lost, not before pairing."""
    backend, events = _make_backend()

    # a fresh daemon reports logged_in=False while advertising for pairing
    await backend._handle_event(
        _event("auth_state", SoloistAuthState(logged_in=False, is_active=False))
    )
    await backend._handle_event(
        _event("auth_state", SoloistAuthState(logged_in=True, is_active=True))
    )
    await backend._handle_event(
        _event("auth_state", SoloistAuthState(logged_in=False, is_active=False))
    )

    assert [event.type for event in events] == [
        BackendEventType.SESSION_INACTIVE,
        BackendEventType.SESSION_ACTIVE,
        BackendEventType.AUTH_REQUIRED,
    ]


async def test_error_event_carries_message() -> None:
    """An error event forwards the daemon's message on the normalized event."""
    backend, events = _make_backend()

    await backend._handle_event(
        _event("error", SoloistErrorMessage(message="command requires authentication"))
    )

    assert events[0].type is BackendEventType.ERROR
    assert events[0].error == "command requires authentication"


async def test_track_changed_maps_decorations() -> None:
    """A track_changed event maps the entity decorations onto normalized metadata."""
    backend, events = _make_backend()
    item = SoloistEntity(
        uri="spotify:track:t1",
        entity_type="track",
        # shape captured from a real soloist 1.3.7 playback_state payload
        decorations={
            "identity": {"name": "My Song"},
            "playback": {"duration_ms": 210999, "content_ratings": []},
            "creators": [
                {
                    "entity": {
                        "uri": "spotify:artist:a1",
                        "entity_type": "artist",
                        "decorations": {"identity": {"name": "Main Artist"}},
                    }
                },
                {
                    "entity": {
                        "uri": "spotify:artist:a2",
                        "entity_type": "artist",
                        "decorations": {"identity": {"name": "Feat Artist"}},
                    }
                },
            ],
            "parent": {
                "entity": {
                    "uri": "spotify:album:al1",
                    "entity_type": "album",
                    "decorations": {"identity": {"name": "The Album"}},
                }
            },
            "visual_identity": {
                "cover": [
                    {"url": "http://img.invalid/small.jpg", "size": "small"},
                    {"url": "http://img.invalid/c.jpg", "size": "large"},
                    {"url": "http://img.invalid/xl.jpg", "size": "xlarge"},
                ]
            },
        },
    )

    await backend._handle_event(_event("track_changed", SoloistTrackChanged(item=item)))

    event = events[0]
    assert event.type is BackendEventType.METADATA
    assert event.track_uri == "spotify:track:t1"
    metadata = event.metadata
    assert metadata is not None
    assert metadata.track_uri == "spotify:track:t1"
    assert metadata.title == "My Song"
    assert metadata.artist == "Main Artist"
    assert metadata.album == "The Album"
    assert metadata.image_url == "http://img.invalid/c.jpg"
    assert metadata.duration == 210
    assert metadata.position == 0


async def test_track_changed_with_sparse_decorations() -> None:
    """Undecorated entities still produce a METADATA event with only the uri set."""
    backend, events = _make_backend()
    item = SoloistEntity(uri="spotify:track:t2", entity_type="track")

    await backend._handle_event(_event("track_changed", SoloistTrackChanged(item=item)))

    metadata = events[0].metadata
    assert metadata is not None
    assert metadata.track_uri == "spotify:track:t2"
    assert metadata.title is None
    assert metadata.artist is None
    assert metadata.album is None
    assert metadata.image_url is None
    assert metadata.duration is None


async def test_track_changed_without_item_is_other() -> None:
    """A track_changed event without an item degrades to OTHER."""
    backend, events = _make_backend()

    await backend._handle_event(_event("track_changed", SoloistTrackChanged(item=None)))

    assert events[0].type is BackendEventType.OTHER
    assert events[0].metadata is None


async def test_position_sync_maps_to_seconds() -> None:
    """A position_sync event carries the position in whole seconds."""
    backend, events = _make_backend()

    await backend._handle_event(
        _event(
            "position_sync",
            SoloistPositionSync(
                position=SoloistPosition(position_ms=45999, timestamp_ms=1, speed=1.0)
            ),
        )
    )

    assert events[0].type is BackendEventType.POSITION
    assert events[0].position == 45


async def test_queue_changed_maps_entries() -> None:
    """A queue_changed event maps its entries to normalized uid/uri/source/name entries."""
    backend, events = _make_backend()
    previous = [
        SoloistQueueEntry(
            uid="p1",
            source="context",
            item=SoloistEntity(
                uri="spotify:track:t0",
                entity_type="track",
                decorations={"identity": {"name": "Played Song"}},
            ),
        ),
    ]
    upcoming = [
        SoloistQueueEntry(
            uid="u1",
            source="queue",
            item=SoloistEntity(
                uri="spotify:track:t1",
                entity_type="track",
                decorations={"identity": {"name": "Queued Song"}},
            ),
        ),
        # a name-less entry is tolerated (decorations is an extensible bag)
        SoloistQueueEntry(
            uid="u2",
            source="autoplay",
            item=SoloistEntity(uri="spotify:track:t2", entity_type="track"),
        ),
        # the title fallback used for track metadata applies to queue entries too
        SoloistQueueEntry(
            uid="u3",
            source="new_source_kind",
            item=SoloistEntity(
                uri="spotify:track:t3",
                entity_type="track",
                decorations={"identity": {"title": "Titled Song"}},
            ),
        ),
        # entries without a resolvable uri are skipped
        SoloistQueueEntry(uid="u4", source="autoplay", item=None),
        SoloistQueueEntry(
            uid="u5", source="autoplay", item=SoloistEntity(uri="", entity_type="track")
        ),
    ]

    await backend._handle_event(
        _event("queue_changed", SoloistQueueChanged(previous=previous, upcoming=upcoming))
    )

    event = events[0]
    assert event.type is BackendEventType.QUEUE_CHANGED
    queue = event.queue
    assert queue is not None
    assert [(e.uid, e.uri, e.source, e.name) for e in queue.previous] == [
        ("p1", "spotify:track:t0", QueueEntrySource.CONTEXT, "Played Song"),
    ]
    assert [(e.uid, e.uri, e.source, e.name) for e in queue.upcoming] == [
        ("u1", "spotify:track:t1", QueueEntrySource.QUEUE, "Queued Song"),
        ("u2", "spotify:track:t2", QueueEntrySource.AUTOPLAY, None),
        # an unrecognized source value degrades to UNKNOWN instead of raising
        ("u3", "spotify:track:t3", QueueEntrySource.UNKNOWN, "Titled Song"),
    ]


async def test_options_changed_maps_shuffle_and_repeat() -> None:
    """An options_changed event carries the session's shuffle and repeat state."""
    backend, events = _make_backend()

    await backend._handle_event(
        _event(
            "options_changed",
            SoloistOptionsChanged(options=SoloistPlaybackOptions(shuffle=True, repeat="context")),
        )
    )

    event = events[0]
    assert event.type is BackendEventType.OPTIONS_CHANGED
    assert event.options is not None
    assert event.options.shuffle is True
    assert event.options.repeat is RepeatMode.ALL


async def test_playback_state_options_emit_options_changed_precursor() -> None:
    """A playback_state carrying options emits OPTIONS_CHANGED before the state event."""
    backend, events = _make_backend()

    await backend._handle_event(
        _event(
            "playback_state",
            SoloistPlaybackState(
                status="playing", options=SoloistPlaybackOptions(shuffle=True, repeat="track")
            ),
        )
    )

    assert [e.type for e in events] == [
        BackendEventType.OPTIONS_CHANGED,
        BackendEventType.PLAYING,
    ]
    options = events[0].options
    assert options is not None
    assert options.shuffle is True
    assert options.repeat is RepeatMode.ONE
    # the state event itself carries no options; OPTIONS_CHANGED is the one channel
    assert events[1].options is None


async def test_unknown_repeat_vocabulary_degrades_to_unknown() -> None:
    """An unrecognized repeat value from the wire maps to RepeatMode.UNKNOWN."""
    backend, events = _make_backend()

    await backend._handle_event(
        _event(
            "options_changed",
            SoloistOptionsChanged(options=SoloistPlaybackOptions(repeat="context_repeat")),
        )
    )

    assert events[0].options is not None
    assert events[0].options.repeat is RepeatMode.UNKNOWN


async def test_uri_cache_feeds_all_events() -> None:
    """Context/track uris from state events feed every later normalized event."""
    backend, events = _make_backend()
    state = SoloistPlaybackState(
        status="playing",
        item=SoloistEntity(uri="spotify:track:t1", entity_type="track"),
        context=SoloistEntity(uri="spotify:playlist:ctx", entity_type="playlist"),
    )

    await backend._handle_event(_event("playback_state", state))
    await backend._handle_event(
        _event(
            "position_sync",
            SoloistPositionSync(
                position=SoloistPosition(position_ms=1000, timestamp_ms=1, speed=1.0)
            ),
        )
    )

    # the unseen track first yields its metadata, then the playback event
    assert events[0].type is BackendEventType.METADATA
    assert events[1].type is BackendEventType.PLAYING
    assert events[1].context_uri == "spotify:playlist:ctx"
    assert events[1].track_uri == "spotify:track:t1"
    # the position event does not carry uris itself; the cache fills them in
    assert events[2].type is BackendEventType.POSITION
    assert events[2].context_uri == "spotify:playlist:ctx"
    assert events[2].track_uri == "spotify:track:t1"


async def test_event_resets_restart_counter() -> None:
    """A delivered event proves the daemon is healthy and resets the failure counter."""
    backend, _events = _make_backend()
    backend._restart_error_count = 3

    await backend._handle_event(_event("command_result", SoloistCommandResult(command="pause")))

    assert backend._restart_error_count == 0


async def test_get_stream_source_named_pipe_with_readrate_pacing() -> None:
    """The stream source is the sink FIFO as a named pipe, paced by ffmpeg readrate."""
    backend, _events = _make_backend()
    server: Any = _FakeServer()
    sink: Any = _FakeSink()
    backend._server = server
    backend._sink = sink
    backend._sink_generation = server.generation

    source = await backend.get_stream_source()

    assert source.stream_type is StreamType.NAMED_PIPE
    assert source.path == str(sink.fifo_path)
    assert source.extra_input_args == ["-readrate", "1", "-readrate_initial_burst", "0.5"]


async def test_get_stream_source_stale_generation_raises_without_recovery() -> None:
    """A stale sink fails the (side-effect-free) stream request; recovery is not run."""
    backend, _events = _make_backend()
    server: Any = _FakeServer()
    server.generation = 3
    sink: Any = _FakeSink("old")
    proc: Any = _FakeProc()
    backend._server = server
    backend._sink = sink
    backend._sink_generation = 2  # the pulse daemon restarted since sink creation
    backend._proc = proc

    with pytest.raises(AudioError, match="not available"):
        await backend.get_stream_source()

    # pure read: nothing was unloaded, closed or flagged for respawn
    assert sink.unloaded == 0
    assert backend._sink is sink
    assert proc.closed == 0
    assert backend._respawn_requested is False


async def test_generation_watcher_recovers_stale_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher notices a pulse daemon restart and drops sink + daemon for rebuild."""
    backend, _events = _runner_backend()
    monkeypatch.setattr(soloist_backend, "GENERATION_WATCH_INTERVAL_S", 0)
    server: Any = backend._server
    sink: Any = backend._sink
    proc: Any = _FakeProc()
    backend._proc = proc
    watcher = asyncio.get_running_loop().create_task(backend._generation_watcher())
    # a fresh generation passes several watch cycles untouched
    for _ in range(5):
        await asyncio.sleep(0)
    assert sink.unloaded == 0

    server.generation += 1
    async with asyncio.timeout(1.0):
        while proc.closed == 0:
            await asyncio.sleep(0)

    # the sink is dropped; the daemon supervisor recreates it before the respawn
    assert sink.unloaded == 1
    assert backend._sink is None
    assert backend._respawn_requested is True
    watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher


async def test_concurrent_ensure_fresh_sink_creates_single_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent supervisor calls replace a stale sink exactly once."""
    backend, _events = _make_backend()
    server: Any = _FakeServer()
    server.generation = 5
    old_sink: Any = _FakeSink("old")
    backend._server = server
    backend._sink = old_sink
    backend._sink_generation = 4  # stale: the pulse daemon restarted
    created: list[Any] = []
    gate = asyncio.Event()

    async def _create(_server: Any, _prefix: str) -> Any:
        await gate.wait()
        sink = _FakeSink("new")
        created.append(sink)
        return sink

    monkeypatch.setattr(soloist_backend, "PipeSink", SimpleNamespace(create=_create))
    loop = asyncio.get_running_loop()
    task1 = loop.create_task(backend._ensure_fresh_sink())
    task2 = loop.create_task(backend._ensure_fresh_sink())
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    sink1, sink2 = await asyncio.gather(task1, task2)

    assert len(created) == 1
    assert sink1 is sink2 is created[0]
    assert old_sink.unloaded == 1


async def test_get_stream_source_after_stop_raises_clean_error() -> None:
    """get_stream_source on a stopped backend raises AudioError, not AssertionError."""
    backend, _events = _make_backend()
    server: Any = _FakeServer()
    backend._server = server
    await backend.stop()

    with pytest.raises(AudioError, match="not available"):
        await backend.get_stream_source()


async def test_spawn_resets_stale_volume_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A freshly spawned daemon starts at 100%: stale volume state and sink gain are reset."""
    backend, _events = _runner_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    backend._spotify_volume = 25  # stale from before a crash (sink compensating at 400%)
    sink: Any = backend._sink
    proc = _FakeProc(on_close=lambda: setattr(backend, "_stop_called", True))
    _patch_spawn(monkeypatch, [proc])

    await backend._daemon_runner()

    assert backend._spotify_volume == 100
    assert sink.volumes == [100]


async def test_failed_unity_reset_fails_closed() -> None:
    """A failed unity reset drops sink and daemon: a stale gain must never clip audio."""
    backend, _events = _runner_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = backend._sink
    sink.set_volume_error = RuntimeError("pulse gone")
    proc: Any = _FakeProc()
    backend._proc = proc

    await backend._reset_volume_state(sink)

    assert sink.unloaded == 1
    assert backend._sink is None
    assert backend._respawn_requested is True
    assert proc.closed == 1


async def test_failed_compensation_fails_closed_and_suppresses_volume_event() -> None:
    """A failed compensation set recovers sink + daemon and never forwards the VOLUME event."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = _FakeSink()
    sink.set_volume_error = RuntimeError("pulse gone")
    proc: Any = _FakeProc()
    backend._sink = sink
    backend._proc = proc

    await backend._handle_event(_volume_event(50))

    # the player must not adopt a volume whose compensation is unknown
    assert events == []
    assert sink.unloaded == 1
    assert backend._sink is None
    assert backend._respawn_requested is True
    assert proc.closed == 1


async def test_failed_compensation_drops_the_playback_snapshot() -> None:
    """A snapshot whose volume resync triggered recovery is not forwarded as playback state."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = _FakeSink()
    sink.set_volume_error = RuntimeError("pulse gone")
    proc: Any = _FakeProc()
    backend._sink = sink
    backend._proc = proc

    await backend._handle_event(
        _event("playback_state", SoloistPlaybackState(status="playing", volume=50))
    )

    # no PLAYING against the torn-down sink; the respawned daemon reports fresh state
    assert events == []
    assert backend._sink is None
    assert proc.closed == 1


async def test_binary_refresh_loop_respawns_on_new_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daily refresh survives failures and restarts the daemon once a new build lands."""
    backend, _events = _runner_backend()
    monkeypatch.setattr(soloist_backend, "BINARY_REFRESH_INTERVAL_S", 0)
    proc: Any = _FakeProc()
    backend._proc = proc
    backend._build_sha = "sha-old"
    checks: list[bool] = []
    sha = {"value": "sha-old"}

    class _FakeManager:
        """Fake binary manager: fails once, idles once, then installs a new build."""

        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        def diagnostics(self) -> dict[str, Any]:
            """Report the currently installed build's digest."""
            return {"installed": True, "sha256": sha["value"]}

        async def ensure_fresh(self, consent: bool, *, force: bool = False) -> Path:
            """Fail the first check, keep the build on the second, replace it on the third."""
            checks.append(consent)
            if len(checks) == 1:
                raise OSError("cdn offline")
            if len(checks) >= 3:
                # a replacement build installs onto the SAME path; only the
                # install metadata's digest changes
                sha["value"] = "sha-new"
            return Path("/fake/bin/soloist")

    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _FakeManager)
    loop_task = asyncio.get_running_loop().create_task(backend._binary_refresh_loop())

    async with asyncio.timeout(1.0):
        while proc.closed == 0:
            await asyncio.sleep(0)
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task

    # failed check + unchanged check passed without a respawn; the changed
    # digest triggered exactly one intentional daemon restart
    assert len(checks) >= 3
    assert all(checks)  # ensure_fresh is always called with the consent flag


async def test_binary_refresh_loop_picks_up_sibling_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build installed by a sibling instance onto the shared path still triggers a respawn."""
    backend, _events = _runner_backend()
    monkeypatch.setattr(soloist_backend, "BINARY_REFRESH_INTERVAL_S", 0)
    proc: Any = _FakeProc()
    backend._proc = proc
    # this instance spawned its daemon from the old build; a sibling instance
    # already replaced the shared install before this loop's first check
    backend._build_sha = "sha-old"

    class _FakeManager:
        """Fake binary manager whose shared install was updated by a sibling."""

        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        def diagnostics(self) -> dict[str, Any]:
            """Report the sibling-installed build's digest."""
            return {"installed": True, "sha256": "sha-new"}

        async def ensure_fresh(self, consent: bool, *, force: bool = False) -> Path:
            """Return the (already fresh) shared install path."""
            return Path("/fake/bin/soloist")

    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _FakeManager)
    loop_task = asyncio.get_running_loop().create_task(backend._binary_refresh_loop())

    async with asyncio.timeout(1.0):
        while proc.closed == 0:
            await asyncio.sleep(0)
    loop_task.cancel()
    with suppress(asyncio.CancelledError):
        await loop_task

    assert backend._build_sha == "sha-new"
    assert proc.closed == 1
    assert backend._respawn_requested is True
    assert backend._binary == Path("/fake/bin/soloist")


async def test_stdout_redacts_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The api key is redacted from daemon stdout lines before they are logged."""
    backend, _events = _runner_backend()
    proc = _FakeProc(
        stdout_lines=[f"argv: --api-key {_API_KEY}"],
        on_close=lambda: setattr(backend, "_stop_called", True),
    )
    _patch_spawn(monkeypatch, [proc])

    with caplog.at_level(logging.DEBUG):
        await backend._daemon_runner()

    assert all(_API_KEY not in record.getMessage() for record in caplog.records)
    assert any("<redacted>" in record.getMessage() for record in caplog.records)


async def test_daemon_runner_does_not_wait_for_the_log_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A log reader that never ends must not hold up the daemon supervisor.

    AsyncProcess.close() takes the stream lock and keeps it, so a reader parked
    mid-line when another supervisor closes the daemon (sink replacement, binary
    refresh) never reaches EOF. The runner therefore has to key off the process
    exit, not off its own reader.
    """

    class _StuckReaderProc(_FakeProc):
        """A daemon whose output reader never ends, but which does exit."""

        async def iter_stdout(self) -> AsyncGenerator[str]:
            await asyncio.Event().wait()  # never returns, never yields
            yield ""  # pragma: no cover

        async def wait(self) -> int:
            return self._exit_code

    backend, _events = _runner_backend()
    proc = _StuckReaderProc(on_close=lambda: setattr(backend, "_stop_called", True))
    _patch_spawn(monkeypatch, [proc])
    # a short drain leaves the outer deadline real headroom, so the assertion is
    # about the runner returning rather than about which timeout fires first
    monkeypatch.setattr(soloist_backend, "DAEMON_LOG_DRAIN_TIMEOUT_S", 0.1)

    # the supervisor must return on its own; a hang here is the regression
    async with asyncio.timeout(5):
        await backend._daemon_runner()

    assert proc.closed == 1


async def test_daemon_runner_drains_buffered_log_after_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Output still buffered when the daemon exits is logged, not dropped.

    A daemon that fails at startup writes its reason and exits within
    milliseconds, so dropping the reader the moment the process ends throws
    away exactly the output that explains the failure.
    """

    class _BufferedProc(_FakeProc):
        """A daemon that has already exited with its output still queued."""

        async def wait(self) -> int:
            return self._exit_code

        async def iter_stdout(self) -> AsyncGenerator[str]:
            for line in self._stdout_lines:
                await asyncio.sleep(0)  # the reader cannot drain it all in one step
                yield line

    backend, _events = _runner_backend()
    proc = _BufferedProc(
        stdout_lines=[f"buffered line {index}" for index in range(20)],
        on_close=lambda: setattr(backend, "_stop_called", True),
    )
    _patch_spawn(monkeypatch, [proc])

    with caplog.at_level(logging.DEBUG):
        await backend._daemon_runner()

    logged = [record.getMessage() for record in caplog.records]
    assert sum("buffered line" in message for message in logged) == 20


async def test_intentional_respawn_skips_failure_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sink recovery respawn restarts immediately and never counts as a failure."""
    backend, _events = _runner_backend()
    server: Any = backend._server
    proc1 = _FakeProc(block_stdout=True)
    proc2 = _FakeProc(block_stdout=True)
    spawned = _patch_spawn(monkeypatch, [proc1, proc2])
    new_sink: Any = _FakeSink("new")
    monkeypatch.setattr(
        soloist_backend, "PipeSink", SimpleNamespace(create=AsyncMock(return_value=new_sink))
    )
    runner = asyncio.get_running_loop().create_task(backend._daemon_runner())
    async with asyncio.timeout(1.0):
        while backend._proc is None:
            await asyncio.sleep(0)

    # the pulse daemon restarted: the recovery routine (as run by the watcher)
    # drops the sink and intentionally closes the running daemon
    server.generation += 1
    await backend._recover_sink()
    assert proc1.closed >= 1
    # the supervisor recreates the sink and respawns without the restart delay
    # (no RESTART_DELAY patch: a counted failure would make this wait time out)
    async with asyncio.timeout(1.0):
        while len(spawned) < 2:
            await asyncio.sleep(0)

    source = await backend.get_stream_source()
    assert source.path == str(new_sink.fifo_path)
    assert backend._sink is new_sink
    assert backend._sink_generation == server.generation
    assert backend._restart_error_count == 0
    assert backend._respawn_requested is False
    backend._stop_called = True
    await proc2.close()
    await runner


async def test_playback_state_volume_resyncs_compensation() -> None:
    """A playback_state carrying a volume resyncs the sink before the playback event."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = _FakeSink()
    backend._sink = sink

    await backend._handle_event(
        _event("playback_state", SoloistPlaybackState(status="playing", volume=50))
    )

    assert sink.volumes == [200.0]
    assert [(event.type, event.volume) for event in events] == [
        (BackendEventType.VOLUME, 50),
        (BackendEventType.PLAYING, None),
    ]

    # an unchanged volume on the next snapshot is not re-applied
    await backend._handle_event(
        _event("playback_state", SoloistPlaybackState(status="paused", volume=50))
    )
    assert sink.volumes == [200.0]
    assert events[-1].type is BackendEventType.PAUSED


async def test_playback_state_volume_pins_in_player_only() -> None:
    """player_only: an off-100 playback_state volume re-pins the daemon, no VOLUME event."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_PLAYER_ONLY)
    client = AsyncMock()
    backend._client = client
    sink: Any = _FakeSink()
    backend._sink = sink

    await backend._handle_event(
        _event("playback_state", SoloistPlaybackState(status="playing", volume=80))
    )

    client.set_volume.assert_awaited_once_with(100)
    assert [event.type for event in events] == [BackendEventType.PLAYING]


async def test_failed_pin_marks_volume_unknown_and_retries() -> None:
    """player_only: a failed 100% pin is retried by a snapshot reporting the same volume."""
    backend, _events = _make_backend(volume_mode=VOLUME_MODE_PLAYER_ONLY)
    client = AsyncMock()
    client.set_volume.side_effect = [OSError("ws down"), None]
    backend._client = client

    await backend._handle_event(
        _event("playback_state", SoloistPlaybackState(status="playing", volume=80))
    )
    assert backend._spotify_volume is None
    # the reconnect snapshot reports the unchanged volume; the pin is retried
    await backend._handle_event(
        _event("playback_state", SoloistPlaybackState(status="paused", volume=80))
    )
    assert client.set_volume.await_count == 2


async def test_playback_state_snapshot_emits_metadata_for_unseen_track() -> None:
    """A snapshot carrying an unseen track emits its metadata; a repeat does not."""
    backend, events = _make_backend()
    state = SoloistPlaybackState(
        status="playing",
        item=SoloistEntity(uri="spotify:track:t1", entity_type="track"),
    )

    await backend._handle_event(_event("playback_state", state))
    await backend._handle_event(_event("playback_state", state))

    assert [event.type for event in events] == [
        BackendEventType.METADATA,
        BackendEventType.PLAYING,
        BackendEventType.PLAYING,
    ]
    assert events[0].metadata is not None
    assert events[0].metadata.track_uri == "spotify:track:t1"


def test_sink_prefix_is_sanitized() -> None:
    """Characters unsafe for PA sink names are stripped from the instance id."""
    backend, _events = _make_backend(instance_id="weird id!*")

    assert backend._sink_prefix == "weird_id__"


def test_audio_formats_report_the_capture_pcm() -> None:
    """Display and decoded format both report the fixed capture PCM (s32le/44.1/2)."""
    backend, _events = _make_backend()

    for fmt in (backend.audio_format, backend.decoded_audio_format):
        assert fmt.content_type is ContentType.PCM_S32LE
        assert fmt.sample_rate == 44100
        assert fmt.bit_depth == 32
        assert fmt.channels == 2
    assert backend.get_audio_reader() is None


async def test_transport_commands_map_to_client() -> None:
    """Transport commands map 1:1 onto the SoloistClient methods."""
    backend, _events = _make_backend()
    client = AsyncMock()
    backend._client = client

    await backend.play("spotify:album:x", skip_to_uri="spotify:track:y")
    # play claims active device status first (Connect transfer), then plays
    client.activate.assert_awaited_once_with(await_result=True)
    client.play.assert_awaited_once_with("spotify:album:x")
    call_names = [name for name, _args, _kwargs in client.mock_calls]
    assert call_names.index("activate") < call_names.index("play")

    # resume also re-claims active device status first
    client.reset_mock()
    await backend.resume()
    client.activate.assert_awaited_once_with(await_result=True)
    client.resume.assert_awaited_once_with()
    await backend.pause()
    client.pause.assert_awaited_once_with()
    await backend.next()
    client.skip_next.assert_awaited_once_with()
    await backend.previous()
    client.skip_prev.assert_awaited_once_with()
    await backend.seek(30000)
    client.seek.assert_awaited_once_with(30000)

    # deactivate pauses first (position preserved), then gives up the device
    client.reset_mock()
    await backend.deactivate()
    client.pause.assert_awaited_once_with(await_result=True)
    client.deactivate.assert_awaited_once_with()
    call_names = [name for name, _args, _kwargs in client.mock_calls]
    assert call_names.index("pause") < call_names.index("deactivate")


async def test_queue_commands_map_to_client() -> None:
    """The queue-session verbs pass through to the SoloistClient."""
    backend, _events = _make_backend()
    client = AsyncMock()
    backend._client = client

    assert backend.supports_queue_control is True

    await backend.add_to_queue("spotify:track:t1")
    client.add_to_queue.assert_awaited_once_with("spotify:track:t1")

    await backend.set_shuffle(True)
    client.set_shuffle.assert_awaited_once_with(True)

    # the queue snapshot arrives as a queue_changed event, no ack is awaited
    await backend.request_queue(limit=25)
    client.get_queue.assert_awaited_once_with(25)


@pytest.mark.parametrize(
    ("repeat", "expected_calls"),
    [
        (RepeatMode.OFF, [("set_repeat_track", False), ("set_repeat_context", False)]),
        (RepeatMode.ALL, [("set_repeat_track", False), ("set_repeat_context", True)]),
        (RepeatMode.ONE, [("set_repeat_context", False), ("set_repeat_track", True)]),
    ],
)
async def test_set_repeat_sequences_the_two_flags(
    repeat: RepeatMode, expected_calls: list[tuple[str, bool]]
) -> None:
    """set_repeat disables one repeat flag before enabling the other, awaiting each ack."""
    backend, _events = _make_backend()
    client = AsyncMock()
    backend._client = client

    await backend.set_repeat(repeat)

    assert [(name, args[0]) for name, args, _kwargs in client.mock_calls] == expected_calls
    # each command waits for its ack so the pair cannot race
    assert all(kwargs == {"await_result": True} for _name, _args, kwargs in client.mock_calls)


async def test_set_repeat_rejects_unknown() -> None:
    """set_repeat refuses RepeatMode.UNKNOWN instead of silently disabling repeat."""
    backend, _events = _make_backend()
    client = AsyncMock()
    backend._client = client

    with pytest.raises(ValueError, match="unknown repeat mode"):
        await backend.set_repeat(RepeatMode.UNKNOWN)
    assert client.mock_calls == []


async def test_set_repeat_serializes_concurrent_calls() -> None:
    """Concurrent set_repeat calls cannot interleave their two-command sequences."""
    backend, _events = _make_backend()
    call_order: list[tuple[str, bool]] = []

    async def record(name: str, enabled: bool, **_kwargs: Any) -> None:
        call_order.append((name, enabled))
        await asyncio.sleep(0)  # yield so an unserialized second call could interleave

    client = AsyncMock()
    client.set_repeat_track.side_effect = partial(record, "set_repeat_track")
    client.set_repeat_context.side_effect = partial(record, "set_repeat_context")
    backend._client = client

    await asyncio.gather(backend.set_repeat(RepeatMode.ALL), backend.set_repeat(RepeatMode.ONE))

    assert call_order == [
        ("set_repeat_track", False),
        ("set_repeat_context", True),
        ("set_repeat_context", False),
        ("set_repeat_track", True),
    ]


async def test_player_only_pins_spotify_volume_and_suppresses_events() -> None:
    """player_only: off-100 volume events reset the daemon to 100 and are suppressed."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_PLAYER_ONLY)
    client = AsyncMock()
    backend._client = client

    await backend._handle_event(_volume_event(80))
    client.set_volume.assert_awaited_once_with(100)
    assert events == []  # never forwarded: it would fight the MA player volume

    client.set_volume.reset_mock()
    await backend._handle_event(_volume_event(100))
    client.set_volume.assert_not_awaited()
    assert events == []


async def test_player_only_set_volume_pins_100_once() -> None:
    """player_only: MA volume pushes pin the daemon at 100 and dedupe afterwards."""
    backend, _events = _make_backend(volume_mode=VOLUME_MODE_PLAYER_ONLY)
    client = AsyncMock()
    backend._client = client

    await backend.set_volume(55)
    client.set_volume.assert_awaited_once_with(100)

    # once the daemon confirmed 100 (via its volume event) the pin is deduped
    await backend._handle_event(_volume_event(100))
    client.set_volume.reset_mock()
    await backend.set_volume(70)
    client.set_volume.assert_not_awaited()


@pytest.mark.parametrize(
    ("volume", "sink_pct"),
    [(80, 125.0), (50, 200.0), (25, 400.0), (10, 1000.0), (1, 10000.0)],
)
async def test_sync_spotify_reciprocal_sink_compensation(volume: int, sink_pct: float) -> None:
    """sync_spotify: the sink gain is the reciprocal of the Spotify volume percentage."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = _FakeSink()
    backend._sink = sink

    await backend._handle_event(_volume_event(volume))

    assert sink.volumes == [sink_pct]
    assert [(event.type, event.volume) for event in events] == [(BackendEventType.VOLUME, volume)]


async def test_sync_spotify_zero_volume_silences_sink() -> None:
    """sync_spotify: volume 0 silences the sink (no reciprocal exists) and forwards 0."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = _FakeSink()
    backend._sink = sink

    await backend._handle_event(_volume_event(0))

    assert sink.volumes == [0.0]
    assert [(event.type, event.volume) for event in events] == [(BackendEventType.VOLUME, 0)]


async def test_sync_spotify_set_volume_passes_through() -> None:
    """sync_spotify: MA volume changes go straight to the daemon, not the sink."""
    backend, _events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    client = AsyncMock()
    sink: Any = _FakeSink()
    backend._client = client
    backend._sink = sink

    await backend.set_volume(42)

    client.set_volume.assert_awaited_once_with(42)
    assert sink.volumes == []


async def test_sync_spotify_volume_ops_serialized() -> None:
    """sync_spotify: concurrent volume events apply their sink/forward ops in order."""
    backend, events = _make_backend(volume_mode=VOLUME_MODE_SYNC_SPOTIFY)
    sink: Any = _FakeSink()
    sink.gate = asyncio.Event()
    backend._sink = sink

    task1 = asyncio.get_running_loop().create_task(backend._handle_event(_volume_event(50)))
    task2 = asyncio.get_running_loop().create_task(backend._handle_event(_volume_event(80)))
    for _ in range(5):
        await asyncio.sleep(0)
    # first op parked on the sink gate, second queued on the volume lock
    assert sink.volumes == []
    assert events == []

    sink.gate.set()
    await asyncio.gather(task1, task2)

    assert sink.volumes == [200.0, 125.0]
    assert [(event.type, event.volume) for event in events] == [
        (BackendEventType.VOLUME, 50),
        (BackendEventType.VOLUME, 80),
    ]


async def test_stop_teardown_order_and_idempotency() -> None:
    """stop() tears down events task, daemon task, process, sink, server — exactly once."""
    backend, _events = _make_backend()
    order: list[str] = []

    async def _supervisor(tag: str) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append(tag)
            raise

    class _Proc:
        """Minimal process stub recording its close."""

        returncode: int | None = None

        async def close(self) -> None:
            """Record the close."""
            order.append("proc")

    class _Sink:
        """Minimal sink stub recording its unload."""

        async def unload(self) -> None:
            """Record the unload."""
            order.append("sink")

    class _Server:
        """Minimal capture server stub recording its release."""

        generation = 1

        async def release(self) -> None:
            """Record the release."""
            order.append("server")

    loop = asyncio.get_running_loop()
    backend._events_task = loop.create_task(_supervisor("events"))
    backend._daemon_task = loop.create_task(_supervisor("daemon"))
    await asyncio.sleep(0)  # let the supervisors enter their sleep
    proc: Any = _Proc()
    sink: Any = _Sink()
    server: Any = _Server()
    backend._proc = proc
    backend._sink = sink
    backend._server = server

    await backend.stop()

    assert order == ["events", "daemon", "proc", "sink", "server"]
    assert backend._stop_called is True

    await backend.stop()  # second call must be a no-op
    assert order == ["events", "daemon", "proc", "sink", "server"]


async def test_start_failure_releases_capture_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup failure after acquiring the capture server releases it again."""
    backend, _events = _make_backend(base_dir=tmp_path)

    class _FakeManager:
        def __init__(self, mass: Any) -> None:
            """Accept the mass argument like the real manager."""

        def diagnostics(self) -> dict[str, Any]:
            """Report the installed build's digest."""
            return {"installed": True, "sha256": "sha-1"}

        async def ensure_fresh(self, consent: bool, *, force: bool = False) -> Path:
            """Hand out a fake binary path."""
            return Path("/fake/bin/soloist")

    server: Any = _FakeServer()
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", _FakeManager)
    monkeypatch.setattr(soloist_backend, "get_pulse_capture_server", lambda _mass: server)
    monkeypatch.setattr(
        soloist_backend,
        "PipeSink",
        SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("sink creation failed"))),
    )

    with pytest.raises(RuntimeError, match="sink creation failed"):
        await backend.start()

    # the acquire must be paired with a release despite the aborted startup
    assert server.released


def _prefs_backend(
    tmp_path: Path,
    *,
    crossfade_ms: int,
    normalization: bool,
    audio_quality: str = AUDIO_QUALITY_LOSSLESS,
) -> SoloistBackend:
    """Build a backend with the given audio behavior, rooted in a real tmp data dir."""
    backend, _ = _make_backend(base_dir=tmp_path)
    backend._crossfade_ms = crossfade_ms
    backend._loudness_normalization = normalization
    backend._audio_quality = audio_quality
    backend._data_dir = tmp_path / "soloist-data"
    return backend


def test_audio_prefs_written_to_global_and_per_user(tmp_path: Path) -> None:
    """Managed keys are replaced in the global and every per-user prefs store."""
    backend = _prefs_backend(tmp_path, crossfade_ms=8000, normalization=False)
    settings = backend._data_dir / "settings"
    (settings / "Users" / "alice-user").mkdir(parents=True)
    (settings / "prefs").write_text("core.clock_delta=0\naudio.crossfade_v2=false\n")
    (settings / "Users" / "alice-user" / "prefs").write_text(
        "storage.size=512\naudio.crossfade.time_v2=99\n"
    )

    backend._write_audio_prefs()

    global_prefs = (settings / "prefs").read_text().splitlines()
    user_prefs = (settings / "Users" / "alice-user" / "prefs").read_text().splitlines()
    for prefs in (global_prefs, user_prefs):
        assert "audio.crossfade_v2=true" in prefs
        assert "audio.crossfade.time_v2=8000" in prefs
        assert "audio.normalize_v2=false" in prefs
    # foreign keys survive, replaced stale values do not
    assert "core.clock_delta=0" in global_prefs
    assert "storage.size=512" in user_prefs
    assert "audio.crossfade_v2=false" not in global_prefs
    assert "audio.crossfade.time_v2=99" not in user_prefs


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (AUDIO_QUALITY_NORMAL, 2),
        (AUDIO_QUALITY_HIGH, 3),
        (AUDIO_QUALITY_VERY_HIGH, 4),
        (AUDIO_QUALITY_LOSSLESS, 5),
        # an unknown tier must never reach the prefs file: the engine rejects
        # anything outside 1-5 and silently drops back to ~160 kbps
        ("nonsense", 5),
    ],
)
def test_audio_prefs_quality_tier_mapping(tmp_path: Path, tier: str, expected: int) -> None:
    """Each quality tier writes its bitrate enumeration to both quality keys."""
    backend = _prefs_backend(tmp_path, crossfade_ms=0, normalization=True, audio_quality=tier)

    backend._write_audio_prefs()

    prefs = (backend._data_dir / "settings" / "prefs").read_text().splitlines()
    assert f"audio.play_bitrate_enumeration={expected}" in prefs
    assert f"audio.play_bitrate_non_metered_enumeration={expected}" in prefs
    # without the migration marker the engine derives the non-metered value itself
    assert "audio.play_bitrate_non_metered_migrated=true" in prefs


def test_audio_prefs_replace_a_stale_quality_tier(tmp_path: Path) -> None:
    """A quality value left by a previous run is replaced, not appended to."""
    backend = _prefs_backend(
        tmp_path, crossfade_ms=0, normalization=True, audio_quality=AUDIO_QUALITY_NORMAL
    )
    settings = backend._data_dir / "settings"
    settings.mkdir(parents=True)
    (settings / "prefs").write_text("audio.play_bitrate_non_metered_enumeration=5\n")

    backend._write_audio_prefs()

    prefs = (settings / "prefs").read_text().splitlines()
    assert "audio.play_bitrate_non_metered_enumeration=5" not in prefs
    assert "audio.play_bitrate_non_metered_enumeration=2" in prefs


def test_audio_prefs_crossfade_off_omits_the_time_key(tmp_path: Path) -> None:
    """
    Crossfade off writes crossfade_v2=false and no time key.

    Sub-second time values silently disable crossfade, so the time key may only
    exist while crossfade is enabled.
    """
    backend = _prefs_backend(tmp_path, crossfade_ms=0, normalization=True)

    backend._write_audio_prefs()

    global_prefs = (backend._data_dir / "settings" / "prefs").read_text()
    assert "audio.crossfade_v2=false" in global_prefs
    assert "audio.crossfade.time_v2" not in global_prefs
    assert "audio.normalize_v2=true" in global_prefs


def test_audio_prefs_write_failure_is_non_fatal(tmp_path: Path) -> None:
    """A failing prefs write logs a warning instead of blocking the daemon spawn."""
    backend = _prefs_backend(tmp_path, crossfade_ms=8000, normalization=True)
    backend._data_dir = Path("/proc/no-such-place")

    backend._write_audio_prefs()  # must not raise


def test_audio_prefs_corrupt_file_skips_only_that_store(tmp_path: Path) -> None:
    """
    A prefs file with invalid UTF-8 (truncated write) does not block the spawn.

    Only the corrupt store is skipped; the remaining stores are still updated.
    """
    backend = _prefs_backend(tmp_path, crossfade_ms=8000, normalization=True)
    settings = backend._data_dir / "settings"
    (settings / "Users" / "alice-user").mkdir(parents=True)
    corrupt = b"core.clock_delta=0\naudio.play_bitrate\xc3"
    (settings / "prefs").write_bytes(corrupt)

    backend._write_audio_prefs()  # must not raise

    # the corrupt global store is left untouched, the per-user store is written
    assert (settings / "prefs").read_bytes() == corrupt
    user_prefs = (settings / "Users" / "alice-user" / "prefs").read_text()
    assert "audio.crossfade.time_v2=8000" in user_prefs
