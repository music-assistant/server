"""Tests for attaching and detaching protocols: the guards, what is stored and the events."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import PropertyMock, patch

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_PLAYERS
from tests.scenarios.harness import Event, FakeProtocol, Scenario

if TYPE_CHECKING:
    from music_assistant.models.player import Player

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
# a device whose hardware address is not known yet
ADDRESS_ONLY = {IdentifierType.IP_ADDRESS: "192.168.1.90"}

# a changed feature set also comes with a config event, which is left out here
LIFECYCLE_EVENTS = (
    EventType.PLAYER_ADDED,
    EventType.PLAYER_UPDATED,
    EventType.PLAYER_REMOVED,
    EventType.QUEUE_ADDED,
)


def _stored(scenario: Scenario) -> tuple[list[str], list[str]]:
    """Return the ids of the stored player configs and of the stored protocol configs."""
    config = scenario.mass.config
    return sorted(config.get(CONF_PLAYERS, {})), sorted(config.get(CONF_PLAYER_PROTOCOLS, {}))


def _record_hooks(protocol: FakeProtocol, gate: asyncio.Event | None = None) -> list[str]:
    """
    Record the lifecycle hooks a protocol object gets, in order.

    :param protocol: The protocol object.
    :param gate: Lets on_attach wait until it is set.
    """
    calls: list[str] = []
    hooks = {
        name: getattr(protocol, name) for name in ("on_config_updated", "on_attach", "on_detach")
    }

    async def on_config_updated() -> None:
        calls.append("on_config_updated")
        await hooks["on_config_updated"]()

    async def on_attach(player: Player) -> None:
        calls.append("on_attach")
        if gate is not None:
            await gate.wait()
        await hooks["on_attach"](player)
        calls.append("on_attach done")

    async def on_detach() -> None:
        calls.append("on_detach")
        await hooks["on_detach"]()

    protocol.on_config_updated = on_config_updated  # type: ignore[method-assign]
    protocol.on_attach = on_attach  # type: ignore[method-assign]
    protocol.on_detach = on_detach  # type: ignore[method-assign]
    return calls


async def test_new_device_gets_a_player_with_an_id_of_its_own(scenario: Scenario) -> None:
    """A device that is new becomes a player whose id is not the id of its protocol."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    calls = _record_hooks(speaker)
    scenario.clear_events()

    await scenario.attach(speaker)

    player_id = scenario.single_player(speaker).player_id
    assert player_id.startswith("pl")
    assert player_id != "RINCON_KITCHEN"
    assert calls == ["on_config_updated", "on_attach", "on_attach done"]
    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN"])
    assert scenario.events(*LIFECYCLE_EVENTS) == [
        Event(EventType.PLAYER_ADDED, player_id),
        Event(EventType.QUEUE_ADDED, player_id),
    ]
    # neither id answers for the other kind
    players = scenario.mass.players
    assert players.get_player("RINCON_KITCHEN") is None
    assert players.get_protocol(player_id) is None
    assert players.get_player_by_protocol("RINCON_KITCHEN") is players.get_player(player_id)


