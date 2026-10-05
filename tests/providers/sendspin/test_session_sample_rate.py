"""Tests for the Sendspin session sample rate following the first track."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from aiosendspin.models import AudioCodec
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.roles.player.v1 import PlayerPersistentState, PlayerV1Role
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession
from music_assistant.providers.sendspin.player import (
    CONF_PREFERRED_SENDSPIN_FORMAT,
    SENDSPIN_FORMAT_AUTOMATIC,
    SendspinPlayer,
)

PLAYER_ID = "client-1"
MEMBER_ID = "client-2"
FLAC_48K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=16)
FLAC_44K = SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=16)
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
    player._player_id = player_id
    player.api = client
    player.logger = MagicMock()
    player._config = MagicMock()
    player._config.get_value.side_effect = lambda key, default=None: config_values.get(key, default)
    player._provider = MagicMock()
    player._provider.server_api.get_client.return_value = client
    player.mass = MagicMock()
    player.mass.streams.audio = audio
    player.mass.players.get_player.return_value = player
    return player, role


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
