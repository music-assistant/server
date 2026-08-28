"""Tests for the Spotify Connect provider."""

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderType
from music_assistant_models.event import MassEvent
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_CROSSFADE_DURATION
from music_assistant.providers.spotify_connect import (
    BACKEND_SOLOIST,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_SOLOIST_CONSENT,
    CONF_VOLUME_MODE,
    SpotifyConnectProvider,
)
from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_HIGH,
    AUDIO_QUALITY_LOSSLESS,
)
from music_assistant.providers.spotify_connect.go_librespot.backend import (
    API_PORT_RANGE_END,
    API_PORT_RANGE_START,
    GoLibrespotBackend,
)
from music_assistant.providers.spotify_connect.go_librespot.client import GoLibrespotClient
from music_assistant.providers.spotify_connect.models import BackendEvent, BackendEventType
from music_assistant.providers.spotify_connect.provider import (
    CONF_AUDIO_QUALITY,
    CONF_LOUDNESS_NORMALIZATION,
    _PlayerDaemon,
)
from music_assistant.providers.spotify_connect.soloist.backend import (
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
)

# the connected player the tested daemon is bound to; doubles as the source item_id
_PLAYER_ID = "player1"


def _make_daemon(publish_name: str = "Test Speaker") -> _PlayerDaemon:
    """Build a bare daemon state bound to the test player."""
    return _PlayerDaemon(
        player_id=_PLAYER_ID,
        safe_player_id=_PLAYER_ID,
        publish_name=publish_name,
        stream_metadata=StreamMetadata(title=f"Spotify Connect | {publish_name}"),
    )


async def test_backend_start_probes_api_port_on_ipv4_loopback() -> None:
    """The daemon API port is selected on the address go-librespot binds."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.logger = MagicMock()
    backend.mass.create_task.side_effect = lambda coroutine: coroutine.close()

    with (
        patch(
            "music_assistant.providers.spotify_connect.go_librespot.backend"
            ".get_go_librespot_binary",
            return_value="/usr/bin/go-librespot",
        ),
        patch(
            "music_assistant.providers.spotify_connect.go_librespot.backend.select_free_port",
            new=AsyncMock(return_value=38801),
        ) as select_port,
    ):
        await backend.start()

    select_port.assert_awaited_once_with(API_PORT_RANGE_START, API_PORT_RANGE_END, host="127.0.0.1")
    assert backend._client is not None
    assert backend._client.base_url == "http://127.0.0.1:38801"


async def test_daemon_runner_reselects_api_port_when_taken(tmp_path: Path) -> None:
    """An API port taken while the daemon was down is replaced before (re)starting."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.mass.streams.get_source_ip = AsyncMock(return_value="192.168.1.5")
    backend.logger = MagicMock()
    backend.name = "Spotify Test"
    backend.cache_dir = str(tmp_path)
    backend._binary = "/usr/bin/go-librespot"
    backend._api_port = 38800
    backend._client = GoLibrespotClient(backend.mass, "http://127.0.0.1:38800", backend.logger)
    backend._event_callback = AsyncMock()
    # exit the supervisor loop after a single iteration
    backend._stop_called = True
    backend._restart_error_count = 0

    async def _no_stderr() -> AsyncGenerator[str]:
        return
        yield

    proc = MagicMock()
    proc.start = AsyncMock()
    proc.close = AsyncMock()
    proc.iter_stderr = _no_stderr

    with (
        patch(
            "music_assistant.providers.spotify_connect.go_librespot.backend.is_port_in_use",
            new=AsyncMock(return_value=True),
        ) as port_probe,
        patch(
            "music_assistant.providers.spotify_connect.go_librespot.backend.select_free_port",
            new=AsyncMock(return_value=38801),
        ),
        patch(
            "music_assistant.providers.spotify_connect.go_librespot.backend.AsyncProcess",
            return_value=proc,
        ),
        patch.object(GoLibrespotBackend, "_write_config") as write_config,
    ):
        await backend._daemon_runner()

    port_probe.assert_awaited_once_with(38800, host="127.0.0.1")
    assert backend._api_port == 38801
    assert backend._client.base_url == "http://127.0.0.1:38801"
    # the daemon config pins the advertisement to the player-facing interface
    write_config.assert_called_once_with("192.168.1.5")