async def test_attaching_the_attached_object_again_publishes_what_it_reports(
    scenario: Scenario,
) -> None:
    """A refresh re-reads the reported values, without setting the protocol up again."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=20)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    calls = _record_hooks(speaker)
    scenario.clear_events()

    speaker._set_reported(volume_level=35)
    await scenario.attach(speaker)

    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level == 35
    assert calls == []
    assert scenario.events(*LIFECYCLE_EVENTS) == [Event(EventType.PLAYER_UPDATED, player_id)]


async def test_another_object_of_a_protocol_takes_the_place_of_the_attached_one(
    scenario: Scenario,
) -> None:
    """A reconnect replaces the object: the old one is torn down, the player stays."""
    squeezelite = await scenario.add_provider("squeezelite")
    old = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
    await scenario.attach(old)
    player_id = scenario.single_player(old).player_id
    old_calls = _record_hooks(old)
    scenario.clear_events()

    new = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN, volume_level=35)
    new_calls = _record_hooks(new)
    await scenario.attach(new)

    assert old_calls == ["on_detach"]
    assert old.player is None
    assert new_calls == ["on_config_updated", "on_attach", "on_attach done"]
    assert scenario.player_of(new) == player_id
    assert scenario.events(*LIFECYCLE_EVENTS) == [Event(EventType.PLAYER_UPDATED, player_id)]
    # an update the replaced object still sends changes nothing
    old._set_reported(volume_level=99)
    old.update_state()
    await scenario.settle()
    state = scenario.player(player_id)
    assert state is not None
    assert state.volume_level != 99


async def test_detached_protocol_leaves_its_player_listed(scenario: Scenario) -> None:
    """A detach tears the protocol down and keeps the player, its config and its queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    calls = _record_hooks(speaker)
    scenario.clear_events()

    await scenario.detach(speaker)
    await scenario.detach(speaker)

    assert calls == ["on_detach"]
    state = scenario.player(player_id)
    assert state is not None
    assert state.available is False
    assert state.unavailable_since is not None
    assert scenario.queue(player_id) is not None
    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN"])
    assert scenario.events(*LIFECYCLE_EVENTS) == [Event(EventType.PLAYER_UPDATED, player_id)]
    # clients that only read the features of a player from its config are told as well
    assert scenario.events(EventType.PLAYER_CONFIG_UPDATED) == [
        Event(EventType.PLAYER_CONFIG_UPDATED, player_id)
    ]


async def test_detach_of_one_protocol_leaves_the_others_playing(scenario: Scenario) -> None:
    """Only the detach of the protocol that plays ends what the queue plays."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(native)
    await scenario.attach(audio)
    player_id = scenario.single_player(native, audio).player_id
    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)

    await scenario.detach(native)
    await scenario.idle()

    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING

    await scenario.detach(audio)
    await scenario.idle()

    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.IDLE


async def test_detach_while_a_new_device_attaches_stops_the_attach(scenario: Scenario) -> None:
    """A device that is lost while its attach waits for the provider leaves nothing behind."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    gate = asyncio.Event()
    calls = _record_hooks(speaker, gate)
    scenario.clear_events()
    players = scenario.mass.players

    task = await scenario.command(players.attach(speaker))
    assert calls == ["on_config_updated", "on_attach"]
    await players.detach("RINCON_KITCHEN")
    gate.set()
    await scenario.settle()
    await task
    await scenario.idle()

    # the hook that finished after the detach is followed by a teardown of its own
    assert calls == ["on_config_updated", "on_attach", "on_detach", "on_attach done", "on_detach"]
    assert players.get_protocol("RINCON_KITCHEN") is None
    assert speaker.player is None
    assert scenario.visible_players() == []
    assert list(players) == []
    assert _stored(scenario) == ([], [])
    assert scenario.events(*LIFECYCLE_EVENTS) == []


async def test_detach_while_the_address_of_a_device_is_resolved_stops_the_attach(
    scenario: Scenario,
) -> None:
    """A device that is lost before it was bound to a player is not attached afterwards."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    calls = _record_hooks(speaker)
    players = scenario.mass.players
    gate = asyncio.Event()
    resolve = players._resolve_mac_addresses

    async def _resolve_later(protocol: Any) -> None:
        await gate.wait()
        await resolve(protocol)

    players._resolve_mac_addresses = _resolve_later  # type: ignore[method-assign]
    task = await scenario.command(players.attach(speaker))
    await players.detach("RINCON_KITCHEN")
    gate.set()
    await scenario.settle()
    await task
    await scenario.idle()

    assert calls == []
    assert players.get_protocol("RINCON_KITCHEN") is None
    assert scenario.visible_players() == []
    assert _stored(scenario) == ([], [])


async def test_detach_of_another_protocol_does_not_stop_an_attach(scenario: Scenario) -> None:
    """An attach only stops for a detach of its own protocol."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(native)
    player_id = scenario.single_player(native).player_id
    gate = asyncio.Event()
    _record_hooks(audio, gate)

    task = await scenario.command(scenario.mass.players.attach(audio))
    await scenario.mass.players.detach("RINCON_KITCHEN")
    gate.set()
    await scenario.settle()
    await task

    assert scenario.player_of(audio) == player_id
    assert scenario.player_of(native) is None


