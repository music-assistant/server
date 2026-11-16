"""Tests for attaching and detaching protocols: the guards, what is stored and the events."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import PropertyMock, patch

import pytest
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import ActionUnavailable, PlayerUnavailableError

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_PLAYERS
from music_assistant.controllers.players.lifecycle import REMOVE_UNAVAILABLE_GRACE
from tests.common import as_user
from tests.scenarios.harness import (
    DEFAULT_FEATURES,
    Event,
    FakePlayerProvider,
    FakeProtocol,
    Scenario,
)

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

    await scenario.register(speaker)

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


async def test_technical_name_of_a_new_device_is_shown_but_never_stored(
    scenario: Scenario,
) -> None:
    """A device shows a technical name until it reports a real one, which it then keeps."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "54:78:C9:E6:0D:A0", role="audio", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    default_name_key = f"{CONF_PLAYERS}/{player_id}/default_name"

    assert scenario.single_player(speaker).name == "54:78:C9:E6:0D:A0"
    assert scenario.mass.config.get(default_name_key) is None

    await scenario.report(speaker, name="Kitchen")
    await scenario.report(speaker, name="54:78:C9:E6:0D:A0")

    assert scenario.single_player(speaker).name == "Kitchen"
    assert scenario.mass.config.get(default_name_key) == "Kitchen"


async def test_attaching_the_attached_object_again_publishes_what_it_reports(
    scenario: Scenario,
) -> None:
    """A refresh re-reads the reported values, without setting the protocol up again."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, volume_level=20)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    calls = _record_hooks(speaker)
    scenario.clear_events()

    speaker._set_reported(volume_level=35)
    await scenario.register(speaker)

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
    await scenario.register(old)
    player_id = scenario.single_player(old).player_id
    old_calls = _record_hooks(old)
    scenario.clear_events()

    new = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN, volume_level=35)
    new_calls = _record_hooks(new)
    await scenario.register(new)

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
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    calls = _record_hooks(speaker)
    scenario.clear_events()

    await scenario.unregister(speaker)
    await scenario.unregister(speaker)

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
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)

    await scenario.unregister(native)
    await scenario.idle()

    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING

    await scenario.unregister(audio)
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

    task = await scenario.command(players.register(speaker))
    assert calls == ["on_config_updated", "on_attach"]
    await players.unregister("RINCON_KITCHEN")
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


@pytest.mark.parametrize("step", ["power_cache", "player_config"])
@pytest.mark.parametrize("session_only", [True, False])
async def test_detach_while_a_new_player_is_published_settles_that_player(
    scenario: Scenario, step: str, session_only: bool
) -> None:
    """A device that is lost while its new player is announced leaves it as any detach does."""
    if session_only:
        sendspin = await scenario.add_provider("sendspin")
        protocol = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    else:
        sonos = await scenario.add_provider("sonos")
        protocol = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    players = scenario.mass.players
    reached = asyncio.Event()
    release = asyncio.Event()
    load_config = scenario.mass.config.get_player_object_config
    cache_get = scenario.mass.cache.get

    async def _load_config_later(player: Player) -> Any:
        reached.set()
        await release.wait()
        return await load_config(player)

    async def _cache_get_later(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("provider") == players.domain:
            reached.set()
            await release.wait()
        return await cache_get(*args, **kwargs)

    if step == "power_cache":
        delayed = patch.object(scenario.mass.cache, "get", _cache_get_later)
    else:
        delayed = patch.object(scenario.mass.config, "get_player_object_config", _load_config_later)
    scenario.clear_events()
    with delayed:
        task = await scenario.command(players.register(protocol))
        assert reached.is_set()
        await players.unregister(protocol.protocol_id)
        release.set()
        await scenario.settle()
        await task
    await scenario.idle()

    assert protocol.player is None
    if session_only:
        assert list(players) == []
        assert scenario.events(*LIFECYCLE_EVENTS) == [
            Event(EventType.PLAYER_ADDED, "sendspin_web_1"),
            Event(EventType.QUEUE_ADDED, "sendspin_web_1"),
            Event(EventType.PLAYER_REMOVED, "sendspin_web_1"),
        ]
        return
    # a device that was found stays known, unavailable until it is found again
    (visible,) = scenario.visible_players()
    state = scenario.player(visible.player_id)
    assert state is not None
    assert state.available is False
    assert [(info.protocol_id, info.available) for info in state.protocols] == [
        ("RINCON_KITCHEN", False)
    ]
    assert _stored(scenario) == ([visible.player_id], ["RINCON_KITCHEN"])


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
    task = await scenario.command(players.register(speaker))
    await players.unregister("RINCON_KITCHEN")
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
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    gate = asyncio.Event()
    _record_hooks(audio, gate)

    task = await scenario.command(scenario.mass.players.register(audio))
    await scenario.mass.players.unregister("RINCON_KITCHEN")
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
        await scenario.mass.players.register(speaker)
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
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id

    async def _refuse(player: Player) -> None:  # noqa: ARG001
        raise RuntimeError("device refused")

    on_attach = audio.on_attach
    audio.on_attach = _refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="device refused"):
        await scenario.mass.players.register(audio)
    await scenario.settle()

    assert scenario.player_of(native) == player_id
    assert scenario.mass.players.get_protocol("ap_kitchen") is None
    assert player_id in _stored(scenario)[0]
    # a later attach of the same protocol is not blocked by the failed one
    audio.on_attach = on_attach  # type: ignore[method-assign]
    await scenario.register(audio)
    assert scenario.player_of(audio) == player_id


@pytest.mark.parametrize("ends", ["failed", "interrupted"])
async def test_incomplete_first_attach_of_another_protocol_stores_nothing_of_it(
    scenario: Scenario, ends: str
) -> None:
    """A new protocol of a player that does not get attached is no output of that player."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    players = scenario.mass.players

    if ends == "failed":

        async def _refuse(player: Player) -> None:  # noqa: ARG001
            raise RuntimeError("device refused")

        audio.on_attach = _refuse  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="device refused"):
            await players.register(audio)
    else:
        gate = asyncio.Event()
        _record_hooks(audio, gate)
        task = await scenario.command(players.register(audio))
        await players.unregister("ap_kitchen")
        gate.set()
        await scenario.settle()
        await task

    state = scenario.player(player_id)
    assert state is not None
    assert [info.protocol_id for info in state.protocols] == ["RINCON_KITCHEN"]
    await scenario.idle()
    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN"])