def _volume_sync_provider(
    volume_level: int | None,
) -> tuple[SpotifyConnectProvider, _PlayerDaemon, AsyncMock]:
    """Build a minimal provider whose linked player reports the given volume."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.mass.players.get_audio_source_session.return_value = MagicMock(
        playback_session_id="playback-session"
    )
    provider.logger = MagicMock()
    daemon = _make_daemon()
    backend = MagicMock()
    set_volume = AsyncMock()
    backend.set_volume = set_volume
    daemon.backend = backend
    provider._daemons = {_PLAYER_ID: daemon}
    player = MagicMock()
    player.state.volume_level = volume_level
    provider.mass.players.get_player.return_value = player
    return provider, daemon, set_volume


async def test_sync_player_volume_pushes_player_volume_to_backend() -> None:
    """The player's volume is pushed to the backend and cached for echo dedupe."""
    provider, daemon, set_volume = _volume_sync_provider(50)

    await provider._sync_player_volume_to_spotify(daemon, "player1")

    set_volume.assert_awaited_once_with(50)
    assert daemon.last_volume_sent == 50


async def test_sync_player_volume_pushes_when_cache_matches() -> None:
    """The push is unconditional: the backend's volume resets between sessions."""
    provider, daemon, set_volume = _volume_sync_provider(50)
    daemon.last_volume_sent = 50

    await provider._sync_player_volume_to_spotify(daemon, "player1")

    set_volume.assert_awaited_once_with(50)


async def test_sync_player_volume_skips_when_volume_unknown() -> None:
    """No push happens when the player does not expose a volume level."""
    provider, daemon, set_volume = _volume_sync_provider(None)

    await provider._sync_player_volume_to_spotify(daemon, "player1")

    set_volume.assert_not_awaited()
    assert daemon.last_volume_sent is None


async def test_sync_player_volume_restores_cache_on_failure() -> None:
    """A failed push restores the dedupe cache so a retry is not wrongly deduped."""
    provider, daemon, set_volume = _volume_sync_provider(50)
    set_volume.side_effect = OSError("daemon gone")

    await provider._sync_player_volume_to_spotify(daemon, "player1")

    assert daemon.last_volume_sent is None


def _tethered_provider() -> tuple[SpotifyConnectProvider, _PlayerDaemon, AsyncMock]:
    """Build a provider tethered to queue 'player1' with an active (paused) Spotify session."""
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
    daemon = _make_daemon()
    backend = MagicMock()
    deactivate = AsyncMock()
    backend.deactivate = deactivate
    daemon.backend = backend
    daemon.active_player_id = "player1"
    daemon.spotify_session_active = True
    provider._daemons = {_PLAYER_ID: daemon}
    return provider, daemon, deactivate


def test_get_player_audio_sources_scopes_to_the_daemon_player() -> None:
    """Each daemon's source is bound to its own connected player only."""
    provider = object.__new__(SpotifyConnectProvider)
    daemon = _make_daemon()
    daemon.audio_source = MagicMock()
    provider._daemons = {_PLAYER_ID: daemon}

    assert provider.get_player_audio_sources(_PLAYER_ID) == [daemon.audio_source]
    assert provider.get_player_audio_sources("other_player") == []


async def test_releasing_a_player_releases_a_paused_spotify_session() -> None:
    """Letting the player go releases the session the paused stream's teardown left behind."""
    provider, _daemon, deactivate = _tethered_provider()

    await provider.on_source_released(_PLAYER_ID, "player1")

    deactivate.assert_awaited_once()


async def test_release_while_the_stream_is_winding_down_still_releases() -> None:
    """
    A release landing before the paused stream finished tearing down still releases.

    The teardown itself releases nothing for a paused source, so waiting for it to hand the
    claim back would leave the Spotify app tethered for good.
    """
    provider, daemon, deactivate = _tethered_provider()
    daemon.in_use_by_player = "player1"

    await provider.on_source_released(_PLAYER_ID, "player1")

    deactivate.assert_awaited_once()


