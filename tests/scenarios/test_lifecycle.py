"""When a player comes into being and when it goes: providers, disabled devices, sessions, restarts."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY

import pytest
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlayerFeature,
    ProviderTier,
)
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_UNLINKED_FROM,
    SETUP_REASON_PROVIDER_NOT_INSTALLED,
)
from music_assistant.controllers.players.lifecycle import REMOVE_UNAVAILABLE_GRACE
from music_assistant.helpers.config_entries import (
    create_connected_players_entry,
    create_player_selector,
)

from .harness import DEFAULT_FEATURES, TEST_TRACK_URI, Event, Role, Scenario, VisiblePlayer

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant_models.config_entries import ConfigEntry

LIVING = {
    IdentifierType.MAC_ADDRESS: "A8:40:41:00:00:01",
    IdentifierType.IP_ADDRESS: "192.168.1.21",
}
TV = {IdentifierType.MAC_ADDRESS: "F4:F5:D8:00:00:02", IdentifierType.IP_ADDRESS: "192.168.1.22"}
DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}
# what Sendspin reports for a browser or app session today
WEB_PLAYER: dict[str, Any] = {"hidden_by_default": True, "private": True}

MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac")


async def test_player_outlives_its_removed_provider_on_its_other_protocols(
    scenario: Scenario,
) -> None:
    """A player whose native provider is removed stays, with its id, carried by its audio protocols."""
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    living_airplay = airplay.protocol("airplay_living", "Living", role="audio", identifiers=LIVING)
    living_sendspin = sendspin.protocol(
        "sendspin_living", "Living", role="audio", identifiers=LIVING
    )
    for protocol in (living, living_airplay, living_sendspin):
        await scenario.register(protocol)
    player_id = scenario.single_player(living, living_airplay, living_sendspin).player_id
    scenario.clear_events()

    await scenario.remove_provider(wiim)
    await scenario.advance(15)
    await scenario.idle()

    # clients resolve the provider a player names: that of its highest-priority protocol left
    assert scenario.players_carrying(living_airplay, living_sendspin) == [
        VisiblePlayer(
            player_id=player_id,
            name="Living",
            available=True,
            powered=None,
            provider="airplay",
            outputs=(("airplay", True), ("sendspin", True)),
        )
    ]
    assert scenario.events(EventType.PLAYER_REMOVED) == []


async def test_player_without_other_protocols_goes_with_its_removed_provider(
    scenario: Scenario,
) -> None:
    """A player left without any protocol config when its provider is removed is removed with it."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    scenario.clear_events()

    await scenario.remove_provider(wiim)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.queue(player_id) is None
    assert await scenario.player_config(player_id) is None
    assert scenario.events(EventType.PLAYER_REMOVED) == [Event(EventType.PLAYER_REMOVED, player_id)]


async def test_removed_player_of_a_present_device_comes_back_as_new(scenario: Scenario) -> None:
    """A removed player whose device is still there comes back new: a new id, its defaults."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    await scenario.configure_player(player_id, name="Kitchen")
    scenario.clear_events()

    await scenario.remove(player_id)
    await scenario.idle()

    current = scenario.protocol("wiim_living")
    assert current is not living
    assert scenario.players_carrying(current) == [
        VisiblePlayer(
            player_id=ANY,
            name="Living",
            available=True,
            powered=None,
            provider="wiim",
            outputs=(("wiim", True),),
        )
    ]
    new_player_id = scenario.single_player(current).player_id
    assert new_player_id != player_id
    assert scenario.events(EventType.PLAYER_REMOVED, EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_REMOVED, player_id),
        Event(EventType.PLAYER_ADDED, new_player_id),
    ]


@pytest.mark.parametrize(
    ("domain", "role"),
    [("airplay", "audio"), ("chromecast", "native")],
    ids=["airplay_mac", "cast_tv"],
)
async def test_disabled_by_default_device_is_no_player_until_enabled(
    scenario: Scenario, domain: str, role: Role
) -> None:
    """
    A device that is disabled by default is listed in the settings only.

    Once the user enables it, the player appears with its queue and plays.
    """
    provider = await scenario.add_provider(domain)
    tv = provider.protocol(
        f"{domain}_tv", "Living Room TV", role=role, identifiers=TV, enabled_by_default=False
    )
    await scenario.register(tv)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.events(EventType.PLAYER_ADDED) == []
    listed = [
        config
        for config in await scenario.player_configs()
        if config.default_name == "Living Room TV"
    ]
    assert [config.enabled for config in listed] == [False]

    await scenario.enable(listed[0].player_id)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.players_carrying(tv) == [
        VisiblePlayer(
            player_id=ANY,
            name="Living Room TV",
            available=True,
            powered=None,
            provider=domain,
            outputs=((domain, True),),
        )
    ]
    player_id = scenario.single_player(tv).player_id
    assert scenario.queue(player_id) is not None
    assert scenario.events(EventType.PLAYER_ADDED) == [Event(EventType.PLAYER_ADDED, player_id)]
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()
    assert ("play_media", MEDIA) in tv.commands


async def _stored_mentions(scenario: Scenario, *texts: str) -> list[str]:
    """Return everything in the stored config that mentions any of the texts."""
    return [mention for text in texts for mention in await scenario.stored_config_mentioning(text)]


async def test_session_only_web_player_exists_while_attached(scenario: Scenario) -> None:
    """
    A web player exists while its protocol is attached and leaves nothing behind.

    The protocol reports that it is session-only. Nothing of it is stored anywhere in the
    config, no player once it detaches, a new session replaces the old one, and nothing after
    a restart.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol(
        "sendspin_web_3f2a", "Web Browser", session_only=True, volume_level=20, **WEB_PLAYER
    )
    await scenario.register(browser)
    await scenario.idle()
    player_id = scenario.single_player(browser).player_id

    assert await _stored_mentions(scenario, player_id, "sendspin_web_3f2a") == []

    # the page reloads: a new session connects with the same client id
    reloaded = sendspin.protocol(
        "sendspin_web_3f2a", "Web Browser", session_only=True, volume_level=35, **WEB_PLAYER
    )
    await scenario.register(reloaded)
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()

    assert [player.player_id for player in scenario.players_carrying(reloaded)] == [player_id]
    player = scenario.player(player_id)
    assert player is not None
    assert player.volume_level == 35
    assert ("play_media", MEDIA) in reloaded.commands
    assert browser.commands == []

    await scenario.unregister(reloaded)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await _stored_mentions(scenario, player_id, "sendspin_web_3f2a") == []

    await scenario.restart()
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []


