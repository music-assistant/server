"""Tests for removing the player of a Sonos speaker that became a satellite of a set."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from zeroconf import ServiceStateChange

from music_assistant.mass import MusicAssistant
from music_assistant.providers.sonos.const import SATELLITE_RECHECK_INTERVAL
from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos.provider import SonosPlayerProvider
from tests.common import virtual_loop_clock
from tests.providers.sonos.test_provider import _bind_discovering_provider

INSTANCE = "sonos--x7rQ2pLm"
SPEAKER = "RINCON_F0F6C1CC8D0401400"
ADDRESS = "192.168.1.61"
SERVICE_NAME = f"{SPEAKER}@Living Room._sonos._tcp.local."

# the info the second speaker of a stereo pair answers with, and the info of a player
SATELLITE_INFO = {
    "device": {
        "id": SPEAKER,
        "name": "Living Room",
        "modelDisplayName": "Era 100",
        "capabilities": ["CLOUD", "AUDIO_CLIP", "LINE_IN", "VOICE", "MICROPHONE_SWITCH"],
    }
}
PLAYER_INFO = {
    "device": {
        "id": SPEAKER,
        "name": "Living Room",
        "modelDisplayName": "Era 100",
        "capabilities": ["CLOUD", "PLAYBACK", "AIRPLAY", "AUDIO_CLIP", "LINE_IN", "VOICE"],
    }
}


@dataclass
class _Setup:
    """A provider that set up the speaker earlier, with the mocks it talks to."""

    provider: SonosPlayerProvider
    players: MagicMock
    config: MagicMock
    sonos_player: MagicMock
    get_info: AsyncMock


def _provider(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[Any],
    registered: bool = True,
    record_provider: str = INSTANCE,
    wanted: bool = True,
) -> _Setup:
    """
    Create a provider that set up the speaker earlier and lost its connection to it.

    :param mass: The bare MusicAssistant with the real task machinery.
    :param answers: What the speaker answers to each info request, in order.
    :param registered: Whether the speaker's protocol is registered in this run.
    :param record_provider: The provider instance the stored protocol record names.
    :param wanted: Whether the server wants the speaker (its player is not disabled).
    """
    provider = _bind_discovering_provider(mass)
    provider.config = MagicMock(instance_id=INSTANCE)
    mass._http_session_no_ssl = MagicMock()
    players = MagicMock()
    config = MagicMock()
    mass.players = players
    mass.config = config
    sonos_player = MagicMock(spec=SonosPlayer, connected=False)
    sonos_player.logger = logging.getLogger("test.sonos.satellite")
    sonos_player.client = MagicMock()
    players.get_protocol.return_value = sonos_player if registered else None
    players.wants_protocol.return_value = wanted
    players.remove_protocol = AsyncMock()
    config.get_protocol_record.return_value = {
        "provider": record_provider,
        "player_id": "pl0123456789ab",
    }
    get_info = AsyncMock(side_effect=answers)
    monkeypatch.setattr("music_assistant.providers.sonos.provider.get_discovery_info", get_info)
    return _Setup(provider, players, config, sonos_player, get_info)


def _announce(provider: SonosPlayerProvider) -> Any:
    """Return the mdns update the speaker sends, as the provider receives it."""
    info = MagicMock()
    info.decoded_properties = {"uuid": SPEAKER}
    info.ip_addresses_by_version.return_value = [
        MagicMock(
            is_loopback=False, is_link_local=False, is_unspecified=False, __str__=lambda _: ADDRESS
        )
    ]
    return provider.on_mdns_service_state_change(SERVICE_NAME, ServiceStateChange.Updated, info)


async def _let_the_check_run() -> None:
    """Let a started check run to its end on the virtual clock."""
    await asyncio.sleep(SATELLITE_RECHECK_INTERVAL + 60)


async def test_a_speaker_that_became_a_satellite_loses_its_player(
    timer_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A known speaker that answers twice as a satellite, some time apart, is removed once."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO, SATELLITE_INFO])
    caplog.set_level(logging.INFO)

    with virtual_loop_clock():
        await _announce(setup.provider)
        await asyncio.sleep(SATELLITE_RECHECK_INTERVAL - 1)
        setup.players.remove_protocol.assert_not_awaited()
        await _let_the_check_run()

    setup.sonos_player.reconnect.assert_called_once()
    assert setup.get_info.await_count == 2
    setup.players.remove_protocol.assert_awaited_once_with(SPEAKER)
    assert "Removing Living Room as the speaker is now part of a stereo pair" in caplog.text


@pytest.mark.parametrize(
    "second_answer",
    [
        PLAYER_INFO,
        ClientError("unreachable"),
        TimeoutError(),
        {"device": {"id": "RINCON_000E58AAAAAA01400", "capabilities": ["CLOUD"]}},
        {"device": {"id": SPEAKER}},
    ],
    ids=["player-again", "no-answer", "timeout", "other-speaker", "no-capabilities"],
)
async def test_a_speaker_is_kept_unless_it_answers_as_a_satellite_twice(
    timer_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    second_answer: Any,
) -> None:
    """A player again, a missing or a foreign answer at the second look keeps the player."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO, second_answer])

    with virtual_loop_clock():
        await _announce(setup.provider)
        await _let_the_check_run()

    setup.players.remove_protocol.assert_not_awaited()
    assert [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ] == []