async def test_clearing_another_queue_leaves_the_session_alone() -> None:
    """Only the queue the source is tethered to may release it."""
    provider, _daemon, deactivate = _tethered_provider()

    await provider.on_source_released(_PLAYER_ID, "player2")

    deactivate.assert_not_awaited()


async def test_queue_clear_without_an_active_session_does_nothing() -> None:
    """There is nothing to release when MA is not the active Spotify device."""
    provider, daemon, deactivate = _tethered_provider()
    daemon.spotify_session_active = False

    await provider.on_source_released(_PLAYER_ID, "player1")

    deactivate.assert_not_awaited()


async def _session_inactive(provider: SpotifyConnectProvider, daemon: _PlayerDaemon) -> list[str]:
    """Run the backend's 'session inactive' answer and return the players it wanted stopped."""
    stopped: list[str] = []

    def _record_stop(daemon: _PlayerDaemon, player_id: str) -> None:
        del daemon
        stopped.append(player_id)

    provider._schedule_pause_stop = _record_stop  # type: ignore[method-assign]
    await provider._handle_backend_event(
        daemon, BackendEvent(type=BackendEventType.SESSION_INACTIVE)
    )
    return stopped


async def test_releasing_the_session_leaves_the_new_playback_alone() -> None:
    """
    Releasing must not stop the player that took the source's place.

    The backend answers a release with the same "session inactive" it sends when the user picks
    another device in the Spotify app - and that one does stop the player. By then this player is
    playing whatever replaced the source, so stopping it would cut the music the user just started.
    """
    provider, daemon, _ = _tethered_provider()

    await provider.on_source_released(_PLAYER_ID, "player1")

    assert await _session_inactive(provider, daemon) == []


async def test_a_spotify_side_deselect_still_stops_the_player() -> None:
    """Picking another device in the Spotify app does stop what MA was playing from it."""
    provider, daemon, _ = _tethered_provider()

    assert await _session_inactive(provider, daemon) == ["player1"]


async def test_queue_clear_survives_a_failing_release() -> None:
    """A backend that cannot be reached must not break clearing the queue."""
    provider, _daemon, deactivate = _tethered_provider()
    deactivate.side_effect = OSError("daemon gone")

    await provider.on_source_released(_PLAYER_ID, "player1")

    deactivate.assert_awaited_once()


async def test_a_slow_stop_after_pause_is_reported() -> None:
    """A stop that takes its time is reported, and still runs to completion."""
    stopped = asyncio.Event()

    async def _slow_stop(_player_id: str) -> None:
        await asyncio.sleep(0.05)
        stopped.set()

    provider, _daemon, _ = _tethered_provider()
    mass = cast("Any", provider.mass)
    mass.loop = asyncio.get_running_loop()
    mass.players.cmd_stop = AsyncMock(side_effect=_slow_stop)
    logger = cast("MagicMock", provider.logger)

    with patch("music_assistant.providers.spotify_connect.provider.SLOW_STOP_WARN_S", 0.01):
        await provider._stop_paused_player("player1")

    assert stopped.is_set()
    logger.warning.assert_called_once()


async def test_a_prompt_stop_after_pause_is_not_reported() -> None:
    """A stop that finishes promptly is not reported as slow."""
    provider, _daemon, _ = _tethered_provider()
    mass = cast("Any", provider.mass)
    mass.loop = asyncio.get_running_loop()
    mass.players.cmd_stop = AsyncMock()
    logger = cast("MagicMock", provider.logger)

    await provider._stop_paused_player("player1")

    mass.players.cmd_stop.assert_awaited_once_with("player1")
    logger.warning.assert_not_called()


