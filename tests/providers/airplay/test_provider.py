"""Unit tests for the AirPlay provider (sync_adjust migration + PTP timing source)."""

import asyncio
import logging
import time
from contextlib import AbstractContextManager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

from music_assistant_models.enums import PlaybackState

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.providers.airplay.constants import (
    CONF_FORCE_RAOP,
    CONF_LEGACY_AIRPLAY_PROTOCOL,
    CONF_LEGACY_FORCE_RAOP,
    CONF_PROTOCOL_MIGRATION_MARKER,
    CONF_SYNC_ADJUST_RESET_MARKER,
    AirPlayRemoteCommand,
    StreamingProtocol,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.sendspin_bridge import (
    SendspinAirPlayBridge,
    SendspinBridgeManager,
)
from music_assistant.providers.airplay.stream import AirPlayStream
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession
from music_assistant.providers.airplay_receiver import airplay_receiver_port

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


def test_remote_pause_routes_to_sendspin_session() -> None:
    """A bridged device pause targets the owning Sendspin session."""
    prov = _make_provider(False, {}, {})
    players = cast("MagicMock", prov.mass.players)
    manager = SendspinBridgeManager(prov)
    prov._bridge_manager = manager

    airplay_player = MagicMock()
    airplay_player.player_id = "apc43875e9e53a"
    airplay_player.playback_state = PlaybackState.PLAYING

    bridge = SendspinAirPlayBridge(prov, airplay_player, MagicMock())
    bridge._bridge_client_id = "sendspin-bridge"
    bridge._airplay_stream = airplay_player.stream = MagicMock()
    manager._bridges[airplay_player.player_id] = bridge

    bridge_player = MagicMock()
    bridge_player.synced_to = "sendspin-leader"
    bridge_player.protocol_parent_id = airplay_player.player_id
    bridge_player.player_id = "sendspin-bridge"

    sync_leader = MagicMock()
    sync_leader.protocol_parent_id = "sendspin-session"
    sync_leader.player_id = "sendspin-leader"
    players.get_player.side_effect = {
        "sendspin-bridge": bridge_player,
        "sendspin-leader": sync_leader,
    }.get

    prov.handle_remote_command(airplay_player, AirPlayRemoteCommand.PAUSE)

    players.cmd_pause.assert_called_once_with("sendspin-session")


def test_remote_pause_keeps_normal_airplay_routing() -> None:
    """An inactive bridge does not change normal AirPlay pause routing."""
    prov = _make_provider(False, {}, {})
    players = cast("MagicMock", prov.mass.players)
    manager = SendspinBridgeManager(prov)
    prov._bridge_manager = manager
    airplay_player = MagicMock()
    airplay_player.player_id = "apc43875e9e53a"
    airplay_player.playback_state = PlaybackState.PLAYING
    airplay_player.stream = MagicMock()

    bridge = SendspinAirPlayBridge(prov, airplay_player, MagicMock())
    bridge._bridge_client_id = "sendspin-bridge"
    manager._bridges[airplay_player.player_id] = bridge

    prov.handle_remote_command(airplay_player, AirPlayRemoteCommand.PAUSE)

    players.cmd_pause.assert_called_once_with(airplay_player.player_id)


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


async def test_ptp_daemon_spawn_advertises_dacp_identity() -> None:
    """The spawned PTP daemon is handed the provider's DACP id as its clock identity."""
    prov = _ptp_provider()
    prov.dacp_id = "AABBCCDD11223344"
    prov.mass = MagicMock()
    prov.mass.streams.bind_ip = "0.0.0.0"
    # The spawn mirrors the live logger level into --debug flags; pin the
    # level so suite ordering (other tests raising verbosity) cannot leak
    # extra args into this assertion.
    prov.logger.setLevel(logging.INFO)

    def _consume_task(coro: object) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    prov.mass.create_task.side_effect = _consume_task

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_cli_binary",
            AsyncMock(return_value="/bin/cliairplay"),
        ),
        patch("music_assistant.providers.airplay.provider.AsyncProcess") as process_cls,
    ):
        process_cls.return_value.start = AsyncMock()
        await prov._start_ptp_daemon()

    assert process_cls.call_args.args[0] == [
        "/bin/cliairplay",
        "--ptp-daemon",
        "--dacp",
        "AABBCCDD11223344",
    ]


