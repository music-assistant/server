"""Tests for the Sendspin session sample rate following the first track."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
from aiosendspin.models import AudioCodec
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.push_stream import (
    _create_resampler_state,
    _processing_format_for_roles,
    _resample_pcm_standalone,
    _ResamplerKey,
)
from aiosendspin.server.roles.player.v1 import PlayerPersistentState, PlayerV1Role
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession
from music_assistant.providers.sendspin.player import (
    CONF_PREFERRED_SENDSPIN_FORMAT,
    SENDSPIN_FORMAT_AUTOMATIC,
    SendspinPlayer,
)
from tests.common import MockProvider, attach_mock, mock_player_mass

if TYPE_CHECKING:
    import pytest

    from music_assistant.models.player_protocol import PlayerProtocol

PLAYER_ID = "client-1"
MEMBER_ID = "client-2"
FLAC_48K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=16)
FLAC_44K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=16)
FLAC_96K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=96000, bit_depth=24)
OPUS_48K = SupportedAudioFormat(codec=AudioCodec.OPUS, channels=2, sample_rate=48000, bit_depth=16)


async def test_session_follows_first_track_rate() -> None:
    """A 44.1 kHz first track plays at 44.1 kHz in the client's own top codec."""
    player, role = _player([FLAC_48K, FLAC_44K])
    session = _session(player, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 44100
    assert role.preferred_codec == AudioCodec.FLAC
    assert role.preferred_format == SendspinAudioFormat(44100, 16, 2)
    # a config update during the session keeps the steered format
    await player._apply_preferred_format()
    assert role.preferred_format == SendspinAudioFormat(44100, 16, 2)


async def test_explicit_format_is_not_overridden() -> None:
    """An explicitly configured Sendspin format wins over the session sample rate."""
    player, role = _player([FLAC_44K, FLAC_48K], preferred_format="flac:48000:16:2")
    session = _session(player, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert role.preferred_format == SendspinAudioFormat(48000, 16, 2)


async def test_rate_missing_in_top_codec_keeps_client_default() -> None:
    """Without the session rate in its top codec, the client keeps its own default format."""
    player, role = _player([OPUS_48K, FLAC_44K])
    session = _session(player, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert role.preferred_codec == AudioCodec.OPUS
    assert role._state().preferred_format_override is None


def test_supported_rates_follow_the_codec_in_use() -> None:
    """Only rates of the client's top codec, or of an explicit format, are reported."""
    automatic, _ = _player([OPUS_48K, FLAC_44K])
    explicit, _ = _player([OPUS_48K, FLAC_44K], preferred_format="flac:44100:16:2")

    assert automatic.supported_sample_rates == [(48000, 16)]
    assert explicit.supported_sample_rates == [(44100, 16)]


async def test_session_without_queue_item_returns_to_client_default() -> None:
    """A session without a first queue item (e.g. an audio source) uses the client's default."""
    player, role = _player([FLAC_48K, FLAC_44K])
    session = _session(player, sample_rate=44100)
    await session._follow_session_sample_rate([PLAYER_ID])

    session._start_streamdetails = None
    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000
    assert role._state().preferred_format_override is None


async def test_unlisted_explicit_format_reports_client_rates() -> None:
    """An explicit format the client no longer lists does not limit the supported rates."""
    player, _ = _player([FLAC_48K, FLAC_44K], preferred_format="flac:96000:24:2")

    assert player.supported_sample_rates == [(44100, 16), (48000, 16)]


async def test_late_joining_member_follows_session_rate() -> None:
    """A member joining a running session is steered to the session's first track rate."""
    leader, _ = _player([FLAC_48K, FLAC_44K])
    member, member_role = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    session = _session(leader, sample_rate=44100)
    players = {PLAYER_ID: leader, MEMBER_ID: member}
    cast("MagicMock", leader.mass).players.get_player.side_effect = players.get

    await session.add_member(MEMBER_ID)

    assert member_role.preferred_format == SendspinAudioFormat(44100, 16, 2)


async def test_group_session_takes_the_source_rate_a_member_plays() -> None:
    """A 96 kHz track in a group led by a 48 kHz player runs at 96 kHz for its 96 kHz member."""
    leader, leader_role = _player([FLAC_48K])
    member, member_role = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, sendspin_format = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 96000
    assert sendspin_format.sample_rate == 96000
    assert member_role.preferred_format == SendspinAudioFormat(96000, 24, 2)
    assert leader_role.preferred_format == SendspinAudioFormat(48000, 16, 2)


async def test_group_session_follows_a_44k_track_a_member_plays() -> None:
    """A 44.1 kHz track in a group led by a 48 kHz player runs at 44.1 kHz for its 44.1 kHz member."""
    leader, _ = _player([FLAC_48K])
    member, member_role = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 44100
    assert member_role.preferred_format == SendspinAudioFormat(44100, 16, 2)


async def test_group_of_48k_members_stays_at_48k() -> None:
    """A group whose members only play 48 kHz runs at 48 kHz for 44.1 and 96 kHz tracks."""
    for track_rate in (44100, 96000):
        leader, _ = _player([FLAC_48K])
        member, _ = _player([OPUS_48K], player_id=MEMBER_ID)
        _group(leader, member)
        session = _session(leader, sample_rate=track_rate)

        await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
        pcm_format, _ = session._select_session_pcm_formats()

        assert pcm_format.sample_rate == 48000


async def test_group_session_does_not_go_above_the_track_rate() -> None:
    """A member with a configured 96 kHz format does not lift a 44.1 kHz track above 48 kHz."""
    leader, _ = _player([FLAC_48K])
    member, member_role = _player(
        [FLAC_48K, FLAC_96K], player_id=MEMBER_ID, preferred_format="flac:96000:24:2"
    )
    _group(leader, member)
    session = _session(leader, sample_rate=44100)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert member_role.preferred_format == SendspinAudioFormat(96000, 24, 2)
    assert pcm_format.sample_rate == 48000


async def test_lossy_leader_does_not_cap_the_group() -> None:
    """A leader that plays a lossy codec does not hold a 96 kHz member at 48 kHz."""
    leader, leader_role = _player([OPUS_48K])
    member, _ = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert leader_role.preferred_codec == AudioCodec.OPUS
    assert pcm_format.sample_rate == 96000


async def test_lossy_member_counts_with_at_most_48k(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member with a lossy codec counts with 48 kHz, whatever rate it plays."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)
    formats = {
        PLAYER_ID: AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100),
        MEMBER_ID: AudioFormat(content_type=ContentType.OPUS, sample_rate=96000),
    }
    monkeypatch.setattr(session, "_get_member_preferred_format", formats.get)

    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000


async def test_hand_picked_leader_format_does_not_cap_the_group() -> None:
    """A leader with a configured 48 kHz format keeps it, while the group runs at 96 kHz."""
    leader, leader_role = _player([FLAC_48K, FLAC_96K], preferred_format="flac:48000:16:2")
    member, _ = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert leader_role.preferred_format == SendspinAudioFormat(48000, 16, 2)
    assert pcm_format.sample_rate == 96000


async def test_group_session_without_first_item_keeps_leader_rate() -> None:
    """Without a first item to follow, the group runs at the leader's rate."""
    leader, _ = _player([FLAC_96K, FLAC_48K])
    member, _ = _player([FLAC_48K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=44100)
    session._start_streamdetails = None

    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 96000


async def test_group_client_without_player_role_does_not_count() -> None:
    """A group client that plays no audio leaves the session rate to the members that do."""
    leader, _ = _player([FLAC_48K])
    controller, _ = _player([], player_id=MEMBER_ID)
    cast("MagicMock", controller.api).roles_by_family.return_value = []
    _group(leader, controller)
    session = _session(leader, sample_rate=96000)
    session._pcm_format = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=96000)

    await session._follow_session_sample_rate([PLAYER_ID])
    pcm_format, _ = session._select_session_pcm_formats()

    assert pcm_format.sample_rate == 48000


async def test_tone_above_24khz_reaches_a_96k_member() -> None:
    """A 30 kHz tone in a 96 kHz track reaches a 96 kHz member of a group led by a 48 kHz player."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_96K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=96000)
    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()
    member_rate = session._get_member_sample_rate(MEMBER_ID)
    leader_rate = session._get_member_sample_rate(PLAYER_ID)
    assert member_rate == 96000
    assert leader_rate == 48000

    with_group_rate = await _tone_level_at_member(pcm_format.sample_rate, member_rate)
    with_leader_rate = await _tone_level_at_member(leader_rate, member_rate)

    assert with_group_rate > 0.9
    assert with_leader_rate < 0.001


async def test_consecutive_44k_tracks_flow_without_restart() -> None:
    """A group of a 48 kHz leader and a 44.1 kHz member flows from one 44.1 kHz track to the next."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)

    assert await _flow_items_played(leader, [44100, 44100, 44100]) == ["0", "1", "2"]


async def test_higher_track_rate_a_member_plays_restarts_the_flow() -> None:
    """A 96 kHz track restarts the flow when a member plays 96 kHz."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    hires_member, _ = _player([FLAC_48K, FLAC_96K], player_id="client-3")
    _group(leader, member, hires_member)

    assert await _flow_items_played(leader, [44100, 44100, 96000]) == ["0", "1"]
    assert await _flow_items_played(leader, [48000, 48000, 96000]) == ["0", "1"]


async def test_higher_track_rate_no_member_plays_keeps_the_flow() -> None:
    """A 96 kHz track keeps a 48 kHz flow going when the members play 48 and 44.1 kHz."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)

    assert await _flow_items_played(leader, [48000, 96000, 44100]) == ["0", "1", "2"]


async def test_96k_track_lifts_a_44k_flow_to_48k_as_for_a_lone_player() -> None:
    """A 96 kHz track restarts a 44.1 kHz flow at 48 kHz, as for a lone 44.1/48 kHz player."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([FLAC_48K, FLAC_44K], player_id=MEMBER_ID)
    _group(leader, member)
    lone_player, _ = _player([FLAC_48K, FLAC_44K], player_id="client-3")

    assert await _flow_items_played(leader, [44100, 96000]) == ["0"]
    assert await _flow_items_played(lone_player, [44100, 96000]) == ["0"]


async def test_group_of_48k_members_restarts_as_its_leader_alone() -> None:
    """A group of 48 kHz members takes the same restart decisions as its leader's rates."""
    leader, _ = _player([FLAC_48K])
    member, _ = _player([OPUS_48K], player_id=MEMBER_ID)
    _group(leader, member)
    session = _session(leader, sample_rate=44100)
    await session._follow_session_sample_rate([PLAYER_ID, MEMBER_ID])
    pcm_format, _ = session._select_session_pcm_formats()
    leader.playback_session = session
    audio = leader.mass.streams.audio
    supported = sorted({rate for rate, _ in leader.get_supported_sample_rates()})

    for next_rate in (22050, 44100, 48000, 88200, 96000, 192000):
        item = _flow_item("next", next_rate)
        group_rate = await leader.get_flow_sample_rate(item.streamdetails)
        decisions = [
            audio._flow_stream_needs_restart(
                cast("Any", item),
                pcm_format,
                supported,
                FLOW_MODE_SAMPLE_RATE_SMART,
                is_first_track=False,
                next_flow_sample_rate=flow_rate,
            )
            for flow_rate in (group_rate, None)
        ]
        assert decisions == [False, False], next_rate


async def test_flow_of_other_players_follows_their_supported_rates() -> None:
    """A player outside a Sendspin group restarts its flow by its own supported rates."""
    mass = mock_player_mass()
    _, protocol = attach_mock(MockProvider("test", mass=mass), "renderer-1")
    protocol._attr_supported_sample_rates = [(48000, 16), (96000, 24)]

    assert await protocol.get_flow_sample_rate(_flow_item("0", 96000).streamdetails) is None
    assert await _flow_items_played(protocol, [44100, 48000, 44100], 48000) == ["0", "1", "2"]
    assert await _flow_items_played(protocol, [44100, 48000, 96000], 48000) == ["0", "1"]


def _player(
    formats: list[SupportedAudioFormat],
    *,
    player_id: str = PLAYER_ID,
    preferred_format: str = SENDSPIN_FORMAT_AUTOMATIC,
) -> tuple[SendspinPlayer, PlayerV1Role]:
    """Build an un-initialized SendspinPlayer around a real player role."""
    client = MagicMock(client_id=player_id)
    client.info.player_support.supported_formats = formats
    client.group.has_active_stream = False
    client.get_or_create_role_state.return_value = PlayerPersistentState()
    role = PlayerV1Role(client)
    role._ensure_preferred_format()
    client.roles_by_family.return_value = [role]
    client.group.clients = [client]

    audio = StreamsAudio(MagicMock())
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=False)
    )
    config_values = {
        CONF_PREFERRED_SENDSPIN_FORMAT: preferred_format,
        CONF_FLOW_MODE_SAMPLE_RATE: FLOW_MODE_SAMPLE_RATE_SMART,
    }
    player = SendspinPlayer.__new__(SendspinPlayer)
    player._protocol_id = player_id
    player._player = None
    player.api = client
    player.logger = MagicMock()
    player._config = MagicMock()
    player._config.get_value.side_effect = lambda key, default=None: config_values.get(key, default)
    player._provider = MagicMock()
    player._provider.server_api.get_client.return_value = client
    player._provider.get_protocol = lambda pid: player.mass.players.get_player(pid)
    player.mass = MagicMock()
    player.mass.streams.audio = audio
    player.mass.players.get_player.return_value = player
    return player, role


def _group(*players: SendspinPlayer) -> None:
    """Put the given players in one Sendspin group, led by the first."""
    clients = {player.protocol_id: player.api for player in players}
    players_by_id = {player.protocol_id: player for player in players}
    for player in players:
        cast("MagicMock", player.api).group.clients = list(clients.values())
        cast("MagicMock", player.provider).server_api.get_client.side_effect = clients.get
        cast("MagicMock", player.mass).players.get_player.side_effect = players_by_id.get


async def _flow_items_played(
    protocol: PlayerProtocol, item_rates: list[int], flow_rate: int | None = None
) -> list[str]:
    """
    Return the items a flow stream for a protocol plays before it ends or restarts.

    :param protocol: The protocol that consumes the flow stream.
    :param item_rates: The sample rates of the queue items, from the first on.
    :param flow_rate: The rate of the flow stream; by default the rate of the Sendspin
        session of the protocol for the first item.
    """
    items = [_flow_item(str(index), rate) for index, rate in enumerate(item_rates)]
    if flow_rate is None:
        leader = cast("SendspinPlayer", protocol)
        session = SendspinPlaybackSession(leader)
        session._start_streamdetails = items[0].streamdetails
        await session._follow_session_sample_rate(
            [client.client_id for client in leader.api.group.clients]
        )
        flow_rate = session._select_session_pcm_formats()[0].sample_rate
        leader.playback_session = session

    async def _load_next(_queue_id: str, current_id: str) -> Any:
        if (index := int(current_id) + 1) < len(items):
            return items[index]
        raise QueueEmpty

    async def _one_chunk(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        yield b"\x00" * 8

    audio = StreamsAudio(MagicMock())
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass = cast("MagicMock", audio.mass)
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item = _load_next
    mass.player_queues.get_active_queue.return_value = None
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 0
    audio.get_queue_item_stream = _one_chunk  # type: ignore[method-assign]
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    pcm_format = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=flow_rate)
    async for _chunk in audio.get_queue_flow_stream(
        cast("Any", queue),
        cast("Any", items[0]),
        pcm_format,
        session_id="session-1",
        protocol_player=protocol,
    ):
        pass
    return [entry.queue_item_id for entry in queue_data.flow_mode_stream_log]


def _flow_item(item_id: str, sample_rate: int) -> SimpleNamespace:
    """Return a queue item of a track with the given sample rate."""
    return SimpleNamespace(
        queue_item_id=item_id,
        name=item_id,
        media_type=MediaType.TRACK,
        duration=300,
        extra_attributes={},
        streamdetails=StreamDetails(
            provider="test",
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.FLAC, sample_rate=sample_rate, bit_depth=16
            ),
            media_type=MediaType.TRACK,
            duration=300,
        ),
    )