async def test_a_customised_browser_player_stays_listed_while_its_browser_is_away(
    scenario: Scenario,
) -> None:
    """
    A web player the user customised stays a player, unavailable, while its client is away.

    Its client disconnecting is no removal, also not after a restart; when it connects again
    the player is available under the same id, with its settings and its queue.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(browser)
    player_id = scenario.single_player(browser).player_id
    await scenario.configure_player(player_id, name="Den browser")
    await scenario.play(player_id)
    await scenario.idle()
    scenario.clear_events()

    await scenario.unregister(browser)
    await scenario.idle()

    assert [(p.player_id, p.name, p.available) for p in scenario.visible_players()] == [
        (player_id, "Den browser", False)
    ]
    assert scenario.events(EventType.PLAYER_UPDATED, object_id=player_id) != []

    await scenario.restart()
    await scenario.idle()

    assert [(p.player_id, p.name, p.available) for p in scenario.visible_players()] == [
        (player_id, "Den browser", False)
    ]
    assert [(c.player_id, c.name) for c in await scenario.player_configs()] == [
        (player_id, "Den browser")
    ]

    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [(p.player_id, p.name, p.available) for p in scenario.visible_players()] == [
        (player_id, "Den browser", True)
    ]
    assert [item.uri for item in scenario.mass.player_queues.items(player_id)] == [TEST_TRACK_URI]
    assert scenario.removals() == []


async def test_the_settings_list_every_stored_player_and_every_connected_one(
    scenario: Scenario,
) -> None:
    """
    The settings list every player with a stored config and every connected player without one.

    A customised web player whose client is away is listed like any other stored player.
    """
    wiim = await scenario.add_provider("wiim")
    sendspin = await scenario.add_provider("sendspin")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    customised = sendspin.protocol(
        "sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER
    )
    uncustomised = sendspin.protocol("sendspin_app_9c1d", "Phone app", session_only=True)
    for protocol in (living, customised, uncustomised):
        await scenario.register(protocol)
    living_id = scenario.single_player(living).player_id
    await scenario.configure_player("sendspin_web_3f2a", name="Den browser")
    await scenario.unregister(customised)
    await scenario.idle()

    assert sorted((c.player_id, c.name) for c in await scenario.player_configs()) == sorted(
        [(living_id, None), ("sendspin_web_3f2a", "Den browser"), ("sendspin_app_9c1d", None)]
    )


def _defaults(scenario: Scenario, player_id: str) -> tuple[Any, ...]:
    """
    Return what a player has from what its protocol supplies.

    Whether it is hidden in the UI, exposed to Home Assistant and private, its device info
    and its icon.

    :param scenario: The running server.
    :param player_id: ID of the player.
    """
    state = scenario.player(player_id)
    assert state is not None
    return (
        state.hide_in_ui,
        state.expose_to_ha,
        state.private,
        state.device_info.manufacturer,
        state.device_info.model,
        state.icon,
    )


async def test_a_kept_browser_player_keeps_the_defaults_of_its_client_while_away(
    scenario: Scenario,
) -> None:
    """
    A kept web player is hidden, not exposed and private while away, as its client has it.

    The same after a restart and when its client connects again.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol(
        "sendspin_web_3f2a",
        "Web Browser",
        session_only=True,
        expose_to_ha_by_default=False,
        **WEB_PLAYER,
    )
    await scenario.register(browser)
    await scenario.configure_player("sendspin_web_3f2a", name="Den browser")
    connected = _defaults(scenario, "sendspin_web_3f2a")
    assert connected[:3] == (True, False, True)

    await scenario.unregister(browser)
    await scenario.idle()
    away = _defaults(scenario, "sendspin_web_3f2a")
    await scenario.restart()
    await scenario.idle()
    restarted = _defaults(scenario, "sendspin_web_3f2a")
    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [away, restarted, _defaults(scenario, "sendspin_web_3f2a")] == [connected] * 3