async def test_failed_attach_of_a_protocol_that_returns_keeps_its_config(
    scenario: Scenario,
) -> None:
    """A protocol whose attach fails on its return keeps what was stored for it before."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    await scenario.unregister(audio)

    async def _refuse(player: Player) -> None:  # noqa: ARG001
        raise RuntimeError("device refused")

    audio.on_attach = _refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="device refused"):
        await scenario.mass.players.register(audio)
    await scenario.idle()

    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN", "ap_kitchen"])


@pytest.mark.parametrize("stopping", ["provider", "server"])
async def test_nothing_attaches_while_its_provider_or_the_server_stops(
    scenario: Scenario, stopping: str
) -> None:
    """An attach that arrives during a teardown is ignored."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    if stopping == "provider":
        sonos.unloading = True
        await scenario.mass.players.register(speaker)
        sonos.unloading = False
    else:
        closing = PropertyMock(return_value=True)
        with patch.object(type(scenario.mass), "closing", closing):
            await scenario.mass.players.register(speaker)

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
    await scenario.register(native)
    await scenario.register(audio)
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


async def test_removed_protocol_takes_the_protocols_that_ride_on_it_along(
    scenario: Scenario,
) -> None:
    """
    A protocol that rides on a removed one goes with it, and the player with its last one.

    A protocol that rides on another protocol of the same player stays.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    sendspin = await scenario.add_provider("sendspin")
    base_ap = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    base_cast = chromecast.protocol("cast_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(base_ap)
    await scenario.register(base_cast)
    player_id = scenario.single_player(base_ap, base_cast).player_id
    bridge = sendspin.protocol(
        "spb_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    await scenario.register(bridge)
    assert scenario.player_of(bridge) == player_id

    await scenario.mass.players.remove_protocol("cast_kitchen")
    await scenario.settle()

    assert _stored(scenario) == ([player_id], ["ap_kitchen", "spb_kitchen"])
    assert scenario.player_of(bridge) == player_id
    scenario.clear_events()

    await scenario.mass.players.remove_protocol("ap_kitchen")
    await scenario.idle()

    assert _stored(scenario) == ([], [])
    assert scenario.visible_players() == []
    assert scenario.events(EventType.PLAYER_REMOVED) == [Event(EventType.PLAYER_REMOVED, player_id)]


async def test_a_removed_provider_takes_the_protocols_that_ride_on_its_own_along(
    scenario: Scenario,
) -> None:
    """The player of a device goes with the provider of its only protocol, bridge and all."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    base = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(base)
    player_id = scenario.player_of(base)
    bridge = sendspin.protocol(
        "spb_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    await scenario.register(bridge)
    assert scenario.player_of(bridge) == player_id
    # the bridge goes when its provider unloads the protocol it rides on
    await scenario.unregister(bridge)

    await scenario.remove_provider(airplay)
    await scenario.idle()

    assert _stored(scenario) == ([], [])
    assert scenario.visible_players() == []


async def test_removal_reaches_the_protocol_of_a_disabled_player(scenario: Scenario) -> None:
    """A provider is told about the removal of a device it holds without it being attached."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
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


@pytest.mark.parametrize("away", ["detached", "switched_off"])
async def test_removal_reaches_a_protocol_its_provider_still_holds(
    scenario: Scenario, away: str
) -> None:
    """A provider is told about the removal of a device whose protocol is not attached."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    removed: list[str] = []

    async def _on_remove() -> None:
        removed.append(audio.protocol_id)

    audio.on_remove = _on_remove  # type: ignore[method-assign]
    if away == "detached":
        await scenario.unregister(audio)
    else:
        await scenario.mass.config.save_protocol_config("ap_kitchen", {"enabled": False})
        await scenario.settle()
    assert audio.player is None
    for protocol_id in ("RINCON_KITCHEN", "ap_kitchen"):
        scenario.set_present(protocol_id, False)

    await scenario.remove(player_id)
    await scenario.idle()

    assert removed == ["ap_kitchen"]


async def test_removal_does_not_reach_a_protocol_of_a_provider_that_unloaded(
    scenario: Scenario,
) -> None:
    """A protocol object of a provider that unloaded is not told about a removal."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    removed: list[str] = []

    async def _on_remove() -> None:
        removed.append(speaker.protocol_id)

    speaker.on_remove = _on_remove  # type: ignore[method-assign]
    await scenario.mass.unload_provider(sonos.instance_id)
    await scenario.settle()

    await scenario.remove(player_id)
    await scenario.idle()

    assert removed == []
    assert _stored(scenario) == ([], [])


async def test_removed_protocol_is_no_longer_named_by_its_player(scenario: Scenario) -> None:
    """A protocol that is removed leaves no output or control choice naming it."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id
    choices = ("preferred_output_protocol", "volume_control", "mute_control")
    await scenario.configure_player(player_id, **dict.fromkeys(choices, "ap_kitchen"))

    await scenario.remove_provider(airplay)

    config = scenario.mass.config
    assert [config.get_player_value(player_id, key) for key in choices] == [None, None, None]


async def test_player_names_the_provider_of_its_protocols_after_one_is_removed(
    scenario: Scenario,
) -> None:
    """The provider stored for a player follows when the protocol that named it is removed."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    await scenario.register(audio)
    player_id = scenario.single_player(native, audio).player_id

    await scenario.remove_provider(sonos)

    stored_provider = scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}/provider")
    assert stored_provider == airplay.instance_id


async def test_removed_player_leaves_the_player_filters_of_the_users(scenario: Scenario) -> None:
    """A player that is removed no longer stands in the list of players a user may use."""
    sonos = await scenario.add_provider("sonos")
    kitchen = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    den = sonos.protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1"}
    )
    await scenario.register(kitchen)
    await scenario.register(den)
    kitchen_id = scenario.single_player(kitchen).player_id
    den_id = scenario.single_player(den).player_id
    auth = scenario.mass.webserver.auth
    user = await auth.create_user(username="guest", player_filter=[kitchen_id, den_id])
    scenario.set_present("RINCON_KITCHEN", False)

    await scenario.remove(kitchen_id)
    await scenario.idle()

    stored_user = await auth.get_user(user.user_id)
    assert stored_user is not None
    assert stored_user.player_filter == [den_id]


async def test_group_that_is_removed_while_it_plays_stops_its_members(scenario: Scenario) -> None:
    """A player that plays is stopped before it goes: a group stops what its members play."""
    await scenario.mass.config._create_provider_instance("universal_group", {})
    sonos = await scenario.add_provider("sonos")
    member_a = sonos.protocol("RINCON_A", "Player A")
    member_b = sonos.protocol("RINCON_B", "Player B")
    await scenario.register(member_a)
    await scenario.register(member_b)
    member_ids = [scenario.single_player(member).player_id for member in (member_a, member_b)]
    group = await scenario.mass.players.create_group_player(
        "universal_group", "Group Z", member_ids, False
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    member_a.commands.clear()
    member_b.commands.clear()

    await scenario.remove(group.player_id)

    for member, member_id in zip((member_a, member_b), member_ids, strict=True):
        assert ("stop",) in member.commands
        state = scenario.player(member_id)
        assert state is not None
        assert state.playback_state == PlaybackState.IDLE


@pytest.mark.parametrize("power", [False, True])
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("group_type", ["universal_group", "sync_group"])
async def test_group_plays_on_when_one_of_its_members_is_removed(
    scenario: Scenario, group_type: str, dynamic: bool, power: bool
) -> None:
    """A member that is removed only leaves its group, also one that can not let it go."""
    if group_type == "universal_group":
        await scenario.mass.config._create_provider_instance(group_type, {})
    else:
        # a playing sync group of fake devices does not stop within the shutdown timeout
        scenario.expect_log("Failed to stop queue")
    sonos = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    if power:
        features.add(PlayerFeature.POWER)
    reported: dict[str, Any] = {"powered": True} if power else {}
    protocol_ids = ["RINCON_A", "RINCON_B", "RINCON_C"]
    members: list[FakeProtocol] = []
    for index, protocol_id in enumerate(protocol_ids):
        member = sonos.protocol(
            protocol_id,
            f"Player {protocol_id[-1]}",
            identifiers={IdentifierType.MAC_ADDRESS: f"54:78:C9:E6:0D:A{index}"},
            supported_features=features,
            can_group_with=set(protocol_ids) - {protocol_id},
            **reported,
        )
        await scenario.register(member)
        members.append(member)
    # the members learn who they can be grouped with
    await scenario.advance(3)
    member_ids = [scenario.single_player(member).player_id for member in members]
    group = await scenario.mass.players.create_group_player(
        group_type, "Group Z", member_ids, dynamic
    )
    await scenario.settle()
    await scenario.play(group.player_id)
    await scenario.advance(2)
    for member in members:
        member.commands.clear()
    scenario.set_present("RINCON_C", False)

    await scenario.remove(member_ids[2])
    await scenario.advance(6)

    group_state = scenario.player(group.player_id)
    assert group_state is not None
    assert group_state.playback_state == PlaybackState.PLAYING
    assert group_state.group_members == member_ids[:2]
    queue = scenario.queue(group.player_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING
    assert scenario.mass.players.get_player(member_ids[2]) is None
    for member in members[:2]:
        assert ("stop",) not in member.commands
    assert ("power", False) not in members[2].commands
    if group_type == "universal_group" and dynamic:
        # the group stops what the member it lets go still plays of it
        assert members[2].commands == [("stop",)]
    else:
        assert ("stop",) not in members[2].commands


async def test_sync_member_that_is_removed_only_leaves_its_leader(scenario: Scenario) -> None:
    """The leader of a sync member that is removed plays on, without that member."""
    sonos = await scenario.add_provider("sonos")
    features = {*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS}
    leader = sonos.protocol(
        "RINCON_KITCHEN",
        "Kitchen",
        identifiers=KITCHEN,
        supported_features=features,
        can_group_with={"RINCON_HALL"},
    )
    member = sonos.protocol(
        "RINCON_HALL",
        "Hall",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0"},
        supported_features=features,
        can_group_with={"RINCON_KITCHEN"},
    )
    await scenario.register(leader)
    await scenario.register(member)
    leader_id = scenario.single_player(leader).player_id
    member_id = scenario.single_player(member).player_id
    await scenario.play(leader_id)
    await scenario.mass.players.cmd_set_members(leader_id, player_ids_to_add=[member_id])
    await scenario.idle()
    member_state = scenario.player(member_id)
    assert member_state is not None
    assert member_state.synced_to == leader_id
    leader.commands.clear()
    member.commands.clear()
    scenario.set_present("RINCON_HALL", False)

    await scenario.remove(member_id)
    await scenario.idle()

    assert leader.commands == [("set_members", None, ["RINCON_HALL"])]
    assert member.commands == []
    leader_state = scenario.player(leader_id)
    assert leader_state is not None
    assert leader_state.playback_state == PlaybackState.PLAYING
    assert leader_state.group_members == []
    queue = scenario.queue(leader_id)
    assert queue is not None
    assert queue.state == PlaybackState.PLAYING


async def test_removal_stops_a_player_also_when_its_user_can_not_control_it(
    scenario: Scenario,
) -> None:
    """The stop before a removal is the server's own: it is no command of the user who removes."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.play(player_id)
    speaker.commands.clear()
    scenario.set_present("RINCON_KITCHEN", False)

    # an administrator of the players whose own use is limited to another player
    with as_user(["pl000000000000"]):
        await scenario.remove(player_id)

    assert speaker.commands == [("stop",)]
    assert scenario.mass.players.get_player(player_id) is None


@pytest.mark.parametrize("playing", [False, True])
async def test_removed_player_is_stopped_but_not_powered_off(
    scenario: Scenario, playing: bool
) -> None:
    """A removal ends what the player plays, and leaves the device on as it is."""
    # a source of the device itself, which it plays without the server
    reported: dict[str, Any] = (
        {"playback_state": PlaybackState.PLAYING, "active_source": "hdmi1"} if playing else {}
    )
    musiccast = await scenario.add_provider("musiccast")
    receiver = musiccast.protocol(
        "avr_1",
        "AVR",
        identifiers=KITCHEN,
        powered=True,
        supported_features={*DEFAULT_FEATURES, PlayerFeature.POWER},
        **reported,
    )
    await scenario.register(receiver)
    player_id = scenario.single_player(receiver).player_id
    receiver.commands.clear()

    await scenario.remove(player_id)

    assert receiver.commands == ([("stop",)] if playing else [])


@pytest.mark.parametrize("left", ["unreachable", "idle_output"])
async def test_removed_player_without_playback_to_end_gets_no_command(
    scenario: Scenario, left: str
) -> None:
    """A player that plays nothing it can stop is removed without a command or an error."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(native)
    protocols = [native]
    if left == "idle_output":
        await scenario.register(audio)
        protocols.append(audio)
    player_id = scenario.single_player(*protocols).player_id
    if left == "idle_output":
        await scenario.configure_player(player_id, preferred_output_protocol="ap_kitchen")
    await scenario.play(player_id)
    if left == "unreachable":
        # the device drops while it plays, its last state stays
        await scenario.report(native, available=False)
    else:
        # the stream ended on the output, which is still marked as the active one
        await scenario.report(audio, playback_state=PlaybackState.IDLE, current_media=None)
    for protocol in protocols:
        protocol.commands.clear()
    scenario.set_present("RINCON_KITCHEN", False)
    scenario.set_present("ap_kitchen", False)

    await scenario.remove(player_id)

    assert [protocol.commands for protocol in protocols] == [[] for _ in protocols]
    assert _stored(scenario) == ([], [])


async def test_removed_player_leaves_its_power_control_switched_on(scenario: Scenario) -> None:
    """A player on a smart plug is removed with the plug on, so its device is found again."""
    squeezelite = await scenario.add_provider("squeezelite")
    speaker = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    plug = await scenario.add_power_control("plug_1", "Plug", powered=True)
    await scenario.configure_player(player_id, power_control=plug.control_id)
    await scenario.play(player_id)
    plug.commands.clear()

    await scenario.remove(player_id)
    await scenario.idle()

    assert plug.commands == []
    assert plug.powered is True
    assert ("stop",) in speaker.commands
    found_again = scenario.single_player(scenario.protocol("aa:bb:cc:00:00:01"))
    assert found_again.player_id != player_id
    assert found_again.available is True


@pytest.mark.parametrize("failing", ["stop", "power"])
async def test_removal_goes_through_when_the_device_fails_a_command(
    scenario: Scenario, failing: str
) -> None:
    """A device that does not answer is removed all the same."""
    musiccast = await scenario.add_provider("musiccast")
    receiver = musiccast.protocol(
        "avr_1",
        "AVR",
        identifiers=KITCHEN,
        powered=True,
        supported_features={*DEFAULT_FEATURES, PlayerFeature.POWER},
    )
    await scenario.register(receiver)
    player_id = scenario.single_player(receiver).player_id
    await scenario.play(player_id)

    async def _fail(*_args: Any) -> None:
        raise ConnectionError("device does not answer")

    setattr(receiver, failing, _fail)
    scenario.set_present("avr_1", False)
    if failing == "stop":
        scenario.expect_log("before its removal")

    await scenario.remove(player_id)

    assert scenario.mass.players.get_player(player_id) is None
    assert _stored(scenario) == ([], [])


async def test_protocol_that_waits_takes_no_place_on_a_player_that_is_removed(
    scenario: Scenario,
) -> None:
    """A protocol that waits for a place on a player is not bound to it while it is removed."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    carrier = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    client = sendspin.protocol("sendspin_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(carrier)
    await scenario.register(client)
    player_id = scenario.single_player(carrier, client).player_id
    # the place of its domain is taken by the client of the device itself
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    await scenario.register(bridge)
    assert scenario.player_of(bridge) is None
    for protocol_id in ("ap_kitchen", "sendspin_kitchen", "sendspin_bridge_kitchen"):
        scenario.set_present(protocol_id, False)

    await scenario.remove(player_id)
    await scenario.idle()

    assert bridge.player is None
    assert scenario.mass.players.get_player(player_id) is None
    assert _stored(scenario) == ([], [])


async def test_derived_protocol_that_connects_while_its_player_is_removed_waits(
    scenario: Scenario,
) -> None:
    """A bridge that connects again while the player of its carrier is removed gets no place."""
    chromecast = await scenario.add_provider("chromecast")
    sendspin = await scenario.add_provider("sendspin")
    carrier = chromecast.protocol("cast_kitchen", "Kitchen", identifiers=KITCHEN)
    await scenario.register(carrier)
    player_id = scenario.single_player(carrier).player_id
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="cast_kitchen"
    )
    await scenario.register(bridge)
    assert scenario.player_of(bridge) == player_id
    await scenario.unregister(bridge)
    for protocol_id in ("cast_kitchen", "sendspin_bridge_kitchen"):
        scenario.set_present(protocol_id, False)
    players = scenario.mass.players
    dropping = asyncio.Event()
    drop_player = players._drop_player

    async def _drop_slowly(player_id: str, player: Player | None) -> None:
        # releasing what the player played takes a while
        await dropping.wait()
        await drop_player(player_id, player)

    with patch.object(players, "_drop_player", _drop_slowly):
        removal = await scenario.command(players.remove(player_id))
        # the receiver app on the speaker connects again
        await players.register(bridge)
        dropping.set()
        await scenario.settle()
        await removal
    await scenario.idle()

    assert bridge.player is None
    assert players.get_player(player_id) is None
    assert _stored(scenario) == ([], [])


