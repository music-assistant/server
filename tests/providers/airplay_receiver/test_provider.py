"""Unit tests for the AirPlay Receiver provider (ports + daemon reconciliation)."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, EventType
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.player import PlayerAvailabilityTracker
from music_assistant.models.player import Player
from music_assistant.providers.airplay_receiver import (
    AirPlayReceiverProvider,
    airplay_receiver_ports,
)

# --- Port derivation -----------------------------------------------------------


def test_airplay_receiver_ports_are_deterministic() -> None:
    """The port derivation must be stable across processes/restarts (unlike ``hash()``)."""
    # pinned expectations guard against accidental changes to the derivation:
    # the AirPlay provider relies on reproducing these ports from config alone
    assert airplay_receiver_ports("airplay_receiver", ["player-a", "player-b"]) == {
        "player-a": 7765,
        "player-b": 7158,
    }


def test_airplay_receiver_ports_stay_in_expected_range_and_unique() -> None:
    """Derived ports stay within the 7000-7999 AirPlay range and never collide."""
    ports = airplay_receiver_ports("airplay_receiver", [f"player{index}" for index in range(50)])
    assert all(7000 <= port <= 7999 for port in ports.values())
    assert len(set(ports.values())) == 50


def test_airplay_receiver_ports_deduplicates_player_ids() -> None:
    """A duplicated player id claims a single port instead of probing extra slots."""
    ports = airplay_receiver_ports("airplay_receiver", ["player-a", "player-a"])
    assert ports == {"player-a": 7765}


def test_airplay_receiver_ports_collision_probing_is_order_independent() -> None:
    """Colliding derivations probe deterministically regardless of the input order."""
    # player22 and player23 both derive base port 7525 for this instance id
    colliding = ["player22", "player23"]
    ports = airplay_receiver_ports("airplay_receiver", colliding)
    assert ports == {"player22": 7525, "player23": 7526}
    assert airplay_receiver_ports("airplay_receiver", list(reversed(colliding))) == ports


# --- Daemon reconciliation -----------------------------------------------------


@dataclass
class _ReconcileMocks:
    """The mocked collaborators of a reconcile-test provider."""

    mass: MagicMock
    start_receiver: MagicMock
    stop_receiver: AsyncMock


def _reconcile_provider(
    assigned: tuple[str, ...],
    registered: dict[str, str],
    unavailable: set[str] | None = None,
    legacy_ids: dict[str, str] | None = None,
) -> tuple[AirPlayReceiverProvider, _ReconcileMocks]:
    """
    Build a bare provider with the real reconcile logic and mocked daemon control.

    :param assigned: The connected player ids the provider was loaded with.
    :param registered: Currently registered player ids mapped to their display name.
    :param unavailable: Registered player ids that are currently unavailable.
    :param legacy_ids: The ids of merged players, mapped to the player they went into;
        the caller may change it afterwards.
    """
    prov = AirPlayReceiverProvider.__new__(AirPlayReceiverProvider)
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

    legacy = legacy_ids if legacy_ids is not None else {}

    def get_player(player_id: str) -> MagicMock | None:
        player_id = legacy.get(player_id, player_id)
        if player_id not in registered:
            return None
        player = MagicMock()
        player.player_id = player_id
        player.display_name = registered[player_id]
        player.available = player_id not in (unavailable or set())
        return player

    mass.players.get_player.side_effect = get_player
    mass.players.resolve_player_id.side_effect = lambda player_id: legacy.get(player_id, player_id)
    prov._availability = PlayerAvailabilityTracker(mass)

    def start_receiver(player_id: str, _player: MagicMock, airplay_name: str) -> None:
        # stop_called / active_player_id are spelled out: a bare MagicMock attribute is
        # truthy, which would trip the stopped-daemon guard and the deselect path
        prov._daemons[player_id] = MagicMock(
            player_id=player_id,
            airplay_name=airplay_name,
            stop_called=False,
            active_player_id=None,
        )

    start_mock = MagicMock(side_effect=start_receiver)
    stop_mock = AsyncMock()
    prov._start_receiver = start_mock  # type: ignore[method-assign]
    prov._stop_receiver = stop_mock  # type: ignore[method-assign]
    return prov, _ReconcileMocks(mass=mass, start_receiver=start_mock, stop_receiver=stop_mock)


async def test_reconcile_starts_daemon_when_assigned_player_registers() -> None:
    """A daemon starts only once its connected player has actually registered."""
    registered: dict[str, str] = {}
    prov, mocks = _reconcile_provider(("p1",), registered)

    # cold boot: the player has not registered yet, so nothing starts
    await prov._reconcile()
    mocks.start_receiver.assert_not_called()

    registered["p1"] = "Kitchen"
    await prov._reconcile()
    mocks.start_receiver.assert_called_once()
    assert mocks.start_receiver.call_args.args[2] == "Kitchen | Music Assistant"
    assert "p1" in prov._daemons


async def test_reconcile_restarts_daemon_on_advertised_name_drift() -> None:
    """A renamed player gets its daemon restarted with the new advertised name."""
    registered = {"p1": "Kitchen"}
    prov, mocks = _reconcile_provider(("p1",), registered)
    await prov._reconcile()
    old_daemon = prov._daemons["p1"]

    # a second pass without changes is a no-op
    await prov._reconcile()
    mocks.stop_receiver.assert_not_awaited()
    assert mocks.start_receiver.call_count == 1

    # a live session on the old daemon is released before the daemon is replaced
    old_daemon.active_player_id = "consumer"
    registered["p1"] = "Cellar"
    await prov._reconcile()
    mocks.stop_receiver.assert_awaited_once_with(old_daemon)
    assert prov._daemons["p1"].airplay_name == "Cellar | Music Assistant"
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
    mocks.stop_receiver.assert_not_awaited()
    assert prov._daemons["p1"] is daemon


async def test_reconcile_keeps_daemon_for_a_registered_player_turning_unavailable() -> None:
    """A registered player that turns unavailable keeps its running receiver (stable identity)."""
    unavailable: set[str] = set()
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"}, unavailable)
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    unavailable.add("p1")
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="p1"))
    mocks.stop_receiver.assert_not_awaited()
    assert prov._daemons["p1"] is daemon


async def test_player_removed_event_stops_daemon() -> None:
    """A permanently removed player gets its daemon stopped and dropped."""
    registered = {"p1": "Kitchen"}
    prov, mocks = _reconcile_provider(("p1",), registered)
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="p1"))
    mocks.stop_receiver.assert_awaited_once_with(daemon)
    assert not prov._daemons


async def test_receiver_of_a_merged_player_runs_once_under_its_stored_id() -> None:
    """A connected player merged before the load gets one receiver, on the player it went into."""
    prov, mocks = _reconcile_provider(
        ("p_old",), {"p_new": "Kitchen"}, legacy_ids={"p_old": "p_new"}
    )

    await prov._reconcile()
    await prov._reconcile()

    mocks.start_receiver.assert_called_once()
    assert mocks.start_receiver.call_args.args[1].player_id == "p_new"
    # the receiver keeps the port the AirPlay provider derives from the stored id
    assert list(prov._daemons) == ["p_old"]
    assert prov.get_player_audio_sources("p_new") == [prov._daemons["p_old"].audio_source]


async def test_receiver_follows_a_connected_player_that_is_merged_into_another() -> None:
    """A merge of the connected player keeps its receiver, which plays on the kept player."""
    registered = {"p_old": "Kitchen"}
    legacy_ids: dict[str, str] = {}
    prov, mocks = _reconcile_provider(("p_old",), registered, legacy_ids=legacy_ids)
    await prov.loaded_in_mass()
    daemon = prov._daemons["p_old"]

    registered.pop("p_old")
    registered["p_new"] = "Kitchen"
    legacy_ids["p_old"] = "p_new"
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="p_old"))

    mocks.stop_receiver.assert_not_awaited()
    assert prov._daemons == {"p_old": daemon}
    # the events of the player it went into are followed from now on
    assert mocks.mass.subscribe.call_args.kwargs["id_filter"] == ("p_new",)
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="p_new"))
    mocks.stop_receiver.assert_awaited_once_with(daemon)


async def test_merge_of_two_connected_players_keeps_the_receiver_of_the_one_that_stays() -> None:
    """Two connected players turn out to be one: one receiver stays, that of the kept player."""
    registered = {"p_old": "Kitchen", "p_new": "Kitchen"}
    legacy_ids: dict[str, str] = {}
    prov, mocks = _reconcile_provider(("p_old", "p_new"), registered, legacy_ids=legacy_ids)
    await prov.loaded_in_mass()
    absorbed = prov._daemons["p_old"]
    kept = prov._daemons["p_new"]

    registered.pop("p_old")
    legacy_ids["p_old"] = "p_new"
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="p_old"))
    await prov._reconcile()

    assert prov._daemons == {"p_new": kept}
    mocks.stop_receiver.assert_awaited_once_with(absorbed)
    assert prov.get_player_audio_sources("p_new") == [kept.audio_source]

    restarted, restarted_mocks = _reconcile_provider(
        ("p_old", "p_new"), registered, legacy_ids=legacy_ids
    )
    await restarted._reconcile()
    assert list(restarted._daemons) == ["p_new"]
    restarted_mocks.start_receiver.assert_called_once()


async def test_player_added_event_triggers_reconcile() -> None:
    """A player registering (cold boot path) starts its daemon via the event handler."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_ADDED, object_id="p1"))
    mocks.start_receiver.assert_called_once()


