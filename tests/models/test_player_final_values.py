"""Tests that a player gives every value of the state it published, under the same name."""

from __future__ import annotations

import dataclasses

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import Player as PlayerState
from music_assistant_models.player import PlayerMedia

from music_assistant.models.player import Player
from tests.common import MockProvider, attach_mock, make_player_state, mock_player_mass

STATE_FIELDS = [field.name for field in dataclasses.fields(PlayerState)]
STATE_PROPERTIES = [
    name for name, member in vars(PlayerState).items() if isinstance(member, property)
]
FINAL_VALUES = [*STATE_FIELDS, *STATE_PROPERTIES]


@pytest.fixture
def player() -> Player:
    """Return a player with a protocol attached."""
    return attach_mock(MockProvider("test", mass=mock_player_mass()), "device")[0]


def test_every_value_of_the_state_is_a_read_only_property_of_the_player() -> None:
    """A player has a property without a setter for each field and property of its state."""
    for name in FINAL_VALUES:
        member = vars(Player).get(name)
        assert isinstance(member, property), name
        assert member.fset is None, name


def test_the_final_values_are_one_block_in_the_order_of_the_state() -> None:
    """The final values follow each other on the player in the order the state has them in."""
    members = list(vars(Player))
    first = members.index(FINAL_VALUES[0])
    assert members[first : first + len(FINAL_VALUES)] == FINAL_VALUES


def test_a_player_gives_the_fields_of_its_state(player: Player) -> None:
    """Each field of the player is the very value its state holds."""
    state = make_player_state(player.player_id)
    for name in STATE_FIELDS:
        # the id of a player never changes
        if name != "player_id":
            setattr(state, name, object())
    player._state = state

    for name in STATE_FIELDS:
        assert getattr(player, name) is getattr(state, name), name


def test_a_player_gives_what_its_state_calculates(player: Player) -> None:
    """Each value the state calculates from its fields is that value on the player."""
    player._state = make_player_state(
        player.player_id,
        playback_state=PlaybackState.PAUSED,
        elapsed_time=12.5,
        elapsed_time_last_updated=1.0,
        current_media=PlayerMedia(uri="http://stream", queue_item_id="item_1"),
    )

    for name in STATE_PROPERTIES:
        value = getattr(player, name)
        assert value is not None, name
        assert value == getattr(player.state, name), name
