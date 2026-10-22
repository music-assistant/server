"""Tests for the Teufel Raumfeld player provider."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.raumfeld.constants import GROUP_SETTLE_TIME
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
    provider.mass = mass
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


def _button_provider() -> tuple[RaumfeldPlayerProvider, MagicMock]:
    """Build a provider whose host has a media server, and return the button action."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider.host = MagicMock()
    provider.host.media_server_udn = "uuid:media-server"
    provider.host.resolve = {"udn_to_devloc": {"uuid:media-server": "http://host/ms.xml"}}
    provider.mass = MagicMock()
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


async def test_a_room_whose_buttons_cannot_be_read_offers_none() -> None:
    """A media server that does not answer leaves the room without button sources."""
    provider, _ = _button_provider()
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(side_effect=TimeoutError),
    ):
        assert await provider._read_station_buttons("Bar") == []
