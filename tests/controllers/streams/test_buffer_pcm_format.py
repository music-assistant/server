"""Test that PCM formats follow the arriving audio, not the advertised source."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails


def _streamdetails(advertised: AudioFormat, arriving: AudioFormat | None) -> StreamDetails:
    """Return StreamDetails advertising one format while delivering another."""
    return StreamDetails(
        provider="test--1",
        item_id="1",
        audio_format=advertised,
        decoded_audio_format=arriving,
        media_type=MediaType.TRACK,
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
