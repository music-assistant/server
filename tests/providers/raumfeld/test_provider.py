"""Tests for the Teufel Raumfeld player provider."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import ResourceBusyError
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.raumfeld.constants import (
    CONF_RESTART_ON_JOIN,
    GROUP_SETTLE_TIME,
    PLAYER_CONFIG_ENTRIES,
)
from music_assistant.providers.raumfeld.player import RaumfeldPlayer
from music_assistant.providers.raumfeld.provider import RaumfeldPlayerProvider

_LOCATION = "http://192.168.1.42:54730/renderer.xml"
_DEVICE_DESCRIPTION = (
    '<?xml version="1.0"?>'
    '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
    "<friendlyName>Speaker Bar</friendlyName>"
    "<modelName>Raumfeld One S</modelName>"
    "<serialNumber>04:a3:16:f3:b6:c4</serialNumber>"
    "<serviceList><service>"
    "<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
    "<controlURL>/AVTransport/ctrl</controlURL>"
    "</service></serviceList>"
    "</device></root>"
)


def _provider(description: str) -> tuple[RaumfeldPlayerProvider, MagicMock]:
    """Build a provider whose room renderer answers with the given device description."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider._room_serials = {}
    provider._room_models = {}
    provider._room_transport_paths = {}
    provider.host = MagicMock()
    provider.host.resolve = {
        "room_to_udn": {"Bar": "uuid:room"},
        "roomudn_to_rendudn": {"uuid:room": "uuid:renderer"},
        "udn_to_devloc": {"uuid:renderer": _LOCATION},
    }
    response = MagicMock()
    response.text = AsyncMock(return_value=description)
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=None)
    mass = MagicMock()
    mass.http_session.get.return_value = request
    provider.mass = mass
    provider.logger = MagicMock()
    return provider, mass


async def test_room_model_is_read_with_the_serial() -> None:
    """Reading a room's device description stores its model name along with the serial."""
    provider, mass = _provider(_DEVICE_DESCRIPTION)
    assert provider.room_model("Bar") is None

    assert await provider._ensure_room_serial("Bar") == "04:a3:16:f3:b6:c4"

    assert provider.room_model("Bar") == "Raumfeld One S"
    assert mass.http_session.get.call_args.args[0] == _LOCATION


async def test_room_without_a_model_name_has_no_model() -> None:
    """A description without a model name leaves the room's model unknown."""
    provider, _ = _provider(
        _DEVICE_DESCRIPTION.replace("<modelName>Raumfeld One S</modelName>", "")
    )

    assert await provider._ensure_room_serial("Bar") == "04:a3:16:f3:b6:c4"

    assert provider.room_model("Bar") is None


async def test_transport_url_follows_the_renderers_current_address() -> None:
    """A renderer that restarts on another port is reached there, though read only once."""
    provider, _ = _provider(_DEVICE_DESCRIPTION)
    await provider._ensure_room_serial("Bar")
    assert provider.room_transport_url("Bar") == "http://192.168.1.42:54730/AVTransport/ctrl"

    provider.host.resolve["udn_to_devloc"]["uuid:renderer"] = "http://192.168.1.42:41000/r.xml"

    assert provider.room_transport_url("Bar") == "http://192.168.1.42:41000/AVTransport/ctrl"


ROOMS = ("Bar", "Sofa", "Kitchen", "Office")
_BASE_URL = "http://mass.local:8097"


@pytest.fixture(autouse=True)
def update_state() -> Iterator[MagicMock]:
    """Keep the players from reporting state to a controller that is not there."""
    with patch.object(RaumfeldPlayer, "update_state") as mock:
        yield mock


def _player(room: str) -> RaumfeldPlayer:
    """Build a RaumfeldPlayer for a room, with just the group state."""
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player.room = room
    player._player_id = f"raumfeld_{room.lower()}"
    player._attr_group_members = []
    player._advance_task_id = f"raumfeld_advance_{room.lower()}"
    player._advance_armed = False
    player._next_media = None
    player._attr_active_source = None
    player._attr_playback_state = PlaybackState.IDLE
    return player


def _zone_provider(
    zones: list[list[str]],
) -> tuple[RaumfeldPlayerProvider, dict[str, RaumfeldPlayer]]:
    """Build a provider with a player per room, whose host reports the given zones."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    players = {room: _player(room) for room in ROOMS}
    by_id = {player.player_id: player for player in players.values()}
    provider._room_ids = {room: player.player_id for room, player in players.items()}
    provider.host = MagicMock()
    provider.host.get_zones.return_value = zones
    # the host lists a zone's rooms sorted; the first one given here is its coordinator
    provider.host.resolve = {}
    mass = MagicMock()
    mass.players.get_player.side_effect = by_id.get
    mass.players.all_players.return_value = list(by_id.values())
    # a task a player starts (a rate check after a join) is not run unless a test runs it
    mass.create_task.side_effect = lambda coro: coro.close()
    provider.mass = mass
    for player in players.values():
        player.mass = mass
    provider.logger = MagicMock()
    return provider, players


def _groups(players: dict[str, RaumfeldPlayer]) -> dict[str, list[str]]:
    """Return the non-empty member lists, as room names keyed by the leading room."""
    names = {player.player_id: room for room, player in players.items()}
    return {
        room: [names[pid] for pid in player._attr_group_members]
        for room, player in players.items()
        if player._attr_group_members
    }


def _sync(provider: RaumfeldPlayerProvider, players: dict[str, RaumfeldPlayer]) -> None:
    """Run a group sync with the provider's players in place."""
    with patch.object(
        RaumfeldPlayerProvider, "players", new_callable=lambda: property(lambda _: players.values())
    ):
        provider._sync_groups()


