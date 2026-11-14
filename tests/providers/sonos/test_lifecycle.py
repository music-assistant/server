"""Tests for a Sonos speaker attached to its player on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import SonosCapability
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import IdentifierType, MediaType, ProtocolRole
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_POWER_CONTROL
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.sonos.const import DEVICE_FEATURE_WAKEABLE
from music_assistant.providers.sonos.player import SonosPlayer
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from aiosonos.api.models import DiscoveryInfo

UID = "RINCON_F0F6C1CC8D0401400"


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


class _Subscriptions:
    """Stand-in for the subscribe method of a speaker's client, keeping what it returned."""

    def __init__(self) -> None:
        """Initialize without subscriptions."""
        self.results: list[MagicMock] = []

    def __call__(self, *_args: object) -> MagicMock:
        """Subscribe, returning the callback that unsubscribes."""
        self.results.append(MagicMock())
        return self.results[-1]


async def _attach_speaker(scenario: Scenario) -> SonosPlayer:
    """Attach a portable speaker that wakes on LAN, the way discovery sets it up."""
    provider: Any = await scenario.add_provider("sonos")
    # the UPnP of all speakers, which the Sonos provider sets up
    provider.upnp = MagicMock()
    discovery_info = cast(
        "DiscoveryInfo",
        {
            "device": {
                "id": UID,
                "name": "Speaker A",
                "modelDisplayName": "Roam",
                "capabilities": [SonosCapability.PLAYBACK],
                "deviceFeatures": [{"name": DEVICE_FEATURE_WAKEABLE}],
            }
        },
    )
    protocol = SonosPlayer(provider, UID, discovery_info=discovery_info)
    protocol.device_info.add_identifier(IdentifierType.IP_ADDRESS, "192.168.1.61")
    client = MagicMock()
    client.disconnect = AsyncMock()
    client.subscribe.side_effect = _Subscriptions()
    client.player.has_fixed_volume = False
    client.player.is_passive = False
    with (
        patch("music_assistant.providers.sonos.player.SonosLocalApiClient", return_value=client),
        patch.object(protocol, "_connect", AsyncMock()),
    ):
        await protocol.setup()
    await scenario.settle()
    return protocol


async def test_a_new_speaker_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A speaker first seen now gets a player of its own, identified by its MAC and its uid."""
    protocol = await _attach_speaker(scenario)

    player = protocol.player
    assert player is not None
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(UID) == ProtocolRole.NATIVE
    assert (IdentifierType.UUID, UID) in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, "F0:F6:C1:CC:8D:04") in protocol.identifiers


async def test_a_new_speaker_sleeps_as_powered_off_by_the_power_control_of_its_player(
    scenario: Scenario,
) -> None:
    """The power control the speaker follows is the setting of its player."""
    protocol = await _attach_speaker(scenario)
    assert protocol.player is not None
    assert protocol.player.player_id != UID
    assert protocol._sleeps_as_power_off

    await scenario.configure_player(
        protocol.player.player_id, **{CONF_POWER_CONTROL: PLAYER_CONTROL_NONE}
    )

    assert not protocol._sleeps_as_power_off


async def test_a_new_speaker_fetches_its_cloud_queue_under_its_own_id(
    scenario: Scenario,
) -> None:
    """The cloud queue a speaker is handed is served under the id of its protocol."""
    protocol = await _attach_speaker(scenario)
    assert protocol.player is not None
    group = cast("MagicMock", protocol.client.player.group)
    group.play_cloud_queue = AsyncMock()
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id=protocol.player.player_id,
        queue_item_id="item_a",
        queue_session_id="session_a",
    )

    await protocol.play_media(media)

    cloud_queue_url = group.play_cloud_queue.call_args.args[0]
    assert cloud_queue_url.endswith(f"/sonos_queue/{UID}/v2.3/")
    # the queue it plays is the queue of its player
    assert protocol.cloud_queue_id == protocol.player.player_id


async def test_a_speaker_listens_to_its_events_only_while_attached(scenario: Scenario) -> None:
    """The events of the speaker are subscribed at every attach and released at every detach."""
    protocol = await _attach_speaker(scenario)
    client = cast("MagicMock", protocol.client)
    unsubscribers = list(client.subscribe.side_effect.results)
    assert len(unsubscribers) == 4

    await scenario.mass.players.unregister(UID)
    await scenario.settle()

    for unsubscribe in unsubscribers:
        unsubscribe.assert_called_once_with()
    assert protocol._on_detach_callbacks == []


async def test_a_speaker_of_a_disabled_player_releases_its_connection(scenario: Scenario) -> None:
    """A speaker set up for a player the user disabled does not keep its connection."""
    airplay = await scenario.add_provider("airplay")
    audio = airplay.protocol(
        "apf0f6c1cc8d04",
        "Speaker A",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "F0:F6:C1:CC:8D:04"},
    )
    await scenario.register(audio)
    player_id = scenario.player_of(audio)
    assert player_id is not None
    await scenario.disable(player_id)

    protocol = await _attach_speaker(scenario)

    assert protocol.player is None
    cast("AsyncMock", protocol.client.disconnect).assert_awaited_once_with()


@pytest.mark.parametrize("asleep", [False, True])
async def test_a_speaker_taken_off_its_player_connects_again(
    scenario: Scenario, asleep: bool
) -> None:
    """A speaker moved to a player of its own connects again, unless it sleeps."""
    protocol = await _attach_speaker(scenario)
    if asleep:
        protocol._mark_asleep()
    audio = (await scenario.add_provider("airplay")).protocol(
        "apf0f6c1cc8d04",
        "Speaker A",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "F0:F6:C1:CC:8D:04"},
    )
    await scenario.register(audio)
    assert protocol.player is not None
    old_player_id = protocol.player.player_id
    assert scenario.player_of(audio) == old_player_id
    connect = AsyncMock()

    with patch.object(protocol, "_connect", connect):
        new_player_id = await scenario.mass.players.unlink_protocol(UID)
        await scenario.advance(2)

    assert protocol.player is not None
    assert protocol.player.player_id == new_player_id != old_player_id
    assert connect.await_count == (0 if asleep else 1)
