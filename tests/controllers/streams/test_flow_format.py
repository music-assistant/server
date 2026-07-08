"""Tests for the flow mode sample-rate selection and restart logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_FLOW_MODE_SAMPLE_RATE,
    FLOW_MODE_SAMPLE_RATE_48000,
    FLOW_MODE_SAMPLE_RATE_96000,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_HIGHEST,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.controllers.streams.audio import (
    StreamsAudio,
    _snap_supported_rate_down,
    _snap_supported_rate_up,
)

# --- snap helpers ---


@pytest.mark.parametrize(
    ("target", "supported", "expected"),
    [
        (48000, [44100, 48000, 96000], 48000),  # exact match
        (50000, [44100, 48000, 96000], 96000),  # snap up to next higher
        (200000, [44100, 48000, 96000], 96000),  # no higher; fall back to max
        (40000, [44100, 48000], 44100),  # snap up to lowest higher
    ],
)
def test_snap_supported_rate_up(target: int, supported: list[int], expected: int) -> None:
    """_snap_supported_rate_up picks the lowest supported >= target, else max."""
    assert _snap_supported_rate_up(target, supported) == expected


@pytest.mark.parametrize(
    ("target", "supported", "expected"),
    [
        (48000, [44100, 48000, 96000], 48000),  # exact match
        (60000, [44100, 48000, 96000], 48000),  # snap down to highest lower
        (40000, [48000, 96000], 48000),  # no lower; fall back to min
        (200000, [44100, 48000], 48000),  # snap down (well above all)
    ],
)
def test_snap_supported_rate_down(target: int, supported: list[int], expected: int) -> None:
    """_snap_supported_rate_down picks the highest supported <= target, else min."""
    assert _snap_supported_rate_down(target, supported) == expected


# --- select_flow_pcm_format ---


@pytest.mark.asyncio
async def test_select_flow_pcm_format_highest() -> None:
    """'highest' mode picks the player's max supported sample rate."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_HIGHEST,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.sample_rate == 96000


@pytest.mark.asyncio
async def test_select_flow_pcm_format_48000_exact_match() -> None:
    """'48000' mode picks 48 kHz when supported."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_48000,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.sample_rate == 48000


@pytest.mark.asyncio
async def test_select_flow_pcm_format_48000_snaps_down_when_unsupported() -> None:
    """'48000' mode falls back to 44.1 kHz when the player can't do 48 kHz."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_48000,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.sample_rate == 44100


@pytest.mark.asyncio
async def test_select_flow_pcm_format_96000_falls_back_to_highest_below() -> None:
    """'96000' mode snaps down to the highest supported rate <= 96000."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_96000,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.sample_rate == 48000


@pytest.mark.asyncio
async def test_select_flow_pcm_format_96000_snaps_up_when_only_higher_supported() -> None:
    """'96000' mode falls back to the lowest supported rate above 96 kHz when no lower exists."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(192000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_96000,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.sample_rate == 192000


@pytest.mark.asyncio
async def test_select_flow_pcm_format_smart_anchors_on_start_track() -> None:
    """'smart' mode anchors on the first track's sample rate when supported."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(sample_rate=44100, bit_depth=16)
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=streamdetails)
    assert fmt.sample_rate == 44100


@pytest.mark.asyncio
async def test_select_flow_pcm_format_smart_snaps_up_unsupported_rate() -> None:
    """When the anchor rate isn't supported, smart snaps up to the next higher rate."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(48000, 24), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(sample_rate=44100, bit_depth=16)
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=streamdetails)
    assert fmt.sample_rate == 48000


@pytest.mark.asyncio
async def test_select_flow_pcm_format_smart_without_start_format_uses_max() -> None:
    """Smart mode with no start_streamdetails falls back to the player's highest rate."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.sample_rate == 96000


@pytest.mark.asyncio
async def test_select_flow_pcm_format_bit_perfect_matches_track() -> None:
    """'bit_perfect' mode also anchors on the first track's sample rate."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    )
    streamdetails = _make_streamdetails(sample_rate=96000, bit_depth=24)
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=streamdetails)
    assert fmt.sample_rate == 96000


# --- bit-depth optimization ---