def test_a_zone_made_in_the_raumfeld_app_becomes_a_group() -> None:
    """A new zone takes the host's coordinator as leader; single rooms stay solo."""
    provider, players = _zone_provider([["Bar", "Sofa"], ["Kitchen"]])
    _sync(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


def test_the_leader_music_assistant_picked_keeps_leading(update_state: MagicMock) -> None:
    """A zone whose coordinator is another room than MA's leader does not swap leaders."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    sofa = players["Sofa"]
    sofa._attr_group_members = [sofa.player_id, players["Bar"].player_id]
    _sync(provider, players)
    assert _groups(players) == {"Sofa": ["Sofa", "Bar"]}
    # nothing changed, so no state update goes out
    update_state.assert_not_called()


def test_a_room_added_in_the_raumfeld_app_joins_the_existing_group() -> None:
    """The group keeps its leader and gains the new room."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    sofa = players["Sofa"]
    sofa._attr_group_members = [sofa.player_id, players["Bar"].player_id]
    _sync(provider, players)
    assert _groups(players) == {"Sofa": ["Sofa", "Bar", "Kitchen"]}


def test_a_dissolved_zone_clears_the_group() -> None:
    """Rooms the host no longer groups are solo again, the former leader included."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    bar = players["Bar"]
    bar._attr_group_members = [bar.player_id, players["Sofa"].player_id]
    _sync(provider, players)
    assert _groups(players) == {}


def test_merged_groups_end_up_with_a_single_leader() -> None:
    """Two groups merged into one zone never leave two players leading each other."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Office", "Sofa"]])
    bar, kitchen = players["Bar"], players["Kitchen"]
    bar._attr_group_members = [bar.player_id, players["Sofa"].player_id]
    kitchen._attr_group_members = [kitchen.player_id, players["Office"].player_id]
    _sync(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Kitchen", "Office", "Sofa"]}


async def _follow(provider: RaumfeldPlayerProvider, players: dict[str, RaumfeldPlayer]) -> None:
    """Run one look of the zone watcher with the provider's players in place."""
    with patch.object(
        RaumfeldPlayerProvider, "players", new_callable=lambda: property(lambda _: players.values())
    ):
        await provider._follow_zones()


async def test_the_zone_watcher_keeps_running_after_an_unexpected_error() -> None:
    """One look that fails unexpectedly is logged, and the next look runs as usual."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider.logger = MagicMock()
    looks = 0

    async def follow() -> None:
        nonlocal looks
        looks += 1
        if looks == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    with (
        patch.object(provider, "_follow_zones", side_effect=follow),
        patch("music_assistant.providers.raumfeld.provider.asyncio.sleep", AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._watch_zones()
    assert looks == 2
    provider.logger.exception.assert_called_once()


async def test_a_changed_zone_layout_is_followed_once_it_held() -> None:
    """A new layout is taken over on the second look in a row, not on the first."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    provider.host.get_zones.return_value = [["Bar", "Sofa"]]
    await _follow(provider, players)
    assert _groups(players) == {}
    await _follow(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


async def test_a_layout_that_keeps_changing_is_not_followed() -> None:
    """States the host passes through while regrouping are never mirrored."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    for zones in ([["Bar", "Sofa"]], [["Bar", "Kitchen"]], [["Bar", "Sofa"]]):
        provider.host.get_zones.return_value = zones
        await _follow(provider, players)
    assert _groups(players) == {}


async def test_zones_are_left_alone_while_a_change_from_music_assistant_settles() -> None:
    """A group change MA just made is not overwritten by what the host shows meanwhile."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    provider.host.get_zones.return_value = [["Bar", "Sofa"]]
    with patch("music_assistant.providers.raumfeld.provider.time.time", return_value=1000.0):
        with provider.changing_group():
            pass
        await _follow(provider, players)
        await _follow(provider, players)
        assert _groups(players) == {}
    with patch(
        "music_assistant.providers.raumfeld.provider.time.time",
        return_value=1000.0 + GROUP_SETTLE_TIME + 1,
    ):
        await _follow(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


async def test_zones_are_left_alone_while_a_change_from_music_assistant_runs() -> None:
    """A group change that takes longer than the settle time is not overwritten meanwhile."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    provider.host.get_zones.return_value = [["Bar", "Sofa"]]
    clock = "music_assistant.providers.raumfeld.provider.time.time"
    with provider.changing_group():
        with patch(clock, return_value=1000.0 + GROUP_SETTLE_TIME + 1):
            await _follow(provider, players)
            await _follow(provider, players)
        assert _groups(players) == {}
    # the settle time counts from the end of the change, a failed one included
    with pytest.raises(RuntimeError), provider.changing_group():
        raise RuntimeError
    await _follow(provider, players)
    assert _groups(players) == {}
    with patch(clock, return_value=time.time() + GROUP_SETTLE_TIME + 1):
        await _follow(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


async def test_a_group_made_here_and_undone_in_the_raumfeld_app_is_cleared() -> None:
    """A group MA made disappears when the host no longer has it, even if never mirrored."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    # grouped in Music Assistant, which the host has undone again before any look at it
    bar = players["Bar"]
    bar._attr_group_members = [bar.player_id, players["Sofa"].player_id]
    await _follow(provider, players)
    await _follow(provider, players)
    assert _groups(players) == {}


def _playing_group(
    provider: RaumfeldPlayerProvider,
    players: dict[str, RaumfeldPlayer],
    rooms: list[str],
    playing_zones: list[list[str]],
) -> MagicMock:
    """
    Make the first room lead the others, playing its own queue; return the mass mock.

    :param provider: The provider the players belong to.
    :param players: The players by room.
    :param rooms: The group's rooms, the leader first.
    :param playing_zones: The zones whose transport reads as playing; all others stop.
    """
    leader = players[rooms[0]]
    leader._attr_group_members = [players[room].player_id for room in rooms]
    leader._attr_active_source = leader.player_id
    leader._attr_playback_state = PlaybackState.PLAYING
    for player in players.values():
        player._provider = provider
        player.mass = provider.mass
        player.logger = provider.logger

    async def transport(zone: list[str]) -> dict[str, str]:
        state = "PLAYING" if zone in playing_zones else "STOPPED"
        return {"CurrentTransportState": state}

    async def position(zone: list[str]) -> dict[str, str]:
        if zone not in playing_zones:
            return {"TrackURI": ""}
        return {"TrackURI": f"{_BASE_URL}/flow/s1/{leader.player_id}/i1/{leader.player_id}.flac"}

    provider.host.async_get_transport_info = AsyncMock(side_effect=transport)
    provider.host.async_get_position_info = AsyncMock(side_effect=position)
    mass = cast("MagicMock", provider.mass)
    mass.streams.base_url = _BASE_URL
    queue = MagicMock(queue_id=leader.player_id, state=PlaybackState.PLAYING)
    mass.player_queues.get_active_queue.return_value = queue
    return mass


async def test_a_leader_taken_out_of_its_playing_group_hands_its_queue_over() -> None:
    """The rooms that play on get the leader's queue, as when MA takes the leader out."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], [["Kitchen", "Sofa"]])
    provider.host.get_zones.return_value = [["Bar"], ["Kitchen", "Sofa"]]
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_called_once_with(players["Bar"].player_id)
    mass.create_task.assert_called_once()
    # the core moves the group over; mirroring the zones first would empty it here
    assert _groups(players) == {"Bar": ["Bar", "Sofa", "Kitchen"]}


async def test_a_partial_split_hands_the_queue_to_the_part_that_plays_on() -> None:
    """A member staying with the leader does not hide the part that plays its queue on."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Office", "Sofa"]])
    mass = _playing_group(
        provider, players, ["Bar", "Sofa", "Kitchen", "Office"], [["Kitchen", "Office"]]
    )
    provider.host.get_zones.return_value = [["Bar", "Sofa"], ["Kitchen", "Office"]]
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_called_once_with(players["Bar"].player_id)
    assert _groups(players) == {"Bar": ["Bar", "Kitchen", "Office"]}


async def test_rooms_taken_out_of_a_playing_group_are_mirrored() -> None:
    """A leader that plays on keeps its queue; only the group is mirrored from the zones."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa"], [["Bar"]])
    provider.host.get_zones.return_value = [["Bar"], ["Sofa"]]
    await _follow(provider, players)
    await _follow(provider, players)
    mass.players.cmd_ungroup.assert_not_called()
    assert _groups(players) == {}


async def test_a_leader_whose_rooms_left_its_zone_waits_for_the_watcher() -> None:
    """Until the watcher tells who left, the leader's own poll must not end its queue."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [["Sofa"]])
    bar = players["Bar"]
    assert bar._zones_without_leader() == []
    provider.host.get_zones.return_value = [["Bar"], ["Sofa"]]
    # also while a group change MA made settles, when the watcher does not look yet
    with provider.changing_group(), patch.object(bar, "_update_power"):
        await bar.poll()
    assert bar._attr_playback_state == PlaybackState.PLAYING
    provider.host.async_get_transport_info.assert_not_awaited()


@pytest.mark.parametrize("failing", [["Kitchen", "Sofa"], ["Bar"]])
@pytest.mark.parametrize("reply", [None, {"CurrentTransportState": "TRANSITIONING"}])
async def test_a_zone_that_does_not_tell_holds_the_decision(
    failing: list[str], reply: dict[str, str] | None
) -> None:
    """A failed read, or a zone between states, tells nothing: the group waits, either zone."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], [["Kitchen", "Sofa"]])
    provider.host.get_zones.return_value = [["Bar"], ["Kitchen", "Sofa"]]
    answering = provider.host.async_get_transport_info.side_effect

    async def inconclusive(zone: list[str]) -> dict[str, str] | None:
        return reply if zone == failing else cast("dict[str, str]", await answering(zone))

    provider.host.async_get_transport_info.side_effect = inconclusive
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
        assert _groups(players) == {"Bar": ["Bar", "Sofa", "Kitchen"]}
        mass.players.cmd_ungroup.assert_not_called()
        provider.host.async_get_transport_info.side_effect = answering
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_called_once_with(players["Bar"].player_id)


async def test_zones_that_never_answer_are_mirrored_in_the_end() -> None:
    """A zone that keeps not answering does not keep the groups as they were for good."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], [])
    provider.host.get_zones.return_value = [["Bar"], ["Kitchen", "Sofa"]]
    provider.host.async_get_transport_info = AsyncMock(return_value=None)
    clock = "music_assistant.providers.raumfeld.provider.time.time"
    with patch(clock, return_value=1000.0):
        await _follow(provider, players)
        await _follow(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa", "Kitchen"]}
    with patch(clock, return_value=1000.0 + GROUP_SETTLE_TIME + 1):
        await _follow(provider, players)
    assert _groups(players) == {"Kitchen": ["Kitchen", "Sofa"]}


@pytest.mark.parametrize("playing", [[["Kitchen", "Sofa"]], [["Bar"]]])
async def test_a_group_change_during_the_reads_is_left_alone(playing: list[list[str]]) -> None:
    """A change MA starts while the zones are read is not undone by what was read before."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], playing)
    provider.host.get_zones.return_value = [["Bar"], ["Kitchen", "Sofa"]]
    answering = provider.host.async_get_transport_info.side_effect

    async def change_starts(zone: list[str]) -> dict[str, str]:
        provider._group_changes = 1
        return cast("dict[str, str]", await answering(zone))

    provider.host.async_get_transport_info.side_effect = change_starts
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_not_called()
    assert _groups(players) == {"Bar": ["Bar", "Sofa", "Kitchen"]}


async def test_a_play_during_the_reads_is_not_handed_over() -> None:
    """A queue started on the leader while its zone is read stays with the leader."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], [["Kitchen", "Sofa"]])
    provider.host.get_zones.return_value = [["Bar"], ["Kitchen", "Sofa"]]
    answering = provider.host.async_get_transport_info.side_effect
    bar = players["Bar"]

    async def play_starts(zone: list[str]) -> dict[str, str]:
        bar._playback_generation += 1
        return cast("dict[str, str]", await answering(zone))

    provider.host.async_get_transport_info.side_effect = play_starts
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_not_called()
    assert _groups(players) == {"Bar": ["Bar", "Sofa", "Kitchen"]}


async def test_only_the_rooms_that_play_on_get_the_queue() -> None:
    """Rooms that left the group's zone before the leader did are not handed its queue."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Office", "Sofa"]])
    mass = _playing_group(
        provider, players, ["Bar", "Sofa", "Kitchen", "Office"], [["Kitchen", "Office"]]
    )
    provider.host.get_zones.return_value = [["Bar"], ["Sofa"], ["Kitchen", "Office"]]
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_called_once_with(players["Bar"].player_id)
    assert _groups(players) == {"Bar": ["Bar", "Kitchen", "Office"]}


async def test_rooms_that_play_on_another_source_are_not_handed_the_queue() -> None:
    """Rooms that switched to another source after the leader left keep playing that."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa"], [["Sofa"]])
    provider.host.get_zones.return_value = [["Bar"], ["Sofa"]]
    provider.host.async_get_position_info = AsyncMock(
        return_value={"TrackURI": "http://opml.radiotime.com/Tune.ashx?id=s25777"}
    )
    await _follow(provider, players)
    await _follow(provider, players)
    mass.players.cmd_ungroup.assert_not_called()
    assert _groups(players) == {}