async def test_ptp_daemon_spawn_quiet_at_debug_level() -> None:
    """A normal debug session keeps the daemon quiet (no per-packet tracing)."""
    prov = _ptp_provider()
    prov.dacp_id = "AABBCCDD11223344"
    prov.mass = MagicMock()
    prov.mass.streams.bind_ip = "0.0.0.0"
    prov.logger.setLevel(logging.DEBUG)

    def _consume_task(coro: object) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    prov.mass.create_task.side_effect = _consume_task

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_cli_binary",
            AsyncMock(return_value="/bin/cliairplay"),
        ),
        patch("music_assistant.providers.airplay.provider.AsyncProcess") as process_cls,
    ):
        process_cls.return_value.start = AsyncMock()
        await prov._start_ptp_daemon()

    assert "--debug" not in process_cls.call_args.args[0]


async def test_ptp_daemon_spawn_traces_at_verbose_level() -> None:
    """A verbose session turns on the daemon's per-packet timing trace."""
    prov = _ptp_provider()
    prov.dacp_id = "AABBCCDD11223344"
    prov.mass = MagicMock()
    prov.mass.streams.bind_ip = "0.0.0.0"
    prov.logger.setLevel(VERBOSE_LOG_LEVEL)

    def _consume_task(coro: object) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    prov.mass.create_task.side_effect = _consume_task

    with (
        patch(
            "music_assistant.providers.airplay.provider.get_cli_binary",
            AsyncMock(return_value="/bin/cliairplay"),
        ),
        patch("music_assistant.providers.airplay.provider.AsyncProcess") as process_cls,
    ):
        process_cls.return_value.start = AsyncMock()
        await prov._start_ptp_daemon()

    assert process_cls.call_args.args[0][-2:] == ["--debug", "10"]


def test_pyatv_logging_quiet_at_debug_level() -> None:
    """A normal debug session keeps pyatv's own (very chatty) logging quiet."""
    prov = _ptp_provider()
    prov.logger.setLevel(logging.DEBUG)

    prov._set_pyatv_log_level()

    # pyatv debug output is dropped; only INFO and above pass through
    assert logging.getLogger("pyatv").level == logging.INFO


def test_pyatv_logging_traces_at_verbose_level() -> None:
    """A verbose session passes pyatv's debug logging through."""
    prov = _ptp_provider()
    prov.logger.setLevel(VERBOSE_LOG_LEVEL)

    prov._set_pyatv_log_level()

    assert logging.getLogger("pyatv").level == logging.DEBUG


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


async def test_ptp_daemon_bind_failure_degrades_without_restart() -> None:
    """
    Verify a privileged-port bind failure leaves streams on NTP timing.

    The provider must clear readiness without restarting an immediate bind failure.
    """
    prov = _ptp_provider()
    daemon = MagicMock()
    daemon.wait = AsyncMock(return_value=2)
    ready = asyncio.Event()
    ready.set()
    prov.logger = MagicMock()
    prov._ptp_daemon = daemon
    prov._ptp_daemon_ready = ready
    prov._ptp_daemon_stop_requested = False
    prov._ptp_daemon_started = time.monotonic()
    prov._ptp_daemon_restarted = False

    with patch.object(prov, "_start_ptp_daemon", new_callable=AsyncMock) as restart:
        await prov._ptp_daemon_monitor(daemon)

    restart.assert_not_awaited()
    assert not await prov.wait_ptp_daemon_ready(timeout=0)
    warning = prov.logger.warning
    warning.assert_called_once()
    assert warning.call_args.args[1] == 2
    assert "CAP_NET_BIND_SERVICE" in warning.call_args.args[0]


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

    async def _start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = MagicMock(running=True, connected=True)
        player.stream.wait_for_connection = AsyncMock()
        player.stream.flush = AsyncMock(return_value=True)
        player.stream.start = AsyncMock()

    with patch.object(
        session, "_start_client", new_callable=AsyncMock, side_effect=_start_client
    ) as mock_start:
        await session.add_client(cast("AirPlayPlayer", ap2_player))

    prov.wait_ptp_daemon_ready.assert_awaited_once()
    assert session.use_shared_ptp is True
    assert session._shared_ptp_resolved is True
    await_args = mock_start.await_args
    assert await_args is not None
    assert await_args.args[1] is True