async def test_loaded_in_mass_with_empty_connected_players_is_idle() -> None:
    """An empty connected-players selection loads the provider fully idle."""
    prov, mocks = _reconcile_provider((), {})

    await prov.loaded_in_mass()
    mocks.mass.subscribe.assert_not_called()
    mocks.start_receiver.assert_not_called()
    assert not prov._daemons


async def test_loaded_in_mass_subscribes_to_assigned_players_only() -> None:
    """Player events are only watched for the connected players."""
    prov, mocks = _reconcile_provider(("p1", "p2"), {})

    await prov.loaded_in_mass()
    mocks.mass.subscribe.assert_called_once()
    assert mocks.mass.subscribe.call_args.kwargs["id_filter"] == ("p1", "p2")


async def test_get_player_audio_sources_scopes_to_the_daemon_player() -> None:
    """Each receiver's source is bound to its own connected player only."""
    prov, _mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    assert prov.get_player_audio_sources("p1") == [daemon.audio_source]
    assert prov.get_player_audio_sources("p2") == []


async def test_unload_stops_all_daemons() -> None:
    """Unload stops every running daemon and stops watching player events."""
    registered = {"p1": "Kitchen", "p2": "Garage"}
    prov, mocks = _reconcile_provider(("p1", "p2"), registered)
    await prov._reconcile()
    unsubscribe = MagicMock()
    prov._unsubscribe = unsubscribe

    await prov.unload()
    unsubscribe.assert_called_once()
    assert mocks.stop_receiver.await_count == 2
    assert not prov._daemons


