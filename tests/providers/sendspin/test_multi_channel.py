"""Tests for the multi-channel players the Sendspin provider combines from Sendspin speakers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlaybackStateType, PlayerCommand
from aiosendspin.noise.trust_store import TrustedUnpairedClient
from aiosendspin.server import (
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientUpdatedEvent,
)
from aiosendspin.server import client as sendspin_client_module
from aiosendspin.server.roles.player.events import OutputDelayChangedEvent
from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import (
    ChannelLayout,
    ConfigEntryType,
    ContentType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerType,
    ProtocolRole,
    ProviderType,
    SpeakerChannel,
)
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    SetupFailedError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_PROVIDERS
from music_assistant.models.player import PlayerMedia
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sendspin import multi_channel
from music_assistant.providers.sendspin.bridge_role import BRIDGE_ROLE_ID, BridgePlayerRole
from music_assistant.providers.sendspin.constants import (
    CONF_MULTI_CHANNEL_GROUP_ID,
    MULTI_CHANNEL_ID_PREFIX,
)
from music_assistant.providers.sendspin.multi_channel import SendspinMultiChannelPlayer
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession
from music_assistant.providers.sendspin.player import _group_leader_id, _member_clients
from music_assistant.providers.sendspin.provider import SendspinProvider
from tests.common import MockProtocol

from .test_pairing_eviction import _record

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiohttp import ClientWebSocketResponse
    from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol

LEFT = "speaker-left"
RIGHT = "speaker-right"
DEN = "speaker-den"
BRIDGE = "spb_a0b765001122"
WEB = "browser-player"
# a hardware MAC address, which ties the outputs of one device to one player
SPEAKER_MAC = "a0:b7:65:00:11:22"
SPEAKER_FORMAT = SupportedAudioFormat(
    codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=24
)
FLAC_44K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=16)
FLAC_96K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=96000, bit_depth=24)


class _SpeakerRole(BridgePlayerRole):
    """The player role of a Sendspin speaker, with what a device declares in its state."""

    formats: list[SupportedAudioFormat]
    volume_commands: list[int]
    mute_commands: list[bool]
    delay_commands: list[int]
    # levels and delays sent that the speaker reports once the test lets it, None for at once
    unreported: list[int] | None
    unreported_delays: list[int] | None
    # whether the speaker reports a mute it gets, which not every client does
    reports_mute: bool

    @property
    def state_supported_commands(self) -> list[PlayerCommand]:
        """Return the commands the speaker accepts."""
        return [PlayerCommand.VOLUME, PlayerCommand.MUTE, PlayerCommand.SET_OUTPUT_DELAY]

    def get_output_delay_ms(self) -> int:
        """Return the output delay of the speaker."""
        return cast("int", getattr(self, "output_delay_ms", 0))

    def set_output_delay(self, delay_ms: int) -> None:
        """Take an output delay the server sends, and report it back."""
        self.delay_commands.append(delay_ms)
        if self.unreported_delays is None:
            self.report_delay(delay_ms)
        else:
            self.unreported_delays.append(delay_ms)

    def report_delay(self, delay_ms: int) -> None:
        """Report an output delay, one the server sent or one set on the speaker itself."""
        self.output_delay_ms = delay_ms
        self.emit_client_event(OutputDelayChangedEvent(output_delay_ms=delay_ms))

    def report_next_delay(self) -> None:
        """Report the oldest delay sent that the speaker did not report yet."""
        assert self.unreported_delays
        self.report_delay(self.unreported_delays.pop(0))

    def get_supported_formats(self) -> list[SupportedAudioFormat]:
        """Return the formats the speaker plays."""
        return self.formats

    def set_preferred_format(
        self, audio_format: SendspinAudioFormat | None, codec: AudioCodec | None = None
    ) -> bool:
        """Take the format the server prefers."""
        return True

    def set_player_volume(self, volume: int) -> None:
        """Take a volume the server sends, and report it back."""
        self.volume_commands.append(volume)
        if self.unreported is None:
            super().set_player_volume(volume)
        else:
            self.unreported.append(volume)

    def set_player_mute(self, muted: bool) -> None:
        """Take a mute the server sends, and report it back if the speaker does."""
        self.mute_commands.append(muted)
        if self.reports_mute:
            super().set_player_mute(muted)

    def report_next(self) -> None:
        """Report the oldest level sent that the speaker did not report yet."""
        assert self.unreported
        self.update_player_state(volume=self.unreported.pop(0))


def _connection() -> MagicMock:
    """Return the connection of a speaker, which the server closes when it stops."""
    return MagicMock(disconnect=AsyncMock())


def _sendspin(mass: MusicAssistant) -> SendspinProvider:
    """Return the loaded Sendspin provider."""
    provider = mass.get_provider("sendspin")
    assert provider is not None
    return cast("SendspinProvider", provider)


async def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait until a condition holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("Condition not met within timeout")


def _player_id(sendspin: SendspinProvider, client_id: str) -> str:
    """Return the id of the player of a Sendspin client."""
    protocol = sendspin.get_protocol(client_id)
    assert protocol is not None
    assert protocol.player is not None
    return protocol.player.player_id


async def _add_speaker(
    sendspin: SendspinProvider,
    client_id: str,
    name: str,
    *,
    volume: int = 40,
    manufacturer: str = "Acme",
    mac_address: str | None = None,
    as_player: bool = True,
) -> _SpeakerRole:
    """
    Let a Sendspin speaker connect.

    :param client_id: The client id of the speaker.
    :param name: The name it reports.
    :param volume: The volume it is at.
    :param manufacturer: The manufacturer it reports.
    :param mac_address: The MAC address it reports.
    :param as_player: Wait until it is a player of its own.
    """
    hello = ClientHelloPayload(
        client_id=client_id,
        name=name,
        version=1,
        supported_roles=[BRIDGE_ROLE_ID],
        device_info=SendspinDeviceInfo(
            product_name="Speaker", manufacturer=manufacturer, mac_address=mac_address
        ),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[SPEAKER_FORMAT], buffer_capacity=1_000
        ),
    )
    client = sendspin.server_api.register_external_player(
        hello, on_stream_start=lambda _request: None
    )
    role = cast("Any", client.roles_by_family("player")[0])
    role.__class__ = _SpeakerRole
    role.formats = [SPEAKER_FORMAT]
    role.volume_commands = []
    role.mute_commands = []
    role.delay_commands = []
    role.unreported = None
    role.unreported_delays = None
    role.reports_mute = True
    role._volume = volume
    client._connection = _connection()
    client._connected = True
    sendspin.event_cb(sendspin.server_api, ClientConnectedEvent(client_id))
    if as_player:
        await _wait_for(
            lambda: (
                (protocol := sendspin.get_protocol(client_id)) is not None
                and protocol.player is not None
                and protocol.player.initialized.is_set()
                and protocol.player.available
            )
        )
    return cast("_SpeakerRole", role)


async def _add_airplay_output(mass: MusicAssistant, mac_address: str) -> PlayerProtocol:
    """
    Let an AirPlay receiver of a speaker register, as another output of its device.

    :param mass: The server.
    :param mac_address: The MAC address of the device.
    """
    instance_id = "airplay--speaker"
    mass.config.set(
        f"{CONF_PROVIDERS}/{instance_id}",
        {"type": "player", "domain": "airplay", "instance_id": instance_id, "enabled": True},
    )
    config = ProviderConfig(
        values={},
        type=ProviderType.PLAYER,
        domain="airplay",
        instance_id=instance_id,
        name="AirPlay",
    )
    provider = PlayerProvider(mass, mass.get_provider_manifest("airplay"), config)
    provider.available = True
    mass._providers[instance_id] = provider
    protocol = MockProtocol(
        cast("Any", provider),
        "ap_kitchen_left",
        "Kitchen left",
        role=ProtocolRole.AUDIO,
        identifiers={IdentifierType.MAC_ADDRESS: mac_address},
    )
    await mass.players.register(protocol)
    await _wait_for(lambda: protocol.player is not None and protocol.player.initialized.is_set())
    return protocol


def _track(sample_rate: int) -> StreamDetails:
    """Return the stream details of a track at a sample rate."""
    return StreamDetails(
        provider="test",
        item_id=str(sample_rate),
        audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=sample_rate, bit_depth=16
        ),
        media_type=MediaType.TRACK,
        duration=300,
    )


def _go_away(sendspin: SendspinProvider, client_id: str) -> None:
    """Let a connected speaker drop off the network."""
    client = cast("Any", sendspin.server_api.get_client(client_id))
    client._connection = None
    client._connected = False
    sendspin.event_cb(sendspin.server_api, ClientDisconnectedEvent(client_id, None))


def _come_back(sendspin: SendspinProvider, client_id: str) -> None:
    """Let a speaker that dropped off the network connect again."""
    client = cast("Any", sendspin.server_api.get_client(client_id))
    client._connection = _connection()
    client._connected = True
    sendspin.event_cb(sendspin.server_api, ClientConnectedEvent(client_id))


def _set_of(sendspin: SendspinProvider, player: Player) -> SendspinMultiChannelPlayer:
    """Return the protocol of a multi-channel player."""
    protocol = player.own_protocol
    assert protocol is not None
    set_protocol = sendspin.get_protocol(protocol.protocol_id)
    assert isinstance(set_protocol, SendspinMultiChannelPlayer)
    return set_protocol


def _group_client_ids(protocol: SendspinMultiChannelPlayer) -> set[str]:
    """Return the client ids in the Sendspin group a multi-channel player plays in."""
    return {client.client_id for client in protocol.api.group.clients}


async def _compose(
    mass: MusicAssistant, sendspin: SendspinProvider
) -> tuple[Player, _SpeakerRole, _SpeakerRole]:
    """Combine a left and a right speaker into the multi-channel player Kitchen."""
    left = await _add_speaker(sendspin, LEFT, "Kitchen left", volume=30)
    right = await _add_speaker(sendspin, RIGHT, "Kitchen right", volume=60)
    player = await mass.players.create_multi_channel_player(
        "sendspin",
        "Kitchen",
        {
            _player_id(sendspin, LEFT): SpeakerChannel.FRONT_LEFT,
            _player_id(sendspin, RIGHT): SpeakerChannel.FRONT_RIGHT,
        },
    )
    set_protocol = _set_of(sendspin, player)
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(set_protocol))
    return player, left, right


@pytest.fixture(autouse=True)
def _quick_speaker_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a speaker that connects again and a channel change take effect at once."""
    monkeypatch.setattr(multi_channel, "SPEAKER_JOIN_GRACE", 0.05)
    monkeypatch.setattr(multi_channel, "CHANNEL_CHANGE_DEBOUNCE", 0.05)
    monkeypatch.setattr(multi_channel, "SPEAKER_GROUP_SETTLE", 0.05)