async def test_failed_attach_of_a_new_device_leaves_nothing_behind(scenario: Scenario) -> None:
    """A device whose attach fails is no player, also not after a restart."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    calls = _record_hooks(speaker)

    async def _refuse(player: Player) -> None:  # noqa: ARG001
        raise RuntimeError("device refused")

    speaker.on_attach = _refuse  # type: ignore[method-assign]
    scenario.clear_events()

    with pytest.raises(RuntimeError, match="device refused"):
        await scenario.mass.players.attach(speaker)
    await scenario.settle()

    assert calls == ["on_config_updated", "on_detach"]
    assert scenario.mass.players.get_protocol("RINCON_KITCHEN") is None
    assert speaker.player is None
    assert _stored(scenario) == ([], [])
    assert scenario.events(*LIFECYCLE_EVENTS) == []

    scenario.set_present("RINCON_KITCHEN", False)
    await scenario.restart()

    assert scenario.visible_players() == []


async def test_failed_attach_to_a_player_that_exists_leaves_that_player_alone(
    scenario: Scenario,
) -> None:
    """A second protocol whose attach fails takes nothing of the player it belongs to."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(native)
    player_id = scenario.single_player(native).player_id

    async def _refuse(player: Player) -> None:  # noqa: ARG001
        raise RuntimeError("device refused")

    on_attach = audio.on_attach
    audio.on_attach = _refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="device refused"):
        await scenario.mass.players.attach(audio)
    await scenario.settle()

    assert scenario.player_of(native) == player_id
    assert scenario.mass.players.get_protocol("ap_kitchen") is None
    assert player_id in _stored(scenario)[0]
    # a later attach of the same protocol is not blocked by the failed one
    audio.on_attach = on_attach  # type: ignore[method-assign]
    await scenario.attach(audio)
    assert scenario.player_of(audio) == player_id


@pytest.mark.parametrize("stopping", ["provider", "server"])
async def test_nothing_attaches_while_its_provider_or_the_server_stops(
    scenario: Scenario, stopping: str
) -> None:
    """An attach that arrives during a teardown is ignored."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    if stopping == "provider":
        sonos.unloading = True
        await scenario.mass.players.attach(speaker)
        sonos.unloading = False
    else:
        closing = PropertyMock(return_value=True)
        with patch.object(type(scenario.mass), "closing", closing):
            await scenario.mass.players.attach(speaker)

    assert scenario.mass.players.get_protocol("RINCON_KITCHEN") is None
    assert _stored(scenario) == ([], [])


async def test_removed_protocol_takes_its_player_with_it_when_it_was_the_last(
    scenario: Scenario,
) -> None:
    """A provider that forgets a device deletes its config; the player goes with its last one."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(native)
    await scenario.attach(audio)
    player_id = scenario.single_player(native, audio).player_id
    scenario.clear_events()

    await scenario.mass.players.remove_protocol("ap_kitchen")
    await scenario.settle()

    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN"])
    state = scenario.player(player_id)
    assert state is not None
    assert [protocol.protocol_id for protocol in state.protocols] == ["RINCON_KITCHEN"]
    assert scenario.events(EventType.PLAYER_REMOVED) == []

    await scenario.mass.players.remove_protocol("RINCON_KITCHEN")
    await scenario.idle()

    assert _stored(scenario) == ([], [])
    assert scenario.visible_players() == []
    assert scenario.events(EventType.PLAYER_REMOVED) == [Event(EventType.PLAYER_REMOVED, player_id)]


