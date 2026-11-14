"""Tests for the players controller around attach, detach and removal: races, failures, cleanup."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    IdentifierType,
    PlayerType,
)

from music_assistant.constants import CONF_GROUP_MEMBERS, CONF_PLAYER_PROTOCOLS, CONF_PLAYERS
from music_assistant.controllers.players.constants import PlayerLockPurpose
from tests.scenarios.harness import FAKE_SETTING, Event, FakeProtocol, Scenario

if TYPE_CHECKING:
    from music_assistant.models.player import Player

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
LIVING = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B0",
    IdentifierType.IP_ADDRESS: "192.168.1.21",
}
# the key of the allow-list of the group providers
CONF_ALLOWED_MEMBERS = "allowed_members"
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


def _record_hooks(protocol: FakeProtocol) -> list[str]:
    """
    Record the lifecycle hooks a protocol object gets, in order.

    :param protocol: The protocol object.
    """
    calls: list[str] = []
    for name in ("on_config_updated", "on_attach", "on_detach"):
        hook = getattr(protocol, name)

        async def _record(*args: Any, _name: str = name, _hook: Any = hook) -> None:
            calls.append(_name)
            await _hook(*args)

        setattr(protocol, name, _record)
    return calls


def _locks_of(scenario: Scenario, player_id: str) -> list[str]:
    """Return the keys of the command locks the controller holds for a player."""
    return sorted(
        key
        for key in scenario.mass.players._player_command_locks
        if key in {f"{purpose.value}_{player_id}" for purpose in PlayerLockPurpose}
    )


async def _take_locks(scenario: Scenario, player_id: str) -> None:
    """Let commands take (and release) the playback and the volume lock of a player."""
    players = scenario.mass.players
    for purpose in (PlayerLockPurpose.PLAYBACK, PlayerLockPurpose.VOLUME):
        async with players.get_player_lock(player_id, purpose):
            pass


async def _kitchen_player(scenario: Scenario) -> tuple[FakeProtocol, str]:
    """Return the attached protocol of a speaker and the id of its player."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(speaker)
    return speaker, scenario.single_player(speaker).player_id


async def _group_listing(scenario: Scenario, *member_ids: str) -> FakeProtocol:
    """
    Return the attached protocol of a group whose stored member list holds the given players.

    The group declares its member list (and allow-list) as config entries, as a group
    provider does, so its live config copy holds them as well.

    :param member_ids: The ids of the players the group lists, in order.
    """
    snapcast = await scenario.add_provider("snapcast")
    group = snapcast.protocol("group_all", "All rooms", role="virtual", type=PlayerType.GROUP)
    entries = await group.get_config_entries()

    async def _get_config_entries() -> list[ConfigEntry]:
        return [
            *entries,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Members",
                multi_value=True,
                default_value=[],
                required=False,
            ),
            ConfigEntry(
                key=CONF_ALLOWED_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Allowed members",
                multi_value=True,
                default_value=[],
                required=False,
            ),
        ]

    group.get_config_entries = _get_config_entries  # type: ignore[method-assign]
    await scenario.register(group)
    config = scenario.mass.config
    config.set_protocol_value("group_all", CONF_GROUP_MEMBERS, list(member_ids))
    config.set_protocol_value("group_all", CONF_ALLOWED_MEMBERS, list(member_ids))
    return group