async def test_device_found_again_while_its_player_is_removed_becomes_a_new_player(
    scenario: Scenario,
) -> None:
    """A device that reconnects while its player is removed ends up as a player of its own."""
    squeezelite = await scenario.add_provider("squeezelite")
    old = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
    await scenario.register(old)
    player_id = scenario.single_player(old).player_id
    players = scenario.mass.players
    removed: list[str] = []
    closing = asyncio.Event()

    async def _close_slowly() -> None:
        await closing.wait()

    async def _on_remove() -> None:
        removed.append(old.protocol_id)

    old.on_detach = _close_slowly  # type: ignore[method-assign]
    old.on_remove = _on_remove  # type: ignore[method-assign]
    removal = await scenario.command(players.remove(player_id))
    new = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
    await players.register(new)
    closing.set()
    await scenario.settle()
    await removal
    await scenario.idle()

    assert new.player is not None
    new_player_id = new.player.player_id
    assert new_player_id != player_id
    assert players.get_player(player_id) is None
    assert [player.player_id for player in scenario.visible_players()] == [new_player_id]
    assert _stored(scenario) == ([new_player_id], ["aa:bb:cc:00:00:01"])
    # its provider still needs the device it found again
    assert removed == []


async def test_remove_unavailable_removes_the_players_whose_devices_are_gone(
    scenario: Scenario,
) -> None:
    """Only a player that no running provider finds a device of is removed, and returned."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    present = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    gone = sonos.protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1"}
    )
    unloaded = airplay.protocol(
        "ap_office",
        "Office",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C1"},
    )
    for protocol in (present, gone, unloaded):
        await scenario.register(protocol)
    present_id, gone_id, unloaded_id = (
        scenario.single_player(protocol).player_id for protocol in (present, gone, unloaded)
    )
    # a player with nothing but a config of its own, as the migration keeps a customised one
    scenario.mass.config.create_player_record(
        "up0a1b2c3d", provider="", player_type=PlayerType.PLAYER, default_name="Kitchen"
    )
    scenario.mass.config.set_player_value("up0a1b2c3d", "name", "Kitchen radio")
    scenario.set_present("RINCON_DEN", False)
    scenario.set_present("ap_office", False)
    await scenario.restart()
    await scenario.mass.unload_provider(scenario.provider("airplay").instance_id)
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)

    removed = await scenario.mass.players.remove_unavailable()
    await scenario.idle()

    assert sorted(removed) == sorted([gone_id, "up0a1b2c3d"])
    assert sorted(_stored(scenario)[0]) == sorted([present_id, unloaded_id])


async def test_remove_unavailable_keeps_a_player_whose_device_needs_setup(
    scenario: Scenario,
) -> None:
    """A player whose device is reachable is no leftover, also while it awaits its setup."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_den", "Den", role="audio", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.report(speaker, needs_setup=True, setup_reason="pairing_required")
    state = scenario.player(player_id)
    assert state is not None
    assert state.available is False
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)

    assert await scenario.mass.players.remove_unavailable() == []
    assert scenario.player_of(speaker) == player_id


