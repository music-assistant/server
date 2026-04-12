"""Tests for get_plugin_source_stream ffmpeg arguments.

Verifies that the plugin source stream uses -readrate with initial burst
instead of -re, matching the queue flow stream behavior.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.models.plugin import PluginSource


async def _fake_ffmpeg_stream(**_kwargs: Any) -> Any:
    """Fake ffmpeg stream that yields one chunk then stops."""
    yield b"audio-data"


@pytest.fixture
def mock_streams_controller() -> MagicMock:
    """Create a minimal StreamsController-like object with get_plugin_source_stream."""
    ctrl = MagicMock(spec=StreamsController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    # Use the real method, bound to our mock
    ctrl.get_plugin_source_stream = StreamsController.get_plugin_source_stream.__get__(ctrl)
    return ctrl


@pytest.mark.asyncio
async def test_plugin_source_stream_uses_readrate(mock_streams_controller: MagicMock) -> None:
    """Plugin source stream must use -readrate + initial_burst, not -re."""
    plugin_source = PluginSource(
        id="test_plugin",
        name="Test",
        stream_type=StreamType.CUSTOM,
        audio_format=AudioFormat(content_type=ContentType.PCM_S16LE),
    )
    plugin_source.in_use_by = "player1"

    mock_prov = MagicMock()
    mock_prov.get_source.return_value = plugin_source
    mock_prov.get_audio_stream = AsyncMock()
    mock_streams_controller.mass.get_provider.return_value = mock_prov

    output_format = AudioFormat(content_type=ContentType.FLAC)

    with patch(
        "music_assistant.controllers.streams.controller.get_ffmpeg_stream",
    ) as mock_ffmpeg:
        mock_ffmpeg.side_effect = lambda **kw: _fake_ffmpeg_stream(**kw)

        chunks = []
        async for chunk in mock_streams_controller.get_plugin_source_stream(
            plugin_source_id="test_plugin",
            output_format=output_format,
            player_id="player1",
        ):
            chunks.append(chunk)

        mock_ffmpeg.assert_called_once()
        call_kwargs = mock_ffmpeg.call_args.kwargs
        extra_args = call_kwargs["extra_input_args"]

        # Must NOT contain -re
        assert "-re" not in extra_args, f"Found -re in extra_input_args: {extra_args}"

        # Must contain -readrate with burst
        assert "-readrate" in extra_args
        assert "1.1" in extra_args
        assert "-readrate_initial_burst" in extra_args
        assert "5" in extra_args

        # Also keep -y
        assert "-y" in extra_args
