"""Which protocols end up on which player: identifiers, zones, claimed instances, roles."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import ANY

import pytest
from music_assistant_models.enums import EventType, IdentifierType

from .harness import FAKE_SETTING, Event, Role, Scenario, ScenarioError, VisiblePlayer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .harness import FakePlayerProvider, FakeProtocol

# a Raspberry Pi running two receivers of one protocol, one per amplifier
PI = {IdentifierType.MAC_ADDRESS: "B8:27:EB:11:22:33", IdentifierType.IP_ADDRESS: "192.168.1.40"}
# a multi-zone amplifier: one chassis, one network interface
CHASSIS = {
    IdentifierType.MAC_ADDRESS: "00:1A:2B:3C:4D:5E",
    IdentifierType.IP_ADDRESS: "192.168.1.50",
}
# an ESP32 speaker, identified by its MAC under any firmware
ESP32 = {IdentifierType.MAC_ADDRESS: "A0:B7:65:00:11:22", IdentifierType.IP_ADDRESS: "192.168.1.70"}


class _Tv(NamedTuple):
    """A TV with a Cast receiver and an AirPlay receiver, by what they report."""

    name: str
    ip: str
    cast_uuid: str
    airplay_id: str


LIVING_TV = _Tv(
    "Living TV", "192.168.1.60", "6f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b", "C0:FF:EE:00:00:01"
)
BEDROOM_TV = _Tv(
    "Bedroom TV", "192.168.1.61", "9d8e7f6a-5b4c-4d3e-af21-0b9c8d7e6f5a", "C0:FF:EE:00:00:02"
)
# the Raumfeld host and the renderers of its room
HOST_IP = "192.168.1.80"
CONNECTOR_UDN = "uuid:raumfeld-connector-0001"
ONE_M_UDN = "uuid:raumfeld-one-m-0002"
ONE_S_UDN = "uuid:raumfeld-one-s-0003"


@pytest.mark.parametrize(
    ("domain", "kitchen_ids", "den_ids"),
    [
        pytest.param(
            "airplay",
            {IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:01"},
            {IdentifierType.AIRPLAY_ID: "AA:AA:AA:AA:AA:02"},
            id="airplay_id",
        ),
        pytest.param(
            "dlna",
            {IdentifierType.UUID: "uuid:gmrender-0001"},
            {IdentifierType.UUID: "uuid:gmrender-0002"},
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="phase 2 (#416): a differing UUID vetoes a match like an AirPlay id does",
            ),
            id="uuid",
        ),
    ],
)
async def test_receivers_on_one_host_stay_separate_players(
    scenario: Scenario,
    domain: str,
    kitchen_ids: dict[IdentifierType, str],
    den_ids: dict[IdentifierType, str],
) -> None:
    """
    Two receivers of one protocol on one host (same IP and MAC, different strong ids) are two players.

    A differing strong identifier vetoes a match before the MAC or IP fallback, also while the
    first receiver is away and the player it left behind has no protocol attached.
    """
    provider = await scenario.add_provider(domain)
    kitchen = provider.protocol(
        f"{domain}_kitchen", "Kitchen", role="audio", identifiers={**PI, **kitchen_ids}
    )
    den = provider.protocol(f"{domain}_den", "Den", role="audio", identifiers={**PI, **den_ids})
    await scenario.attach(kitchen)
    await scenario.advance(15)
    kitchen_id = scenario.single_player(kitchen).player_id
    await scenario.detach(kitchen)

    await scenario.attach(den)
    await scenario.advance(15)
    await scenario.attach(kitchen)
    await scenario.advance(15)
    await scenario.idle()

    assert scenario.players_carrying(kitchen) == [
        VisiblePlayer(
            player_id=kitchen_id,
            name="Kitchen",
            available=True,
            powered=None,
            provider=ANY,
            outputs=((domain, True),),
        )
    ]
    assert scenario.players_carrying(den) == [
        VisiblePlayer(
            player_id=ANY,
            name="Den",
            available=True,
            powered=None,
            provider=ANY,
            outputs=((domain, True),),
        )
    ]
    assert len(scenario.visible_players()) == 2


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): a protocol-only player names its protocol's provider",
)
async def test_protocol_only_player_names_the_provider_of_its_protocol(scenario: Scenario) -> None:
    """A player with one audio protocol and no native one names that protocol's provider."""
    airplay = await scenario.add_provider("airplay")
    kitchen = airplay.protocol("airplay_kitchen", "Kitchen", role="audio", identifiers=PI)
    await scenario.attach(kitchen)
    await scenario.advance(15)
    await scenario.idle()

    assert [player.provider for player in scenario.players_carrying(kitchen)] == ["airplay"]


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

    The room reports the UUIDs of its renderers among its identifiers; that it claims them by
    those is the assumed contract. The renderers are attached hidden, so none of them is a
    player of its own.
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
    room_id = scenario.player_of(living)
    assert {scenario.player_of(renderer) for renderer in (connector, one_m, one_s)} == {room_id}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): a protocol matching a player without one of its domain attaches to it",
)
async def test_reflashed_device_keeps_its_player(scenario: Scenario) -> None:
    """
    A device re-flashed to another firmware with the same MAC keeps its player.

    The new protocol attaches to it as an available output and the id and the custom name
    stay. Whether the stale protocol of the other provider stays listed is not a rule.
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

    players = scenario.players_carrying(esp_sendspin)
    assert [(p.player_id, p.name, p.provider) for p in players] == [
        (player_id, "Kitchen Speaker", "sendspin")
    ]
    assert ("sendspin", True) in players[0].outputs


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): a protocol matching an offline one of its provider takes its place",
)
async def test_device_with_a_new_protocol_id_keeps_its_player_and_settings(
    scenario: Scenario,
) -> None:
    """
    A device its provider identifies by a new id (ESPHome: the MAC, then a key) keeps its player.

    The new protocol attaches to the player with the same MAC and inherits the config of the
    offline protocol it replaces, whose stale config is dropped.
    """
    sendspin = await scenario.add_provider("sendspin")
    by_mac = sendspin.protocol("sendspin_a0b765001122", "esp32-loud", identifiers=ESP32)
    await scenario.attach(by_mac)
    player_id = scenario.single_player(by_mac).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker", **{FAKE_SETTING: "loud"})
    await scenario.detach(by_mac)

    by_key = sendspin.protocol("sendspin_key_7f3a9c", "esp32-loud", identifiers=ESP32)
    await scenario.attach(by_key)
    await scenario.advance(15)
    await scenario.idle()

    assert [(p.player_id, p.name) for p in scenario.players_carrying(by_key)] == [
        (player_id, "Kitchen Speaker")
    ]
    assert (scenario.protocol_config("sendspin_key_7f3a9c") or {}).get(FAKE_SETTING) == "loud"
    assert await scenario.stored_config_mentioning("sendspin_a0b765001122") == []


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
    tv = chromecast.protocol(
        LIVING_TV.cast_uuid,
        LIVING_TV.name,
        role=first_role,
        identifiers={
            IdentifierType.CAST_UUID: LIVING_TV.cast_uuid,
            IdentifierType.IP_ADDRESS: LIVING_TV.ip,
        },
    )
    await scenario.attach(tv)
    await scenario.advance(15)
    player_id = scenario.single_player(tv).player_id
    scenario.clear_events()

    await scenario.attach(tv, role=second_role)
    await scenario.advance(15)
    await scenario.idle()

    assert [player.player_id for player in scenario.players_carrying(tv)] == [player_id]
    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []


class _TwoPlayers(NamedTuple):
    """The two players the receivers of one TV ended up as, the AirPlay one older."""

    airplay: FakeProtocol
    cast: FakeProtocol
    older_id: str
    younger_id: str


async def _two_players_for_one_tv(
    scenario: Scenario, airplay: FakePlayerProvider, chromecast: FakePlayerProvider, tv: _Tv
) -> _TwoPlayers:
    """
    Let the AirPlay and the Cast receiver of one TV end up as two players, the AirPlay one older.

    The AirPlay receiver advertises no usable address at first, so nothing ties the two together.
    """
    tv_airplay = airplay.protocol(
        tv.airplay_id, tv.name, role="audio", identifiers={IdentifierType.AIRPLAY_ID: tv.airplay_id}
    )
    tv_cast = chromecast.protocol(
        tv.cast_uuid,
        tv.name,
        role="audio",
        identifiers={IdentifierType.CAST_UUID: tv.cast_uuid, IdentifierType.IP_ADDRESS: tv.ip},
    )
    await scenario.attach(tv_airplay)
    await scenario.advance(15)
    older_id = scenario.single_player(tv_airplay).player_id
    await scenario.attach(tv_cast)
    await scenario.advance(15)
    younger_id = scenario.single_player(tv_cast).player_id
    if younger_id == older_id:
        msg = f"the receivers of {tv.name} were matched at first sight"
        raise ScenarioError(msg)
    return _TwoPlayers(tv_airplay, tv_cast, older_id, younger_id)


async def _seen_again_with_its_address(scenario: Scenario, protocol: FakeProtocol, ip: str) -> None:
    """Let the receiver report its address and be found again by its provider."""
    await scenario.report(protocol, identifiers={IdentifierType.IP_ADDRESS: ip})
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
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    tv = await _two_players_for_one_tv(scenario, airplay, chromecast, LIVING_TV)

    await _seen_again_with_its_address(scenario, tv.airplay, LIVING_TV.ip)

    assert scenario.players_carrying(tv.airplay, tv.cast) == [
        VisiblePlayer(
            player_id=tv.older_id,
            name=LIVING_TV.name,
            available=True,
            powered=None,
            provider=ANY,
            outputs=(("airplay", True), ("chromecast", True)),
        )
    ]
    resolved = scenario.player(tv.younger_id)
    assert resolved is not None
    assert resolved.player_id == tv.older_id


async def _played(scenario: Scenario, players: _TwoPlayers) -> None:
    """Play through the younger player's queue, which reaches its receiver, and stop; the queue is saved."""
    await scenario.play(players.younger_id)
    await scenario.mass.player_queues.stop(players.younger_id)
    await scenario.idle()
    if not any(command[0] == "play_media" for command in players.cast.commands):
        msg = f"{players.cast.player_id} did not play"
        raise ScenarioError(msg)
    if not await scenario.saved_queue(players.younger_id):
        msg = f"the queue of {players.younger_id} was not saved"
        raise ScenarioError(msg)


