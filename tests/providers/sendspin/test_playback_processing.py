"""Tests for Sendspin player-specific processing plans."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.audio_processing import (
    AudioDSPDetails,
    AudioOutputDetails,
)
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import ContentType, IdentifierType, SpeakerChannel
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_OUTPUT_CHANNELS
from music_assistant.controllers.streams.audio_processing import AudioOutputPlan
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

DEN = {IdentifierType.MAC_ADDRESS: "00:11:22:33:44:55", IdentifierType.IP_ADDRESS: "192.168.1.30"}


def _leader(**kwargs: Any) -> MagicMock:
    """Return a mocked leader whose provider holds no virtual or multi-channel players."""
    leader = MagicMock(protocol_id="leader", **kwargs)
    leader.provider.is_silent_client.return_value = False
    leader.provider.speaker_output.side_effect = lambda client_id: (client_id, None, False)
    return leader


def test_empty_plan_does_not_require_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan without filters keeps the member on the shared Sendspin channel."""
    player = _leader()
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
    player.mass.streams.audio.get_player_output_plan.assert_called_once_with(
        "member",
        pcm_format,
        pcm_format,
        handoff_format=pcm_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id=None,
        channel=None,
        with_subwoofer=False,
    )


def test_a_member_takes_the_dsp_settings_of_its_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DSP settings of a member are those of the player its protocol belongs to."""
    player = _leader()
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


def test_a_client_that_renders_nothing_takes_the_audio_as_it_is() -> None:
    """The silent client of a virtual or multi-channel player gets no processing of its own."""
    player = _leader()
    player.provider.is_silent_client.side_effect = lambda client_id: client_id == "leader"
    player.mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=True)
    session = SendspinPlaybackSession(player)

    config = session._read_pipeline_config("leader")

    assert not config.requires_transform
    player.mass.streams.audio.get_player_output_plan.assert_not_called()


def test_a_speaker_of_a_set_plays_with_the_settings_of_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker of a multi-channel player plays with the DSP of that player, on its channel."""
    player = _leader()
    player.provider.speaker_output.side_effect = lambda client_id: (
        ("sendspin_mc_set", SpeakerChannel.FRONT_LEFT, False)
        if client_id == "left"
        else (client_id, None, False)
    )
    player.mass.players.get_player_by_protocol.side_effect = lambda protocol_id: (
        MagicMock(player_id="pl_set") if protocol_id == "sendspin_mc_set" else None
    )
    player.mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=True)
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
    )
    player.mass.streams.audio.get_player_output_plan.side_effect = RuntimeError("no plan")
    session = SendspinPlaybackSession(player)
    session._pcm_format = pcm_format
    monkeypatch.setattr(session, "_get_member_output_format", lambda _player_id: pcm_format)

    config = session._read_pipeline_config("left")

    assert config.requires_transform
    player.mass.config.get_player_dsp_config.assert_called_once_with("pl_set")
    player.mass.streams.audio.get_player_output_plan.assert_called_once_with(
        "sendspin_mc_set",
        pcm_format,
        pcm_format,
        handoff_format=pcm_format,
        queue_id=None,
        session_id=None,
        queue_item_id=None,
        channel=SpeakerChannel.FRONT_LEFT,
        with_subwoofer=False,
    )


@pytest.mark.parametrize(
    ("channel", "reported"),
    [
        pytest.param(SpeakerChannel.FRONT_LEFT, True, id="a main speaker"),
        pytest.param(SpeakerChannel.LFE, False, id="the subwoofer"),
    ],
)
def test_the_main_speakers_of_a_set_report_its_output(
    monkeypatch: pytest.MonkeyPatch, channel: SpeakerChannel, reported: bool
) -> None:
    """
    The output of a multi-channel player is reported by its main speakers alone.

    A subwoofer plays along with them, so what the player shows does not depend on which of
    its speakers was planned last.
    """
    player = _leader()
    player.provider.speaker_output.side_effect = lambda _client_id: (
        "sendspin_mc_set",
        channel,
        True,
    )
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
    )
    plan = MagicMock(filter_params=[])
    plan.output_details.player_ids = ["pl_set"]
    player.mass.streams.audio.get_player_output_plan.return_value = plan
    session = SendspinPlaybackSession(player)
    session._pcm_format = pcm_format
    session._queue_id = "pl_set"
    session._queue_session_id = "session-1"
    monkeypatch.setattr(session, "_get_member_output_format", lambda _player_id: pcm_format)

    session._read_pipeline_config("speaker")

    # the plan is reported for the queue it is planned with
    get_plan = player.mass.streams.audio.get_player_output_plan
    assert get_plan.call_args.kwargs["queue_id"] == ("pl_set" if reported else None)


async def test_a_bridged_member_follows_the_output_channels_of_the_output_it_rides_on(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bridged member gets its own processing for the channel set on the output it rides on."""
    airplay = await scenario.add_provider("airplay")
    sendspin = await scenario.add_provider("sendspin")
    await scenario.register(airplay.protocol("ap_den", "Den", role="audio", identifiers=DEN))
    await scenario.register(
        sendspin.protocol("spb_den", "Den", role="audio", underlying_protocol_id="ap_den")
    )
    await scenario.idle()
    scenario.mass.config.set_protocol_value("ap_den", CONF_OUTPUT_CHANNELS, "left")
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
    )
    session = SendspinPlaybackSession(_leader(mass=scenario.mass))
    session._pcm_format = pcm_format
    monkeypatch.setattr(session, "_get_member_output_format", lambda _player_id: pcm_format)

    config = session._read_pipeline_config("spb_den")

    plan = scenario.mass.streams.audio.get_player_output_plan(
        "spb_den", pcm_format, pcm_format, handoff_format=pcm_format
    )
    assert "pan=stereo|c0=FL|c1=FL" in plan.filter_params
    assert config.requires_transform
    assert config.filter_params == tuple(plan.filter_params)
