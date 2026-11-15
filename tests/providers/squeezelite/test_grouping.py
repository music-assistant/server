"""Tests for groups of squeezelite players on a running server: they play through Sendspin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.providers.squeezelite.player import SqueezelitePlayer
from tests.scenarios.harness import DEFAULT_FEATURES

if TYPE_CHECKING:
    from music_assistant_models.player import Player as PlayerState_

    from tests.scenarios.harness import FakePlayerProvider, FakeProtocol, Scenario

GROUPING = frozenset({*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS})

KITCHEN = "b8:27:eb:11:22:33"
OFFICE = "b8:27:eb:44:55:66"
GARDEN = "b8:27:eb:77:88:99"


def _client(player_id: str, name: str, device_model: str = "SqueezeLite") -> MagicMock:
    """Return a connected slimproto client that reports the state its commands lead to."""
    client = MagicMock(
        player_id=player_id,
        device_model=device_model,
        device_type="squeezelite",
        device_address=f"192.168.1.{int(player_id[-2:], 16)}",
        max_sample_rate=192000,
        connected=True,
        powered=True,
        state=SlimPlayerState.STOPPED,
        volume_level=20,
        muted=False,
        current_media=None,
        elapsed_seconds=0,
    )
    client.name = name
    for command in ("stop", "mute", "volume_set", "power", "configure_display"):
        setattr(client, command, AsyncMock())
    return client


async def _squeezelite(
    scenario: Scenario, player_id: str, name: str, device_model: str = "SqueezeLite"
) -> SqueezelitePlayer:
    """
    Let a squeezelite client connect and return its protocol.

    :param scenario: The running server.
    :param player_id: The slimproto id of the client.
    :param name: The name the client reports.
    :param device_model: The device model the client reports.
    """
    client = _client(player_id, name, device_model)
    protocol = SqueezelitePlayer(
        scenario.provider("squeezelite"),  # type: ignore[arg-type]
        player_id,
        client,
    )
    await protocol.setup()
    await scenario.settle()

    def _leads_to(state: SlimPlayerState) -> AsyncMock:
        async def _command(*_args: Any, **_kwargs: Any) -> None:
            client.state = state
            protocol.handle_slim_event(
                SlimEvent(type=SlimEventType.PLAYER_UPDATED, player_id=player_id)
            )

        return AsyncMock(side_effect=_command)

    client.play_url = _leads_to(SlimPlayerState.PLAYING)
    client.play = _leads_to(SlimPlayerState.PLAYING)
    client.pause = _leads_to(SlimPlayerState.PAUSED)
    client.stop = _leads_to(SlimPlayerState.STOPPED)
    return protocol


async def _sendspin_output(
    scenario: Scenario, sendspin: FakePlayerProvider, rides_on: str, name: str
) -> FakeProtocol:
    """
    Attach the Sendspin output a bridge gives the player of a protocol.

    :param scenario: The running server.
    :param sendspin: The Sendspin provider.
    :param rides_on: The id of the protocol the bridge plays through.
    :param name: The name of the bridge client.
    """
    output = sendspin.protocol(
        f"spb_{rides_on}",
        name,
        role="audio",
        underlying_protocol_id=rides_on,
        supported_features=set(GROUPING),
        can_group_with={sendspin.instance_id},
    )
    await scenario.register(output)
    return output


async def _bridged_speakers(
    scenario: Scenario, *speakers: tuple[str, str]
) -> tuple[list[SqueezelitePlayer], list[FakeProtocol]]:
    """
    Connect squeezelite speakers that each have a Sendspin bridge.

    :param scenario: The running server.
    :param speakers: The slimproto id and name of each speaker.
    :return: Their squeezelite protocols and the Sendspin outputs of their bridges.
    """
    await scenario.add_provider("squeezelite")
    sendspin = await scenario.add_provider("sendspin")
    protocols = [await _squeezelite(scenario, player_id, name) for player_id, name in speakers]
    outputs = [
        await _sendspin_output(scenario, sendspin, player_id, name) for player_id, name in speakers
    ]
    await scenario.idle()
    return protocols, outputs


def _player_id(protocol: SqueezelitePlayer) -> str:
    """Return the id of the player a squeezelite protocol is attached to."""
    assert protocol.player is not None
    return protocol.player.player_id


def _state(scenario: Scenario, player_id: str) -> PlayerState_:
    """Return the state a player publishes."""
    state = scenario.player(player_id)
    assert state is not None
    return state


def _active_output(scenario: Scenario, player_id: str) -> str | None:
    """Return the output a player plays through."""
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    return player.active_output_protocol


def _streams(protocol: SqueezelitePlayer) -> int:
    """Return how many streams the squeezelite protocol started on its client."""
    return cast("int", cast("MagicMock", protocol.client).play_url.await_count)


def _commands(output: FakeProtocol, *names: str) -> list[tuple[Any, ...]]:
    """Return the commands of the given names a Sendspin output received, in order."""
    return [command for command in output.commands if command[0] in names]


async def _stop(scenario: Scenario, player_id: str) -> None:
    """Stop what a player plays, letting the clock run for the stop to settle."""
    stop = await scenario.command(scenario.mass.players.cmd_stop(player_id))
    await scenario.advance(12)
    await stop
    await scenario.idle()


async def test_speakers_group_on_their_sendspin_outputs(scenario: Scenario) -> None:
    """
    Two squeezelite speakers group through the Sendspin outputs of their bridges.

    The group then plays as a Sendspin group: the squeezelite protocols start no stream.
    """
    (kitchen, office), (kitchen_out, office_out) = await _bridged_speakers(
        scenario, (KITCHEN, "Kitchen"), (OFFICE, "Office")
    )
    kitchen_id, office_id = _player_id(kitchen), _player_id(office)
    assert _state(scenario, kitchen_id).can_group_with == {office_id}

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()

    assert _commands(kitchen_out, "set_members") == [
        ("set_members", [office_out.protocol_id], None)
    ]
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, kitchen_id).group_members == [kitchen_id, office_id]
    assert _active_output(scenario, kitchen_id) == kitchen_out.protocol_id
    assert _active_output(scenario, office_id) == office_out.protocol_id

    await scenario.play(kitchen_id)

    assert len(_commands(kitchen_out, "play_media")) == 1
    assert _commands(office_out, "play_media") == []
    assert [_streams(kitchen), _streams(office)] == [0, 0]
    await _stop(scenario, kitchen_id)


async def test_a_speaker_on_its_own_plays_its_own_stream(scenario: Scenario) -> None:
    """A speaker that plays alone plays through the squeezelite protocol, not its bridge."""
    (kitchen, _office), (kitchen_out, _) = await _bridged_speakers(
        scenario, (KITCHEN, "Kitchen"), (OFFICE, "Office")
    )
    kitchen_id = _player_id(kitchen)

    await scenario.play(kitchen_id)

    assert _streams(kitchen) == 1
    assert _active_output(scenario, kitchen_id) == "native"
    assert kitchen_out.commands == []
    await _stop(scenario, kitchen_id)


async def test_a_speaker_that_plays_moves_to_its_sendspin_output_for_a_member(
    scenario: Scenario,
) -> None:
    """
    A speaker that plays alone carries on through its Sendspin output once a member joins.

    Its bridge takes the client over with the stream of the group, so the squeezelite
    protocol neither stops the client nor starts another stream on it.
    """
    (kitchen, office), (kitchen_out, office_out) = await _bridged_speakers(
        scenario, (KITCHEN, "Kitchen"), (OFFICE, "Office")
    )
    kitchen_id, office_id = _player_id(kitchen), _player_id(office)
    await scenario.play(kitchen_id)

    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.idle()

    assert _commands(kitchen_out, "set_members", "play_media")[0] == (
        "set_members",
        [office_out.protocol_id],
        None,
    )
    assert len(_commands(kitchen_out, "play_media")) == 1
    assert _active_output(scenario, kitchen_id) == kitchen_out.protocol_id
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING
    assert [_streams(kitchen), _streams(office)] == [1, 0]
    cast("MagicMock", kitchen.client).stop.assert_not_awaited()
    await _stop(scenario, kitchen_id)


async def test_a_speaker_that_left_its_group_plays_its_own_stream_again(
    scenario: Scenario,
) -> None:
    """
    A member that leaves plays on its own right away, its former leader from its next start.

    The leader plays on through its Sendspin output until it is stopped.
    """
    (kitchen, office), (kitchen_out, office_out) = await _bridged_speakers(
        scenario, (KITCHEN, "Kitchen"), (OFFICE, "Office")
    )
    kitchen_id, office_id = _player_id(kitchen), _player_id(office)
    await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[office_id])
    await scenario.play(kitchen_id)

    await scenario.mass.players.cmd_ungroup(office_id)
    await scenario.idle()

    assert _active_output(scenario, office_id) is None
    assert _active_output(scenario, kitchen_id) == kitchen_out.protocol_id
    assert _state(scenario, kitchen_id).playback_state == PlaybackState.PLAYING

    await scenario.play(office_id)

    assert _streams(office) == 1
    assert _active_output(scenario, office_id) == "native"
    assert _commands(office_out, "play_media") == []

    await _stop(scenario, kitchen_id)
    assert _active_output(scenario, kitchen_id) is None
    await scenario.play(kitchen_id)

    assert _streams(kitchen) == 1
    assert _active_output(scenario, kitchen_id) == "native"
    assert len(_commands(kitchen_out, "play_media")) == 1
    await _stop(scenario, kitchen_id)
    await _stop(scenario, office_id)


@pytest.mark.parametrize("had_output", [False, True], ids=["never bridged", "output gone"])
async def test_a_speaker_without_a_sendspin_output_stays_out_of_groups(
    scenario: Scenario, caplog: pytest.LogCaptureFixture, had_output: bool
) -> None:
    """A speaker without a Sendspin output is offered to no player, and can not join or lead."""
    (kitchen,), (kitchen_out,) = await _bridged_speakers(scenario, (KITCHEN, "Kitchen"))
    garden = await _squeezelite(scenario, GARDEN, "Garden")
    if had_output:
        garden_out = await _sendspin_output(
            scenario, scenario.provider("sendspin"), GARDEN, "Garden"
        )
        await scenario.idle()
        assert _state(scenario, _player_id(kitchen)).can_group_with == {_player_id(garden)}
        await scenario.unregister(garden_out)
    await scenario.idle()
    kitchen_id, garden_id = _player_id(kitchen), _player_id(garden)

    assert PlayerFeature.SET_MEMBERS not in _state(scenario, garden_id).supported_features
    assert _state(scenario, garden_id).can_group_with == set()
    assert _state(scenario, kitchen_id).can_group_with == set()

    scenario.expect_log("can not be grouped with")
    with caplog.at_level(logging.WARNING, logger="music_assistant"):
        await scenario.mass.players.cmd_set_members(kitchen_id, player_ids_to_add=[garden_id])
        await scenario.idle()

    assert "Player Garden can not be grouped with Kitchen" in caplog.text
    assert _commands(kitchen_out, "set_members") == []
    assert _state(scenario, garden_id).synced_to is None
    with pytest.raises(UnsupportedFeaturedException, match="does not support group commands"):
        await scenario.mass.players.cmd_set_members(garden_id, player_ids_to_add=[kitchen_id])


@pytest.mark.parametrize("leader", ["squeezelite", "airplay"])
async def test_a_speaker_groups_with_an_airplay_speaker_over_sendspin(
    scenario: Scenario, leader: str
) -> None:
    """A squeezelite speaker and an AirPlay speaker with a bridge group as Sendspin players."""
    (kitchen,), (kitchen_out,) = await _bridged_speakers(scenario, (KITCHEN, "Kitchen"))
    airplay = await scenario.add_provider("airplay")
    den = airplay.protocol(
        "ap_den",
        "Den",
        identifiers={IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55"},
        supported_features=set(GROUPING),
        can_group_with={airplay.instance_id},
    )
    await scenario.register(den)
    den_out = await _sendspin_output(scenario, scenario.provider("sendspin"), "ap_den", "Den")
    await scenario.idle()
    kitchen_id, den_id = _player_id(kitchen), scenario.player_of(den)
    assert den_id is not None
    leader_id, member_id = (kitchen_id, den_id) if leader == "squeezelite" else (den_id, kitchen_id)
    leader_out, member_out = (
        (kitchen_out, den_out) if leader == "squeezelite" else (den_out, kitchen_out)
    )

    await scenario.mass.players.cmd_set_members(leader_id, player_ids_to_add=[member_id])
    await scenario.idle()

    assert _commands(leader_out, "set_members") == [("set_members", [member_out.protocol_id], None)]
    assert _commands(den, "set_members") == []
    assert _state(scenario, member_id).synced_to == leader_id
    assert _active_output(scenario, kitchen_id) == kitchen_out.protocol_id


@pytest.mark.parametrize("leader", ["speaker", "wiim"])
async def test_a_wiim_groups_with_a_speaker_over_sendspin(scenario: Scenario, leader: str) -> None:
    """
    A WiiM and a squeezelite speaker group as Sendspin players, also when the WiiM leads.

    The squeezelite output of the WiiM carries no group: its Sendspin output, which rides on
    its AirPlay output, does.
    """
    (kitchen,), (kitchen_out,) = await _bridged_speakers(scenario, (KITCHEN, "Kitchen"))
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    wiim_mac = {IdentifierType.MAC_ADDRESS: OFFICE}
    study = wiim.protocol("wiim_study", "Study", identifiers=wiim_mac)
    await scenario.register(study)
    study_airplay = airplay.protocol(
        "ap_study",
        "Study",
        role="audio",
        identifiers=wiim_mac,
        supported_features=set(GROUPING),
        can_group_with={airplay.instance_id},
    )
    await scenario.register(study_airplay)
    study_squeezelite = await _squeezelite(scenario, OFFICE, "Study", "WiiM Player")
    study_out = await _sendspin_output(scenario, scenario.provider("sendspin"), "ap_study", "Study")
    await scenario.idle()
    kitchen_id, study_id = _player_id(kitchen), _player_id(study_squeezelite)
    assert study_id == scenario.player_of(study)
    leader_id, member_id = (kitchen_id, study_id) if leader == "speaker" else (study_id, kitchen_id)
    leader_out, member_out = (
        (kitchen_out, study_out) if leader == "speaker" else (study_out, kitchen_out)
    )

    await scenario.mass.players.cmd_set_members(leader_id, player_ids_to_add=[member_id])
    await scenario.play(leader_id)

    assert _commands(leader_out, "set_members") == [("set_members", [member_out.protocol_id], None)]
    assert _commands(study_airplay, "set_members", "play_media") == []
    assert len(_commands(leader_out, "play_media")) == 1
    assert _active_output(scenario, study_id) == study_out.protocol_id
    assert [_streams(kitchen), _streams(study_squeezelite)] == [0, 0]
    await _stop(scenario, leader_id)


async def test_a_stored_sync_group_of_speakers_plays_through_sendspin(scenario: Scenario) -> None:
    """A sync group stores its members as players, so it plays on whichever output groups them."""
    speakers = ((KITCHEN, "Kitchen"), (OFFICE, "Office"))
    (kitchen, office), _ = await _bridged_speakers(scenario, *speakers)
    kitchen_id, office_id = _player_id(kitchen), _player_id(office)
    group = await scenario.mass.players.create_group_player(
        "sync_group", "Downstairs", [kitchen_id, office_id], dynamic=False
    )
    group_id = group.player_id
    await scenario.idle()
    assert group.own_protocol is not None
    stored = scenario.protocol_config(group.own_protocol.protocol_id)
    assert stored is not None
    assert stored["group_members"] == [kitchen_id, office_id]

    await scenario.restart()
    kitchen, office = [
        await _squeezelite(scenario, player_id, name) for player_id, name in speakers
    ]
    await scenario.idle()
    kitchen_out = scenario.protocol(f"spb_{KITCHEN}")
    office_out = scenario.protocol(f"spb_{OFFICE}")

    await scenario.play(group_id)
    await scenario.idle()

    assert _commands(kitchen_out, "set_members") == [
        ("set_members", [office_out.protocol_id], None)
    ]
    assert len(_commands(kitchen_out, "play_media")) == 1
    assert _state(scenario, office_id).synced_to == kitchen_id
    assert _state(scenario, group_id).playback_state == PlaybackState.PLAYING
    assert [_streams(kitchen), _streams(office)] == [0, 0]
    await _stop(scenario, group_id)