async def test_a_split_during_the_leaders_poll_leaves_its_queue_alone() -> None:
    """A zone split while the leader's poll reads it does not end the queue it plays."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [["Bar", "Sofa"]])
    bar = players["Bar"]

    async def split_while_read(_zone: list[str]) -> dict[str, str]:
        provider.host.get_zones.return_value = [["Bar"], ["Sofa"]]
        return {"CurrentTransportState": "STOPPED"}

    provider.host.async_get_transport_info = AsyncMock(side_effect=split_while_read)
    with (
        patch.object(bar, "_update_volume", AsyncMock()),
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
    ):
        await bar._poll_zone(provider.host, ["Bar", "Sofa"])
    assert bar._attr_playback_state == PlaybackState.PLAYING


async def test_a_room_that_joined_a_group_during_its_poll_drops_its_next_item() -> None:
    """With flow mode off, a stale reading of a new follower does not start its next item."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    _playing_group(provider, players, ["Sofa", "Bar"], [])
    bar, sofa = players["Bar"], players["Sofa"]
    bar._attr_playback_state = PlaybackState.PLAYING
    bar._advance_armed = True
    bar._next_media = MagicMock()
    synced_to = PropertyMock(return_value=None)

    async def joined_while_read(_zone: list[str]) -> dict[str, str]:
        synced_to.return_value = sofa.player_id
        return {"CurrentTransportState": "STOPPED"}

    provider.host.async_get_transport_info = AsyncMock(side_effect=joined_while_read)
    with (
        patch.object(bar, "_update_volume", AsyncMock()),
        patch.object(bar, "_advance_to_next") as advance,
        patch.object(RaumfeldPlayer, "synced_to", synced_to),
    ):
        await bar._poll_zone(provider.host, ["Bar"])
    advance.assert_not_called()
    assert _next_cleared(bar)


async def test_a_leader_switched_off_while_playing_leaves_its_zone_to_the_others() -> None:
    """The other rooms keep their zone, so handing them the queue needs no new one."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], [["Bar", "Kitchen", "Sofa"]])
    provider.host.room_is_spotify_single_room.return_value = False
    bar = players["Bar"]
    bar._attr_powered = True

    async def drop(_room: str) -> None:
        provider.host.get_zones.return_value = [["Kitchen", "Sofa"]]

    provider.host.async_drop_room_from_zone = AsyncMock(side_effect=drop)
    with (
        patch.object(
            bar, "_update_power", side_effect=lambda: setattr(bar, "_attr_powered", False)
        ),
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(bar, "_poll_zone", AsyncMock()),
    ):
        await bar.poll()
    provider.host.async_drop_room_from_zone.assert_awaited_once_with("Bar")
    # until MA has handed the queue over, the leader keeps reporting it as playing
    assert bar._attr_playback_state == PlaybackState.PLAYING


@pytest.mark.parametrize("case", ["already off", "idle", "not leading"])
async def test_a_room_switched_off_otherwise_stays_in_its_zone(case: str) -> None:
    """Only a leader of a playing group leaves its zone when it is switched off."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa", "Kitchen"], [["Bar", "Kitchen", "Sofa"]])
    provider.host.room_is_spotify_single_room.return_value = False
    provider.host.async_drop_room_from_zone = AsyncMock()
    bar = players["Bar"]
    bar._attr_powered = case != "already off"
    if case == "idle":
        bar._attr_playback_state = PlaybackState.IDLE
    if case == "not leading":
        bar._attr_group_members = []
    with (
        patch.object(
            bar, "_update_power", side_effect=lambda: setattr(bar, "_attr_powered", False)
        ),
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(bar, "_poll_zone", AsyncMock()),
    ):
        await bar.poll()
    provider.host.async_drop_room_from_zone.assert_not_awaited()


async def test_a_leader_that_switched_source_still_hands_its_queue_over() -> None:
    """A leader playing something else by now does not keep the queue its old rooms play."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    mass = _playing_group(
        provider, players, ["Bar", "Sofa", "Kitchen"], [["Kitchen", "Sofa"], ["Bar"]]
    )
    provider.host.get_zones.return_value = [["Bar"], ["Kitchen", "Sofa"]]
    queue_position = provider.host.async_get_position_info.side_effect

    async def radio_on_bar(zone: list[str]) -> dict[str, str]:
        if zone == ["Bar"]:
            return {"TrackURI": "http://opml.radiotime.com/Tune.ashx?id=s25777"}
        return cast("dict[str, str]", await queue_position(zone))

    provider.host.async_get_position_info.side_effect = radio_on_bar
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    mass.players.cmd_ungroup.assert_called_once_with(players["Bar"].player_id)


async def test_the_end_watch_leaves_a_split_group_to_the_watcher() -> None:
    """With flow mode off, the next item does not start on a leader its group left."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [["Sofa"]])
    bar = players["Bar"]
    bar._advance_armed = True
    bar._next_media = MagicMock()
    with (
        patch.object(bar, "_advance_to_next") as advance,
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
    ):
        await bar._watch_for_track_end(0)
    advance.assert_not_called()
    provider.host.async_get_transport_info.assert_not_awaited()


def _next_cleared(player: RaumfeldPlayer) -> bool:
    """Return whether the player's next item and its end watch were dropped."""
    return not player._advance_armed and player._next_media is None


