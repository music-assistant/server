"""Tests for Sendspin player-specific processing plans."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.audio_processing import (
    AudioDSPDetails,
    AudioOutputDetails,
)
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.audio_processing import AudioOutputPlan
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

if TYPE_CHECKING:
    import pytest


def test_empty_plan_does_not_require_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan without filters keeps the member on the shared Sendspin channel."""
    player = MagicMock(protocol_id="leader")
    player.mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    player.mass.config.get_protocol_value.return_value = "stereo"
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=48000,
        bit_depth=32,
        channels=2,
    )
    output_details = AudioOutputDetails(
        player_ids=["member"],
        dsp=AudioDSPDetails(),
        output_format=pcm_format,
    )
    output_plan = AudioOutputPlan(
        filter_params=[],
        output_details=output_details,
        input_format=pcm_format,
    )
    player.mass.streams.audio.get_player_output_plan.return_value = output_plan
    session = SendspinPlaybackSession(player)
    session._pcm_format = pcm_format
    session._queue_id = "queue-1"
    session._queue_session_id = "session-1"
    monkeypatch.setattr(session, "_get_member_output_format", lambda _player_id: pcm_format)

    config = session._read_pipeline_config("member")

    assert not config.requires_transform
    player.mass.streams.audio_processing.update_output.assert_called_once_with(
        "member",
        output_plan,
        queue_id="queue-1",
        session_id="session-1",
    )


def test_a_member_takes_the_dsp_settings_of_its_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DSP settings of a member are those of the player its protocol belongs to."""
    player = MagicMock(protocol_id="leader")
    player.mass.players.get_player_by_protocol.side_effect = lambda protocol_id: (
        MagicMock(player_id="pl_member") if protocol_id == "member" else None
    )
    player.mass.config.get_player_dsp_config.side_effect = lambda player_id: DSPConfig(
        enabled=player_id == "pl_member"
    )
    player.mass.config.get_protocol_value.return_value = "stereo"
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
    )
    player.mass.streams.audio.get_player_output_plan.side_effect = RuntimeError("no plan")
    session = SendspinPlaybackSession(player)
    monkeypatch.setattr(session, "_get_member_output_format", lambda _player_id: pcm_format)

    config = session._read_pipeline_config("member")

    assert config.requires_transform
    player.mass.config.get_player_dsp_config.assert_called_once_with("pl_member")