async def test_a_value_the_user_set_on_a_kept_browser_player_wins_over_its_client(
    scenario: Scenario,
) -> None:
    """A web player the user showed and exposed stays shown and exposed while its client is away."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol(
        "sendspin_web_3f2a",
        "Web Browser",
        session_only=True,
        expose_to_ha_by_default=False,
        **WEB_PLAYER,
    )
    await scenario.register(browser)
    await scenario.configure_player("sendspin_web_3f2a", hide_in_ui=False, expose_player_to_ha=True)
    chosen = _defaults(scenario, "sendspin_web_3f2a")
    assert chosen[:3] == (False, True, True)

    await scenario.unregister(browser)
    await scenario.idle()
    away = _defaults(scenario, "sendspin_web_3f2a")
    await scenario.restart()
    await scenario.idle()

    assert [away, _defaults(scenario, "sendspin_web_3f2a")] == [chosen] * 2


async def test_a_player_keeps_the_defaults_of_its_device_while_the_device_is_away(
    scenario: Scenario,
) -> None:
    """
    A player its device hides, keeps from Home Assistant and keeps private stays so while away.

    A device that comes back with other defaults gives the player those from then on.
    """
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol(
        "wiim_living",
        "Living",
        identifiers=LIVING,
        hidden_by_default=True,
        expose_to_ha_by_default=False,
        private=True,
    )
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    connected = _defaults(scenario, player_id)
    assert connected[:3] == (True, False, True)

    await scenario.unregister(living)
    await scenario.idle()
    away = _defaults(scenario, player_id)
    await scenario.restart()
    await scenario.idle()

    assert [away, _defaults(scenario, player_id)] == [connected] * 2

    updated = scenario.provider("wiim").protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(updated)
    await scenario.unregister(updated)
    await scenario.idle()

    assert _defaults(scenario, player_id)[:3] == (False, True, False)


async def test_only_the_own_protocol_of_a_player_gives_it_its_defaults(
    scenario: Scenario,
) -> None:
    """
    An audio protocol or a bridge that hides itself gives its player none of that.

    The Den player has no protocol of its own, the Living player has one that gives the
    generic defaults: both keep those, attached, away and after a restart, and nothing of
    the audio protocols is stored as their defaults.
    """
    hiding: dict[str, Any] = {
        "hidden_by_default": True,
        "expose_to_ha_by_default": False,
        "private": True,
    }
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    den = airplay.protocol("ap_den", "Den", role="audio", identifiers=DEN, **hiding)
    den_bridge = sendspin.protocol(
        "spb_den", "Den", role="audio", underlying_protocol_id="ap_den", **hiding
    )
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    living_airplay = airplay.protocol(
        "ap_living", "Living", role="audio", identifiers=LIVING, **hiding
    )
    for protocol in (den, den_bridge, living, living_airplay):
        await scenario.register(protocol)
    den_id = scenario.single_player(den, den_bridge).player_id
    living_id = scenario.single_player(living, living_airplay).player_id
    generic = (False, True, False)

    attached = [_defaults(scenario, den_id)[:3], _defaults(scenario, living_id)[:3]]
    for protocol in (den_bridge, den, living, living_airplay):
        await scenario.unregister(protocol)
    await scenario.idle()
    away = [_defaults(scenario, den_id)[:3], _defaults(scenario, living_id)[:3]]
    await scenario.restart()
    await scenario.idle()
    restarted = [_defaults(scenario, den_id)[:3], _defaults(scenario, living_id)[:3]]

    assert [attached, away, restarted] == [[generic, generic]] * 3
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{den_id}/values/protocol_defaults") is None


async def test_session_only_player_has_the_same_id_in_every_session(scenario: Scenario) -> None:
    """A web player is known by the id of its protocol, in every session and after a restart."""
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(browser)

    assert scenario.player_of(browser) == "sendspin_web_3f2a"

    await scenario.unregister(browser)
    await scenario.register(browser)

    assert scenario.player_of(browser) == "sendspin_web_3f2a"

    await scenario.unregister(browser)
    await scenario.restart()
    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [player.player_id for player in scenario.visible_players()] == ["sendspin_web_3f2a"]
    assert (
        scenario.events(EventType.PLAYER_ADDED)
        == [Event(EventType.PLAYER_ADDED, "sendspin_web_3f2a")] * 3
    )


async def test_disabling_a_player_takes_it_out_and_enabling_brings_it_back(
    scenario: Scenario,
) -> None:
    """
    A disabled player leaves the players with a last update that it is unavailable.

    It is not announced as removed and stays listed in the settings, disabled, with its
    queue saved. Enabling it announces it as added under the same id, with that queue.
    """
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    await scenario.play(player_id)
    await scenario.idle()
    scenario.clear_events()

    await scenario.disable(player_id)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert [(c.player_id, c.enabled) for c in await scenario.player_configs()] == [
        (player_id, False)
    ]
    assert scenario.removals() == []
    # as on dev: the last update of the player, then the config event of the save
    assert scenario.events(
        EventType.PLAYER_UPDATED,
        EventType.PLAYER_CONFIG_UPDATED,
        EventType.PLAYER_REMOVED,
        object_id=player_id,
    )[-2:] == [
        Event(EventType.PLAYER_UPDATED, player_id),
        Event(EventType.PLAYER_CONFIG_UPDATED, player_id),
    ]
    assert scenario.availability_updates(player_id)[-1] is False
    scenario.clear_events()

    await scenario.enable(player_id)
    await scenario.idle()

    assert scenario.events(EventType.PLAYER_REMOVED, EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_ADDED, player_id),
    ]
    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [(player_id, True)]
    assert [item.uri for item in scenario.mass.player_queues.items(player_id)] == [TEST_TRACK_URI]


@pytest.mark.parametrize("power", ["native", "external", "none"])
async def test_the_last_update_of_a_disabled_player_says_it_is_disabled_and_unavailable(
    scenario: Scenario, power: str
) -> None:
    """
    A player that is disabled leaves with an update that it is disabled and unavailable.

    Also when its power control switched it off just before, which otherwise shows a player
    as available and powered off.
    """
    features = set(DEFAULT_FEATURES)
    if power == "native":
        features.add(PlayerFeature.POWER)
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol(
        "wiim_living", "Living", identifiers=LIVING, supported_features=features, powered=True
    )
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    if power == "external":
        plug = await scenario.add_power_control("switch.living_plug", "Living plug", powered=True)
        await scenario.configure_player(player_id, power_control=plug.control_id)
    await scenario.idle()
    scenario.clear_events()

    await scenario.disable(player_id)
    await scenario.idle()

    last = scenario.player_updates(player_id)[-1]
    assert (last.enabled, last.available) == (False, False)


async def test_a_browser_player_that_was_only_seen_is_removed_when_its_client_disconnects(
    scenario: Scenario,
) -> None:
    """
    A web player that was never changed nor played on is removed when its client disconnects.

    Nothing of it is listed anymore. When the client connects again it is announced as added
    under the same id.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(browser)
    await scenario.idle()
    scenario.clear_events()

    await scenario.unregister(browser)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []
    assert scenario.removals() == [("sendspin_web_3f2a", None)]

    await scenario.register(browser)
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [
        ("sendspin_web_3f2a", True)
    ]
    assert scenario.events(EventType.PLAYER_REMOVED, EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_REMOVED, "sendspin_web_3f2a"),
        Event(EventType.PLAYER_ADDED, "sendspin_web_3f2a"),
    ]