@pytest.mark.asyncio
async def test_select_flow_pcm_format_uses_source_bit_depth_without_processing() -> None:
    """When no processing is active, the source's bit depth is reused (no F32 upcast)."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(sample_rate=44100, bit_depth=16)
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=streamdetails)
    assert fmt.bit_depth == 16


@pytest.mark.asyncio
async def test_select_flow_pcm_format_uses_f32_when_crossfade_enabled() -> None:
    """Smartfades requires F32 headroom; bit depth must be 32 regardless of source."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(sample_rate=44100, bit_depth=16)
    fmt = await audio.select_flow_pcm_format(
        player, start_streamdetails=streamdetails, crossfade_enabled=True
    )
    assert fmt.bit_depth == 32


@pytest.mark.asyncio
async def test_select_flow_pcm_format_uses_f32_when_overlay_active() -> None:
    """An active audio overlay requires F32 headroom for clipping-free mixing."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(sample_rate=44100, bit_depth=16)
    fmt = await audio.select_flow_pcm_format(
        player, start_streamdetails=streamdetails, overlay_active=True
    )
    assert fmt.bit_depth == 32


@pytest.mark.asyncio
async def test_select_pcm_format_uses_stereo_f32_for_radio_overlay() -> None:
    """A mixed radio overlay gets float headroom and a stereo pivot."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(sample_rate=44100, bit_depth=16)
    streamdetails.media_type = MediaType.RADIO
    streamdetails.audio_format.channels = 1

    fmt = await audio.select_pcm_format(
        player,
        streamdetails,
        crossfade_enabled=False,
        overlay_active=True,
    )

    assert fmt.content_type == ContentType.PCM_F32LE
    assert fmt.bit_depth == 32
    assert fmt.channels == 2


@pytest.mark.asyncio
async def test_select_flow_pcm_format_uses_f32_when_volume_normalization_active() -> None:
    """Active volume normalization on the start track triggers F32 headroom."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(
        sample_rate=44100,
        bit_depth=16,
        volume_normalization_mode=VolumeNormalizationMode.MEASUREMENT_ONLY,
    )
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=streamdetails)
    assert fmt.bit_depth == 32


@pytest.mark.asyncio
async def test_select_flow_pcm_format_uses_f32_when_no_start_streamdetails() -> None:
    """Without streamdetails we conservatively fall back to F32."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    fmt = await audio.select_flow_pcm_format(player)
    assert fmt.bit_depth == 32


# --- select_pcm_format (AUDIO_SOURCE passthrough) ---


@pytest.mark.asyncio
async def test_select_pcm_format_audio_source_passthrough_when_supported() -> None:
    """AUDIO_SOURCE keeps source rate/bit depth/channels even with smartfades requested."""
    audio = _make_streams_audio()
    player = _make_player(supported=[(44100, 16), (48000, 24), (96000, 24)])
    streamdetails = _make_streamdetails(
        sample_rate=48000, bit_depth=24, channels=1, media_type=MediaType.AUDIO_SOURCE
    )
    fmt = await audio.select_pcm_format(player, streamdetails, crossfade_enabled=True)
    assert fmt.sample_rate == 48000
    assert fmt.bit_depth == 24
    assert fmt.channels == 1


@pytest.mark.asyncio
async def test_select_pcm_format_audio_source_snaps_down_unsupported_rate() -> None:
    """AUDIO_SOURCE snaps the sample rate down when the player can't accept the source rate."""
    audio = _make_streams_audio()
    player = _make_player(supported=[(44100, 16), (48000, 24)])
    streamdetails = _make_streamdetails(
        sample_rate=88200, bit_depth=24, media_type=MediaType.AUDIO_SOURCE
    )
    fmt = await audio.select_pcm_format(player, streamdetails, crossfade_enabled=False)
    assert fmt.sample_rate == 48000
    assert fmt.bit_depth == 24


@pytest.mark.asyncio
async def test_select_pcm_format_audio_source_falls_back_to_min_supported() -> None:
    """When the source rate is below every supported rate, fall back to the player's lowest."""
    audio = _make_streams_audio()
    player = _make_player(supported=[(44100, 16), (48000, 24)])
    streamdetails = _make_streamdetails(
        sample_rate=22050, bit_depth=16, media_type=MediaType.AUDIO_SOURCE
    )
    fmt = await audio.select_pcm_format(player, streamdetails, crossfade_enabled=False)
    assert fmt.sample_rate == 44100