async def _renamed(scenario: Scenario, players: _TwoPlayers) -> None:
    """Give the younger player a custom name."""
    await scenario.configure_player(players.younger_id, name="Kitchen TV")


@pytest.mark.parametrize("used", [_played, _renamed], ids=["played", "renamed"])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 2 (#416): only an unused duplicate is merged by itself",
)
async def test_used_duplicate_is_not_merged(
    scenario: Scenario, used: Callable[[Scenario, _TwoPlayers], Awaitable[None]]
) -> None:
    """
    A younger duplicate that has played or was customised stays a player of its own.

    The unused younger duplicate of a second TV in the same session is merged, as the
    control: the scenario only holds when the merge policy tells the two apart, neither when
    nothing merges nor when everything does.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    living = await _two_players_for_one_tv(scenario, airplay, chromecast, LIVING_TV)
    bedroom = await _two_players_for_one_tv(scenario, airplay, chromecast, BEDROOM_TV)
    await used(scenario, bedroom)
    scenario.clear_events()

    await _seen_again_with_its_address(scenario, living.airplay, LIVING_TV.ip)
    await _seen_again_with_its_address(scenario, bedroom.airplay, BEDROOM_TV.ip)

    # the unused duplicate is merged: its id becomes a legacy id of the older player
    assert [p.player_id for p in scenario.players_carrying(living.airplay, living.cast)] == [
        living.older_id
    ]
    resolved = scenario.player(living.younger_id)
    assert resolved is not None
    assert resolved.player_id == living.older_id
    assert scenario.events(EventType.PLAYER_REMOVED, object_id=living.younger_id) == [
        Event(EventType.PLAYER_REMOVED, living.younger_id)
    ]
    # the used duplicate is kept
    assert [p.player_id for p in scenario.players_carrying(bedroom.airplay)] == [bedroom.older_id]
    assert [p.player_id for p in scenario.players_carrying(bedroom.cast)] == [bedroom.younger_id]