async def test_a_browser_player_that_played_stays_listed_while_its_browser_is_away(
    scenario: Scenario,
) -> None:
    """
    A web player that was played on stays a player, unavailable, while its client is away.

    Its client disconnects right after the play, before the queue was saved. Clients are told
    it is updated, not removed; the settings list it, also after a restart; when its client
    connects again it is available under the same id, with its queue.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(browser)
    await scenario.idle()
    scenario.clear_events()
    await scenario.play("sendspin_web_3f2a")

    await scenario.unregister(browser)
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [
        ("sendspin_web_3f2a", False)
    ]
    assert scenario.removals() == []
    assert scenario.availability_updates("sendspin_web_3f2a")[-1] is False
    assert [c.player_id for c in await scenario.player_configs()] == ["sendspin_web_3f2a"]

    await scenario.restart()
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [
        ("sendspin_web_3f2a", False)
    ]
    assert [c.player_id for c in await scenario.player_configs()] == ["sendspin_web_3f2a"]

    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [
        ("sendspin_web_3f2a", True)
    ]
    assert [item.uri for item in scenario.mass.player_queues.items("sendspin_web_3f2a")] == [
        TEST_TRACK_URI
    ]
    assert scenario.removals() == []


async def test_a_browser_player_whose_queue_was_cleared_is_removed_when_its_client_disconnects(
    scenario: Scenario,
) -> None:
    """
    A web player that played, but whose queue was cleared, has nothing to keep.

    It is removed for good when its client disconnects: nothing of its queue stays saved.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(browser)
    await scenario.play("sendspin_web_3f2a")
    await scenario.idle()
    scenario.mass.player_queues.clear("sendspin_web_3f2a")
    await scenario.idle()
    scenario.clear_events()

    await scenario.unregister(browser)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []
    assert scenario.removals() == [("sendspin_web_3f2a", None)]
    assert await scenario.saved_queue("sendspin_web_3f2a") is None