def _provider_with_stored_config(
    setup_data: dict[str, Any], tmp_path: Path
) -> SpotifyConnectProvider:
    """Build a provider whose stored setup_data resolves through the real accessors."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.mass.storage_path = str(tmp_path / "storage")
    provider.mass.cache_path = str(tmp_path / "cache")
    provider.mass.config.get.return_value = setup_data
    provider.mass.config.decrypt_string.side_effect = lambda value: value
    provider.logger = MagicMock()
    provider.config = ProviderConfig(
        values={},
        type=ProviderType.PLUGIN,
        domain="spotify_connect",
        instance_id="spotify_connect",
        name="Spotify Connect",
    )
    return provider


def test_config_without_backend_choice_loads_go_librespot(tmp_path: Path) -> None:
    """A config from before the backend choice existed loads go-librespot unchanged."""
    provider = _provider_with_stored_config({}, tmp_path)

    backend = provider._create_backend(_make_daemon(), "Player 1")

    assert isinstance(backend, GoLibrespotBackend)


def test_soloist_setup_data_loads_soloist_backend(tmp_path: Path) -> None:
    """A flow-configured soloist instance loads the soloist backend with its stored values."""
    provider = _provider_with_stored_config(
        {
            CONF_BACKEND: BACKEND_SOLOIST,
            CONF_API_KEY: "soloist-api-key-0123456789abcdef",
            CONF_SOLOIST_CONSENT: True,
        },
        tmp_path,
    )
    # the volume mode lives in the provider options, not in the setup data
    provider.config.values[CONF_VOLUME_MODE] = ConfigEntry(
        key=CONF_VOLUME_MODE,
        type=ConfigEntryType.STRING,
        value=VOLUME_MODE_SYNC_SPOTIFY,
    )

    provider.config.values[CONF_CROSSFADE_DURATION] = ConfigEntry(
        key=CONF_CROSSFADE_DURATION,
        type=ConfigEntryType.INTEGER,
        value=8,
    )
    provider.config.values[CONF_LOUDNESS_NORMALIZATION] = ConfigEntry(
        key=CONF_LOUDNESS_NORMALIZATION,
        type=ConfigEntryType.BOOLEAN,
        value=False,
    )
    provider.config.values[CONF_AUDIO_QUALITY] = ConfigEntry(
        key=CONF_AUDIO_QUALITY,
        type=ConfigEntryType.STRING,
        value=AUDIO_QUALITY_HIGH,
    )

    backend = provider._create_backend(_make_daemon(), "Player 1")

    assert isinstance(backend, SoloistBackend)
    assert backend._api_key == "soloist-api-key-0123456789abcdef"
    assert backend._consent is True
    assert backend._volume_mode == VOLUME_MODE_SYNC_SPOTIFY
    assert backend._crossfade_ms == 8000
    assert backend._loudness_normalization is False
    assert backend._audio_quality == AUDIO_QUALITY_HIGH


def test_audio_behavior_defaults_reach_the_backend(tmp_path: Path) -> None:
    """Without stored values, crossfade is off and normalization enabled."""
    provider = _provider_with_stored_config({}, tmp_path)

    backend = provider._create_backend(_make_daemon(), "Player 1")

    assert isinstance(backend, GoLibrespotBackend)
    assert backend._crossfade_ms == 0
    assert backend._loudness_normalization is True
    assert backend._audio_quality == AUDIO_QUALITY_LOSSLESS


def test_audio_behavior_values_reach_the_backend(tmp_path: Path) -> None:
    """The configured crossfade seconds (as ms) and normalization reach the backend."""
    provider = _provider_with_stored_config({}, tmp_path)
    provider.config.values[CONF_CROSSFADE_DURATION] = ConfigEntry(
        key=CONF_CROSSFADE_DURATION,
        type=ConfigEntryType.INTEGER,
        value=8,
    )
    provider.config.values[CONF_LOUDNESS_NORMALIZATION] = ConfigEntry(
        key=CONF_LOUDNESS_NORMALIZATION,
        type=ConfigEntryType.BOOLEAN,
        value=False,
    )
    provider.config.values[CONF_AUDIO_QUALITY] = ConfigEntry(
        key=CONF_AUDIO_QUALITY,
        type=ConfigEntryType.STRING,
        value=AUDIO_QUALITY_HIGH,
    )

    backend = provider._create_backend(_make_daemon(), "Player 1")

    assert isinstance(backend, GoLibrespotBackend)
    assert backend._crossfade_ms == 8000
    assert backend._loudness_normalization is False
    assert backend._audio_quality == AUDIO_QUALITY_HIGH


def test_source_processing_defaults_are_reported(tmp_path: Path) -> None:
    """Spotify reports its default source processing as normalization only."""
    provider = _provider_with_stored_config({}, tmp_path)

    assert provider.delivers_crossfaded_audio(MagicMock()) is False
    assert provider.delivers_normalized_audio(MagicMock()) is True


def test_source_processing_config_is_reported(tmp_path: Path) -> None:
    """Spotify reports the source processing configured for its backend."""
    provider = _provider_with_stored_config({}, tmp_path)
    provider.config.values[CONF_CROSSFADE_DURATION] = ConfigEntry(
        key=CONF_CROSSFADE_DURATION,
        type=ConfigEntryType.INTEGER,
        value=8,
    )
    provider.config.values[CONF_LOUDNESS_NORMALIZATION] = ConfigEntry(
        key=CONF_LOUDNESS_NORMALIZATION,
        type=ConfigEntryType.BOOLEAN,
        value=False,
    )

    assert provider.delivers_crossfaded_audio(MagicMock()) is True
    assert provider.delivers_normalized_audio(MagicMock()) is False


def test_write_config_carries_the_audio_behavior_keys(tmp_path: Path) -> None:
    """The generated config.yml carries crossfade_duration (ms) and normalisation_disabled."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.logger = MagicMock()
    backend._publish_name = "Test Speaker"
    backend._identity_key = "spotify_connect_player1"
    backend._api_port = 38800
    backend.cache_dir = str(tmp_path)
    backend._crossfade_ms = 8000
    backend._loudness_normalization = False
    backend._audio_quality = AUDIO_QUALITY_HIGH

    backend._write_config(None)

    config = json.loads((tmp_path / "config.yml").read_text(encoding="utf-8"))
    assert config["crossfade_duration"] == 8000
    assert config["normalisation_disabled"] is True
    assert config["bitrate"] == 160


