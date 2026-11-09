"""Which protocols end up on which player: identifiers, zones, claimed instances, roles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import ANY

import pytest
from music_assistant_models.enums import EventType, IdentifierType
from music_assistant_models.player import PlayerMedia

from .harness import Role, Scenario, ScenarioError, VisiblePlayer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .harness import FakeProtocol

# a Raspberry Pi running two shairport-sync receivers, one per amplifier
PI = {IdentifierType.MAC_ADDRESS: "B8:27:EB:11:22:33", IdentifierType.IP_ADDRESS: "192.168.1.40"}
# a multi-zone amplifier: one chassis, one network interface
CHASSIS = {
    IdentifierType.MAC_ADDRESS: "00:1A:2B:3C:4D:5E",
    IdentifierType.IP_ADDRESS: "192.168.1.50",
}
# an ESP32 speaker, identified by its MAC under any firmware
ESP32 = {IdentifierType.MAC_ADDRESS: "A0:B7:65:00:11:22", IdentifierType.IP_ADDRESS: "192.168.1.70"}
# a TV with a Cast receiver and an AirPlay receiver
TV_IP = "192.168.1.60"
TV_CAST = {
    IdentifierType.CAST_UUID: "6f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b",
    IdentifierType.IP_ADDRESS: TV_IP,
}
TV_AIRPLAY_ID = "C0:FF:EE:00:00:01"
# the Raumfeld host and the renderers of its room
HOST_IP = "192.168.1.80"
CONNECTOR_UDN = "uuid:raumfeld-connector-0001"
ONE_M_UDN = "uuid:raumfeld-one-m-0002"
ONE_S_UDN = "uuid:raumfeld-one-s-0003"

MEDIA = PlayerMedia(uri="http://ma.local/stream/track.flac")


async def test_airplay_receivers_on_one_host_stay_separate_players(scenario: Scenario) -> None:
    """
    Two AirPlay receivers on one host (same IP and MAC, different AirPlay ids) are two players.

    A differing strong identifier vetoes a match before the MAC or IP fallback, also while the
    first receiver is away and the player it left behind has no protocol attached.
    """
    airplay = await scenario.add_provider("airplay")
    kitchen = airplay.protocol(
        "airplay_kitchen",
        "Kitchen",
        role="audio",
        identifiers={**PI, IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:01"},
    )
    den = airplay.protocol(
        "airplay_den",
        "Den",
        role="audio",
        identifiers={**PI, IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:02"},
    )
    await scenario.attach(kitchen)
    await scenario.advance(15)
    kitchen_id = scenario.single_player(kitchen).player_id
    await scenario.detach(kitchen)

    await scenario.attach(den)
    await scenario.advance(15)
    await scenario.attach(kitchen)
    await scenario.idle()

    assert scenario.players_carrying(kitchen) == [
        VisiblePlayer(
            player_id=kitchen_id,
            name="Kitchen",
            available=True,
            powered=None,
            provider=ANY,
            outputs=(("airplay", True),),
        )
    ]
    assert scenario.players_carrying(den) == [
        VisiblePlayer(
            player_id=ANY,
            name="Den",
            available=True,
            powered=None,
            provider=ANY,
            outputs=(("airplay", True),),
        )
    ]
    assert len(scenario.visible_players()) == 2


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, AttributeError),
    reason="phase 2 (#416): a zone identifier tells the zones of one chassis apart",
)
async def test_zones_of_one_chassis_are_one_player_each(scenario: Scenario) -> None:
    """
    The zones of a multi-zone device behind one MAC and IP are one player each.

    The audio protocols of a zone land on that zone's player. The zone identifier type does
    not exist yet.
    """
    bluesound = await scenario.add_provider("bluesound")
    airplay = await scenario.add_provider("airplay")
    zone = IdentifierType.ZONE  # type: ignore[attr-defined]
    zone_1 = bluesound.protocol("bluesound_zone_1", "Zone 1", identifiers={**CHASSIS, zone: "1"})
    zone_2 = bluesound.protocol("bluesound_zone_2", "Zone 2", identifiers={**CHASSIS, zone: "2"})
    airplay_1 = airplay.protocol(
        "airplay_zone_1",
        "Zone 1",
        role="audio",
        identifiers={**CHASSIS, IdentifierType.AIRPLAY_ID: "00:1A:2B:3C:4D:01", zone: "1"},
    )
    airplay_2 = airplay.protocol(
        "airplay_zone_2",
        "Zone 2",
        role="audio",
        identifiers={**CHASSIS, IdentifierType.AIRPLAY_ID: "00:1A:2B:3C:4D:02", zone: "2"},
    )
    await scenario.attach(zone_1)
    await scenario.attach(zone_2)

    # the receiver of zone 2 is found first: on the chassis identifiers alone it lands on zone 1
    await scenario.attach(airplay_2)
    await scenario.attach(airplay_1)
    await scenario.idle()

    assert scenario.players_carrying(zone_1, airplay_1) == [
        VisiblePlayer(
            player_id=ANY,
            name="Zone 1",
            available=True,
            powered=None,
            provider="bluesound",
            outputs=(("airplay", True), ("bluesound", True)),
        )
    ]
    assert scenario.players_carrying(zone_2, airplay_2) == [
        VisiblePlayer(
            player_id=ANY,
            name="Zone 2",
            available=True,
            powered=None,
            provider="bluesound",
            outputs=(("airplay", True), ("bluesound", True)),
        )
    ]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): a native protocol claims the further instances of one domain",
)
async def test_room_of_several_renderers_is_one_player(scenario: Scenario) -> None:
    """
    A native room standing for three DLNA renderers is one visible player with one DLNA output.

    The renderers the room stands for are attached hidden, so none of them is a player of its own.
    """
    dlna = await scenario.add_provider("dlna")
    raumfeld = await scenario.add_provider("raumfeld")
    connector = dlna.protocol(
        CONNECTOR_UDN,
        "Connector Living",
        role="audio",
        identifiers={
            IdentifierType.UUID: CONNECTOR_UDN,
            IdentifierType.MAC_ADDRESS: "00:1E:C0:00:00:01",
            IdentifierType.IP_ADDRESS: HOST_IP,
        },
    )
    one_m = dlna.protocol(
        ONE_M_UDN,
        "Speaker Living",
        role="audio",
        identifiers={
            IdentifierType.UUID: ONE_M_UDN,
            IdentifierType.MAC_ADDRESS: "00:1E:C0:00:00:02",
            IdentifierType.IP_ADDRESS: "192.168.1.81",
        },
    )
    one_s = dlna.protocol(
        ONE_S_UDN,
        "Speaker Living",
        role="audio",
        identifiers={
            IdentifierType.UUID: ONE_S_UDN,
            IdentifierType.MAC_ADDRESS: "00:1E:C0:00:00:03",
            IdentifierType.IP_ADDRESS: "192.168.1.82",
        },
    )
    # the room is controlled through the host and stands for every renderer in it
    living = raumfeld.protocol(
        "raumfeld_living",
        "Living",
        identifiers={
            IdentifierType.UUID: [CONNECTOR_UDN, ONE_M_UDN, ONE_S_UDN],
            IdentifierType.IP_ADDRESS: HOST_IP,
        },
    )
    for renderer in (connector, one_m, one_s):
        await scenario.attach(renderer)

    await scenario.attach(living)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.visible_players() == [
        VisiblePlayer(
            player_id=ANY,
            name="Living",
            available=True,
            powered=None,
            provider="raumfeld",
            outputs=(("dlna", True), ("raumfeld", True)),
        )
    ]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): a protocol matching a player without one of its domain attaches to it",
)
async def test_reflashed_device_keeps_its_player(scenario: Scenario) -> None:
    """
    A device re-flashed to another firmware with the same MAC keeps its player.

    The new protocol attaches to it, the id and the custom name stay, and the stale protocol
    is gone from its outputs.
    """
    squeezelite = await scenario.add_provider("squeezelite")
    sendspin = await scenario.add_provider("sendspin")
    esp_squeezelite = squeezelite.protocol("a0:b7:65:00:11:22", "ESP32 Loud", identifiers=ESP32)
    esp_sendspin = sendspin.protocol(
        "sendspin_esp32_loud", "esp32-loud", role="audio", identifiers=ESP32
    )
    await scenario.attach(esp_squeezelite)
    player_id = scenario.single_player(esp_squeezelite).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker")
    await scenario.detach(esp_squeezelite)

    await scenario.attach(esp_sendspin)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.players_carrying(esp_sendspin) == [
        VisiblePlayer(
            player_id=player_id,
            name="Kitchen Speaker",
            available=True,
            powered=None,
            provider="sendspin",
            outputs=(("sendspin", True),),
        )
    ]


@pytest.mark.parametrize(
    ("first_role", "second_role"),
    [("audio", "native"), ("native", "audio")],
    ids=["audio_to_native", "native_to_audio"],
)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): a role change is a re-attach, the universal takeover goes",
)
async def test_role_flip_of_a_protocol_keeps_its_player(
    scenario: Scenario, first_role: Role, second_role: Role
) -> None:
    """A protocol that attaches again in another role stays on its player: no second one, no new id."""
    chromecast = await scenario.add_provider("chromecast")
    tv = chromecast.protocol("cast_tv", "TV", role=first_role, identifiers=TV_CAST)
    await scenario.attach(tv)
    await scenario.advance(15)
    player_id = scenario.single_player(tv).player_id
    scenario.clear_events()

    await scenario.attach(tv, role=second_role)
    await scenario.advance(15)
    await scenario.idle()

    assert [player.player_id for player in scenario.players_carrying(tv)] == [player_id]
    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []


async def _two_players_for_one_tv(
    scenario: Scenario,
) -> tuple[FakeProtocol, FakeProtocol, str, str]:
    """
    Let the AirPlay and the Cast receiver of one TV end up as two players, the AirPlay one older.

    The AirPlay receiver advertises no usable address at first, so nothing ties the two together.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    tv_airplay = airplay.protocol(
        "airplay_tv", "TV", role="audio", identifiers={IdentifierType.AIRPLAY_ID: TV_AIRPLAY_ID}
    )
    tv_cast = chromecast.protocol("cast_tv", "TV", role="audio", identifiers=TV_CAST)
    await scenario.attach(tv_airplay)
    await scenario.advance(15)
    older_id = scenario.single_player(tv_airplay).player_id
    await scenario.attach(tv_cast)
    await scenario.advance(15)
    younger_id = scenario.single_player(tv_cast).player_id
    if younger_id == older_id:
        msg = "the receivers were matched at first sight"
        raise ScenarioError(msg)
    return tv_airplay, tv_cast, older_id, younger_id


