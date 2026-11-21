"""
Tests for the line-in of the other speakers of a Sonos household.

A speaker lists the line-in of every other speaker of its household that has one, and loads
it on its group when it is selected. Sonos does not report whose line-in plays, so a speaker
reports the line-in it loaded itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.api.models import ContainerType, MusicService
from aiosonos.api.models import PlayBackState as SonosPlayBackState

from music_assistant.providers.sonos.const import (
    PLAYER_SOURCE_MAP,
    SOURCE_LINE_IN,
    SOURCE_LINE_IN_OF,
)
from tests.providers.sonos.test_player import _actions, _connected_player

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo

    from music_assistant.providers.sonos.player import SonosPlayer

HOUSEHOLD = "Sonos_household"
DEN = "RINCON_DEN000000001400"
BATHROOM = "RINCON_BATHROOM00001400"
LINE_IN_OF_DEN = f"{SOURCE_LINE_IN_OF}{DEN}"


def _discovery_info(
    speaker_id: str, capabilities: list[str], household: str = HOUSEHOLD
) -> DiscoveryInfo:
    """Return what a speaker reports when it is discovered."""
    return cast(
        "DiscoveryInfo",
        {
            "householdId": household,
            "device": {"id": speaker_id, "modelDisplayName": "Five", "capabilities": capabilities},
        },
    )


def _speaker(speaker_id: str, name: str, capabilities: list[str], **kwargs: str) -> SonosPlayer:
    """Return another speaker of the provider, as far as the household listing reads it."""
    speaker, _, _ = _connected_player()
    speaker._protocol_id = speaker_id
    speaker._attr_name = name
    speaker._config = MagicMock()
    speaker.discovery_info = _discovery_info(speaker_id, capabilities, **kwargs)
    return speaker


def _kitchen(*others: SonosPlayer) -> tuple[SonosPlayer, MagicMock]:
    """
    Return Kitchen, a speaker with a line-in that leads its own group, with the given speakers.

    :param others: The other speakers of the provider.
    """
    kitchen, _, client = _connected_player()
    kitchen._protocol_id = "RINCON_KITCHEN0000001400"
    kitchen._attr_name = "Kitchen"
    kitchen._config = MagicMock()
    kitchen.discovery_info = _discovery_info(kitchen.protocol_id, ["PLAYBACK", "LINE_IN"])
    kitchen._attr_source_list = [PLAYER_SOURCE_MAP[SOURCE_LINE_IN]]
    cast("MagicMock", kitchen.provider).protocols = [kitchen, *others]
    client.player.is_coordinator = True
    client.player.group_members = [kitchen.protocol_id]
    client.player.group.load_line_in = AsyncMock()
    return kitchen, client


def _report(kitchen: SonosPlayer, client: MagicMock, container_type: str | None) -> None:
    """Let the group of Kitchen report what it plays."""
    group = client.player.group
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PLAYING
    group.position = 0.0
    group.container_type = container_type
    group.active_service = MusicService.SPOTIFY if container_type == "spotify.connect" else None
    group.playback_metadata = {}
    group.playback_actions = _actions({})
    kitchen.on_player_event(None)


def test_a_speaker_lists_the_line_in_of_the_other_speakers_of_its_household() -> None:
    """
    Kitchen lists the line-in of Den and Bathroom, in the order of their names.

    Not the speaker of another household, nor one without a line-in.
    """
    kitchen, _ = _kitchen(
        _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"]),
        _speaker("RINCON_OFFICE0000001400", "Office", ["PLAYBACK"]),
        _speaker("RINCON_CABIN00000001400", "Cabin", ["PLAYBACK", "LINE_IN"], household="other"),
        _speaker(BATHROOM, "Bathroom", ["PLAYBACK", "LINE_IN"]),
    )

    entries = [
        (x.id, x.name, x.is_input, x.passive, x.owner_player_id) for x in kitchen.source_list
    ]

    assert entries == [
        (SOURCE_LINE_IN, "Line-in", True, False, None),
        (f"{SOURCE_LINE_IN_OF}{BATHROOM}", "Line-in", True, False, BATHROOM),
        (LINE_IN_OF_DEN, "Line-in", True, False, DEN),
    ]


def test_a_speaker_without_a_line_in_lists_the_one_of_another_speaker() -> None:
    """A speaker without a line-in of its own still offers the line-in of Den."""
    kitchen, _ = _kitchen(_speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"]))
    kitchen._attr_source_list = []

    assert [x.id for x in kitchen.source_list] == [LINE_IN_OF_DEN]


@pytest.mark.parametrize(
    ("source", "device_id"),
    [(LINE_IN_OF_DEN, DEN), (SOURCE_LINE_IN, None)],
    ids=["other_speaker", "own"],
)
async def test_selecting_a_line_in_loads_it_on_the_group(
    source: str, device_id: str | None
) -> None:
    """The line-in of Den is loaded from Den on the group of Kitchen, its own from Kitchen."""
    kitchen, client = _kitchen(_speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"]))

    await kitchen.select_source(source)

    client.player.group.load_line_in.assert_awaited_once_with(
        device_id=device_id, play_on_completion=True
    )


async def test_the_line_in_a_speaker_loaded_is_its_active_source() -> None:
    """Once the group plays a line-in after Kitchen loaded the one of Den, Den's is reported."""
    kitchen, client = _kitchen(_speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"]))

    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, client, ContainerType.LINEIN)

    assert kitchen._attr_active_source == LINE_IN_OF_DEN
    # the entry of the household listing stands for it, the speaker adds none of its own
    assert [x.passive for x in kitchen.source_list if x.id == LINE_IN_OF_DEN] == [False]


async def test_a_report_from_before_the_load_keeps_the_line_in_it_loaded() -> None:
    """What the group played before the line-in started does not make Kitchen forget it."""
    kitchen, client = _kitchen(_speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"]))
    _report(kitchen, client, "spotify.connect")

    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, client, "spotify.connect")
    _report(kitchen, client, ContainerType.LINEIN)

    assert kitchen._attr_active_source == LINE_IN_OF_DEN


async def test_a_line_in_reported_after_the_loaded_one_stopped_is_the_own() -> None:
    """A line-in started outside Music Assistant later is reported as Kitchen's own."""
    kitchen, client = _kitchen(_speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"]))
    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, client, ContainerType.LINEIN)

    _report(kitchen, client, "spotify.connect")
    _report(kitchen, client, ContainerType.LINEIN)

    assert kitchen._attr_active_source == SOURCE_LINE_IN
