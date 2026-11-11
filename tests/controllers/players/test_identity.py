"""Tests for which player a protocol belongs to when it attaches, and when it has to wait."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState, ProtocolRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_LEGACY_IDS,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
)
from music_assistant.controllers.players.controller import PlayerController
from tests.scenarios.harness import FAKE_SETTING, Event, Scenario

KITCHEN = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.20",
}
# what a protocol reports of the same device while its hardware address is not known yet
KITCHEN_ADDRESS = {IdentifierType.IP_ADDRESS: "192.168.1.20"}
PLAYER_EVENTS = (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED)


def _edge(scenario: Scenario, protocol_id: str) -> str | None:
    """Return the id of the player a protocol is stored as belonging to."""
    record = scenario.mass.config.get_protocol_record(protocol_id)
    return record.get("player_id") if record else None


async def test_audio_protocol_without_a_hardware_address_waits_for_its_device(
    scenario: Scenario,
) -> None:
    """A protocol that could still turn out to belong to a device gets a player after 15 s."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    scenario.clear_events()

    await scenario.attach(speaker)
    await scenario.advance(14)

    assert scenario.player_of(speaker) is None
    assert scenario.mass.players.get_protocol("ap_kitchen") is speaker
    assert scenario.visible_players() == []
    assert scenario.mass.config.get(CONF_PLAYER_PROTOCOLS, {}) == {}

    await scenario.advance(1)
    await scenario.idle()

    player_id = scenario.single_player(speaker).player_id
    assert player_id.startswith("pl")
    assert scenario.events(*PLAYER_EVENTS) == [Event(EventType.PLAYER_ADDED, player_id)]


async def test_waiting_protocol_gets_its_player_once_its_hardware_address_is_known(
    scenario: Scenario,
) -> None:
    """The wait ends as soon as the address resolves to the hardware address of the device."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.attach(speaker)

    await scenario.report(speaker, identifiers=KITCHEN)
    await scenario.idle()

    assert scenario.player_of(speaker) is not None


async def test_waiting_protocol_joins_the_player_its_native_protocol_creates(
    scenario: Scenario,
) -> None:
    """A protocol that waits is attached as soon as another protocol created its player."""
    airplay = await scenario.add_provider("airplay")
    sonos = await scenario.add_provider("sonos")
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(audio)
    await scenario.advance(2)

    await scenario.attach(native)
    await scenario.idle()

    assert scenario.player_of(audio) == scenario.player_of(native) is not None
    assert len(scenario.visible_players()) == 1


async def test_two_waiting_protocols_of_one_device_become_one_player(scenario: Scenario) -> None:
    """Protocols that wait together end up on the player the first of them gets."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    first = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    second = chromecast.protocol("cc_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.attach(first)
    await scenario.attach(second)

    await scenario.advance(15)
    await scenario.idle()

    assert scenario.player_of(first) == scenario.player_of(second) is not None
    assert len(scenario.visible_players()) == 1


async def test_protocol_that_is_lost_while_it_waits_becomes_no_player(scenario: Scenario) -> None:
    """A detach during the wait ends it without leaving a player or a config behind."""
    airplay = await scenario.add_provider("airplay")
    speaker = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN_ADDRESS)
    await scenario.attach(speaker)

    await scenario.detach(speaker)
    await scenario.advance(20)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.mass.players.get_protocol("ap_kitchen") is None
    assert scenario.mass.config.get(CONF_PLAYER_PROTOCOLS, {}) == {}


async def test_native_protocol_never_waits(scenario: Scenario) -> None:
    """The protocol that stands for the device itself gets its player at once."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN_ADDRESS)

    await scenario.attach(speaker)

    assert scenario.player_of(speaker) is not None


async def test_protocol_that_rides_on_another_waits_for_it(scenario: Scenario) -> None:
    """A derived protocol is attached to the player of its base, however late that comes."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    base = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    bridge = sendspin.protocol(
        "sendspin_bridge_kitchen", "Kitchen", role="audio", underlying_protocol_id="ap_kitchen"
    )
    await scenario.attach(bridge)
    await scenario.advance(60)

    assert scenario.player_of(bridge) is None

    await scenario.attach(base)
    await scenario.idle()

    assert scenario.player_of(bridge) == scenario.player_of(base) is not None


