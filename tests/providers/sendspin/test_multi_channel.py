"""Tests for the multi-channel players the Sendspin provider combines from Sendspin speakers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server import ClientConnectedEvent, ClientDisconnectedEvent
from music_assistant_models.enums import (
    ChannelLayout,
    PlaybackState,
    PlayerType,
    SpeakerChannel,
)
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.models.player import PlayerMedia
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

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

LEFT = "speaker-left"
RIGHT = "speaker-right"
DEN = "speaker-den"
SPEAKER_FORMAT = SupportedAudioFormat(
    codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=24
)


class _SpeakerRole(BridgePlayerRole):
    """The player role of a Sendspin speaker, with what a device declares in its state."""

    volume_commands: list[int]
    # levels sent that the speaker reports once the test lets it, None for at once
    unreported: list[int] | None

    @property
    def state_supported_commands(self) -> list[PlayerCommand]:
        """Return the commands the speaker accepts."""
        return [PlayerCommand.VOLUME, PlayerCommand.MUTE, PlayerCommand.SET_OUTPUT_DELAY]

    def get_output_delay_ms(self) -> int:
        """Return the output delay of the speaker."""
        return cast("int", getattr(self, "output_delay_ms", 0))

    def set_output_delay(self, delay_ms: int) -> None:
        """Take an output delay the server sends."""
        self.output_delay_ms = delay_ms

    def get_supported_formats(self) -> list[SupportedAudioFormat]:
        """Return the formats the speaker plays."""
        return [SPEAKER_FORMAT]

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
    as_player: bool = True,
) -> _SpeakerRole:
    """
    Let a Sendspin speaker connect.

    :param client_id: The client id of the speaker.
    :param name: The name it reports.
    :param volume: The volume it is at.
    :param manufacturer: The manufacturer it reports.
    :param as_player: Wait until it is a player of its own.
    """
    hello = ClientHelloPayload(
        client_id=client_id,
        name=name,
        version=1,
        supported_roles=[BRIDGE_ROLE_ID],
        device_info=SendspinDeviceInfo(product_name="Speaker", manufacturer=manufacturer),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[SPEAKER_FORMAT], buffer_capacity=1_000
        ),
    )
    client = sendspin.server_api.register_external_player(
        hello, on_stream_start=lambda _request: None
    )
    role = cast("Any", client.roles_by_family("player")[0])
    role.__class__ = _SpeakerRole
    role.volume_commands = []
    role.unreported = None
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

    await mass.config.save_player_config(player.player_id, {"speaker_1_channel": "front_right"})

    assert sendspin.speaker_output(LEFT)[1] == SpeakerChannel.FRONT_RIGHT
    assert sendspin.speaker_output(RIGHT)[1] == SpeakerChannel.FRONT_LEFT
    await _wait_for(lambda: restart.await_count == 1)


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
