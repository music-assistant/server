"""Tests for Sendspin player-specific processing plans."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.audio_processing import (
    AudioLimiterDetails,
    AudioOutputPath,
)
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio_processing import AudioOutputPlan
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

if TYPE_CHECKING:
    import pytest


def test_limiter_is_not_reported_when_shared_channel_skips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limiter-only plan reflects that the shared Sendspin channel bypasses it."""
    player = MagicMock(player_id="leader")
    player.mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    player.mass.config.get_raw_player_config_value.return_value = "stereo"
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=48000,
        bit_depth=32,
        channels=2,
    )
    output_path = AudioOutputPath(
        player_ids=["member"],
        input_format=pcm_format,
        limiter=AudioLimiterDetails(enabled=True, threshold_dbfs=-2.0),
        output_format=pcm_format,
    )
    player.mass.streams.audio.get_player_output_plan.return_value = AudioOutputPlan(
        filter_params=["alimiter=limit=-2dB:level=false:asc=true"],
        output_path=output_path,
    )
    session = SendspinPlaybackSession(player)
    session._pcm_format = pcm_format
    session._queue_id = "queue-1"
    session._queue_session_id = "session-1"
    monkeypatch.setattr(session, "_get_member_output_format", lambda _player_id: pcm_format)

    config = session._read_pipeline_config("member")

    assert not config.requires_transform
    assert not output_path.limiter.enabled
    assert output_path.limiter.threshold_dbfs is None
    player.mass.streams.audio_processing.update_output.assert_called_once_with(
        "member",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
    )