@pytest.mark.parametrize("after", ["restart", "provider_reload"])
async def test_remove_unavailable_is_refused_while_the_providers_find_their_devices(
    scenario: Scenario, after: str
) -> None:
    """Right after a start or a provider load nothing is removed: devices are still found."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    scenario.set_present("RINCON_KITCHEN", False)
    if after == "restart":
        await scenario.restart()
    else:
        await scenario.unregister(speaker)
        await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
        await scenario.reload_provider(sonos)

    with pytest.raises(ActionUnavailable):
        await scenario.mass.players.remove_unavailable()
    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN"])

    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    assert await scenario.mass.players.remove_unavailable() == [player_id]


async def test_remove_unavailable_waits_only_for_the_players_of_a_provider_that_just_loaded(
    scenario: Scenario,
) -> None:
    """
    A provider that was loaded shortly ago protects its own players, not those of the others.

    A player without protocols, which any provider may find again, waits for all of them.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    gone = sonos.protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1"}
    )
    reloading = airplay.protocol(
        "ap_office",
        "Office",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C1"},
    )
    await scenario.register(gone)
    await scenario.register(reloading)
    gone_id = scenario.single_player(gone).player_id
    reloading_id = scenario.single_player(reloading).player_id
    # a player with nothing but a config of its own, as the migration keeps a customised one
    scenario.mass.config.create_player_record(
        "up0a1b2c3d", provider="", player_type=PlayerType.PLAYER, default_name="Kitchen"
    )
    scenario.mass.config.set_player_value("up0a1b2c3d", "name", "Kitchen radio")
    scenario.set_present("RINCON_DEN", False)
    scenario.set_present("ap_office", False)
    await scenario.restart()
    airplay = scenario.provider("airplay")
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    # the provider keeps reloading
    for _ in range(3):
        await scenario.advance(45)
        airplay = await scenario.reload_provider(airplay)

    assert await scenario.mass.players.remove_unavailable() == [gone_id]
    with pytest.raises(ActionUnavailable):
        await scenario.mass.players.remove_unavailable()
    assert sorted(_stored(scenario)[0]) == sorted([reloading_id, "up0a1b2c3d"])

    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    assert sorted(await scenario.mass.players.remove_unavailable()) == sorted(
        [reloading_id, "up0a1b2c3d"]
    )


