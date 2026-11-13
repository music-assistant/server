"""The queue DJ of a player through the lifecycle of that player, on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, IdentifierType

from .test_queue_dj import DummyQueueDJ

if TYPE_CHECKING:
    from pathlib import Path

    from tests.scenarios.harness import Scenario

LIVING = {IdentifierType.MAC_ADDRESS: "A8:40:41:00:00:01"}
# the events the provider follows the queues with
DJ_EVENTS = (
    EventType.QUEUE_ADDED,
    EventType.QUEUE_ITEMS_UPDATED,
    EventType.QUEUE_UPDATED,
    EventType.PLAYER_REMOVED,
)


def _queue_dj(scenario: Scenario, tmp_path: Path) -> DummyQueueDJ:
    """
    Return a queue DJ that follows the events of the running server.

    :param scenario: The running server.
    :param tmp_path: Where the DJ stores its assignments.
    """
    dj = DummyQueueDJ(tmp_path)
    dj.mass = scenario.mass  # type: ignore[assignment]
    scenario.mass.subscribe(dj._on_dj_queue_event, DJ_EVENTS)
    return dj


async def test_a_dj_stays_on_the_queue_of_a_player_that_is_disabled_and_enabled(
    scenario: Scenario, tmp_path: Path
) -> None:
    """Disabling a player is no removal: its queue keeps its DJ once it is enabled again."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.attach(living)
    player_id = scenario.single_player(living).player_id
    dj = _queue_dj(scenario, tmp_path)
    await scenario.play(player_id)
    await dj.set_queue_dj(player_id, "rick")
    await scenario.idle()

    await scenario.disable(player_id)
    await scenario.idle()
    await scenario.enable(player_id)
    await scenario.idle()

    assert scenario.player(player_id) is not None
    assert dj._dj_queues[player_id].host_id == "rick"


async def test_a_dj_goes_with_its_player_when_that_is_removed(
    scenario: Scenario, tmp_path: Path
) -> None:
    """A removed player is gone: its queue loses its DJ."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.attach(living)
    player_id = scenario.single_player(living).player_id
    dj = _queue_dj(scenario, tmp_path)
    await dj.set_queue_dj(player_id, "rick")
    await scenario.idle()

    await scenario.remove(player_id)
    await scenario.idle()

    assert player_id not in dj._dj_queues