async def test_a_player_the_user_removes_is_announced_as_removed(scenario: Scenario) -> None:
    """A player the user removes is announced as removed, without data, as before the split."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(living)
    player_id = scenario.single_player(living).player_id
    scenario.clear_events()

    await scenario.remove(player_id)
    await scenario.idle()

    assert scenario.removals() == [(player_id, None)]


@pytest.mark.parametrize(
    "state", ["disabled", "provider_not_loaded", "unavailable", "browser_away"]
)
async def test_a_player_that_is_not_there_is_announced_once_when_it_is_removed(
    scenario: Scenario, state: str
) -> None:
    """
    A player that is disabled, away or without its provider is removed for good, announced once.

    Its record and its saved queue go with it.
    """
    wiim = await scenario.add_provider("wiim")
    sendspin = await scenario.add_provider("sendspin")
    if state == "browser_away":
        protocol = sendspin.protocol(
            "sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER
        )
    else:
        protocol = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.register(protocol)
    player_id = scenario.single_player(protocol).player_id
    await scenario.play(player_id)
    await scenario.idle()
    if state == "disabled":
        await scenario.disable(player_id)
    elif state == "provider_not_loaded":
        await scenario.mass.unload_provider(wiim.instance_id)
    else:
        await scenario.unregister(protocol)
    await scenario.idle()
    scenario.clear_events()

    if state == "unavailable":
        await scenario.advance(REMOVE_UNAVAILABLE_GRACE)
        assert await scenario.mass.players.remove_unavailable() == [player_id]
    else:
        await scenario.remove(player_id)
    await scenario.idle()

    assert scenario.removals() == [(player_id, None)]
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}") is None
    assert await scenario.saved_queue(player_id) is None


async def test_the_player_that_goes_in_a_merge_is_announced_as_removed(
    scenario: Scenario,
) -> None:
    """The player that goes in a merge is announced as removed."""
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
    await scenario.idle()
    keep_id = scenario.single_player(tv_airplay).player_id
    absorb_id = scenario.single_player(tv_cast).player_id
    scenario.clear_events()

    await scenario.mass.players.merge(absorb_id, keep_id)
    await scenario.idle()

    assert scenario.removals() == [(absorb_id, None)]


async def test_a_disabled_player_that_goes_in_a_merge_is_announced_as_removed(
    scenario: Scenario,
) -> None:
    """A disabled player merged into another one is gone for good: announced once, queue gone."""
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
    await scenario.idle()
    keep_id = scenario.single_player(tv_airplay).player_id
    absorb_id = scenario.single_player(tv_cast).player_id
    await scenario.play(absorb_id)
    await scenario.idle()
    await scenario.disable(absorb_id)
    await scenario.idle()
    scenario.clear_events()

    await scenario.mass.players.merge(absorb_id, keep_id)
    await scenario.idle()

    assert scenario.removals() == [(absorb_id, None)]
    assert scenario.mass.config.get(f"{CONF_PLAYERS}/{absorb_id}") is None
    assert await scenario.saved_queue(absorb_id) is None


# the stored config of a web player as written before the split, where every player was stored
PRE_SPLIT_WEB_PLAYER: dict[str, Any] = {
    "player_id": "sendspin_web_3f2a",
    "provider": "sendspin",
    "player_type": "player",
    "enabled": True,
    "default_name": "Web Browser",
    "values": {"sendspin_static_delay": 120},
}


@pytest.mark.parametrize(
    ("name", "stored_name"),
    [(None, None), ("Study browser", "Study browser")],
    ids=["uncustomised", "customised"],
)
async def test_pre_split_session_only_player_comes_back_with_its_id(
    scenario: Scenario, name: str | None, stored_name: str | None
) -> None:
    """
    A web player stored before the split has the same id when it next connects.

    Its config is only kept when the user customised it: then it is a player from the start,
    unavailable until its client connects, with its config.
    """
    sendspin = await scenario.add_provider("sendspin")
    sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)

    await scenario.seed_pre_split_config(
        {"sendspin_web_3f2a": PRE_SPLIT_WEB_PLAYER | ({"name": name} if name else {})}
    )
    await scenario.idle()

    assert [(p.player_id, p.name, p.available) for p in scenario.visible_players()] == (
        [("sendspin_web_3f2a", stored_name, False)] if stored_name else []
    )
    assert [(c.player_id, c.name) for c in await scenario.player_configs()] == (
        [("sendspin_web_3f2a", stored_name)] if stored_name else []
    )

    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [(player.player_id, player.name) for player in scenario.visible_players()] == [
        ("sendspin_web_3f2a", stored_name or "Web Browser")
    ]


async def test_a_pre_split_browser_player_with_a_saved_queue_is_kept_once_it_connects(
    scenario: Scenario,
) -> None:
    """
    A web player an older version stored without changes, whose queue was saved, loses nothing.

    The upgrade deletes its stored config, not its saved queue: when its client connects it
    is a player under the same id with that queue, and it stays when the client disconnects.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(browser)
    await scenario.play("sendspin_web_3f2a")
    await scenario.idle()
    await scenario.unregister(browser)
    await scenario.idle()
    await scenario.seed_config({CONF_PLAYERS: {"sendspin_web_3f2a": None}})
    await scenario.seed_pre_split_config({"sendspin_web_3f2a": PRE_SPLIT_WEB_PLAYER})
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []
    assert await scenario.saved_queue("sendspin_web_3f2a") == [TEST_TRACK_URI]

    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [item.uri for item in scenario.mass.player_queues.items("sendspin_web_3f2a")] == [
        TEST_TRACK_URI
    ]

    scenario.clear_events()
    await scenario.unregister(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [(p.player_id, p.available) for p in scenario.visible_players()] == [
        ("sendspin_web_3f2a", False)
    ]
    assert scenario.removals() == []


async def test_pre_split_browser_players_keep_the_defaults_of_their_client_from_the_start(
    scenario: Scenario,
) -> None:
    """
    A web player an older version stored and the upgrade keeps is hidden, not exposed, private.

    It is so right after the upgrade, before its client connected, and stays so when it
    does; a value the user set wins. One the user never touched is gone.
    """
    sendspin = await scenario.add_provider("sendspin")
    for client_id in ("sendspin_web_renamed", "sendspin_web_untouched", "sendspin_web_exposed"):
        sendspin.protocol(
            client_id,
            "Web Browser",
            session_only=True,
            expose_to_ha_by_default=False,
            **WEB_PLAYER,
        )
    await scenario.seed_pre_split_config(
        {
            "sendspin_web_renamed": PRE_SPLIT_WEB_PLAYER
            | {"player_id": "sendspin_web_renamed", "name": "Study browser"},
            "sendspin_web_untouched": PRE_SPLIT_WEB_PLAYER
            | {"player_id": "sendspin_web_untouched"},
            "sendspin_web_exposed": PRE_SPLIT_WEB_PLAYER
            | {
                "player_id": "sendspin_web_exposed",
                "values": {"sendspin_static_delay": 120, "expose_player_to_ha": True},
            },
        }
    )
    await scenario.idle()

    def _listed() -> list[tuple[Any, ...]]:
        return sorted(
            (p.player_id, p.available, *_defaults(scenario, p.player_id)[:3])
            for p in scenario.visible_players()
        )

    assert _listed() == [
        ("sendspin_web_exposed", False, True, True, True),
        ("sendspin_web_renamed", False, True, False, True),
    ]
    assert sorted(c.player_id for c in await scenario.player_configs()) == [
        "sendspin_web_exposed",
        "sendspin_web_renamed",
    ]

    for client_id in ("sendspin_web_renamed", "sendspin_web_exposed"):
        await scenario.register(scenario.protocol(client_id))
    await scenario.idle()

    assert _listed() == [
        ("sendspin_web_exposed", True, True, True, True),
        ("sendspin_web_renamed", True, True, False, True),
    ]


async def test_a_stored_player_with_part_of_its_defaults_has_the_generic_rest(
    scenario: Scenario,
) -> None:
    """A kept web player whose record holds only some of its defaults has the others generic."""
    sendspin = await scenario.add_provider("sendspin")
    sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.seed_pre_split_config(
        {
            "sendspin_web_3f2a": PRE_SPLIT_WEB_PLAYER
            | {
                "name": "Study browser",
                "values": {"protocol_defaults": {"hidden_by_default": True}},
            }
        }
    )
    await scenario.idle()

    assert _defaults(scenario, "sendspin_web_3f2a")[:3] == (True, True, False)


async def test_pre_split_session_only_player_the_user_disabled_stays_disabled(
    scenario: Scenario,
) -> None:
    """A web player the user disabled before the split is still disabled when it next connects."""
    sendspin = await scenario.add_provider("sendspin")
    sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)

    await scenario.seed_pre_split_config(
        {"sendspin_web_3f2a": PRE_SPLIT_WEB_PLAYER | {"enabled": False}}
    )
    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.events(EventType.PLAYER_ADDED) == []
    assert [(c.player_id, c.enabled) for c in await scenario.player_configs()] == [
        ("sendspin_web_3f2a", False)
    ]