@pytest.mark.parametrize("joins", ["before the watch", "during its read"])
async def test_a_room_that_joined_a_group_drops_its_own_next_item(joins: str) -> None:
    """With flow mode off, a new follower's own next item never replaces the group's audio."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    _playing_group(provider, players, ["Sofa", "Bar"], [])
    bar, sofa = players["Bar"], players["Sofa"]
    bar._advance_armed = True
    bar._next_media = MagicMock()
    synced_to = PropertyMock(return_value=sofa.player_id if joins == "before the watch" else None)

    async def joined_while_read(_zone: list[str]) -> dict[str, str]:
        synced_to.return_value = sofa.player_id
        return {"CurrentTransportState": "STOPPED"}

    provider.host.async_get_transport_info = AsyncMock(side_effect=joined_while_read)
    with (
        patch.object(bar, "_advance_to_next") as advance,
        patch.object(RaumfeldPlayer, "synced_to", synced_to),
    ):
        await bar._watch_for_track_end(0)
    advance.assert_not_called()
    assert _next_cleared(bar)


async def test_a_merge_of_two_playing_groups_keeps_the_queue_that_plays_on() -> None:
    """The room whose queue the merged zone plays leads, not the first one with a queue."""
    provider, players = _zone_provider([["Bar", "Sofa"], ["Kitchen", "Office"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [])
    kitchen = players["Kitchen"]
    kitchen._attr_group_members = [kitchen.player_id, players["Office"].player_id]
    kitchen._attr_active_source = kitchen.player_id
    kitchen._attr_playback_state = PlaybackState.PLAYING
    provider.host.get_zones.return_value = [["Bar", "Kitchen", "Office", "Sofa"]]
    provider.host.async_get_transport_info = AsyncMock(
        return_value={"CurrentTransportState": "PLAYING"}
    )
    kitchen_stream = f"{_BASE_URL}/flow/s1/{kitchen.player_id}/i1/{kitchen.player_id}.flac"
    provider.host.async_get_position_info = AsyncMock(return_value={"TrackURI": kitchen_stream})
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
    groups = _groups(players)
    assert list(groups) == ["Kitchen"]
    assert sorted(groups["Kitchen"]) == ["Bar", "Kitchen", "Office", "Sofa"]


def _merged_playing_groups() -> tuple[RaumfeldPlayerProvider, dict[str, RaumfeldPlayer], str]:
    """Build two playing groups the host merged into one zone; return Kitchen's stream."""
    provider, players = _zone_provider([["Bar", "Sofa"], ["Kitchen", "Office"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [])
    kitchen = players["Kitchen"]
    kitchen._attr_group_members = [kitchen.player_id, players["Office"].player_id]
    kitchen._attr_active_source = kitchen.player_id
    kitchen._attr_playback_state = PlaybackState.PLAYING
    provider.host.get_zones.return_value = [["Bar", "Kitchen", "Office", "Sofa"]]
    provider.host.async_get_transport_info = AsyncMock(
        return_value={"CurrentTransportState": "PLAYING"}
    )
    return provider, players, f"{_BASE_URL}/flow/s1/{kitchen.player_id}/i1/{kitchen.player_id}.flac"


@pytest.mark.parametrize("disturbance", ["no answer", "playback starts"])
async def test_a_merge_waits_for_reads_that_tell_its_queue(disturbance: str) -> None:
    """Reads that tell nothing, or that a play overtook, pick no leader; the next look does."""
    provider, players, kitchen_stream = _merged_playing_groups()
    kitchen = players["Kitchen"]

    async def disturbed(_zone: list[str]) -> dict[str, str] | None:
        if disturbance == "no answer":
            return None
        kitchen._playback_generation += 1
        return {"TrackURI": kitchen_stream}

    provider.host.async_get_position_info = AsyncMock(side_effect=disturbed)
    with patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None):
        await _follow(provider, players)
        await _follow(provider, players)
        assert set(_groups(players)) == {"Bar", "Kitchen"}
        provider.host.async_get_position_info = AsyncMock(return_value={"TrackURI": kitchen_stream})
        await _follow(provider, players)
    assert list(_groups(players)) == ["Kitchen"]


def test_the_room_playing_a_music_assistant_queue_leads_a_new_zone() -> None:
    """A room added in the Raumfeld app joins the room that plays MA's queue, not vice versa."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    sofa = players["Sofa"]
    sofa._attr_active_source = sofa.player_id
    sofa._attr_playback_state = PlaybackState.PLAYING
    _sync(provider, players)
    assert _groups(players) == {"Sofa": ["Sofa", "Bar"]}


def test_the_playing_room_leads_over_an_idle_leader_when_zones_merge() -> None:
    """An idle group merged into a room playing MA's queue does not take that queue's lead."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    bar, sofa = players["Bar"], players["Sofa"]
    bar._attr_group_members = [bar.player_id, players["Kitchen"].player_id]
    sofa._attr_active_source = sofa.player_id
    sofa._attr_playback_state = PlaybackState.PLAYING
    _sync(provider, players)
    assert _groups(players) == {"Sofa": ["Sofa", "Bar", "Kitchen"]}


def test_a_follower_still_naming_its_own_queue_does_not_take_the_lead() -> None:
    """A follower mirrors its leader's playing state, which is not a queue of its own."""
    provider, players = _zone_provider([["Bar", "Kitchen", "Sofa"]])
    bar, sofa = players["Bar"], players["Sofa"]
    sofa._attr_group_members = [sofa.player_id, bar.player_id]
    for player in (bar, sofa):
        player._attr_active_source = player.player_id
        player._attr_playback_state = PlaybackState.PLAYING
    _sync(provider, players)
    assert _groups(players) == {"Sofa": ["Sofa", "Bar", "Kitchen"]}


def test_a_room_that_follows_after_a_merge_drops_its_own_next_item() -> None:
    """With flow mode off, a merge drops a new follower's next item, before any split."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _playing_group(provider, players, ["Bar"], [])
    sofa = players["Sofa"]
    sofa._advance_armed = True
    sofa._next_media = MagicMock()
    provider.host.get_zones.return_value = [["Bar", "Sofa"]]
    _sync(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}
    assert _next_cleared(sofa)
    # split again: on its own, the room has nothing left for an end watch to start
    provider.host.get_zones.return_value = [["Bar"], ["Sofa"]]
    _sync(provider, players)
    assert _next_cleared(sofa)


async def test_a_reading_of_a_zone_the_room_left_is_dropped() -> None:
    """A zone change finishing during the poll's reads leaves the old zone's reading unused."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [])
    bar = players["Bar"]

    async def regrouped_while_read(_zone: list[str]) -> dict[str, str]:
        # the watcher mirrored the split during the read: Bar leads nothing any more
        provider.host.get_zones.return_value = [["Bar"], ["Sofa"]]
        bar._attr_group_members = []
        return {"CurrentTransportState": "STOPPED"}

    provider.host.async_get_transport_info = AsyncMock(side_effect=regrouped_while_read)
    with (
        patch.object(bar, "_update_volume", AsyncMock()),
        patch.object(RaumfeldPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
    ):
        await bar._poll_zone(provider.host, ["Bar", "Sofa"])
    assert bar._attr_playback_state == PlaybackState.PLAYING


class _Cache:
    """Keep what the provider caches in memory, until it is expired by the test."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str, int], Any] = {}

    async def get(self, key: str, provider: str = "default", category: int = 0) -> Any:
        return self.entries.get((key, provider, category))

    async def set(
        self, key: str, data: Any, expiration: int, provider: str = "default", category: int = 0
    ) -> None:
        assert expiration > 0
        self.entries[(key, provider, category)] = data


def _button_provider() -> tuple[RaumfeldPlayerProvider, MagicMock]:
    """Build a provider whose host has a media server, and return the button action."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider.host = MagicMock()
    provider.host.media_server_udn = "uuid:media-server"
    provider.host.resolve = {"udn_to_devloc": {"uuid:media-server": "http://host/ms.xml"}}
    provider.config = MagicMock(instance_id="raumfeld_test")
    provider.mass = MagicMock()
    provider.mass.cache = _Cache()
    provider.logger = MagicMock()
    provider.resolve_room_renderer = MagicMock(  # type: ignore[method-assign]
        return_value=("renderer-uuid", "192.168.1.42")
    )
    action = MagicMock()
    return provider, action


async def test_station_buttons_are_read_with_their_titles() -> None:
    """Assigned buttons are listed with their item and title; empty or unread ones are not."""
    provider, action = _button_provider()
    assigned = {1: "0/RadioTime/Search/s-s25777", 2: "", 3: "0/My Music/Albums/42", 4: ""}
    action.async_call = AsyncMock(
        side_effect=lambda **kwargs: {"ObjectID": assigned[kwargs["Button"]]}
    )
    listings = {
        "0/RadioTime/Search/s-s25777": '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/'
        'DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item id="0/RadioTime/Search/'
        's-s25777"><dc:title>NPO Radio 2</dc:title></item></DIDL-Lite>',
        "0/My Music/Albums/42": None,
    }
    provider.host.async_browse_media_server = AsyncMock(
        side_effect=lambda object_id, _flag: listings[object_id]
    )
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(return_value=action),
    ):
        buttons = await provider._read_station_buttons("Bar")
    # button 3's item could not be read, so whether it plays an album is unknown: left out
    assert buttons == [(1, "0/RadioTime/Search/s-s25777", "NPO Radio 2", False)]
    assert action.async_call.await_args_list[0].kwargs == {
        "Renderer": "uuid:renderer-uuid",
        "Button": 1,
    }


async def test_a_button_item_is_only_described_when_it_is_new() -> None:
    """Reading the buttons again describes a reassigned button's item, not the known ones."""
    provider, action = _button_provider()
    assigned = {1: "0/RadioTime/Search/s-s25777", 2: "", 3: "", 4: ""}
    action.async_call = AsyncMock(
        side_effect=lambda **kwargs: {"ObjectID": assigned[kwargs["Button"]]}
    )
    listing = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"><item id="{0}">'
        "<dc:title>{1}</dc:title></item></DIDL-Lite>"
    )
    titles = {"0/RadioTime/Search/s-s25777": "NPO Radio 2", "0/StationButtons/432": "WYWH 50"}
    provider.host.async_browse_media_server = AsyncMock(
        side_effect=lambda object_id, _flag: listing.format(object_id, titles[object_id])
    )
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(return_value=action),
    ):
        await provider._read_station_buttons("Bar")
        await provider._read_station_buttons("Bar")
        assert provider.host.async_browse_media_server.await_count == 1
        assigned[4] = "0/StationButtons/432"
        buttons = await provider._read_station_buttons("Bar")
    assert provider.host.async_browse_media_server.await_count == 2
    assert buttons == [
        (1, "0/RadioTime/Search/s-s25777", "NPO Radio 2", False),
        (4, "0/StationButtons/432", "WYWH 50", False),
    ]


async def test_a_renamed_button_item_gets_its_new_title_once_its_cache_expired() -> None:
    """An item that keeps its object id but is renamed shows the new title after the expiry."""
    provider, action = _button_provider()
    action.async_call = AsyncMock(
        side_effect=lambda **kwargs: {
            "ObjectID": "0/RadioTime/Search/s-s25777" if kwargs["Button"] == 1 else ""
        }
    )
    titles = iter(["NPO Radio 2", "NPO 2"])
    provider.host.async_browse_media_server = AsyncMock(
        side_effect=lambda object_id, _flag: (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<item id="{object_id}"><dc:title>{next(titles)}</dc:title></item></DIDL-Lite>'
        )
    )
    cache = cast("_Cache", provider.mass.cache)
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(return_value=action),
    ):
        assert (await provider._read_station_buttons("Bar") or [])[0][2] == "NPO Radio 2"
        cache.entries.clear()
        assert (await provider._read_station_buttons("Bar") or [])[0][2] == "NPO 2"