def test_write_config_caps_lossless_at_the_engine_maximum(tmp_path: Path) -> None:
    """go-librespot cannot do lossless, so that tier lands on its 320 kbps ceiling."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.logger = MagicMock()
    backend._publish_name = "Test Speaker"
    backend._identity_key = "spotify_connect_player1"
    backend._api_port = 38800
    backend.cache_dir = str(tmp_path)
    backend._crossfade_ms = 0
    backend._loudness_normalization = True
    backend._audio_quality = AUDIO_QUALITY_LOSSLESS

    backend._write_config(None)

    config = json.loads((tmp_path / "config.yml").read_text(encoding="utf-8"))
    assert config["bitrate"] == 320


async def test_soloist_data_dir_matches_the_migration_target(tmp_path: Path) -> None:
    """The per-player soloist data dir is exactly where the migration moves old data to."""
    provider = _provider_with_stored_config(
        {
            CONF_BACKEND: BACKEND_SOLOIST,
            CONF_API_KEY: "soloist-api-key-0123456789abcdef",
            CONF_SOLOIST_CONSENT: True,
        },
        tmp_path,
    )
    provider.manifest = MagicMock()
    provider.manifest.domain = "spotify_connect"
    provider._daemons = {}
    player = MagicMock()
    player.player_id = "player one!"
    player.display_name = "Player One"

    with patch.object(SoloistBackend, "start", new=AsyncMock()):
        await provider._start_daemon(player, "Player One | Music Assistant")

    daemon = provider._daemons["player one!"]
    backend = cast("SoloistBackend", daemon.backend)
    assert backend._data_dir == (
        tmp_path / "storage" / "spotify_connect" / "spotify_connect_player_one_" / "soloist-data"
    )


# --- Daemon reconciliation -----------------------------------------------------


@dataclass
class _ReconcileMocks:
    """The mocked collaborators of a reconcile-test provider."""

    mass: MagicMock
    start_daemon: AsyncMock
    stop_daemon: AsyncMock


def _reconcile_provider(
    assigned: tuple[str, ...],
    registered: dict[str, str],
) -> tuple[SpotifyConnectProvider, _ReconcileMocks]:
    """
    Build a bare provider with the real reconcile logic and mocked daemon control.

    :param assigned: The connected player ids the provider was loaded with.
    :param registered: Currently registered player ids mapped to their display name.
    """
    prov = SpotifyConnectProvider.__new__(SpotifyConnectProvider)
    prov.logger = MagicMock()
    prov.config = MagicMock()
    prov.mass = mass = MagicMock()
    prov._daemons = {}
    prov._failed_player_ids = set()
    prov._reconcile_lock = asyncio.Lock()
    prov._unload_called = False
    prov._unsubscribe = None
    prov._assigned_player_ids = assigned
    prov.get_config_value = MagicMock(return_value="player_mass")  # type: ignore[method-assign]

    def get_player(player_id: str) -> MagicMock | None:
        if player_id not in registered:
            return None
        player = MagicMock()
        player.player_id = player_id
        player.display_name = registered[player_id]
        return player

    mass.players.get_player.side_effect = get_player

    async def start_daemon(player: MagicMock, publish_name: str) -> None:
        # stop_called / active_player_id are spelled out: a bare MagicMock attribute is
        # truthy, which would trip the stopped-daemon guard and the deselect path
        prov._daemons[player.player_id] = MagicMock(
            player_id=player.player_id,
            publish_name=publish_name,
            stop_called=False,
            active_player_id=None,
        )

    start_mock = AsyncMock(side_effect=start_daemon)
    stop_mock = AsyncMock()
    prov._start_daemon = start_mock  # type: ignore[method-assign]
    prov._stop_daemon = stop_mock  # type: ignore[method-assign]
    return prov, _ReconcileMocks(mass=mass, start_daemon=start_mock, stop_daemon=stop_mock)


async def test_reconcile_starts_daemon_when_assigned_player_registers() -> None:
    """A daemon starts only once its connected player has actually registered."""
    registered: dict[str, str] = {}
    prov, mocks = _reconcile_provider(("p1",), registered)

    # cold boot: the player has not registered yet, so nothing starts
    await prov._reconcile()
    mocks.start_daemon.assert_not_awaited()

    registered["p1"] = "Kitchen"
    await prov._reconcile()
    mocks.start_daemon.assert_awaited_once()
    assert mocks.start_daemon.call_args.args[1] == "Kitchen | Music Assistant"
    assert "p1" in prov._daemons


async def test_reconcile_restarts_daemon_on_advertised_name_drift() -> None:
    """A renamed player gets its daemon restarted with the new advertised name."""
    registered = {"p1": "Kitchen"}
    prov, mocks = _reconcile_provider(("p1",), registered)
    await prov._reconcile()
    old_daemon = prov._daemons["p1"]

    # a second pass without changes is a no-op
    await prov._reconcile()
    mocks.stop_daemon.assert_not_awaited()
    assert mocks.start_daemon.await_count == 1

    # a live session on the old daemon is released before the daemon is replaced
    old_daemon.active_player_id = "consumer"
    registered["p1"] = "Cellar"
    await prov._reconcile()
    mocks.stop_daemon.assert_awaited_once_with(old_daemon)
    assert prov._daemons["p1"].publish_name == "Cellar | Music Assistant"
    mocks.mass.players.deselect_source.assert_called_once()
    assert mocks.mass.players.deselect_source.call_args.args[0] == "consumer"


async def test_reconcile_keeps_daemon_for_temporarily_unavailable_player() -> None:
    """A temporarily unregistered player keeps its running daemon (stable identity)."""
    registered = {"p1": "Kitchen"}
    prov, mocks = _reconcile_provider(("p1",), registered)
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    registered.clear()
    await prov._reconcile()
    mocks.stop_daemon.assert_not_awaited()
    assert prov._daemons["p1"] is daemon


async def test_player_removed_event_stops_daemon() -> None:
    """A permanently removed player gets its daemon stopped and dropped."""
    registered = {"p1": "Kitchen"}
    prov, mocks = _reconcile_provider(("p1",), registered)
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="p1"))
    mocks.stop_daemon.assert_awaited_once_with(daemon)
    assert not prov._daemons


async def test_player_added_event_triggers_reconcile() -> None:
    """A player registering (cold boot path) starts its daemon via the event handler."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_ADDED, object_id="p1"))
    mocks.start_daemon.assert_awaited_once()