class TestCleanupWhenAPlayerGoes:
    """What the controller keeps for a player goes with the player, not with a detach."""

    async def test_command_locks_stay_over_a_detach_and_go_with_the_player(
        self, scenario: Scenario
    ) -> None:
        """A detached player keeps its command locks; a removed one takes them along."""
        speaker, player_id = await _kitchen_player(scenario)
        await _take_locks(scenario, player_id)
        locks = _locks_of(scenario, player_id)
        assert locks == sorted([f"playback_{player_id}", f"volume_{player_id}"])

        await scenario.unregister(speaker)

        assert _locks_of(scenario, player_id) == locks

        await scenario.remove(player_id)

        assert _locks_of(scenario, player_id) == []

    async def test_removal_leaves_the_locks_of_another_player(self, scenario: Scenario) -> None:
        """Removing one player keeps the command locks of every other player."""
        speaker, player_id = await _kitchen_player(scenario)
        sonos = scenario.provider("sonos")
        other = sonos.protocol("RINCON_LIVING", "Living", identifiers=LIVING)
        await scenario.register(other)
        other_id = scenario.single_player(other).player_id
        for one in (player_id, other_id):
            await _take_locks(scenario, one)
        scenario.set_present(speaker.protocol_id, False)

        await scenario.remove(player_id)

        assert _locks_of(scenario, player_id) == []
        assert _locks_of(scenario, other_id) == sorted(
            [f"playback_{other_id}", f"volume_{other_id}"]
        )

    async def test_removal_does_not_take_the_locks_of_a_player_whose_id_ends_alike(
        self, scenario: Scenario
    ) -> None:
        """Removing player ``b`` keeps the locks of player ``a_b`` (no suffix matching)."""
        sendspin = await scenario.add_provider("sendspin")
        # a player that only exists while connected takes the id of its protocol
        for protocol_id in ("b", "a_b"):
            await scenario.register(sendspin.protocol(protocol_id, protocol_id, session_only=True))
            await _take_locks(scenario, protocol_id)
        assert scenario.mass.players.get_player("a_b") is not None

        await scenario.remove("b")

        assert _locks_of(scenario, "b") == []
        assert _locks_of(scenario, "a_b") == ["playback_a_b", "volume_a_b"]

    async def test_detach_or_removal_of_what_is_unknown_changes_nothing(
        self, scenario: Scenario
    ) -> None:
        """A detach of an unknown protocol and the removal of an unknown player are ignored."""
        speaker, player_id = await _kitchen_player(scenario)
        await _take_locks(scenario, player_id)
        stored = _stored(scenario)
        scenario.clear_events()

        await scenario.mass.players.unregister("nonexistent")
        await scenario.remove("nonexistent")
        await scenario.idle()

        assert scenario.player_of(speaker) == player_id
        assert _locks_of(scenario, player_id) == sorted(
            [f"playback_{player_id}", f"volume_{player_id}"]
        )
        assert _stored(scenario) == stored
        assert scenario.events(*LIFECYCLE_EVENTS) == []

    async def test_update_scheduled_before_the_removal_publishes_nothing(
        self, scenario: Scenario
    ) -> None:
        """A debounced update of a player that was removed meanwhile is not published."""
        speaker, player_id = await _kitchen_player(scenario)
        scenario.set_present(speaker.protocol_id, False)
        speaker._set_reported(volume_level=35)
        scenario.mass.players.trigger_player_update(player_id)

        await scenario.remove(player_id)
        scenario.clear_events()
        await scenario.idle()

        assert scenario.events(*LIFECYCLE_EVENTS) == []
        assert scenario.visible_players() == []

    async def test_sleep_timer_stays_over_a_detach_and_goes_with_the_player(
        self, scenario: Scenario
    ) -> None:
        """A sleep timer belongs to the player: a removal cancels and clears it."""
        speaker, player_id = await _kitchen_player(scenario)
        await scenario.play(player_id)
        players = scenario.mass.players
        expires_at = players.set_sleep_timer(player_id, 600)
        timer_id = f"player_sleep_timer_{player_id}"

        await scenario.unregister(speaker)

        player = players.get_player(player_id)
        assert player is not None
        assert player.sleep_timer_expires_at == expires_at
        assert timer_id in scenario.mass._tracked_timers

        await scenario.remove(player_id)

        assert player.sleep_timer_expires_at is None
        assert player.sleep_timer_mode is None
        assert timer_id not in scenario.mass._tracked_timers