async def test_the_periodic_read_keeps_the_buttons_of_a_room_it_cannot_read() -> None:
    """A failed periodic read leaves a room's buttons; an empty read clears them."""
    provider, _ = _button_provider()
    player = RaumfeldPlayer.__new__(RaumfeldPlayer)
    player.room = "Bar"
    player.set_station_buttons = MagicMock()  # type: ignore[method-assign]
    with (
        patch.object(
            RaumfeldPlayerProvider, "players", new_callable=PropertyMock, return_value=[player]
        ),
        patch.object(provider, "_read_station_buttons", AsyncMock(side_effect=[None, []])),
    ):
        await provider._load_library_sources()
        player.set_station_buttons.assert_not_called()
        await provider._load_library_sources()
    player.set_station_buttons.assert_called_once_with([])


async def test_buttons_that_cannot_be_read_are_reported_as_unknown() -> None:
    """A media server that does not answer gives no buttons, not an empty list of them."""
    provider, _ = _button_provider()
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(side_effect=TimeoutError),
    ):
        assert await provider._read_station_buttons("Bar") is None


async def test_a_room_whose_buttons_cannot_be_read_again_keeps_them() -> None:
    """Reading a room's buttons before playing one leaves them as they were on a failure."""
    provider, _ = _button_provider()
    player = MagicMock(room="Bar")
    with patch.object(provider, "_read_station_buttons", AsyncMock(return_value=None)):
        await provider.refresh_station_buttons(player)
    player.set_station_buttons.assert_not_called()
    buttons = [(1, "0/RadioTime/Search/s-s25777", "NPO Radio 2", False)]
    with patch.object(provider, "_read_station_buttons", AsyncMock(return_value=buttons)):
        await provider.refresh_station_buttons(player)
    player.set_station_buttons.assert_called_once_with(buttons)


def _allow(player: RaumfeldPlayer, max_rate: int) -> None:
    """Give a room a sample rate setting with every rate up to max_rate, at 16 and 24 bit."""
    values = [
        f"{rate}{MULTI_VALUE_SPLITTER}{depth}"
        for rate in (44100, 48000, 88200, 96000, 176400, 192000)
        if rate <= max_rate
        for depth in (16, 24)
    ]
    player._config = MagicMock(get_value=MagicMock(return_value=values))


def test_a_group_plays_the_rates_every_room_plays() -> None:
    """The host relays the leader's stream unchanged, so the group's rates are the common ones."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    _playing_group(provider, players, ["Bar", "Sofa"], [["Bar", "Sofa"]])
    bar, sofa = players["Bar"], players["Sofa"]
    _allow(bar, 192000)
    _allow(sofa, 96000)
    rates = bar.supported_sample_rates
    assert rates is not None
    assert max(rate for rate, _ in rates) == 96000
    assert (96000, 24) in rates
    # a room on its own plays what its own setting allows
    assert sofa.supported_sample_rates is None


def _allow_16_bit(player: RaumfeldPlayer, max_rate: int) -> None:
    """Give a room a sample rate setting with every rate up to max_rate, at 16 bit only."""
    values = [
        f"{rate}{MULTI_VALUE_SPLITTER}16"
        for rate in (44100, 48000, 88200, 96000, 176400, 192000)
        if rate <= max_rate
    ]
    player._config = MagicMock(get_value=MagicMock(return_value=values))


def _joined_group(
    sofa_rates: Callable[[RaumfeldPlayer], None], stream_started_in_group: bool
) -> tuple[RaumfeldPlayer, MagicMock, MagicMock]:
    """Have Sofa join a group Bar leads and plays a stream in; return Bar, mass and queue."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    mass = _playing_group(provider, players, ["Bar", "Sofa"], [["Bar", "Sofa"]])
    bar, sofa = players["Bar"], players["Sofa"]
    _allow(bar, 192000)
    sofa_rates(sofa)
    # the rates offered when the stream started: Bar's own, or the group's
    bar._stream_rates = (
        bar.get_supported_sample_rates()
        if stream_started_in_group
        else bar._configured_sample_rates()
    )
    queue = MagicMock(queue_id=bar.player_id, corrected_elapsed_time=63.4)
    queue.current_item.duration = 240
    mass.players.get_active_queue.return_value = queue
    mass.player_queues.seek = AsyncMock()
    mass.player_queues.resume = AsyncMock()
    return bar, mass, queue


async def test_a_room_joining_restarts_a_stream_it_may_not_play() -> None:
    """A stream started at rates a joining room does not play starts again where it was."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_awaited_once_with(bar.player_id, 63)


async def test_a_stream_the_whole_group_already_plays_is_not_restarted() -> None:
    """A stream started while the room was in the group already plays at a rate it plays."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_not_awaited()


async def test_a_room_playing_only_16_bit_restarts_a_stream_at_the_same_rate() -> None:
    """A bit depth the joining room does not play needs a restart as much as a rate does."""
    bar, mass, _ = _joined_group(
        lambda sofa: _allow_16_bit(sofa, 192000), stream_started_in_group=False
    )
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_awaited_once_with(bar.player_id, 63)


async def test_radio_is_restarted_live_for_a_room_that_joined() -> None:
    """A stream without a length has no position to seek to, so it starts again live."""
    bar, mass, queue = _joined_group(
        lambda sofa: _allow(sofa, 96000), stream_started_in_group=False
    )
    queue.current_item.duration = 0
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_not_awaited()
    mass.player_queues.resume.assert_awaited_once_with(bar.player_id, fade_in=False)


def _held_playback_lock(mass: MagicMock) -> asyncio.Lock:
    """Give the players controller a real playback lock, and return it."""
    lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def player_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        async with lock:
            yield

    mass.players.get_group_and_player_lock = player_lock
    return lock


@pytest.mark.parametrize("meanwhile", ["stop", "seek"])
async def test_a_check_waits_for_playback_in_progress_and_looks_again(meanwhile: str) -> None:
    """A stop or a restart already in progress settles the check it held up."""
    bar, mass, queue = _joined_group(
        lambda sofa: _allow(sofa, 96000), stream_started_in_group=False
    )
    queue.current_item.duration = 0
    lock = _held_playback_lock(mass)
    await lock.acquire()
    check = asyncio.create_task(bar._restart_for_group_rates())
    await asyncio.sleep(0)
    assert not check.done()
    if meanwhile == "stop":
        bar._attr_playback_state = PlaybackState.IDLE
    else:
        # the stream that play started is at a format the whole group plays
        bar._stream_rates = bar.get_supported_sample_rates()
    lock.release()
    await check
    mass.player_queues.seek.assert_not_awaited()
    mass.player_queues.resume.assert_not_awaited()


async def test_a_sync_group_that_plays_through_the_room_is_restarted() -> None:
    """The Sync Group's queue starts again, never the room's own queue parked on radio."""
    bar, mass, queue = _joined_group(
        lambda sofa: _allow(sofa, 96000), stream_started_in_group=False
    )
    queue.queue_id = "syncgroup_living"
    own_queue = MagicMock(queue_id=bar.player_id)
    own_queue.current_item.duration = 0
    mass.player_queues.get.return_value = own_queue
    await bar._restart_for_group_rates()
    mass.players.get_active_queue.assert_called_once_with(bar)
    mass.player_queues.seek.assert_awaited_once_with("syncgroup_living", 63)
    mass.player_queues.resume.assert_not_awaited()


@pytest.mark.parametrize(("stream_started_in_group", "restarts"), [(False, True), (True, False)])
async def test_a_restart_for_the_rate_waits_for_the_rooms_to_play(
    stream_started_in_group: bool, restarts: bool
) -> None:
    """The stream restarts for a joined room once that room plays, and only if it has to."""
    bar, _, _ = _joined_group(
        lambda sofa: _allow(sofa, 96000), stream_started_in_group=stream_started_in_group
    )
    with (
        patch.object(bar, "_wait_for_rooms_playing", AsyncMock()) as wait,
        patch.object(bar, "_restart_for_group_rates", AsyncMock()) as restart,
    ):
        await bar._check_join(["raumfeld_sofa"])
    if restarts:
        wait.assert_awaited_once_with(["raumfeld_sofa"])
        restart.assert_awaited_once()
    else:
        wait.assert_not_awaited()
        restart.assert_not_awaited()


async def test_the_wait_ends_once_the_joined_room_plays() -> None:
    """The wait ends once the joined room reports PLAYING itself, not while it transitions."""
    bar, _, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    sofa_id = bar._attr_group_members[1]
    states = iter(["NO_MEDIA_PRESENT", "TRANSITIONING", "PLAYING"])
    reads = AsyncMock(side_effect=lambda _url: {"TransportState": next(states)})
    with (
        patch.object(bar.raumfeld, "room_transport_url", return_value="http://sofa/ctrl"),
        patch("music_assistant.providers.raumfeld.player.JOIN_SETTLE_INTERVAL", 0),
        patch.object(bar, "_read_room_state", reads),
    ):
        await bar._wait_for_rooms_playing([sofa_id])
    assert reads.await_count == 3


