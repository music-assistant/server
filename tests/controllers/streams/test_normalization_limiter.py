"""Tests for the limiter after a positive volume normalization gain in get_queue_item_stream."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerType,
    VolumeNormalizationMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.streams.audio import StreamsAudio

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE,
    codec_type=ContentType.PCM_F32LE,
    sample_rate=44100,
    bit_depth=32,
    channels=2,
)
LIMITER = "alimiter=limit=-2.0dB:level=false:asc=true:latency=true"
TARGET_LOUDNESS = -14.0
# a quiet track: -14 - (-20) = +6 dB of gain
QUIET_LOUDNESS = -20.0
QUEUE_ID = "player_a"


class _Harness:
    """StreamsAudio with buffer/config mocked out and the captured filter chain."""

    def __init__(self, controller: StreamsAudio) -> None:
        self.controller = controller
        self.mass = cast("MagicMock", controller.mass)
        self.mode = VolumeNormalizationMode.MEASUREMENT_ONLY
        self.filter_params: list[str] = []
        self.players: dict[str, SimpleNamespace] = {}
        self.dsp_player_ids: set[str] = set()
        self.mass.players.get_player.side_effect = self.players.get
        self.mass.config.get_player_dsp_config.side_effect = lambda player_id: DSPConfig(
            enabled=player_id in self.dsp_player_ids
        )

    def add_player(
        self,
        player_id: str,
        group_members: tuple[str, ...] = (),
        multi_device_dsp: bool = False,
        dsp: bool = False,
        player_type: PlayerType = PlayerType.PLAYER,
    ) -> None:
        """Register a player, optionally with DSP enabled in its config."""
        self.players[player_id] = SimpleNamespace(
            player_id=player_id,
            protocol_parent_id=None,
            provider=SimpleNamespace(domain="test"),
            state=SimpleNamespace(
                group_members=list(group_members),
                supported_features={PlayerFeature.MULTI_DEVICE_DSP} if multi_device_dsp else set(),
                type=player_type,
            ),
        )
        if dsp:
            self.dsp_player_ids.add(player_id)

    async def stream_filters(
        self, loudness: float | None = QUIET_LOUDNESS, playback_speed: float = 1.0
    ) -> list[str]:
        """Stream a queue item and return the filter chain applied to its buffer."""
        streamdetails = StreamDetails(
            provider="test_provider",
            item_id="track_a",
            audio_format=PCM_FORMAT,
            media_type=MediaType.TRACK,
        )
        streamdetails.queue_id = QUEUE_ID
        streamdetails.target_loudness = TARGET_LOUDNESS
        streamdetails.loudness = loudness
        queue_item = MagicMock()
        queue_item.media_type = MediaType.TRACK
        queue_item.streamdetails = streamdetails
        queue_item.name = "Track A"
        async for _ in self.controller.get_queue_item_stream(
            queue_item, PCM_FORMAT, playback_speed=playback_speed
        ):
            pass
        return self.filter_params


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Build the test harness around a StreamsAudio with a mocked mass."""
    controller = StreamsAudio(MagicMock())
    test_harness = _Harness(controller)

    class _FakeBuffer:
        """AudioBuffer test double: records the filter_params, yields no audio."""

        has_error = False
        pcm_format = PCM_FORMAT

        @classmethod
        async def get_buffer(cls, **_kwargs: Any) -> _FakeBuffer:
            return cls()

        async def get_stream(
            self,
            output_format: AudioFormat,
            seek_position_ms: int = 0,
            filter_params: list[str] | None = None,
            exact_seek: bool = False,
        ) -> AsyncGenerator[bytes]:
            del output_format, seek_position_ms, exact_seek
            test_harness.filter_params = filter_params or []
            empty: tuple[bytes, ...] = ()
            for chunk in empty:
                yield chunk

    monkeypatch.setattr(audio_mod, "AudioBuffer", _FakeBuffer)
    monkeypatch.setattr(audio_mod, "get_normalization_mode", lambda *_a, **_k: test_harness.mode)
    test_harness.mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)
    test_harness.mass.config.get_player_config = AsyncMock(return_value=MagicMock())
    test_harness.mass.player_queues.queue_data_or_none.return_value = None
    return test_harness


