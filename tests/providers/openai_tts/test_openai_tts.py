"""Tests for the OpenAI Text-to-speech provider."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError, web
from music_assistant_models.enums import ContentType, MediaType, ProviderType, StreamType

from music_assistant.providers.openai_tts import (
    CONF_VOICES,
    DEFAULT_VOICES,
    SUPPORTED_FEATURES,
    OpenAITTSProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

INSTANCE_ID = "openai_tts_instance"


def create_provider(**config_values: ConfigValueType) -> OpenAITTSProvider:
    """Construct an openai_tts provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    mass.streams.base_url = "http://mass.local:8095"
    # no voice listing endpoint available in these tests
    mass.http_session.get = MagicMock(side_effect=ClientError("no connection"))
    manifest = MagicMock()
    manifest.type = ProviderType.PLUGIN
    manifest.domain = "openai_tts"
    config = MagicMock()
    config.name = "OpenAI Text-to-speech"
    config.instance_id = INSTANCE_ID
    config.values = {}
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: config_values.get(key, default)
    )
    return OpenAITTSProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def test_resolve_voices_honours_config_override() -> None:
    """The config override wins, stripped and de-duplicated with empty entries dropped."""
    provider = create_provider(**{CONF_VOICES: " nova ,alloy,, nova,echo "})
    assert await provider._resolve_voices() == ["nova", "alloy", "echo"]


async def test_resolve_voices_falls_back_to_defaults() -> None:
    """Without an override and with failing discovery, the default voices are used."""
    provider = create_provider()
    assert await provider._resolve_voices() == list(DEFAULT_VOICES)


async def test_get_tts_engines_yields_engine_per_voice() -> None:
    """Every voice is exposed as an engine, using the voice identifier verbatim."""
    provider = create_provider()
    provider._voices = ["alloy", "nova"]
    engines = await provider.get_tts_engines()
    assert [(engine.id, engine.name) for engine in engines] == [
        ("alloy", "alloy"),
        ("nova", "nova"),
    ]
    assert all(engine.provider is provider for engine in engines)


async def test_get_tts_message_returns_http_streamdetails() -> None:
    """The rendered clip is served as MP3 over the instance's own stream route."""
    provider = create_provider()
    provider._voices = ["alloy"]
    file_id = "a" * 64
    with patch.object(provider, "_render_speech", AsyncMock(return_value=file_id)):
        streamdetails = await provider.get_tts_message("hello there")
    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.audio_format.content_type == ContentType.MP3
    assert streamdetails.media_type == MediaType.SOUND_EFFECT
    assert streamdetails.item_id == file_id
    assert streamdetails.path == f"http://mass.local:8095/{INSTANCE_ID}_speech?id={file_id}"


async def test_handle_speech_request_rejects_invalid_id() -> None:
    """A missing or non-hash id is rejected before the filesystem is touched."""
    provider = create_provider()
    for query in ({}, {"id": "../../../etc/passwd"}, {"id": "NOTAHASH"}):
        request = MagicMock(spec=web.Request)
        request.query = query
        # _cache_dir is unset without handle_async_init, so any filesystem
        # access in the handler would raise instead of returning a response
        response = await provider._handle_speech_request(request)
        assert response.status == 400