async def test_give_up_receiver_stops_only_the_failed_daemon() -> None:
    """A permanently failed receiver is dropped while the other receivers keep running."""
    prov, mocks = _reconcile_provider(("p1", "p2"), {"p1": "Kitchen", "p2": "Garage"})
    await prov._reconcile()
    daemon = prov._daemons["p1"]
    daemon.active_player_id = "consumer"

    await prov._give_up_receiver(daemon, "shairport-sync daemon failed to start multiple times.")

    assert "p1" not in prov._daemons
    assert "p2" in prov._daemons
    mocks.stop_receiver.assert_awaited_once_with(daemon)
    assert prov._failed_player_ids == {"p1"}
    mocks.mass.players.trigger_player_update.assert_called_with("p1")
    mocks.mass.players.deselect_source.assert_called_once()
    cast("MagicMock", prov.logger).warning.assert_called_once()


async def test_reconcile_skips_a_given_up_receiver() -> None:
    """A receiver that gave up permanently is not relaunched by an ordinary reconcile."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    prov._failed_player_ids = {"p1"}

    await prov._reconcile()

    mocks.start_receiver.assert_not_called()
    assert "p1" not in prov._daemons


async def test_player_added_gives_a_failed_receiver_a_fresh_start() -> None:
    """A player re-registering lifts the block and starts its receiver again."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    prov._failed_player_ids = {"p1"}

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_ADDED, object_id="p1"))

    assert "p1" not in prov._failed_player_ids
    mocks.start_receiver.assert_called_once()
    assert "p1" in prov._daemons