@pytest.mark.asyncio
async def test_positive_measurement_gain_is_followed_by_limiter(harness: _Harness) -> None:
    """A positive measured gain gets the limiter right after it, ahead of the speed change."""
    harness.add_player(QUEUE_ID)

    filters = await harness.stream_filters(playback_speed=1.5)

    assert filters == ["volume=6.0dB", LIMITER, "atempo=1.5"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loudness",
    [
        pytest.param(-8.0, id="negative gain"),
        pytest.param(TARGET_LOUDNESS, id="zero gain"),
    ],
)
async def test_non_positive_measurement_gain_has_no_limiter(
    harness: _Harness, loudness: float
) -> None:
    """A gain that only lowers or keeps the level needs no limiter."""
    harness.add_player(QUEUE_ID)

    filters = await harness.stream_filters(loudness=loudness)

    assert LIMITER not in filters
    assert any(f.startswith("volume=") for f in filters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixed_gain", "expected"),
    [
        pytest.param(3.0, ["volume=3.0dB", LIMITER], id="positive gain"),
        pytest.param(-6.0, ["volume=-6.0dB"], id="default gain"),
    ],
)
async def test_fixed_gain_limiter(
    harness: _Harness, fixed_gain: float, expected: list[str]
) -> None:
    """A fixed gain is only limited when it raises the level."""
    harness.mode = VolumeNormalizationMode.FIXED_GAIN
    harness.mass.streams.get_config_value.return_value = fixed_gain
    harness.add_player(QUEUE_ID)

    assert await harness.stream_filters() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("players", "expect_limiter"),
    [
        pytest.param([], True, id="unknown queue player"),
        pytest.param([(QUEUE_ID, (), False, True)], False, id="dsp on single player"),
        pytest.param(
            [(QUEUE_ID, (QUEUE_ID, "player_b"), True, True), ("player_b", (), False, False)],
            True,
            id="dsp on leader, member without dsp",
        ),
        pytest.param(
            [(QUEUE_ID, (QUEUE_ID, "player_b"), True, True), ("player_b", (), False, True)],
            False,
            id="dsp on leader and all members",
        ),
        pytest.param(
            [(QUEUE_ID, (QUEUE_ID, "player_b"), False, True), ("player_b", (), False, True)],
            True,
            id="grouping prevents dsp",
        ),
        pytest.param(
            [
                (QUEUE_ID, ("player_b", "player_c"), True, False, PlayerType.GROUP),
                ("player_b", (), False, True),
                ("player_c", (), False, True),
            ],
            False,
            id="group player, dsp on all members",
        ),
        pytest.param(
            [
                (QUEUE_ID, ("player_b", "player_c"), True, False, PlayerType.GROUP),
                ("player_b", (), False, True),
                ("player_c", (), False, False),
            ],
            True,
            id="group player, member without dsp",
        ),
    ],
)
async def test_player_dsp_decides_limiter(
    harness: _Harness,
    players: list[tuple[Any, ...]],
    expect_limiter: bool,
) -> None:
    """The limiter is left to the players only when DSP runs on every output player."""
    for player in players:
        harness.add_player(*player)

    filters = await harness.stream_filters()

    assert (LIMITER in filters) is expect_limiter


@pytest.mark.asyncio
async def test_dynamic_normalization_has_no_limiter(harness: _Harness) -> None:
    """Dynamic normalization limits its own true peak, so it gets loudnorm only."""
    harness.mode = VolumeNormalizationMode.DYNAMIC
    harness.add_player(QUEUE_ID)

    filters = await harness.stream_filters()

    assert len(filters) == 1
    assert filters[0].startswith("loudnorm=")
