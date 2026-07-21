"""Unit tests for the AirPlay provider (sync_adjust migration + PTP timing source)."""

import asyncio
import logging
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.providers.airplay.constants import (
    CONF_FORCE_RAOP,
    CONF_LEGACY_AIRPLAY_PROTOCOL,
    CONF_LEGACY_FORCE_RAOP,
    CONF_PROTOCOL_MIGRATION_MARKER,
    CONF_SYNC_ADJUST_RESET_MARKER,
    StreamingProtocol,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream import AirPlayStream
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

INSTANCE_ID = "airplay"
START_UNIX_MS = 1_750_000_000_000

# A representative readiness line emitted by `cliairplay --ptp-daemon` on the
# real device (from a live DEBUG log) once it has bound 319/320 and opened its
# control channel.
DAEMON_UP_LINE = (
    "[15:44:56.101] ap2_ptp_run_daemon:1467 [PTP] daemon up: engine on 319/320, "
    "clock in /cliairplay-ptp, control on 127.0.0.1:9010"
)


def _make_provider(
    marker_set: bool,
    player_configs: dict[str, dict[str, object]],
    stored_sync_adjust: dict[str, int],
) -> AirPlayProvider:
    """
    Build a bare provider wired to a mocked config store.

    :param marker_set: Whether the one-time migration already ran.
    :param player_configs: Raw player config store contents.
    :param stored_sync_adjust: Persisted sync_adjust value per player id.
    """
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.mass = MagicMock()
    prov.logger = logging.getLogger("test.airplay.provider")
    prov.config = MagicMock()
    prov.config.instance_id = INSTANCE_ID
    prov.mass.config.get_raw_provider_config_value.return_value = marker_set
    prov.mass.config.get.return_value = player_configs
    prov.mass.config.get_raw_player_config_value.side_effect = lambda player_id, _key, default=0: (
        stored_sync_adjust.get(player_id, default)
    )
    return prov


def test_sync_adjust_migration_resets_stored_values() -> None:
    """Persisted sync_adjust values are reset once and the marker is written."""
    player_configs: dict[str, dict[str, object]] = {
        "apaaa": {"player_id": "apaaa", "provider": INSTANCE_ID},
        "apbbb": {"player_id": "apbbb", "provider": INSTANCE_ID},
        # player of another provider must be left alone
        "sonos1": {"player_id": "sonos1", "provider": "sonos"},
    }
    prov = _make_provider(
        marker_set=False,
        player_configs=player_configs,
        stored_sync_adjust={"apaaa": 120, "apbbb": 0, "sonos1": 250},
    )

    prov._migrate_sync_adjust()

    # only the airplay player with a non-zero offset is reset
    cast("MagicMock", prov.mass.config).set_raw_player_config_value.assert_called_once_with(
        "apaaa", CONF_SYNC_ADJUST, 0
    )
    # the one-time marker is written afterwards
    cast("MagicMock", prov.mass.config).set_raw_provider_config_value.assert_called_once_with(
        INSTANCE_ID, CONF_SYNC_ADJUST_RESET_MARKER, True
    )


def test_sync_adjust_migration_runs_only_once() -> None:
    """With the marker set, the migration must not touch player configs again."""
    player_configs: dict[str, dict[str, object]] = {
        "apaaa": {"player_id": "apaaa", "provider": INSTANCE_ID}
    }
    prov = _make_provider(
        marker_set=True,
        player_configs=player_configs,
        stored_sync_adjust={"apaaa": 120},
    )

    prov._migrate_sync_adjust()

    cast("MagicMock", prov.mass.config).set_raw_player_config_value.assert_not_called()
    cast("MagicMock", prov.mass.config).set_raw_provider_config_value.assert_not_called()


def test_protocol_migration_preserves_only_explicit_raop_preferences() -> None:
    """Legacy forced RAOP values migrate without overriding newer toggle values."""
    player_configs: dict[str, dict[str, object]] = {
        "raop": {"player_id": "raop", "provider": INSTANCE_ID},
        "airplay2": {"player_id": "airplay2", "provider": INSTANCE_ID},
        "automatic": {"player_id": "automatic", "provider": INSTANCE_ID},
        "new-toggle": {"player_id": "new-toggle", "provider": INSTANCE_ID},
        "other": {"player_id": "other", "provider": "sonos"},
    }
    stored_values: dict[tuple[str, str], object] = {
        ("raop", CONF_LEGACY_AIRPLAY_PROTOCOL): StreamingProtocol.RAOP,
        ("airplay2", CONF_LEGACY_AIRPLAY_PROTOCOL): StreamingProtocol.AIRPLAY2,
        ("automatic", CONF_LEGACY_AIRPLAY_PROTOCOL): 0,
        ("new-toggle", CONF_LEGACY_AIRPLAY_PROTOCOL): StreamingProtocol.RAOP,
        ("new-toggle", CONF_FORCE_RAOP): False,
        ("other", CONF_LEGACY_AIRPLAY_PROTOCOL): StreamingProtocol.RAOP,
    }
    prov = _make_provider(False, player_configs, {})
    config = cast("MagicMock", prov.mass.config)
    config.get_raw_player_config_value.side_effect = lambda player_id, key, default=None: (
        stored_values.get((player_id, key), default)
    )

    prov._migrate_protocol_preferences()

    cast("MagicMock", prov.mass.config).set_raw_player_config_value.assert_has_calls(
        [
            call("raop", CONF_FORCE_RAOP, True),
            call("raop", CONF_LEGACY_FORCE_RAOP, True),
        ]
    )
    assert cast("MagicMock", prov.mass.config).set_raw_player_config_value.call_count == 2
    cast("MagicMock", prov.mass.config).set_raw_provider_config_value.assert_called_once_with(
        INSTANCE_ID, CONF_PROTOCOL_MIGRATION_MARKER, True
    )


def test_protocol_migration_runs_only_once() -> None:
    """The protocol migration marker prevents a disabled toggle being restored later."""
    prov = _make_provider(
        True,
        {"raop": {"player_id": "raop", "provider": INSTANCE_ID}},
        {},
    )

    prov._migrate_protocol_preferences()

    cast("MagicMock", prov.mass.config).set_raw_player_config_value.assert_not_called()
    cast("MagicMock", prov.mass.config).set_raw_provider_config_value.assert_not_called()


async def test_unload_awaits_cancelled_ptp_stdout_reader() -> None:
    """Provider unload waits for its cancelled PTP stdout reader before process cleanup."""
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.mass = MagicMock()
    prov._ptp_daemon_ready = None
    prov._ptp_daemon = None
    prov._dacp_server = MagicMock()
    prov._dacp_server.__bool__.return_value = False
    prov._dacp_info = MagicMock()
    prov._dacp_info.__bool__.return_value = False
    reader_started = asyncio.Event()

    async def _stdout_reader() -> None:
        reader_started.set()
        await asyncio.Event().wait()

    reader_task = asyncio.create_task(_stdout_reader())
    prov._ptp_daemon_stdout_task = reader_task
    await reader_started.wait()

    await prov.unload()

    assert reader_task.cancelled()


# --- Shared PTP clock daemon: readiness signal ---------------------------------


def _ptp_provider() -> AirPlayProvider:
    """Build a bare provider with just a logger for PTP readiness tests."""
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.logger = logging.getLogger("test.airplay.provider")
    prov._ptp_daemon = None
    prov._ptp_daemon_ready = None
    return prov


def _live_ptp_daemon() -> MagicMock:
    """Build a process mock that remains alive until its wait task is cancelled."""
    daemon = MagicMock()
    daemon.closed = False
    daemon.returncode = None

    async def _wait_forever() -> int:
        await asyncio.Event().wait()
        return 0

    daemon.wait = AsyncMock(side_effect=_wait_forever)
    return daemon


def test_ptp_daemon_ready_event_set_on_daemon_up_line() -> None:
    """The readiness event is set when the daemon prints its 'daemon up' line."""
    prov = _ptp_provider()
    prov._ptp_daemon_ready = asyncio.Event()

    prov._handle_ptp_daemon_line(DAEMON_UP_LINE)

    assert prov._ptp_daemon_ready.is_set()


def test_ptp_daemon_line_without_marker_leaves_event_clear() -> None:
    """A normal daemon log line does not trip the readiness event."""
    prov = _ptp_provider()
    prov._ptp_daemon_ready = asyncio.Event()

    prov._handle_ptp_daemon_line(
        "[15:44:56.101] ap2_ptp_engine_start:1173 [PTP] Engine started on UDP 319/320"
    )

    assert not prov._ptp_daemon_ready.is_set()


def test_ptp_daemon_line_handler_tolerates_no_event() -> None:
    """The line handler is a no-op for readiness before any daemon has started."""
    prov = _ptp_provider()  # _ptp_daemon_ready is None
    # Must not raise even when the readiness gate does not exist yet.
    prov._handle_ptp_daemon_line(DAEMON_UP_LINE)


async def test_wait_ptp_daemon_ready_true_when_signalled() -> None:
    """wait_ptp_daemon_ready returns True once readiness has been signalled."""
    prov = _ptp_provider()
    prov._ptp_daemon = _live_ptp_daemon()
    prov._ptp_daemon_ready = asyncio.Event()
    prov._ptp_daemon_ready.set()

    assert await prov.wait_ptp_daemon_ready(timeout=0.1) is True


async def test_wait_ptp_daemon_ready_false_without_daemon() -> None:
    """With no daemon ever started (event is None), readiness is False."""
    prov = _ptp_provider()

    assert await prov.wait_ptp_daemon_ready(timeout=0.1) is False


async def test_wait_ptp_daemon_ready_times_out_when_never_ready() -> None:
    """A spawned-but-not-ready daemon yields False after the bounded wait."""
    prov = _ptp_provider()
    prov._ptp_daemon = _live_ptp_daemon()
    prov._ptp_daemon_ready = asyncio.Event()  # but readiness never signalled

    assert await prov.wait_ptp_daemon_ready(timeout=0.05) is False


async def test_wait_ptp_daemon_ready_false_fast_when_daemon_gone() -> None:
    """A daemon that has exited short-circuits to False without burning the timeout."""
    prov = _ptp_provider()
    prov._ptp_daemon = None  # process has exited
    prov._ptp_daemon_ready = asyncio.Event()  # stale gate left un-set

    started = time.monotonic()
    assert await prov.wait_ptp_daemon_ready(timeout=5.0) is False
    assert time.monotonic() - started < 1.0


async def test_wait_ptp_daemon_ready_false_fast_when_process_exited() -> None:
    """An exited process object cannot keep readiness waiting on a stale event."""
    prov = _ptp_provider()
    prov._ptp_daemon = MagicMock(closed=True, returncode=1)
    prov._ptp_daemon_ready = asyncio.Event()

    started = time.monotonic()
    assert await prov.wait_ptp_daemon_ready(timeout=5.0) is False
    assert time.monotonic() - started < 1.0


async def test_wait_ptp_daemon_ready_rejects_stale_ready_event() -> None:
    """A stale readiness signal cannot make an exited daemon appear usable."""
    prov = _ptp_provider()
    prov._ptp_daemon = MagicMock(closed=True, returncode=1)
    prov._ptp_daemon_ready = asyncio.Event()
    prov._ptp_daemon_ready.set()

    assert await prov.wait_ptp_daemon_ready(timeout=0.1) is False


async def test_wait_ptp_daemon_ready_signalled_during_wait() -> None:
    """A readiness signal that arrives while waiting resolves the wait to True."""
    prov = _ptp_provider()
    prov._ptp_daemon = _live_ptp_daemon()
    prov._ptp_daemon_ready = asyncio.Event()

    async def _signal_soon() -> None:
        await asyncio.sleep(0.02)
        prov._handle_ptp_daemon_line(DAEMON_UP_LINE)

    signaller = asyncio.create_task(_signal_soon())
    try:
        assert await prov.wait_ptp_daemon_ready(timeout=1.0) is True
    finally:
        await signaller


# --- Session-wide PTP timing decision ------------------------------------------


def _ap2_player() -> MagicMock:
    """Return a mock player that resolves to native AirPlay 2."""
    player = MagicMock()
    player.protocol = StreamingProtocol.AIRPLAY2
    player.wait_start = 2500
    return player


def _raop_player() -> MagicMock:
    """Return a mock player that resolves to legacy RAOP."""
    player = MagicMock()
    player.protocol = StreamingProtocol.RAOP
    player.wait_start = 1500
    return player


def _make_ptp_session(prov: MagicMock, sync_clients: list[MagicMock]) -> AirPlayStreamSession:
    """Build a stream session wired to a mock provider for PTP-decision tests."""
    pcm_format = MagicMock()
    return AirPlayStreamSession(
        prov, cast("list[AirPlayPlayer]", sync_clients), pcm_format, MagicMock()
    )


async def test_session_resolves_shared_ptp_when_daemon_ready() -> None:
    """A ready daemon makes the whole session opt into shared PTP, no warning."""
    prov = MagicMock()
    prov.wait_ptp_daemon_ready = AsyncMock(return_value=True)
    session = _make_ptp_session(prov, [_ap2_player(), _ap2_player()])

    assert await session._resolve_shared_ptp() is True
    prov.logger.warning.assert_not_called()


async def test_session_degrades_and_warns_for_group_when_not_ready() -> None:
    """A not-ready daemon degrades a group to no shared PTP and warns once."""
    prov = MagicMock()
    prov.wait_ptp_daemon_ready = AsyncMock(return_value=False)
    session = _make_ptp_session(prov, [_ap2_player(), _ap2_player()])

    assert await session._resolve_shared_ptp() is False
    prov.logger.warning.assert_called_once()


async def test_session_single_ap2_player_not_ready_does_not_warn() -> None:
    """A lone native AP2 player degrades silently: self-bind is fine with no partner."""
    prov = MagicMock()
    prov.wait_ptp_daemon_ready = AsyncMock(return_value=False)
    session = _make_ptp_session(prov, [_ap2_player()])

    assert await session._resolve_shared_ptp() is False
    prov.logger.warning.assert_not_called()


async def test_session_skips_ptp_wait_for_raop_members() -> None:
    """A RAOP-only session does not spend playback lead waiting for PTP."""
    prov = MagicMock()
    prov.wait_ptp_daemon_ready = AsyncMock(return_value=True)
    session = _make_ptp_session(prov, [_raop_player(), _raop_player()])

    assert await session._resolve_shared_ptp() is False
    prov.wait_ptp_daemon_ready.assert_not_awaited()


async def test_raop_session_resolves_ptp_for_first_ap2_late_joiner() -> None:
    """A deferred AP2 join still commits the session to the shared PTP clock."""
    prov = MagicMock()
    raop_player = _raop_player()
    raop_player.player_id = "raop"
    raop_player.stream = MagicMock()
    raop_player.stream.running = True
    ap2_player = _ap2_player()
    ap2_player.player_id = "airplay2"
    ap2_player.stream = None
    session = _make_ptp_session(prov, [raop_player])
    pcm_format = cast("MagicMock", session.pcm_format)
    pcm_format.pcm_sample_size = 176_400
    pcm_format.bit_depth = 16
    pcm_format.channels = 2
    session.start_time = time.time() - 5
    session.wait_start = 1.5
    session.seconds_streamed = 5

    async def _wait_ptp_daemon_ready() -> bool:
        assert not session._lock.locked()
        return True

    prov.wait_ptp_daemon_ready = AsyncMock(side_effect=_wait_ptp_daemon_ready)
    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        await session.add_client(cast("AirPlayPlayer", ap2_player))

    prov.wait_ptp_daemon_ready.assert_awaited_once()
    assert session.use_shared_ptp is True
    assert session._shared_ptp_resolved is True
    await_args = mock_start.await_args
    assert await_args is not None
    assert await_args.args[2] is True


async def test_session_start_applies_uniform_ptp_decision_to_all_members() -> None:
    """start() resolves the timing source once and passes it identically to every member."""
    prov = MagicMock()
    prov.wait_ptp_daemon_ready = AsyncMock(return_value=True)
    players = [_ap2_player(), _ap2_player(), _ap2_player()]
    for player in players:
        player.stream = MagicMock()
        player.stream.wait_for_connection = AsyncMock()
    session = _make_ptp_session(prov, players)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        await session.start(MagicMock())

    # One start per member, and every member received the same resolved decision.
    assert mock_start.call_count == len(players)
    ptp_decisions = {call.args[2] for call in mock_start.call_args_list}
    assert ptp_decisions == {True}
    assert session.use_shared_ptp is True


async def test_session_start_calculates_anchor_after_ptp_resolution() -> None:
    """PTP startup time cannot consume the audible setup lead."""
    prov = MagicMock()
    players = [_ap2_player(), _ap2_player()]
    for player in players:
        player.stream = MagicMock()
        player.stream.wait_for_connection = AsyncMock()
    session = _make_ptp_session(prov, players)
    now = 100.0

    async def _resolve_shared_ptp(_ap2_members: int | None = None) -> bool:
        nonlocal now
        now = 103.0
        return True

    with (
        patch.object(session, "_resolve_shared_ptp", new=_resolve_shared_ptp),
        patch(
            "music_assistant.providers.airplay.stream_session.time.time",
            side_effect=lambda: now,
        ),
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        await session.start(MagicMock())

    assert session.start_unix_ms == 105_500
    assert {call.args[1] for call in mock_start.call_args_list} == {105_500}


# --- Session decision reaches the CLI args (overrides bare liveness) ------------


def _stream_player(*, ptp_daemon_running: bool) -> MagicMock:
    """Build a minimal AirPlay player mock sufficient for _build_cli_args."""
    player = MagicMock()
    player.player_id = "apaabbccddeeff"
    player.display_name = "Player A"
    player.address = "192.168.1.50"
    player.protocol = StreamingProtocol.AIRPLAY2
    player.protocol_override = None
    player.volume_level = 40
    player.device_info.mac_address = "AA:BB:CC:DD:EE:FF"
    player.device_info.ip_address = "192.168.1.50"
    player.logger = logging.getLogger("test.airplay.player")
    player.config.get_value = MagicMock(return_value=None)
    # Keep the arg build on its shortest path: no discovery records to expand.
    player.airplay_discovery_info = None
    player.raop_discovery_info = None

    prov = MagicMock()
    prov.dacp_id = "ABCDEF0123456789"
    prov.ptp_daemon_running = ptp_daemon_running
    prov.logger = logging.getLogger("test.airplay.prov")
    prov.mass.streams.publish_ip = "192.168.1.99"
    player.provider = prov
    return player


async def _build_args(player: MagicMock, use_shared_ptp: bool | None) -> list[str]:
    """Assemble CLI args for the player with the externals patched out."""
    stream = AirPlayStream(player)
    with (
        patch(
            "music_assistant.providers.airplay.stream.get_cli_binary",
            return_value="/fake/cliairplay",
        ),
        patch(
            "music_assistant.providers.airplay.stream.resolve_if_ip",
            return_value="192.168.1.5",
        ),
    ):
        return await stream._build_cli_args(START_UNIX_MS, use_shared_ptp)


async def test_build_cli_args_explicit_shared_ptp_overrides_dead_daemon() -> None:
    """An explicit True adds --ptp-shared even when the daemon reads as not-live."""
    player = _stream_player(ptp_daemon_running=False)

    args = await _build_args(player, use_shared_ptp=True)

    assert "--ptp-shared" in args


async def test_build_cli_args_explicit_no_shared_ptp_overrides_live_daemon() -> None:
    """An explicit False omits --ptp-shared even while the daemon is live."""
    player = _stream_player(ptp_daemon_running=True)

    args = await _build_args(player, use_shared_ptp=False)

    assert "--ptp-shared" not in args


async def test_build_cli_args_none_falls_back_to_daemon_liveness() -> None:
    """Legacy single-stream callers (None) still gate --ptp-shared on daemon liveness."""
    assert "--ptp-shared" in await _build_args(
        _stream_player(ptp_daemon_running=True), use_shared_ptp=None
    )
    assert "--ptp-shared" not in await _build_args(
        _stream_player(ptp_daemon_running=False), use_shared_ptp=None
    )
