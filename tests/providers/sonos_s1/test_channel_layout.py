"""Tests for the channel layout of a Sonos S1 player made of several speakers."""

import asyncio
import itertools
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import ChannelLayout, PlayerType
from soco import SoCo
from soco.zonegroupstate import ZoneGroupState

from music_assistant.providers.sonos_s1.player import SonosPlayer

HOUSEHOLD = "Sonos_channel_layout_test"
PRIMARY = "RINCON_000E58A0000101400"
SUB = "RINCON_000E58A0000201400"
LEFT_REAR = "RINCON_000E58A0000301400"
RIGHT_REAR = "RINCON_000E58A0000401400"

PLAYBAR = "Sonos Playbar"
AMP = "Sonos Amp"
SPEAKER = "Sonos Play:5"

# SoCo keeps one instance per address for the life of the process
_addresses = (f"10.99.{n // 250}.{n % 250 + 1}" for n in itertools.count())


@pytest.fixture
def zone_group_state() -> Iterator[ZoneGroupState]:
    """Give the household a zone group state that a topology subscription keeps current."""
    zgs = ZoneGroupState()
    subscription = MagicMock(sid="uuid:test", time_left=600)
    subscription.service.service_type = "ZoneGroupTopology"
    zgs.add_subscription(subscription)
    SoCo.zone_group_states[HOUSEHOLD] = zgs
    # reading the topology must not reach the speaker
    with patch("soco.services.Service.send_command", side_effect=AssertionError("request")):
        yield zgs
    del SoCo.zone_group_states[HOUSEHOLD]


def _member(uid: str, address: str, tag: str = "ZoneGroupMember", **attributes: str) -> str:
    """Return a member element of a zone group state payload."""
    extra = "".join(f' {key}="{value}"' for key, value in attributes.items())
    location = f"http://{address}:1400/xml/device_description.xml"
    return f'<{tag} UUID="{uid}" Location="{location}" ZoneName="Living"{extra}'


def _home_theater(primary: str, satellites: dict[str, str]) -> str:
    """
    Return the payload of a front with satellites, as the speaker reports it.

    :param primary: The address of the front.
    :param satellites: The channel of each satellite, by its uid.
    """
    channel_map = ";".join(
        [f"{PRIMARY}:LF,RF", *(f"{uid}:{channel}" for uid, channel in satellites.items())]
    )
    elements = "".join(
        _member(uid, next(_addresses), "Satellite", HTSatChanMapSet=channel_map, Invisible="1")
        + "/>"
        for uid in satellites
    )
    front = _member(PRIMARY, primary, HTSatChanMapSet=channel_map) + f">{elements}"
    return _payload(front + "</ZoneGroupMember>")


def _speaker_with_sub(primary: str) -> str:
    """Return the payload of a speaker with a subwoofer bonded to it."""
    channel_map = f"{PRIMARY}:LF,RF;{SUB}:SW,SW"
    front = _member(PRIMARY, primary, ChannelMapSet=channel_map) + "/>"
    sub = _member(SUB, next(_addresses), ChannelMapSet=channel_map, Invisible="1") + "/>"
    return _payload(front + sub)


def _alone(primary: str) -> str:
    """Return the payload of a speaker without bonded speakers."""
    return _payload(_member(PRIMARY, primary) + "/>")


def _payload(members: str) -> str:
    """Return a zone group state payload with one group of the given members."""
    group = f'<ZoneGroup Coordinator="{PRIMARY}" ID="{PRIMARY}:1">{members}</ZoneGroup>'
    return f"<ZoneGroupState><ZoneGroups>{group}</ZoneGroups></ZoneGroupState>"


def _player(zgs: ZoneGroupState, model: str, payload: str, address: str) -> SonosPlayer:
    """
    Create a player for the front of the given topology.

    :param zgs: The zone group state of the household.
    :param model: The model name the speaker reports.
    :param payload: The zone group state the speaker reports.
    :param address: The address of the front, as used in the payload.
    """
    soco = SoCo(address)
    soco._household_id = HOUSEHOLD
    soco.speaker_info = {"model_name": model}
    zgs.process_payload(payload, "event", address)
    provider = MagicMock()
    provider.topology_condition = asyncio.Condition()
    player = SonosPlayer(provider=provider, soco=soco, fixed_volume=False)
    provider.get_protocol = lambda protocol_id: player if protocol_id == PRIMARY else None
    return player


