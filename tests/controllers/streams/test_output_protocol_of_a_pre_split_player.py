"""Tests for the protocol the streams take their settings from, for a player from before the split."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import IdentifierType

from music_assistant.constants import (
    CONF_ENTRY_REQUIRES_FIXED_CONTENT_LENGTH,
    CONF_FLOW_MODE_SAMPLE_RATE,
    CONF_REQUIRES_FIXED_CONTENT_LENGTH,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
)

# the real server with fake providers, for a player whose id is also that of its protocol
from tests.scenarios.conftest import scenario  # noqa: F401
from tests.scenarios.harness import FakeProtocol

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from tests.scenarios.harness import Scenario


@pytest.fixture(autouse=True)
def dlna_declares_fixed_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the fake DLNA protocols declare the fixed content length setting, as DLNA does."""
    entries = FakeProtocol.get_config_entries

    async def _entries(protocol: FakeProtocol) -> list[ConfigEntry]:
        own = await entries(protocol)
        if protocol.provider.domain != "dlna":
            return own
        return [*own, CONF_ENTRY_REQUIRES_FIXED_CONTENT_LENGTH]

    monkeypatch.setattr(FakeProtocol, "get_config_entries", _entries)


# a player from before the split keeps the id of its native protocol
PLAYER_ID = "RINCON_KITCHEN"
KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


async def _playing_through_dlna(
    scenario: Scenario,  # noqa: F811
) -> None:
    """Boot on a pre-split player with a DLNA protocol, and play to it through DLNA."""
    sonos = await scenario.add_provider("sonos")
    dlna = await scenario.add_provider("dlna")
    sonos.protocol(PLAYER_ID, "Kitchen", identifiers=KITCHEN)
    dlna.protocol("dlna_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    scenario.set_present(PLAYER_ID, True)
    scenario.set_present("dlna_kitchen", True)
    await scenario.seed_pre_split_config(
        {
            PLAYER_ID: {
                "player_id": PLAYER_ID,
                "provider": sonos.instance_id,
                "player_type": "player",
                "enabled": True,
                "default_name": "Kitchen",
                "values": {"linked_protocol_ids": ["dlna_kitchen"]},
            },
            "dlna_kitchen": {
                "player_id": "dlna_kitchen",
                "provider": dlna.instance_id,
                "player_type": "protocol",
                "enabled": True,
                "default_name": "Kitchen",
                "values": {
                    "protocol_parent_id": PLAYER_ID,
                    CONF_REQUIRES_FIXED_CONTENT_LENGTH: True,
                    CONF_FLOW_MODE_SAMPLE_RATE: FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
                },
            },
        }
    )
    await scenario.idle()
    await scenario.configure_player(PLAYER_ID, preferred_output_protocol="dlna_kitchen")
    await scenario.play(PLAYER_ID)
    player = scenario.mass.players.get_player(PLAYER_ID)
    assert player is not None
    assert player.active_output_protocol == "dlna_kitchen"


async def test_announcement_is_served_with_the_content_length_of_the_output_that_renders(
    scenario: Scenario,  # noqa: F811
) -> None:
    """An announcement for the player is served as its rendering protocol expects."""
    await _playing_through_dlna(scenario)

    fixed_content_length = scenario.mass.streams._announcement_requires_fixed_content_length(
        PLAYER_ID,
        {
            "announcement_url": "",
            "pre_announce": False,
            "pre_announce_url": "",
            "announce_player_id": None,
        },
    )

    assert fixed_content_length is True


async def test_flow_restart_follows_the_settings_of_the_output_that_renders(
    scenario: Scenario,  # noqa: F811
) -> None:
    """The flow stream of the queue of the player restarts by its rendering protocol's settings."""
    await _playing_through_dlna(scenario)

    sample_rate_mode, _ = scenario.mass.streams.audio._flow_restart_context(PLAYER_ID, None)

    assert sample_rate_mode == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
