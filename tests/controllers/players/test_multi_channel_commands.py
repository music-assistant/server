"""Tests for the command that combines speakers into one multi-channel player."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, ProviderFeature, SpeakerChannel
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)

from tests.common import as_user

if TYPE_CHECKING:
    from tests.scenarios.harness import FakePlayerProvider, Scenario

KITCHEN = {IdentifierType.MAC_ADDRESS: "aa:bb:cc:00:00:01"}
OFFICE = {IdentifierType.MAC_ADDRESS: "aa:bb:cc:00:00:02"}
DEN = {IdentifierType.MAC_ADDRESS: "aa:bb:cc:00:00:03"}


async def _speakers(
    scenario: Scenario, *, combines: bool = True
) -> tuple[FakePlayerProvider, str, str]:
    """
    Attach the Kitchen and Office speakers of a provider that can combine speakers.

    :param combines: Whether the provider can combine speakers into a multi-channel player.
    :return: The provider and the player ids of Kitchen and Office.
    """
    sendspin = await scenario.add_provider("sendspin")
    if combines:
        sendspin._supported_features.add(ProviderFeature.CREATE_MULTI_CHANNEL_PLAYER)
    sendspin.create_multi_channel_player = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    sendspin.get_multi_channel_candidates = MagicMock(  # type: ignore[method-assign]
        return_value=["ss_kitchen", "ss_office"]
    )
    kitchen = sendspin.protocol("ss_kitchen", "Kitchen", identifiers=KITCHEN)
    office = sendspin.protocol("ss_office", "Office", identifiers=OFFICE)
    await scenario.register(kitchen)
    await scenario.register(office)
    players = {player.name: player.player_id for player in scenario.mass.players}
    return sendspin, players["Kitchen"], players["Office"]


async def test_the_speakers_go_to_their_provider_by_the_ids_of_its_protocols(
    scenario: Scenario,
) -> None:
    """The provider gets the channel of each speaker by the id of its own protocol of it."""
    sendspin, kitchen_id, office_id = await _speakers(scenario)

    await scenario.mass.players.create_multi_channel_player(
        sendspin.instance_id,
        " Living ",
        {kitchen_id: SpeakerChannel.FRONT_LEFT, office_id: SpeakerChannel.FRONT_RIGHT},
    )

    sendspin.create_multi_channel_player.assert_awaited_once_with(  # type: ignore[attr-defined]
        "Living",
        {"ss_kitchen": SpeakerChannel.FRONT_LEFT, "ss_office": SpeakerChannel.FRONT_RIGHT},
    )


@pytest.mark.parametrize(
    ("request_kind", "error"),
    [
        ("an unknown provider", ProviderUnavailableError),
        ("a provider that can not combine speakers", UnsupportedFeaturedException),
        ("no name", InvalidDataError),
        ("an unknown player", PlayerUnavailableError),
        ("a player without an output of the provider", UnsupportedFeaturedException),
        ("one channel twice", InvalidDataError),
        ("an unknown channel", InvalidDataError),
    ],
)
async def test_a_request_the_controller_can_not_pass_on_is_refused(
    scenario: Scenario, request_kind: str, error: type[Exception]
) -> None:
    """A request with a wrong provider, name, player or channel never reaches the provider."""
    sendspin, kitchen_id, office_id = await _speakers(
        scenario, combines=request_kind != "a provider that can not combine speakers"
    )
    airplay = await scenario.add_provider("airplay")
    await scenario.register(airplay.protocol("ap_den", "Den", identifiers=DEN))
    den_id = next(player.player_id for player in scenario.mass.players if player.name == "Den")
    provider = "unknown" if request_kind == "an unknown provider" else sendspin.instance_id
    name = "" if request_kind == "no name" else "Living"
    members: dict[str, Any] = {
        kitchen_id: SpeakerChannel.FRONT_LEFT,
        office_id: SpeakerChannel.FRONT_RIGHT,
    }
    if request_kind == "an unknown player":
        members["pl_unknown"] = SpeakerChannel.LFE
    elif request_kind == "a player without an output of the provider":
        members[den_id] = SpeakerChannel.LFE
    elif request_kind == "one channel twice":
        members[office_id] = SpeakerChannel.FRONT_LEFT
    elif request_kind == "an unknown channel":
        members[office_id] = SpeakerChannel.UNKNOWN

    with pytest.raises(error):
        await scenario.mass.players.create_multi_channel_player(provider, name, members)

    sendspin.create_multi_channel_player.assert_not_awaited()  # type: ignore[attr-defined]


async def test_the_candidates_are_the_players_of_the_protocols_their_provider_names(
    scenario: Scenario,
) -> None:
    """The candidates of a provider are the players of the protocols it can combine."""
    sendspin, kitchen_id, office_id = await _speakers(scenario)

    candidates = scenario.mass.players.multi_channel_candidates(sendspin.instance_id)

    assert candidates == [kitchen_id, office_id]


async def test_the_candidates_leave_out_the_players_a_user_may_not_use(
    scenario: Scenario,
) -> None:
    """A user limited to some players only gets those of them that can be combined."""
    sendspin, kitchen_id, _ = await _speakers(scenario)

    with as_user([kitchen_id]):
        candidates = scenario.mass.players.multi_channel_candidates(sendspin.instance_id)

    assert candidates == [kitchen_id]


@pytest.mark.parametrize(
    ("request_kind", "error"),
    [
        ("an unknown provider", ProviderUnavailableError),
        ("a provider that can not combine speakers", UnsupportedFeaturedException),
    ],
)
async def test_the_candidates_of_a_provider_that_can_not_combine_are_refused(
    scenario: Scenario, request_kind: str, error: type[Exception]
) -> None:
    """Candidates are refused for a provider that is not there or can not combine speakers."""
    sendspin, _, _ = await _speakers(
        scenario, combines=request_kind != "a provider that can not combine speakers"
    )
    provider = "unknown" if request_kind == "an unknown provider" else sendspin.instance_id

    with pytest.raises(error):
        scenario.mass.players.multi_channel_candidates(provider)

    sendspin.get_multi_channel_candidates.assert_not_called()  # type: ignore[attr-defined]
