"""Tests for the channel layout of a Sonos player made of several speakers."""

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aiosonos.api.models import DiscoveryInfo
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.api.models import Player as PlayerData
from aiosonos.player import SonosPlayer as AioSonosPlayer
from music_assistant_models.enums import ChannelLayout, PlayerType

from music_assistant.providers.sonos.helpers import get_channel_layout
from music_assistant.providers.sonos.player import SonosPlayer
from tests.providers.sonos.test_player import _connected_player

PRIMARY = "RINCON_38420B9A1C2E01400"


@pytest.mark.parametrize(
    ("front_channels", "paired", "surrounds", "subwoofer", "layout"),
    [
        (2, True, False, False, ChannelLayout.STEREO),
        (2, True, False, True, ChannelLayout.LAYOUT_2_1),
        (2, False, False, True, ChannelLayout.LAYOUT_2_1),
        (1, False, False, True, ChannelLayout.LAYOUT_1_1),
        (3, False, False, True, ChannelLayout.LAYOUT_3_1),
        (2, False, True, False, ChannelLayout.LAYOUT_4_0),
        (2, False, True, True, ChannelLayout.LAYOUT_4_1),
        (3, False, True, False, ChannelLayout.LAYOUT_5_0),
        (3, False, True, True, ChannelLayout.LAYOUT_5_1),
        (1, False, True, False, ChannelLayout.UNKNOWN),
        (1, False, False, False, None),
        (2, False, False, False, None),
        (3, False, False, False, None),
    ],
)
def test_channel_layout(
    front_channels: int,
    paired: bool,
    surrounds: bool,
    subwoofer: bool,
    layout: ChannelLayout | None,
) -> None:
    """Each set of speakers maps to its layout; a front device alone is no set."""
    assert get_channel_layout(front_channels, paired, surrounds, subwoofer) == layout


def _member(player_id: str, *channels: str) -> dict[str, Any]:
    """Return a member of a zone as the speaker reports it."""
    return {
        "_objectType": "activeZoneMember",
        "channelMap": list(channels),
        "id": player_id,
        "state": {"_objectType": "zoneMemberState", "disconnected": False},
    }


def _player_data(*members: dict[str, Any]) -> PlayerData:
    """Return the player data a groups event carries for the primary of a zone."""
    return cast(
        "PlayerData",
        {
            "_objectType": "player",
            "id": PRIMARY,
            "name": "Living Room",
            "zoneInfo": {"_objectType": "activeZone", "members": list(members), "name": "Room"},
        },
    )


def _report_zone(client: MagicMock, *members: dict[str, Any]) -> None:
    """Let the speaker report the given zone, read through aiosonos as it arrives in an event."""
    zone = AioSonosPlayer(MagicMock(groups=[]), _player_data(*members))
    client.player.has_subwoofer = zone.has_subwoofer
    client.player.has_surrounds = zone.has_surrounds
    client.player.is_stereo_pair = zone.is_stereo_pair
    client.player.bonded_player_ids = zone.bonded_player_ids


def _zone(*channels: list[str]) -> list[dict[str, Any]]:
    """
    Return the members of a zone, the primary first.

    :param channels: The channel map of each member.
    """
    return [
        _member(PRIMARY if index == 0 else f"RINCON_5CAAFD0C1D2E0{index}400", *chans)
        for index, chans in enumerate(channels)
    ]


def _speaker(
    model: str, capabilities: list[str], *members: dict[str, Any]
) -> tuple[SonosPlayer, MagicMock]:
    """
    Create a connected speaker that coordinates its own group and reports the given zone.

    :param model: The model of the primary device.
    :param capabilities: The capabilities of the primary device.
    :param members: The members of the zone.
    """
    player, _, client = _connected_player()
    player.discovery_info = cast(
        "DiscoveryInfo",
        {"device": {"id": PRIMARY, "modelDisplayName": model, "capabilities": capabilities}},
    )
    client.player.is_coordinator = True
    client.player.group_members = [PRIMARY]
    client.player.group.playback_state = SonosPlayBackState.PLAYBACK_STATE_IDLE
    client.player.group.playback_metadata = {}
    _report_zone(client, *members)
    return player, client


SOUNDBAR = ("Arc", ["PLAYBACK", "HT_PLAYBACK", "LINE_IN", "AIRPLAY"])
AMP = ("Amp", ["PLAYBACK", "HT_PLAYBACK", "LINE_IN", "AIRPLAY"])
SPEAKER = ("Era 100", ["PLAYBACK", "AIRPLAY", "VOICE"])


