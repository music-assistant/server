"""Which protocols end up on which player: identifiers, zones, rooms, roles."""

from __future__ import annotations

import asyncio
import itertools
from copy import deepcopy
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import ANY, AsyncMock

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import EventType, IdentifierType, PlayerFeature, ProtocolRole
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidCommand,
    PlayerUnavailableError,
)
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import (
    CONF_MAX_VOLUME,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    MAIN_ZONE,
)
from music_assistant.controllers.config.player_split import (
    remove_player_records_of_older_versions,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
)
from music_assistant.controllers.player_queues.state import PlayerQueueData
from tests.common import as_user

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
# the main zone and a second zone of that chassis, and its AirPlay receiver, which plays on
# the main zone and knows nothing of zones
MAIN = {**CHASSIS, IdentifierType.ZONE: MAIN_ZONE}
SECOND = {**CHASSIS, IdentifierType.ZONE: "zone2"}
CHASSIS_AIRPLAY = {**CHASSIS, IdentifierType.AIRPLAY_ID: "00:1A:2B:3C:4D:5F"}
# a speaker with a native provider of its vendor, which Home Assistant has as a media player
SPEAKER = {
    IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
    IdentifierType.IP_ADDRESS: "192.168.1.90",
}
# the Home Assistant media player of that speaker: its MAC and the id of its entity registry
# entry, which a rename of its entity id keeps
TWIN = {
    IdentifierType.MAC_ADDRESS: SPEAKER[IdentifierType.MAC_ADDRESS],
    IdentifierType.UUID: "5d1e8c0f3a2b4e6d9c7a1f0e2b3d4c5a",
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
    await scenario.register(kitchen)
    await scenario.advance(15)
    kitchen_id = scenario.single_player(kitchen).player_id
    await scenario.unregister(kitchen)

    await scenario.register(den)
    await scenario.advance(15)
    await scenario.register(kitchen)
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


async def test_protocol_only_player_names_the_provider_of_its_protocol(scenario: Scenario) -> None:
    """A player with one audio protocol and no native one names that protocol's provider."""
    airplay = await scenario.add_provider("airplay")
    kitchen = airplay.protocol("airplay_kitchen", "Kitchen", role="audio", identifiers=PI)
    await scenario.register(kitchen)
    await scenario.advance(15)
    await scenario.idle()

    assert [player.provider for player in scenario.players_carrying(kitchen)] == ["airplay"]


# which stored protocol of a player comes first depends on the hash of the ids
@pytest.mark.parametrize("room", ["office", "study", "hall", "attic", "loft", "garage"])
async def test_a_player_without_a_protocol_of_its_own_has_the_neutral_icon(
    scenario: Scenario, room: str
) -> None:
    """
    The default icon names the provider of the own protocol only.

    A player of audio receivers alone, one or more, gets the neutral speaker icon; a player with a
    protocol of its own gets the icon of that protocol's provider, also while it is away and its
    Home Assistant media player, a control of it, is there. An icon the user picked stays.
    """
    airplay = await scenario.add_provider("airplay")
    chromecast = await scenario.add_provider("chromecast")
    hass_players = await scenario.add_provider("hass_players")
    den = airplay.protocol("airplay_den", "Den", role="audio", identifiers=PI)
    den_cast = chromecast.protocol("cast_den", "Den", role="audio", identifiers=PI)
    patio = airplay.protocol("airplay_patio", "Patio", role="audio", identifiers=CHASSIS)
    office = airplay.protocol(f"ap_{room}", "Office", identifiers=SPEAKER)
    office_cast = chromecast.protocol(f"cast_{room}", "Office", role="audio", identifiers=SPEAKER)
    office_twin = hass_players.protocol(f"media_player.{room}", "Office speaker", identifiers=TWIN)
    for protocol in (den, den_cast, patio, office, office_cast, office_twin):
        await scenario.register(protocol)
    await scenario.advance(15)
    await scenario.idle()

    def _icon(protocol: FakeProtocol) -> str:
        state = scenario.player(scenario.single_player(protocol).player_id)
        assert state is not None
        return state.icon

    assert (_icon(den), _icon(patio), _icon(office)) == ("speaker", "speaker", "airplay")

    await scenario.unregister(office)
    await scenario.idle()
    assert _icon(office_cast) == "airplay"

    await scenario.configure_player(scenario.single_player(den).player_id, icon="radio")
    assert _icon(den) == "radio"


async def test_zones_of_one_chassis_are_one_player_each(scenario: Scenario) -> None:
    """
    The zones of a multi-zone device behind one MAC and IP are one player each.

    The audio protocols of a zone land on that zone's player.
    """
    bluesound = await scenario.add_provider("bluesound")
    airplay = await scenario.add_provider("airplay")
    zone = IdentifierType.ZONE
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
    await scenario.register(zone_1)
    await scenario.register(zone_2)

    # the receiver of zone 2 is found first: on the chassis identifiers alone it lands on zone 1
    await scenario.register(airplay_2)
    await scenario.register(airplay_1)
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


class _Receiver(NamedTuple):
    """The protocols of a receiver with two zones, by their part."""

    main: FakeProtocol
    second: FakeProtocol
    airplay: FakeProtocol


async def _receiver(scenario: Scenario) -> _Receiver:
    """Create the zones of a receiver and its AirPlay receiver; register them to use them."""
    musiccast = await scenario.add_provider("musiccast")
    airplay = await scenario.add_provider("airplay")
    return _Receiver(
        main=musiccast.protocol("receiver_main", "Living", identifiers=MAIN),
        second=musiccast.protocol("receiver_zone2", "Patio", identifiers=SECOND),
        airplay=airplay.protocol(
            "receiver_airplay", "AV Receiver", role="audio", identifiers=CHASSIS_AIRPLAY
        ),
    )


def _receiver_players(scenario: Scenario, receiver: _Receiver) -> tuple[str, str]:
    """Return the players of the main zone, with the AirPlay receiver, and of the second zone."""
    main = scenario.players_carrying(receiver.main, receiver.airplay)
    second = scenario.players_carrying(receiver.second)
    assert [player.outputs for player in main] == [(("airplay", True), ("musiccast", True))]
    assert [player.outputs for player in second] == [(("musiccast", True),)]
    return main[0].player_id, second[0].player_id


@pytest.mark.parametrize(
    "order", list(itertools.permutations(("main", "second", "airplay"))), ids="-".join
)
async def test_a_receiver_without_a_zone_lands_on_the_main_zone(
    scenario: Scenario, order: tuple[str, ...]
) -> None:
    """
    The AirPlay receiver of a multi-zone chassis is an output of its main zone, in every order.

    It shares the MAC and IP of every zone and reports no zone. After a restart every
    protocol finds the same player again, in the reverse order this time.
    """
    receiver = await _receiver(scenario)
    for part in order:
        await scenario.register(getattr(receiver, part))
    await scenario.idle()
    player_ids = _receiver_players(scenario, receiver)

    for protocol in receiver:
        scenario.set_present(protocol.protocol_id, False)
    await scenario.restart()
    for part in reversed(order):
        await scenario.register(scenario.protocol(getattr(receiver, part).protocol_id))
    await scenario.idle()

    receiver = _Receiver(*(scenario.protocol(protocol.protocol_id) for protocol in receiver))
    assert _receiver_players(scenario, receiver) == player_ids
    assert len(scenario.visible_players()) == 2


@pytest.mark.parametrize("main_zone", ["away", "disabled"])
async def test_a_receiver_first_seen_without_its_main_zone_still_lands_on_it(
    scenario: Scenario, main_zone: str
) -> None:
    """
    A receiver found while the main zone is away, or its player disabled, belongs to its player.

    It never lands on the second zone, which is there and was found first.
    """
    receiver = await _receiver(scenario)
    await scenario.register(receiver.second)
    await scenario.register(receiver.main)
    main_id = scenario.single_player(receiver.main).player_id
    if main_zone == "away":
        await scenario.unregister(receiver.main)
    else:
        await scenario.disable(main_id)

    await scenario.register(receiver.airplay)
    await scenario.idle()

    assert scenario.player_of(receiver.airplay) == (main_id if main_zone == "away" else None)
    if main_zone == "away":
        await scenario.register(receiver.main)
    else:
        await scenario.enable(main_id)
    await scenario.idle()
    receiver = _Receiver(*(scenario.protocol(protocol.protocol_id) for protocol in receiver))
    assert _receiver_players(scenario, receiver)[0] == main_id


async def test_a_receiver_lands_on_the_main_zone_again_after_a_removal(scenario: Scenario) -> None:
    """
    A receiver its provider forgets, or the player of the main zone the user removes, is found again.

    Either way it lands on the main zone again, not on the second zone.
    """
    receiver = await _receiver(scenario)
    for protocol in receiver:
        await scenario.register(protocol)
    main_id, second_id = _receiver_players(scenario, receiver)

    await scenario.mass.players.remove_protocol(receiver.airplay.protocol_id)
    await scenario.register(receiver.airplay)
    await scenario.idle()
    assert _receiver_players(scenario, receiver) == (main_id, second_id)

    await scenario.remove(main_id)
    await scenario.idle()

    receiver = _Receiver(*(scenario.protocol(protocol.protocol_id) for protocol in receiver))
    new_main_id, new_second_id = _receiver_players(scenario, receiver)
    assert (new_main_id != main_id, new_second_id) == (True, second_id)


# a chassis whose AirPlay receiver was stored on its second zone before the player split
PRE_SPLIT_RECEIVER_ON_THE_SECOND_ZONE: dict[str, dict[str, Any]] = {
    "receiver_main": {
        "player_id": "receiver_main",
        "provider": "musiccast",
        "player_type": "player",
        "enabled": True,
        "default_name": "Living",
        "values": {},
    },
    "receiver_zone2": {
        "player_id": "receiver_zone2",
        "provider": "musiccast",
        "player_type": "player",
        "enabled": True,
        "default_name": "Patio",
        "values": {"linked_protocol_ids": ["receiver_airplay"]},
    },
    "receiver_airplay": {
        "player_id": "receiver_airplay",
        "provider": "airplay",
        "player_type": "protocol",
        "enabled": True,
        "default_name": "AV Receiver",
        "values": {"protocol_parent_id": "receiver_zone2"},
    },
}


async def test_a_receiver_stored_on_another_zone_stays_there(scenario: Scenario) -> None:
    """
    A receiver an existing install keeps on a zone other than the main one stays on that zone.

    Its link was made before the zones were told apart; whether it is the right zone is
    not known, so nothing moves it by itself.
    """
    receiver = await _receiver(scenario)
    for protocol in receiver:
        scenario.set_present(protocol.protocol_id, True)

    await scenario.seed_pre_split_config(PRE_SPLIT_RECEIVER_ON_THE_SECOND_ZONE)
    await scenario.idle()

    receiver = _Receiver(*(scenario.protocol(protocol.protocol_id) for protocol in receiver))
    assert [p.player_id for p in scenario.players_carrying(receiver.second, receiver.airplay)] == [
        "receiver_zone2"
    ]
    assert [p.player_id for p in scenario.players_carrying(receiver.main)] == ["receiver_main"]
    assert len(scenario.visible_players()) == 2


class _Speaker(NamedTuple):
    """The protocols of a speaker: its vendor's, its Home Assistant twin's, its AirPlay receiver."""

    vendor: FakeProtocol
    twin: FakeProtocol
    airplay: FakeProtocol


async def _speaker(scenario: Scenario) -> _Speaker:
    """Create the protocols of a speaker; register them to use them."""
    sonos = await scenario.add_provider("sonos")
    hass_players = await scenario.add_provider("hass_players")
    airplay = await scenario.add_provider("airplay")
    return _Speaker(
        vendor=sonos.protocol("RINCON_5478C9E60DA001400", "Kitchen", identifiers=SPEAKER),
        twin=hass_players.protocol(
            "media_player.kitchen",
            "Kitchen speaker",
            identifiers=TWIN,
        ),
        airplay=airplay.protocol(
            "airplay_kitchen",
            "Kitchen",
            role="audio",
            identifiers={**SPEAKER, IdentifierType.AIRPLAY_ID: "54:78:C9:E6:0D:A1"},
        ),
    )


def _roles(scenario: Scenario, player_id: str) -> dict[str, ProtocolRole]:
    """Return the role of each protocol a player lists."""
    state = scenario.player(player_id)
    assert state is not None
    return {protocol.protocol_id: protocol.role for protocol in state.protocols}


def _played_through(protocol: FakeProtocol) -> bool:
    """Return whether a protocol got media to play."""
    return any(command[0] == "play_media" for command in protocol.commands)


def _current(scenario: Scenario, speaker: _Speaker) -> _Speaker:
    """Return the current protocol objects of a speaker, after a restart or a reload."""
    return _Speaker(*(scenario.protocol(protocol.protocol_id) for protocol in speaker))


@pytest.mark.parametrize(
    "order", list(itertools.permutations(("vendor", "twin", "airplay"))), ids="-".join
)
async def test_a_home_assistant_twin_is_a_control_of_the_vendor_player(
    scenario: Scenario, order: tuple[str, ...]
) -> None:
    """
    The media player Home Assistant has for a speaker joins the player of the speaker.

    The vendor's protocol is its native protocol whichever of the two is found first; the
    twin is a control protocol, no output of the player. After a restart every protocol has
    the same place and role, found in the reverse order this time.
    """
    speaker = await _speaker(scenario)
    for part in order:
        await scenario.register(getattr(speaker, part))
    await scenario.idle()

    player = scenario.single_player(*speaker)
    assert (player.provider, player.outputs) == ("sonos", (("airplay", True), ("sonos", True)))
    roles = {
        speaker.vendor.protocol_id: ProtocolRole.NATIVE,
        speaker.twin.protocol_id: ProtocolRole.CONTROL,
        speaker.airplay.protocol_id: ProtocolRole.AUDIO,
    }
    assert _roles(scenario, player.player_id) == roles

    for protocol in speaker:
        scenario.set_present(protocol.protocol_id, False)
    await scenario.restart()
    for part in reversed(order):
        await scenario.register(scenario.protocol(getattr(speaker, part).protocol_id))
    await scenario.idle()

    assert scenario.single_player(*_current(scenario, speaker)).player_id == player.player_id
    assert _roles(scenario, player.player_id) == roles
    assert len(scenario.visible_players()) == 1


async def test_a_vendor_protocol_joins_the_player_of_its_home_assistant_twin(
    scenario: Scenario,
) -> None:
    """
    A speaker that was a player through Home Assistant keeps it when its vendor protocol arrives.

    Same id and name, now played through the vendor's protocol.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.twin).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker")

    await scenario.register(speaker.vendor)
    await scenario.idle()

    assert scenario.players_carrying(speaker.vendor, speaker.twin) == [
        VisiblePlayer(
            player_id=player_id,
            name="Kitchen Speaker",
            available=True,
            powered=None,
            provider="sonos",
            outputs=(("sonos", True),),
        )
    ]
    assert _roles(scenario, player_id)[speaker.twin.protocol_id] == ProtocolRole.CONTROL


@pytest.mark.parametrize("gone", ["away", "reloaded", "disabled"])
async def test_the_twin_stays_a_control_while_the_vendor_protocol_is_gone(
    scenario: Scenario, gone: str
) -> None:
    """
    A vendor protocol that is away, reloads or belongs to a disabled player keeps its rank.

    The twin stays a control protocol meanwhile, so the player is unavailable while the vendor
    protocol is away, and nothing changes once the vendor protocol is back.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    roles = _roles(scenario, player_id)

    if gone == "away":
        await scenario.unregister(speaker.vendor)
        assert [(p.player_id, p.available) for p in scenario.players_carrying(speaker.twin)] == [
            (player_id, False)
        ]
        assert _roles(scenario, player_id) == roles
        await scenario.register(speaker.vendor)
    elif gone == "reloaded":
        await scenario.reload_provider(scenario.provider("sonos"))
    else:
        await scenario.disable(player_id)
        await scenario.enable(player_id)
    await scenario.idle()

    speaker = _current(scenario, speaker)
    assert scenario.single_player(speaker.vendor, speaker.twin).player_id == player_id
    assert _roles(scenario, player_id) == roles


@pytest.mark.parametrize("removal", ["protocol", "provider"])
async def test_the_twin_is_the_native_protocol_once_the_vendor_protocol_is_removed(
    scenario: Scenario, removal: str
) -> None:
    """
    The twin stands for the speaker once the vendor protocol is gone for good: same player.

    Gone for good is a protocol its provider forgets, or one of a provider that is removed.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    scenario.set_present(speaker.vendor.protocol_id, False)

    if removal == "protocol":
        await scenario.mass.players.remove_protocol(speaker.vendor.protocol_id)
    else:
        await scenario.remove_provider(scenario.provider("sonos"))
    await scenario.idle()

    assert scenario.players_carrying(speaker.twin) == [
        VisiblePlayer(
            player_id=player_id,
            name="Kitchen speaker",
            available=True,
            powered=None,
            provider="hass_players",
            outputs=(("hass_players", True),),
        )
    ]
    assert _roles(scenario, player_id) == {speaker.twin.protocol_id: ProtocolRole.NATIVE}


@pytest.mark.parametrize("found", ["while_enabled", "while_disabled"])
async def test_the_settings_of_a_disabled_player_are_those_of_its_vendor_protocol(
    scenario: Scenario, found: str
) -> None:
    """
    A disabled player keeps its settings on its vendor protocol, not on the twin.

    The vendor protocol here controls the device without an audio path of its own, like
    the twin does as control protocol, so only the rank tells the two apart. Found while
    the player is disabled, it does not attach, and the twin still stands as native in
    what is stored for it.
    """
    wiim = await scenario.add_provider("wiim")
    hass_players = await scenario.add_provider("hass_players")
    twin = hass_players.protocol(
        "media_player.kitchen",
        "Kitchen speaker",
        identifiers=TWIN,
    )
    linkplay = wiim.protocol("linkplay_kitchen", "Kitchen", role="control", identifiers=SPEAKER)
    await scenario.register(twin)
    player_id = scenario.single_player(twin).player_id
    await scenario.configure_player(player_id, **{FAKE_SETTING: "of the twin"})
    if found == "while_enabled":
        await scenario.register(linkplay)
        await scenario.disable(player_id)
    else:
        await scenario.disable(player_id)
        await scenario.register(linkplay)
        assert scenario.player_of(linkplay) is None
        assert (scenario.mass.config.get_protocol_record(twin.protocol_id) or {})["role"] == (
            ProtocolRole.NATIVE.value
        )

    await scenario.mass.config.save_protocol_config(
        linkplay.protocol_id, {FAKE_SETTING: "of the vendor"}
    )

    value = await scenario.mass.config.get_player_config_value(
        player_id, FAKE_SETTING, return_type=str
    )
    assert value == "of the vendor"


async def test_the_settings_of_a_disabled_player_follow_the_protocol_switched_on(
    scenario: Scenario,
) -> None:
    """A disabled player keeps its settings on the twin while the vendor's provider is disabled."""
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    for protocol, value in ((speaker.vendor, "of the vendor"), (speaker.twin, "of the twin")):
        await scenario.mass.config.save_protocol_config(protocol.protocol_id, {FAKE_SETTING: value})
    scenario.set_present(speaker.vendor.protocol_id, False)
    await scenario.mass.config.save_provider_config(
        "sonos", {"enabled": False}, instance_id=scenario.provider("sonos").instance_id
    )
    await scenario.idle()

    await scenario.disable(player_id)

    value = await scenario.mass.config.get_player_config_value(
        player_id, FAKE_SETTING, return_type=str
    )
    assert value == "of the twin"


@pytest.mark.parametrize("failure", ["raises", "cancelled"])
async def test_a_vendor_protocol_whose_first_attach_fails_leaves_the_twin_native(
    scenario: Scenario, failure: str
) -> None:
    """
    A vendor protocol whose first attach fails, or is cancelled, leaves nothing behind.

    The twin it made a control protocol for the attach stands for the speaker again, and
    the player plays through it.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.twin).player_id

    if failure == "raises":
        speaker.vendor.on_attach = AsyncMock(side_effect=RuntimeError("the device went away"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await scenario.register(speaker.vendor)
    else:
        never = asyncio.Event()

        async def _never_returns(_player: object) -> None:
            await never.wait()

        speaker.vendor.on_attach = _never_returns  # type: ignore[method-assign, assignment]
        attach = await scenario.command(scenario.mass.players.register(speaker.vendor))
        attach.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attach
    await scenario.idle()

    assert scenario.players_carrying(speaker.twin) == [
        VisiblePlayer(
            player_id=player_id,
            name="Kitchen speaker",
            available=True,
            powered=None,
            provider="hass_players",
            outputs=(("hass_players", True),),
        )
    ]
    assert _roles(scenario, player_id) == {speaker.twin.protocol_id: ProtocolRole.NATIVE}
    await scenario.play(player_id)
    assert _played_through(speaker.twin)
    assert not _played_through(speaker.vendor)


@pytest.mark.parametrize("switch", ["provider", "unloaded_provider", "protocol"])
async def test_the_twin_is_native_while_the_user_has_the_vendor_protocol_switched_off(
    scenario: Scenario, switch: str
) -> None:
    """
    A vendor protocol the user switched off, or whose provider the user disabled, has no rank.

    The twin stands for the speaker meanwhile, also after a restart, and the player plays
    through it. Switched on again, the vendor protocol ranks above it again at once, also
    while it is away. A provider that is not loaded, as after a failed load, is disabled the
    same way.
    """
    speaker = await _speaker(scenario)
    sonos = scenario.provider("sonos")
    await scenario.register(speaker.twin)
    await scenario.register(speaker.vendor)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    scenario.set_present(speaker.vendor.protocol_id, False)
    if switch == "unloaded_provider":
        await scenario.mass.unload_provider(sonos.instance_id)
        await scenario.idle()
        assert _roles(scenario, player_id)[speaker.twin.protocol_id] == ProtocolRole.CONTROL

    async def _switch(enabled: bool) -> None:
        if switch != "protocol":
            await scenario.mass.config.save_provider_config(
                "sonos", {"enabled": enabled}, instance_id=sonos.instance_id
            )
        else:
            await scenario.mass.config.save_protocol_config(
                speaker.vendor.protocol_id, {"enabled": enabled}
            )
        await scenario.idle()

    await _switch(False)
    for _ in range(2):
        assert [(p.player_id, p.available) for p in scenario.players_carrying(speaker.twin)] == [
            (player_id, True)
        ]
        assert _roles(scenario, player_id) == {
            speaker.vendor.protocol_id: ProtocolRole.NATIVE,
            speaker.twin.protocol_id: ProtocolRole.NATIVE,
        }
        await scenario.restart()
        await scenario.idle()
    await scenario.play(player_id)
    assert _played_through(scenario.protocol(speaker.twin.protocol_id))

    await _switch(True)

    assert [(p.player_id, p.available) for p in scenario.players_carrying(speaker.twin)] == [
        (player_id, False)
    ]
    assert _roles(scenario, player_id) == {
        speaker.vendor.protocol_id: ProtocolRole.NATIVE,
        speaker.twin.protocol_id: ProtocolRole.CONTROL,
    }


async def test_the_twin_object_attached_again_keeps_its_role_and_its_hooks_quiet(
    scenario: Scenario,
) -> None:
    """A demoted twin attached again as the same object keeps its role and is not set up again."""
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    roles = _roles(scenario, player_id)
    on_attach = speaker.twin.on_attach = AsyncMock(wraps=speaker.twin.on_attach)  # type: ignore[method-assign]

    await scenario.register(speaker.twin)
    await scenario.idle()

    on_attach.assert_not_awaited()
    assert _roles(scenario, player_id) == roles


@pytest.mark.parametrize(
    "first", ["RINCON_5478C9E60DA001400", "media_player.kitchen"], ids=["vendor", "twin"]
)
async def test_a_twin_under_a_new_entity_id_returns_to_its_own_player(
    scenario: Scenario, first: str
) -> None:
    """
    A twin stored as a player of its own that comes back under a new entity id goes there.

    It takes over the record of the entity it replaces, whichever of the two players is
    listed first, and never joins the vendor player.
    """
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin):
        scenario.set_present(protocol.protocol_id, True)
    stored = {
        player_id: PRE_SPLIT_SPEAKER_AND_TWIN[player_id]
        for player_id in sorted(
            PRE_SPLIT_SPEAKER_AND_TWIN, key=lambda player_id: player_id != first
        )
    }
    await scenario.seed_pre_split_config(stored)
    await scenario.idle()
    # the entity is renamed while the server is down
    scenario.set_present(speaker.twin.protocol_id, False)
    await scenario.restart()
    await scenario.idle()
    if scenario.mass.players.all_player_states()[0].player_id != first:
        msg = f"{first} is not the first player"
        raise ScenarioError(msg)

    renamed = scenario.provider("hass_players").protocol(
        "media_player.kitchen_2",
        "Kitchen speaker",
        identifiers=TWIN,
    )
    await scenario.register(renamed)
    await scenario.idle()

    assert scenario.player_of(renamed) == "media_player.kitchen"
    assert [
        p.outputs for p in scenario.players_carrying(scenario.protocol(speaker.vendor.protocol_id))
    ] == [(("sonos", True),)]
    # of the old entity only the id of its player is left, as the player of the new one
    assert scenario.protocol_config("media_player.kitchen") is None
    record = scenario.mass.config.get_protocol_record("media_player.kitchen_2")
    assert record is not None
    assert record["player_id"] == "media_player.kitchen"
    player_config = await scenario.player_config("media_player.kitchen")
    assert player_config is not None
    assert player_config.name == "Kitchen (HA)"


async def test_a_removed_player_of_a_speaker_comes_back_with_the_same_roles(
    scenario: Scenario,
) -> None:
    """A speaker whose player the user removed is found again as one player, the twin its control."""
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id

    await scenario.remove(player_id)
    await scenario.idle()

    speaker = _current(scenario, speaker)
    new_player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    assert new_player_id != player_id
    assert _roles(scenario, new_player_id) == {
        speaker.vendor.protocol_id: ProtocolRole.NATIVE,
        speaker.twin.protocol_id: ProtocolRole.CONTROL,
    }


async def test_two_native_protocols_of_one_rank_on_one_host_stay_two_players(
    scenario: Scenario,
) -> None:
    """A Squeezelite and a Snapcast client on one Raspberry Pi are two players."""
    squeezelite = await scenario.add_provider("squeezelite")
    snapcast = await scenario.add_provider("snapcast")
    kitchen = squeezelite.protocol("b8:27:eb:11:22:33", "Kitchen", identifiers=PI)
    den = snapcast.protocol("snapcast_den", "Den", identifiers=PI)

    await scenario.register(kitchen)
    await scenario.register(den)
    await scenario.idle()

    assert [player.name for player in scenario.players_carrying(kitchen)] == ["Kitchen"]
    assert [player.name for player in scenario.players_carrying(den)] == ["Den"]
    assert len(scenario.visible_players()) == 2


# a speaker and its Home Assistant twin as two players, as an install from before the split has them
PRE_SPLIT_SPEAKER_AND_TWIN: dict[str, dict[str, Any]] = {
    "RINCON_5478C9E60DA001400": {
        "player_id": "RINCON_5478C9E60DA001400",
        "provider": "sonos",
        "player_type": "player",
        "enabled": True,
        "default_name": "Kitchen",
        "values": {},
    },
    "media_player.kitchen": {
        "player_id": "media_player.kitchen",
        "provider": "hass_players",
        "player_type": "player",
        "enabled": True,
        "name": "Kitchen (HA)",
        "default_name": "Kitchen speaker",
        "values": {},
    },
}


async def _save_queue(scenario: Scenario, queue_id: str, song: str) -> None:
    """Store a saved queue of one track for a player, as the server saves one."""
    item_id = song.lower().replace(" ", "_")
    track = Track(
        item_id=item_id,
        provider="test",
        name=song,
        duration=60,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )
    item = QueueItem(queue_id=queue_id, queue_item_id=f"{queue_id}_1", name=song, duration=60)
    item.media_item = track
    saved = PlayerQueueData(
        queue=PlayerQueue(
            queue_id=queue_id, active=False, display_name=song, available=False, items=1
        ),
        items=[item],
    )
    for category, data in (
        (CACHE_CATEGORY_PLAYER_QUEUE_STATE, saved.to_cache()),
        (CACHE_CATEGORY_PLAYER_QUEUE_ITEMS, saved.items_to_cache()),
    ):
        await scenario.mass.cache.set(
            key=queue_id,
            data=data,
            provider=scenario.mass.player_queues.domain,
            category=category,
            persistent=True,
        )


async def test_a_speaker_and_its_twin_stored_as_two_players_stay_two_players(
    scenario: Scenario,
) -> None:
    """
    An install that has a speaker and its Home Assistant twin as two players keeps both.

    Each keeps its name, its settings, its saved queue and its protocol as its own protocol;
    making them one is a merge, which the rank of a protocol never does by itself.
    """
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin):
        scenario.set_present(protocol.protocol_id, True)
    own = {
        "RINCON_5478C9E60DA001400": (60, "Kitchen Song"),
        "media_player.kitchen": (80, "Kitchen (HA) Song"),
    }
    stored = deepcopy(PRE_SPLIT_SPEAKER_AND_TWIN)
    for player_id, (max_volume, song) in own.items():
        stored[player_id]["values"][CONF_MAX_VOLUME] = max_volume
        # the queue the older version saved for the player
        await _save_queue(scenario, player_id, song)

    await scenario.seed_pre_split_config(stored)
    await scenario.idle()

    assert sorted((p.player_id, p.name) for p in scenario.visible_players()) == [
        ("RINCON_5478C9E60DA001400", "Kitchen"),
        ("media_player.kitchen", "Kitchen (HA)"),
    ]
    speaker = _current(scenario, speaker)
    for protocol in (speaker.vendor, speaker.twin):
        player_id = protocol.protocol_id
        max_volume, song = own[player_id]
        assert _roles(scenario, player_id) == {protocol.protocol_id: ProtocolRole.NATIVE}
        config = await scenario.player_config(player_id)
        assert config is not None
        assert config.get_value(CONF_MAX_VOLUME) == max_volume
        assert [item.name for item in scenario.mass.player_queues.items(player_id)] == [song]


@pytest.mark.parametrize("choice", ["volume_and_mute", "preferred_output"])
async def test_a_choice_that_names_the_demoted_twin_sends_it_nothing(
    scenario: Scenario, choice: str
) -> None:
    """
    A control or output the user picked for the twin is passed over once the twin is demoted.

    It resolves as if nothing were picked: to the vendor protocol, or to the AirPlay receiver
    when the vendor protocol has no volume of its own, and the player plays through the vendor
    protocol.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.twin).player_id
    twin_id = speaker.twin.protocol_id
    if choice == "volume_and_mute":
        picked = {"volume_control": twin_id, "mute_control": twin_id}
    else:
        picked = {"preferred_output_protocol": twin_id}
    # what the user picked while the entity stood for the speaker
    await scenario.seed_config({CONF_PLAYERS: {player_id: {"values": picked}}})
    speaker = _current(scenario, speaker)
    if choice == "preferred_output":
        await scenario.report(speaker.vendor, supported_features={PlayerFeature.PLAY_MEDIA})
        await scenario.register(speaker.airplay)
    await scenario.register(speaker.vendor)
    await scenario.idle()
    assert _roles(scenario, player_id)[twin_id] == ProtocolRole.CONTROL

    await scenario.mass.players.cmd_volume_set(player_id, 30)
    await scenario.mass.players.cmd_volume_mute(player_id, True)
    await scenario.idle()

    target = speaker.vendor if choice == "volume_and_mute" else speaker.airplay
    assert [command for command in target.commands if command[0].startswith("volume")] == [
        ("volume_set", 30),
        ("volume_mute", True),
    ]
    assert speaker.twin.commands == []
    if choice == "volume_and_mute":
        # also for the output that takes over the level when it starts to play
        player = scenario.mass.players.get_player(player_id)
        assert player is not None
        assert player.volume_control_for_output("native") == PLAYER_CONTROL_NATIVE

    await scenario.play(player_id)

    assert _played_through(speaker.vendor)
    assert speaker.twin.commands == []