async def test_session_start_applies_uniform_ptp_decision_to_all_members() -> None:
    """start() resolves the timing source once and passes it identically to every member."""
    prov = MagicMock()
    prov.wait_ptp_daemon_ready = AsyncMock(return_value=True)
    players = [_ap2_player(), _ap2_player(), _ap2_player()]
    for player in players:
        player.stream = MagicMock()
        player.stream.wait_for_connection = AsyncMock()
        player.stream.start = AsyncMock()
    session = _make_ptp_session(prov, players)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        await session.start(MagicMock())

    # One start per member, and every member received the same resolved decision.
    assert mock_start.call_count == len(players)
    ptp_decisions = {call.args[1] for call in mock_start.call_args_list}
    assert ptp_decisions == {True}
    assert session.use_shared_ptp is True


async def test_session_start_calculates_anchor_after_ptp_resolution() -> None:
    """PTP startup time cannot consume the audible setup lead."""
    prov = MagicMock()
    players = [_ap2_player(), _ap2_player()]
    for player in players:
        player.stream = MagicMock()
        player.stream.wait_for_connection = AsyncMock()
        player.stream.start = AsyncMock()
        player.config.get_value = MagicMock(return_value=0)
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
        patch.object(session, "_start_client", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        await session.start(MagicMock())

    # anchor = now (103_000 ms) + max member setup lead (AP2 = 2500 ms)
    assert session.start_unix_ms == 105_500
    for player in players:
        player.stream.start.assert_awaited_once()
        assert player.stream.start.await_args.args[0] == 105_500


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
        return await stream._build_cli_args(use_shared_ptp)


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


# --- Own AirPlay Receiver filtering in discovery -------------------------------

RECEIVER_INSTANCE_ID = "airplay_receiver--test1234"
HOST_IPS = ("192.168.1.10", "172.30.32.1")


def _receiver_filter_provider(
    provider_configs: dict[str, dict[str, object]],
    receiver_instances: tuple[MagicMock, ...] = (),
) -> AirPlayProvider:
    """Build a bare provider wired to raw provider configs and running receiver instances."""
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.mass = MagicMock()
    prov.logger = logging.getLogger("test.airplay.provider")
    prov.mass.config.get.return_value = provider_configs
    prov.mass.get_provider_instances.return_value = list(receiver_instances)
    return prov


def _receiver_config(
    airplay_name: str | None = "Garage [AirPlay]", enabled: bool = True
) -> dict[str, dict[str, object]]:
    """Build the raw provider config store with a single AirPlay Receiver instance."""
    values: dict[str, object] = {"airplay_name": airplay_name} if airplay_name else {}
    return {
        RECEIVER_INSTANCE_ID: {
            "domain": "airplay_receiver",
            "instance_id": RECEIVER_INSTANCE_ID,
            "enabled": enabled,
            "values": values,
        },
        "spotify": {"domain": "spotify", "instance_id": "spotify", "values": {}},
    }


def _discovery_info(
    addresses: list[str],
    port: int | None = 7123,
    properties: dict[str, str] | None = None,
) -> MagicMock:
    """Build a minimal mdns service info stand-in for the receiver filter."""
    info = MagicMock()
    info.parsed_addresses.return_value = addresses
    info.port = port
    info.decoded_properties = properties or {}
    return info


def _patch_host_ips() -> AbstractContextManager[AsyncMock]:
    """Patch the host IP enumeration used by the receiver filter."""
    return patch(
        "music_assistant.providers.airplay.provider.get_ip_addresses",
        AsyncMock(return_value=HOST_IPS),
    )


async def test_own_receiver_filtered_by_name_without_txt_record() -> None:
    """A host-local advertisement matching a receiver name is ours, even without TXT data."""
    prov = _receiver_filter_provider(_receiver_config())
    # advertised via a secondary host interface (e.g. a docker bridge) and
    # with an unresolved TXT record - both must not defeat the filter
    info = _discovery_info(["172.30.32.1"], port=9999)

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Garage [AirPlay]", info) is True


async def test_own_receiver_filtered_with_default_name() -> None:
    """A receiver instance without an explicit name advertises the default name."""
    prov = _receiver_filter_provider(_receiver_config(airplay_name=None))
    info = _discovery_info(["192.168.1.10"])

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Music Assistant", info) is True


async def test_own_receiver_filtered_on_loopback() -> None:
    """A loopback advertisement matching a receiver name is filtered without host IP probing."""
    prov = _receiver_filter_provider(_receiver_config())
    info = _discovery_info(["127.0.0.1"])

    assert await prov._is_own_airplay_receiver("Garage [AirPlay]", info) is True


async def test_own_receiver_filtered_by_port_and_model() -> None:
    """A host-local ShairportSync advertisement on a receiver port is ours, any name."""
    prov = _receiver_filter_provider(_receiver_config())
    info = _discovery_info(
        ["192.168.1.10"],
        port=airplay_receiver_port(RECEIVER_INSTANCE_ID),
        properties={"am": "ShairportSync"},
    )

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Renamed Receiver", info) is True


async def test_own_receiver_filtered_by_running_instance_port() -> None:
    """The port of a running receiver instance is honoured next to the derived ports."""
    receiver = MagicMock()
    receiver.airplay_port = 7555
    prov = _receiver_filter_provider(_receiver_config(), receiver_instances=(receiver,))
    info = _discovery_info(["192.168.1.10"], port=7555, properties={"am": "ShairportSync"})

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Renamed Receiver", info) is True


async def test_same_name_receiver_on_other_host_not_filtered() -> None:
    """A device with a matching name on another host must not be filtered."""
    prov = _receiver_filter_provider(_receiver_config())
    info = _discovery_info(["192.168.1.55"], properties={"am": "ShairportSync"})

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Garage [AirPlay]", info) is False


async def test_user_shairport_on_same_host_not_filtered() -> None:
    """A user-run shairport-sync on this host with its own name and port stays usable."""
    prov = _receiver_filter_provider(_receiver_config())
    info = _discovery_info(["192.168.1.10"], port=5000, properties={"am": "ShairportSync"})

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("My Shairport", info) is False


async def test_local_receiver_with_other_model_not_filtered_on_port_collision() -> None:
    """A non-shairport local receiver is kept even when its port collides (e.g. macOS on 7000)."""
    receiver = MagicMock()
    receiver.airplay_port = 7000
    prov = _receiver_filter_provider(_receiver_config(), receiver_instances=(receiver,))
    info = _discovery_info(["192.168.1.10"], port=7000, properties={"am": "MacBookAir10,1"})

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Marcel's MacBook Air", info) is False


async def test_disabled_receiver_config_not_filtered() -> None:
    """A disabled receiver instance cannot be advertising, so its identity is ignored."""
    prov = _receiver_filter_provider(_receiver_config(enabled=False))
    info = _discovery_info(["192.168.1.10"])

    with _patch_host_ips():
        assert await prov._is_own_airplay_receiver("Garage [AirPlay]", info) is False


async def test_no_receiver_configs_never_filters() -> None:
    """Without any configured receiver instance the filter is a no-op."""
    prov = _receiver_filter_provider(
        {"spotify": {"domain": "spotify", "instance_id": "spotify", "values": {}}}
    )
    info = _discovery_info(["192.168.1.10"], properties={"am": "ShairportSync"})

    assert await prov._is_own_airplay_receiver("Garage [AirPlay]", info) is False


async def test_setup_player_skips_own_receiver_before_service_lookup() -> None:
    """Discovery setup bails out on own receivers before any counterpart service lookup."""
    prov = AirPlayProvider.__new__(AirPlayProvider)
    prov.mass = MagicMock()
    prov.logger = logging.getLogger("test.airplay.provider")
    prov.mass.config.get_raw_player_config_value.return_value = True
    prov._is_own_airplay_receiver = AsyncMock(return_value=True)  # type: ignore[method-assign]
    info = _discovery_info(["192.168.1.10"])
    info.type = "_raop._tcp.local."

    await prov._setup_player("apdb1ff0aae80e", "Garage [AirPlay]", info)

    prov.mass.discovery.async_find_mdns_service.assert_not_called()
    prov.mass.players.register.assert_not_called()