async def test_reconcile_waits_for_an_unavailable_player() -> None:
    """A registered but unavailable player gets its receiver once it turns available."""
    unavailable = {"p1"}
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"}, unavailable)

    await prov._reconcile()
    mocks.start_receiver.assert_not_called()

    unavailable.clear()
    await prov._reconcile()
    mocks.start_receiver.assert_called_once()
    assert "p1" in prov._daemons


async def test_player_turning_available_gives_a_failed_receiver_a_fresh_start() -> None:
    """A registered player that comes back lifts the block and starts its receiver again."""
    unavailable = {"p1"}
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"}, unavailable)
    prov._failed_player_ids = {"p1"}

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="p1"))
    assert prov._failed_player_ids == {"p1"}

    unavailable.clear()
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="p1"))

    assert "p1" not in prov._failed_player_ids
    mocks.start_receiver.assert_called_once()
    assert "p1" in prov._daemons


async def test_merged_player_turning_available_gives_its_failed_receiver_a_fresh_start() -> None:
    """The player a connected one was merged into coming back lifts the block of its receiver."""
    unavailable = {"p_new"}
    prov, mocks = _reconcile_provider(
        ("p_old",), {"p_new": "Kitchen"}, unavailable, legacy_ids={"p_old": "p_new"}
    )
    prov._failed_player_ids = {"p_old"}
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="p_new"))

    unavailable.clear()
    await prov._on_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="p_new"))

    assert not prov._failed_player_ids
    mocks.start_receiver.assert_called_once()


async def test_update_of_an_available_player_keeps_a_failed_receiver_blocked() -> None:
    """A player that stayed available does not lift the block of its failed receiver."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    mocks.mass.players.iter_players.return_value = [mocks.mass.players.get_player("p1")]
    prov._availability = PlayerAvailabilityTracker(mocks.mass)
    prov._failed_player_ids = {"p1"}

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_UPDATED, object_id="p1"))

    assert prov._failed_player_ids == {"p1"}
    mocks.start_receiver.assert_not_called()


async def test_give_up_on_a_replaced_receiver_is_a_noop() -> None:
    """A give-up landing after the receiver was replaced leaves the replacement running."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    await prov._reconcile()
    old_daemon = prov._daemons["p1"]
    replacement = MagicMock(player_id="p1", airplay_name="Kitchen | Music Assistant")
    prov._daemons["p1"] = replacement

    await prov._give_up_receiver(old_daemon, "boom")

    mocks.stop_receiver.assert_not_awaited()
    assert not prov._failed_player_ids
    assert prov._daemons["p1"] is replacement


