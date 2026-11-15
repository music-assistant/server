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
    await scenario.register(living)
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
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    dj = _queue_dj(scenario, tmp_path)
    await dj.set_queue_dj(player_id, "rick")
    await scenario.idle()

    await scenario.remove(player_id)
    await scenario.idle()

    assert player_id not in dj._dj_queues


async def test_a_dj_keeps_a_browser_player_whose_client_disconnects(
    scenario: Scenario, tmp_path: Path
) -> None:
    """
    A DJ switched on for the queue of a browser player keeps that player while it is away.

    Its queue is empty; the player stays, unavailable, and when its client connects again its
    queue still has the DJ. Switching the DJ off again does not undo that.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True)
    await scenario.register(browser)
    dj = _queue_dj(scenario, tmp_path)
    await dj.set_queue_dj("sendspin_web_3f2a", "rick")
    await scenario.idle()

    await scenario.unregister(browser)
    await scenario.idle()

    away = scenario.player("sendspin_web_3f2a")
    assert away is not None
    assert away.available is False
    assert scenario.removals() == []

    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert dj._dj_queues["sendspin_web_3f2a"].host_id == "rick"

    await dj.set_queue_dj("sendspin_web_3f2a", None)
    await scenario.unregister(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert scenario.player("sendspin_web_3f2a") is not None
    assert scenario.removals() == []


async def test_a_dj_goes_with_a_disabled_player_that_is_removed(
    scenario: Scenario, tmp_path: Path
) -> None:
    """A disabled player that is then removed is gone: its queue loses its DJ."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    dj = _queue_dj(scenario, tmp_path)
    await dj.set_queue_dj(player_id, "rick")
    await scenario.disable(player_id)
    await scenario.idle()

    await scenario.remove(player_id)
    await scenario.idle()

    assert player_id not in dj._dj_queues


async def test_a_dj_goes_with_a_disabled_player_that_goes_in_a_merge(
    scenario: Scenario, tmp_path: Path
) -> None:
    """A disabled player merged into another one is gone: its queue loses its DJ."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    tv_airplay = airplay.protocol(
        "ap_tv", "Living TV", role="audio", identifiers={IdentifierType.AIRPLAY_ID: "C0:FF:EE:01"}
    )
    tv_cast = chromecast.protocol(
        "cast_tv", "Living TV", role="audio", identifiers={IdentifierType.CAST_UUID: "6f0c1a2b"}
    )
    await scenario.register(tv_airplay)
    await scenario.advance(15)
    await scenario.register(tv_cast)
    await scenario.advance(15)
    keep_id = scenario.single_player(tv_airplay).player_id
    absorb_id = scenario.single_player(tv_cast).player_id
    dj = _queue_dj(scenario, tmp_path)
    await dj.set_queue_dj(absorb_id, "rick")
    await scenario.disable(absorb_id)
    await scenario.idle()

    await scenario.mass.players.merge(absorb_id, keep_id)
    await scenario.idle()

    assert absorb_id not in dj._dj_queues