async def test_protocol_returns_to_the_player_it_is_stored_for(scenario: Scenario) -> None:
    """A protocol that comes back is attached to its player without matching it again."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(native)
    await scenario.attach(audio)
    player_id = scenario.single_player(native, audio).player_id
    await scenario.detach(audio)

    # it reports nothing its player could be recognised by anymore
    moved = airplay.protocol(
        "ap_kitchen",
        "Kitchen",
        role="audio",
        identifiers={IdentifierType.IP_ADDRESS: "192.168.1.77"},
    )
    await scenario.attach(moved)

    assert scenario.player_of(moved) == player_id


async def test_stored_player_that_turns_out_to_be_another_device_is_left(
    scenario: Scenario,
) -> None:
    """A protocol whose stored player holds an identifier that rules it out is matched anew."""
    chromecast = await scenario.add_provider("chromecast")
    airplay = await scenario.add_provider("airplay")
    cast = chromecast.protocol(
        "cc_kitchen",
        "Kitchen",
        role="audio",
        identifiers=KITCHEN | {IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:01"},
    )
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(cast)
    await scenario.attach(audio)
    player_id = scenario.single_player(cast, audio).player_id
    await scenario.detach(audio)

    other = airplay.protocol(
        "ap_kitchen",
        "Bedroom",
        role="audio",
        identifiers={
            IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1",
            IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:02",
        },
    )
    await scenario.attach(other)
    await scenario.idle()

    new_player_id = scenario.player_of(other)
    assert new_player_id not in (None, player_id)
    assert _edge(scenario, "ap_kitchen") == new_player_id
    state = scenario.player(player_id)
    assert state is not None
    assert [protocol.protocol_id for protocol in state.protocols] == ["cc_kitchen"]


async def test_device_that_matches_a_disabled_player_stays_without_a_player(
    scenario: Scenario,
) -> None:
    """A protocol of a disabled player is stored as belonging to it, and attached once enabled."""
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(native)
    player_id = scenario.single_player(native).player_id
    await scenario.disable(player_id)
    scenario.clear_events()

    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(audio)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.player_of(audio) is None
    assert _edge(scenario, "ap_kitchen") == player_id
    assert scenario.events(*PLAYER_EVENTS) == []

    await scenario.enable(player_id)
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("ap_kitchen")) == player_id
    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == player_id


@pytest.mark.parametrize(
    ("domains", "roles"),
    [(("squeezelite", "snapcast"), ("native", "native")), (("airplay", "airplay"), ("audio",) * 2)],
    ids=["two_natives", "one_domain_twice"],
)
async def test_protocols_a_player_has_no_second_place_for_stay_separate(
    scenario: Scenario, domains: tuple[str, str], roles: tuple[Any, Any]
) -> None:
    """A player holds one protocol that stands for the device and one per domain."""
    first_provider = await scenario.add_provider(domains[0])
    second_provider = (
        first_provider if domains[1] == domains[0] else await scenario.add_provider(domains[1])
    )
    first = first_provider.protocol("first", "Pi", role=roles[0], identifiers=KITCHEN)
    second = second_provider.protocol("second", "Pi", role=roles[1], identifiers=KITCHEN)

    await scenario.attach(first)
    await scenario.attach(second)
    await scenario.idle()

    assert scenario.player_of(first) != scenario.player_of(second)
    assert None not in (scenario.player_of(first), scenario.player_of(second))


async def test_protocol_that_becomes_a_second_own_protocol_gets_a_player_of_its_own(
    scenario: Scenario,
) -> None:
    """An audio protocol that reports to be the device itself leaves a player that has one."""
    sonos = await scenario.add_provider("sonos")
    squeezelite = await scenario.add_provider("squeezelite")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    other = squeezelite.protocol("sq_kitchen", "Kitchen", role="audio", identifiers=KITCHEN)
    await scenario.attach(native)
    await scenario.attach(other)
    player_id = scenario.single_player(native, other).player_id
    scenario.clear_events()

    await scenario.attach(other, role="native")
    await scenario.idle()

    new_player_id = scenario.player_of(other)
    assert new_player_id not in (None, player_id)
    assert scenario.player_of(native) == player_id
    state = scenario.player(player_id)
    assert state is not None
    assert [protocol.protocol_id for protocol in state.protocols] == ["RINCON_KITCHEN"]
    assert scenario.events(*PLAYER_EVENTS) == [Event(EventType.PLAYER_ADDED, new_player_id)]


async def test_native_protocol_that_becomes_an_audio_protocol_stops_what_it_played(
    scenario: Scenario,
) -> None:
    """A protocol that changes its role is detached and attached again on the same player."""
    squeezelite = await scenario.add_provider("squeezelite")
    speaker = squeezelite.protocol("sq_kitchen", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    player_id = scenario.single_player(speaker).player_id
    await scenario.play(player_id)
    scenario.clear_events()

    await scenario.attach(speaker, role="audio")
    await scenario.idle()

    assert scenario.player_of(speaker) == player_id
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    assert player.get_role("sq_kitchen") == ProtocolRole.AUDIO
    assert player.own_protocol is None
    assert player.active_output_protocol is None
    queue = scenario.queue(player_id)
    assert queue is not None
    assert queue.state == PlaybackState.IDLE
    assert scenario.events(*PLAYER_EVENTS) == []


async def test_session_only_player_keeps_its_player_when_its_protocol_changes_role(
    scenario: Scenario,
) -> None:
    """A role change keeps a player that nothing is stored of, with its id."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", role="audio", session_only=True)
    await scenario.attach(browser)
    assert scenario.player_of(browser) == "sendspin_web_1"
    scenario.clear_events()

    await scenario.attach(browser, role="native")

    assert scenario.player_of(browser) == "sendspin_web_1"
    assert scenario.events(*PLAYER_EVENTS) == []


