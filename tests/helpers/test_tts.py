"""Tests for the TTS engine query helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.tts import (
    TTSLanguageNotSupportedError,
    query_tts_engine_with_language_fallback,
)
from music_assistant.models.plugin import PluginProvider, TTSEngine


def _create_engine() -> tuple[TTSEngine, AsyncMock]:
    """Create a TTS engine and the mock that renders its speech."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "hass"
    provider.get_tts_message = AsyncMock()
    return TTSEngine(id="tts.x", name="X", provider=provider), provider.get_tts_message


async def test_language_rejection_retries_without_language(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected language is retried once with the engine's default voice."""
    engine, get_tts_message = _create_engine()
    sentinel = MagicMock()
    get_tts_message.side_effect = [
        TTSLanguageNotSupportedError("TTS engine 'tts.x' does not support language 'en-US'"),
        sentinel,
    ]

    with caplog.at_level("WARNING"):
        result = await query_tts_engine_with_language_fallback(engine, "Hello", "en-US")

    assert result is sentinel
    assert get_tts_message.call_count == 2
    second_call = get_tts_message.call_args_list[1]
    assert second_call.kwargs["language"] is None
    assert "retrying with the engine's default voice" in caplog.text


async def test_generic_errors_do_not_retry() -> None:
    """A generic exception from the engine propagates without a retry."""
    engine, get_tts_message = _create_engine()
    get_tts_message.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await query_tts_engine_with_language_fallback(engine, "Hello", "en-US")

    assert get_tts_message.call_count == 1


async def test_music_assistant_error_does_not_retry() -> None:
    """A structured MusicAssistantError propagates without a retry."""
    engine, get_tts_message = _create_engine()
    get_tts_message.side_effect = MusicAssistantError("nope")

    with pytest.raises(MusicAssistantError):
        await query_tts_engine_with_language_fallback(engine, "Hello", "en-US")

    assert get_tts_message.call_count == 1


async def test_rejection_without_language_does_not_retry() -> None:
    """A language rejection raised while no language was requested is not retried."""
    engine, get_tts_message = _create_engine()
    get_tts_message.side_effect = TTSLanguageNotSupportedError(
        "TTS engine 'tts.x' does not support language 'en-US'"
    )

    with pytest.raises(TTSLanguageNotSupportedError):
        await query_tts_engine_with_language_fallback(engine, "Hello", None)

    assert get_tts_message.call_count == 1
