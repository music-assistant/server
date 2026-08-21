"""Tests for the Spotify Connect provider."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderType

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
    AUDIO_SOURCE_ID,
    CONF_LOUDNESS_NORMALIZATION,
)
from music_assistant.providers.spotify_connect.soloist.backend import (
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
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


def _volume_sync_provider(volume_level: int | None) -> tuple[SpotifyConnectProvider, AsyncMock]:
    """Build a minimal provider whose linked player reports the given volume."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider._last_volume_sent = None
    backend = MagicMock()
    set_volume = AsyncMock()
    backend.set_volume = set_volume
    provider._backend = backend
    player = MagicMock()
    player.state.volume_level = volume_level
    provider.mass.players.get_player.return_value = player
    return provider, set_volume


async def test_sync_player_volume_pushes_player_volume_to_backend() -> None:
    """The player's volume is pushed to the backend and cached for echo dedupe."""
    provider, set_volume = _volume_sync_provider(50)

    await provider._sync_player_volume_to_spotify("player1")

    set_volume.assert_awaited_once_with(50)
    assert provider._last_volume_sent == 50


async def test_sync_player_volume_pushes_when_cache_matches() -> None:
    """The push is unconditional: the backend's volume resets between sessions."""
    provider, set_volume = _volume_sync_provider(50)
    provider._last_volume_sent = 50

    await provider._sync_player_volume_to_spotify("player1")

    set_volume.assert_awaited_once_with(50)


async def test_sync_player_volume_skips_when_volume_unknown() -> None:
    """No push happens when the player does not expose a volume level."""
    provider, set_volume = _volume_sync_provider(None)

    await provider._sync_player_volume_to_spotify("player1")

    set_volume.assert_not_awaited()
    assert provider._last_volume_sent is None


async def test_sync_player_volume_restores_cache_on_failure() -> None:
    """A failed push restores the dedupe cache so a retry is not wrongly deduped."""
    provider, set_volume = _volume_sync_provider(50)
    set_volume.side_effect = OSError("daemon gone")

    await provider._sync_player_volume_to_spotify("player1")

    assert provider._last_volume_sent is None