async def test_a_speaker_that_answers_as_a_player_first_is_not_looked_at_again(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that starts up answers as a player and is left alone."""
    setup = _provider(timer_mass, monkeypatch, [PLAYER_INFO])

    with virtual_loop_clock():
        await _announce(setup.provider)
        await _let_the_check_run()

    assert setup.get_info.await_count == 1
    setup.players.remove_protocol.assert_not_awaited()


async def test_a_speaker_that_connects_again_is_kept(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that plays again before the second look is a player, whatever it answers."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO, SATELLITE_INFO])

    with virtual_loop_clock():
        await _announce(setup.provider)
        await asyncio.sleep(1)
        setup.sonos_player.connected = True
        await _let_the_check_run()

    setup.players.remove_protocol.assert_not_awaited()


@pytest.mark.parametrize(
    ("record_provider", "wanted"),
    [("sonos--otherinst", True), (INSTANCE, False)],
    ids=["other-provider", "disabled"],
)
async def test_a_speaker_of_another_provider_or_a_disabled_player_is_not_asked(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, record_provider: str, wanted: bool
) -> None:
    """Only a speaker this provider set up, of a player that is not disabled, is looked at."""
    setup = _provider(
        timer_mass,
        monkeypatch,
        [SATELLITE_INFO, SATELLITE_INFO],
        record_provider=record_provider,
        wanted=wanted,
    )

    with virtual_loop_clock():
        await _announce(setup.provider)
        await _let_the_check_run()

    setup.get_info.assert_not_awaited()
    setup.players.remove_protocol.assert_not_awaited()


async def test_a_speaker_without_a_stored_protocol_is_not_asked(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A satellite that never was a player of this provider is left alone without a request."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO], registered=False)
    setup.config.get_protocol_record.return_value = None

    with virtual_loop_clock():
        await setup.provider._setup_player(SPEAKER, SERVICE_NAME, _discovery(ADDRESS))
        await _let_the_check_run()

    assert setup.get_info.await_count == 1  # the discovery's own request
    setup.players.remove_protocol.assert_not_awaited()


async def test_the_checks_of_one_speaker_are_rate_limited(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated announcements start one check per interval."""
    setup = _provider(timer_mass, monkeypatch, [PLAYER_INFO] * 3)

    with virtual_loop_clock():
        for _ in range(5):
            await _announce(setup.provider)
            await asyncio.sleep(1)
        assert setup.get_info.await_count == 1
        await asyncio.sleep(SATELLITE_RECHECK_INTERVAL)
        await _announce(setup.provider)
        await asyncio.sleep(1)

    assert setup.get_info.await_count == 2


def _discovery(address: str) -> MagicMock:
    """Return the mdns info of a speaker answering on the given address."""
    info = MagicMock()
    info.ip_addresses_by_version.return_value = [
        MagicMock(
            is_loopback=False, is_link_local=False, is_unspecified=False, __str__=lambda _: address
        )
    ]
    return info


async def test_a_satellite_found_after_a_restart_loses_its_stored_player(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker bonded while the server was down is removed through its stored protocol."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO] * 3, registered=False)

    with virtual_loop_clock():
        await setup.provider._setup_player(SPEAKER, SERVICE_NAME, _discovery(ADDRESS))
        await _let_the_check_run()

    assert setup.get_info.await_count == 3  # the discovery's own request and the two looks
    setup.players.remove_protocol.assert_awaited_once_with(SPEAKER)


async def test_a_satellite_at_a_manual_address_loses_its_player(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker entered by IP address that became a satellite is removed as well."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO] * 3)
    cast("MagicMock", setup.provider.config).get_value.return_value = [ADDRESS]

    with virtual_loop_clock():
        await setup.provider.discover_players()
        await _let_the_check_run()

    setup.players.remove_protocol.assert_awaited_once_with(SPEAKER)


async def test_a_speaker_that_left_its_set_is_found_as_a_new_player(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker taken out of its set answers as a player again and is set up as one."""
    setup = _provider(timer_mass, monkeypatch, [PLAYER_INFO], registered=False)
    setup.config.get_protocol_record.return_value = None
    player_class = MagicMock()
    player_class.return_value.setup = AsyncMock()
    monkeypatch.setattr("music_assistant.providers.sonos.provider.SonosPlayer", player_class)

    await setup.provider._setup_player(SPEAKER, SERVICE_NAME, _discovery(ADDRESS))

    player_class.assert_called_once_with(setup.provider, SPEAKER, discovery_info=PLAYER_INFO)
    player_class.return_value.setup.assert_awaited_once()


async def test_unload_ends_a_running_check(
    timer_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that waits for its second look ends with the provider."""
    setup = _provider(timer_mass, monkeypatch, [SATELLITE_INFO, SATELLITE_INFO])
    setup.provider._cloud_queue = MagicMock()

    with virtual_loop_clock():
        await _announce(setup.provider)
        await asyncio.sleep(1)
        await setup.provider.unload()
        await _let_the_check_run()

    setup.players.remove_protocol.assert_not_awaited()