async def test_the_wait_ends_once_the_joined_room_cannot_hook_in() -> None:
    """A room that runs out of buffer and drops the stream gets the restart at once."""
    bar, _, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    sofa_id = bar._attr_group_members[1]
    states = iter(
        [
            {"TransportState": "TRANSITIONING", "TransportStatus": "OK"},
            {"TransportState": "NO_MEDIA_PRESENT", "TransportStatus": "buffer underrun"},
            {"TransportState": "PLAYING", "TransportStatus": "OK"},
        ]
    )
    reads = AsyncMock(side_effect=lambda _url: next(states))
    with (
        patch.object(bar.raumfeld, "room_transport_url", return_value="http://sofa/ctrl"),
        patch("music_assistant.providers.raumfeld.player.JOIN_SETTLE_INTERVAL", 0),
        patch.object(bar, "_read_room_state", reads),
    ):
        await bar._wait_for_rooms_playing([sofa_id])
    assert reads.await_count == 2


async def test_a_room_that_cannot_hook_in_leaves_the_others_waited_for() -> None:
    """One room dropping the stream does not end the wait for another still hooking in."""
    bar, _, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    rooms = {"raumfeld_sofa": _player("Sofa"), "raumfeld_kitchen": _player("Kitchen")}
    states = {
        "http://sofa/ctrl": iter(
            [{"TransportState": "NO_MEDIA_PRESENT", "TransportStatus": "buffer underrun"}]
        ),
        "http://kitchen/ctrl": iter(
            [
                {"TransportState": "TRANSITIONING", "TransportStatus": "OK"},
                {"TransportState": "TRANSITIONING", "TransportStatus": "OK"},
                {"TransportState": "PLAYING", "TransportStatus": "OK"},
            ]
        ),
    }
    reads = AsyncMock(side_effect=lambda url: next(states[url]))
    with (
        patch.object(bar.mass.players, "get_player", side_effect=rooms.get),
        patch.object(
            bar.raumfeld,
            "room_transport_url",
            side_effect=lambda room: f"http://{room.lower()}/ctrl",
        ),
        patch("music_assistant.providers.raumfeld.player.JOIN_SETTLE_INTERVAL", 0),
        patch.object(bar, "_read_room_state", reads),
    ):
        await bar._wait_for_rooms_playing(list(rooms))
    assert reads.await_count == 4


async def test_a_room_without_media_but_no_underrun_is_waited_for() -> None:
    """NO_MEDIA_PRESENT with status OK is a room not hooked in yet, so the wait goes on."""
    bar, _, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    sofa_id = bar._attr_group_members[1]
    states = iter(
        [
            {"TransportState": "NO_MEDIA_PRESENT", "TransportStatus": "OK"},
            {"TransportState": "PLAYING", "TransportStatus": "OK"},
        ]
    )
    reads = AsyncMock(side_effect=lambda _url: next(states))
    with (
        patch.object(bar.raumfeld, "room_transport_url", return_value="http://sofa/ctrl"),
        patch("music_assistant.providers.raumfeld.player.JOIN_SETTLE_INTERVAL", 0),
        patch.object(bar, "_read_room_state", reads),
    ):
        await bar._wait_for_rooms_playing([sofa_id])
    assert reads.await_count == 2


async def test_the_wait_for_slow_rooms_keeps_to_its_budget() -> None:
    """Rooms whose reads hang hold the restart up for JOIN_SETTLE_TIME in all, not per read."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    rooms = [bar._attr_group_members[1], "raumfeld_kitchen", "raumfeld_office"]
    players = {pid: MagicMock(spec=RaumfeldPlayer, room=pid) for pid in rooms[1:]}
    players[rooms[0]] = mass.players.get_player(rooms[0])
    mass.players.get_player.side_effect = players.get

    async def hangs(_url: str) -> dict[str, str]:
        await asyncio.Event().wait()
        return {}

    loop = asyncio.get_running_loop()
    with (
        patch.object(bar.raumfeld, "room_transport_url", side_effect=lambda room: f"http://{room}"),
        patch("music_assistant.providers.raumfeld.player.JOIN_SETTLE_INTERVAL", 0),
        patch("music_assistant.providers.raumfeld.player.JOIN_SETTLE_TIME", 0.05),
        patch.object(bar, "_read_room_state", AsyncMock(side_effect=hangs)),
    ):
        started = loop.time()
        await bar._wait_for_rooms_playing(rooms)
    assert loop.time() - started < 1


async def test_rooms_joining_a_playing_group_have_its_rate_checked() -> None:
    """A join made in Music Assistant while the leader plays its queue checks the rate."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _playing_group(provider, players, ["Bar"], [["Bar"]])
    bar, sofa = players["Bar"], players["Sofa"]
    bar._attr_group_members = []
    _allow(bar, 192000)
    _allow(sofa, 96000)
    started: list[Coroutine[Any, Any, None]] = []
    cast("MagicMock", bar.mass).create_task.side_effect = started.append
    with (
        patch.object(bar, "_add_rooms_to_zone", AsyncMock()),
        patch.object(bar, "_wake_rooms", AsyncMock()),
        patch.object(bar, "_check_join", AsyncMock()) as check,
    ):
        await bar.set_members([sofa.player_id])
        for coro in started:
            await coro
    check.assert_awaited_once_with([sofa.player_id])
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


async def test_a_join_seen_while_playback_starts_is_checked_once_it_plays() -> None:
    """A room grouped in the app while the leader's play still runs is checked after it."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    sofa_id = bar._attr_group_members[1]
    bar._attr_group_members = [bar.player_id]
    # the play has not reported playing yet, and holds the playback lock
    bar._attr_playback_state = PlaybackState.IDLE
    lock = _held_playback_lock(mass)
    await lock.acquire()
    started: list[asyncio.Task[None]] = []
    mass.create_task.side_effect = lambda coro: started.append(asyncio.create_task(coro))
    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock()):
        bar.set_group_members([bar.player_id, sofa_id])
        assert len(started) == 1
        await asyncio.sleep(0)
        # the play finishes and reports playing
        bar._attr_playback_state = PlaybackState.PLAYING
        lock.release()
        await started[0]
    mass.player_queues.seek.assert_awaited_once_with(bar.player_id, 63)


@pytest.mark.parametrize("media_type", [MediaType.FLOW_STREAM, MediaType.ANNOUNCEMENT])
async def test_audio_another_player_encoded_is_not_restarted_for_its_rate(
    media_type: MediaType,
) -> None:
    """A Universal Group's stream or an announcement comes as it is: no rates to check."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    media = PlayerMedia(uri="http://mass.local:8097/ugp/stream.flac", media_type=media_type)
    mass.streams.resolve_stream_url = AsyncMock(return_value=media.uri)
    with (
        patch.object(bar, "_ensure_playable_zone", AsyncMock(return_value=["Bank", "Bar"])),
        patch.object(bar, "_wake_rooms", AsyncMock()),
        patch.object(bar, "_stop_before_switching", AsyncMock()),
        patch.object(bar, "_zone_command", AsyncMock()),
        patch.object(bar, "set_current_media"),
        patch.object(RaumfeldPlayer, "flow_mode", new_callable=PropertyMock, return_value=True),
    ):
        await bar.play_media(media)
    assert bar._stream_rates is None
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_not_awaited()
    mass.player_queues.resume.assert_not_awaited()


async def test_a_room_leaving_while_the_stream_starts_widens_the_saved_rates() -> None:
    """A room leaving before the renderer fetched the stream: its rates count too."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    sofa_id = bar._attr_group_members[1]
    media = PlayerMedia(uri="library://track/1", media_type=MediaType.TRACK)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://mass.local:8097/flow.flac")

    async def sofa_leaves_meanwhile(_zone: list[str], action: str, **_kwargs: str) -> None:
        if action == "Play":
            bar._attr_group_members = [bar.player_id]

    with (
        patch.object(bar, "_ensure_playable_zone", AsyncMock(return_value=["Bar", "Sofa"])),
        patch.object(bar, "_wake_rooms", AsyncMock()),
        patch.object(bar, "_stop_before_switching", AsyncMock()),
        patch.object(bar, "_zone_command", AsyncMock(side_effect=sofa_leaves_meanwhile)),
        patch.object(bar, "set_current_media"),
        patch.object(RaumfeldPlayer, "flow_mode", new_callable=PropertyMock, return_value=True),
    ):
        await bar.play_media(media)
    assert bar._stream_rates is not None
    assert max(rate for rate, _ in bar._stream_rates) == 192000
    # so the room rejoining has the stream restarted, whichever rates it was encoded at
    bar._attr_group_members = [bar.player_id, sofa_id]
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_awaited_once_with(bar.player_id, 63)


async def test_the_next_item_starts_under_the_playback_lock() -> None:
    """With flow mode off, the next item's play waits for a group rate check in progress."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    lock = _held_playback_lock(mass)
    await lock.acquire()
    bar._advance_armed = True
    with patch.object(bar, "play_media", AsyncMock()) as play:
        start = asyncio.create_task(bar._play_next(PlayerMedia(uri="library://track/2")))
        await asyncio.sleep(0)
        play.assert_not_awaited()
        lock.release()
        await start
    play.assert_awaited_once()


