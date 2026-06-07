"""Tests for the SynchronizerRole."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from music_assistant.providers.sendspin.synchronizer_role import (
    SYNC_ROLE_ID,
    SynchronizerRole,
)


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a mock SendspinClient."""
    client = MagicMock()
    client.client_id = "test-client-123"
    client.info.visualizer_support = MagicMock()
    client.info.visualizer_support.to_dict.return_value = {
        "buffer_capacity": 4096,
        "types": ["loudness", "f_peak"],
        "batch_max": 1,
    }
    client.group.group_role.return_value = MagicMock()
    return client


class TestSynchronizerRoleProperties:
    """Test role properties and identity."""

    def test_role_id(self, mock_client: MagicMock) -> None:
        """Test that role ID is correct."""
        role = SynchronizerRole(mock_client)
        assert role.role_id == SYNC_ROLE_ID
        assert role.role_id == "visualizer@_sync"

    def test_role_family(self, mock_client: MagicMock) -> None:
        """Test that role family is visualizer."""
        role = SynchronizerRole(mock_client)
        assert role.role_family == "visualizer"

    def test_has_connection_returns_true(self, mock_client: MagicMock) -> None:
        """Test that has_connection always returns True for bridge roles."""
        role = SynchronizerRole(mock_client)
        assert role.has_connection() is True

    def test_supports_preconnect_audio(self, mock_client: MagicMock) -> None:
        """Test that preconnect audio is supported."""
        role = SynchronizerRole(mock_client)
        assert role.supports_preconnect_audio() is True


class TestSynchronizerRoleAudioRequirements:
    """Test audio requirement setup."""

    def test_initial_requirements_none(self, mock_client: MagicMock) -> None:
        """Test that audio requirements are None before setup."""
        role = SynchronizerRole(mock_client)
        assert role.get_audio_requirements() is None

    def test_setup_audio_requirements(self, mock_client: MagicMock) -> None:
        """Test that setup creates proper audio requirements."""
        role = SynchronizerRole(mock_client)
        role.setup_audio_requirements()
        reqs = role.get_audio_requirements()
        assert reqs is not None
        assert reqs.sample_rate == 48_000
        assert reqs.bit_depth == 16
        assert reqs.channels == 2
        assert reqs.frame_duration_us == 25_000


class TestSynchronizerRoleCallbacks:
    """Test callback wiring and invocation."""

    def test_callbacks_not_set_initially(self, mock_client: MagicMock) -> None:
        """Test that callbacks are None before set_callbacks."""
        role = SynchronizerRole(mock_client)
        assert role._on_visualization_data_cb is None
        assert role._on_stream_start_cb is None
        assert role._on_stream_end_cb is None

    def test_set_callbacks(self, mock_client: MagicMock) -> None:
        """Test that set_callbacks wires up all callbacks."""
        role = SynchronizerRole(mock_client)
        viz_cb = MagicMock()
        start_cb = MagicMock()
        end_cb = MagicMock()
        role.set_callbacks(
            on_visualization_data=viz_cb,
            on_stream_start=start_cb,
            on_stream_end=end_cb,
        )
        assert role._on_visualization_data_cb is viz_cb
        assert role._on_stream_start_cb is start_cb
        assert role._on_stream_end_cb is end_cb

    def test_on_stream_start_invokes_callback(self, mock_client: MagicMock) -> None:
        """Test that on_stream_start creates extractor and calls callback."""
        role = SynchronizerRole(mock_client)
        start_cb = MagicMock()
        role.set_callbacks(
            on_visualization_data=MagicMock(),
            on_stream_start=start_cb,
            on_stream_end=MagicMock(),
        )
        role.setup_audio_requirements()
        role.setup_stream_config()

        with patch(
            "music_assistant.providers.sendspin.synchronizer_role.VisualizerFeatureExtractor"
        ):
            role.on_stream_start()

        start_cb.assert_called_once()

    def test_on_stream_end_invokes_callback(self, mock_client: MagicMock) -> None:
        """Test that on_stream_end clears extractor and calls callback."""
        role = SynchronizerRole(mock_client)
        end_cb = MagicMock()
        role.set_callbacks(
            on_visualization_data=MagicMock(),
            on_stream_start=MagicMock(),
            on_stream_end=end_cb,
        )
        role.on_stream_end()
        end_cb.assert_called_once()
        assert role._extractor is None

    def test_on_audio_chunk_without_extractor_is_noop(self, mock_client: MagicMock) -> None:
        """Test that audio chunks are ignored before stream starts."""
        role = SynchronizerRole(mock_client)
        viz_cb = MagicMock()
        role.set_callbacks(
            on_visualization_data=viz_cb,
            on_stream_start=MagicMock(),
            on_stream_end=MagicMock(),
        )
        chunk = MagicMock()
        chunk.data = b"\x00" * 100
        chunk.timestamp_us = 0
        role.on_audio_chunk(chunk)
        viz_cb.assert_not_called()

    def test_on_audio_chunk_with_extractor_calls_callback(self, mock_client: MagicMock) -> None:
        """Test that audio chunks produce visualization data callback."""
        role = SynchronizerRole(mock_client)
        viz_cb = MagicMock()
        role.set_callbacks(
            on_visualization_data=viz_cb,
            on_stream_start=MagicMock(),
            on_stream_end=MagicMock(),
        )
        role.setup_audio_requirements()
        role.setup_stream_config()

        mock_extractor = MagicMock()
        mock_frame = MagicMock()
        mock_extractor.process_chunk.return_value = [mock_frame]
        role._extractor = mock_extractor

        chunk = MagicMock()
        chunk.data = b"\x00" * 100
        chunk.timestamp_us = 1000000
        role.on_audio_chunk(chunk)

        mock_extractor.process_chunk.assert_called_once_with(chunk.data, chunk.timestamp_us)
        viz_cb.assert_called_once_with(mock_frame)