async def _topology_event(player: SonosPlayer) -> bool:
    """Hand the player the topology event of a change, return whether its state was signalled."""
    event = MagicMock(zone_player_uui_ds_in_group=PRIMARY)
    with patch.object(player, "update_state") as update_state:
        await player.create_update_groups_coro(event)
    return bool(update_state.called)


@pytest.mark.parametrize(
    ("model", "satellites", "layout"),
    [
        (PLAYBAR, {LEFT_REAR: "LR", RIGHT_REAR: "RR"}, ChannelLayout.LAYOUT_5_0),
        (PLAYBAR, {SUB: "SW", LEFT_REAR: "LR", RIGHT_REAR: "RR"}, ChannelLayout.UNKNOWN),
        (PLAYBAR, {SUB: "SW"}, ChannelLayout.UNKNOWN),
        (AMP, {SUB: "SW"}, ChannelLayout.UNKNOWN),
        (AMP, {LEFT_REAR: "LR", RIGHT_REAR: "RR"}, ChannelLayout.UNKNOWN),
    ],
)
async def test_a_front_with_satellites_is_a_multi_channel_player(
    zone_group_state: ZoneGroupState,
    model: str,
    satellites: dict[str, str],
    layout: ChannelLayout,
) -> None:
    """A soundbar or an Amp with satellites is a set, with the layout SoCo can tell."""
    address = next(_addresses)
    player = _player(zone_group_state, model, _home_theater(address, satellites), address)

    await _topology_event(player)

    assert player.type == PlayerType.MULTI_CHANNEL
    assert player.channel_layout == layout


async def test_a_speaker_with_a_subwoofer_is_a_multi_channel_player(
    zone_group_state: ZoneGroupState,
) -> None:
    """A speaker with a subwoofer is a set, whose layout SoCo cannot tell."""
    address = next(_addresses)
    player = _player(zone_group_state, SPEAKER, _speaker_with_sub(address), address)

    await _topology_event(player)

    assert player.type == PlayerType.MULTI_CHANNEL
    assert player.channel_layout == ChannelLayout.UNKNOWN


@pytest.mark.parametrize("model", [PLAYBAR, AMP, SPEAKER])
async def test_a_device_alone_is_a_plain_player(
    zone_group_state: ZoneGroupState, model: str
) -> None:
    """A soundbar, an Amp or a speaker without bonded speakers is no set."""
    address = next(_addresses)
    player = _player(zone_group_state, model, _alone(address), address)

    await _topology_event(player)

    assert player.type == PlayerType.PLAYER
    assert player.channel_layout is None


async def test_the_layout_follows_a_change_made_in_the_sonos_app(
    zone_group_state: ZoneGroupState,
) -> None:
    """Surrounds added or removed in the Sonos app change the player on the next event."""
    address = next(_addresses)
    player = _player(zone_group_state, PLAYBAR, _alone(address), address)
    await _topology_event(player)
    assert (player.type, player.channel_layout) == (PlayerType.PLAYER, None)
    assert not await _topology_event(player)

    zone_group_state.process_payload(
        _home_theater(address, {LEFT_REAR: "LR", RIGHT_REAR: "RR"}), "event", address
    )
    assert await _topology_event(player)
    assert (player.type, player.channel_layout) == (
        PlayerType.MULTI_CHANNEL,
        ChannelLayout.LAYOUT_5_0,
    )

    zone_group_state.process_payload(_alone(address), "event", address)
    assert await _topology_event(player)
    assert (player.type, player.channel_layout) == (PlayerType.PLAYER, None)


async def test_a_set_is_registered_as_a_multi_channel_player(
    zone_group_state: ZoneGroupState,
) -> None:
    """A speaker that is a set when it is set up joins the players as a multi-channel player."""
    address = next(_addresses)
    player = _player(
        zone_group_state,
        PLAYBAR,
        _home_theater(address, {LEFT_REAR: "LR", RIGHT_REAR: "RR"}),
        address,
    )
    registered: list[tuple[PlayerType, ChannelLayout | None]] = []

    async def _register(protocol: SonosPlayer) -> None:
        registered.append((protocol.type, protocol.channel_layout))

    with (
        patch.object(player.mass.players, "register", AsyncMock(side_effect=_register)),
        patch("soco.core.SoCo.volume", new_callable=PropertyMock, return_value=20),
        patch("soco.core.SoCo.mute", new_callable=PropertyMock, return_value=False),
        patch.object(player, "update_groups"),
        patch.object(player, "poll_media"),
        patch.object(player, "subscribe", AsyncMock()),
    ):
        await player.setup()

    assert registered == [(PlayerType.MULTI_CHANNEL, ChannelLayout.LAYOUT_5_0)]