@pytest.mark.parametrize(("joins", "checked"), [(True, True), (False, False)])
async def test_a_room_grouped_in_the_raumfeld_app_has_the_rate_checked(
    joins: bool, checked: bool
) -> None:
    """A room the Raumfeld app adds to a playing group is checked like a join from MA."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    mass = _playing_group(provider, players, ["Bar"], [["Bar"]])
    bar, sofa = players["Bar"], players["Sofa"]
    bar._attr_group_members = [bar.player_id, sofa.player_id] if not joins else []
    started: list[Coroutine[Any, Any, None]] = []
    mass.create_task.side_effect = started.append
    with patch.object(bar, "_check_join", AsyncMock()) as check:
        # what following the zones does once the host shows Sofa in Bar's zone
        bar.set_group_members([bar.player_id, sofa.player_id])
        for coro in started:
            await coro
    assert check.await_count == (1 if checked else 0)


async def test_unload_waits_for_its_tasks_to_end() -> None:
    """The supervisor, the update loop and the zone watcher have ended once unload returns."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    ended: list[str] = []

    async def runs_until_cancelled(name: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # a long-poll that needs a moment to close its connection
            await asyncio.sleep(0.01)
            ended.append(name)
            raise

    provider._supervisor_task = asyncio.create_task(runs_until_cancelled("supervisor"))
    provider._update_task = asyncio.create_task(runs_until_cancelled("update"))
    provider._zone_watch_task = asyncio.create_task(runs_until_cancelled("zones"))
    await asyncio.sleep(0)
    await provider.unload()
    assert sorted(ended) == ["supervisor", "update", "zones"]
    assert provider._supervisor_task is None


async def test_a_next_item_waiting_for_the_lock_gives_way_to_a_newer_play() -> None:
    """A play that replaced the ending track while the advance waited is not undone."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    lock = _held_playback_lock(mass)
    await lock.acquire()
    bar._advance_armed = True
    with patch.object(bar, "play_media", AsyncMock()) as play:
        start = asyncio.create_task(bar._play_next(PlayerMedia(uri="library://track/2")))
        await asyncio.sleep(0)
        # a seek or a track the user picked runs under the lock meanwhile
        bar._playback_generation += 2
        lock.release()
        await start
    play.assert_not_awaited()


async def test_a_next_item_waiting_for_the_lock_is_dropped_when_the_room_follows() -> None:
    """A room that became a follower while its advance waited does not start the item."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._advance_armed = True
    lock = _held_playback_lock(mass)
    await lock.acquire()
    with patch.object(bar, "play_media", AsyncMock()) as play:
        start = asyncio.create_task(bar._play_next(PlayerMedia(uri="library://track/2")))
        await asyncio.sleep(0)
        # following the zones makes the room another one's follower meanwhile
        bar.drop_next_item()
        lock.release()
        await start
    play.assert_not_awaited()


async def test_the_next_item_takes_the_lock_strictly() -> None:
    """The next item never starts without the lock, as the queue's own play actions do."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._advance_armed = True
    taken: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def player_lock(*_args: Any, **kwargs: Any) -> AsyncIterator[None]:
        taken.append(kwargs)
        yield

    mass.players.get_group_and_player_lock = player_lock
    with patch.object(bar, "play_media", AsyncMock()) as play:
        await bar._play_next(PlayerMedia(uri="library://track/2"))
    assert taken == [{"strict": True}]
    play.assert_awaited_once()


async def test_a_next_item_that_never_gets_the_lock_is_not_started() -> None:
    """A player kept busy past the strict timeout leaves the next item to that action."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._advance_armed = True

    busy = MagicMock()
    busy.return_value.__aenter__ = AsyncMock(side_effect=ResourceBusyError("busy"))
    mass.players.get_group_and_player_lock = busy
    with patch.object(bar, "play_media", AsyncMock()) as play:
        await bar._play_next(PlayerMedia(uri="library://track/2"))
    play.assert_not_awaited()


async def test_the_rate_check_takes_the_lock_strictly() -> None:
    """The check never looks at playback without the lock, and gives up if it stays taken."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)
    taken: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def player_lock(*_args: Any, **kwargs: Any) -> AsyncIterator[None]:
        taken.append(kwargs)
        yield

    mass.players.get_group_and_player_lock = player_lock
    await bar._restart_for_group_rates()
    assert taken == [{"strict": True}]
    mass.player_queues.seek.assert_awaited_once()

    busy = MagicMock()
    busy.return_value.__aenter__ = AsyncMock(side_effect=ResourceBusyError("busy"))
    mass.players.get_group_and_player_lock = busy
    mass.player_queues.seek.reset_mock()
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_not_awaited()


@pytest.mark.parametrize(("restart_on_join", "restarts"), [(False, False), (True, True)])
async def test_a_join_restarts_a_stream_the_group_plays_when_set_to(
    restart_on_join: bool, restarts: bool
) -> None:
    """With restart on join set, a join restarts the stream even when its rate fits."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    rates = cast("MagicMock", bar._config).get_value.return_value
    bar._config = MagicMock(
        get_value=MagicMock(
            side_effect=lambda key, *_: restart_on_join if key == CONF_RESTART_ON_JOIN else rates
        )
    )
    await bar._restart_for_group_rates()
    assert mass.player_queues.seek.await_count == (1 if restarts else 0)


async def test_a_join_restarts_the_stream_when_the_joining_room_asks_for_it() -> None:
    """The setting on the room that plays behind counts as much as on the leader."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    sofa = mass.players.get_player(bar._attr_group_members[1])
    rates = cast("MagicMock", sofa._config).get_value.return_value
    sofa._config = MagicMock(
        get_value=MagicMock(side_effect=lambda key, *_: key == CONF_RESTART_ON_JOIN or rates)
    )
    await bar._restart_for_group_rates()
    mass.player_queues.seek.assert_awaited_once_with(bar.player_id, 63)


def _restart_on_join(player: RaumfeldPlayer, on: bool) -> None:
    """Switch the restart on join setting of a room on or off, keeping its sample rates."""
    rates = cast("MagicMock", player._config).get_value.return_value
    player._config = MagicMock(
        get_value=MagicMock(
            side_effect=lambda key, *_: on if key == CONF_RESTART_ON_JOIN else rates
        )
    )


@pytest.mark.parametrize(
    ("stream_started_in_group", "restart_on_join", "restarts"),
    [(True, True, True), (True, False, False), (False, True, True), (False, False, True)],
)
async def test_a_join_restarts_once_the_rooms_play_when_set_or_needed(
    stream_started_in_group: bool, restart_on_join: bool, restarts: bool
) -> None:
    """A restart for sync or for the rate waits for the joined rooms; none, no wait."""
    bar, _, _ = _joined_group(
        lambda sofa: _allow(sofa, 96000), stream_started_in_group=stream_started_in_group
    )
    _restart_on_join(bar, restart_on_join)
    with (
        patch.object(bar, "_wait_for_rooms_playing", AsyncMock()) as wait,
        patch.object(bar, "_restart_for_group_rates", AsyncMock()) as restart,
    ):
        await bar._check_join(["raumfeld_sofa"])
    assert wait.await_count == restart.await_count == (1 if restarts else 0)


def test_restart_on_join_is_off_by_default() -> None:
    """Only a user who hears the offset of a joining room switches the restart on."""
    entry = next(e for e in PLAYER_CONFIG_ENTRIES if e.key == CONF_RESTART_ON_JOIN)
    assert entry.default_value is False


async def test_overlapping_joins_restart_the_stream_once() -> None:
    """Two joins checked together: the second finds the stream already restarted for both."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    _restart_on_join(bar, True)

    async def restarted(*_args: Any, **_kwargs: Any) -> None:
        # MA plays the queue again, which starts a new stream
        bar._playback_generation += 2

    mass.player_queues.seek = AsyncMock(side_effect=restarted)
    both_joined = asyncio.Event()
    waiting = 0

    async def wait_for_both(_rooms: list[str]) -> None:
        # both rooms joined the same stream before either one plays
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            both_joined.set()
        await both_joined.wait()

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=wait_for_both)):
        await asyncio.gather(
            bar._check_join(["raumfeld_sofa"]), bar._check_join(["raumfeld_kitchen"])
        )
    mass.player_queues.seek.assert_awaited_once()