class TestDetachWhileANewDeviceAttaches:
    """A device that is lost while its attach is still setting it up leaves nothing behind."""

    @pytest.mark.parametrize("step", ["config", "on_config_updated"])
    async def test_no_later_hook_runs_after_the_detach(self, scenario: Scenario, step: str) -> None:
        """A detach while the config loads or the config hook runs stops the attach there."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        calls = _record_hooks(speaker)
        players = scenario.mass.players
        reached = asyncio.Event()
        release = asyncio.Event()
        config = scenario.mass.config
        load_config = config.get_protocol_config
        on_config_updated = speaker.on_config_updated

        async def _load_config_later(protocol: Any) -> Any:
            reached.set()
            await release.wait()
            return await load_config(protocol)

        async def _hook_waits() -> None:
            reached.set()
            await release.wait()
            await on_config_updated()

        patched: AbstractContextManager[object]
        if step == "config":
            patched = patch.object(config, "get_protocol_config", _load_config_later)
        else:
            patched = patch.object(speaker, "on_config_updated", _hook_waits)
        scenario.clear_events()

        with patched:
            task = await scenario.command(players.register(speaker))
            assert reached.is_set()
            await players.unregister("RINCON_KITCHEN")
            release.set()
            await scenario.settle()
            await task
        await scenario.idle()

        if step == "config":
            # the provider hooks never run on a protocol that was torn down already
            assert "on_config_updated" not in calls
        assert "on_attach" not in calls
        assert players.get_protocol("RINCON_KITCHEN") is None
        assert speaker.player is None
        assert list(players) == []
        assert scenario.visible_players() == []
        assert _stored(scenario) == ([], [])
        assert scenario.events(*LIFECYCLE_EVENTS) == []

    async def test_player_removed_while_its_queue_is_restored_has_no_queue(
        self, scenario: Scenario
    ) -> None:
        """A queue that is restored for a player that was removed meanwhile is dropped."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        players = scenario.mass.players
        queues = scenario.mass.player_queues
        restoring = asyncio.Event()
        release = asyncio.Event()
        restore = queues.on_player_register

        async def _restore_later(player: Player) -> None:
            restoring.set()
            await release.wait()
            await restore(player)

        scenario.clear_events()
        with patch.object(queues, "on_player_register", _restore_later):
            task = await scenario.command(players.register(speaker))
            assert restoring.is_set()
            player = players.get_player_by_protocol("RINCON_KITCHEN")
            assert player is not None
            scenario.set_present("RINCON_KITCHEN", False)
            await players.remove(player.player_id)
            release.set()
            await scenario.settle()
            await task
        await scenario.idle()

        assert queues.get(player.player_id) is None
        assert players.get_player(player.player_id) is None
        assert scenario.events(EventType.QUEUE_ADDED) == []


class TestAnotherObjectOfAnAttachedProtocol:
    """A reconnect hands the player to a new protocol object of the device."""

    async def test_new_object_reads_the_stored_setting_of_the_protocol(
        self, scenario: Scenario
    ) -> None:
        """The object that takes the place of the attached one gets its stored config."""
        squeezelite = await scenario.add_provider("squeezelite")
        old = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
        await scenario.register(old)
        player_id = scenario.single_player(old).player_id
        scenario.mass.config.set_protocol_value("aa:bb:cc:00:00:01", FAKE_SETTING, "custom")
        assert old.config.get_value(FAKE_SETTING) == "custom"

        new = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
        calls = _record_hooks(new)
        await scenario.register(new)

        assert scenario.player_of(new) == player_id
        # without its stored config the new object reads the default of every setting
        assert new.config.get_value(FAKE_SETTING) == "custom"
        assert calls.count("on_config_updated") == 1

    async def test_new_object_detached_during_its_setup_leaves_the_player_unavailable(
        self, scenario: Scenario
    ) -> None:
        """A replacement that is lost while its config hook runs is not attached."""
        squeezelite = await scenario.add_provider("squeezelite")
        old = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
        await scenario.register(old)
        player_id = scenario.single_player(old).player_id
        players = scenario.mass.players
        new = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
        calls = _record_hooks(new)
        on_config_updated = new.on_config_updated

        async def _lost_during_setup() -> None:
            await on_config_updated()
            await players.unregister("aa:bb:cc:00:00:01")

        new.on_config_updated = _lost_during_setup  # type: ignore[method-assign]
        scenario.clear_events()

        await players.register(new)
        await scenario.idle()

        assert "on_attach" not in calls
        assert players.get_protocol("aa:bb:cc:00:00:01") is None
        assert new.player is None
        assert old.player is None
        player = players.get_player(player_id)
        assert player is not None
        assert player.initialized.is_set()
        state = scenario.player(player_id)
        assert state is not None
        assert state.available is False
        assert EventType.PLAYER_REMOVED not in {event.type for event in scenario.events()}

    async def test_new_object_waits_for_the_attach_still_setting_the_first_one_up(
        self, scenario: Scenario
    ) -> None:
        """An object is never swapped in while the attach of the one before it still runs."""
        squeezelite = await scenario.add_provider("squeezelite")
        first = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
        players = scenario.mass.players
        first_calls = _record_hooks(first)
        release = asyncio.Event()
        on_attach = first.on_attach

        async def _attach_later(player: Player) -> None:
            await release.wait()
            await on_attach(player)

        first.on_attach = _attach_later  # type: ignore[method-assign]
        first_task = await scenario.command(players.register(first))
        second = squeezelite.protocol("aa:bb:cc:00:00:01", "Bedroom", identifiers=KITCHEN)
        second_task = await scenario.command(players.register(second))

        # the first attach is still setting its object up, so the second one waits
        assert players.get_protocol("aa:bb:cc:00:00:01") is first
        assert not second_task.done()

        release.set()
        await scenario.settle()
        await first_task
        await second_task

        assert players.get_protocol("aa:bb:cc:00:00:01") is second
        assert first_calls[-1] == "on_detach"
        player = players.get_player_by_protocol("aa:bb:cc:00:00:01")
        assert player is not None
        assert player.initialized.is_set()
        assert second.player is player