async def test_dry_run_of_remove_unavailable_names_what_the_command_removes(
    scenario: Scenario,
) -> None:
    """
    The dry run applies the rules of the command and changes nothing, its refusal included.

    Kept are a reachable player that needs setup and the player of a provider that is not
    loaded; a player of a provider that was just loaded and one without protocols wait.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    dlna = await scenario.add_provider("dlna")
    gone = sonos.protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1"}
    )
    in_setup = sonos.protocol(
        "RINCON_HALL", "Hall", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B2"}
    )
    reloading = airplay.protocol(
        "ap_office",
        "Office",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C1"},
    )
    unloaded = dlna.protocol(
        "uuid:study",
        "Study",
        role="audio",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:D1"},
    )
    for protocol in (gone, in_setup, reloading, unloaded):
        await scenario.register(protocol)
    await scenario.report(in_setup, needs_setup=True, setup_reason="pairing_required")
    gone_id, reloading_id = (
        scenario.single_player(protocol).player_id for protocol in (gone, reloading)
    )
    # a player with nothing but a config of its own, as the migration keeps a customised one
    scenario.mass.config.create_player_record(
        "up0a1b2c3d", provider="", player_type=PlayerType.PLAYER, default_name="Kitchen"
    )
    scenario.mass.config.set_player_value("up0a1b2c3d", "name", "Kitchen radio")
    for protocol_id in ("RINCON_DEN", "ap_office", "uuid:study"):
        scenario.set_present(protocol_id, False)
    await scenario.restart()
    await scenario.mass.unload_provider(scenario.provider("dlna").instance_id)
    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    await scenario.reload_provider(scenario.provider("airplay"))
    players = scenario.mass.players

    async def _dry_run() -> list[str]:
        stored = (
            deepcopy(scenario.mass.config.get(CONF_PLAYERS)),
            deepcopy(scenario.mass.config.get(CONF_PLAYER_PROTOCOLS)),
        )
        live = sorted(players._players)
        scenario.clear_events()
        would_remove = await players.remove_unavailable(dry_run=True)
        assert scenario.events(*LIFECYCLE_EVENTS) == []
        assert (
            deepcopy(scenario.mass.config.get(CONF_PLAYERS)),
            deepcopy(scenario.mass.config.get(CONF_PLAYER_PROTOCOLS)),
        ) == stored
        assert sorted(players._players) == live
        return would_remove

    assert await _dry_run() == [gone_id]
    assert await players.remove_unavailable() == [gone_id]
    await scenario.settle()

    with pytest.raises(ActionUnavailable):
        await players.remove_unavailable(dry_run=True)
    with pytest.raises(ActionUnavailable):
        await players.remove_unavailable()

    await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
    would_remove = sorted(await _dry_run())
    assert would_remove == sorted([reloading_id, "up0a1b2c3d"])
    assert sorted(await players.remove_unavailable()) == would_remove


async def test_discovery_after_a_removal_is_dropped_when_its_provider_unloads(
    scenario: Scenario,
) -> None:
    """The discovery a removal asks for never runs against a provider that unloaded meanwhile."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    task_id = f"discover_players_{sonos.instance_id}"

    await scenario.remove(player_id)

    assert task_id in scenario.mass._tracked_timers
    await scenario.mass.unload_provider(sonos.instance_id)
    await scenario.settle()

    assert task_id not in scenario.mass._tracked_timers