def _tethered_provider() -> tuple[SpotifyConnectProvider, AsyncMock]:
    """Build a provider tethered to queue 'player1' with an active (paused) Spotify session."""
    provider = object.__new__(SpotifyConnectProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    backend = MagicMock()
    deactivate = AsyncMock()
    backend.deactivate = deactivate
    provider._backend = backend
    provider._active_player_id = "player1"
    provider._in_use_by_queue = None
    provider._active_session_id = None
    provider._spotify_session_active = True
    provider._playing = False
    provider._pending_pause_stop_task = None
    provider._pending_play_media_task = None
    return provider, deactivate


async def test_queue_clear_releases_a_paused_spotify_session() -> None:
    """Clearing the queue releases the session the paused stream's teardown left behind."""
    provider, deactivate = _tethered_provider()

    await provider.on_source_removed(AUDIO_SOURCE_ID, "player1")

    deactivate.assert_awaited_once()


async def test_queue_clear_releases_while_the_stream_is_winding_down() -> None:
    """
    A clear landing before the paused stream finished tearing down still releases.

    The teardown itself releases nothing for a paused source, so waiting for it to hand the
    claim back would leave the Spotify app tethered for good.
    """
    provider, deactivate = _tethered_provider()
    provider._in_use_by_queue = "player1"

    await provider.on_source_removed(AUDIO_SOURCE_ID, "player1")

    deactivate.assert_awaited_once()


async def test_clearing_another_queue_leaves_the_session_alone() -> None:
    """Only the queue the source is tethered to may release it."""
    provider, deactivate = _tethered_provider()

    await provider.on_source_removed(AUDIO_SOURCE_ID, "player2")

    deactivate.assert_not_awaited()


async def test_queue_clear_without_an_active_session_does_nothing() -> None:
    """There is nothing to release when MA is not the active Spotify device."""
    provider, deactivate = _tethered_provider()
    provider._spotify_session_active = False

    await provider.on_source_removed(AUDIO_SOURCE_ID, "player1")

    deactivate.assert_not_awaited()


async def _session_inactive(provider: SpotifyConnectProvider) -> list[str]:
    """Run the backend's 'session inactive' answer and return the players it wanted stopped."""
    stopped: list[str] = []
    provider._schedule_pause_stop = lambda player_id: stopped.append(player_id)  # type: ignore[method-assign]
    with patch.object(SpotifyConnectProvider, "name", "Spotify Test"):
        await provider._handle_backend_event(BackendEvent(type=BackendEventType.SESSION_INACTIVE))
    return stopped


async def test_releasing_the_session_leaves_the_new_playback_alone() -> None:
    """
    Releasing must not stop the player that took the source's place.

    The backend answers a release with the same "session inactive" it sends when the user picks
    another device in the Spotify app - and that one does stop the player. By then this player is
    playing whatever replaced the source, so stopping it would cut the music the user just started.
    """
    provider, _ = _tethered_provider()

    with patch.object(SpotifyConnectProvider, "name", "Spotify Test"):
        await provider.on_source_removed(AUDIO_SOURCE_ID, "player1")

    assert await _session_inactive(provider) == []


async def test_a_spotify_side_deselect_still_stops_the_player() -> None:
    """Picking another device in the Spotify app does stop what MA was playing from it."""
    provider, _ = _tethered_provider()

    assert await _session_inactive(provider) == ["player1"]


async def test_queue_clear_survives_a_failing_release() -> None:
    """A backend that cannot be reached must not break clearing the queue."""
    provider, deactivate = _tethered_provider()
    deactivate.side_effect = OSError("daemon gone")

    await provider.on_source_removed(AUDIO_SOURCE_ID, "player1")

    deactivate.assert_awaited_once()


async def test_a_transferred_source_follows_its_new_queue() -> None:
    """A paused source moved to another player is tracked on the queue that took it over."""
    provider, _ = _tethered_provider()

    await provider.on_source_transferred(AUDIO_SOURCE_ID, "player1", "player2")

    assert provider._active_player_id == "player2"


async def test_a_transfer_of_another_queue_is_ignored() -> None:
    """A transfer that does not involve the tethered queue leaves the tracking alone."""
    provider, _ = _tethered_provider()

    await provider.on_source_transferred(AUDIO_SOURCE_ID, "player3", "player2")

    assert provider._active_player_id == "player1"


async def test_clearing_a_transferred_queue_releases_the_session() -> None:
    """
    The queue a paused source was transferred to can release it.

    Transferring a paused source never re-selects it on the target, so without the handover
    the plugin would still be pointed at the queue it left and the release would not fire.
    """
    provider, deactivate = _tethered_provider()

    await provider.on_source_transferred(AUDIO_SOURCE_ID, "player1", "player2")
    await provider.on_source_removed(AUDIO_SOURCE_ID, "player2")

    deactivate.assert_awaited_once()


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
    provider._publish_name = "Test Speaker"
    provider.config = ProviderConfig(
        values={},
        type=ProviderType.PLUGIN,
        domain="spotify_connect",
        instance_id="spotify_connect--test",
        name="Spotify Connect",
    )
    return provider


def test_config_without_backend_choice_loads_go_librespot(tmp_path: Path) -> None:
    """A config from before the backend choice existed loads go-librespot unchanged."""
    provider = _provider_with_stored_config({}, tmp_path)

    assert isinstance(provider._create_backend(), GoLibrespotBackend)


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

    backend = provider._create_backend()

    assert isinstance(backend, SoloistBackend)
    assert backend._api_key == "soloist-api-key-0123456789abcdef"
    assert backend._consent is True
    assert backend._volume_mode == VOLUME_MODE_SYNC_SPOTIFY
    assert backend._crossfade_ms == 8000
    assert backend._loudness_normalization is False


def test_audio_behavior_defaults_reach_the_backend(tmp_path: Path) -> None:
    """Without stored values, crossfade is off and normalization enabled."""
    provider = _provider_with_stored_config({}, tmp_path)

    backend = provider._create_backend()

    assert isinstance(backend, GoLibrespotBackend)
    assert backend._crossfade_ms == 0
    assert backend._loudness_normalization is True


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

    backend = provider._create_backend()

    assert isinstance(backend, GoLibrespotBackend)
    assert backend._crossfade_ms == 8000
    assert backend._loudness_normalization is False


def test_write_config_carries_the_audio_behavior_keys(tmp_path: Path) -> None:
    """The generated config.yml carries crossfade_duration (ms) and normalisation_disabled."""
    backend = object.__new__(GoLibrespotBackend)
    backend.mass = MagicMock()
    backend.logger = MagicMock()
    backend._publish_name = "Test Speaker"
    backend._instance_id = "spotify_connect--test"
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
    backend._instance_id = "spotify_connect--test"
    backend._api_port = 38800
    backend.cache_dir = str(tmp_path)
    backend._crossfade_ms = 0
    backend._loudness_normalization = True
    backend._audio_quality = AUDIO_QUALITY_LOSSLESS

    backend._write_config(None)

    config = json.loads((tmp_path / "config.yml").read_text(encoding="utf-8"))
    assert config["bitrate"] == 320