class TestFailedAttach:
    """An attach that fails, or is cancelled, takes back what it did."""

    @pytest.mark.parametrize("step", ["config", "on_config_updated"])
    async def test_failed_setup_of_a_new_device_can_be_repeated(
        self, scenario: Scenario, step: str
    ) -> None:
        """After a failed config load or config hook a fresh object of the device attaches."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        players = scenario.mass.players
        config = scenario.mass.config

        async def _fail(*_args: Any) -> None:
            raise BrokenPipeError("device gone")

        patched: AbstractContextManager[object]
        if step == "config":
            patched = patch.object(config, "get_protocol_config", _fail)
        else:
            patched = patch.object(speaker, "on_config_updated", _fail)

        with patched, pytest.raises(BrokenPipeError):
            await players.register(speaker)
        await scenario.settle()

        assert players.get_protocol("RINCON_KITCHEN") is None
        assert list(players) == []
        assert _stored(scenario) == ([], [])

        # the provider tries again with a fresh object once the device reappears
        retried = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        await scenario.register(retried)

        player = players.get_player_by_protocol("RINCON_KITCHEN")
        assert player is not None
        assert player.initialized.is_set()
        assert retried.player is player

    async def test_failed_setup_after_a_detach_raises_its_own_error(
        self, scenario: Scenario
    ) -> None:
        """The error of the setup survives a detach that already took the protocol away."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        players = scenario.mass.players

        async def _detach_then_fail() -> None:
            await players.unregister("RINCON_KITCHEN")
            raise BrokenPipeError("device gone")

        speaker.on_config_updated = _detach_then_fail  # type: ignore[method-assign]

        with pytest.raises(BrokenPipeError):
            await players.register(speaker)
        await scenario.settle()

        assert players.get_protocol("RINCON_KITCHEN") is None
        assert list(players) == []
        assert _stored(scenario) == ([], [])
        scenario.set_present("RINCON_KITCHEN", False)
        await scenario.restart()
        assert scenario.visible_players() == []

    async def test_failure_after_the_player_was_announced_keeps_the_player(
        self, scenario: Scenario
    ) -> None:
        """A player clients were told about stays listed when the rest of its attach fails."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        players = scenario.mass.players

        async def _fail(_player: Player) -> None:
            raise RuntimeError("boom")

        scenario.clear_events()
        with (
            patch.object(scenario.mass.player_queues, "on_player_register", _fail),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await players.register(speaker)
        await scenario.settle()

        added = scenario.events(EventType.PLAYER_ADDED)
        assert len(added) == 1
        player_id = added[0].object_id
        assert player_id is not None
        player = players.get_player(player_id)
        # dropping it now would take it out of every listing without a PLAYER_REMOVED
        assert player is not None
        assert player.initialized.is_set()
        assert [visible.player_id for visible in scenario.visible_players()] == [player_id]
        assert scenario.events(EventType.PLAYER_REMOVED) == []
        assert player_id in _stored(scenario)[0]

    async def test_cancelled_attach_leaves_nothing_behind(self, scenario: Scenario) -> None:
        """An attach cancelled while it runs (a new discovery round) can be repeated."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        players = scenario.mass.players
        reached = asyncio.Event()

        async def _block() -> None:
            reached.set()
            await asyncio.Event().wait()

        speaker.on_config_updated = _block  # type: ignore[method-assign]
        task = await scenario.command(players.register(speaker))
        assert reached.is_set()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await scenario.settle()

        assert players.get_protocol("RINCON_KITCHEN") is None
        assert list(players) == []
        assert _stored(scenario) == ([], [])

        retried = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        await scenario.register(retried)
        assert scenario.player_of(retried) is not None

    async def test_failing_teardown_keeps_the_error_of_the_setup(self, scenario: Scenario) -> None:
        """A protocol that also fails to release itself does not hide why its setup failed."""
        sonos = await scenario.add_provider("sonos")
        speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
        players = scenario.mass.players
        scenario.expect_log("Error detaching protocol RINCON_KITCHEN")

        async def _setup_fails() -> None:
            raise BrokenPipeError("device gone")

        async def _teardown_fails() -> None:
            raise RuntimeError("teardown")

        speaker.on_config_updated = _setup_fails  # type: ignore[method-assign]
        speaker.on_detach = _teardown_fails  # type: ignore[method-assign]

        with pytest.raises(BrokenPipeError):
            await players.register(speaker)
        await scenario.settle()

        assert players.get_protocol("RINCON_KITCHEN") is None
        assert list(players) == []
        assert _stored(scenario) == ([], [])