async def test_a_provider_unloaded_while_it_finishes_loading_leaves_nothing_of_its_devices(
    scenario: Scenario,
) -> None:
    """
    A provider that is unloaded before its load is done never looks for its devices.

    The device it registered before the unload, which still waited for its player, is let
    go: no protocol of the provider is left, nothing is pending and nothing is stored.
    """
    airplay = await scenario.add_provider("airplay")
    instance_id = airplay.instance_id
    airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=ADDRESS_ONLY)
    scenario.set_present("ap_kitchen", True)
    stored = _stored(scenario)
    mass = scenario.mass
    update_cache = mass._update_available_providers_cache
    # the provider instance that was loading, its device, and whether the controller held
    # that device until it knows its player
    loading: list[tuple[FakePlayerProvider, FakeProtocol, bool]] = []

    async def _unload_while_loading() -> None:
        await update_cache()
        provider = mass.get_provider(instance_id)
        if loading or not isinstance(provider, FakePlayerProvider) or provider is airplay:
            return
        # the new instance is registered and runs its finish-loading hooks: it finds its
        # device (as a listener it started before does), and is unloaded right after
        device = provider._protocol_for("ap_kitchen")
        await mass.players.register(device)
        held = (
            mass.players.get_protocol("ap_kitchen") is device
            and device.player is None
            and "ap_kitchen" in mass.players._pending_protocol_evaluations
        )
        loading.append((provider, device, held))
        await mass.unload_provider(instance_id)

    with patch.object(mass, "_update_available_providers_cache", _unload_while_loading):
        await mass.load_provider(instance_id)
        await scenario.settle()

    assert len(loading) == 1
    provider, device, held = loading[0]
    assert held
    assert mass.get_provider(instance_id) is None
    assert not provider.initialized.is_set()

    await scenario.advance(30)

    assert device.player is None
    assert list(mass.players.iter_protocols(instance_id)) == []
    assert not mass.players._pending_protocol_evaluations
    assert f"discover_players_{instance_id}" not in mass._tracked_timers
    assert scenario.visible_players() == []
    assert _stored(scenario) == stored


