"""Tests for the Teufel Raumfeld player provider."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

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


def _follow(provider: RaumfeldPlayerProvider, players: dict[str, RaumfeldPlayer]) -> None:
    """Run one look of the zone watcher with the provider's players in place."""
    with patch.object(
        RaumfeldPlayerProvider, "players", new_callable=lambda: property(lambda _: players.values())
    ):
        provider._follow_zones()


def test_a_changed_zone_layout_is_followed_once_it_held() -> None:
    """A new layout is taken over on the second look in a row, not on the first."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    provider.host.get_zones.return_value = [["Bar", "Sofa"]]
    _follow(provider, players)
    assert _groups(players) == {}
    _follow(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


def test_a_layout_that_keeps_changing_is_not_followed() -> None:
    """States the host passes through while regrouping are never mirrored."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    for zones in ([["Bar", "Sofa"]], [["Bar", "Kitchen"]], [["Bar", "Sofa"]]):
        provider.host.get_zones.return_value = zones
        _follow(provider, players)
    assert _groups(players) == {}


def test_zones_are_left_alone_while_a_change_from_music_assistant_settles() -> None:
    """A group change MA just made is not overwritten by what the host shows meanwhile."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    provider.host.get_zones.return_value = [["Bar", "Sofa"]]
    with patch("music_assistant.providers.raumfeld.provider.time.time", return_value=1000.0):
        provider.note_group_change()
        _follow(provider, players)
        _follow(provider, players)
        assert _groups(players) == {}
    with patch(
        "music_assistant.providers.raumfeld.provider.time.time",
        return_value=1000.0 + GROUP_SETTLE_TIME + 1,
    ):
        _follow(provider, players)
    assert _groups(players) == {"Bar": ["Bar", "Sofa"]}


def test_a_group_made_here_and_undone_in_the_raumfeld_app_is_cleared() -> None:
    """A group MA made disappears when the host no longer has it, even if never mirrored."""
    provider, players = _zone_provider([["Bar"], ["Sofa"]])
    _sync(provider, players)
    # grouped in Music Assistant, which the host has undone again before any look at it
    bar = players["Bar"]
    bar._attr_group_members = [bar.player_id, players["Sofa"].player_id]
    _follow(provider, players)
    _follow(provider, players)
    assert _groups(players) == {}


def test_the_room_playing_a_music_assistant_queue_leads_a_new_zone() -> None:
    """A room added in the Raumfeld app joins the room that plays MA's queue, not vice versa."""
    provider, players = _zone_provider([["Bar", "Sofa"]])
    sofa = players["Sofa"]
    sofa._attr_active_source = sofa.player_id
    sofa._attr_playback_state = PlaybackState.PLAYING
    _sync(provider, players)
    assert _groups(players) == {"Sofa": ["Sofa", "Bar"]}
