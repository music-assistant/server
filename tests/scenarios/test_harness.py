"""Tests of the scenario harness itself: its clock, settle(), failure reporting and restarts."""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import time
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState

from music_assistant.controllers.players.controller import PlayerController
from music_assistant.controllers.players.protocol_linking import ProtocolLinkingMixin

from . import harness
from .harness import (
    CLOCK_START,
    FAKE_SETTING,
    TEST_TRACK_URI,
    Event,
    Scenario,
    ScenarioError,
    xfail_origin_error,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine
    from typing import Any

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}

# the stray tasks of the tests, as the loop only keeps a weak reference to a task
_STRAY_TASKS: list[asyncio.Task[None]] = []


async def test_advance_stops_at_every_timer(scenario: Scenario) -> None:
    """Chained timers run at their own deadlines, timers of one deadline in arming order."""
    loop = asyncio.get_running_loop()
    ran: dict[str, float] = {}
    order: list[int] = []

    def _chain(name: str, delay: float, then: tuple[str, float] | None = None) -> None:
        def _run() -> None:
            ran[name] = loop.time() - CLOCK_START
            if then:
                _chain(*then)

        loop.call_later(delay, _run)

    assert loop.time() == CLOCK_START
    _chain("debounce", 0.25, ("debounce again", 0.25))
    _chain("delay", 5, ("long delay", 15))
    for index in range(20):
        loop.call_later(1, order.append, index)
    await scenario.advance(0.5)
    await scenario.advance(19.5)

    assert ran == {"debounce": 0.25, "debounce again": 0.5, "delay": 5, "long delay": 20}
    assert order == list(range(20))


async def test_settle_waits_for_runnable_work_only(scenario: Scenario) -> None:
    """Work blocked on the clock or on another task is quiet, thread work is waited for."""
    lock = asyncio.Lock()
    steps: list[str] = []

    async def _hold_lock() -> None:
        async with lock:
            await asyncio.sleep(10)

    async def _wait_lock() -> None:
        async with lock:
            pass

    async def _sleep_in_generator() -> AsyncIterator[None]:
        await asyncio.sleep(10)
        yield

    async def _consume() -> None:
        async for _ in _sleep_in_generator():
            pass

    async def _wait_event() -> None:
        async with asyncio.timeout(10):
            await asyncio.Event().wait()

    async def _command() -> None:
        steps.append("started")
        await asyncio.to_thread(time.sleep, 0.05)
        steps.append("acted")
        await asyncio.sleep(30)
        steps.append("done")

    waits = [
        asyncio.wait_for(asyncio.sleep(10), 30),
        asyncio.gather(asyncio.sleep(10)),
        _hold_lock(),
        _wait_lock(),
        _consume(),
        _wait_event(),
    ]
    tasks = [asyncio.ensure_future(wait) for wait in waits]
    await scenario.settle()
    command = await scenario.command(_command())

    # the work blocked on the clock or on another task is left waiting, the clock unmoved
    assert asyncio.get_running_loop().time() == CLOCK_START
    assert not any(task.done() for task in tasks)
    assert steps == ["started", "acted"]
    for task in [*tasks, command]:
        task.cancel()
    await asyncio.gather(*tasks, command, return_exceptions=True)


async def test_settle_waits_for_thread_work_of_a_cancelled_caller(scenario: Scenario) -> None:
    """Thread work counts until the thread is done, also when its caller was cancelled."""
    done: list[bool] = []

    def _work() -> None:
        time.sleep(0.05)
        done.append(True)

    task = asyncio.ensure_future(asyncio.to_thread(_work))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await scenario.settle()

    assert done == [True]