# the stored config of a protocol-only player as written before the split: a universal player
# config and the configs of its protocols pointing at it
PRE_SPLIT_DEN_TV: dict[str, dict[str, Any]] = {
    "upf3eba71c": {
        "player_id": "upf3eba71c",
        "provider": "universal_player",
        "player_type": "player",
        "enabled": True,
        "name": "Den TV",
        "default_name": "Den",
        "values": {
            "linked_protocol_ids": ["airplay_den", "cast_den"],
            "created_at": 1794243987775090000,
            "device_identifiers": {
                "mac_address": "00:11:22:33:44:55",
                "ip_address": "192.168.1.30",
            },
            "device_info": {"model": "Fake airplay", "manufacturer": "Fake"},
        },
    },
    "airplay_den": {
        "player_id": "airplay_den",
        "provider": "airplay",
        "player_type": "protocol",
        "enabled": True,
        "default_name": "Den",
        "values": {"protocol_parent_id": "upf3eba71c"},
    },
    "cast_den": {
        "player_id": "cast_den",
        "provider": "chromecast",
        "player_type": "protocol",
        "enabled": True,
        "default_name": "Den",
        "values": {"protocol_parent_id": "upf3eba71c"},
    },
}
# the same config as an interrupted shutdown leaves it: the parent's protocol list never written
HALF_WRITTEN_DEN_TV = deepcopy(PRE_SPLIT_DEN_TV)
del HALF_WRITTEN_DEN_TV["upf3eba71c"]["values"]["linked_protocol_ids"]