async def test_loaded_in_mass_with_empty_connected_players_is_idle() -> None:
    """An empty connected-players selection loads the provider fully idle."""
    prov, mocks = _reconcile_provider((), {})

    await prov.loaded_in_mass()
    mocks.mass.subscribe.assert_not_called()
    mocks.start_daemon.assert_not_awaited()
    assert not prov._daemons


async def test_loaded_in_mass_subscribes_to_assigned_players_only() -> None:
    """Player events are only watched for the connected players."""
    prov, mocks = _reconcile_provider(("p1", "p2"), {})

    await prov.loaded_in_mass()
    mocks.mass.subscribe.assert_called_once()
    assert mocks.mass.subscribe.call_args.kwargs["id_filter"] == ("p1", "p2")


async def test_unload_stops_all_daemons() -> None:
    """Unload stops every running daemon and stops watching player events."""
    registered = {"p1": "Kitchen", "p2": "Garage"}
    prov, mocks = _reconcile_provider(("p1", "p2"), registered)
    await prov._reconcile()
    unsubscribe = MagicMock()
    prov._unsubscribe = unsubscribe

    await prov.unload()
    unsubscribe.assert_called_once()
    assert mocks.stop_daemon.await_count == 2
    assert not prov._daemons


async def test_fatal_backend_error_gives_up_only_the_failed_daemon() -> None:
    """A permanently failed backend drops its own daemon and leaves the others running."""
    prov, mocks = _reconcile_provider(("p1", "p2"), {"p1": "Kitchen", "p2": "Garage"})
    unload_with_error = MagicMock()
    prov.unload_with_error = unload_with_error  # type: ignore[method-assign]
    await prov._reconcile()
    daemon = prov._daemons["p1"]
    daemon.active_player_id = "consumer"

    await prov._handle_backend_event(
        daemon, BackendEvent(type=BackendEventType.FATAL_ERROR, error="boom")
    )
    # the give-up is a deferred task so it does not stop the runner task it is called from
    assert mocks.mass.create_task.call_args.kwargs == {"eager_start": False}
    give_up = mocks.mass.create_task.call_args.args[0]
    await give_up

    assert "p1" not in prov._daemons
    assert "p2" in prov._daemons
    mocks.stop_daemon.assert_awaited_once_with(daemon)
    assert prov._failed_player_ids == {"p1"}
    mocks.mass.players.trigger_player_update.assert_called_with("p1")
    mocks.mass.players.deselect_source.assert_called_once()
    cast("MagicMock", prov.logger).warning.assert_called_once()
    unload_with_error.assert_not_called()


