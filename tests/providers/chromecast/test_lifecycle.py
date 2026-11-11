"""Tests for the role, the discovery and the player of a Cast device."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest
from music_assistant_models.enums import IdentifierType, PlayerType, ProtocolRole
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_GROUP

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.chromecast.constants import CONF_DEFER_IDLE_VOLUME
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer
from music_assistant.providers.chromecast.provider import ChromecastProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

MAC_ADDRESS = "00:11:22:33:44:66"


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


def _cast_info(
    manufacturer: str = "Google Inc.",
    cast_type: str = CAST_TYPE_AUDIO,
    multichannel: bool = False,
) -> ChromecastInfo:
    """
    Return the info of a Cast device as discovery fills it out.

    :param manufacturer: The manufacturer the device reports.
    :param cast_type: The cast type of the device.
    :param multichannel: Whether a group is a stereo pair.
    """
    return ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name="Test Model",
        friendly_name="Speaker A",
        host="192.168.1.71",
        port=8009,
        cast_type=cast_type,
        manufacturer=manufacturer,
        mac_address=MAC_ADDRESS,
        is_dynamic_group=False,
        is_multichannel_group=multichannel,
    )


def _cast_player(provider: Any, cast_info: ChromecastInfo) -> ChromecastPlayer:
    """Construct the protocol of a Cast device without connecting to it."""
    with patch("music_assistant.providers.chromecast.player.CastStatusListener"):
        return ChromecastPlayer(provider, str(cast_info.uuid), cast_info, MagicMock())


@pytest.mark.parametrize(
    ("cast_info", "role", "player_type"),
    [
        (_cast_info(), ProtocolRole.NATIVE, PlayerType.PLAYER),
        (_cast_info(manufacturer="Sonos"), ProtocolRole.AUDIO, PlayerType.PLAYER),
        (_cast_info(cast_type=CAST_TYPE_GROUP), ProtocolRole.NATIVE, PlayerType.GROUP),
        (
            _cast_info(cast_type=CAST_TYPE_GROUP, multichannel=True),
            ProtocolRole.NATIVE,
            PlayerType.STEREO_PAIR,
        ),
        # a group of speakers of another brand is still a Cast group
        (
            _cast_info(manufacturer="Sonos", cast_type=CAST_TYPE_GROUP),
            ProtocolRole.NATIVE,
            PlayerType.GROUP,
        ),
    ],
    ids=["google-speaker", "other-brand", "cast-group", "stereo-pair", "other-brand-group"],
)
def test_a_cast_device_reports_its_role(
    cast_info: ChromecastInfo, role: ProtocolRole, player_type: PlayerType
) -> None:
    """Cast is native for a Google device and a Cast group, an audio output of any other."""
    protocol = _cast_player(MagicMock(), cast_info)

    assert protocol.role == role
    assert protocol.type == player_type


def _discovering_provider(attached: set[UUID], known: list[UUID]) -> Any:
    """
    Return a provider whose discovery is running and knows the given devices.

    :param attached: The devices that have an attached protocol.
    :param known: The devices the discovery found.
    """
    provider: Any = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider.mass.loop.run_in_executor = AsyncMock(side_effect=lambda _executor, func: func())
    provider.browser = MagicMock(devices=dict.fromkeys(known))
    provider._discovery_running = True
    provider.get_protocol = MagicMock(
        side_effect=lambda protocol_id: MagicMock() if UUID(protocol_id) in attached else None
    )
    provider._on_chromecast_discovered = MagicMock()
    return provider


async def test_discovery_offers_the_known_devices_without_an_attached_protocol() -> None:
    """A removed or re-enabled device is offered from what the running discovery knows."""
    attached, removed = uuid4(), uuid4()
    provider = _discovering_provider(attached={attached}, known=[attached, removed])

    await provider.discover_players()

    provider._on_chromecast_discovered.assert_called_once_with(removed, "")
    provider.browser.start_discovery.assert_not_called()


async def test_the_first_discovery_starts_the_browser() -> None:
    """The first discovery starts the browser, which reports every device it finds."""
    provider = _discovering_provider(attached=set(), known=[uuid4()])
    provider._discovery_running = False

    await provider.discover_players()

    provider.browser.start_discovery.assert_called_once_with()
    provider._on_chromecast_discovered.assert_not_called()


async def test_an_endpoint_found_to_be_passive_is_removed() -> None:
    """A passive multichannel endpoint is removed with its record and its bridge client's."""
    removals = MagicMock(remove_bridge=AsyncMock(), remove_protocol=AsyncMock())
    provider: Any = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider.mass.players.remove_protocol = removals.remove_protocol
    provider.bridge_manager = MagicMock(remove_bridge=removals.remove_bridge)
    provider.logger = MagicMock()
    provider._pending_discoveries = set()
    castplayer = MagicMock(spec=ChromecastPlayer)
    castplayer.cast_info = MagicMock()
    provider.get_protocol = MagicMock(return_value=castplayer)
    protocol_id = str(uuid4())

    with patch("music_assistant.providers.chromecast.provider.ChromecastInfo") as info_class:
        info_class.from_cast_info.return_value.is_multichannel_child = True
        await provider._recheck_multichannel_child(protocol_id, MagicMock())

    # the bridge client goes first: once the protocol is gone, its record would keep the player
    assert removals.mock_calls == [
        call.remove_bridge(protocol_id, permanent=True),
        call.remove_protocol(protocol_id),
    ]