async def test_config_stored_for_a_protocol_before_it_attaches_is_kept(scenario: Scenario) -> None:
    """A provider that stores the settings of a new device first finds them after the attach."""
    sonos = await scenario.add_provider("sonos")
    config = scenario.mass.config
    config.create_protocol_record(
        "RINCON_KITCHEN",
        provider=sonos.instance_id,
        player_id=None,
        role=ProtocolRole.NATIVE,
        default_name="Kitchen",
    )
    config.set_protocol_value("RINCON_KITCHEN", FAKE_SETTING, "seeded")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    seen: list[Any] = []

    async def _on_config_updated() -> None:
        seen.append(speaker.config.get_value(FAKE_SETTING))

    speaker.on_config_updated = _on_config_updated  # type: ignore[method-assign]
    await scenario.attach(speaker)

    player_id = scenario.single_player(speaker).player_id
    assert seen == ["seeded"]
    assert _edge(scenario, "RINCON_KITCHEN") == player_id
    stored = scenario.protocol_config("RINCON_KITCHEN")
    assert stored is not None
    assert stored[FAKE_SETTING] == "seeded"
    assert config.get_protocol_ids_of_provider(sonos.instance_id) == ["RINCON_KITCHEN"]
    assert config.get_protocol_ids_of_provider("airplay") == []


