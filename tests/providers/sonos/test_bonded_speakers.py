"""Tests for a Sonos speaker that reports the other speakers of its set, whose players go."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import PlayBackState as SonosPlayBackState
from aiosonos.api.models import SonosCapability
from music_assistant_models.enums import IdentifierType, PlayerType

from music_assistant.providers.sonos.player import SonosPlayer
from tests.providers.sonos.test_channel_layout import (
    PRIMARY,
    SOUNDBAR,
    SPEAKER,
    _report_zone,
    _speaker,
    _zone,
)
from tests.providers.sonos.test_lifecycle import _Subscriptions
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from aiosonos.api.models import DiscoveryInfo

    from tests.scenarios.harness import FakePlayerProvider

# the ids _zone() gives the second, third and fourth speaker of a zone
SECOND = "RINCON_5CAAFD0C1D2E01400"
THIRD = "RINCON_5CAAFD0C1D2E02400"
FOURTH = "RINCON_5CAAFD0C1D2E03400"

ALONE = (["LF", "RF"],)
PAIR = (["LF", "LF"], ["RF", "RF"])
HOME_THEATER = (["LF", "RF"], ["SW"], ["LR"], ["RR"])


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


def _attached(device: tuple[str, list[str]], *channels: list[str]) -> tuple[SonosPlayer, MagicMock]:
    """
    Create a connected primary, attached to its player, whose speaker reports the given zone.

    :param device: The model and capabilities of the primary.
    :param channels: The channel map of each speaker of the zone, the primary first.
    """
    player, client = _speaker(*device, *_zone(*channels))
    player._protocol_id = PRIMARY
    player.set_player(MagicMock())
    return player, client


def _reports(player: SonosPlayer) -> list[tuple[str, list[str]]]:
    """Return the sets the player reported to the players controller, in order."""
    remove = cast("MagicMock", player.mass.players.remove_absorbed_players)
    return [cast("tuple[str, list[str]]", call.args) for call in remove.call_args_list]


async def test_a_pair_reports_its_second_speaker_once_it_is_attached() -> None:
    """At setup the pair reports its second speaker, only once its protocol is attached."""
    player, _ = _speaker(*SPEAKER, *_zone(*PAIR))
    player._protocol_id = PRIMARY

    # setup reads the speaker before it registers the protocol
    player.update_attributes()
    assert _reports(player) == []

    player.set_player(MagicMock())
    await player.on_attach(MagicMock())

    assert _reports(player) == [(PRIMARY, [SECOND])]
    remove = cast("MagicMock", player.mass.players.remove_absorbed_players)
    cast("MagicMock", player.mass.create_task).assert_called_once_with(remove.return_value)


async def test_a_home_theater_set_reports_its_sub_and_surrounds() -> None:
    """A soundbar reports its subwoofer and both surrounds."""
    player, _ = _attached(SOUNDBAR, *HOME_THEATER)

    await player.on_attach(MagicMock())

    assert _reports(player) == [(PRIMARY, [SECOND, THIRD, FOURTH])]


@pytest.mark.parametrize("device", [SOUNDBAR, SPEAKER])
async def test_a_speaker_alone_reports_nothing(device: tuple[str, list[str]]) -> None:
    """A speaker without bonded speakers makes no call."""
    player, _ = _attached(device, *ALONE)

    await player.on_attach(MagicMock())
    player.on_player_event(None)

    assert _reports(player) == []


def test_two_speakers_bonded_while_the_server_runs_report_the_new_speaker() -> None:
    """A pair made in the Sonos app reports the speaker that joined, once."""
    player, client = _attached(SPEAKER, *ALONE)
    player.on_player_event(None)
    assert _reports(player) == []

    _report_zone(client, *_zone(*PAIR))
    player.on_player_event(None)

    assert _reports(player) == [(PRIMARY, [SECOND])]


def test_an_unchanged_zone_is_not_reported_again() -> None:
    """Later events of the same set report nothing; a speaker that joins is reported alone."""
    player, client = _attached(SOUNDBAR, ["LF", "RF"], ["SW"])
    player.on_player_event(None)
    player.on_player_event(None)
    assert _reports(player) == [(PRIMARY, [SECOND])]

    _report_zone(client, *_zone(*HOME_THEATER))
    player.on_player_event(None)
    player.on_player_event(None)

    assert _reports(player) == [(PRIMARY, [SECOND]), (PRIMARY, [THIRD, FOURTH])]


def test_a_speaker_that_rejoins_its_set_is_reported_again() -> None:
    """A pair taken apart and made again reports the second speaker again."""
    player, client = _attached(SPEAKER, *PAIR)
    player.on_player_event(None)

    _report_zone(client, *_zone(*ALONE))
    player.on_player_event(None)
    _report_zone(client, *_zone(*PAIR))
    player.on_player_event(None)

    assert _reports(player) == [(PRIMARY, [SECOND]), (PRIMARY, [SECOND])]


async def test_a_speaker_that_is_not_connected_reports_nothing() -> None:
    """Without a live connection the zone the speaker reported last is not reported."""
    player, _ = _attached(SPEAKER, *PAIR)
    player.connected = False

    await player.on_attach(MagicMock())
    player.on_player_event(None)

    assert _reports(player) == []


async def _set_up(provider: FakePlayerProvider, protocol_id: str, address: str) -> SonosPlayer:
    """
    Set up a speaker the way discovery does, without a connection to a device.

    :param provider: The (fake) Sonos provider.
    :param protocol_id: The id of the speaker.
    :param address: The IP address of the speaker.
    """
    # the UPnP of all speakers, which the Sonos provider sets up
    cast("Any", provider).upnp = MagicMock()
    discovery_info = cast(
        "DiscoveryInfo",
        {
            "device": {
                "id": protocol_id,
                "name": protocol_id,
                "modelDisplayName": "Era 100",
                "capabilities": [SonosCapability.PLAYBACK],
            }
        },
    )
    protocol = SonosPlayer(cast("Any", provider), protocol_id, discovery_info=discovery_info)
    protocol.device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
    client = MagicMock()
    client.disconnect = AsyncMock()
    client.subscribe.side_effect = _Subscriptions()
    client.player.has_fixed_volume = False
    client.player.volume_level = 20
    client.player.volume_muted = False
    client.player.is_coordinator = True
    client.player.group_members = [protocol_id]
    client.player.group.playback_state = SonosPlayBackState.PLAYBACK_STATE_IDLE
    client.player.group.playback_metadata = {}
    client.player.group.position = 0.0
    client.player.group.container_type = None
    client.player.group.active_service = None
    _report_zone(client, *_zone(*ALONE))
    with (
        patch("music_assistant.providers.sonos.player.SonosLocalApiClient", return_value=client),
        patch.object(protocol, "_connect", AsyncMock()),
    ):
        await protocol.setup()
    # no request reaches a device: a poll would read its options
    protocol.upnp = MagicMock(follow=AsyncMock(), unfollow=AsyncMock(), follows=False)
    protocol._attr_needs_poll = False
    protocol.connected = True
    return protocol


async def test_a_pair_bonded_while_the_server_runs_removes_the_player_of_its_second_speaker(
    scenario: Scenario,
) -> None:
    """The second speaker's player goes for good, and its connection is released."""
    provider = await scenario.add_provider("sonos")
    second = await _set_up(provider, SECOND, "192.168.1.62")
    primary = await _set_up(provider, PRIMARY, "192.168.1.61")
    await scenario.settle()
    second_player = second.player
    assert second_player is not None
    assert primary.player is not None
    second_player_id = second_player.player_id

    _report_zone(primary.client, *_zone(*PAIR))  # type: ignore[arg-type]
    primary.on_player_event(None)
    await scenario.settle()

    assert scenario.mass.players.get_player(second_player_id) is None
    assert scenario.mass.config.get_protocol_record(SECOND) is None
    assert second.player is None
    assert not second.connected
    cast("AsyncMock", second.client.disconnect).assert_awaited_once_with()
    assert primary.player is not None
    assert primary.player.type == PlayerType.MULTI_CHANNEL
    # nothing connects to the second speaker again
    with patch.object(second, "_connect", AsyncMock()) as connect:
        await scenario.advance(60)
    connect.assert_not_awaited()
