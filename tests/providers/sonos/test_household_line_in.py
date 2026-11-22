"""
Tests for the line-in of the speakers of a Sonos household.

A speaker lists its own line-in, and the line-in of another speaker of its household while
something is connected to it, by one id on every speaker, and loads it on its group when it is
selected. Sonos names the speaker whose line-in a group plays, so a group member reports the
line-in of its coordinator's group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import ContainerType, MusicService
from aiosonos.api.models import PlayBackState as SonosPlayBackState

from music_assistant.providers.sonos.const import (
    AUDIO_IN_SERVICE,
    RENDERING_CONTROL_SERVICE,
    SOURCE_LINE_IN,
    SOURCE_LINE_IN_OF,
)
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
    speaker_id: str,
    name: str,
    capabilities: list[str],
    household: str = HOUSEHOLD,
    line_in_connected: bool | None = True,
) -> tuple[SonosPlayer, MagicMock]:
    """Return a connected speaker that leads its own group, and its client."""
    speaker, _, client = _connected_player()
    speaker._protocol_id = speaker_id
    speaker._attr_name = name
    speaker._config = MagicMock()
    speaker.discovery_info = _discovery_info(speaker_id, capabilities, household)
    speaker._line_in_connected = line_in_connected
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
    """Return Kitchen and Den, both with something connected to their line-in, and clients."""
    kitchen, kitchen_client = _speaker(KITCHEN, "Kitchen", ["PLAYBACK", "LINE_IN"])
    den, den_client = _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"])
    _household(kitchen, den)
    return kitchen, kitchen_client, den, den_client


def _report(
    speaker: SonosPlayer,
    client: MagicMock,
    container_type: str | None,
    line_in_source: str | None = None,
) -> None:
    """Let the group a speaker leads report what it plays, the line-in by the speaker named."""
    group = client.player.group
    group.playback_state = SonosPlayBackState.PLAYBACK_STATE_PLAYING
    group.position = 0.0
    group.container_type = container_type
    group.active_service = MusicService.SPOTIFY if container_type == "spotify.connect" else None
    container: dict[str, Any] = {"type": container_type}
    if line_in_source:
        container["lineInSource"] = line_in_source
    group.playback_metadata = {"container": container} if container_type else {}
    group.playback_actions = _actions({})
    speaker.on_player_event(None)


def _join(member: SonosPlayer, member_client: MagicMock, coordinator: SonosPlayer) -> None:
    """Let a speaker follow the group of a coordinator, and report what it plays."""
    member_client.player.is_coordinator = False
    member_client.player.group.coordinator_id = coordinator.protocol_id
    member.on_player_event(None)


def test_a_speaker_lists_the_line_in_of_the_speakers_of_its_household() -> None:
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


@pytest.mark.parametrize(
    ("connected", "listed"),
    [(True, True), (False, False), (None, False)],
    ids=["connected", "nothing_connected", "not_reported_yet"],
)
def test_the_line_in_of_another_speaker_is_listed_while_something_is_connected(
    connected: bool | None, listed: bool
) -> None:
    """Kitchen lists Den's line-in once Den reported something connected; its own always."""
    kitchen, _ = _speaker(KITCHEN, "Kitchen", ["PLAYBACK", "LINE_IN"], line_in_connected=False)
    den, _ = _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"], line_in_connected=connected)
    _household(kitchen, den)

    expected = [LINE_IN_OF_KITCHEN, LINE_IN_OF_DEN] if listed else [LINE_IN_OF_KITCHEN]
    assert [x.id for x in kitchen.source_list] == expected


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


@pytest.mark.parametrize("started_by_ma", [True, False], ids=["by_ma", "outside_ma"])
@pytest.mark.parametrize("line_in_source", [DEN, KITCHEN], ids=["den", "own"])
async def test_the_line_in_sonos_names_is_the_active_source(
    started_by_ma: bool, line_in_source: str
) -> None:
    """Kitchen reports the line-in of the speaker Sonos names, whoever started it."""
    kitchen, kitchen_client, _, _ = _kitchen_and_den()
    line_in = f"{SOURCE_LINE_IN_OF}{line_in_source}"
    if started_by_ma:
        await kitchen.select_source(line_in)

    _report(kitchen, kitchen_client, ContainerType.LINEIN, line_in_source)

    assert kitchen._attr_active_source == line_in
    # the entry of the household listing stands for it, the speaker adds none of its own
    assert [x.passive for x in kitchen.source_list if x.id == line_in] == [False]


async def test_a_line_in_switched_outside_music_assistant_follows_what_sonos_names() -> None:
    """Kitchen loaded Den's line-in; the Sonos app then switches the group to Kitchen's own."""
    kitchen, kitchen_client, _, _ = _kitchen_and_den()
    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, kitchen_client, ContainerType.LINEIN, DEN)

    _report(kitchen, kitchen_client, ContainerType.LINEIN, KITCHEN)

    assert kitchen._attr_active_source == LINE_IN_OF_KITCHEN