async def test_runner_exit_retries_before_the_restart_budget_is_spent() -> None:
    """A daemon exit within the restart budget schedules a delayed restart."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    await prov._reconcile()
    daemon = prov._daemons["p1"]
    daemon.started = asyncio.Event()
    daemon.started.set()
    daemon.runner_error_count = 0

    prov._handle_runner_exit(daemon)

    assert daemon.runner_error_count == 1
    mocks.mass.call_later.assert_called_once_with(2, prov._setup_shairport_daemon, daemon)


async def test_runner_exit_gives_up_once_the_restart_budget_is_spent() -> None:
    """Exhausting the restart budget gives up this receiver instead of unloading."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    unload_with_error = MagicMock()
    prov.unload_with_error = unload_with_error  # type: ignore[method-assign]
    await prov._reconcile()
    daemon = prov._daemons["p1"]
    daemon.started = asyncio.Event()
    daemon.started.set()
    daemon.runner_error_count = 5

    prov._handle_runner_exit(daemon)
    # the give-up runs as a deferred task so it never stops its own runner task
    assert mocks.mass.create_task.call_args.kwargs == {"eager_start": False}
    await mocks.mass.create_task.call_args.args[0]

    assert "p1" not in prov._daemons
    assert prov._failed_player_ids == {"p1"}
    unload_with_error.assert_not_called()


async def test_runner_exit_without_any_output_unloads_the_provider() -> None:
    """A daemon that never produced any output is an environment-level provider failure."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    unload_with_error = MagicMock()
    prov.unload_with_error = unload_with_error  # type: ignore[method-assign]
    await prov._reconcile()
    daemon = prov._daemons["p1"]
    daemon.started = asyncio.Event()

    prov._handle_runner_exit(daemon)

    unload_with_error.assert_called_once()
    mocks.mass.create_task.assert_not_called()


async def test_runner_exit_after_a_deliberate_stop_does_nothing() -> None:
    """A deliberately stopped receiver never restarts, gives up or unloads."""
    prov, mocks = _reconcile_provider(("p1",), {"p1": "Kitchen"})
    unload_with_error = MagicMock()
    prov.unload_with_error = unload_with_error  # type: ignore[method-assign]
    await prov._reconcile()
    daemon = prov._daemons["p1"]
    daemon.started = asyncio.Event()
    daemon.stop_called = True

    prov._handle_runner_exit(daemon)

    unload_with_error.assert_not_called()
    mocks.mass.create_task.assert_not_called()
    mocks.mass.call_later.assert_not_called()


def test_a_fatal_log_line_marks_the_daemon_started() -> None:
    """A fatal-error log line still proves the binary runs, routing exits to the restart path."""
    prov, _mocks = _reconcile_provider(("p1",), {})
    daemon = MagicMock(started=asyncio.Event())

    prov._process_shairport_log_line(daemon, "fatal error: Could not bind any listening ports")

    assert daemon.started.is_set()


# --- Receiver config ------------------------------------------------------------


async def test_receiver_config_starts_at_the_volume_of_the_player(tmp_path: Path) -> None:
    """The receiver's default volume is the level the player's state shows."""
    prov = AirPlayReceiverProvider.__new__(AirPlayReceiverProvider)
    prov.mass = MagicMock()
    player = MagicMock(spec=Player)
    player.volume_level = 40
    prov.mass.players.get_player.return_value = player
    prov._get_mdns_interface_line = AsyncMock(return_value="")  # type: ignore[method-assign]
    daemon = MagicMock(
        player_id="pl0123456789ab",
        airplay_name="Player A",
        port=7000,
        config_file=str(tmp_path / "shairport.conf"),
    )
    daemon.metadata_pipe.path = str(tmp_path / "metadata")
    daemon.audio_pipe.path = str(tmp_path / "audio")

    await prov._create_config_file(daemon)

    prov.mass.players.get_player.assert_called_once_with("pl0123456789ab")
    # 40 % of the -30.0 to 0.0 dB range
    config = (tmp_path / "shairport.conf").read_text(encoding="utf-8")
    assert "default_airplay_volume = -18.0;" in config


def test_receiver_pipes_and_config_live_in_the_runtime_dir() -> None:
    """The receiver's FIFOs and shairport-sync config are created in the runtime dir."""
    prov, mocks = _reconcile_provider(("p1",), {})
    mocks.mass.runtime_path = "/ma-runtime"
    prov.config.instance_id = "airplay_receiver"
    prov.config.name = "AirPlay Receiver"
    prov.manifest = MagicMock(domain="airplay_receiver")
    prov._ports = {"p1": 7000}
    prov._audio_format = AudioFormat(content_type=ContentType.ALAC)
    prov._setup_shairport_daemon = MagicMock()  # type: ignore[method-assign]
    player = MagicMock(player_id="p1", display_name="Player A")

    AirPlayReceiverProvider._start_receiver(prov, "p1", player, "Player A | Music Assistant")

    daemon = prov._daemons["p1"]
    assert {
        daemon.audio_pipe.path,
        daemon.metadata_pipe.path,
        daemon.config_file,
    } == {
        "/ma-runtime/ma_airplay_audio_airplay_receiver_p1",
        "/ma-runtime/ma_airplay_metadata_airplay_receiver_p1",
        "/ma-runtime/ma_shairport_sync_airplay_receiver_p1.conf",
    }