async def test_provider_wide_fatal_error_unloads_the_provider() -> None:
    """An engine-level failure keeps taking the whole provider down."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    unload_with_error = MagicMock()
    prov.unload_with_error = unload_with_error  # type: ignore[method-assign]
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    await prov._handle_backend_event(
        daemon,
        BackendEvent(
            type=BackendEventType.FATAL_ERROR, error="api key revoked", provider_wide=True
        ),
    )

    unload_with_error.assert_called_once_with("api key revoked")
    mocks.mass.create_task.assert_not_called()
    assert "p1" in prov._daemons


async def test_reconcile_skips_a_given_up_daemon() -> None:
    """A daemon that gave up permanently is not relaunched by an ordinary reconcile."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    prov._failed_player_ids = {"p1"}

    await prov._reconcile()

    mocks.start_daemon.assert_not_awaited()
    assert "p1" not in prov._daemons


async def test_player_added_gives_a_failed_daemon_a_fresh_start() -> None:
    """A player re-registering lifts the block and starts its daemon again."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    prov._failed_player_ids = {"p1"}

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_ADDED, object_id="p1"))

    assert "p1" not in prov._failed_player_ids
    mocks.start_daemon.assert_awaited_once()
    assert "p1" in prov._daemons


async def test_give_up_on_a_replaced_daemon_is_a_noop() -> None:
    """A give-up landing after the daemon was replaced leaves the replacement running."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    await prov._reconcile()
    old_daemon = prov._daemons["p1"]
    replacement = MagicMock(player_id="p1", publish_name="Kitchen | Music Assistant")
    prov._daemons["p1"] = replacement

    await prov._give_up_daemon(old_daemon, "boom")

    mocks.stop_daemon.assert_not_awaited()
    assert not prov._failed_player_ids
    assert prov._daemons["p1"] is replacement