@pytest.mark.parametrize(
    "stored", [PRE_SPLIT_DEN_TV, HALF_WRITTEN_DEN_TV], ids=["complete", "half_written"]
)
async def test_pre_split_universal_player_config_comes_back_as_the_same_player(
    scenario: Scenario, stored: dict[str, dict[str, Any]]
) -> None:
    """
    A universal player config from before the split boots as the player with its id and name.

    Also a half-written one: the boot never fails on it, and the player comes back.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)
    scenario.set_present("airplay_den", True)
    scenario.set_present("cast_den", True)

    await scenario.seed_pre_split_config(stored)
    await scenario.idle()

    assert scenario.visible_players() == [
        VisiblePlayer(
            player_id="upf3eba71c",
            name="Den TV",
            available=True,
            powered=None,
            provider=ANY,
            outputs=(("airplay", True), ("chromecast", True)),
        )
    ]


async def test_protocol_only_player_is_restored_with_its_id_after_a_restart(
    scenario: Scenario,
) -> None:
    """A player without a native protocol comes back after a restart with its id, name and protocols."""
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)
    await scenario.register(den_airplay)
    await scenario.register(den_cast)
    await scenario.advance(15)
    player_id = scenario.single_player(den_airplay, den_cast).player_id
    await scenario.configure_player(player_id, name="Den TV")
    await scenario.idle()

    await scenario.restart()
    await scenario.idle()

    assert scenario.visible_players() == [
        VisiblePlayer(
            player_id=player_id,
            name="Den TV",
            available=True,
            powered=None,
            provider=ANY,
            outputs=(("airplay", True), ("chromecast", True)),
        )
    ]
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()
    den_airplay, den_cast = scenario.protocol("airplay_den"), scenario.protocol("cast_den")
    assert ("play_media", MEDIA) in den_airplay.commands + den_cast.commands


async def test_a_player_whose_own_protocol_was_unlinked_stays_at_the_next_start(
    scenario: Scenario,
) -> None:
    """
    A player from before the split whose own protocol got a player of its own starts as it was.

    It keeps its id, name and queue settings, and its other protocol attaches to it.
    """
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    living_airplay = airplay.protocol("airplay_living", "Living", role="audio", identifiers=LIVING)
    scenario.set_present("wiim_living", True)
    scenario.set_present("airplay_living", True)
    queue_values = {"crossfade": True}

    await scenario.seed_config(
        {
            CONF_PLAYERS: {
                "wiim_living": {
                    "player_id": "wiim_living",
                    "provider": "wiim",
                    "player_type": "player",
                    "enabled": True,
                    "name": "Living Room",
                    "default_name": "Living",
                    "values": {},
                },
                "pl0a1b2c3d4e5f": {
                    "player_id": "pl0a1b2c3d4e5f",
                    "provider": "wiim",
                    "player_type": "player",
                    "enabled": True,
                    "default_name": "Living",
                    "values": {},
                },
            },
            CONF_PLAYER_PROTOCOLS: {
                "wiim_living": {
                    "protocol_id": "wiim_living",
                    "provider": "wiim",
                    "player_id": "pl0a1b2c3d4e5f",
                    "role": "native",
                    "enabled": True,
                    "default_name": "Living",
                    # as the unlink stored it, so the two are not merged back by themselves
                    "values": {CONF_UNLINKED_FROM: "wiim_living"},
                },
                "airplay_living": {
                    "protocol_id": "airplay_living",
                    "provider": "airplay",
                    "player_id": "wiim_living",
                    "role": "audio",
                    "enabled": True,
                    "default_name": "Living",
                    "values": {},
                },
            },
            CONF_PLAYER_QUEUES: {"wiim_living": {"values": queue_values}},
        }
    )
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.player_of(living_airplay) == "wiim_living"
    assert scenario.player_of(living) == "pl0a1b2c3d4e5f"
    assert ("wiim_living", "Living Room") in [
        (player.player_id, player.name) for player in scenario.visible_players()
    ]
    assert scenario.mass.config.get(f"{CONF_PLAYER_QUEUES}/wiim_living/values") == queue_values


def _older_version_record(
    player_id: str, provider: str, player_type: str = "protocol", **values: Any
) -> dict[str, Any]:
    """Return a player config as a version from before the split stores it."""
    return {
        "player_id": player_id,
        "provider": provider,
        "player_type": player_type,
        "enabled": True,
        "default_name": player_id,
        "values": values,
    }


async def test_what_an_older_version_stored_goes_at_the_next_start(scenario: Scenario) -> None:
    """
    A split config that an older version ran on again starts with exactly its own players.

    A device the older version saw first is found again, and a browser that connects
    afterwards has its client id as player id.
    """
    wiim = await scenario.add_provider("wiim")
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    sendspin = await scenario.add_provider("sendspin")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    living_airplay = airplay.protocol("airplay_living", "Living", role="audio", identifiers=LIVING)
    den_airplay = airplay.protocol("airplay_den", "Den", role="audio", identifiers=DEN)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=DEN)
    for protocol in (living, living_airplay, den_airplay, den_cast):
        await scenario.register(protocol)
    await scenario.advance(15)
    await scenario.idle()
    living_id = scenario.single_player(living, living_airplay).player_id
    den_id = scenario.single_player(den_airplay, den_cast).player_id
    tv = airplay.protocol("airplay_tv", "TV", role="audio", identifiers=TV)
    scenario.set_present("airplay_tv", True)
    sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    scenario.expect_log("player records that an older version of Music Assistant stored")

    await scenario.seed_config(
        {
            CONF_PLAYERS: {
                "airplay_living": _older_version_record(
                    "airplay_living", "airplay", protocol_parent_id=living_id
                ),
                "airplay_den": _older_version_record(
                    "airplay_den", "airplay", protocol_parent_id="upa1b2c3d4"
                ),
                "cast_den": _older_version_record("cast_den", "chromecast", "player"),
                "upa1b2c3d4": _older_version_record(
                    "upa1b2c3d4",
                    "universal_player",
                    "player",
                    linked_protocol_ids=["airplay_den", "cast_den"],
                ),
                "airplay_tv": _older_version_record(
                    "airplay_tv", "airplay", protocol_parent_id="upb2c3d4e5"
                ),
                "upb2c3d4e5": _older_version_record(
                    "upb2c3d4e5", "universal_player", "player", linked_protocol_ids=["airplay_tv"]
                ),
                "sendspin_web_3f2a": PRE_SPLIT_WEB_PLAYER,
            }
        }
    )
    await scenario.advance(15)
    await scenario.idle()

    tv_id = scenario.single_player(tv).player_id
    assert sorted((player.player_id, player.name) for player in scenario.visible_players()) == (
        sorted([(living_id, "Living"), (den_id, "Den"), (tv_id, "TV")])
    )
    assert scenario.player_of(scenario.protocol("airplay_living")) == living_id
    assert scenario.player_of(scenario.protocol("cast_den")) == den_id
    assert sorted(config.player_id for config in await scenario.player_configs()) == sorted(
        [living_id, den_id, tv_id]
    )

    await scenario.register(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert scenario.player_of(scenario.protocol("sendspin_web_3f2a")) == "sendspin_web_3f2a"


def _stored_player(player_id: str, provider: str, name: str) -> dict[str, Any]:
    """Return the stored config of a player of this version that has no protocol stored."""
    return {
        "player_id": player_id,
        "provider": provider,
        "player_type": "player",
        "enabled": True,
        "default_name": name,
        "values": {},
    }


async def test_a_stored_player_whose_provider_is_not_installed_says_so(
    scenario: Scenario,
) -> None:
    """
    A stored player of a provider this install does not have is listed, and says why.

    It is unavailable with the setup reason that its provider is not installed. A player of a
    provider that is installed but not set up is unavailable without that reason.
    """
    await scenario.seed_config(
        {
            CONF_PLAYERS: {
                "pl0a1b2c3d4e5f": _stored_player("pl0a1b2c3d4e5f", "retired_speakers", "Patio"),
                "pl1b2c3d4e5f6a": _stored_player("pl1b2c3d4e5f6a", "sonos", "Kitchen"),
            }
        }
    )
    await scenario.idle()

    assert [(p.player_id, p.name, p.available) for p in scenario.visible_players()] == [
        ("pl1b2c3d4e5f6a", "Kitchen", False),
        ("pl0a1b2c3d4e5f", "Patio", False),
    ]
    not_installed = scenario.player("pl0a1b2c3d4e5f")
    assert not_installed is not None
    assert not_installed.setup_reason == SETUP_REASON_PROVIDER_NOT_INSTALLED
    not_loaded = scenario.player("pl1b2c3d4e5f6a")
    assert not_loaded is not None
    assert not_loaded.setup_reason is None


def _stored_protocol(protocol_id: str, provider: str, player_id: str, role: str) -> dict[str, Any]:
    """Return the stored config of a protocol of a stored player."""
    return {
        "protocol_id": protocol_id,
        "provider": provider,
        "player_id": player_id,
        "role": role,
        "enabled": True,
        "default_name": "Patio",
        "values": {},
    }


async def test_the_settings_of_a_player_whose_provider_is_not_installed_can_be_read(
    scenario: Scenario,
) -> None:
    """
    The settings of a stored player open while the providers of its protocols are not installed.

    Its outputs are listed under the domain of their provider, for lack of a name.
    """
    await scenario.seed_config(
        {
            CONF_PLAYERS: {
                "pl0a1b2c3d4e5f": _stored_player("pl0a1b2c3d4e5f", "retired_speakers", "Patio"),
            },
            CONF_PLAYER_PROTOCOLS: {
                "retired_patio": _stored_protocol(
                    "retired_patio", "retired_speakers", "pl0a1b2c3d4e5f", "native"
                ),
                "cast_patio": _stored_protocol(
                    "cast_patio", "retired_cast", "pl0a1b2c3d4e5f", "audio"
                ),
            },
        }
    )
    await scenario.idle()

    config = await scenario.player_config("pl0a1b2c3d4e5f")

    assert config is not None
    outputs = config.values[CONF_PREFERRED_OUTPUT_PROTOCOL].options
    assert [(option.title, option.value) for option in outputs if option.value != "auto"] == [
        ("RETIRED_SPEAKERS (native)", "native"),
        ("RETIRED_CAST", "cast_patio"),
    ]
    assert [listed.player_id for listed in await scenario.player_configs()] == ["pl0a1b2c3d4e5f"]


async def test_a_stored_player_follows_a_provider_installed_or_removed_while_the_server_runs(
    scenario: Scenario, tmp_path: Path
) -> None:
    """
    A provider that is installed from a second folder counts as installed, like a shipped one.

    The stored Patio player says its provider is not installed. Once the provider is
    installed, without a restart, Patio is unavailable without that reason, as the player of
    a shipped provider that is not set up. It says so again once the provider is removed.
    """
    provider_dir = tmp_path / "community_providers" / "garden_speakers"
    provider_dir.mkdir(parents=True)
    (provider_dir / "manifest.json").write_text(
        json.dumps(
            {
                "type": "player",
                "domain": "garden_speakers",
                "name": "Garden Speakers",
                "description": "A provider that is installed later",
                "codeowners": [],
            }
        )
    )
    await scenario.seed_config(
        {
            CONF_PLAYERS: {
                "pl0a1b2c3d4e5f": _stored_player("pl0a1b2c3d4e5f", "garden_speakers", "Patio"),
            },
            CONF_PLAYER_PROTOCOLS: {
                "garden_patio": _stored_protocol(
                    "garden_patio", "garden_speakers", "pl0a1b2c3d4e5f", "native"
                ),
            },
        }
    )
    await scenario.idle()

    def _patio() -> tuple[bool, str | None]:
        state = scenario.player("pl0a1b2c3d4e5f")
        assert state is not None
        return state.available, state.setup_reason

    assert _patio() == (False, SETUP_REASON_PROVIDER_NOT_INSTALLED)

    await scenario.mass.register_provider_manifest(str(provider_dir), ProviderTier.COMMUNITY)
    await scenario.idle()

    assert _patio() == (False, None)
    config = await scenario.player_config("pl0a1b2c3d4e5f")
    assert config is not None
    outputs = config.values[CONF_PREFERRED_OUTPUT_PROTOCOL].options
    assert [option.title for option in outputs if option.value == "native"] == [
        "Garden Speakers (native)"
    ]

    await scenario.mass.unregister_provider_manifest("garden_speakers")
    await scenario.idle()

    assert _patio() == (False, SETUP_REASON_PROVIDER_NOT_INSTALLED)


async def test_a_browser_player_is_offered_to_a_plugin_only_while_it_is_its_choice(
    scenario: Scenario,
) -> None:
    """
    The player pickers a plugin builds its settings with offer no browser player.

    A browser player that is the plugin's choice already stays offered, so saving the form
    keeps it.
    """
    wiim = await scenario.add_provider("wiim")
    sendspin = await scenario.add_provider("sendspin")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.register(living)
    await scenario.register(browser)
    await scenario.idle()
    living_id = scenario.single_player(living).player_id

    def _offered(entry: ConfigEntry) -> list[str]:
        return [str(option.value) for option in entry.options]

    assert _offered(create_player_selector(scenario.mass, "player")) == [living_id]
    assert _offered(create_connected_players_entry(scenario.mass)) == [living_id]
    assert _offered(create_player_selector(scenario.mass, "player", "sendspin_web_3f2a")) == [
        living_id,
        "sendspin_web_3f2a",
    ]
    assert _offered(create_connected_players_entry(scenario.mass, ["sendspin_web_3f2a"])) == [
        living_id,
        "sendspin_web_3f2a",
    ]