def test_receiver_of_a_merged_player_keeps_the_port_and_pipes_of_its_stored_id() -> None:
    """A connected player merged into another keeps its port, pipes and config file."""
    prov, mocks = _reconcile_provider(
        ("p_old",), {"p_new": "Player A"}, legacy_ids={"p_old": "p_new"}
    )
    mocks.mass.runtime_path = "/ma-runtime"
    prov.config.instance_id = "airplay_receiver"
    prov.config.name = "AirPlay Receiver"
    prov.manifest = MagicMock(domain="airplay_receiver")
    prov._ports = {"p_old": 7000}
    prov._audio_format = AudioFormat(content_type=ContentType.ALAC)
    prov._setup_shairport_daemon = MagicMock()  # type: ignore[method-assign]

    AirPlayReceiverProvider._start_receiver(
        prov, "p_old", mocks.mass.players.get_player("p_old"), "Player A | Music Assistant"
    )

    daemon = prov._daemons["p_old"]
    assert daemon.port == 7000
    assert {
        daemon.audio_pipe.path,
        daemon.metadata_pipe.path,
        daemon.config_file,
    } == {
        "/ma-runtime/ma_airplay_audio_airplay_receiver_p_old",
        "/ma-runtime/ma_airplay_metadata_airplay_receiver_p_old",
        "/ma-runtime/ma_shairport_sync_airplay_receiver_p_old.conf",
    }
    assert daemon.audio_source.name == "AirPlay Receiver (Player A)"


async def test_session_hooks_reach_a_metadata_pipe_whose_path_has_spaces(tmp_path: Path) -> None:
    """A runtime dir under a cache path with spaces still gets the play start and end markers."""
    runtime_path = tmp_path / "cache with spaces" / "runtime"
    runtime_path.mkdir(parents=True)
    prov, mocks = _reconcile_provider(("p1",), {})
    mocks.mass.runtime_path = str(runtime_path)
    prov.config.instance_id = "airplay_receiver"
    prov.config.name = "AirPlay Receiver"
    prov.manifest = MagicMock(domain="airplay_receiver")
    prov._ports = {"p1": 7000}
    prov._audio_format = AudioFormat(content_type=ContentType.ALAC)
    prov._setup_shairport_daemon = MagicMock()  # type: ignore[method-assign]
    prov._get_mdns_interface_line = AsyncMock(return_value="")  # type: ignore[method-assign]
    AirPlayReceiverProvider._start_receiver(
        prov, "p1", MagicMock(player_id="p1"), "Player A | Music Assistant"
    )
    daemon = prov._daemons["p1"]

    await AirPlayReceiverProvider._create_config_file(prov, daemon)

    config = Path(daemon.config_file).read_text(encoding="utf-8")
    for hook in ("run_this_before_entering_active_state", "run_this_after_exiting_active_state"):
        line = next(ln for ln in config.splitlines() if ln.strip().startswith(hook))
        libconfig_value = line.split("=", 1)[1].strip().removesuffix(";")
        # shairport-sync splits the command like a shell (popt) and runs `sh -c <script>`
        command = libconfig_value[1:-1].replace('\\"', '"')
        script = shlex.split(command)[2]
        assert shlex.split(script)[-1] == daemon.metadata_pipe.path
