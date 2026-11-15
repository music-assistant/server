"""Tests for the name a player is shown by."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_PLAYERS

# the real server with fake providers, for the name of a player that is away
from tests.scenarios.conftest import scenario  # noqa: F401

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


async def _shown_names(
    scenario: Scenario,  # noqa: F811
    player_id: str,
) -> tuple[str, str | None]:
    """Return the name of a player in the list of players and in the list of player configs."""
    [listed] = [player for player in scenario.visible_players() if player.player_id == player_id]
    [config] = [
        config for config in await scenario.player_configs() if config.player_id == player_id
    ]
    return listed.name, config.default_name


async def test_player_whose_device_reports_a_technical_name_keeps_it_while_away(
    scenario: Scenario,  # noqa: F811
) -> None:
    """A name that is no user-friendly one is still shown while the device is away."""
    sendspin = await scenario.add_provider("sendspin")
    device = sendspin.protocol(
        "54:78:c9:e6:0d:a0", "sendspin_kitchen", role="audio", identifiers=KITCHEN
    )
    await scenario.register(device)
    player_id = scenario.single_player(device).player_id
    assert await _shown_names(scenario, player_id) == ("sendspin_kitchen", "sendspin_kitchen")

    await scenario.unregister(device)

    assert await _shown_names(scenario, player_id) == ("sendspin_kitchen", "sendspin_kitchen")

    scenario.set_present(device.protocol_id, False)
    await scenario.restart()

    assert await _shown_names(scenario, player_id) == ("sendspin_kitchen", "sendspin_kitchen")


async def test_player_that_is_away_is_shown_by_the_name_of_its_own_protocol_first(
    scenario: Scenario,  # noqa: F811
) -> None:
    """Of the names its protocols were last seen with, that of its own protocol wins."""
    player_id = "pl0123456789ab"

    def _protocol(protocol_id: str, provider: str, role: str, name: str) -> dict[str, object]:
        return {
            "protocol_id": protocol_id,
            "provider": provider,
            "player_id": player_id,
            "role": role,
            "enabled": True,
            "default_name": name,
            "values": {},
        }

    await scenario.seed_config(
        {
            CONF_PLAYERS: {
                player_id: {
                    "player_id": player_id,
                    "provider": "sonos--s1",
                    "player_type": "player",
                    "enabled": True,
                    "name": None,
                    "default_name": None,
                    "values": {},
                }
            },
            CONF_PLAYER_PROTOCOLS: {
                "ap_den": _protocol("ap_den", "airplay--a1", "audio", "ap_den"),
                "sonos_den": _protocol("sonos_den", "sonos--s1", "native", "Den"),
            },
        }
    )

    assert await _shown_names(scenario, player_id) == ("Den", "Den")
