"""
Tests for the line-in of the speakers of a Sonos household.

A speaker lists the line-in of every speaker of its household that has one, by one id on every
speaker, and loads it on its group when it is selected. Sonos does not report whose line-in
plays, so a speaker reports the line-in it loaded itself, and a group member the one its
coordinator loaded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.api.models import ContainerType, MusicService
from aiosonos.api.models import PlayBackState as SonosPlayBackState

from music_assistant.providers.sonos.const import SOURCE_LINE_IN_OF
from tests.providers.sonos.test_player import _actions, _connected_player

if TYPE_CHECKING:
    from aiosonos.api.models import DiscoveryInfo

    from music_assistant.providers.sonos.player import SonosPlayer

HOUSEHOLD = "Sonos_household"
KITCHEN = "RINCON_KITCHEN0000001400"
DEN = "RINCON_DEN000000001400"
BATHROOM = "RINCON_BATHROOM00001400"
LINE_IN_OF_KITCHEN = f"{SOURCE_LINE_IN_OF}{KITCHEN}"
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


def _speaker(
    speaker_id: str, name: str, capabilities: list[str], household: str = HOUSEHOLD
) -> tuple[SonosPlayer, MagicMock]:
    """Return a connected speaker that leads its own group, and its client."""
    speaker, _, client = _connected_player()
    speaker._protocol_id = speaker_id
    speaker._attr_name = name
    speaker._config = MagicMock()
    speaker.discovery_info = _discovery_info(speaker_id, capabilities, household)
    client.player.is_coordinator = True
    client.player.group_members = [speaker_id]
    client.player.group.load_line_in = AsyncMock()
    return speaker, client


def _household(*speakers: SonosPlayer) -> None:
    """Let the given speakers be the speakers of the provider, and find each other."""
    by_id = {x.protocol_id: x for x in speakers}
    for speaker in speakers:
        provider = cast("MagicMock", speaker.provider)
        provider.protocols = list(speakers)
        provider.get_protocol = by_id.get


def _kitchen_and_den() -> tuple[SonosPlayer, MagicMock, SonosPlayer, MagicMock]:
    """Return Kitchen and Den, both with a line-in, and their clients."""
    kitchen, kitchen_client = _speaker(KITCHEN, "Kitchen", ["PLAYBACK", "LINE_IN"])
    den, den_client = _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"])
    _household(kitchen, den)
    return kitchen, kitchen_client, den, den_client


def _report(speaker: SonosPlayer, client: MagicMock, container_type: str | None) -> None:
    """Let the group a speaker leads report what it plays."""
    group = client.player.group
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PLAYING
    group.position = 0.0
    group.container_type = container_type
    group.active_service = MusicService.SPOTIFY if container_type == "spotify.connect" else None
    group.playback_metadata = {}
    group.playback_actions = _actions({})
    speaker.on_player_event(None)


def _join(member: SonosPlayer, member_client: MagicMock, coordinator: SonosPlayer) -> None:
    """Let a speaker follow the group of a coordinator, and report what it plays."""
    member_client.player.is_coordinator = False
    member_client.player.group.coordinator_id = coordinator.protocol_id
    member.on_player_event(None)


def test_a_speaker_lists_the_line_in_of_every_speaker_of_its_household() -> None:
    """
    Kitchen lists its own line-in first, then those of Bathroom and Den in name order.

    Not the speaker of another household, nor one without a line-in.
    """
    kitchen, _ = _speaker(KITCHEN, "Kitchen", ["PLAYBACK", "LINE_IN"])
    _household(
        kitchen,
        _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"])[0],
        _speaker("RINCON_OFFICE0000001400", "Office", ["PLAYBACK"])[0],
        _speaker("RINCON_CABIN00000001400", "Cabin", ["PLAYBACK", "LINE_IN"], "other")[0],
        _speaker(BATHROOM, "Bathroom", ["PLAYBACK", "LINE_IN"])[0],
    )

    entries = [
        (x.id, x.name, x.is_input, x.passive, x.owner_player_id) for x in kitchen.source_list
    ]

    assert entries == [
        (LINE_IN_OF_KITCHEN, "Line-in", True, False, None),
        (f"{SOURCE_LINE_IN_OF}{BATHROOM}", "Line-in", True, False, BATHROOM),
        (LINE_IN_OF_DEN, "Line-in", True, False, DEN),
    ]


def test_a_speaker_without_a_line_in_lists_the_one_of_another_speaker() -> None:
    """A speaker without a line-in of its own still offers the line-in of Den."""
    kitchen, _ = _speaker(KITCHEN, "Kitchen", ["PLAYBACK"])
    _household(kitchen, _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"])[0])

    assert [x.id for x in kitchen.source_list] == [LINE_IN_OF_DEN]


@pytest.mark.parametrize(
    ("source", "device_id"),
    [(LINE_IN_OF_DEN, DEN), (LINE_IN_OF_KITCHEN, None)],
    ids=["other_speaker", "own"],
)
async def test_selecting_a_line_in_loads_it_on_the_group(
    source: str, device_id: str | None
) -> None:
    """The line-in of Den is loaded from Den on the group of Kitchen, its own from Kitchen."""
    kitchen, kitchen_client, _, _ = _kitchen_and_den()

    await kitchen.select_source(source)

    kitchen_client.player.group.load_line_in.assert_awaited_once_with(
        device_id=device_id, play_on_completion=True
    )


async def test_the_line_in_a_speaker_loaded_is_its_active_source() -> None:
    """Once the group plays a line-in after Kitchen loaded the one of Den, Den's is reported."""
    kitchen, kitchen_client, _, _ = _kitchen_and_den()

    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, kitchen_client, ContainerType.LINEIN)

    assert kitchen._attr_active_source == LINE_IN_OF_DEN
    # the entry of the household listing stands for it, the speaker adds none of its own
    assert [x.passive for x in kitchen.source_list if x.id == LINE_IN_OF_DEN] == [False]