async def test_restart_waits_for_thread_work_of_the_stopped_server(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread work still in flight at a restart finishes before the next boot starts."""
    done: list[bool] = []
    done_at_boot: list[list[bool]] = []
    boot = scenario._boot

    def _work() -> None:
        # outlasts the server stop, so only the wait for thread work lets it finish first
        time.sleep(0.5)
        done.append(True)

    async def _recording_boot() -> None:
        done_at_boot.append(list(done))
        await boot()

    monkeypatch.setattr(scenario, "_boot", _recording_boot)
    task = asyncio.ensure_future(asyncio.to_thread(_work))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await scenario.restart()

    assert done_at_boot == [[True]]


async def test_settle_fails_on_runaway_work(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work that never lets the loop get quiet fails the action."""
    monkeypatch.setattr(harness, "SETTLE_TIMEOUT", 0.2)

    async def _spin() -> None:
        while True:
            await asyncio.sleep(0)

    task = asyncio.ensure_future(_spin())
    try:
        with pytest.raises(ScenarioError, match="did not get quiet"):
            await scenario.settle()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_idle_fails_while_work_is_due(scenario: Scenario) -> None:
    """idle() refuses to call the server idle while it has work due within the horizon."""
    scenario.mass.call_later(15, lambda: None, task_id="later")

    with pytest.raises(ScenarioError, match="later"):
        await scenario.idle()
    await scenario.idle()


async def test_idle_fails_while_a_protocol_evaluation_is_pending(scenario: Scenario) -> None:
    """idle() refuses while an audio protocol waits 15 s for the hardware address of its device."""
    airplay = await scenario.add_provider("airplay")
    await scenario.register(airplay.protocol("airplay_den", "Den", role="audio"))

    with pytest.raises(ScenarioError, match="protocol evaluation of airplay_den"):
        await scenario.idle()
    await scenario.advance(5)
    await scenario.idle()


async def test_idle_passes_while_a_player_plays(scenario: Scenario) -> None:
    """The queue updates of a playing player are background work, not pending work."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)

    await scenario.idle()


async def test_single_instance_provider_survives_a_restart(scenario: Scenario) -> None:
    """A provider that allows one instance gets the instance id the server gives it."""
    kiosk = await scenario.add_provider("fully_kiosk")
    tablet = kiosk.protocol("kiosk", "Kiosk", identifiers=KITCHEN)
    await scenario.register(tablet)
    player_id = scenario.single_player(tablet).player_id

    await scenario.restart()
    await scenario.idle()

    assert kiosk.instance_id == "fully_kiosk"
    assert scenario.protocol("kiosk").provider.instance_id == "fully_kiosk"
    assert [player.player_id for player in scenario.visible_players()] == [player_id]


async def test_server_modules_share_the_clock(scenario: Scenario) -> None:
    """time.monotonic() and time.time() of every server module follow the loop clock."""
    loop = asyncio.get_running_loop()
    modules = [
        importlib.import_module(name)
        for name in (
            "music_assistant.controllers.players.controller",
            "music_assistant.controllers.players.audio_sources",
            "music_assistant.models.player",
        )
    ]
    wall_before = modules[0].time.time()

    await scenario.advance(60)

    assert {module.time.monotonic() for module in modules} == {CLOCK_START + 60, loop.time()}
    assert modules[0].time.time() - wall_before == 60


async def _fail_evaluation(scenario: Scenario) -> None:
    """Let a server task fail in the background."""

    async def _fail(*_args: object, **_kwargs: object) -> None:
        raise KeyError("bug in the protocol evaluation")

    with patch.object(ProtocolLinkingMixin, "_evaluate_pending_protocol", _fail):
        airplay = await scenario.add_provider("airplay")
        await scenario.register(airplay.protocol("airplay_den", "Den", role="audio"))
        await scenario.advance(20)


async def _log_warning(scenario: Scenario) -> None:
    """Let the server log a caught failure at warning level."""
    scenario.mass.players.logger.warning("bug caught and logged")


async def _fail_unload(scenario: Scenario) -> None:
    """Let a provider unload fail while the server stops."""

    async def _fail(*_args: object) -> None:
        raise KeyError("bug in on_provider_unload")

    await scenario.add_provider("sonos")
    with patch.object(PlayerController, "on_provider_unload", _fail):
        await scenario.restart()


async def _wait_forever() -> None:
    """Wait for an event nobody sets."""
    await asyncio.Event().wait()


async def _leave_task(scenario: Scenario) -> None:
    """Leave a task running that is nobody's."""
    _STRAY_TASKS.append(scenario.mass.loop.create_task(_wait_forever(), name="stray task"))


async def _leave_failing_task(scenario: Scenario) -> None:
    """Leave a task running whose cleanup fails when it is cancelled."""

    async def _task() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("task cleanup went wrong")

    _STRAY_TASKS.append(scenario.mass.loop.create_task(_task()))


async def _fail_command_cleanup(scenario: Scenario) -> None:
    """Start a command whose cleanup fails when it is cancelled."""

    async def _command() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("cleanup went wrong")

    await scenario.command(_command())


async def _leave_failed_command(scenario: Scenario) -> None:
    """Start a command that fails and is never awaited."""

    async def _command() -> None:
        raise ValueError("command went wrong")

    await scenario.command(_command())


@pytest.mark.parametrize(
    ("cause", "error"),
    [
        (_fail_evaluation, "bug in the protocol evaluation"),
        (_log_warning, "bug caught and logged"),
        (_fail_unload, "bug in on_provider_unload"),
        (_leave_task, "stray task"),
        (_leave_failing_task, "task cleanup went wrong"),
        (_fail_command_cleanup, "cleanup went wrong"),
        (_leave_failed_command, "command went wrong"),
    ],
)
async def test_failures_fail_the_scenario(
    tmp_path: pathlib.Path, cause: Callable[[Scenario], Coroutine[Any, Any, None]], error: str
) -> None:
    """What the server only logs or leaves behind fails the scenario when it closes."""
    scenario = Scenario(tmp_path)
    await scenario.start()
    try:
        await cause(scenario)
    finally:
        with pytest.raises(ScenarioError, match=error):
            await scenario.close()


async def test_expected_log_passes(tmp_path: pathlib.Path) -> None:
    """A failure the scenario causes on purpose passes once it is expected."""
    scenario = Scenario(tmp_path)
    await scenario.start()
    try:
        await _fail_evaluation(scenario)
        scenario.expect_log("bug in the protocol evaluation")
    finally:
        await scenario.close()


async def test_restart_leaves_nothing_of_the_old_server(scenario: Scenario) -> None:
    """After a restart no timer, command or event of the old server is left and the clock held."""
    loop = asyncio.get_running_loop()
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)
    old_server = scenario.mass
    old_events: list[EventType] = []
    old_server.subscribe(lambda event: old_events.append(event.event))

    async def _command() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            # cleanup that waits on a timer of its own
            await asyncio.sleep(0.01)

    command = await scenario.command(_command())
    before = loop.time()

    await scenario.restart()
    await scenario.advance(10)

    assert before + 10 == loop.time()
    assert command.cancelled()
    assert old_events == []
    with pytest.raises(ScenarioError, match=r"scenario\.protocol"):
        await scenario.register(speaker)
    await scenario.register(scenario.protocol("RINCON_KITCHEN"))


