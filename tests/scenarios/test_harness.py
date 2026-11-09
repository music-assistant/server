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

from music_assistant.controllers.players.protocol_linking import ProtocolLinkingMixin

from . import harness
from .harness import CLOCK_START, Event, Scenario, xfail_origin_error

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


async def test_advance_stops_at_every_timer(scenario: Scenario) -> None:
    """Chained timers run at their own deadlines, however advance() is cut up."""
    loop = asyncio.get_running_loop()
    ran: dict[str, float] = {}

    def _chain(name: str, delay: float, then: tuple[str, float] | None = None) -> None:
        def _run() -> None:
            ran[name] = loop.time() - CLOCK_START
            if then:
                _chain(*then)

        loop.call_later(delay, _run)

    assert loop.time() == CLOCK_START
    _chain("debounce", 0.25, ("debounce again", 0.25))
    _chain("delay", 5, ("long delay", 15))
    await scenario.advance(0.5)
    await scenario.advance(19.5)

    assert ran == {"debounce": 0.25, "debounce again": 0.5, "delay": 5, "long delay": 20}


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
    started = time.monotonic()
    await scenario.settle()
    command = await scenario.command(_command())

    assert time.monotonic() - started < 1
    assert steps == ["started", "acted"]
    for task in [*tasks, command]:
        task.cancel()
    await asyncio.gather(*tasks, command, return_exceptions=True)


async def test_settle_fails_on_runaway_work(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work that never lets the loop get quiet fails the action."""
    monkeypatch.setattr(harness, "SETTLE_TIMEOUT", 0.2)

    async def _spin() -> None:
        while True:
            await asyncio.sleep(0)

    task = asyncio.ensure_future(_spin())
    with pytest.raises(RuntimeError, match="did not get quiet"):
        await scenario.settle()
    task.cancel()


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


@pytest.mark.parametrize("expected", [False, True])
async def test_background_failure_fails_the_scenario(
    tmp_path: pathlib.Path, expected: bool
) -> None:
    """A server task that fails in the background fails the scenario, unless it is expected."""

    async def _fail(*_args: object) -> None:
        raise KeyError("bug in the protocol evaluation")

    scenario = Scenario(tmp_path)
    await scenario.start()
    try:
        with patch.object(ProtocolLinkingMixin, "_delayed_protocol_evaluation", _fail):
            airplay = await scenario.add_provider("airplay")
            await scenario.attach(airplay.protocol("airplay_den", "Den", role="audio"))
            await scenario.advance(20)
        if expected:
            scenario.expect_log("bug in the protocol evaluation")
    finally:
        if expected:
            await scenario.close()
        else:
            with pytest.raises(RuntimeError, match="bug in the protocol evaluation"):
                await scenario.close()


async def test_unawaited_command_failure_fails_the_scenario(tmp_path: pathlib.Path) -> None:
    """A command that fails and is never awaited fails the scenario."""

    async def _fail() -> None:
        raise ValueError("command went wrong")

    scenario = Scenario(tmp_path)
    await scenario.start()
    await scenario.command(_fail())
    with pytest.raises(RuntimeError, match="command went wrong"):
        await scenario.close()


async def test_restart_leaves_nothing_of_the_old_server(scenario: Scenario) -> None:
    """After a restart no timer, command or event of the old server is left and the clock held."""
    loop = asyncio.get_running_loop()
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    await scenario.report(speaker, playback_state=PlaybackState.PLAYING)
    old_server = scenario.mass
    old_events: list[EventType] = []
    old_server.subscribe(lambda event: old_events.append(event.event))
    command = await scenario.command(asyncio.sleep(60))
    before = loop.time()

    await scenario.restart()
    await scenario.advance(10)

    assert before + 10 == loop.time()
    assert command.cancelled()
    assert all(handle.cancelled() for handle in old_server._tracked_timers.values())
    assert old_events == []
    with pytest.raises(RuntimeError, match=r"scenario\.protocol"):
        await scenario.attach(speaker)
    await scenario.attach(scenario.protocol("RINCON_KITCHEN"))


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
    """An expected failure counts when this module raises it in the call phase only."""

    def _assert_here() -> None:
        raise AssertionError

    def _assert_elsewhere() -> None:
        # an AssertionError raised by another module
        unittest.TestCase().assertEqual(1, 2)  # noqa: PT009

    this_module = pathlib.Path(__file__)

    assert xfail_origin_error(this_module, pytest.CallInfo.from_call(_assert_here, "call")) is None
    for when, function in (("call", _assert_elsewhere), ("setup", _assert_here)):
        call = pytest.CallInfo.from_call(function, when)  # type: ignore[arg-type]
        assert (reason := xfail_origin_error(this_module, call))
        assert reason.startswith("[xfail for the wrong reason]")
