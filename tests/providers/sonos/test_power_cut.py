"""Tests for a Sonos speaker whose power is cut at a power control, and comes back."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosonos.exceptions import CannotConnect
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import DEFAULT_POWER_ON_STARTUP_TIME
from music_assistant.controllers.players.constants import POWER_RESTORE_TIMEOUT
from music_assistant.controllers.players.power import EXTERNAL_POWER_OFF_STOP_DELAY
from tests.providers.sonos.test_lifecycle import _attach_speaker
from tests.providers.sonos.test_player import _bind_player, _connect_player
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sonos.player import SonosPlayer
    from tests.scenarios.harness import FakePowerControl

RECONNECT = "sonos_reconnect_sonos_player"
# a stream of the server itself, whose address goes to the speaker as it is
MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac", media_type=MediaType.FLOW_STREAM)


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


def _speaker(mass: MusicAssistant) -> tuple[SonosPlayer, MagicMock, list[tuple[bool, bool]]]:
    """
    Bind a speaker that is attached to a player, and whose API port answers.

    :param mass: A MusicAssistant with the real task and timer machinery.
    :return: The speaker, its client, and (connected, available) at each state it published.
    """
    speaker, client = _bind_player(mass)
    speaker._attr_name = "Den"
    speaker._player = MagicMock()
    speaker.update_attributes = MagicMock()  # type: ignore[method-assign]
    speaker._is_reachable = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _start_listening(init_ready: asyncio.Event) -> None:
        init_ready.set()
        await asyncio.sleep(3600)

    client.connect = AsyncMock()
    client.start_listening = _start_listening
    published: list[tuple[bool, bool]] = []
    update_state: Any = speaker.update_state
    update_state.side_effect = lambda *_args, **_kwargs: published.append(
        (speaker.connected, bool(speaker._attr_available))
    )
    return speaker, client, published


async def _ended(listener: asyncio.Task[None] | None) -> bool:
    """Return whether the listener of a connection ended as cancelled."""
    assert listener is not None
    with pytest.raises(asyncio.CancelledError):
        await listener
    return True


async def test_a_power_cut_lets_go_of_the_connection(timer_mass: MusicAssistant) -> None:
    """A speaker whose power is cut is disconnected, reads unavailable and is not tried again."""
    speaker, client, published = _speaker(timer_mass)
    await _connect_player(speaker, client)
    listener = speaker._listen_task

    await speaker.on_power_cut()

    assert published == [(False, False)]
    client.disconnect.assert_awaited_once()
    # its listener ends without asking for a reconnect
    assert await _ended(listener)
    assert timer_mass._tracked_timers == {}
    # told once more, nothing changes
    await speaker.on_power_cut()
    assert published == [(False, False)] * 2
    assert timer_mass._tracked_timers == {}


@pytest.mark.parametrize("reconnect", ["waits", "runs"])
async def test_a_power_cut_ends_a_reconnect(timer_mass: MusicAssistant, reconnect: str) -> None:
    """
    A reconnect to a speaker whose power is cut is called off, whether it waits or runs.

    :param reconnect: Whether the reconnect still waits for its time, or tries to connect.
    """
    speaker, client, published = _speaker(timer_mass)
    connecting = asyncio.Event()

    async def _hangs() -> None:
        connecting.set()
        await asyncio.Event().wait()

    client.connect = AsyncMock(side_effect=_hangs)
    speaker.reconnect(0 if reconnect == "runs" else 30)
    running = None
    if reconnect == "runs":
        await connecting.wait()
        running = timer_mass._tracked_tasks[RECONNECT]

    await speaker.on_power_cut()
    await asyncio.sleep(0.01)

    assert RECONNECT not in timer_mass._tracked_timers
    assert RECONNECT not in timer_mass._tracked_tasks
    assert running is None or running.cancelled()
    assert connecting.is_set() is (reconnect == "runs")
    assert published == [(False, False)]


async def test_a_speaker_whose_power_is_back_is_connected_and_reads_available(
    timer_mass: MusicAssistant,
) -> None:
    """A speaker is connected to when it is told that its power is back, and reads available."""
    speaker, client, published = _speaker(timer_mass)
    await _connect_player(speaker, client)
    await speaker.on_power_cut()
    client.connect.reset_mock()
    published.clear()

    await speaker.on_power_restored()

    client.connect.assert_awaited_once()
    assert published[-1] == (True, True)
    update_attributes: Any = speaker.update_attributes
    update_attributes.assert_called_once_with()
    assert timer_mass._tracked_timers == {}
    await speaker.on_detach()


async def test_a_power_that_is_back_without_a_cut_before_it_replaces_the_connection(
    timer_mass: MusicAssistant,
) -> None:
    """
    A connection from before the power went is dropped, and the speaker connected to anew.

    A play within the first seconds of a power cut switches the power on again before the
    speaker was told of the cut: it still holds the connection to the speaker as it was.
    """
    speaker, client, published = _speaker(timer_mass)
    await _connect_player(speaker, client)
    old_listener = speaker._listen_task
    client.connect.reset_mock()

    await speaker.on_power_restored()

    client.disconnect.assert_awaited_once()
    assert await _ended(old_listener)
    client.connect.assert_awaited_once()
    new_listener = speaker._listen_task
    assert new_listener is not None
    assert new_listener is not old_listener
    assert not new_listener.done()
    assert published == [(False, False), (True, True)]
    assert timer_mass._tracked_timers == {}
    await speaker.on_detach()


async def test_a_power_that_is_back_twice_leaves_one_connection(
    timer_mass: MusicAssistant,
) -> None:
    """A speaker told twice that its power is back ends up with one connection."""
    speaker, client, published = _speaker(timer_mass)
    await _connect_player(speaker, client)
    await speaker.on_power_cut()

    await speaker.on_power_restored()
    first_listener = speaker._listen_task
    await speaker.on_power_restored()

    assert await _ended(first_listener)
    second_listener = speaker._listen_task
    assert second_listener is not None
    assert not second_listener.done()
    assert published[-1] == (True, True)
    assert timer_mass._tracked_timers == {}
    await speaker.on_detach()


@pytest.mark.parametrize("answer", ["the port does not answer", "the connect fails"])
async def test_a_speaker_that_does_not_answer_yet_is_tried_again_soon(
    timer_mass: MusicAssistant, answer: str
) -> None:
    """
    A speaker that is not up when its start-up time is over reads unavailable and is retried.

    :param answer: What the speaker does when it is connected to.
    """
    speaker, client, published = _speaker(timer_mass)
    if answer == "the port does not answer":
        speaker._is_reachable = AsyncMock(return_value=False)  # type: ignore[method-assign]
    else:
        client.connect = AsyncMock(side_effect=CannotConnect(OSError("handshake failed")))

    await speaker.on_power_restored()

    assert client.connect.await_count == (0 if answer == "the port does not answer" else 1)
    assert published == [(False, False)]
    retry = timer_mass._tracked_timers[RECONNECT]
    assert retry.when() - timer_mass.loop.time() == pytest.approx(5, abs=0.5)


async def test_a_speaker_whose_connect_is_ended_before_it_answers_is_tried_again_soon(
    timer_mass: MusicAssistant,
) -> None:
    """A connect that outlasts the time the players controller gives leaves a retry behind."""
    speaker, client, _ = _speaker(timer_mass)

    async def _hangs() -> None:
        await asyncio.Event().wait()

    client.connect = AsyncMock(side_effect=_hangs)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await speaker.on_power_restored()

    assert speaker.connected is False
    retry = timer_mass._tracked_timers[RECONNECT]
    assert retry.when() - timer_mass.loop.time() == pytest.approx(5, abs=0.5)


async def test_a_speaker_detached_while_it_is_probed_is_not_connected_to(
    timer_mass: MusicAssistant,
) -> None:
    """A speaker taken off its player while its port is probed gets no connection and no retry."""
    speaker, client, _ = _speaker(timer_mass)
    probing = asyncio.Event()
    answer = asyncio.Event()

    async def _is_reachable() -> bool:
        probing.set()
        await answer.wait()
        return True

    speaker._is_reachable = _is_reachable  # type: ignore[method-assign]
    restoring = asyncio.create_task(speaker.on_power_restored())
    await probing.wait()
    speaker._player = None
    await speaker.on_detach()
    answer.set()
    await restoring

    client.connect.assert_not_awaited()
    assert speaker.connected is False
    assert timer_mass._tracked_timers == {}


async def test_a_speaker_detached_while_it_connects_keeps_no_connection(
    timer_mass: MusicAssistant,
) -> None:
    """A connection made after the speaker was taken off its player is let go of again."""
    speaker, client, _ = _speaker(timer_mass)
    connecting = asyncio.Event()
    connect = asyncio.Event()

    async def _connects() -> None:
        connecting.set()
        await connect.wait()

    client.connect = AsyncMock(side_effect=_connects)
    restoring = asyncio.create_task(speaker.on_power_restored())
    await connecting.wait()
    speaker._player = None
    await speaker.on_detach()
    connect.set()
    await restoring

    assert speaker.connected is False
    assert await _ended(speaker._listen_task)
    assert timer_mass._tracked_timers == {}


async def _speaker_on_a_plug(
    scenario: Scenario, *, answers: bool
) -> tuple[SonosPlayer, MagicMock, FakePowerControl, str]:
    """
    Attach a connected speaker on a smart plug that cuts its power, and cut it.

    :param answers: Whether the API port of the speaker answers once its power is back.
    :return: The protocol of the speaker, its client, the plug and the id of its player.
    """
    protocol = await _attach_speaker(scenario)
    client = cast("MagicMock", protocol.client)
    protocol.upnp = MagicMock(
        follow=AsyncMock(), unfollow=AsyncMock(), follows=True, player_ip=client.player_ip
    )

    async def _start_listening(init_ready: asyncio.Event) -> None:
        init_ready.set()
        await asyncio.Event().wait()

    client.connect = AsyncMock()
    client.start_listening = _start_listening
    client.player.group.play_stream_url = AsyncMock()
    # what the speaker reports and its device settings are not at stake here, only whether
    # it is connected
    protocol.update_attributes = lambda: setattr(  # type: ignore[method-assign]
        protocol, "_attr_available", protocol.connected
    )
    protocol._update_options = AsyncMock()  # type: ignore[method-assign]
    protocol._is_reachable = AsyncMock(return_value=answers)  # type: ignore[method-assign]
    await protocol._connect()
    protocol.on_player_event(None)
    await scenario.settle()
    assert protocol.player is not None
    player_id = protocol.player.player_id
    plug = await scenario.add_power_control("switch.plug", "Plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    # this setting is offered once the plug is the power control of the player
    await scenario.configure_player(player_id, power_control_cuts_power=True)
    await scenario.switch(plug, False)
    await scenario.advance(EXTERNAL_POWER_OFF_STOP_DELAY)
    client.disconnect.assert_awaited_once()
    assert (protocol.connected, protocol.available) == (False, False)
    client.connect.reset_mock()
    return protocol, client, plug, player_id


async def test_a_start_after_the_startup_time_plays_through_the_speaker(
    scenario: Scenario,
) -> None:
    """
    A play of a speaker whose power was cut plays through Sonos once it is connected again.

    The plug of a Sonos speaker, which cuts its power, is off, and the connection to the
    speaker was let go of. A play switches the plug on and waits for the start-up time.
    Then the speaker is connected to anew, and the music is loaded through it.
    """
    protocol, client, plug, player_id = await _speaker_on_a_plug(scenario, answers=True)

    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
    assert plug.commands == [True]
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME - 1)
    client.connect.assert_not_awaited()
    assert not play.done(), play.exception()
    await scenario.advance(2)
    assert play.done()
    await play

    client.connect.assert_awaited_once()
    assert (protocol.connected, protocol.available) == (True, True)
    client.player.group.play_stream_url.assert_awaited_once()


async def test_a_start_fails_with_the_hint_when_the_speaker_does_not_answer(
    scenario: Scenario,
) -> None:
    """
    A play of a speaker that is not up after its start-up time fails with the hint.

    The plug of a Sonos speaker, which cuts its power, is off. A play switches it on and
    waits for the start-up time. The speaker does not answer by then: the play fails and
    tells the user to raise the start-up time, nothing was sent to the speaker, and it is
    tried again a few seconds later.
    """
    protocol, client, plug, player_id = await _speaker_on_a_plug(scenario, answers=False)

    play = await scenario.command(scenario.mass.players.play_media(player_id, MEDIA))
    await scenario.advance(DEFAULT_POWER_ON_STARTUP_TIME + 1)

    assert plug.commands == [True]
    with pytest.raises(PlayerUnavailableError, match="could not be reached 30s after"):
        await play
    client.connect.assert_not_awaited()
    client.player.group.play_stream_url.assert_not_awaited()
    assert (protocol.connected, protocol.available) == (False, False)
    # the retry it left behind connects once the speaker is up
    await scenario.advance(POWER_RESTORE_TIMEOUT)
    client.connect.assert_awaited_once()
    assert protocol.connected is True