async def test_two_speakers_combine_into_one_multi_channel_player(mass: MusicAssistant) -> None:
    """
    Two Sendspin speakers become one stereo multi-channel player.

    Their own players go, the player of the set leads a Sendspin group with them, and the
    set has the volume of the speaker the user started from.
    """
    sendspin = _sendspin(mass)
    left = await _add_speaker(sendspin, LEFT, "Kitchen left", volume=30)
    right = await _add_speaker(sendspin, RIGHT, "Kitchen right", volume=60)
    left_player_id, right_player_id = _player_id(sendspin, LEFT), _player_id(sendspin, RIGHT)

    player = await mass.players.create_multi_channel_player(
        "sendspin",
        "Kitchen",
        {left_player_id: SpeakerChannel.FRONT_LEFT, right_player_id: SpeakerChannel.FRONT_RIGHT},
    )

    set_protocol = _set_of(sendspin, player)
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(set_protocol))
    assert (player.type, player.channel_layout) == (PlayerType.MULTI_CHANNEL, ChannelLayout.STEREO)
    assert (player.name, player.available) == ("Kitchen", True)
    assert player.hide_in_ui is False
    assert mass.players.get_player(left_player_id) is None
    assert mass.players.get_player(right_player_id) is None
    assert sendspin.get_protocol(LEFT) is None
    assert mass.config.get_protocol_record(LEFT) is None
    assert mass.config.get_protocol_record(RIGHT) is None
    assert not {LEFT, RIGHT} & set(set_protocol.group_members)
    assert player.volume_level == 30
    assert (left.volume_commands, right.volume_commands) == ([], [30])
    values = (mass.config.get_protocol_record(set_protocol.protocol_id) or {})["values"]
    group_id = values[CONF_MULTI_CHANNEL_GROUP_ID]
    assert len(group_id) == 43
    assert set_protocol.protocol_id == f"{MULTI_CHANNEL_ID_PREFIX}{group_id}"
    assert {key: value for key, value in values.items() if key.startswith("speaker_")} == {
        "speaker_1_client_id": LEFT,
        "speaker_1_name": "Kitchen left",
        "speaker_1_channel": "front_left",
        "speaker_1_output_delay": 0,
        "speaker_2_client_id": RIGHT,
        "speaker_2_name": "Kitchen right",
        "speaker_2_channel": "front_right",
        "speaker_2_output_delay": 0,
    }


async def test_combining_and_taking_apart_keep_the_pairing_of_each_speaker(
    mass: MusicAssistant,
) -> None:
    """
    A speaker keeps its pairing or its approval as a guest when it goes up in a set and back.

    A speaker without either can no longer connect.
    """
    sendspin = _sendspin(mass)
    store = sendspin.server_api.pairing_store
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    left_record = _record(LEFT)
    await store.store_record(left_record)
    await store.add_trusted_unpaired(TrustedUnpairedClient(client_id=RIGHT))

    player = await mass.players.create_multi_channel_player(
        "sendspin",
        "Kitchen",
        {
            _player_id(sendspin, LEFT): SpeakerChannel.FRONT_LEFT,
            _player_id(sendspin, RIGHT): SpeakerChannel.FRONT_RIGHT,
        },
    )
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(_set_of(sendspin, player)))

    assert await store.record_by_client_id(LEFT) == left_record, (
        "combining dropped the pairing of the left speaker"
    )
    assert await store.trusted_unpaired(RIGHT) is not None, (
        "combining dropped the guest approval of the right speaker"
    )

    await mass.players.remove(player.player_id)
    await _wait_for(
        lambda: all(
            (protocol := sendspin.get_protocol(client_id)) is not None
            and protocol.player is not None
            for client_id in (LEFT, RIGHT)
        )
    )

    assert await store.record_by_client_id(LEFT) == left_record, (
        "taking the set apart dropped the pairing of the left speaker"
    )
    assert await store.trusted_unpaired(RIGHT) is not None, (
        "taking the set apart dropped the guest approval of the right speaker"
    )


