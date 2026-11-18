"""Channel defaults through the real MA models and non-flow audio helpers."""

from types import SimpleNamespace
from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.helpers.audio import calculate_content_length
from music_assistant.helpers.ffmpeg import get_ffmpeg_channel_args
from music_assistant.providers.feiniu_music.parsers import parse_track

from .test_provider import provider as provider  # noqa: PLC0414
from .test_provider import track_data


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ({}, 2),
        ({"channel": None}, 2),
        ({"channel": 0}, 2),
        ({"channel": -1}, 2),
        ({"channel": "unknown"}, 2),
        ({"channel": "1"}, 2),
        ({"channel": True}, 2),
        ({"channel": 1.5}, 2),
        ({"channel": []}, 2),
        ({"channel": 1}, 1),
        ({"channel": 2}, 2),
        ({"channel": 6}, 6),
    ],
)
async def test_channels_are_safe_for_ma_playback(
    provider: Any, field: dict[str, Any], expected: int
) -> None:
    """Invalid channels use MA's default without inventing rate/depth or probing audio."""
    row = track_data()
    row["audioSpec"] = {"format": "mp3", "bitrate": 320000, **field}
    mapping = next(iter(parse_track(row, "synthetic").provider_mappings))
    fmt = mapping.audio_format
    assert fmt.channels == expected
    assert AudioFormat.__dataclass_fields__["channels"].default == 2
    assert (fmt.sample_rate, fmt.bit_depth) == (0, 0)
    assert fmt.quality >= 0
    assert mapping.quality >= 0
    other = next(iter(parse_track(track_data(), "other").provider_mappings))
    ranked = sorted([mapping, other], key=lambda item: item.quality, reverse=True)
    assert len(ranked) == 2
    assert ranked[0].quality >= ranked[1].quality

    provider._client.detail.return_value = {"track": row}
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    assert details.audio_format.channels == expected
    provider._client.detail.assert_awaited_once_with("track", "track-test")
    provider._client.page.assert_not_awaited()

    audio: Any = object.__new__(StreamsAudio)
    player: Any = SimpleNamespace(get_supported_sample_rates=lambda: [(44100, 16)])
    # No player DSP is configured; the PCM selection and its bit-depth helper are real.
    audio._resolve_player_dsp_config = lambda _player: SimpleNamespace(enabled=False)
    pcm = await audio.select_pcm_format(player, details, crossfade_enabled=False)
    assert pcm.channels == min(expected, 2)
    assert get_ffmpeg_channel_args(pcm)[:2] == ["-ac", str(min(expected, 2))]
    assert pcm.pcm_sample_size > 0
    assert calculate_content_length(pcm) > 0
