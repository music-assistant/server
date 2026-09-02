"""Test that PCM formats follow the arriving audio, not the advertised source."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

if TYPE_CHECKING:
    from music_assistant.models.player import Player


def _streamdetails(
    advertised: AudioFormat,
    arriving: AudioFormat | None,
    media_type: MediaType = MediaType.TRACK,
) -> StreamDetails:
    """Return StreamDetails advertising one format while delivering another."""
    return StreamDetails(
        provider="test--1",
        item_id="1",
        audio_format=advertised,
        decoded_audio_format=arriving,
        media_type=media_type,
    )


def test_the_buffer_follows_the_decoded_format() -> None:
    """A provider that decoded the source for us must not have its depth taken literally."""
    from music_assistant.controllers.streams.audio_buffer import (  # noqa: PLC0415
        _buffer_pcm_format,
    )

    # advertised as 16-bit lossy, delivered as 32-bit PCM: taking the advert
    # literally would truncate real audio to 16 bits
    advertised = AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=160,
    )
    arriving = AudioFormat(
        content_type=ContentType.PCM_S32LE,
        codec_type=ContentType.PCM_S32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=2,
    )
    pcm = _buffer_pcm_format(_streamdetails(advertised, arriving))
    assert pcm.bit_depth == 32
    assert pcm.content_type == ContentType.PCM_S32LE


def test_the_buffer_uses_the_source_when_nothing_was_decoded() -> None:
    """Without a separate handoff the advertised format describes the bytes too."""
    from music_assistant.controllers.streams.audio_buffer import (  # noqa: PLC0415
        _buffer_pcm_format,
    )

    advertised = AudioFormat(
        content_type=ContentType.FLAC,
        codec_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=24,
        channels=2,
    )
    pcm = _buffer_pcm_format(_streamdetails(advertised, None))
    assert pcm.bit_depth == 24
    assert pcm.sample_rate == 48000


def test_the_buffer_normalizes_ffmpegs_dsd_decoder_output() -> None:
    """DSD's byte-rate probe values must not be mistaken for its decoded PCM format."""
    from music_assistant.controllers.streams.audio_buffer import (  # noqa: PLC0415
        _buffer_pcm_format,
    )

    advertised = AudioFormat(
        content_type=ContentType.DSF,
        codec_type=ContentType.DSD_LSBF_PLANAR,
        sample_rate=352800,
        bit_depth=8,
        channels=2,
    )

    pcm = _buffer_pcm_format(_streamdetails(advertised, None))

    assert pcm.content_type == ContentType.PCM_F32LE
    assert pcm.codec_type == ContentType.PCM_F32LE
    assert pcm.sample_rate == 352800
    assert pcm.bit_depth == 32
    assert pcm.channels == 2


def test_the_buffer_recognizes_dst_compressed_dff_by_path() -> None:
    """DFF/DST needs path-based detection until the shared models expose those types."""
    from music_assistant.controllers.streams.audio_buffer import (  # noqa: PLC0415
        _buffer_pcm_format,
    )

    advertised = AudioFormat(
        content_type=ContentType.UNKNOWN,
        codec_type=ContentType.UNKNOWN,
        sample_rate=705600,
        bit_depth=16,
        channels=2,
    )
    streamdetails = _streamdetails(advertised, None)
    streamdetails.path = "/music/album/track.dff"

    pcm = _buffer_pcm_format(streamdetails)

    assert pcm.content_type == ContentType.PCM_F32LE
    assert pcm.codec_type == ContentType.PCM_F32LE
    assert pcm.sample_rate == 705600
    assert pcm.bit_depth == 32


def test_the_flow_depth_uses_ffmpegs_decoded_dsd_depth() -> None:
    """Unprocessed DSD-to-PCM playback carries FFmpeg's float output without narrowing it."""
    from music_assistant.controllers.streams.audio import StreamsAudio  # noqa: PLC0415

    advertised = AudioFormat(
        content_type=ContentType.DSF,
        codec_type=ContentType.DSD_LSBF_PLANAR,
        sample_rate=352800,
        bit_depth=8,
        channels=2,
    )
    streamdetails = _streamdetails(advertised, None)
    streamdetails.volume_normalization_mode = VolumeNormalizationMode.DISABLED

    content_type, bit_depth = StreamsAudio._pick_pcm_bit_depth(
        cast("StreamsAudio", SimpleNamespace()),
        players=(),
        streamdetails=streamdetails,
        crossfade_enabled=False,
    )

    assert content_type == ContentType.PCM_F32LE
    assert bit_depth == 32


def test_a_surround_source_is_folded_to_stereo() -> None:
    """The buffer holds the stereo fold, so analysis measures what is played."""
    from music_assistant.controllers.streams.audio_buffer import (  # noqa: PLC0415
        _buffer_pcm_format,
    )

    arriving = AudioFormat(
        content_type=ContentType.PCM_S32LE,
        codec_type=ContentType.PCM_S32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=6,
    )
    advertised = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=24)
    assert _buffer_pcm_format(_streamdetails(advertised, arriving)).channels == 2


def test_the_flow_depth_follows_the_arriving_audio() -> None:
    """
    The flow must not narrow a stream to a depth its audio never had.

    Reusing the source's native depth is a passthrough optimisation, so it has
    to read the depth the bytes arrive in - a provider that decoded for us may
    advertise something narrower purely for display.
    """
    from music_assistant.controllers.streams.audio import StreamsAudio  # noqa: PLC0415

    advertised = AudioFormat(
        content_type=ContentType.FLAC,
        codec_type=ContentType.FLAC,
        sample_rate=44100,
        bit_depth=24,
        channels=2,
    )
    arriving = AudioFormat(
        content_type=ContentType.PCM_S32LE,
        codec_type=ContentType.PCM_S32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=2,
    )
    streamdetails = _streamdetails(advertised, arriving)
    streamdetails.volume_normalization_mode = VolumeNormalizationMode.DISABLED

    content_type, bit_depth = StreamsAudio._pick_pcm_bit_depth(
        cast("StreamsAudio", SimpleNamespace()),
        players=(),
        streamdetails=streamdetails,
        crossfade_enabled=False,
    )

    assert bit_depth == 32
    assert content_type == ContentType.PCM_S32LE


def test_the_audio_source_passthrough_follows_the_arriving_audio() -> None:
    """
    A live source's passthrough format must not narrow the audio it passes through.

    The passthrough exists to hand the player the source's own samples, so it
    has to read the format the bytes arrive in - an engine that decoded for us
    (Spotify Connect) advertises the quality tier it asked for, which is
    narrower than the PCM it hands over.
    """
    from music_assistant.controllers.streams.audio import StreamsAudio  # noqa: PLC0415

    advertised = AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=160,
    )
    arriving = AudioFormat(
        content_type=ContentType.PCM_S32LE,
        codec_type=ContentType.PCM_S32LE,
        sample_rate=44100,
        bit_depth=32,
        channels=2,
    )

    pcm_format = StreamsAudio._select_audio_source_pcm_format(
        cast("StreamsAudio", SimpleNamespace()),
        player=cast("Player", SimpleNamespace()),
        streamdetails=_streamdetails(advertised, arriving, MediaType.AUDIO_SOURCE),
        supported_sample_rates=(44100, 48000),
    )

    assert pcm_format.bit_depth == 32
    assert pcm_format.content_type == ContentType.PCM_S32LE
    assert pcm_format.sample_rate == 44100
