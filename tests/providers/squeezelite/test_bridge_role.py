"""
Tests for the Sendspin player role of the bridge of a slimproto player.

The role is tested against what it has to fit: a real Sendspin server, which makes the
role for the hello of a bridge and delivers its audio, and the code of the Sendspin
provider that picks the format of a player for a playback session.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import ClientUpdatedEvent, SendspinServer
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.roles.player.types import PlayerRoleProtocol
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_FLOW_MODE_SAMPLE_RATE
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.providers.sendspin.bridge_role import BridgePlayerRole
from music_assistant.providers.sendspin.player import (
    CONF_PREFERRED_SENDSPIN_FORMAT,
    SendspinPlayer,
)
from music_assistant.providers.squeezelite.bridge_role import (
    SLIMPROTO_BRIDGE_ROLE_ID,
    SlimprotoBridgeRole,
)
from music_assistant.providers.squeezelite.sendspin_bridge import _pack_24_bit

if TYPE_CHECKING:
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import AudioChunk

# The formats of a player that plays up to 48 kHz, as its bridge lists them in its hello.
FORMATS = [(44100, 24), (44100, 16), (48000, 24), (48000, 16)]


def _hello(formats: list[tuple[int, int]]) -> ClientHelloPayload:
    """
    Return the hello of a bridge client.

    :param formats: The sample rate and bit depth of the PCM formats it lists.
    """
    return ClientHelloPayload(
        client_id="spb_aabbccddeeff",
        name="Kitchen",
        version=1,
        supported_roles=[SLIMPROTO_BRIDGE_ROLE_ID, "player@v1"],
        device_info=SendspinDeviceInfo(product_name="SqueezeLite", manufacturer="squeezelite"),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=sample_rate, bit_depth=bit_depth
                )
                for sample_rate, bit_depth in formats
            ],
            buffer_capacity=1_000,
            supported_commands=[PlayerCommand.VOLUME],
        ),
    )


def _role(formats: list[tuple[int, int]] = FORMATS) -> SlimprotoBridgeRole:
    """Return the role of a client whose hello lists the given formats, on its default."""
    role = SlimprotoBridgeRole(client=MagicMock(info=_hello(formats)))
    assert role.set_preferred_format(None)
    return role


def _plays(role: SlimprotoBridgeRole) -> tuple[int, int, int]:
    """Return the sample rate, bit depth and channels the role asks Sendspin for."""
    requirements = role.get_audio_requirements()
    assert requirements is not None
    return requirements.sample_rate, requirements.bit_depth, requirements.channels


def _pcm(sample_rate: int, bit_depth: int) -> SendspinAudioFormat:
    """Return a stereo format as Sendspin names one."""
    return SendspinAudioFormat(sample_rate=sample_rate, bit_depth=bit_depth, channels=2)


@pytest.fixture
async def server() -> AsyncGenerator[SendspinServer]:
    """Return a Sendspin server that is not started."""
    server = SendspinServer(
        asyncio.get_running_loop(),
        Identity.generate(),
        "test",
        pairing_store=InMemoryServerPairingStore(),
    )
    yield server
    await server.close()


def _registered(server: SendspinServer) -> tuple[SendspinClient, SlimprotoBridgeRole]:
    """Register a bridge client with a server and return it with the role it was given."""
    client = server.register_external_player(_hello(FORMATS), on_stream_start=lambda _: None)
    (role,) = client.roles_by_family("player")
    assert isinstance(role, SlimprotoBridgeRole)
    return client, role


def _sendspin_player(role: SlimprotoBridgeRole, config: dict[str, str]) -> Any:
    """
    Return the Sendspin player of a bridge client, as far as it picks formats.

    :param role: The player role of its client.
    :param config: The config values of the player that are set, by key.
    """
    player = MagicMock()
    player.api.roles_by_family.return_value = [role]
    player.config.get_value.side_effect = lambda key, default=None: config.get(key, default)
    player._session_sample_rate = None
    player._player_role = SendspinPlayer._player_role.fget(player)  # type: ignore[attr-defined]
    player._top_codec_formats = SendspinPlayer._top_codec_formats
    for method in ("follow_session_sample_rate", "_apply_preferred_format", "_session_format"):
        setattr(player, method, getattr(SendspinPlayer, method).__get__(player, SendspinPlayer))
    player.get_supported_sample_rates = lambda: SendspinPlayer.supported_sample_rates.fget(  # type: ignore[attr-defined]
        player
    )
    # the format selection of Music Assistant itself, for whatever the content is
    audio = MagicMock()
    audio._pick_pcm_bit_depth.return_value = (ContentType.PCM_F32LE, 32)
    player.mass.streams.audio.select_flow_pcm_format = StreamsAudio.select_flow_pcm_format.__get__(
        audio, StreamsAudio
    )
    player.mass.streams.audio.get_output_format = StreamsAudio.get_output_format.__get__(
        audio, StreamsAudio
    )
    return player


def _track(
    sample_rate: int,
    content_type: ContentType = ContentType.FLAC,
    bit_depth: int = 16,
    media_type: MediaType = MediaType.TRACK,
) -> StreamDetails:
    """
    Return the stream details of a track, or of another item.

    :param sample_rate: The sample rate of its source.
    :param content_type: What its source is.
    :param bit_depth: The bit depth its source is probed as.
    :param media_type: What the item is.
    """
    return StreamDetails(
        provider="test",
        item_id="track",
        audio_format=AudioFormat(
            content_type=content_type, sample_rate=sample_rate, bit_depth=bit_depth
        ),
        media_type=media_type,
        stream_type=StreamType.HTTP,
        path="http://example.local/track",
    )


async def test_sendspin_makes_this_role_for_a_bridge_client(server: SendspinServer) -> None:
    """The hello of a bridge names the role, which Sendspin then creates for its client."""
    client, role = _registered(server)

    assert client.negotiated_role_ids == [SLIMPROTO_BRIDGE_ROLE_ID]
    assert role.role_id == SLIMPROTO_BRIDGE_ROLE_ID
    assert role.role_family == "player"
    # a bridge role like the others, to everything that treats those alike
    assert isinstance(role, BridgePlayerRole)
    # and a player role: what the Sendspin provider picks formats on
    assert isinstance(role, PlayerRoleProtocol)
    assert [(fmt.sample_rate, fmt.bit_depth) for fmt in role.get_supported_formats() or []] == (
        FORMATS
    )


async def test_a_hello_with_another_name_renames_the_client_and_keeps_its_role(
    server: SendspinServer,
) -> None:
    """What a bridge counts on when its player gets another name: only the name changes."""
    client, role = _registered(server)
    assert role.set_preferred_format(_pcm(48000, 24), AudioCodec.PCM)
    events: list[object] = []
    server.add_event_listener(lambda _server, event: events.append(event))
    assert client.group.group_name == "Kitchen"

    client.refresh_identity_from_hello(
        replace(client.info, name="Living room"), negotiated_roles=client.negotiated_role_ids
    )

    assert client.name == "Living room"
    # the name other Sendspin clients show for the group it founded
    assert client.group.group_name == "Living room"
    assert client.roles_by_family("player") == [role]
    assert _plays(role) == (48000, 24, 2)
    assert [(fmt.sample_rate, fmt.bit_depth) for fmt in role.get_supported_formats() or []] == (
        FORMATS
    )
    # which is what makes the Sendspin provider rename its protocol
    assert ClientUpdatedEvent(client.client_id) in events


def test_the_role_plays_the_first_format_of_the_hello_by_default() -> None:
    """Without a preference of Sendspin the role plays what its client lists first."""
    role = _role()
    assert _plays(role) == (44100, 24, 2)

    assert role.set_preferred_format(_pcm(48000, 16), AudioCodec.PCM)
    assert _plays(role) == (48000, 16, 2)
    assert role.set_preferred_format(None)
    assert _plays(role) == (44100, 24, 2)


TRACK = MediaType.TRACK
RADIO = MediaType.RADIO
PLAIN = VolumeNormalizationMode.DISABLED
NORMALIZED = VolumeNormalizationMode.DYNAMIC


@pytest.mark.parametrize(
    ("media_type", "content_type", "bit_depth", "normalization", "dsp", "session_bit_depth"),
    [
        # nothing works on the audio: the bits its first item is decoded to
        (TRACK, ContentType.FLAC, 16, PLAIN, False, 16),
        (TRACK, ContentType.FLAC, 24, PLAIN, False, 24),
        (TRACK, ContentType.MP3, 16, PLAIN, False, 16),
        # DSD is probed as 8 bits and decoded to more than a player takes
        (TRACK, ContentType.DSF, 8, PLAIN, False, 24),
        (TRACK, ContentType.WAVPACK, 8, PLAIN, False, 24),
        # volume normalization or DSP works on a track: all the bits of the player
        (TRACK, ContentType.FLAC, 16, NORMALIZED, False, 24),
        (TRACK, ContentType.FLAC, 16, PLAIN, True, 24),
        # a radio station has the bits of its source, whatever works on it
        (RADIO, ContentType.MP3, 16, NORMALIZED, False, 16),
        (RADIO, ContentType.AAC, 16, PLAIN, True, 16),
        (RADIO, ContentType.FLAC, 24, NORMALIZED, False, 24),
    ],
)
async def test_a_session_keeps_its_bit_depth_for_the_bridge(
    media_type: MediaType,
    content_type: ContentType,
    bit_depth: int,
    normalization: VolumeNormalizationMode,
    dsp: bool,
    session_bit_depth: int,
) -> None:
    """The Sendspin provider hands the bit depth of a session on, and the role plays no more."""
    role = _role()
    player = _sendspin_player(role, {})
    # the bit depth as Music Assistant picks it for the audio of a session
    audio = player.mass.streams.audio.select_flow_pcm_format.__self__
    audio._pick_pcm_bit_depth = StreamsAudio._pick_pcm_bit_depth.__get__(audio, StreamsAudio)
    audio._resolve_player_dsp_config.return_value.enabled = dsp
    role.limit_bit_depth(lambda: player.session_bit_depth)
    streamdetails = _track(48000, content_type, bit_depth, media_type)
    streamdetails.volume_normalization_mode = normalization

    await player.follow_session_sample_rate(streamdetails)

    assert player.session_bit_depth == session_bit_depth
    assert _plays(role) == (48000, session_bit_depth, 2)
    await player.follow_session_sample_rate(None)
    assert player.session_bit_depth is None
    assert _plays(role) == (44100, 24, 2)


@pytest.mark.parametrize(
    ("formats", "asked", "source_bit_depth", "plays"),
    [
        # no more bits than the source has, at the rate that is asked for
        (FORMATS, None, 16, (44100, 16, 2)),
        (FORMATS, _pcm(48000, 24), 16, (48000, 16, 2)),
        (FORMATS, None, 24, (44100, 24, 2)),
        (FORMATS, _pcm(48000, 24), 24, (48000, 24, 2)),
        # nor more than was asked for: a format the user set is not raised
        (FORMATS, _pcm(48000, 16), 24, (48000, 16, 2)),
        # a source that is not known plays as it is asked for
        (FORMATS, None, None, (44100, 24, 2)),
        # a player that plays one depth plays that
        ([(44100, 16), (48000, 16)], None, 24, (44100, 16, 2)),
        ([(44100, 24), (48000, 24)], _pcm(48000, 24), 16, (48000, 24, 2)),
        # a player that only plays more bits plays the fewest it has
        ([(44100, 32), (44100, 24)], None, 16, (44100, 24, 2)),
    ],
)
def test_the_bit_depth_follows_the_source(
    formats: list[tuple[int, int]],
    asked: SendspinAudioFormat | None,
    source_bit_depth: int | None,
    plays: tuple[int, int, int],
) -> None:
    """A 16-bit source is played in 16 bits, also by a player that plays 24."""
    role = _role(formats)
    role.limit_bit_depth(lambda: source_bit_depth)

    assert role.set_preferred_format(asked, AudioCodec.PCM if asked else None)

    assert _plays(role) == plays


def test_another_bit_depth_is_reported_to_the_group() -> None:
    """A source of another depth makes Sendspin start the stream of the role again."""
    client = MagicMock(info=_hello(FORMATS))
    role = SlimprotoBridgeRole(client=client)
    depths = iter([24, 16, 16])
    role.limit_bit_depth(lambda: next(depths))
    assert role.set_preferred_format(None)
    client.group.on_role_format_changed.assert_not_called()

    assert role.set_preferred_format(None)
    client.group.on_role_format_changed.assert_called_once_with(role)
    assert role.set_preferred_format(None)
    client.group.on_role_format_changed.assert_called_once_with(role)


@pytest.mark.parametrize(
    ("audio_format", "codec"),
    [
        (_pcm(96000, 24), AudioCodec.PCM),
        (_pcm(48000, 32), AudioCodec.PCM),
        (SendspinAudioFormat(sample_rate=48000, bit_depth=24, channels=1), AudioCodec.PCM),
        (_pcm(48000, 24), AudioCodec.FLAC),
        (_pcm(48000, 24), None),
    ],
)
def test_the_role_only_plays_a_format_of_the_hello(
    audio_format: SendspinAudioFormat, codec: AudioCodec | None
) -> None:
    """A format the player does not play is refused, and the role plays on as it did."""
    role = _role()
    assert role.set_preferred_format(_pcm(48000, 24), AudioCodec.PCM)

    assert not role.set_preferred_format(audio_format, codec)

    assert _plays(role) == (48000, 24, 2)


def test_a_role_without_formats_plays_none() -> None:
    """A hello that lists no format leaves the role without one."""
    role = SlimprotoBridgeRole(client=MagicMock(info=MagicMock(player_support=None)))

    assert role.get_supported_formats() is None
    assert not role.set_preferred_format(None)
    assert role.get_audio_requirements() is None


def test_another_format_is_reported_to_the_group() -> None:
    """Sendspin has to start the stream of the role again for it, the same format is no news."""
    role = _role()
    group = cast("MagicMock", role._client.group)
    # the first format of a role is no change: it had no stream
    group.on_role_format_changed.assert_not_called()

    assert role.set_preferred_format(None)
    assert role.set_preferred_format(_pcm(44100, 24), AudioCodec.PCM)
    group.on_role_format_changed.assert_not_called()

    assert role.set_preferred_format(_pcm(48000, 24), AudioCodec.PCM)
    group.on_role_format_changed.assert_called_once_with(role)

    # only the format counts: what else the requirements of the role hold is no change
    assert role._audio_requirements is not None
    role._audio_requirements = replace(role._audio_requirements, channel_id=uuid4())
    assert role.set_preferred_format(_pcm(48000, 24), AudioCodec.PCM)
    group.on_role_format_changed.assert_called_once_with(role)


def test_the_delay_of_the_player_is_not_that_of_sendspin() -> None:
    """Sendspin is offered no output delay: the sync delay of the player is its own setting."""
    role = _role()

    role.set_output_delay(250)

    assert role.get_output_delay_ms() == 0
    assert role.get_output_delay_us() == 0
    assert role.state_supported_commands == []


@pytest.mark.parametrize(
    ("content_rate", "plays"),
    [
        # what the player plays as it is
        (44100, (44100, 24, 2)),
        (48000, (48000, 24, 2)),
        # more than the player plays: the highest rate it has that divides it
        (96000, (48000, 24, 2)),
        (88200, (44100, 24, 2)),
        # less than the player plays: the lowest rate it has
        (22050, (44100, 24, 2)),
        # no item to tell the rate from: the default of the player
        (None, (44100, 24, 2)),
    ],
)
async def test_the_sendspin_provider_picks_the_format_for_a_session(
    content_rate: int | None, plays: tuple[int, int, int]
) -> None:
    """A session gets the sample rate of its first item where the player plays that rate."""
    role = _role()
    player = _sendspin_player(role, {})
    assert player.get_supported_sample_rates() == [
        (44100, 16),
        (44100, 24),
        (48000, 16),
        (48000, 24),
    ]
    # the session before it left the role on another format
    assert role.set_preferred_format(_pcm(48000, 16), AudioCodec.PCM)

    await player.follow_session_sample_rate(_track(content_rate) if content_rate else None)

    assert _plays(role) == plays


@pytest.mark.parametrize(
    ("config", "plays"),
    [
        # the format the user set for the Sendspin output of the player
        ({CONF_PREFERRED_SENDSPIN_FORMAT: "pcm:48000:16:2"}, (48000, 16, 2)),
        # the highest sample rate of the player, whatever is played
        ({CONF_FLOW_MODE_SAMPLE_RATE: "highest"}, (48000, 24, 2)),
    ],
)
async def test_the_settings_of_the_sendspin_output_apply(
    config: dict[str, str], plays: tuple[int, int, int]
) -> None:
    """The format and sample rate settings of a Sendspin player work for a bridged player."""
    role = _role()
    player = _sendspin_player(role, config)

    await player.follow_session_sample_rate(_track(44100))

    assert _plays(role) == plays


async def test_sendspin_delivers_24_bit_audio_in_32_bits(server: SendspinServer) -> None:
    """A 24-bit sample comes in 4 bytes with the lowest one unused: the bridge sends 3."""
    client, role = _registered(server)
    chunks: list[AudioChunk] = []
    role.set_callbacks(
        on_audio_chunk=chunks.append,
        on_volume_change=lambda _: None,
        on_mute_change=lambda _: None,
        on_stream_start=lambda: None,
        on_stream_end=lambda: None,
    )
    assert role.set_preferred_format(_pcm(48000, 24), AudioCodec.PCM)
    source = SendspinAudioFormat(sample_rate=48000, bit_depth=32, channels=2, sample_type="float")
    stream = client.group.start_stream()
    try:
        for _ in range(4):
            # 100 ms at half of full scale
            stream.prepare_audio(struct.pack("<f", 0.5) * 9600, source)
            await stream.commit_audio()
    finally:
        stream.stop()

    data = b"".join(chunk.data for chunk in chunks)
    frames = sum(chunk.duration_us for chunk in chunks) * 48000 // 1_000_000
    assert frames > 0
    assert len(data) == frames * 8
    assert set(data[0::4]) == {0}
    packed = _pack_24_bit(data)
    assert len(packed) == frames * 6
    samples = {int.from_bytes(packed[at : at + 3], "little") for at in range(0, len(packed), 3)}
    # half of full scale in 24 bits, give or take the dither
    assert all(abs(sample - 0x400000) <= 2 for sample in samples)