async def test_a_speaker_with_another_output_keeps_its_player_with_that_output(
    mass: MusicAssistant,
) -> None:
    """
    A speaker whose player has another output keeps that player when it goes up in a set.

    Only its Sendspin output moves into the set, and it comes back to the same player when
    the set is taken apart.
    """
    sendspin = _sendspin(mass)
    airplay = await _add_airplay_output(mass, SPEAKER_MAC)
    await _add_speaker(sendspin, LEFT, "Kitchen left", mac_address=SPEAKER_MAC)
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    await _wait_for(
        lambda: (
            (protocol := sendspin.get_protocol(LEFT)) is not None
            and protocol.player is airplay.player
        )
    )
    assert airplay.player is not None
    left_player_id = airplay.player.player_id

    player = await mass.players.create_multi_channel_player(
        "sendspin",
        "Kitchen",
        {
            left_player_id: SpeakerChannel.FRONT_LEFT,
            _player_id(sendspin, RIGHT): SpeakerChannel.FRONT_RIGHT,
        },
    )
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(_set_of(sendspin, player)))

    left_player = mass.players.get_player(left_player_id)
    assert left_player is not None, "the player of the speaker went with its Sendspin output"
    assert set(left_player.attached_protocols) == {airplay.protocol_id}

    await mass.players.remove(player.player_id)
    await _wait_for(
        lambda: (
            (protocol := sendspin.get_protocol(LEFT)) is not None and protocol.player is not None
        )
    )

    protocol = sendspin.get_protocol(LEFT)
    assert protocol is not None
    assert protocol.player is not None
    assert protocol.player.player_id == left_player_id, (
        "the Sendspin output came back as a player of its own"
    )
    assert set(protocol.player.attached_protocols) == {airplay.protocol_id, LEFT}


@pytest.mark.parametrize(
    ("refusal", "error"),
    [
        ("one channel twice", InvalidDataError),
        ("one speaker", InvalidDataError),
        ("an offline speaker", PlayerUnavailableError),
        ("a web player", UnsupportedFeaturedException),
    ],
)
async def test_speakers_that_can_not_be_combined_are_left_as_they_are(
    mass: MusicAssistant, refusal: str, error: type[Exception]
) -> None:
    """A request the speakers can not make a multi-channel player of changes nothing."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(
        sendspin,
        RIGHT,
        "Kitchen right",
        manufacturer="Music Assistant" if refusal == "a web player" else "Acme",
    )
    left_player_id, right_player_id = _player_id(sendspin, LEFT), _player_id(sendspin, RIGHT)
    members = {left_player_id: SpeakerChannel.FRONT_LEFT}
    if refusal == "one channel twice":
        members[right_player_id] = SpeakerChannel.FRONT_LEFT
    elif refusal != "one speaker":
        members[right_player_id] = SpeakerChannel.FRONT_RIGHT
    if refusal == "an offline speaker":
        _go_away(sendspin, RIGHT)

    with pytest.raises(error):
        await mass.players.create_multi_channel_player("sendspin", "Kitchen", members)

    assert mass.players.get_player(left_player_id) is not None
    assert mass.players.get_player(right_player_id) is not None
    assert not [
        protocol_id
        for protocol_id in mass.config.get_protocol_ids_of_provider(sendspin.instance_id)
        if protocol_id.startswith(MULTI_CHANNEL_ID_PREFIX)
    ]


async def test_taking_a_set_apart_gives_its_speakers_players_of_their_own(
    mass: MusicAssistant,
) -> None:
    """Removing the player of a set lets its speakers come back as new players."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    set_id = _set_of(sendspin, player).protocol_id

    await mass.players.remove(player.player_id)

    await _wait_for(
        lambda: all(
            (protocol := sendspin.get_protocol(client_id)) is not None
            and protocol.player is not None
            for client_id in (LEFT, RIGHT)
        )
    )
    assert mass.players.get_player(player.player_id) is None
    assert sendspin.server_api.get_client(set_id) is None
    assert mass.config.get_protocol_record(set_id) is None
    assert sendspin.multi_channel_player_of(LEFT) is None
    left_player = mass.players.get_player(_player_id(sendspin, LEFT))
    assert left_player is not None
    assert left_player.type == PlayerType.PLAYER
    # each in a group of its own again
    assert len(sendspin.get_protocol(LEFT).api.group.clients) == 1  # type: ignore[union-attr]


async def test_removing_the_player_a_speaker_keeps_for_another_output_leaves_it_in_its_set(
    mass: MusicAssistant,
) -> None:
    """
    Removing the player a speaker of a set keeps for another output leaves the speaker in its set.

    It keeps its pairing, also after the discovery that follows the removal.
    """
    sendspin = _sendspin(mass)
    store = sendspin.server_api.pairing_store
    airplay = await _add_airplay_output(mass, SPEAKER_MAC)
    await _add_speaker(sendspin, LEFT, "Kitchen left", mac_address=SPEAKER_MAC)
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    await _wait_for(
        lambda: (
            (protocol := sendspin.get_protocol(LEFT)) is not None
            and protocol.player is airplay.player
        )
    )
    assert airplay.player is not None
    left_player_id = airplay.player.player_id
    left_record = _record(LEFT)
    await store.store_record(left_record)
    player = await mass.players.create_multi_channel_player(
        "sendspin",
        "Kitchen",
        {
            left_player_id: SpeakerChannel.FRONT_LEFT,
            _player_id(sendspin, RIGHT): SpeakerChannel.FRONT_RIGHT,
        },
    )
    set_protocol = _set_of(sendspin, player)
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(set_protocol))

    await mass.players.remove(left_player_id)
    mass.cancel_timer(f"discover_players_{sendspin.instance_id}")
    await sendspin.discover_players()
    await asyncio.sleep(0.1)

    assert mass.players.get_player(left_player_id) is None
    assert await store.record_by_client_id(LEFT) == left_record
    assert sendspin.get_protocol(LEFT) is None
    assert sendspin.multi_channel_player_of(LEFT) == set_protocol.protocol_id
    assert set_protocol.speaker_channel(LEFT) == SpeakerChannel.FRONT_LEFT
    assert LEFT in _group_client_ids(set_protocol)


@pytest.mark.parametrize(
    ("failing_step", "error"),
    [
        pytest.param("wait", SetupFailedError, id="player of the set not registered in time"),
        pytest.param("take", RuntimeError, id="taking the speakers fails after it took them"),
    ],
)
async def test_combining_that_fails_gives_the_speakers_players_of_their_own(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
    error: type[Exception],
) -> None:
    """Combining speakers that fails on the way leaves no set, and each speaker its own player."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    speakers = {
        _player_id(sendspin, LEFT): SpeakerChannel.FRONT_LEFT,
        _player_id(sendspin, RIGHT): SpeakerChannel.FRONT_RIGHT,
    }
    wait_for_player = sendspin._wait_for_virtual_player

    async def fail_on_the_way(protocol_id: str) -> Player:
        if failing_step == "wait":
            raise SetupFailedError(f"Virtual player {protocol_id} was not registered in time")
        player = await wait_for_player(protocol_id)
        set_protocol = cast("SendspinMultiChannelPlayer", sendspin.get_protocol(protocol_id))
        take_speakers = set_protocol.take_speakers

        async def take_then_fail(*, rejoin: bool = False) -> None:
            await take_speakers(rejoin=rejoin)
            # the speakers have no player of their own any longer
            assert sendspin.get_protocol(LEFT) is None
            assert sendspin.get_protocol(RIGHT) is None
            raise RuntimeError("Taking the speakers failed")

        monkeypatch.setattr(set_protocol, "take_speakers", take_then_fail)
        return player

    monkeypatch.setattr(sendspin, "_wait_for_virtual_player", fail_on_the_way)

    with pytest.raises(error):
        await mass.players.create_multi_channel_player("sendspin", "Kitchen", speakers)
    # a registration of the set that is still under way ends first
    await asyncio.sleep(0.3)

    assert not [
        protocol_id
        for protocol_id in mass.config.get(CONF_PLAYER_PROTOCOLS) or {}
        if protocol_id.startswith(MULTI_CHANNEL_ID_PREFIX)
    ]
    assert sendspin.multi_channel_player_of(LEFT) is None
    assert sendspin.multi_channel_player_of(RIGHT) is None
    await _wait_for(
        lambda: all(
            (protocol := sendspin.get_protocol(client_id)) is not None
            and protocol.player is not None
            and protocol.player.type == PlayerType.PLAYER
            for client_id in (LEFT, RIGHT)
        )
    )


async def test_a_speaker_that_is_away_leaves_every_channel_to_the_other(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    While a speaker of a set is away the other plays every channel, until it is back.

    The set shows the speaker as offline and starts the stream again for each change, and
    a speaker that is back takes the volume of the set.
    """
    sendspin = _sendspin(mass)
    restart = AsyncMock()
    monkeypatch.setattr(mass.players, "on_player_dsp_change", restart)
    player, _, right = await _compose(mass, sendspin)
    set_protocol = _set_of(sendspin, player)
    session = set_protocol.playback_session
    assert sendspin.speaker_output(LEFT) == (set_protocol.protocol_id, SpeakerChannel.FRONT_LEFT)
    assert "pan=stereo|c0=FL|c1=FL" in session._read_pipeline_config(LEFT).filter_params

    _go_away(sendspin, RIGHT)

    assert sendspin.speaker_output(LEFT) == (set_protocol.protocol_id, None)
    assert not session._read_pipeline_config(LEFT).requires_transform
    entries = {entry.key: entry for entry in await set_protocol.get_config_entries()}
    assert (entries["speaker_1_state"].default_value, entries["speaker_2_state"].default_value) == (
        "online",
        "offline",
    )
    assert mass.players.get_player(player.player_id).available  # type: ignore[union-attr]
    await _wait_for(lambda: restart.await_count == 1)
    restart.assert_awaited_with(player.player_id)

    # it comes back with the volume it kept
    right._volume = 80
    _come_back(sendspin, RIGHT)

    assert sendspin.speaker_output(LEFT) == (set_protocol.protocol_id, SpeakerChannel.FRONT_LEFT)
    await _wait_for(lambda: right.get_player_volume() == 30)
    await _wait_for(lambda: restart.await_count == 2)
    assert mass.players.get_player(player.player_id).volume_level == 30  # type: ignore[union-attr]