async def test_disable_and_enable_announce_the_player_going_and_coming(scenario: Scenario) -> None:
    """
    A disabled player leaves the players, unavailable; enabling it brings it back.

    It keeps its config. Every protocol of the player goes with it, its audio protocols as well as its own.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.register(speaker)
    await scenario.register(audio)
    player_id = scenario.single_player(speaker, audio).player_id
    calls = _record_hooks(speaker)
    audio_calls = _record_hooks(audio)
    scenario.clear_events()

    await scenario.disable(player_id)

    assert calls == ["on_detach"]
    assert audio_calls == ["on_detach"]
    assert speaker.player is None
    assert audio.player is None
    assert scenario.visible_players() == []
    assert scenario.mass.players.wants_protocol("RINCON_KITCHEN") is False
    assert scenario.mass.players.wants_protocol("ap_kitchen") is False
    assert _stored(scenario) == ([player_id], ["RINCON_KITCHEN", "ap_kitchen"])
    assert scenario.events(*LIFECYCLE_EVENTS, EventType.PLAYER_CONFIG_UPDATED) == [
        Event(EventType.PLAYER_UPDATED, player_id),
        Event(EventType.PLAYER_CONFIG_UPDATED, player_id),
    ]
    assert scenario.availability_updates(player_id) == [False]
    with pytest.raises(PlayerUnavailableError):
        await scenario.mass.players.cmd_stop(player_id)
    # the providers find the device again, which attaches nothing for a disabled player
    await scenario.mass.players.register(speaker)
    await scenario.mass.players.register(audio)
    await scenario.settle()
    assert scenario.visible_players() == []
    assert audio.player is None

    scenario.clear_events()
    await scenario.enable(player_id)
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == player_id
    assert scenario.player_of(scenario.protocol("ap_kitchen")) == player_id
    assert scenario.mass.players.wants_protocol("RINCON_KITCHEN") is True
    assert scenario.mass.players.wants_protocol("ap_kitchen") is True
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
    await scenario.register(speaker)
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
    await scenario.register(scenario.protocol("RINCON_KITCHEN"))

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

    await scenario.register(browser)
    await scenario.unregister(browser)

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
    await scenario.register(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.play(player_id)
    scenario.mass.players.set_sleep_timer(player_id, 1)
    await scenario.unregister(speaker)

    # the scenario fails on a warning the server logs
    await scenario.advance(70)
    await scenario.idle()

    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert player.sleep_timer_expires_at is None
