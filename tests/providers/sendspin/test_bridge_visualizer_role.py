"""Tests for the stream configuration of the in-process Sendspin visualizer bridge role."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aiosendspin.models.visualizer import BeatAvailability, VisualizerStatePayload

from music_assistant.providers.sendspin import bridge_role as bridge_role_module
from music_assistant.providers.sendspin.bridge_role import BridgeVisualizerRole


def _role(request: VisualizerStatePayload | None) -> BridgeVisualizerRole:
    role = BridgeVisualizerRole(client=MagicMock())
    if request is not None:
        role.setup_visualizer(request)
    return role


def test_stream_start_extracts_the_requested_types() -> None:
    """The extractor is configured from the bridge's requested stream configuration."""
    role = _role(VisualizerStatePayload(types=["beat", "loudness"], rate_max=20))

    with patch.object(bridge_role_module, "VisualizerFeatureExtractor") as extractor:
        role.on_stream_start()

    config = extractor.call_args.kwargs["config"]
    assert config.types == ("beat", "loudness")
    assert config.rate_max == 20
    assert config.spectrum is None


def test_stream_start_without_a_request_extracts_nothing() -> None:
    """A role that was never configured builds no extractor."""
    role = _role(None)

    with patch.object(bridge_role_module, "VisualizerFeatureExtractor") as extractor:
        role.on_stream_start()

    extractor.assert_not_called()


def test_beats_are_wanted_only_when_requested_and_available() -> None:
    """Beats go to the bridge when it asked for them and they are not known to be missing."""
    assert not _role(None).wants_beats
    assert not _role(VisualizerStatePayload(types=["peak"], rate_max=20)).wants_beats

    role = _role(VisualizerStatePayload(types=["beat"], rate_max=20))
    assert role.wants_beats
    role.set_beat_availability(BeatAvailability.UNAVAILABLE)
    assert not role.wants_beats