# --- select_flow_pcm_format (AUDIO_SOURCE passthrough) ---


@pytest.mark.asyncio
async def test_select_flow_pcm_format_audio_source_bypasses_flow_mode() -> None:
    """AUDIO_SOURCE as start item ignores flow mode config and passes the source through."""
    audio = _make_streams_audio()
    # 'highest' would normally pick 96000 — passthrough must override that.
    player = _make_player(
        supported=[(44100, 16), (48000, 24), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_HIGHEST,
    )
    streamdetails = _make_streamdetails(
        sample_rate=44100, bit_depth=16, media_type=MediaType.AUDIO_SOURCE
    )
    fmt = await audio.select_flow_pcm_format(
        player, start_streamdetails=streamdetails, crossfade_enabled=True
    )
    assert fmt.sample_rate == 44100
    assert fmt.bit_depth == 16
    assert fmt.channels == 2


@pytest.mark.asyncio
async def test_select_flow_pcm_format_audio_source_preserves_mono() -> None:
    """AUDIO_SOURCE keeps source channels — no forced stereo widening."""
    audio = _make_streams_audio()
    player = _make_player(
        supported=[(44100, 16), (48000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_SMART,
    )
    streamdetails = _make_streamdetails(
        sample_rate=44100, bit_depth=16, channels=1, media_type=MediaType.AUDIO_SOURCE
    )
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=streamdetails)
    assert fmt.channels == 1


# --- _flow_stream_needs_restart ---


def test_needs_restart_first_track_never_breaks() -> None:
    """The first (anchor) track is always allowed to continue."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=192000)
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000, 96000, 192000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
            is_first_track=True,
        )
        is False
    )


def test_needs_restart_radio_always_breaks() -> None:
    """Radio items always break out of flow, even on the first track."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=44100, media_type=MediaType.RADIO)
    pcm = AudioFormat(sample_rate=44100, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_SMART,
            is_first_track=True,
        )
        is True
    )


def test_needs_restart_audio_source_breaks() -> None:
    """Live AUDIO_SOURCE items break out so the controller can use single-item streaming."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=44100, media_type=MediaType.AUDIO_SOURCE)
    pcm = AudioFormat(sample_rate=44100, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_SMART,
            is_first_track=False,
        )
        is True
    )


def test_needs_restart_smart_breaks_when_rate_increases() -> None:
    """Smart mode breaks when the next track's effective rate is higher than the flow's."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=96000)
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000, 96000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_SMART,
            is_first_track=False,
        )
        is True
    )


def test_needs_restart_smart_no_break_on_lower_rate() -> None:
    """Smart mode does not break when the next track's rate is equal or lower."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=44100)
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000, 96000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_SMART,
            is_first_track=False,
        )
        is False
    )


def test_needs_restart_bit_perfect_breaks_on_any_rate_change() -> None:
    """Bit-perfect mode breaks on any rate change (including downsampling)."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=44100)
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000, 96000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
            is_first_track=False,
        )
        is True
    )


def test_needs_restart_snaps_up_unsupported_next_rate_before_compare() -> None:
    """An unsupported raw rate is snapped up before being compared to the flow rate."""
    # raw 44100 isn't supported; the player snaps it up to 48000 — which equals the
    # current flow rate, so no restart is needed even under bit-perfect.
    audio = _make_streams_audio()
    track = _make_queue_track(rate=44100)
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[48000, 96000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
            is_first_track=False,
        )
        is False
    )


def test_needs_restart_fixed_rate_modes_never_break_on_rate() -> None:
    """The fixed-rate modes always resample to the flow rate, so no restart."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=96000)
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    for mode in (
        FLOW_MODE_SAMPLE_RATE_48000,
        FLOW_MODE_SAMPLE_RATE_96000,
        FLOW_MODE_SAMPLE_RATE_HIGHEST,
    ):
        assert (
            audio._flow_stream_needs_restart(
                track,
                pcm,
                supported_sample_rates=[44100, 48000, 96000, 192000],
                flow_mode_sample_rate_conf=mode,
                is_first_track=False,
            )
            is False
        )


def test_needs_restart_returns_false_when_streamdetails_missing() -> None:
    """Without streamdetails the sample rate can't be evaluated; do not break the flow."""
    audio = _make_streams_audio()
    track = _make_queue_track(rate=44100)
    track.streamdetails = None
    pcm = AudioFormat(sample_rate=48000, bit_depth=32, channels=2)
    assert (
        audio._flow_stream_needs_restart(
            track,
            pcm,
            supported_sample_rates=[44100, 48000],
            flow_mode_sample_rate_conf=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
            is_first_track=False,
        )
        is False
    )