async def test_player_of_follows_the_attachment(scenario: Scenario) -> None:
    """A protocol is attached to the player of its device, or to none while it is unknown."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    kitchen_airplay = airplay.protocol("airplay_kitchen", "K", role="audio", identifiers=KITCHEN)
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio")
    await scenario.register(speaker)
    await scenario.register(kitchen_airplay)
    await scenario.register(den_airplay)

    kitchen_id = scenario.single_player(speaker).player_id
    assert scenario.player_of(speaker) == kitchen_id
    assert scenario.player_of(kitchen_airplay) == kitchen_id
    assert scenario.player_of(den_airplay) is None
    await scenario.advance(15)
    assert scenario.player_of(den_airplay) == scenario.single_player(den_airplay).player_id
    assert scenario.player_of(den_airplay) != kitchen_id
    await scenario.unregister(speaker)
    assert scenario.player_of(speaker) is None


async def test_player_configs_lists_the_settings_page(scenario: Scenario) -> None:
    """player_configs() lists stored player configs, disabled ones included, protocol configs not."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    await scenario.register(
        airplay.protocol("airplay_kitchen", "K", role="audio", identifiers=KITCHEN)
    )
    player_id = scenario.single_player(speaker).player_id
    await scenario.disable(player_id)
    await scenario.idle()

    assert scenario.protocol_config("airplay_kitchen") is not None
    assert [(c.player_id, c.enabled) for c in await scenario.player_configs()] == [
        (player_id, False)
    ]


async def test_a_none_identifier_value_drops_the_identifier(scenario: Scenario) -> None:
    """An identifier reported as None is dropped, also from the device after a restart."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol(
        "RINCON_KITCHEN",
        "Kitchen",
        identifiers={IdentifierType.UUID: "RINCON_A", IdentifierType.IP_ADDRESS: "1.2.3.4"},
    )
    await scenario.register(speaker)
    assert speaker.device_info.identifiers[IdentifierType.UUID] == "RINCON_A"

    await scenario.report(speaker, identifiers={IdentifierType.UUID: None})
    await scenario.restart()

    assert IdentifierType.UUID not in speaker.device_info.identifiers
    assert IdentifierType.UUID not in scenario.protocol("RINCON_KITCHEN").device_info.identifiers


async def test_player_config_only_misses_an_unstored_player(scenario: Scenario) -> None:
    """player_config() is None for a player without a stored config, and raises any failure."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id

    async def _fail(*_args: object) -> None:
        raise KeyError("bug in the player config")

    assert await scenario.player_config("unknown") is None
    with (
        patch.object(type(scenario.mass.config), "get_player_config", _fail),
        pytest.raises(KeyError, match="bug in the player config"),
    ):
        await scenario.player_config(player_id)