async def test_protocol_is_found_by_its_id_also_when_that_is_a_legacy_id(
    scenario: Scenario,
) -> None:
    """The id of a protocol names that protocol, also where it names another player as well."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    other = sonos.protocol(
        "RINCON_DEN", "Den", identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:C0"}
    )
    await scenario.attach(speaker)
    await scenario.attach(other)
    kitchen_id = scenario.single_player(speaker).player_id
    den_id = scenario.single_player(other).player_id
    # the den player was once known under the id of the kitchen protocol
    scenario.mass.config.set_player_value(den_id, CONF_LEGACY_IDS, ["RINCON_KITCHEN"])
    players = scenario.mass.players
    players._build_legacy_id_index()

    assert players.get_player("RINCON_KITCHEN") is players.get_player(den_id)
    assert players.get_protocol("RINCON_KITCHEN") is speaker
    # the ids protocols report name protocols first
    assert players.get_player_by_protocol("RINCON_KITCHEN") is players.get_player(kitchen_id)
    assert scenario.mass.player_queues.get("RINCON_KITCHEN") is scenario.queue(den_id)


# a player from before the split that lost its protocols, which the user had given a name
KEPT_WRAPPER: dict[str, Any] = {
    "player_id": "up0a1b2c3d",
    "provider": "universal_player",
    "player_type": "player",
    "enabled": True,
    "name": "Kitchen radio",
    "default_name": "Kitchen",
    "values": {
        "linked_protocol_ids": [],
        "device_identifiers": {"mac_address": "54:78:C9:E6:0D:A0"},
    },
}


async def test_device_returns_to_the_player_that_was_kept_for_it(scenario: Scenario) -> None:
    """A stored player without protocols is offline until a device with its identifiers attaches."""
    sonos = await scenario.add_provider("sonos")
    sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)

    await scenario.seed_pre_split_config({"up0a1b2c3d": KEPT_WRAPPER})
    await scenario.idle()

    state = scenario.player("up0a1b2c3d")
    assert state is not None
    assert (state.name, state.available, state.provider) == ("Kitchen radio", False, "")
    assert state.setup_reason is None
    assert state.device_info.manufacturer

    await scenario.attach(scenario.protocol("RINCON_KITCHEN"))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("RINCON_KITCHEN")) == "up0a1b2c3d"
    state = scenario.player("up0a1b2c3d")
    assert state is not None
    assert (state.name, state.available, state.provider) == ("Kitchen radio", True, "sonos")
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/up0a1b2c3d/provider") == sonos.instance_id


async def test_players_are_restored_without_the_user_database(scenario: Scenario) -> None:
    """The stored players come back at a start before the database of the users is opened."""
    restored: list[bool] = []
    post_setup = PlayerController.post_setup

    async def _post_setup_without_users(self: PlayerController) -> None:
        webserver = self.mass.webserver
        # any use of the webserver, which holds the users, fails from here on
        self.mass.webserver = None  # type: ignore[assignment]
        try:
            await post_setup(self)
        finally:
            self.mass.webserver = webserver
        restored.append(self.get_player("up0a1b2c3d") is not None)

    with patch.object(PlayerController, "post_setup", _post_setup_without_users):
        await scenario.seed_pre_split_config(
            {
                "up0a1b2c3d": KEPT_WRAPPER,
                "sendspin_web_1": {
                    "player_id": "sendspin_web_1",
                    "provider": "sendspin",
                    "player_type": "player",
                    "values": {},
                },
            }
        )

    assert restored == [True]
    assert [player.player_id for player in scenario.visible_players()] == ["up0a1b2c3d"]


async def test_session_only_player_keeps_what_is_stored_under_its_id(scenario: Scenario) -> None:
    """The split deletes the config of a web player, and nothing else that names its id."""
    sendspin = await scenario.add_provider("sendspin")
    sonos = await scenario.add_provider("sonos")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.attach(speaker)
    await scenario.attach(browser)
    await scenario.play("sendspin_web_1")
    saved_queue = await scenario.saved_queue("sendspin_web_1")
    assert saved_queue
    auth = scenario.mass.webserver.auth
    user = await auth.create_user(
        username="guest",
        player_filter=["sendspin_web_1", scenario.single_player(speaker).player_id],
    )
    await scenario.detach(browser)

    await scenario.seed_pre_split_config(
        {
            "sendspin_web_1": {
                "player_id": "sendspin_web_1",
                "provider": "sendspin",
                "player_type": "player",
                "values": {"sendspin_static_delay": 120},
            }
        }
    )
    await scenario.idle()

    assert await scenario.player_config("sendspin_web_1") is None
    stored_user = await scenario.mass.webserver.auth.get_user(user.user_id)
    assert stored_user is not None
    assert "sendspin_web_1" in stored_user.player_filter

    await scenario.attach(scenario.protocol("sendspin_web_1"))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("sendspin_web_1")) == "sendspin_web_1"
    assert await scenario.saved_queue("sendspin_web_1") == saved_queue


async def test_rejected_first_save_of_a_session_only_player_stores_nothing(
    scenario: Scenario,
) -> None:
    """A web player whose first config change is refused stays without a stored config."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_1", "Web Browser", session_only=True)
    await scenario.attach(browser)

    with pytest.raises(InvalidDataError):
        await scenario.mass.config.save_player_config(
            "sendspin_web_1", {CONF_MIN_VOLUME: 80, CONF_MAX_VOLUME: 20}
        )
    await scenario.settle()

    assert await scenario.stored_config_mentioning("sendspin_web_1") == []

    await scenario.configure_player("sendspin_web_1", name="Study browser")

    assert scenario.mass.config.get(f"{CONF_PLAYERS}/sendspin_web_1/name") == "Study browser"
    assert _edge(scenario, "sendspin_web_1") == "sendspin_web_1"