# --- _flow_restart_context ---


def test_flow_restart_context_prefers_flow_player() -> None:
    """The given (protocol) player's config and rates win over the queue player's."""
    audio = _make_streams_audio()
    protocol_player = _make_player(
        supported=[(44100, 16), (96000, 24)],
        flow_mode=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    )
    # queue (wrapper) player without audio config entries: 44100-only fallback
    wrapper_player = _make_player(supported=[(44100, 16)])
    audio.mass.players.get_player = MagicMock(  # type: ignore[method-assign]
        return_value=wrapper_player
    )

    conf, rates = audio._flow_restart_context("queue-1", protocol_player)

    assert conf == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    assert rates == [44100, 96000]
    audio.mass.players.get_player.assert_not_called()


def test_flow_restart_context_falls_back_to_queue_player() -> None:
    """Without a flow player, the queue's own player is used."""
    audio = _make_streams_audio()
    queue_player = _make_player(
        supported=[(48000, 16)], flow_mode=FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    )
    audio.mass.players.get_player = MagicMock(  # type: ignore[method-assign]
        return_value=queue_player
    )

    conf, rates = audio._flow_restart_context("queue-1", None)

    assert conf == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT
    assert rates == [48000]
    audio.mass.players.get_player.assert_called_once_with("queue-1")


def test_flow_restart_context_without_any_player() -> None:
    """When no player can be resolved, the raw queue config and empty rates are used."""
    audio = _make_streams_audio()
    audio.mass.players.get_player = MagicMock(return_value=None)  # type: ignore[method-assign]
    audio.mass.config.get_raw_player_config_value = MagicMock(  # type: ignore[method-assign]
        return_value=FLOW_MODE_SAMPLE_RATE_SMART
    )

    conf, rates = audio._flow_restart_context("queue-1", None)

    assert conf == FLOW_MODE_SAMPLE_RATE_SMART
    assert rates == []


# --- helpers ---


def _make_streams_audio() -> StreamsAudio:
    """Return a StreamsAudio with everything mocked out except the logger."""
    audio = StreamsAudio(MagicMock())
    # DSP must be disabled in tests so the bit-depth optimization can engage;
    # individual tests opt back in via smartfades or volume normalization.
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=False)
    )
    return audio


def _make_player(
    *,
    supported: list[tuple[int, int]],
    flow_mode: str = FLOW_MODE_SAMPLE_RATE_SMART,
) -> MagicMock:
    """Build a player double exposing only what select_flow_pcm_format needs."""
    player = MagicMock()
    player.get_supported_sample_rates = MagicMock(return_value=supported)
    player.config.get_value = MagicMock(
        side_effect=lambda key, default=None: (
            flow_mode if key == CONF_FLOW_MODE_SAMPLE_RATE else default
        )
    )
    return player


def _make_streamdetails(
    *,
    sample_rate: int,
    bit_depth: int,
    channels: int = 2,
    volume_normalization_mode: VolumeNormalizationMode = VolumeNormalizationMode.DISABLED,
    media_type: MediaType = MediaType.TRACK,
) -> MagicMock:
    """Build a StreamDetails double carrying just the fields the format selector reads."""
    streamdetails = MagicMock()
    streamdetails.audio_format = AudioFormat(
        sample_rate=sample_rate, bit_depth=bit_depth, channels=channels
    )
    streamdetails.volume_normalization_mode = volume_normalization_mode
    streamdetails.media_type = media_type
    return streamdetails


def _make_queue_track(*, rate: int, media_type: MediaType = MediaType.TRACK) -> MagicMock:
    """Build a queue_item double with the minimum fields the helper inspects."""
    track = MagicMock()
    track.queue_item_id = "item-1"
    track.name = "Test Track"
    track.media_type = media_type
    track.streamdetails = MagicMock()
    track.streamdetails.audio_format = AudioFormat(sample_rate=rate, bit_depth=16, channels=2)
    return track