async def test_removal_reaches_the_protocol_of_a_disabled_player(scenario: Scenario) -> None:
    """A provider is told about the removal of a device it holds without it being attached."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.disable(player_id)
    removed: list[str] = []
    on_remove = speaker.on_remove

    async def _on_remove() -> None:
        removed.append(speaker.protocol_id)
        await on_remove()

    speaker.on_remove = _on_remove  # type: ignore[method-assign]
    scenario.set_present("RINCON_KITCHEN", False)

    await scenario.remove(player_id)
    await scenario.idle()

    assert removed == ["RINCON_KITCHEN"]
    assert _stored(scenario) == ([], [])


async def test_disable_and_enable_announce_the_player_going_and_coming(scenario: Scenario) -> None:
    """A disabled player is removed for clients and keeps its config; enabling brings it back."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    calls = _record_hooks(speaker)
    scenario.clear_events()

    await scenario.disable(player_id)

    assert calls == ["on_detach"]
    assert scenario.visible_players() == []
    assert scenario.mass.players.wants_protocol("RINCON_KITCHEN") is False
    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN"])
    assert scenario.events(*LIFECYCLE_EVENTS, EventType.PLAYER_CONFIG_UPDATED) == [
        Event(EventType.PLAYER_CONFIG_UPDATED, player_id),
        Event(EventType.PLAYER_REMOVED, player_id),
    ]
    with pytest.raises(PlayerUnavailableError):
        await scenario.mass.players.cmd_stop(player_id)
    # the provider finds the device again, which attaches nothing for a disabled player
    await scenario.mass.players.attach(speaker)
    await scenario.settle()
    assert scenario.visible_players() == []

    scenario.clear_events()
    await scenario.enable(player_id)
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == player_id
    assert scenario.mass.players.wants_protocol("RINCON_KITCHEN") is True
    assert scenario.events(
        EventType.PLAYER_ADDED,
        EventType.PLAYER_REMOVED,
        EventType.PLAYER_CONFIG_UPDATED,
        EventType.QUEUE_ADDED,
    ) == [
        Event(EventType.PLAYER_ADDED, player_id),
        Event(EventType.QUEUE_ADDED, player_id),
        Event(EventType.PLAYER_CONFIG_UPDATED, player_id),
    ]


async def test_stored_player_is_announced_at_boot_before_its_provider_attaches(
    scenario: Scenario,
) -> None:
    """A restart brings a stored player back with its queue, unavailable until it attaches."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    scenario.set_present("RINCON_KITCHEN", False)
    scenario.clear_events()

    await scenario.restart()

    state = scenario.player(player_id)
    assert state is not None
    assert state.available is False
    assert state.provider == sonos.instance_id
    assert state.device_info.manufacturer == "Fake"
    assert state.setup_reason is None
    assert scenario.queue(player_id) is not None
    assert scenario.events(EventType.PLAYER_ADDED, EventType.QUEUE_ADDED) == [
        Event(EventType.PLAYER_ADDED, player_id),
        Event(EventType.QUEUE_ADDED, player_id),
    ]

    scenario.clear_events()
    await scenario.attach(scenario.protocol("RINCON_KITCHEN"))

    state = scenario.player(player_id)
    assert state is not None
    assert state.available is True
    assert scenario.events(*LIFECYCLE_EVENTS) == [Event(EventType.PLAYER_UPDATED, player_id)]


async def test_session_only_player_is_announced_as_removed_when_it_detaches(
    scenario: Scenario,
) -> None:
    """A player that only exists while connected goes with its protocol."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    scenario.clear_events()

    await scenario.attach(browser)
    await scenario.detach(browser)

    assert scenario.events(
        EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED, EventType.QUEUE_ADDED
    ) == [
        Event(EventType.PLAYER_ADDED, "sendspin_web_1"),
        Event(EventType.QUEUE_ADDED, "sendspin_web_1"),
        Event(EventType.PLAYER_REMOVED, "sendspin_web_1"),
    ]
    assert _stored(scenario) == ([], [])


async def test_sleep_timer_that_expires_on_a_player_without_protocol_is_dropped(
    scenario: Scenario,
) -> None:
    """A sleep timer of a player whose device is away ends without a command to it."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.play(player_id)
    scenario.mass.players.set_sleep_timer(player_id, 1)
    await scenario.detach(speaker)

    # the scenario fails on a warning the server logs
    await scenario.advance(70)
    await scenario.idle()

    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert player.sleep_timer_expires_at is None