@pytest.mark.parametrize(
    ("device", "channels", "layout"),
    [
        (SPEAKER, [["LF", "LF"], ["RF", "RF"]], ChannelLayout.STEREO),
        (SPEAKER, [["LF"], ["RF"]], ChannelLayout.STEREO),
        (SPEAKER, [["LF", "LF"], ["RF", "RF"], ["SW"]], ChannelLayout.LAYOUT_2_1),
        (SPEAKER, [["LF"], ["RF"], ["SW"]], ChannelLayout.LAYOUT_2_1),
        (SPEAKER, [["LF", "RF"], ["SW"]], ChannelLayout.LAYOUT_1_1),
        (SOUNDBAR, [["LF", "RF"], ["SW"]], ChannelLayout.LAYOUT_3_1),
        (SOUNDBAR, [["LF", "RF"], ["LR"], ["RR"]], ChannelLayout.LAYOUT_5_0),
        (SOUNDBAR, [["LF", "RF"], ["SW"], ["LR"], ["RR"]], ChannelLayout.LAYOUT_5_1),
        (AMP, [["LF", "RF"], ["SW"]], ChannelLayout.LAYOUT_2_1),
        (AMP, [["LF", "RF"], ["LR"], ["RR"]], ChannelLayout.LAYOUT_4_0),
        (AMP, [["LF", "RF"], ["LR"], ["RR"], ["SW"]], ChannelLayout.LAYOUT_4_1),
    ],
)
def test_a_set_reports_its_layout(
    device: tuple[str, list[str]], channels: list[list[str]], layout: ChannelLayout
) -> None:
    """A stereo pair, or a front with a subwoofer or surrounds, is a multi-channel player."""
    player, _ = _speaker(*device, *_zone(*channels))

    player.update_attributes()

    assert player.type == PlayerType.MULTI_CHANNEL
    assert player.channel_layout == layout


@pytest.mark.parametrize("device", [SOUNDBAR, AMP, SPEAKER])
@pytest.mark.parametrize("channels", [["LF", "RF"], []], ids=["both-fronts", "no-channel-map"])
def test_a_device_alone_is_a_plain_player(
    device: tuple[str, list[str]], channels: list[str]
) -> None:
    """A soundbar, an Amp or a speaker without bonded speakers is no set."""
    player, _ = _speaker(*device, *_zone(channels))

    player.update_attributes()

    assert player.type == PlayerType.PLAYER
    assert player.channel_layout is None


def test_the_layout_follows_a_change_made_in_the_sonos_app() -> None:
    """Satellites added or removed in the Sonos app change the player on the next event."""
    player, client = _speaker(*SOUNDBAR, _member(PRIMARY, "LF", "RF"))
    player.on_player_event(None)
    assert (player.type, player.channel_layout) == (PlayerType.PLAYER, None)

    _report_zone(
        client,
        _member(PRIMARY, "LF", "RF"),
        _member("RINCON_5CAAFD0C1D2E01400", "SW"),
        _member("RINCON_5CAAFD0C1D2E02400", "LR"),
        _member("RINCON_5CAAFD0C1D2E03400", "RR"),
    )
    player.on_player_event(None)
    assert (player.type, player.channel_layout) == (
        PlayerType.MULTI_CHANNEL,
        ChannelLayout.LAYOUT_5_1,
    )

    _report_zone(client, _member(PRIMARY, "LF", "RF"))
    player.on_player_event(None)
    assert (player.type, player.channel_layout) == (PlayerType.PLAYER, None)


def test_a_pair_made_in_the_sonos_app_becomes_a_multi_channel_player() -> None:
    """Two speakers bonded into a pair in the Sonos app change the player, and back."""
    player, client = _speaker(*SPEAKER, *_zone(["LF", "RF"]))
    player.on_player_event(None)
    assert (player.type, player.channel_layout) == (PlayerType.PLAYER, None)

    _report_zone(client, *_zone(["LF", "LF"], ["RF", "RF"]))
    player.on_player_event(None)
    assert (player.type, player.channel_layout) == (
        PlayerType.MULTI_CHANNEL,
        ChannelLayout.STEREO,
    )

    _report_zone(client, *_zone(["LF", "RF"]))
    player.on_player_event(None)
    assert (player.type, player.channel_layout) == (PlayerType.PLAYER, None)