async def test_a_command_to_the_player_never_reaches_the_twin(scenario: Scenario) -> None:
    """
    Commands go to the vendor protocol, and are checked on the access to the player.

    The twin gets none of them, neither while the vendor protocol is there nor while it is
    away, when the player is unavailable.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id

    with as_user([player_id]):
        await scenario.mass.players.cmd_volume_set(player_id, 30)
        await scenario.mass.players.cmd_power(player_id, True)
    with as_user(["another_player"]), pytest.raises(InsufficientPermissions):
        await scenario.mass.players.cmd_volume_set(player_id, 20)
    await scenario.play(player_id)
    await scenario.unregister(speaker.vendor)
    with pytest.raises(PlayerUnavailableError):
        await scenario.mass.players.cmd_play(player_id)
    await scenario.idle()

    assert ("volume_set", 30) in speaker.vendor.commands
    assert any(command[0] == "play_media" for command in speaker.vendor.commands)
    assert speaker.twin.commands == []


@pytest.mark.parametrize(
    "order", list(itertools.permutations(("main", "second", "airplay"))), ids="-".join
)
async def test_unused_zone_players_are_never_merged_by_themselves(
    scenario: Scenario, order: tuple[str, ...]
) -> None:
    """
    The players of the zones of one chassis stay apart while nobody has used them yet.

    No zone player is merged into another zone, nor into the player of the receiver of the
    chassis unless it is the main zone, whatever is found first.
    """
    receiver = await _receiver(scenario)
    for part in order:
        await scenario.register(getattr(receiver, part))
    await scenario.idle()

    _receiver_players(scenario, receiver)
    assert scenario.events(EventType.PLAYER_REMOVED) == []


@pytest.mark.parametrize(
    "order",
    [
        order
        for order in itertools.permutations(("main", "second", "patio", "living"))
        if order.index("patio") < order.index("living")
    ],
    ids="-".join,
)
async def test_a_receiver_on_the_wrong_zone_is_put_right_by_hand(
    scenario: Scenario, order: tuple[str, ...]
) -> None:
    """
    The receiver of a secondary zone that joined the main zone is moved there by the user.

    On a chassis whose every zone has a receiver of its own the first one seen joins the
    main zone, and the other one is a player of its own: here the second zone's receiver is
    found before the main zone's, in every order with the zones. The user
    unlinks the first one and merges its player into the zone it belongs to; the main zone's
    own receiver is merged into the main zone. That stays so after a restart in either
    order, and the automatic merge does not undo it.
    """
    bluesound = await scenario.add_provider("bluesound")
    airplay = await scenario.add_provider("airplay")
    main = bluesound.protocol("chassis_main", "Living", identifiers=MAIN)
    second = bluesound.protocol("chassis_zone2", "Patio", identifiers=SECOND)
    receivers = [
        airplay.protocol(
            protocol_id,
            name,
            role="audio",
            identifiers={**CHASSIS, IdentifierType.AIRPLAY_ID: airplay_id},
        )
        for protocol_id, name, airplay_id in (
            ("airplay_patio", "Patio", "00:1A:2B:3C:4D:02"),
            ("airplay_living", "Living", "00:1A:2B:3C:4D:01"),
        )
    ]
    parts = {"main": main, "second": second, "patio": receivers[0], "living": receivers[1]}
    for part in order:
        await scenario.register(parts[part])
    await scenario.idle()
    main_id = scenario.single_player(main).player_id
    second_id = scenario.single_player(second).player_id
    assert scenario.player_of(receivers[0]) == main_id
    living = scenario.players_carrying(receivers[1])
    assert [player.outputs for player in living] == [(("airplay", True),)]
    living_id = living[0].player_id
    assert living_id not in (main_id, second_id)

    patio_id = await scenario.mass.players.unlink_protocol("airplay_patio")
    await scenario.mass.players.merge(patio_id, second_id)
    await scenario.mass.players.merge(living_id, main_id)
    await scenario.idle()

    for restart_order in (
        ("airplay_patio", "airplay_living", "chassis_zone2", "chassis_main"),
        ("chassis_main", "chassis_zone2", "airplay_living", "airplay_patio"),
    ):
        for protocol_id in restart_order:
            scenario.set_present(protocol_id, False)
        await scenario.restart()
        for protocol_id in restart_order:
            await scenario.register(scenario.protocol(protocol_id))
        await scenario.idle()
        assert {
            protocol_id: scenario.player_of(scenario.protocol(protocol_id))
            for protocol_id in restart_order
        } == {
            "chassis_main": main_id,
            "airplay_living": main_id,
            "chassis_zone2": second_id,
            "airplay_patio": second_id,
        }
        assert len(scenario.visible_players()) == 2


async def _twin_found_first(scenario: Scenario) -> None:
    """Find the twin of a speaker before its vendor protocol."""
    speaker = await _speaker(scenario)
    for protocol in (speaker.twin, speaker.vendor):
        await scenario.register(protocol)


async def _twin_kept_by_a_merge(scenario: Scenario) -> None:
    """Merge the player of a speaker into that of its twin, both stored before the split."""
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin):
        scenario.set_present(protocol.protocol_id, True)
    await scenario.seed_pre_split_config(PRE_SPLIT_SPEAKER_AND_TWIN)
    await scenario.idle()
    await scenario.mass.players.merge("RINCON_5478C9E60DA001400", "media_player.kitchen")


async def _zones(scenario: Scenario) -> None:
    """Find both zones of a receiver and its AirPlay receiver."""
    for protocol in await _receiver(scenario):
        await scenario.register(protocol)


async def _zone_put_right_by_hand(scenario: Scenario) -> None:
    """Unlink the receiver of the main zone and merge its player into the second zone."""
    receiver = await _receiver(scenario)
    for protocol in receiver:
        await scenario.register(protocol)
    await scenario.idle()
    second_id = scenario.single_player(receiver.second).player_id
    unlinked_id = await scenario.mass.players.unlink_protocol(receiver.airplay.protocol_id)
    await scenario.mass.players.merge(unlinked_id, second_id)


@pytest.mark.parametrize(
    "build",
    [_twin_found_first, _twin_kept_by_a_merge, _zones, _zone_put_right_by_hand],
    ids=lambda build: build.__name__.strip("_"),
)
async def test_the_cleanup_at_start_keeps_every_player_of_this_version(
    scenario: Scenario, build: Callable[[Scenario], Awaitable[None]]
) -> None:
    """
    The players of a demoted twin, of zones and of a hand repair keep their records at a start.

    The start removes the player records an older version stored; none of these is one.
    """
    await build(scenario)
    await scenario.idle()
    stored = scenario.mass.config._data
    created = {
        player_id: record["values"].get("created_at")
        for player_id, record in stored[CONF_PLAYERS].items()
    }
    if build is _twin_kept_by_a_merge:
        assert stored[CONF_PLAYER_PROTOCOLS]["media_player.kitchen"]["role"] == "control"

    data = deepcopy(stored)
    assert remove_player_records_of_older_versions(data) is False
    assert data == stored

    await scenario.restart()
    await scenario.idle()
    assert {
        player_id: record["values"].get("created_at")
        for player_id, record in scenario.mass.config._data[CONF_PLAYERS].items()
    } == created


@pytest.mark.parametrize(
    ("keep", "vendor_present"),
    [("vendor", True), ("twin", True), ("twin", False)],
    ids=["vendor", "twin", "twin_while_vendor_away"],
)
async def test_a_speaker_and_its_twin_stored_apart_can_be_merged_by_hand(
    scenario: Scenario, keep: str, vendor_present: bool
) -> None:
    """
    The user merges the player of a speaker and that of its twin: the twin is its control.

    Whichever of the two is kept, the vendor protocol is the native one on it, also while
    it is away, and the id of the other answers for it.
    """
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin):
        scenario.set_present(protocol.protocol_id, True)
    await scenario.seed_pre_split_config(PRE_SPLIT_SPEAKER_AND_TWIN)
    await scenario.idle()
    ids = {"vendor": "RINCON_5478C9E60DA001400", "twin": "media_player.kitchen"}
    kept_id = ids[keep]
    absorbed_id = ids["twin" if keep == "vendor" else "vendor"]
    if not vendor_present:
        await scenario.unregister(scenario.protocol(speaker.vendor.protocol_id))

    await scenario.mass.players.merge(absorbed_id, kept_id)
    await scenario.idle()

    twin = scenario.protocol(speaker.twin.protocol_id)
    assert scenario.player_of(twin) == kept_id
    assert _roles(scenario, kept_id) == {
        speaker.vendor.protocol_id: ProtocolRole.NATIVE,
        speaker.twin.protocol_id: ProtocolRole.CONTROL,
    }
    resolved = scenario.player(absorbed_id)
    assert resolved is not None
    assert resolved.player_id == kept_id
    assert len(scenario.visible_players()) == 1


@pytest.mark.parametrize("twin", ["unused", "renamed"])
async def test_a_twin_stored_apart_is_merged_by_itself_only_while_unused(
    scenario: Scenario, twin: str
) -> None:
    """
    An unused player of the twin goes into the speaker's player by itself, a used one stays.

    The twin's player is the younger one here; once merged, the twin is a control protocol.
    """
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin):
        scenario.set_present(protocol.protocol_id, True)
    stored = {player_id: dict(record) for player_id, record in PRE_SPLIT_SPEAKER_AND_TWIN.items()}
    if twin == "unused":
        del stored["media_player.kitchen"]["name"]

    await scenario.seed_pre_split_config(stored)
    await scenario.idle()

    speaker = _current(scenario, speaker)
    if twin == "unused":
        assert scenario.single_player(speaker.vendor, speaker.twin).player_id == (
            "RINCON_5478C9E60DA001400"
        )
        assert _roles(scenario, "RINCON_5478C9E60DA001400")[speaker.twin.protocol_id] == (
            ProtocolRole.CONTROL
        )
    else:
        assert [scenario.player_of(protocol) for protocol in (speaker.vendor, speaker.twin)] == [
            "RINCON_5478C9E60DA001400",
            "media_player.kitchen",
        ]


async def test_two_players_of_one_rank_on_one_host_can_not_be_merged(scenario: Scenario) -> None:
    """A Squeezelite and a Snapcast client of one host are refused as one player."""
    squeezelite = await scenario.add_provider("squeezelite")
    snapcast = await scenario.add_provider("snapcast")
    kitchen = squeezelite.protocol("b8:27:eb:11:22:33", "Kitchen", identifiers=PI)
    den = snapcast.protocol("snapcast_den", "Den", identifiers=PI)
    await scenario.register(kitchen)
    await scenario.register(den)

    with pytest.raises(InvalidCommand):
        await scenario.mass.players.merge(
            scenario.single_player(den).player_id, scenario.single_player(kitchen).player_id
        )


@pytest.mark.parametrize("unlinked", ["vendor", "twin"])
async def test_a_protocol_unlinked_from_a_speaker_stands_on_its_own(
    scenario: Scenario, unlinked: str
) -> None:
    """
    The vendor protocol or the twin taken off the speaker's player gets one of its own.

    The one that stays stands for the speaker in the role it reports, the one taken off in
    its own role too, and neither is merged back by itself, also after a restart.
    """
    speaker = await _speaker(scenario)
    await scenario.register(speaker.vendor)
    await scenario.register(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    taken_off = speaker.vendor if unlinked == "vendor" else speaker.twin
    staying = speaker.twin if unlinked == "vendor" else speaker.vendor

    new_id = await scenario.mass.players.unlink_protocol(taken_off.protocol_id)
    await scenario.idle()

    for _ in range(2):
        assert _roles(scenario, player_id) == {staying.protocol_id: ProtocolRole.NATIVE}
        assert _roles(scenario, new_id) == {taken_off.protocol_id: ProtocolRole.NATIVE}
        assert [p.available for p in scenario.visible_players()] == [True, True]
        await scenario.restart()
        await scenario.idle()


async def test_a_role_change_neither_merges_nor_splits_players(scenario: Scenario) -> None:
    """
    Disabling the vendor's provider and enabling it again changes roles, never players.

    The twin stands for the speaker while the vendor's provider is disabled and is a control
    again once it is enabled. The speaker and its twin, merged by hand, stay one player; the
    AirPlay receiver the user took off it stays a player of its own.
    """
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin, speaker.airplay):
        scenario.set_present(protocol.protocol_id, True)
    await scenario.seed_pre_split_config(PRE_SPLIT_SPEAKER_AND_TWIN)
    await scenario.idle()
    await scenario.mass.players.merge("media_player.kitchen", "RINCON_5478C9E60DA001400")
    await scenario.idle()
    speaker = _current(scenario, speaker)
    airplay_id = await scenario.mass.players.unlink_protocol(speaker.airplay.protocol_id)
    await scenario.idle()
    scenario.clear_events()

    sonos = scenario.provider("sonos")
    for enabled in (False, True):
        await scenario.mass.config.save_provider_config(
            "sonos", {"enabled": enabled}, instance_id=sonos.instance_id
        )
        await scenario.idle()
        assert sorted(p.player_id for p in scenario.visible_players()) == sorted(
            ["RINCON_5478C9E60DA001400", airplay_id]
        )
        twin = scenario.protocol(speaker.twin.protocol_id)
        assert scenario.player_of(twin) == "RINCON_5478C9E60DA001400"
        assert _roles(scenario, "RINCON_5478C9E60DA001400")[twin.protocol_id] == (
            ProtocolRole.CONTROL if enabled else ProtocolRole.NATIVE
        )

    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []


@pytest.mark.parametrize(
    "order", list(itertools.permutations(("room", "connector", "one_m", "one_s"))), ids="-".join
)
async def test_a_room_is_the_player_of_its_own_renderer_only(
    scenario: Scenario, order: tuple[str, ...]
) -> None:
    """
    A Raumfeld room joins the player of the renderer it reports; its other speakers stay apart.

    That renderer is the DLNA output of the room's player. The renderer of every other
    speaker of the room is a player of its own, which the room neither takes nor changes.
    That holds in every discovery order, and after a restart in the reverse order.
    """
    dlna = await scenario.add_provider("dlna")
    raumfeld = await scenario.add_provider("raumfeld")
    parts = {
        part: dlna.protocol(
            udn,
            name,
            role="audio",
            identifiers={
                IdentifierType.UUID: udn,
                IdentifierType.MAC_ADDRESS: mac_address,
                IdentifierType.IP_ADDRESS: ip_address,
            },
        )
        for part, udn, name, mac_address, ip_address in (
            ("connector", CONNECTOR_UDN, "Connector Living", "00:1E:C0:00:00:01", HOST_IP),
            ("one_m", ONE_M_UDN, "One M Living", "00:1E:C0:00:00:02", "192.168.1.81"),
            ("one_s", ONE_S_UDN, "One S Living", "00:1E:C0:00:00:03", "192.168.1.82"),
        )
    }
    # the room is controlled through the host and reports the renderer that runs on it
    parts["room"] = raumfeld.protocol(
        "raumfeld_living",
        "Living",
        identifiers={IdentifierType.UUID: CONNECTOR_UDN, IdentifierType.IP_ADDRESS: HOST_IP},
    )
    found_on: dict[str, str | None] = {}
    for part in order:
        await scenario.register(parts[part])
        await scenario.idle()
        found_on[part] = scenario.player_of(parts[part])
    room_id = found_on[next(part for part in order if part in ("room", "connector"))]
    placement = {
        "room": room_id,
        "connector": room_id,
        "one_m": found_on["one_m"],
        "one_s": found_on["one_s"],
    }
    players = [
        ("Living", "raumfeld", (("dlna", True), ("raumfeld", True))),
        ("One M Living", "dlna", (("dlna", True),)),
        ("One S Living", "dlna", (("dlna", True),)),
    ]

    for restarted in (False, True):
        if restarted:
            for protocol in parts.values():
                scenario.set_present(protocol.protocol_id, False)
            await scenario.restart()
            for part in reversed(order):
                await scenario.register(scenario.protocol(parts[part].protocol_id))
            await scenario.idle()
        assert {
            part: scenario.player_of(scenario.protocol(protocol.protocol_id))
            for part, protocol in parts.items()
        } == placement
        assert sorted((p.name, p.provider, p.outputs) for p in scenario.visible_players()) == (
            players
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
    await scenario.register(esp_squeezelite)
    player_id = scenario.single_player(esp_squeezelite).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker")
    await scenario.unregister(esp_squeezelite)

    await scenario.register(esp_sendspin)
    await scenario.advance(15)
    await scenario.idle()

    players = scenario.players_carrying(esp_sendspin)
    assert [(p.player_id, p.name, p.provider) for p in players] == [
        (player_id, "Kitchen Speaker", "sendspin")
    ]
    assert ("sendspin", True) in players[0].outputs


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
    await scenario.register(by_mac)
    player_id = scenario.single_player(by_mac).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker", **{FAKE_SETTING: "loud"})
    await scenario.unregister(by_mac)

    by_key = sendspin.protocol("sendspin_key_7f3a9c", "esp32-loud", identifiers=ESP32)
    await scenario.register(by_key)
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
    await scenario.register(tv)
    await scenario.advance(15)
    player_id = scenario.single_player(tv).player_id
    scenario.clear_events()

    await scenario.register(tv, role=second_role)
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
    await scenario.register(tv_airplay)
    await scenario.advance(15)
    older_id = scenario.single_player(tv_airplay).player_id
    await scenario.register(tv_cast)
    await scenario.advance(15)
    younger_id = scenario.single_player(tv_cast).player_id
    if younger_id == older_id:
        msg = f"the receivers of {tv.name} were matched at first sight"
        raise ScenarioError(msg)
    return _TwoPlayers(tv_airplay, tv_cast, older_id, younger_id)


async def _seen_again_with_its_address(scenario: Scenario, protocol: FakeProtocol, ip: str) -> None:
    """Let the receiver report its address and be found again by its provider."""
    await scenario.report(protocol, identifiers={IdentifierType.IP_ADDRESS: ip})
    await scenario.unregister(protocol)
    await scenario.register(protocol)
    await scenario.idle()


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
        msg = f"{players.cast.protocol_id} did not play"
        raise ScenarioError(msg)
    if not await scenario.saved_queue(players.younger_id):
        msg = f"the queue of {players.younger_id} was not saved"
        raise ScenarioError(msg)


async def _renamed(scenario: Scenario, players: _TwoPlayers) -> None:
    """Give the younger player a custom name, which is stored in its config."""
    await scenario.configure_player(players.younger_id, name="Kitchen TV")
    config = await scenario.player_config(players.younger_id)
    if config is None or config.name != "Kitchen TV":
        msg = f"the custom name of {players.younger_id} was not stored"
        raise ScenarioError(msg)


@pytest.mark.parametrize("used", [_played, _renamed], ids=["played", "renamed"])
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

    # the used duplicate is kept
    assert [p.player_id for p in scenario.players_carrying(bedroom.airplay)] == [bedroom.older_id]
    assert [p.player_id for p in scenario.players_carrying(bedroom.cast)] == [bedroom.younger_id]
    # the unused duplicate is merged: its id becomes a legacy id of the older player
    for receiver in (living.airplay, living.cast):
        assert [p.player_id for p in scenario.players_carrying(receiver)] == [living.older_id]
    resolved = scenario.player(living.younger_id)
    assert resolved is not None
    assert resolved.player_id == living.older_id
    assert scenario.events(EventType.PLAYER_REMOVED, object_id=living.younger_id) == [
        Event(EventType.PLAYER_REMOVED, living.younger_id)
    ]