async def test_protocol_config_holds_the_protocol_setting(scenario: Scenario) -> None:
    """A protocol's own setting is stored with its config, which protocol_config() reads back."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id

    assert scenario.protocol_config("unknown") is None
    assert FAKE_SETTING not in (scenario.protocol_config("RINCON_KITCHEN") or {})
    await scenario.configure_player(player_id, **{FAKE_SETTING: "loud"})

    assert (scenario.protocol_config("RINCON_KITCHEN") or {})[FAKE_SETTING] == "loud"


async def test_stored_config_mentions_come_from_the_saved_settings(scenario: Scenario) -> None:
    """A pending save is written first; every key and value with the text is listed by path."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.configure_player(player_id, name="Kitchen Sonos")

    mentions = await scenario.stored_config_mentioning("RINCON_KITCHEN")

    assert "player_protocols/RINCON_KITCHEN" in mentions
    assert "player_protocols/RINCON_KITCHEN/protocol_id = 'RINCON_KITCHEN'" in mentions
    assert f"players/{player_id}/name = 'Kitchen Sonos'" in await scenario.stored_config_mentioning(
        "Kitchen Sonos"
    )
    assert await scenario.stored_config_mentioning("nothing like this") == []


async def test_play_runs_the_queue_on_the_fake_protocol(scenario: Scenario) -> None:
    """play() loads the test track into the player's queue and the protocol reports it playing."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id

    await scenario.play(player_id)

    assert [command[0] for command in speaker.commands] == ["play_media"]
    assert speaker.playback_state == PlaybackState.PLAYING
    queue = scenario.queue(player_id)
    assert queue is not None
    assert (queue.state, queue.items) == (PlaybackState.PLAYING, 1)
    assert queue.current_item is not None
    assert queue.current_item.uri == TEST_TRACK_URI
    await scenario.idle()


async def test_timeline_holds_the_commands_of_every_device_in_order(scenario: Scenario) -> None:
    """The timeline lists what each device received, across devices and providers, in order."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    den = airplay.protocol("airplay_den", "Den", role="audio")
    await scenario.register(kitchen)
    await scenario.register(den)
    await scenario.advance(15)
    await scenario.idle()

    await scenario.mass.players.cmd_volume_set(scenario.single_player(den).player_id, 20)
    await scenario.mass.players.cmd_volume_set(scenario.single_player(kitchen).player_id, 30)

    assert scenario.timeline == [
        ("airplay_den", ("volume_set", 20)),
        ("RINCON_KITCHEN", ("volume_set", 30)),
    ]


async def test_saved_queue_holds_the_items_a_queue_played(scenario: Scenario) -> None:
    """A queue that never held an item has no saved queue; a played one has, also before the debounce."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    await scenario.idle()
    player_id = scenario.single_player(speaker).player_id

    assert await scenario.saved_queue(player_id) is None
    await scenario.play(player_id)
    assert await scenario.saved_queue(player_id) == [TEST_TRACK_URI]
    assert await scenario.saved_queue("unknown") is None


async def test_session_only_flag_stays_with_the_device(scenario: Scenario) -> None:
    """A device created session-only reports so on every protocol object of it."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web", "Web Browser", session_only=True)
    await scenario.register(browser)

    assert browser.session_only is True
    await scenario.restart()
    assert scenario.protocol("sendspin_web").session_only is True
    assert scenario.provider("sendspin").protocol("sendspin_app", "App").session_only is False


def test_seed_values_delete_nested_keys() -> None:
    """A None value deletes a key, also below a branch the stored config does not have."""
    stored = {"players": {"a": {"name": "A", "enabled": True}}, "server_id": "x"}

    harness._merge(
        stored,
        {"players": {"a": {"name": None}, "b": {"name": "B", "values": None}}, "server_id": None},
    )

    assert stored == {"players": {"a": {"enabled": True}, "b": {"name": "B"}}}


def test_events_keep_repeated_additions(tmp_path: pathlib.Path) -> None:
    """Repeats of an update event collapse, repeats of other events are all listed."""
    scenario = Scenario(tmp_path)
    added, updated = EventType.PLAYER_ADDED, EventType.PLAYER_UPDATED
    scenario._events = [Event(added, "a"), Event(added, "a"), Event(updated, "a")] * 2

    assert scenario.events() == [Event(added, "a"), Event(added, "a"), Event(updated, "a")] * 2
    assert scenario.events(updated) == [Event(updated, "a")]


def test_xfail_counts_only_from_the_test_module() -> None:
    """An expected failure counts when this module raises it in the call phase, no harness error."""

    def _assert_here() -> None:
        raise AssertionError

    def _assert_elsewhere() -> None:
        # an AssertionError raised by another module
        unittest.TestCase().assertEqual(1, 2)  # noqa: PT009

    def _harness_error_here() -> None:
        raise ScenarioError

    this_module = pathlib.Path(__file__)

    assert xfail_origin_error(this_module, pytest.CallInfo.from_call(_assert_here, "call")) is None
    for when, function in (
        ("call", _assert_elsewhere),
        ("setup", _assert_here),
        ("call", _harness_error_here),
    ):
        call = pytest.CallInfo.from_call(function, when)  # type: ignore[arg-type]
        assert (reason := xfail_origin_error(this_module, call))
        assert reason.startswith("[xfail for the wrong reason]")
