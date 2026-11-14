"""Tests for the queue of a player that has only its control protocol left: nothing to play on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import IdentifierType, PlayerFeature, QueueOption
from music_assistant_models.errors import PlayerUnavailableError

# the real server with fake providers, for a device whose audio protocol is away
from tests.scenarios.conftest import scenario  # noqa: F401
from tests.scenarios.harness import MUSIC_PROVIDER, TEST_TRACK_URI

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from tests.scenarios.harness import FakeProtocol, Scenario

DEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


async def _den(
    scenario: Scenario,  # noqa: F811
) -> tuple[str, FakeProtocol, FakeProtocol]:
    """Return a device that plays through DLNA and is controlled through its control protocol."""
    wiim = await scenario.add_provider("wiim")
    dlna = await scenario.add_provider("dlna")
    control = wiim.protocol(
        "wiim_den",
        "Den",
        role="control",
        identifiers=DEN,
        supported_features={PlayerFeature.SET_MEMBERS},
    )
    audio = dlna.protocol("dlna_den", "Den", role="audio", identifiers=DEN)
    await scenario.register(control)
    await scenario.register(audio)
    await scenario.idle()
    return scenario.single_player(control, audio).player_id, control, audio


def _queue_and_source(
    scenario: Scenario,  # noqa: F811
    player_id: str,
) -> tuple[Any, ...]:
    """Return what a play request may not change: the items, the position and the source."""
    queue, state = scenario.queue(player_id), scenario.player(player_id)
    assert queue is not None
    assert state is not None
    items = [item.queue_item_id for item in scenario.mass.player_queues.items(player_id)]
    return items, queue.current_index, queue.current_item, state.active_source


@pytest.mark.parametrize(
    "request_playback",
    [
        lambda mass, player_id: mass.player_queues.play_media(
            player_id, TEST_TRACK_URI, option=QueueOption.PLAY
        ),
        lambda mass, player_id: mass.player_queues.play_media(
            player_id, TEST_TRACK_URI, option=QueueOption.REPLACE
        ),
        lambda mass, player_id: mass.player_queues.play_index(player_id, 0),
        lambda mass, player_id: mass.player_queues.resume(player_id),
        lambda mass, player_id: mass.players.select_source(player_id, "source_1"),
    ],
    ids=["play", "replace", "play_index", "resume", "audio_source"],
)
async def test_playback_on_a_player_with_only_its_control_protocol_is_refused_up_front(
    scenario: Scenario,  # noqa: F811
    request_playback: Callable[[Any, str], Coroutine[Any, Any, None]],
) -> None:
    """Nothing is loaded or changed for playback that no protocol of the player can render."""
    player_id, _, audio = await _den(scenario)
    await scenario.play(player_id)
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.unregister(audio)
    before = _queue_and_source(scenario, player_id)

    with pytest.raises(PlayerUnavailableError):
        await request_playback(scenario.mass, player_id)

    assert _queue_and_source(scenario, player_id) == before


async def test_switched_off_player_plays_once_a_protocol_that_can_render_is_back(
    scenario: Scenario,  # noqa: F811
) -> None:
    """The power on of a play waits for the output of the device, not for its control."""
    player_id, control, audio = await _den(scenario)
    plug = await scenario.add_power_control("switch.den_plug", "Den plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    await scenario.switch(plug, False)
    await scenario.unregister(control)
    await scenario.unregister(audio)
    await scenario.idle()
    await scenario.mass.config._create_provider_instance(MUSIC_PROVIDER, {})
    await scenario.settle()

    play = await scenario.command(scenario.mass.player_queues.play_media(player_id, TEST_TRACK_URI))

    assert plug.commands == [True]
    await scenario.register(control)
    # the power on looks for the device every 0.5 s
    await scenario.advance(1)
    assert not play.done()
    await scenario.register(audio)
    await scenario.advance(1)
    await play

    assert "play_media" in [command[0] for command in audio.commands]