def test_a_speaker_without_a_line_in_reports_the_line_in_sonos_names() -> None:
    """Bathroom, without a line-in, reports Den's line-in started from the app, not its own."""
    bathroom, bathroom_client = _speaker(BATHROOM, "Bathroom", ["PLAYBACK"])
    _household(bathroom, _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"])[0])

    _report(bathroom, bathroom_client, ContainerType.LINEIN, DEN)

    assert bathroom._attr_active_source == LINE_IN_OF_DEN
    assert [x.id for x in bathroom.source_list] == [LINE_IN_OF_DEN]


def test_a_line_in_sonos_does_not_name_is_the_line_in_of_no_speaker() -> None:
    """Without the speaker named, Bathroom reports a line-in of no speaker, not one of its own."""
    bathroom, bathroom_client = _speaker(BATHROOM, "Bathroom", ["PLAYBACK"])
    _household(bathroom)

    _report(bathroom, bathroom_client, ContainerType.LINEIN)

    assert bathroom._attr_active_source == SOURCE_LINE_IN


def test_a_line_in_with_nothing_connected_is_listed_while_it_plays() -> None:
    """Kitchen lists Den's line-in while it plays there, so its source resolves, then drops it."""
    kitchen, kitchen_client = _speaker(KITCHEN, "Kitchen", ["PLAYBACK"])
    _household(kitchen, _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"], line_in_connected=False)[0])

    _report(kitchen, kitchen_client, ContainerType.LINEIN, DEN)
    while_playing = [x.id for x in kitchen.source_list]
    _report(kitchen, kitchen_client, "spotify.connect")

    assert while_playing == [LINE_IN_OF_DEN]
    assert LINE_IN_OF_DEN not in [x.id for x in kitchen.source_list]


async def test_a_speaker_that_joins_the_group_playing_its_line_in_lists_it_as_its_own() -> None:
    """
    Den joins Kitchen's group while it plays Den's line-in, so both rooms hear it.

    Den reports Den's line-in, and has it in its own list, without Den after it.
    """
    kitchen, kitchen_client, den, den_client = _kitchen_and_den()
    await kitchen.select_source(LINE_IN_OF_DEN)
    _report(kitchen, kitchen_client, ContainerType.LINEIN, DEN)

    _join(den, den_client, kitchen)

    assert den._attr_active_source == kitchen._attr_active_source == LINE_IN_OF_DEN
    assert [(x.id, x.owner_player_id) for x in den.source_list if x.id == LINE_IN_OF_DEN] == [
        (LINE_IN_OF_DEN, None)
    ]


async def test_a_member_lists_the_line_in_of_its_coordinator_as_that_speaker_s() -> None:
    """Kitchen in the group of Den, which plays its own line-in, has it as Den's line-in."""
    kitchen, kitchen_client, den, den_client = _kitchen_and_den()
    _report(den, den_client, ContainerType.LINEIN, DEN)

    _join(kitchen, kitchen_client, den)

    assert kitchen._attr_active_source == den._attr_active_source == LINE_IN_OF_DEN
    assert [(x.id, x.owner_player_id) for x in kitchen.source_list if x.id == LINE_IN_OF_DEN] == [
        (LINE_IN_OF_DEN, DEN)
    ]


def test_a_member_without_a_line_in_follows_its_coordinator() -> None:
    """Bathroom, without a line-in, in the group of Kitchen that plays Den's line-in."""
    kitchen, kitchen_client, den, _ = _kitchen_and_den()
    bathroom, bathroom_client = _speaker(BATHROOM, "Bathroom", ["PLAYBACK"])
    _household(kitchen, den, bathroom)
    _report(kitchen, kitchen_client, ContainerType.LINEIN, DEN)

    _join(bathroom, bathroom_client, kitchen)

    assert bathroom._attr_active_source == LINE_IN_OF_DEN
    assert [(x.id, x.owner_player_id) for x in bathroom.source_list if x.id == LINE_IN_OF_DEN] == [
        (LINE_IN_OF_DEN, DEN)
    ]


def test_a_change_of_what_is_connected_updates_the_other_speakers() -> None:
    """
    Den's line-in events list it on Kitchen and drop it again; a repeated value changes nothing.

    Den itself and a speaker of another household are not updated for it.
    """
    kitchen, _ = _speaker(KITCHEN, "Kitchen", ["PLAYBACK"])
    den, _ = _speaker(DEN, "Den", ["PLAYBACK", "LINE_IN"], line_in_connected=None)
    cabin, _ = _speaker("RINCON_CABIN00000001400", "Cabin", ["PLAYBACK"], "other")
    _household(kitchen, den, cabin)
    kitchen_update = cast("MagicMock", kitchen.update_state)

    den._on_upnp_change({"AudioInputName": "Turntable", "LineInConnected": "1"})
    listed = ([x.id for x in kitchen.source_list], kitchen_update.call_count)
    den._on_upnp_change({"LineInConnected": "1"})
    repeated = kitchen_update.call_count
    den._on_upnp_change({"LineInConnected": "0"})

    assert listed == ([LINE_IN_OF_DEN], 1)
    assert repeated == 1
    assert kitchen.source_list == []
    assert kitchen_update.call_count == 2
    cast("MagicMock", den.update_state).assert_not_called()
    cast("MagicMock", cabin.update_state).assert_not_called()


@pytest.mark.parametrize(
    ("capabilities", "services"),
    [
        (["PLAYBACK", "LINE_IN"], (RENDERING_CONTROL_SERVICE, AUDIO_IN_SERVICE)),
        (["PLAYBACK"], (RENDERING_CONTROL_SERVICE,)),
    ],
    ids=["line_in", "no_line_in"],
)
async def test_the_line_in_events_are_followed_on_a_speaker_with_a_line_in(
    capabilities: list[str], services: tuple[str, ...]
) -> None:
    """The poll follows (renews) the AudioIn events only of a speaker that has a line-in."""
    speaker, _ = _speaker(KITCHEN, "Kitchen", capabilities)

    with patch.object(speaker, "_update_options", AsyncMock()):
        await speaker.poll()

    cast("AsyncMock", speaker.upnp.follow).assert_awaited_once_with(
        speaker._on_upnp_change, services
    )