class TestPlayerThatGoes:
    """What clients see when a player goes, and what stays for one that only went away."""

    async def test_queue_of_a_removed_player_goes_before_the_player(
        self, scenario: Scenario
    ) -> None:
        """Clients get the final (idle) update of the queue before the player is removed."""
        speaker, player_id = await _kitchen_player(scenario)
        await scenario.play(player_id)
        scenario.set_present(speaker.protocol_id, False)
        scenario.clear_events()

        await scenario.remove(player_id)

        events = scenario.events(
            EventType.QUEUE_UPDATED, EventType.PLAYER_REMOVED, object_id=player_id
        )
        assert events[-2:] == [
            Event(EventType.QUEUE_UPDATED, player_id),
            Event(EventType.PLAYER_REMOVED, player_id),
        ]

    async def test_device_that_reports_to_be_a_group_keeps_its_player_and_queue(
        self, scenario: Scenario
    ) -> None:
        """A protocol that starts to report the group type stays the same player (Chromecast)."""
        chromecast = await scenario.add_provider("chromecast")
        speaker = chromecast.protocol("cc_kitchen", "Kitchen", identifiers=KITCHEN)
        await scenario.register(speaker)
        player_id = scenario.single_player(speaker).player_id
        scenario.clear_events()

        speaker._set_reported(type=PlayerType.GROUP)
        await scenario.register(speaker)
        await scenario.idle()

        assert scenario.player_of(speaker) == player_id
        state = scenario.player(player_id)
        assert state is not None
        assert state.type == PlayerType.GROUP
        assert scenario.queue(player_id) is not None
        assert {event.type for event in scenario.events(*LIFECYCLE_EVENTS)} == {
            EventType.PLAYER_UPDATED
        }

    async def test_failing_teardown_still_takes_the_protocol_off_and_the_player_away(
        self, scenario: Scenario
    ) -> None:
        """A provider that raises while releasing its protocol does not stop a detach or removal."""
        speaker, player_id = await _kitchen_player(scenario)
        players = scenario.mass.players
        scenario.expect_log("Error detaching protocol RINCON_KITCHEN")
        torn_down: list[str] = []

        async def _teardown_fails() -> None:
            torn_down.append(speaker.protocol_id)
            raise RuntimeError("device is gone")

        speaker.on_detach = _teardown_fails  # type: ignore[method-assign]

        await scenario.unregister(speaker)

        assert torn_down == ["RINCON_KITCHEN"]
        assert players.get_protocol("RINCON_KITCHEN") is None
        assert speaker.player is None
        state = scenario.player(player_id)
        assert state is not None
        assert state.available is False

        await scenario.register(speaker)
        await scenario.remove(player_id)

        assert torn_down == ["RINCON_KITCHEN", "RINCON_KITCHEN"]
        assert players.get_player(player_id) is None
        assert scenario.visible_players() == []