async def test_a_speaker_that_rejoins_as_a_session_starts_does_not_restart_it(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A speaker that comes back as a playback session starts plays in it without a restart.

    The session plays the channels of the speakers as they are when it starts, so a restart
    that was pending for a change before it is dropped as well.
    """
    monkeypatch.setattr(multi_channel, "CHANNEL_CHANGE_DEBOUNCE", 0.5)
    sendspin = _sendspin(mass)
    restart = AsyncMock()
    monkeypatch.setattr(mass.players, "on_player_dsp_change", restart)
    player, _, _ = await _compose(mass, sendspin)
    set_protocol = _set_of(sendspin, player)
    left_client = sendspin.server_api.get_client(LEFT)
    assert left_client is not None
    await left_client.handle_availability_change(False)
    await left_client.handle_availability_change(True)
    await asyncio.sleep(0.2)

    await set_protocol.playback_session._follow_session_sample_rate(
        [client.client_id for client in set_protocol.api.group.clients]
    )

    assert LEFT in _group_client_ids(set_protocol)
    await asyncio.sleep(0.8)
    restart.assert_not_awaited()


async def test_both_speakers_away_make_the_set_unavailable(mass: MusicAssistant) -> None:
    """A set with none of its speakers connected is unavailable."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)

    _go_away(sendspin, LEFT)
    _go_away(sendspin, RIGHT)

    await _wait_for(
        lambda: not mass.players.get_player(player.player_id).available  # type: ignore[union-attr]
    )


async def test_a_set_keeps_one_volume_for_its_speakers(mass: MusicAssistant) -> None:
    """
    A volume set on the set reaches every speaker, and one set on a speaker itself the others.

    The level a speaker reports back from the set is not sent on again.
    """
    sendspin = _sendspin(mass)
    player, left, right = await _compose(mass, sendspin)
    right.volume_commands.clear()

    await mass.players.cmd_volume_set(player.player_id, 50)

    assert (left.volume_commands, right.volume_commands) == ([50], [50])

    # turned up on the dial of the left speaker
    left.update_player_state(volume=70)
    await _wait_for(
        lambda: mass.players.get_player(player.player_id).volume_level == 70  # type: ignore[union-attr]
    )

    assert (left.volume_commands, right.volume_commands) == ([50], [50, 70])
    assert (left.get_player_volume(), right.get_player_volume()) == (70, 70)


async def test_a_late_report_of_a_level_the_set_sent_changes_nothing(
    mass: MusicAssistant,
) -> None:
    """
    A speaker that reports an older level the set sent leaves the set at its newer level.

    A speaker reports a level some time after the set sent it, by when the set may have
    sent a newer one, so the speakers never send each other levels back and forth.
    """
    sendspin = _sendspin(mass)
    player, left, right = await _compose(mass, sendspin)
    right.unreported = []
    left.volume_commands.clear()
    right.volume_commands.clear()

    await mass.players.cmd_volume_set(player.player_id, 50)
    await mass.players.cmd_volume_set(player.player_id, 60)
    right.report_next()
    await asyncio.sleep(0.1)

    assert mass.players.get_player(player.player_id).volume_level == 60  # type: ignore[union-attr]
    right.report_next()
    await asyncio.sleep(0.1)
    assert (left.volume_commands, right.volume_commands) == ([50, 60], [50, 60])
    assert mass.players.get_player(player.player_id).volume_level == 60  # type: ignore[union-attr]


async def test_a_channel_set_on_a_speaker_swaps_with_the_speaker_that_had_it(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Giving a speaker the channel of the other speaker swaps their channels."""
    sendspin = _sendspin(mass)
    restart = AsyncMock()
    monkeypatch.setattr(mass.players, "on_player_dsp_change", restart)
    player, _, _ = await _compose(mass, sendspin)

    await mass.config.save_player_config(
        player.player_id, {"speaker_1_channel_setting": "front_right"}
    )

    assert sendspin.speaker_output(LEFT)[1] == SpeakerChannel.FRONT_RIGHT
    assert sendspin.speaker_output(RIGHT)[1] == SpeakerChannel.FRONT_LEFT
    await _wait_for(lambda: restart.await_count == 1)


@pytest.mark.parametrize(
    "submitted",
    [
        pytest.param(
            {"speaker_1_channel_setting": None, "speaker_2_channel_setting": None},
            id="settings reset",
        ),
        pytest.param(
            {"speaker_1_channel_setting": "", "speaker_2_channel_setting": ""}, id="settings blank"
        ),
        pytest.param({"speaker_1_channel_setting": "rear_left"}, id="no channel of the layout"),
        pytest.param({"speaker_1_channel": None, "speaker_2_channel": None}, id="record reset"),
        pytest.param({"speaker_1_channel": "", "speaker_2_channel": ""}, id="record blank"),
    ],
)
async def test_a_config_save_without_a_channel_keeps_the_channels_of_a_set(
    mass: MusicAssistant, submitted: dict[str, Any]
) -> None:
    """
    A save that resets or blanks the channels of a set keeps them, and its layout with them.

    The stored record keeps them, which the set plays again after a restart.
    """
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    set_id = _set_of(sendspin, player).protocol_id

    await mass.config.save_player_config(player.player_id, submitted)

    values = (mass.config.get_protocol_record(set_id) or {})["values"]
    channels = [speaker.channel for speaker in multi_channel.stored_speakers(values)]
    assert channels == [SpeakerChannel.FRONT_LEFT, SpeakerChannel.FRONT_RIGHT]
    assert multi_channel.layout_of(channels) == ChannelLayout.STEREO
    assert player.channel_layout == ChannelLayout.STEREO
    assert not [key for key in values if key.endswith("_channel_setting")]


@pytest.mark.parametrize("blank", [None, ""])
async def test_a_config_save_keeps_the_speakers_and_the_group_id_of_a_set(
    mass: MusicAssistant, blank: str | None
) -> None:
    """A save that resets or blanks the client ids, names and group id of a set changes nothing."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    set_protocol = _set_of(sendspin, player)
    stored = dict((mass.config.get_protocol_record(set_protocol.protocol_id) or {})["values"])

    await mass.config.save_player_config(
        player.player_id,
        {
            CONF_MULTI_CHANNEL_GROUP_ID: blank,
            **{f"speaker_{n}_{field}": blank for n in (1, 2) for field in ("client_id", "name")},
        },
    )

    assert (mass.config.get_protocol_record(set_protocol.protocol_id) or {})["values"] == stored
    assert set_protocol.speaker_channel(LEFT) == SpeakerChannel.FRONT_LEFT
    assert set_protocol.speaker_channel(RIGHT) == SpeakerChannel.FRONT_RIGHT
    assert sendspin.multi_channel_player_of(LEFT) == set_protocol.protocol_id
    assert sendspin.multi_channel_player_of(RIGHT) == set_protocol.protocol_id


async def test_each_speaker_of_a_set_shows_its_state_name_channel_and_delay(
    mass: MusicAssistant,
) -> None:
    """The config of a set has an entry for the state, name, channel and delay of each speaker."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)

    config = await mass.config.get_player_config(player.player_id)

    assert [
        (key, entry.type, entry.value)
        for key, entry in config.values.items()
        if key.startswith("speaker_")
    ] == [
        ("speaker_1_state", ConfigEntryType.LABEL, "online"),
        ("speaker_1_display_name", ConfigEntryType.LABEL, "Kitchen left"),
        ("speaker_1_channel_setting", ConfigEntryType.STRING, "front_left"),
        ("speaker_1_output_delay", ConfigEntryType.INTEGER, 0),
        ("speaker_2_state", ConfigEntryType.LABEL, "online"),
        ("speaker_2_display_name", ConfigEntryType.LABEL, "Kitchen right"),
        ("speaker_2_channel_setting", ConfigEntryType.STRING, "front_right"),
        ("speaker_2_output_delay", ConfigEntryType.INTEGER, 0),
    ]


async def test_a_config_save_keeps_the_names_of_the_speakers_of_a_set(
    mass: MusicAssistant,
) -> None:
    """A save of the config of a set keeps the stored name of each speaker, which shows again."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    set_id = _set_of(sendspin, player).protocol_id

    await mass.config.save_player_config(
        player.player_id,
        {
            "speaker_1_output_delay": 120,
            "speaker_1_display_name": None,
            "speaker_2_display_name": "Somebody else",
        },
    )

    values = (mass.config.get_protocol_record(set_id) or {})["values"]
    assert (values["speaker_1_name"], values["speaker_2_name"]) == ("Kitchen left", "Kitchen right")
    config = await mass.config.get_player_config(player.player_id)
    assert config.get_value("speaker_1_display_name") == "Kitchen left"
    assert config.get_value("speaker_2_display_name") == "Kitchen right"


async def test_a_channel_the_other_speaker_takes_back_stays_with_it(mass: MusicAssistant) -> None:
    """
    A channel one speaker takes back from the other stays with it.

    A later save of another setting of the set changes no channel.
    """
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    await mass.config.save_player_config(
        player.player_id, {"speaker_1_channel_setting": "front_right"}
    )
    await mass.config.save_player_config(
        player.player_id, {"speaker_2_channel_setting": "front_right"}
    )

    await mass.config.save_player_config(player.player_id, {"speaker_1_output_delay": 120})

    assert sendspin.speaker_output(LEFT)[1] == SpeakerChannel.FRONT_LEFT
    assert sendspin.speaker_output(RIGHT)[1] == SpeakerChannel.FRONT_RIGHT


@pytest.mark.parametrize(
    ("formats", "predicted_rate"),
    [
        pytest.param([FLAC_44K, FLAC_96K], 96000, id="speakers that play 96 kHz"),
        pytest.param([FLAC_44K], 44100, id="speakers that play 44.1 kHz only"),
    ],
)
async def test_a_set_steps_up_to_the_sample_rate_of_a_track_its_speakers_play(
    mass: MusicAssistant, formats: list[SupportedAudioFormat], predicted_rate: int
) -> None:
    """
    A set that plays 44.1 kHz picks 96 kHz for a 96 kHz track when its speakers play it.

    A regular player does the same, and the flow stream starts again at the higher rate.
    """
    sendspin = _sendspin(mass)
    player, left, right = await _compose(mass, sendspin)
    left.formats = right.formats = formats
    session = _set_of(sendspin, player).playback_session

    assert await session.get_session_sample_rate(_track(44100)) == 44100
    assert await session.get_session_sample_rate(_track(96000)) == predicted_rate


async def test_an_output_delay_goes_to_its_speaker_and_back(mass: MusicAssistant) -> None:
    """The output delay of a speaker of a set is sent to it and follows what it reports."""
    sendspin = _sendspin(mass)
    player, left, _ = await _compose(mass, sendspin)

    await mass.config.save_player_config(player.player_id, {"speaker_1_output_delay": 120})

    assert left.get_output_delay_ms() == 120


@pytest.mark.parametrize("speakers_first", [False, True])
async def test_a_set_comes_back_after_a_restart_whatever_connects_first(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, speakers_first: bool
) -> None:
    """
    After a restart a set is the same player again, with its speakers, in either order.

    A speaker that connects before its set is there is no player of its own.
    """
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    instance_id = sendspin.instance_id
    await mass.unload_provider(instance_id)
    deferred: list[tuple[str, str, str]] = []
    register_client = SendspinProvider._register_virtual_player_client
    if speakers_first:

        def _defer(
            _self: SendspinProvider,
            player_id: str,
            display_name: str,
            product_name: str = "Virtual Player",
        ) -> None:
            deferred.append((player_id, display_name, product_name))

        monkeypatch.setattr(SendspinProvider, "_register_virtual_player_client", _defer)

    await mass.load_provider(instance_id)
    sendspin = _sendspin(mass)
    if not speakers_first:
        await _wait_for(
            lambda: (protocol := player.own_protocol) is not None and protocol.player is not None
        )
    await _add_speaker(sendspin, LEFT, "Kitchen left", as_player=False)
    await _add_speaker(sendspin, RIGHT, "Kitchen right", as_player=False)
    if speakers_first:
        await asyncio.sleep(0.2)
        assert sendspin.get_protocol(LEFT) is None
        assert sendspin.get_protocol(RIGHT) is None
        monkeypatch.setattr(SendspinProvider, "_register_virtual_player_client", register_client)
        for args in deferred:
            sendspin._register_virtual_player_client(*args)

    await _wait_for(
        lambda: (
            (restored := mass.players.get_player(player.player_id)) is not None
            and restored.own_protocol is not None
            and {LEFT, RIGHT} <= _group_client_ids(_set_of(sendspin, restored))
        )
    )
    restored = mass.players.get_player(player.player_id)
    assert restored is not None
    assert (restored.type, restored.available) == (PlayerType.MULTI_CHANNEL, True)
    assert sendspin.get_protocol(LEFT) is None
    assert sendspin.get_protocol(RIGHT) is None


async def test_a_set_leads_other_sendspin_players(mass: MusicAssistant) -> None:
    """
    Another Sendspin player joins the group of a set, which lists that player but no speaker.

    The session of the set plays for its speakers and the other player, and a leader that
    leaves hands its session to the other player rather than to a speaker.
    """
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")
    den_player_id = _player_id(sendspin, DEN)
    set_protocol = _set_of(sendspin, player)
    await _wait_for(
        lambda: den_player_id in mass.players.get_player(player.player_id).can_group_with  # type: ignore[union-attr]
    )

    await mass.players.cmd_set_members(player.player_id, player_ids_to_add=[den_player_id])

    await _wait_for(lambda: DEN in set_protocol.playback_session._members)
    state = mass.players.get_player(player.player_id)
    assert state is not None
    assert state.group_members == [player.player_id, den_player_id]
    assert mass.players.get_player(den_player_id).synced_to == player.player_id  # type: ignore[union-attr]
    assert set_protocol.playback_session._members == {LEFT, RIGHT, DEN}
    clients = _member_clients(sendspin, set_protocol.api.group.clients)
    assert _group_leader_id(c for c in clients if c.client_id != set_protocol.protocol_id) == DEN


async def test_a_set_that_joins_another_leader_brings_its_speakers(mass: MusicAssistant) -> None:
    """
    A set that joins another Sendspin player plays there with its speakers.

    The leader lists the set and not its speakers, and the speakers leave with the set.
    """
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")
    den_player_id = _player_id(sendspin, DEN)
    set_protocol = _set_of(sendspin, player)
    den = sendspin.get_protocol(DEN)
    assert den is not None

    await mass.players.cmd_set_members(den_player_id, player_ids_to_add=[player.player_id])

    await _wait_for(lambda: {DEN, LEFT, RIGHT} <= _group_client_ids(set_protocol))
    await _wait_for(lambda: LEFT in cast("Any", den).playback_session._members)
    assert den.group_members == [DEN, set_protocol.protocol_id]
    assert mass.players.get_player(player.player_id).synced_to == den_player_id  # type: ignore[union-attr]
    assert cast("Any", den).playback_session._members == {LEFT, RIGHT}

    await mass.players.cmd_set_members(den_player_id, player_ids_to_remove=[player.player_id])

    await _wait_for(
        lambda: _group_client_ids(set_protocol) == {set_protocol.protocol_id, LEFT, RIGHT}
    )


async def test_a_set_plays_in_a_sync_group(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A set in a sync group plays in the group on its leader, together with its speakers.

    No player of the group lists a speaker of the set.
    """
    sendspin = _sendspin(mass)
    started: list[str] = []

    async def _start(self: SendspinPlaybackSession, _media: PlayerMedia, **_kwargs: Any) -> None:
        started.append(self.player.protocol_id)
        self.player._attr_playback_state = PlaybackState.PLAYING
        self.player.update_state()

    monkeypatch.setattr(SendspinPlaybackSession, "start", _start)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")
    den_player_id = _player_id(sendspin, DEN)
    set_protocol = _set_of(sendspin, player)
    group = await mass.players.create_group_player(
        "sync_group", "Downstairs", [player.player_id, den_player_id], dynamic=True
    )

    await mass.players.play_media(group.player_id, PlayerMedia(uri="http://test/track.mp3"))

    await _wait_for(lambda: {DEN, LEFT, RIGHT} <= _group_client_ids(set_protocol))
    assert started
    leader = sendspin.get_protocol(started[0])
    assert leader is not None
    await _wait_for(lambda: len(leader.group_members) == 2)
    assert set(leader.group_members) == {set_protocol.protocol_id, DEN}


async def test_a_set_plays_in_a_universal_group(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set in a universal group plays the stream of the group, as any player does."""
    sendspin = _sendspin(mass)
    started: dict[str, PlayerMedia] = {}

    async def _start(self: SendspinPlaybackSession, media: PlayerMedia, **_kwargs: Any) -> None:
        started[self.player.protocol_id] = media
        self.player._attr_playback_state = PlaybackState.PLAYING
        self.player.update_state()

    monkeypatch.setattr(SendspinPlaybackSession, "start", _start)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")
    set_protocol = _set_of(sendspin, player)
    await mass.config._create_provider_instance("universal_group", {})
    group = await mass.players.create_group_player(
        "universal_group", "Everywhere", [player.player_id, _player_id(sendspin, DEN)]
    )

    await mass.players.play_media(group.player_id, PlayerMedia(uri="http://test/track.mp3"))

    await _wait_for(lambda: set_protocol.protocol_id in started and DEN in started)
    assert started[set_protocol.protocol_id].custom_data == {"ugp_player_id": group.player_id}
    assert {LEFT, RIGHT} <= _group_client_ids(set_protocol)


async def test_a_speaker_being_combined_can_not_go_up_in_a_second_set(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second request for a speaker that a first request is combining is refused."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    await _add_speaker(sendspin, DEN, "Den")
    left_id, right_id, den_id = (_player_id(sendspin, c) for c in (LEFT, RIGHT, DEN))
    # the first request waits for the client of its set
    deferred: list[tuple[SendspinProvider, str, str, str]] = []
    register_client = SendspinProvider._register_virtual_player_client

    def _defer(
        self: SendspinProvider,
        player_id: str,
        display_name: str,
        product_name: str = "Virtual Player",
    ) -> None:
        deferred.append((self, player_id, display_name, product_name))

    monkeypatch.setattr(SendspinProvider, "_register_virtual_player_client", _defer)
    first = asyncio.create_task(
        mass.players.create_multi_channel_player(
            "sendspin",
            "Kitchen",
            {left_id: SpeakerChannel.FRONT_LEFT, right_id: SpeakerChannel.FRONT_RIGHT},
        )
    )
    await _wait_for(lambda: bool(deferred))

    with pytest.raises(UnsupportedFeaturedException):
        await mass.players.create_multi_channel_player(
            "sendspin",
            "Den pair",
            {left_id: SpeakerChannel.FRONT_LEFT, den_id: SpeakerChannel.FRONT_RIGHT},
        )

    monkeypatch.setattr(SendspinProvider, "_register_virtual_player_client", register_client)
    for args in deferred:
        register_client(*args)
    player = await first
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(_set_of(sendspin, player)))
    assert sendspin.multi_channel_player_of(DEN) is None
    assert mass.players.get_player(den_id) is not None


async def test_a_speaker_away_when_its_set_is_taken_apart_stays_once_it_is_back(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A speaker that is away when its set is taken apart is a player of its own once it is back.

    It comes back by itself, also after the discovery that follows the removal. The server
    keeps a client a while after it drops off; one that comes back within that time keeps its
    client, so nothing of its first connection removes it later.
    """
    monkeypatch.setattr(sendspin_client_module, "CLIENT_CLEANUP_DELAY", 0.5)
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    right_client = cast("Any", sendspin.server_api.get_client(RIGHT))
    # a speaker on the network, which the server keeps for a while after it drops off
    sendspin.server_api._external_stream_start_cbs.pop(RIGHT)
    right_client.detach_connection(None)

    await mass.players.remove(player.player_id)
    # the discovery that follows the removal runs while the speaker is still away
    mass.cancel_timer(f"discover_players_{sendspin.instance_id}")
    await sendspin.discover_players()
    await asyncio.sleep(0.1)
    if (client := cast("Any", sendspin.server_api.get_client(RIGHT))) is not None:
        # a connection within the cleanup delay takes the kept client back
        client._cancel_cleanup()
        client._connection = _connection()
        client._connected = True
        sendspin.event_cb(sendspin.server_api, ClientConnectedEvent(RIGHT))
    else:
        await _add_speaker(sendspin, RIGHT, "Kitchen right", as_player=False)

    await _wait_for(
        lambda: (
            (protocol := sendspin.get_protocol(RIGHT)) is not None and protocol.player is not None
        )
    )
    await asyncio.sleep(0.8)
    assert sendspin.server_api.get_client(RIGHT) is not None
    assert sendspin.get_protocol(RIGHT) is not None


async def test_a_level_sent_again_before_the_speaker_reports_is_not_dropped(
    mass: MusicAssistant,
) -> None:
    """
    A level the set sends back before a speaker reported the one before reaches the speaker.

    The late report of the level before leaves the set at its last level.
    """
    sendspin = _sendspin(mass)
    player, left, right = await _compose(mass, sendspin)
    right.unreported = []
    left.volume_commands.clear()
    right.volume_commands.clear()

    await mass.players.cmd_volume_set(player.player_id, 50)
    await mass.players.cmd_volume_set(player.player_id, 30)

    assert right.volume_commands == [50, 30]
    right.report_next()
    right.report_next()
    await asyncio.sleep(0.1)
    assert (left.volume_commands, right.volume_commands) == ([50, 30], [50, 30])
    assert mass.players.get_player(player.player_id).volume_level == 30  # type: ignore[union-attr]


async def test_an_unmute_reaches_a_speaker_that_does_not_report_its_mute(
    mass: MusicAssistant,
) -> None:
    """A speaker that never reports a mute it gets still gets the unmute after it."""
    sendspin = _sendspin(mass)
    player, left, right = await _compose(mass, sendspin)
    right.reports_mute = False

    await mass.players.cmd_volume_mute(player.player_id, True)
    await mass.players.cmd_volume_mute(player.player_id, False)

    assert (left.mute_commands, right.mute_commands) == ([True, False], [True, False])


async def test_a_mute_set_on_a_speaker_that_does_not_report_its_mutes_reaches_the_set(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A mute set on a speaker that never reports the mutes it gets goes to the set and the others.

    The mutes the set sent it are forgotten once the speaker would have reported them.
    """
    monkeypatch.setattr(multi_channel, "UNREPORTED_EXPIRY", 0.2)
    sendspin = _sendspin(mass)
    player, left, right = await _compose(mass, sendspin)
    right.reports_mute = False
    await mass.players.cmd_volume_mute(player.player_id, True)
    await mass.players.cmd_volume_mute(player.player_id, False)
    await asyncio.sleep(0.3)

    # muted with the button of the right speaker
    right.update_player_state(muted=True)

    await _wait_for(
        lambda: mass.players.get_player(player.player_id).volume_muted is True  # type: ignore[union-attr]
    )
    assert left.mute_commands == [True, False, True]


async def test_an_output_delay_sent_again_before_the_speaker_reports_is_not_dropped(
    mass: MusicAssistant,
) -> None:
    """
    A delay set back before a speaker reported the one before reaches the speaker.

    The late reports of the delays the set sent leave the delay that is set in place.
    """
    sendspin = _sendspin(mass)
    player, _, right = await _compose(mass, sendspin)
    right.unreported_delays = []
    right.delay_commands.clear()

    for delay_ms in (120, 0, 120):
        await mass.config.save_player_config(player.player_id, {"speaker_2_output_delay": delay_ms})

    assert right.delay_commands == [120, 0, 120]
    set_id = _set_of(sendspin, player).protocol_id
    right.report_next_delay()
    right.report_next_delay()
    assert mass.config.get_protocol_value(set_id, "speaker_2_output_delay") == 120
    right.report_next_delay()
    assert mass.config.get_protocol_value(set_id, "speaker_2_output_delay") == 120


async def test_a_speaker_that_drops_off_before_it_reports_gets_the_state_again(
    mass: MusicAssistant,
) -> None:
    """
    A speaker that drops off before it reports what the set sent gets it again once it is back.

    What was sent over the connection that ended may never have arrived.
    """
    sendspin = _sendspin(mass)
    player, _, right = await _compose(mass, sendspin)
    right.unreported = []
    right.unreported_delays = []
    right.reports_mute = False
    right.volume_commands.clear()
    right.delay_commands.clear()
    await mass.players.cmd_volume_set(player.player_id, 50)
    await mass.players.cmd_volume_mute(player.player_id, True)
    await mass.config.save_player_config(player.player_id, {"speaker_2_output_delay": 120})

    _go_away(sendspin, RIGHT)
    _come_back(sendspin, RIGHT)

    await _wait_for(lambda: len(right.volume_commands) == 2)
    assert right.volume_commands == [50, 50]
    assert right.mute_commands == [True, True]
    assert right.delay_commands == [120, 120]


async def test_a_delay_a_speaker_reports_itself_is_stored_for_it(mass: MusicAssistant) -> None:
    """An output delay set on a speaker itself is stored as the delay of that speaker."""
    sendspin = _sendspin(mass)
    player, left, _ = await _compose(mass, sendspin)

    left.report_delay(250)

    set_id = _set_of(sendspin, player).protocol_id
    assert mass.config.get_protocol_value(set_id, "speaker_1_output_delay") == 250


async def test_a_speaker_that_becomes_unavailable_is_away_until_the_next_playback(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A speaker that becomes unavailable leaves every channel to the other speaker.

    It plays with the settings of the set no more, does not come back when it is available
    again or when the set changes group, and comes back when the next playback session starts.
    """
    sendspin = _sendspin(mass)
    restart = AsyncMock()
    monkeypatch.setattr(mass.players, "on_player_dsp_change", restart)
    player, _, _ = await _compose(mass, sendspin)
    set_protocol = _set_of(sendspin, player)
    set_id = set_protocol.protocol_id
    left_client = sendspin.server_api.get_client(LEFT)
    assert left_client is not None

    # it plays its line-in
    await left_client.handle_availability_change(False)

    await _wait_for(lambda: sendspin.speaker_output(RIGHT) == (set_id, None))
    assert LEFT not in _group_client_ids(set_protocol)
    assert sendspin.speaker_output(LEFT) == (LEFT, None)
    await _wait_for(lambda: restart.await_count == 1)

    await left_client.handle_availability_change(True)
    await asyncio.sleep(0.2)
    # as when the set joins or leaves a group
    await set_protocol.take_speakers()
    assert LEFT not in _group_client_ids(set_protocol)

    await set_protocol.playback_session._follow_session_sample_rate(
        [client.client_id for client in set_protocol.api.group.clients]
    )

    assert LEFT in _group_client_ids(set_protocol)
    assert sendspin.speaker_output(LEFT) == (set_id, SpeakerChannel.FRONT_LEFT)


async def test_a_speaker_that_another_group_takes_comes_back_to_its_set(
    mass: MusicAssistant,
) -> None:
    """A speaker that its switch moves to the group of another player is taken back at once."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    set_protocol = _set_of(sendspin, player)
    await _add_speaker(sendspin, DEN, "Den")
    den_client = sendspin.server_api.get_client(DEN)
    left_client = sendspin.server_api.get_client(LEFT)
    assert den_client is not None
    assert left_client is not None
    # a group that plays is a group its switch offers
    den_client.group._set_playback_state(PlaybackStateType.PLAYING)

    await left_client.handle_switch_command()

    await _wait_for(lambda: LEFT in _group_client_ids(set_protocol))
    assert [client.client_id for client in den_client.group.clients] == [DEN]
    assert sendspin.speaker_output(LEFT) == (set_protocol.protocol_id, SpeakerChannel.FRONT_LEFT)


async def test_removing_a_disabled_set_gives_its_speakers_back(mass: MusicAssistant) -> None:
    """The speakers of a set that is disabled are players of their own once it is removed."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    instance_id = sendspin.instance_id
    await mass.config.save_player_config(player.player_id, {"enabled": False})
    await mass.unload_provider(instance_id)
    await mass.load_provider(instance_id)
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left", as_player=False)
    await _add_speaker(sendspin, RIGHT, "Kitchen right", as_player=False)
    await asyncio.sleep(0.2)
    assert sendspin.get_protocol(LEFT) is None

    await mass.players.remove(player.player_id)

    await _wait_for(
        lambda: all(
            (protocol := sendspin.get_protocol(client_id)) is not None
            and protocol.player is not None
            for client_id in (LEFT, RIGHT)
        )
    )


@pytest.mark.parametrize("event", ["added", "updated"])
async def test_a_speaker_combined_while_its_client_is_handled_stays_in_its_set(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    """A speaker that goes up in a set while a new connection of it is handled is no player."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    left_id, right_id = _player_id(sendspin, LEFT), _player_id(sendspin, RIGHT)
    entered, release = asyncio.Event(), asyncio.Event()

    async def _slow_enrichment(players: list[Any]) -> None:
        if any(player.protocol_id == LEFT for player in players):
            entered.set()
            await release.wait()

    monkeypatch.setattr(sendspin, "_apply_hass_esphome_enrichment", _slow_enrichment)
    sendspin.event_cb(
        sendspin.server_api,
        ClientAddedEvent(LEFT) if event == "added" else ClientUpdatedEvent(LEFT),
    )
    await asyncio.wait_for(entered.wait(), 5)
    player = await mass.players.create_multi_channel_player(
        "sendspin",
        "Kitchen",
        {left_id: SpeakerChannel.FRONT_LEFT, right_id: SpeakerChannel.FRONT_RIGHT},
    )
    set_protocol = _set_of(sendspin, player)
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(set_protocol))

    release.set()
    await asyncio.sleep(0.2)

    assert sendspin.get_protocol(LEFT) is None
    assert mass.config.get_protocol_record(LEFT) is None


async def test_a_set_that_leaves_its_group_while_playing_hands_the_playback_to_the_other_player(
    mass: MusicAssistant,
) -> None:
    """A set that leads a playing group and leaves it hands its playback to the other player."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")
    den_player_id = _player_id(sendspin, DEN)
    set_protocol = _set_of(sendspin, player)
    den = cast("Any", sendspin.get_protocol(DEN))
    await _wait_for(
        lambda: den_player_id in mass.players.get_player(player.player_id).can_group_with  # type: ignore[union-attr]
    )
    await mass.players.cmd_set_members(player.player_id, player_ids_to_add=[den_player_id])
    await _wait_for(lambda: DEN in set_protocol.playback_session._members)
    session = set_protocol.playback_session
    session.playback_task = asyncio.create_task(asyncio.sleep(30))
    try:
        await set_protocol.set_members(player_ids_to_remove=[set_protocol.protocol_id])

        assert den.playback_session is session
        assert session.player is den
        await _wait_for(
            lambda: _group_client_ids(set_protocol) == {set_protocol.protocol_id, LEFT, RIGHT}
        )
        assert [client.client_id for client in den.api.group.clients] == [DEN]
    finally:
        session.playback_task.cancel()


async def test_a_set_is_combined_through_the_websocket_api(mass: MusicAssistant) -> None:
    """A client combines speakers over the websocket API, naming each channel by its name."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    user = await mass.webserver.auth.create_user(username="admin_user", role=UserRole.ADMIN)
    token = await mass.webserver.auth.create_token(user, "test")

    async with mass.http_session_no_ssl.ws_connect(
        f"{mass.webserver.internal_base_url}/ws"
    ) as client:
        await client.receive_json(timeout=10)
        assert "result" in await _send(client, "auth", token=token)
        answer = await _send(
            client,
            "players/create_multi_channel_player",
            provider="sendspin",
            name="Kitchen",
            members={
                _player_id(sendspin, LEFT): "front_left",
                _player_id(sendspin, RIGHT): "front_right",
            },
        )

    assert (answer["result"]["type"], answer["result"]["channel_layout"]) == (
        "multi_channel",
        "stereo",
    )
    player = mass.players.get_player(answer["result"]["player_id"])
    assert player is not None
    await _wait_for(lambda: {LEFT, RIGHT} <= _group_client_ids(_set_of(sendspin, player)))
    assert sendspin.speaker_output(RIGHT)[1] == SpeakerChannel.FRONT_RIGHT


async def test_only_speakers_that_can_go_up_in_a_set_are_candidates(
    mass: MusicAssistant,
) -> None:
    """
    A connected Sendspin speaker of its own is a candidate for a new set.

    A bridge, a web player, a speaker that is away and a set itself are not, nor are the
    speakers of a set.
    """
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")
    await _add_speaker(sendspin, WEB, "Browser", manufacturer="Music Assistant")
    airplay = await _add_airplay_output(mass, SPEAKER_MAC)
    sendspin.register_bridge_underlying_protocol(BRIDGE, airplay.protocol_id)
    await _add_speaker(sendspin, BRIDGE, "Kitchen left", as_player=False)
    await _wait_for(
        lambda: (
            (protocol := sendspin.get_protocol(BRIDGE)) is not None and protocol.player is not None
        )
    )
    plain = "speaker-plain"
    await _add_speaker(sendspin, plain, "Office")
    _go_away(sendspin, DEN)

    candidates = sendspin.get_multi_channel_candidates()

    assert candidates == [plain], {
        "set": _set_of(sendspin, player).protocol_id in candidates,
        "bridge": BRIDGE in candidates,
        "web player": WEB in candidates,
        "away": DEN in candidates,
    }


async def test_a_set_can_not_go_up_in_another_set(mass: MusicAssistant) -> None:
    """A multi-channel player is refused as a speaker of another one, as no speaker at all."""
    sendspin = _sendspin(mass)
    player, _, _ = await _compose(mass, sendspin)
    await _add_speaker(sendspin, DEN, "Den")

    with pytest.raises(UnsupportedFeaturedException):
        await mass.players.create_multi_channel_player(
            "sendspin",
            "Everywhere",
            {
                player.player_id: SpeakerChannel.FRONT_LEFT,
                _player_id(sendspin, DEN): SpeakerChannel.FRONT_RIGHT,
            },
        )


async def test_a_speaker_being_combined_is_no_candidate(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A speaker that a request is combining into a set is no candidate for another set."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    await _add_speaker(sendspin, DEN, "Den")
    deferred: list[tuple[SendspinProvider, str, str, str]] = []
    register_client = SendspinProvider._register_virtual_player_client

    def _defer(
        self: SendspinProvider,
        player_id: str,
        display_name: str,
        product_name: str = "Virtual Player",
    ) -> None:
        deferred.append((self, player_id, display_name, product_name))

    monkeypatch.setattr(SendspinProvider, "_register_virtual_player_client", _defer)
    combining = asyncio.create_task(
        mass.players.create_multi_channel_player(
            "sendspin",
            "Kitchen",
            {
                _player_id(sendspin, LEFT): SpeakerChannel.FRONT_LEFT,
                _player_id(sendspin, RIGHT): SpeakerChannel.FRONT_RIGHT,
            },
        )
    )
    await _wait_for(lambda: bool(deferred))

    assert sendspin.get_multi_channel_candidates() == [DEN]

    monkeypatch.setattr(SendspinProvider, "_register_virtual_player_client", register_client)
    for args in deferred:
        register_client(*args)
    await combining


async def test_the_candidates_are_listed_through_the_websocket_api(mass: MusicAssistant) -> None:
    """A client gets the players that can go up in a new set over the websocket API."""
    sendspin = _sendspin(mass)
    await _add_speaker(sendspin, LEFT, "Kitchen left")
    await _add_speaker(sendspin, RIGHT, "Kitchen right")
    user = await mass.webserver.auth.create_user(username="admin_user", role=UserRole.ADMIN)
    token = await mass.webserver.auth.create_token(user, "test")

    async with mass.http_session_no_ssl.ws_connect(
        f"{mass.webserver.internal_base_url}/ws"
    ) as client:
        await client.receive_json(timeout=10)
        assert "result" in await _send(client, "auth", token=token)
        answer = await _send(client, "players/multi_channel_candidates", provider="sendspin")

    assert sorted(answer["result"]) == sorted(
        [_player_id(sendspin, LEFT), _player_id(sendspin, RIGHT)]
    )


async def _send(client: ClientWebSocketResponse, command: str, **args: Any) -> dict[str, Any]:
    """
    Send a command over the websocket API and return the answer to it.

    :param client: The connected client.
    :param command: The command.
    :param args: Its arguments.
    """
    message_id = uuid4().hex
    await client.send_json({"message_id": message_id, "command": command, "args": args})
    while True:
        message: dict[str, Any] = await client.receive_json(timeout=10)
        if message.get("message_id") == message_id:
            return message
