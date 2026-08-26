"""Unit tests for the AirPlay Receiver provider (ports + daemon reconciliation)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import EventType
from music_assistant_models.event import MassEvent

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
) -> tuple[AirPlayReceiverProvider, _ReconcileMocks]:
    """
    Build a bare provider with the real reconcile logic and mocked daemon control.

    :param assigned: The connected player ids the provider was loaded with.
    :param registered: Currently registered player ids mapped to their display name.
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

    def get_player(player_id: str) -> MagicMock | None:
        if player_id not in registered:
            return None
        player = MagicMock()
        player.player_id = player_id
        player.display_name = registered[player_id]
        return player

    mass.players.get_player.side_effect = get_player

    def start_receiver(player: MagicMock, airplay_name: str) -> None:
        # stop_called / active_player_id are spelled out: a bare MagicMock attribute is
        # truthy, which would trip the stopped-daemon guard and the deselect path
        prov._daemons[player.player_id] = MagicMock(
            player_id=player.player_id,
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
    assert mocks.start_receiver.call_args.args[1] == "Kitchen | Music Assistant"
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


async def test_player_removed_event_stops_daemon() -> None:
    """A permanently removed player gets its daemon stopped and dropped."""
    registered = {"p1": "Kitchen"}
    prov, mocks = _reconcile_provider(("p1",), registered)
    await prov._reconcile()
    daemon = prov._daemons["p1"]

    await prov._on_player_event(MassEvent(event=EventType.PLAYER_REMOVED, object_id="p1"))
    mocks.stop_receiver.assert_awaited_once_with(daemon)
    assert not prov._daemons


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