async def _seen_again_with_its_address(scenario: Scenario, protocol: FakeProtocol) -> None:
    """Let the receiver report its address and be found again by its provider."""
    await scenario.report(protocol, identifiers={IdentifierType.IP_ADDRESS: TV_IP})
    await scenario.detach(protocol)
    await scenario.attach(protocol)
    await scenario.idle()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): the id of a merged player becomes a legacy id",
)
async def test_unused_duplicate_is_merged_into_the_older_player(scenario: Scenario) -> None:
    """
    Two players for one device merge into the older one once their identifiers match.

    The identifiers were incomplete at first sight. The younger player is unused, so it is
    merged by itself, and its id keeps resolving to the player.
    """
    tv_airplay, tv_cast, older_id, younger_id = await _two_players_for_one_tv(scenario)

    await _seen_again_with_its_address(scenario, tv_airplay)

    assert scenario.players_carrying(tv_airplay, tv_cast) == [
        VisiblePlayer(
            player_id=older_id,
            name="TV",
            available=True,
            powered=None,
            provider=ANY,
            outputs=(("airplay", True), ("chromecast", True)),
        )
    ]
    resolved = scenario.player(younger_id)
    assert resolved is not None
    assert resolved.player_id == older_id


async def _played(scenario: Scenario, player_id: str) -> None:
    """Play something on the player and stop it again."""
    await scenario.mass.players.play_media(player_id, MEDIA)
    await scenario.mass.players.cmd_stop(player_id)
    await scenario.settle()


async def _renamed(scenario: Scenario, player_id: str) -> None:
    """Give the player a custom name."""
    await scenario.configure_player(player_id, name="Kitchen TV")


@pytest.mark.parametrize("used", [_played, _renamed], ids=["played", "renamed"])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): only an unused duplicate is merged by itself",
)
async def test_used_duplicate_is_not_merged(
    scenario: Scenario, used: Callable[[Scenario, str], Awaitable[None]]
) -> None:
    """A younger duplicate that has played or was customised stays a player of its own."""
    tv_airplay, tv_cast, older_id, younger_id = await _two_players_for_one_tv(scenario)
    await used(scenario, younger_id)

    await _seen_again_with_its_address(scenario, tv_airplay)

    assert [player.player_id for player in scenario.players_carrying(tv_airplay)] == [older_id]
    assert [player.player_id for player in scenario.players_carrying(tv_cast)] == [younger_id]