class TestGroupMemberships:
    """A group lists its members by player id in its stored config."""

    async def test_removal_drops_the_player_from_the_member_list_of_a_group(
        self, scenario: Scenario
    ) -> None:
        """A removed player is no member anymore; the allow-list is left as it is."""
        speaker, player_id = await _kitchen_player(scenario)
        await _group_listing(scenario, player_id, "pl_other")
        scenario.set_present(speaker.protocol_id, False)

        await scenario.remove(player_id)

        config = scenario.mass.config
        assert config.get_protocol_value("group_all", CONF_GROUP_MEMBERS) == ["pl_other"]
        # an allow-list that runs empty would stop restricting anything
        assert config.get_protocol_value("group_all", CONF_ALLOWED_MEMBERS) == [
            player_id,
            "pl_other",
        ]

    async def test_detached_player_keeps_its_place_in_a_group(self, scenario: Scenario) -> None:
        """A player whose device is only away stays a member of the group."""
        speaker, player_id = await _kitchen_player(scenario)
        await _group_listing(scenario, player_id, "pl_other")

        await scenario.unregister(speaker)
        await scenario.idle()

        assert scenario.mass.config.get_protocol_value("group_all", CONF_GROUP_MEMBERS) == [
            player_id,
            "pl_other",
        ]

    async def test_attached_group_rereads_its_members_when_one_is_removed(
        self, scenario: Scenario
    ) -> None:
        """An attached group is told to re-read its member list, which no longer has the player."""
        speaker, player_id = await _kitchen_player(scenario)
        group = await _group_listing(scenario, player_id, "pl_other")
        scenario.set_present(speaker.protocol_id, False)
        seen: list[Any] = []
        on_config_updated = group.on_config_updated

        async def _reread() -> None:
            seen.append(group.config.get_value(CONF_GROUP_MEMBERS))
            await on_config_updated()

        group.on_config_updated = _reread  # type: ignore[method-assign]

        await scenario.remove(player_id)
        await scenario.idle()

        # the live config copy is pruned before the group re-reads it
        assert seen == [["pl_other"]]


class TestPowerOnOfAReturningDevice:
    """A device that is switched on at its power control is played to once it is back."""

    async def test_power_on_returns_once_the_returning_protocol_finished_its_attach(
        self, scenario: Scenario
    ) -> None:
        """A protocol that is still setting up the returning device is not played to yet."""
        squeezelite = await scenario.add_provider("squeezelite")
        pi = squeezelite.protocol("b8:27:eb:aa:bb:cc", "Bathroom", identifiers=KITCHEN)
        await scenario.register(pi)
        player_id = scenario.single_player(pi).player_id
        plug = await scenario.add_power_control("switch.plug", "Plug", powered=True)
        await scenario.configure_player(player_id, power_control=plug.control_id)
        await scenario.switch(plug, False)
        await scenario.unregister(pi)
        await scenario.idle()
        players = scenario.mass.players
        release = asyncio.Event()
        on_attach = pi.on_attach

        async def _attach_later(player: Player) -> None:
            await release.wait()
            await on_attach(player)

        pi.on_attach = _attach_later  # type: ignore[method-assign]

        power_on = await scenario.command(players.cmd_power(player_id, True))
        assert plug.commands == [True]
        attach = await scenario.command(players.register(pi))
        # the power on looks for the device every 0.5 s
        await scenario.advance(1)

        assert not power_on.done()

        release.set()
        await scenario.settle()
        await attach
        await scenario.advance(1)
        await power_on
        state = scenario.player(player_id)
        assert state is not None
        assert state.powered is True
