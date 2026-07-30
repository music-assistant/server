"""Tests for Universal Group output formats."""

from music_assistant_models.audio_processing import (
    AudioFidelity,
    AudioOutputDetails,
    AudioQuality,
)
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio_processing import get_audio_quality
from music_assistant.helpers.ffmpeg import get_ffmpeg_args
from music_assistant.providers.universal_group.constants import (
    UGP_OUTPUT_MP3,
    resolve_ugp_output_format,
)


def test_resolve_ugp_mp3_output_format() -> None:
    """Resolve the complete objective format and effective quality for UGP MP3 output."""
    audio_format, extension = resolve_ugp_output_format(UGP_OUTPUT_MP3)

    assert extension == "mp3"
    assert audio_format.content_type == ContentType.MP3
    assert audio_format.codec_type == ContentType.MP3
    assert audio_format.sample_rate == 44100
    assert audio_format.bit_depth == 16
    assert audio_format.bit_rate == 320
    assert get_audio_quality(audio_format) == AudioQuality.STANDARD


def test_ugp_mp3_output_details_serialization() -> None:
    """Serialize the UGP MP3 codec, bitrate and effective quality for clients."""
    audio_format, _ = resolve_ugp_output_format(UGP_OUTPUT_MP3)
    output_details = AudioOutputDetails(
        output_format=audio_format,
        fidelity=AudioFidelity(quality=get_audio_quality(audio_format)),
    )

    payload = output_details.to_dict()

    assert payload["output_format"]["content_type"] == ContentType.MP3.value
    assert payload["output_format"]["codec_type"] == ContentType.MP3.value
    assert payload["output_format"]["bit_rate"] == 320
    assert payload["fidelity"]["quality"] == AudioQuality.STANDARD.value


def test_ugp_mp3_bitrate_matches_ffmpeg_encoder() -> None:
    """Keep UGP MP3 bitrate metadata aligned with the FFmpeg encoder."""
    output_format, _ = resolve_ugp_output_format(UGP_OUTPUT_MP3)
    input_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=output_format.sample_rate,
        bit_depth=32,
        channels=2,
    )

    ffmpeg_args = get_ffmpeg_args(input_format, output_format, [])
    bitrate_index = ffmpeg_args.index("-b:a")

    assert ffmpeg_args[bitrate_index + 1] == f"{output_format.bit_rate}k"
