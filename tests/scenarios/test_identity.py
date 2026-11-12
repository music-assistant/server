"""Which protocols end up on which player: identifiers, zones, claimed instances, roles."""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import ANY, AsyncMock

import pytest
from music_assistant_models.enums import EventType, IdentifierType, ProtocolRole
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidCommand,
    PlayerUnavailableError,
)

from music_assistant.constants import MAIN_ZONE
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


async def test_protocol_only_player_names_the_provider_of_its_protocol(scenario: Scenario) -> None:
    """A player with one audio protocol and no native one names that protocol's provider."""
    airplay = await scenario.add_provider("airplay")
    kitchen = airplay.protocol("airplay_kitchen", "Kitchen", role="audio", identifiers=PI)
    await scenario.attach(kitchen)
    await scenario.advance(15)
    await scenario.idle()

    assert [player.provider for player in scenario.players_carrying(kitchen)] == ["airplay"]


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


class _Receiver(NamedTuple):
    """The protocols of a receiver with two zones, by their part."""

    main: FakeProtocol
    second: FakeProtocol
    airplay: FakeProtocol


async def _receiver(scenario: Scenario) -> _Receiver:
    """Create the zones of a receiver and its AirPlay receiver; attach them to use them."""
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
        await scenario.attach(getattr(receiver, part))
    await scenario.idle()
    player_ids = _receiver_players(scenario, receiver)

    for protocol in receiver:
        scenario.set_present(protocol.protocol_id, False)
    await scenario.restart()
    for part in reversed(order):
        await scenario.attach(scenario.protocol(getattr(receiver, part).protocol_id))
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
    await scenario.attach(receiver.second)
    await scenario.attach(receiver.main)
    main_id = scenario.single_player(receiver.main).player_id
    if main_zone == "away":
        await scenario.detach(receiver.main)
    else:
        await scenario.disable(main_id)

    await scenario.attach(receiver.airplay)
    await scenario.idle()

    assert scenario.player_of(receiver.airplay) == (main_id if main_zone == "away" else None)
    if main_zone == "away":
        await scenario.attach(receiver.main)
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
        await scenario.attach(protocol)
    main_id, second_id = _receiver_players(scenario, receiver)

    await scenario.mass.players.remove_protocol(receiver.airplay.protocol_id)
    await scenario.attach(receiver.airplay)
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
    """Create the protocols of a speaker; attach them to use them."""
    sonos = await scenario.add_provider("sonos")
    hass_players = await scenario.add_provider("hass_players")
    airplay = await scenario.add_provider("airplay")
    return _Speaker(
        vendor=sonos.protocol("RINCON_5478C9E60DA001400", "Kitchen", identifiers=SPEAKER),
        twin=hass_players.protocol(
            "media_player.kitchen",
            "Kitchen speaker",
            identifiers={IdentifierType.MAC_ADDRESS: SPEAKER[IdentifierType.MAC_ADDRESS]},
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
        await scenario.attach(getattr(speaker, part))
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
        await scenario.attach(scenario.protocol(getattr(speaker, part).protocol_id))
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
    await scenario.attach(speaker.twin)
    player_id = scenario.single_player(speaker.twin).player_id
    await scenario.configure_player(player_id, name="Kitchen Speaker")

    await scenario.attach(speaker.vendor)
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

    The twin stays a control protocol meanwhile, so the player is unavailable while only the
    twin reaches the speaker, and nothing changes once the vendor protocol is back.
    """
    speaker = await _speaker(scenario)
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    roles = _roles(scenario, player_id)

    if gone == "away":
        await scenario.detach(speaker.vendor)
        assert [(p.player_id, p.available) for p in scenario.players_carrying(speaker.twin)] == [
            (player_id, False)
        ]
        assert _roles(scenario, player_id) == roles
        await scenario.attach(speaker.vendor)
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
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
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
        identifiers={IdentifierType.MAC_ADDRESS: SPEAKER[IdentifierType.MAC_ADDRESS]},
    )
    linkplay = wiim.protocol("linkplay_kitchen", "Kitchen", role="control", identifiers=SPEAKER)
    await scenario.attach(twin)
    player_id = scenario.single_player(twin).player_id
    await scenario.configure_player(player_id, **{FAKE_SETTING: "of the twin"})
    if found == "while_enabled":
        await scenario.attach(linkplay)
        await scenario.disable(player_id)
    else:
        await scenario.disable(player_id)
        await scenario.attach(linkplay)

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
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
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
    the player plays through it as before.
    """
    speaker = await _speaker(scenario)
    await scenario.attach(speaker.twin)
    player_id = scenario.single_player(speaker.twin).player_id

    if failure == "raises":
        speaker.vendor.on_attach = AsyncMock(side_effect=RuntimeError("the device went away"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await scenario.attach(speaker.vendor)
    else:
        never = asyncio.Event()

        async def _never_returns(_player: object) -> None:
            await never.wait()

        speaker.vendor.on_attach = _never_returns  # type: ignore[method-assign, assignment]
        attach = await scenario.command(scenario.mass.players.attach(speaker.vendor))
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


@pytest.mark.parametrize("switch", ["provider", "protocol"])
async def test_the_twin_is_native_while_the_user_has_the_vendor_protocol_switched_off(
    scenario: Scenario, switch: str
) -> None:
    """
    A vendor protocol the user switched off, or whose provider the user disabled, has no rank.

    The twin stands for the speaker meanwhile, also after a restart, and the player plays
    through it. Switched on again, the vendor protocol ranks above it again at once, also
    while it is away.
    """
    speaker = await _speaker(scenario)
    sonos = scenario.provider("sonos")
    await scenario.attach(speaker.twin)
    await scenario.attach(speaker.vendor)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    scenario.set_present(speaker.vendor.protocol_id, False)

    async def _switch(enabled: bool) -> None:
        if switch == "provider":
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
    """A demoted twin whose provider attaches the same object again is refreshed, not re-attached."""
    speaker = await _speaker(scenario)
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id
    roles = _roles(scenario, player_id)
    on_attach = speaker.twin.on_attach = AsyncMock(wraps=speaker.twin.on_attach)  # type: ignore[method-assign]

    await scenario.attach(speaker.twin)
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
        identifiers={IdentifierType.MAC_ADDRESS: SPEAKER[IdentifierType.MAC_ADDRESS]},
    )
    await scenario.attach(renamed)
    await scenario.idle()

    assert scenario.player_of(renamed) == "media_player.kitchen"
    assert [
        p.outputs for p in scenario.players_carrying(scenario.protocol(speaker.vendor.protocol_id))
    ] == [(("sonos", True),)]
    assert await scenario.stored_config_mentioning('"media_player.kitchen"') == []


async def test_a_removed_player_of_a_speaker_comes_back_with_the_same_roles(
    scenario: Scenario,
) -> None:
    """A speaker whose player the user removed is found again as one player, the twin its control."""
    speaker = await _speaker(scenario)
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
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
    """A Squeezelite and a Snapcast client on one Raspberry Pi are two players, as before."""
    squeezelite = await scenario.add_provider("squeezelite")
    snapcast = await scenario.add_provider("snapcast")
    kitchen = squeezelite.protocol("b8:27:eb:11:22:33", "Kitchen", identifiers=PI)
    den = snapcast.protocol("snapcast_den", "Den", identifiers=PI)

    await scenario.attach(kitchen)
    await scenario.attach(den)
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


async def test_a_speaker_and_its_twin_stored_as_two_players_stay_two_players(
    scenario: Scenario,
) -> None:
    """
    An install that has a speaker and its Home Assistant twin as two players keeps both.

    Each has its own name, settings, queue and Home Assistant entity; making them one is a
    merge, which the rank of a protocol never does by itself.
    """
    speaker = await _speaker(scenario)
    for protocol in (speaker.vendor, speaker.twin):
        scenario.set_present(protocol.protocol_id, True)

    await scenario.seed_pre_split_config(PRE_SPLIT_SPEAKER_AND_TWIN)
    await scenario.idle()

    assert sorted((p.player_id, p.name) for p in scenario.visible_players()) == [
        ("RINCON_5478C9E60DA001400", "Kitchen"),
        ("media_player.kitchen", "Kitchen (HA)"),
    ]


async def test_a_command_to_the_player_never_reaches_the_twin(scenario: Scenario) -> None:
    """
    Commands go to the vendor protocol, and are checked on the access to the player.

    The twin gets none of them, neither while the vendor protocol is there nor while it is
    away, when the player is unavailable.
    """
    speaker = await _speaker(scenario)
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
    player_id = scenario.single_player(speaker.vendor, speaker.twin).player_id

    with as_user([player_id]):
        await scenario.mass.players.cmd_volume_set(player_id, 30)
        await scenario.mass.players.cmd_power(player_id, True)
    with as_user(["another_player"]), pytest.raises(InsufficientPermissions):
        await scenario.mass.players.cmd_volume_set(player_id, 20)
    await scenario.play(player_id)
    await scenario.detach(speaker.vendor)
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
        await scenario.attach(getattr(receiver, part))
    await scenario.idle()

    _receiver_players(scenario, receiver)
    assert scenario.events(EventType.PLAYER_REMOVED) == []


async def test_a_receiver_on_the_wrong_zone_is_put_right_by_hand(scenario: Scenario) -> None:
    """
    The receiver of a secondary zone that joined the main zone is moved there by the user.

    On a chassis whose every zone has a receiver of its own the first one seen joins the
    main zone. The user unlinks it and merges its player into the zone it belongs to; the
    main zone's own receiver is merged into the main zone. That stays so after a restart in
    either order, and the automatic merge does not undo it.
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
    for protocol in (main, second, *receivers):
        await scenario.attach(protocol)
    await scenario.idle()
    main_id = scenario.single_player(main).player_id
    second_id = scenario.single_player(second).player_id
    if scenario.player_of(receivers[0]) != main_id:
        msg = "the receiver of the second zone did not join the main zone"
        raise ScenarioError(msg)

    patio_id = await scenario.mass.players.unlink_protocol("airplay_patio")
    await scenario.mass.players.merge(patio_id, second_id)
    if (living_id := scenario.player_of(receivers[1])) != main_id:
        await scenario.mass.players.merge(str(living_id), main_id)
    await scenario.idle()

    for order in (
        ("airplay_patio", "airplay_living", "chassis_zone2", "chassis_main"),
        ("chassis_main", "chassis_zone2", "airplay_living", "airplay_patio"),
    ):
        for protocol_id in order:
            scenario.set_present(protocol_id, False)
        await scenario.restart()
        for protocol_id in order:
            await scenario.attach(scenario.protocol(protocol_id))
        await scenario.idle()
        assert {
            protocol_id: scenario.player_of(scenario.protocol(protocol_id)) for protocol_id in order
        } == {
            "chassis_main": main_id,
            "airplay_living": main_id,
            "chassis_zone2": second_id,
            "airplay_patio": second_id,
        }
        assert len(scenario.visible_players()) == 2


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
        await scenario.detach(scenario.protocol(speaker.vendor.protocol_id))

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
    await scenario.attach(kitchen)
    await scenario.attach(den)

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
    await scenario.attach(speaker.vendor)
    await scenario.attach(speaker.twin)
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

    The speaker and its twin, merged by hand, stay one player; the AirPlay receiver the
    user took off it stays a player of its own.
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

    assert scenario.events(EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED) == []


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
