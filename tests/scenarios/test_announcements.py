"""Who an announcement plays on, and what plays on after it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, PlaybackState

from .test_group_power_on import _announce
from .test_grouping import _player_id, _speakers, _state, _stop

if TYPE_CHECKING:
    from .harness import FakeProtocol, Scenario


def _commands(speaker: FakeProtocol) -> list[tuple[object, ...]]:
    """Return the playback and grouping commands a speaker received, what it played by type."""
    return [
        (command[0], command[1].media_type) if command[0] == "play_media" else command
        for command in speaker.commands
        if command[0] in ("play_media", "stop", "set_members")
    ]


async def test_an_announcement_to_a_leader_plays_on_the_leader_alone(scenario: Scenario) -> None:
    """
    An announcement to a player that leads others plays on that player alone.

    Kitchen leads Office and plays. The announcement to Kitchen stops the group and takes
    Office off Kitchen, so only Kitchen plays the clip. Office joins Kitchen again after it,
    and the group plays on, on both.
    """
    _, (kitchen, office) = await _speakers(scenario, "Kitchen", "Office")
    kitchen_id, office_id = _player_id(scenario, kitchen), _player_id(scenario, office)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()
    await scenario.play(kitchen_id)
    kitchen.commands.clear()

    await _announce(scenario, kitchen_id, None, [kitchen, office])
    await scenario.idle()

    assert _commands(kitchen) == [
        ("stop",),
        ("set_members", None, ["sonos_office"]),
        ("play_media", MediaType.ANNOUNCEMENT),
        ("stop",),
        ("set_members", ["sonos_office"], None),
        ("play_media", MediaType.TRACK),
    ]
    assert _commands(office) == []
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, office_id]
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING
    await _stop(scenario, kitchen_id)
