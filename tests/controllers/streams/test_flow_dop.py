"""Tests for DSD over PCM (DoP) handling in the flow stream format and restart logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_DSD_DOP,
    CONF_FLOW_MODE_SAMPLE_RATE,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.controllers.streams.audio import StreamsAudio

DOP_RATES = [(44100, 24), (48000, 24), (96000, 24), (176400, 24), (192000, 24), (352800, 24)]


def _make_streams_audio(*, dop: bool) -> StreamsAudio:
    """Return a StreamsAudio whose players have DoP switched on or off."""
    audio = StreamsAudio(MagicMock())
    audio.mass.config.get_player_dsp_config = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(enabled=False)
    )
    audio.mass.config.get_raw_player_config_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _player_id, key, default=None: dop if key == CONF_DSD_DOP else default
    )
    return audio


def _make_player(*, supported: list[tuple[int, int]] = DOP_RATES, members: int = 1) -> MagicMock:
    player = MagicMock()
    player.player_id = "player"
    player.group_members = [f"member-{index}" for index in range(members)]
    player.get_supported_sample_rates = MagicMock(return_value=supported)
    player.config.get_value = MagicMock(
        side_effect=lambda key, default=None: (
            FLOW_MODE_SAMPLE_RATE_SMART if key == CONF_FLOW_MODE_SAMPLE_RATE else default
        )
    )
    return player


def _dsd_streamdetails(probed_rate: int = 352800) -> MagicMock:
    """Stream details of a local DSF file, as the library reports it (DSD rate / 8)."""
    streamdetails = MagicMock()
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.DSF, sample_rate=probed_rate, bit_depth=8, channels=2
    )
    streamdetails.stream_type = StreamType.LOCAL_FILE
    streamdetails.path = "/music/track.dsf"
    streamdetails.decoded_audio_format = None
    streamdetails.media_type = MediaType.TRACK
    return streamdetails


def _queue_track(streamdetails: MagicMock) -> MagicMock:
    track = MagicMock()
    track.queue_item_id = "item-2"
    track.name = "DSD Track"
    track.media_type = MediaType.TRACK
    track.streamdetails = streamdetails
    return track


def _pcm_format(rate: int) -> AudioFormat:
    return AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=rate, bit_depth=32, channels=2
    )


@pytest.mark.asyncio
async def test_flow_format_runs_at_dop_rate_for_dsd_start() -> None:
    """A DSD64 first track anchors the flow at its DoP rate (176.4 kHz), in F32."""
    audio = _make_streams_audio(dop=True)
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 176400
    assert fmt.content_type == ContentType.PCM_F32LE


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_when_disabled() -> None:
    """Without the DoP setting a DSD track is decoded to PCM at the snapped source rate."""
    audio = _make_streams_audio(dop=False)
    fmt = await audio.select_flow_pcm_format(
        _make_player(), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 352800


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_for_unsupported_rate() -> None:
    """
    A player that does not take the DoP rate gets the regular PCM conversion.

    The regular conversion keeps the 44.1 kHz family of the source.
    """
    audio = _make_streams_audio(dop=True)
    player = _make_player(supported=[(44100, 24), (96000, 24), (192000, 24)])
    fmt = await audio.select_flow_pcm_format(player, start_streamdetails=_dsd_streamdetails())
    assert fmt.sample_rate == 44100


@pytest.mark.asyncio
async def test_flow_format_ignores_dop_for_groups() -> None:
    """A grouped player never gets DoP: the group would alter the shared stream."""
    audio = _make_streams_audio(dop=True)
    fmt = await audio.select_flow_pcm_format(
        _make_player(members=2), start_streamdetails=_dsd_streamdetails()
    )
    assert fmt.sample_rate == 352800


@pytest.mark.parametrize(
    ("flow_rate", "probed_rate", "expected"),
    [
        (44100, 352800, True),  # PCM flow -> DSD64 needs the 176.4 kHz DoP rate
        (176400, 352800, False),  # already at the DSD64 DoP rate
        (176400, 705600, True),  # DSD128 needs 352.8 kHz
    ],
)
def test_restart_on_dop_rate_change(flow_rate: int, probed_rate: int, expected: bool) -> None:
    """The flow restarts whenever a DSD track needs a different DoP rate."""
    audio = _make_streams_audio(dop=True)
    assert (
        audio._flow_stream_needs_restart(
            _queue_track(_dsd_streamdetails(probed_rate)),
            _pcm_format(flow_rate),
            [rate for rate, _ in DOP_RATES],
            FLOW_MODE_SAMPLE_RATE_SMART,
            is_first_track=False,
            dop_enabled=True,
        )
        is expected
    )


def test_no_dop_restart_when_disabled() -> None:
    """Without DoP the regular smart-mode rule applies (352.8 kHz > 176.4 kHz restarts)."""
    audio = _make_streams_audio(dop=False)
    assert audio._flow_stream_needs_restart(
        _queue_track(_dsd_streamdetails()),
        _pcm_format(176400),
        [rate for rate, _ in DOP_RATES],
        FLOW_MODE_SAMPLE_RATE_SMART,
        is_first_track=False,
        dop_enabled=False,
    )


def test_dop_transition_detects_dsd_before_resolving() -> None:
    """A DSD track is recognised from its provider mapping before streamdetails exist."""
    audio = _make_streams_audio(dop=True)
    pcm_item = MagicMock()
    pcm_item.queue_item_id = "item-1"
    pcm_item.streamdetails = None
    pcm_item.media_item = None
    from music_assistant_models.media_items import ProviderMapping, Track  # noqa: PLC0415

    dsd_item = MagicMock()
    dsd_item.streamdetails = None
    dsd_item.media_item = Track(
        item_id="1",
        provider="filesystem",
        name="DSD",
        provider_mappings={
            ProviderMapping(
                item_id="1",
                provider_domain="filesystem",
                provider_instance="filesystem",
                audio_format=AudioFormat(
                    content_type=ContentType.DSF, sample_rate=352800, bit_depth=8
                ),
            )
        },
    )
    with patch.object(audio.mass.player_queues, "get_next_item", return_value=dsd_item):
        assert audio._dop_transition("queue", pcm_item)
    with patch.object(audio.mass.player_queues, "get_next_item", return_value=None):
        assert not audio._dop_transition("queue", pcm_item)