async def _tone_level_at_member(session_rate: int, member_rate: int) -> float:
    """
    Return the level of a 30 kHz tone in a 96 kHz source as a 96 kHz-rate member receives it.

    The source is converted to the session rate by ffmpeg as the stream pipeline does, then to
    the member rate by the resampler of the Sendspin library.

    :param session_rate: The sample rate of the session.
    :param member_rate: The sample rate the member plays.
    """
    source_rate, tone_hz, amplitude = 96000, 30000, 0.5
    samples = amplitude * np.sin(2 * np.pi * tone_hz * np.arange(source_rate) / source_rate)
    source_pcm = np.repeat(samples, 2).astype("<f4").tobytes()

    async def _source() -> AsyncGenerator[bytes]:
        yield source_pcm

    session_pcm = b"".join(
        [
            chunk
            async for chunk in get_ffmpeg_stream(
                _source(),
                AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=source_rate),
                AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=session_rate),
            )
        ]
    )
    session_format = SendspinAudioFormat(session_rate, 32, 2, sample_type="float")
    member_format = _processing_format_for_roles(
        session_format, target_sample_rate=member_rate, target_bit_depth=24, target_channels=2
    )
    key = _ResamplerKey(
        channel_id=uuid4(),
        source_format=session_format,
        target_sample_rate=member_rate,
        target_channels=2,
        target_bit_depth=member_format.bit_depth,
        target_sample_type=member_format.sample_type,
    )
    state = _create_resampler_state(key, session_format, member_format)
    member_pcm = _resample_pcm_standalone(state, session_pcm, session_format, 0).pcm_data

    # the middle half second, clear of the resamplers' start and end transients
    left = np.frombuffer(member_pcm, dtype="<f4")[::2]
    middle = left[len(left) // 4 : len(left) // 4 + member_rate // 2]
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    tone_bin = round(tone_hz * len(middle) / member_rate)
    # the hann window halves the peak of a sine that falls on a bin
    return float(spectrum[tone_bin - 2 : tone_bin + 3].max() / (amplitude * len(middle) / 4))


def _session(player: SendspinPlayer, *, sample_rate: int) -> SendspinPlaybackSession:
    """Return a playback session whose first queue item has the given sample rate."""
    streamdetails = StreamDetails(
        provider="test",
        item_id="1",
        audio_format=AudioFormat(
            content_type=ContentType.FLAC, sample_rate=sample_rate, bit_depth=16
        ),
        media_type=MediaType.TRACK,
    )
    cast("MagicMock", player.mass).player_queues.get_item.return_value = MagicMock(
        streamdetails=streamdetails
    )
    session = SendspinPlaybackSession(player)
    media = PlayerMedia(uri="track-1", source_id="queue-1", queue_item_id="item-1")
    session._start_streamdetails = session._get_start_streamdetails(media)
    return session