async def _attach_cast(scenario: Scenario, cast_info: ChromecastInfo) -> ChromecastPlayer:
    """Attach the protocol of a Cast device the way discovery sets it up."""
    provider: Any = scenario.mass.get_provider("chromecast") or await scenario.add_provider(
        "chromecast"
    )
    provider.mz_mgr = MagicMock()
    protocol = _cast_player(provider, cast_info)
    await scenario.mass.players.attach(protocol)
    await scenario.settle()
    return protocol


async def test_a_new_google_device_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A Google speaker first seen now gets a player of its own, identified by its Cast UUID."""
    cast_info = _cast_info()
    protocol = await _attach_cast(scenario, cast_info)

    player = protocol.player
    assert player is not None
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(protocol.protocol_id) == ProtocolRole.NATIVE
    assert (IdentifierType.CAST_UUID, str(cast_info.uuid)) in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, MAC_ADDRESS) in protocol.identifiers


async def test_the_cast_of_a_device_of_another_brand_joins_its_player(
    scenario: Scenario,
) -> None:
    """The Cast output of a speaker of another brand joins the player of its native protocol."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol(
        "RINCON_001122334466", "Speaker A", identifiers={IdentifierType.MAC_ADDRESS: MAC_ADDRESS}
    )
    await scenario.attach(speaker)

    protocol = await _attach_cast(scenario, _cast_info(manufacturer="Sonos"))

    assert protocol.player is not None
    assert protocol.player.player_id == scenario.player_of(speaker)
    assert protocol.player.get_role(protocol.protocol_id) == ProtocolRole.AUDIO


async def test_a_new_device_reads_its_settings_from_its_own_record(scenario: Scenario) -> None:
    """A setting of the Cast device is read from its protocol record, not the player's."""
    protocol = await _attach_cast(scenario, _cast_info())
    assert protocol.player is not None
    assert protocol.player.player_id != protocol.protocol_id
    cast_device = cast("MagicMock", protocol.cc)
    cast_device.app_id = None

    await scenario.mass.config.save_protocol_config(
        protocol.protocol_id, {CONF_DEFER_IDLE_VOLUME: True}
    )
    await scenario.settle()
    await protocol.volume_set(30)

    # an idle receiver keeps the volume back until playback starts
    cast_device.set_volume.assert_not_called()
    assert protocol._pending_volume == 30