async def test_overlapping_joins_restart_once_the_last_room_plays() -> None:
    """A room hooked in first waits for a room of another join that still hooks in."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._attr_group_members = [*bar._attr_group_members, "raumfeld_kitchen"]
    _allow(mass.players.get_player("raumfeld_kitchen"), 96000)
    _restart_on_join(bar, True)
    kitchen_plays = asyncio.Event()
    restarted_while_kitchen_waits = False

    async def wait(rooms: list[str]) -> None:
        if rooms == ["raumfeld_kitchen"]:
            await kitchen_plays.wait()

    async def restart(*_args: Any, **_kwargs: Any) -> None:
        nonlocal restarted_while_kitchen_waits
        restarted_while_kitchen_waits = not kitchen_plays.is_set()

    mass.player_queues.seek = AsyncMock(side_effect=restart)
    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=wait)):
        kitchen = asyncio.create_task(bar._check_join(["raumfeld_kitchen"]))
        await asyncio.sleep(0)
        await bar._check_join(["raumfeld_sofa"])
        mass.player_queues.seek.assert_not_awaited()
        kitchen_plays.set()
        await kitchen
    mass.player_queues.seek.assert_awaited_once()
    assert not restarted_while_kitchen_waits


async def test_a_room_that_joined_after_a_seek_is_restarted_for() -> None:
    """A room that joined the stream a seek started lags, even if an earlier join ends last."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._attr_group_members = [*bar._attr_group_members, "raumfeld_kitchen"]
    _allow(mass.players.get_player("raumfeld_kitchen"), 96000)
    _restart_on_join(bar, True)
    kitchen_plays = asyncio.Event()

    async def wait(rooms: list[str]) -> None:
        if rooms == ["raumfeld_kitchen"]:
            await kitchen_plays.wait()

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=wait)):
        kitchen = asyncio.create_task(bar._check_join(["raumfeld_kitchen"]))
        await asyncio.sleep(0)
        # a seek starts a new stream, which Sofa then joins while Kitchen still hooks in
        bar._playback_generation += 2
        await bar._check_join(["raumfeld_sofa"])
        kitchen_plays.set()
        await kitchen
    mass.player_queues.seek.assert_awaited_once()


async def test_a_room_joining_while_a_check_waits_for_the_lock_is_waited_for() -> None:
    """A check that got the lock while another join's room still hooks in leaves it the restart."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._attr_group_members = [*bar._attr_group_members, "raumfeld_kitchen"]
    _allow(mass.players.get_player("raumfeld_kitchen"), 96000)
    _restart_on_join(bar, True)
    lock_free = asyncio.Event()
    kitchen_plays = asyncio.Event()
    waiting_for_lock = asyncio.Event()

    @contextlib.asynccontextmanager
    async def player_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        waiting_for_lock.set()
        await lock_free.wait()
        yield

    async def wait(rooms: list[str]) -> None:
        if rooms == ["raumfeld_kitchen"]:
            await kitchen_plays.wait()

    mass.players.get_group_and_player_lock = player_lock
    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=wait)):
        sofa = asyncio.create_task(bar._check_join(["raumfeld_sofa"]))
        await waiting_for_lock.wait()
        kitchen = asyncio.create_task(bar._check_join(["raumfeld_kitchen"]))
        await asyncio.sleep(0)
        lock_free.set()
        await sofa
        mass.player_queues.seek.assert_not_awaited()
        kitchen_plays.set()
        await kitchen
    mass.player_queues.seek.assert_awaited_once()


async def test_a_room_that_rejoins_while_an_earlier_check_waits_is_waited_for() -> None:
    """A room that leaves and joins again is waited for, even once the first check is done."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._attr_group_members = [*bar._attr_group_members, "raumfeld_kitchen"]
    _allow(mass.players.get_player("raumfeld_kitchen"), 96000)
    _restart_on_join(bar, True)
    first_done = asyncio.Event()
    rejoined_plays = asyncio.Event()
    calls = 0

    async def wait(_rooms: list[str]) -> None:
        nonlocal calls
        calls += 1
        await (first_done if calls == 1 else rejoined_plays).wait()

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=wait)):
        first = asyncio.create_task(bar._check_join(["raumfeld_sofa", "raumfeld_kitchen"]))
        await asyncio.sleep(0)
        # Sofa leaves and joins again while the first check still waits
        rejoin = asyncio.create_task(bar._check_join(["raumfeld_sofa"]))
        await asyncio.sleep(0)
        first_done.set()
        await first
        mass.player_queues.seek.assert_not_awaited()
        rejoined_plays.set()
        await rejoin
    mass.player_queues.seek.assert_awaited_once()


async def test_a_room_that_rejoined_after_a_seek_counts_with_the_new_stream() -> None:
    """An older check that ends last does not hide the newer stream a room rejoined."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    bar._attr_group_members = [*bar._attr_group_members, "raumfeld_kitchen"]
    _allow(mass.players.get_player("raumfeld_kitchen"), 96000)
    _restart_on_join(bar, True)
    kitchen_plays = asyncio.Event()

    async def wait(rooms: list[str]) -> None:
        if "raumfeld_kitchen" in rooms:
            await kitchen_plays.wait()

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=wait)):
        first = asyncio.create_task(bar._check_join(["raumfeld_sofa", "raumfeld_kitchen"]))
        await asyncio.sleep(0)
        # a seek starts a new stream, then Sofa leaves and joins that one again
        bar._playback_generation += 2
        await bar._check_join(["raumfeld_sofa"])
        kitchen_plays.set()
        await first
    mass.player_queues.seek.assert_awaited_once()


async def test_a_room_that_left_while_hooked_in_leaves_no_restart() -> None:
    """A room gone from the group again before it played needs no restart for sync."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    _restart_on_join(bar, True)

    async def sofa_leaves(_rooms: list[str]) -> None:
        bar._attr_group_members = [bar.player_id]

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=sofa_leaves)):
        await bar._check_join(["raumfeld_sofa"])
    mass.player_queues.seek.assert_not_awaited()


async def test_a_seek_while_waiting_for_the_joined_room_leaves_no_restart() -> None:
    """A seek during the wait already started a stream every room joined at once."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=True)
    _restart_on_join(bar, True)

    async def user_seeks(_rooms: list[str]) -> None:
        bar._playback_generation += 2

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=user_seeks)):
        await bar._check_join(["raumfeld_sofa"])
    mass.player_queues.seek.assert_not_awaited()


async def test_a_seek_while_waiting_still_restarts_a_rate_the_room_cannot_play() -> None:
    """A new stream started at the old rates still gets the rate check."""
    bar, mass, _ = _joined_group(lambda sofa: _allow(sofa, 96000), stream_started_in_group=False)

    async def user_seeks(_rooms: list[str]) -> None:
        bar._playback_generation += 2

    with patch.object(bar, "_wait_for_rooms_playing", AsyncMock(side_effect=user_seeks)):
        await bar._check_join(["raumfeld_sofa"])
    mass.player_queues.seek.assert_awaited_once()


def _supervised(answers: list[bool]) -> tuple[RaumfeldPlayerProvider, AsyncMock]:
    """Build a connected provider whose host answers the health checks as given."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider._connected = True
    provider._host_address = "192.168.1.23"
    provider._host_port = 47365
    provider._update_task = MagicMock(done=MagicMock(return_value=False))
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider._resync = AsyncMock()  # type: ignore[method-assign]
    disconnect = AsyncMock()
    provider._disconnect = disconnect  # type: ignore[method-assign]
    return provider, AsyncMock(side_effect=answers)


async def _supervise_cycles(
    provider: RaumfeldPlayerProvider, check: AsyncMock, cycles: int
) -> None:
    """Run the supervisor for the given number of cycles."""
    sleeps = 0

    async def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= cycles:
            raise asyncio.CancelledError

    with (
        patch("music_assistant.providers.raumfeld.provider.host_is_valid", check),
        patch("music_assistant.providers.raumfeld.provider.asyncio.sleep", sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._supervise()


async def test_a_host_that_misses_a_check_now_and_then_stays_connected() -> None:
    """Two missed health checks followed by an answer keep the connection."""
    provider, check = _supervised([False, False, True, False])
    await _supervise_cycles(provider, check, 4)
    provider._disconnect.assert_not_called()  # type: ignore[attr-defined]
    assert provider._host_check_failures == 1


async def test_a_host_that_misses_three_checks_in_a_row_is_dropped() -> None:
    """Three missed health checks in a row count as a lost host."""
    provider, check = _supervised([False, False, False])
    await _supervise_cycles(provider, check, 3)
    provider._disconnect.assert_awaited_once_with("host connection lost")  # type: ignore[attr-defined]


async def test_an_ended_update_loop_drops_the_connection_at_once() -> None:
    """Hassfeld's update loop having ended is not a slow host: drop without waiting."""
    provider, check = _supervised([True])
    provider._update_task = MagicMock(done=MagicMock(return_value=True))
    await _supervise_cycles(provider, check, 1)
    provider._disconnect.assert_awaited_once_with("host connection lost")  # type: ignore[attr-defined]
    check.assert_not_called()


def _icy_provider(stored: dict[str, Any]) -> RaumfeldPlayerProvider:
    """
    Build a provider whose player config holds the given raw values.

    :param stored: The raw values stored for the player, changed in place on a write.
    """
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    mass = MagicMock()
    mass.config.get_raw_player_config_value.side_effect = lambda _pid, key: stored.get(key)
    mass.config.set_raw_player_config_value.side_effect = lambda _pid, key, value: (
        stored.__setitem__(key, value)
    )
    provider.mass = mass
    return provider


@pytest.mark.parametrize(
    ("stored", "expected"),
    [({}, "basic"), ({"enable_icy_metadata": "full"}, "full")],
    ids=["new_room", "own_choice"],
)
def test_a_room_without_an_icy_preference_gets_basic(stored: dict[str, Any], expected: str) -> None:
    """A room with nothing stored gets "basic"; a stored value is left for the user."""
    provider = _icy_provider(stored)

    provider._ensure_icy_default("raumfeld_bar")

    assert stored["enable_icy_metadata"] == expected