async def test_a_report_from_before_the_load_keeps_the_line_in_it_loaded() -> None:
    """What the group played before the line-in started does not make Kitchen forget it."""
    kitchen, kitchen_client, _, _ = _kitchen_and_den()
    _report(kitchen, kitchen_client, "spotify.connect")

    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, kitchen_client, "spotify.connect")
    _report(kitchen, kitchen_client, ContainerType.LINEIN)

    assert kitchen._attr_active_source == LINE_IN_OF_DEN


async def test_a_line_in_reported_after_the_loaded_one_stopped_is_the_own() -> None:
    """A line-in started outside Music Assistant later is reported as Kitchen's own."""
    kitchen, kitchen_client, _, _ = _kitchen_and_den()
    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, kitchen_client, ContainerType.LINEIN)

    _report(kitchen, kitchen_client, "spotify.connect")
    _report(kitchen, kitchen_client, ContainerType.LINEIN)

    assert kitchen._attr_active_source == LINE_IN_OF_KITCHEN


async def test_a_speaker_that_joins_the_group_playing_its_line_in_lists_it_as_its_own() -> None:
    """
    Den joins Kitchen's group while it plays Den's line-in, so both rooms hear it.

    Den reports Den's line-in, and has it in its own list, without Den after it.
    """
    kitchen, kitchen_client, den, den_client = _kitchen_and_den()
    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, kitchen_client, ContainerType.LINEIN)

    _join(den, den_client, kitchen)

    assert den._attr_active_source == kitchen._attr_active_source == LINE_IN_OF_DEN
    assert [(x.id, x.owner_player_id) for x in den.source_list if x.id == LINE_IN_OF_DEN] == [
        (LINE_IN_OF_DEN, None)
    ]


async def test_a_member_lists_the_line_in_of_its_coordinator_as_that_speaker_s() -> None:
    """Kitchen in the group of Den, which plays its own line-in, has it as Den's line-in."""
    kitchen, kitchen_client, den, den_client = _kitchen_and_den()
    _report(den, den_client, ContainerType.LINEIN)

    _join(kitchen, kitchen_client, den)

    assert kitchen._attr_active_source == den._attr_active_source == LINE_IN_OF_DEN
    assert [(x.id, x.owner_player_id) for x in kitchen.source_list if x.id == LINE_IN_OF_DEN] == [
        (LINE_IN_OF_DEN, DEN)
    ]
