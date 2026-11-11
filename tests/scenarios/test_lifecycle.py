"""When a player comes into being and when it goes: providers, disabled devices, sessions, restarts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import ANY

import pytest
from music_assistant_models.enums import EventType, IdentifierType
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_PLAYER_SPLIT_SESSION_ONLY, CONF_PLAYER_SPLIT_TREE

from .harness import Event, Role, Scenario, VisiblePlayer

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
        await scenario.attach(protocol)
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
    await scenario.attach(living)
    player_id = scenario.single_player(living).player_id
    scenario.clear_events()

    await scenario.remove_provider(wiim)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert scenario.queue(player_id) is None
    assert await scenario.player_config(player_id) is None
    assert scenario.events(EventType.PLAYER_REMOVED) == [Event(EventType.PLAYER_REMOVED, player_id)]


async def test_removed_player_of_a_present_device_comes_back_as_new(scenario: Scenario) -> None:
    """A removed player whose device is still there comes back as a new player with its defaults."""
    wiim = await scenario.add_provider("wiim")
    living = wiim.protocol("wiim_living", "Living", identifiers=LIVING)
    await scenario.attach(living)
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
    assert scenario.events(EventType.PLAYER_REMOVED, EventType.PLAYER_ADDED) == [
        Event(EventType.PLAYER_REMOVED, player_id),
        Event(EventType.PLAYER_ADDED, scenario.single_player(current).player_id),
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
    await scenario.attach(tv)
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
    await scenario.attach(browser)
    await scenario.idle()
    player_id = scenario.single_player(browser).player_id

    assert await _stored_mentions(scenario, player_id, "sendspin_web_3f2a") == []

    # the page reloads: a new session connects with the same client id
    reloaded = sendspin.protocol(
        "sendspin_web_3f2a", "Web Browser", session_only=True, volume_level=35, **WEB_PLAYER
    )
    await scenario.attach(reloaded)
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.settle()

    assert [player.player_id for player in scenario.players_carrying(reloaded)] == [player_id]
    player = scenario.player(player_id)
    assert player is not None
    assert player.volume_level == 35
    assert ("play_media", MEDIA) in reloaded.commands
    assert browser.commands == []

    await scenario.detach(reloaded)
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await _stored_mentions(scenario, player_id, "sendspin_web_3f2a") == []

    await scenario.restart()
    await scenario.idle()

    assert scenario.visible_players() == []
    assert await scenario.player_configs() == []


async def test_customised_session_only_player_keeps_its_config(scenario: Scenario) -> None:
    """
    A session-only player the user customised keeps its config while it is away.

    It comes back with the config, and still exists only while attached.
    """
    sendspin = await scenario.add_provider("sendspin")
    browser = sendspin.protocol("sendspin_web_3f2a", "Web Browser", session_only=True, **WEB_PLAYER)
    await scenario.attach(browser)
    player_id = scenario.single_player(browser).player_id
    await scenario.configure_player(player_id, name="Marcel's browser")

    await scenario.detach(browser)
    await scenario.restart()
    await scenario.idle()

    assert scenario.visible_players() == []
    assert [(c.player_id, c.name) for c in await scenario.player_configs()] == [
        (player_id, "Marcel's browser")
    ]

    await scenario.attach(scenario.protocol("sendspin_web_3f2a"))
    await scenario.idle()

    assert [(p.player_id, p.name) for p in scenario.visible_players()] == [
        (player_id, "Marcel's browser")
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

    # a settings file from before the split has not been through its migration
    not_migrated = {CONF_PLAYER_SPLIT_SESSION_ONLY: None, CONF_PLAYER_SPLIT_TREE: None}
    await scenario.seed_config({"players": stored, **not_migrated})
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
    await scenario.attach(den_airplay)
    await scenario.attach(den_cast)
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
