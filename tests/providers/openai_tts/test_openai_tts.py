"""Tests for the OpenAI Text-to-speech provider."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError, web
from music_assistant_models.enums import ContentType, MediaType, ProviderType, StreamType

from music_assistant.providers.openai_tts import (
    CONF_VOICES,
    DEFAULT_VOICES,
    SUPPORTED_FEATURES,
    OpenAITTSProvider,
    fetch_backend_voices,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

INSTANCE_ID = "openai_tts_instance"


def create_provider(**config_values: ConfigValueType) -> OpenAITTSProvider:
    """Construct an openai_tts provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    mass.streams.base_url = "http://mass.local:8095"
    # no setup data, so every read resolves through the config values below
    mass.config.get = MagicMock(return_value={})
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
    provider = create_provider(**{CONF_VOICES: [" nova ", "alloy", "", " nova", "echo "]})
    assert await provider._resolve_voices() == ["nova", "alloy", "echo"]


async def test_resolve_voices_accepts_a_single_string_override() -> None:
    """A comma separated string is accepted next to the stored list of values."""
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


def create_voices_session(payload: object) -> MagicMock:
    """Return an http session whose voices endpoint responds with the given payload."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(return_value=payload)
    session = MagicMock()
    session.get = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False)
        )
    )
    return session


async def test_fetch_backend_voices_accepts_known_payload_shapes() -> None:
    """A voices listing may be wrapped or bare, holding plain names or objects."""
    for payload, expected in (
        ({"voices": ["af_bella", "am_adam"]}, ["af_bella", "am_adam"]),
        (["af_bella"], ["af_bella"]),
        ({"voices": [{"id": "af_bella"}, {"name": "am_adam"}]}, ["af_bella", "am_adam"]),
        ({"voices": [{"unexpected": "shape"}, "af_bella"]}, ["af_bella"]),
        ({"voices": []}, []),
        # an unusable entry must not discard the voices around it
        ({"voices": [None, 5, "af_bella", {"id": "am_adam"}]}, ["af_bella", "am_adam"]),
        # a bare json string is not a voice list, and must not yield one-letter ids
        ("af_bella", []),
        ({"voices": "af_bella"}, []),
    ):
        session = create_voices_session(payload)
        assert await fetch_backend_voices(session, "http://localhost:8880/v1") == expected


async def test_fetch_backend_voices_never_raises() -> None:
    """A backend without a voice listing yields no voices instead of an error."""
    session = MagicMock()
    session.get = MagicMock(side_effect=ClientError("no connection"))
    assert await fetch_backend_voices(session, "https://api.openai.com/v1") == []


async def test_index_cache_adopts_only_rendered_clips(tmp_path: Path) -> None:
    """Anything this provider did not write itself stays unreachable through the route."""
    provider = create_provider()
    provider._cache_dir = str(tmp_path)
    clip = tmp_path / f"{'b' * 64}.mp3"
    clip.write_bytes(b"clip")
    (tmp_path / "not-a-hash.mp3").write_bytes(b"clip")
    (tmp_path / f"{'b' * 64}.txt").write_bytes(b"clip")
    (tmp_path / f"{'c' * 64}.mp3").symlink_to(clip)

    assert await provider._index_cache() == {"b" * 64: str(clip)}


async def test_handle_speech_request_rejects_missing_id() -> None:
    """A request without an id is rejected."""
    provider = create_provider()
    provider._clips = {}
    request = MagicMock(spec=web.Request)
    request.query = {}
    response = await provider._handle_speech_request(request)
    assert response.status == 400


async def test_handle_speech_request_serves_indexed_clips_only() -> None:
    """An id that this instance did not render never reaches the filesystem."""
    provider = create_provider()
    provider._clips = {}
    for query in ({"id": "../../../etc/passwd"}, {"id": "NOTAHASH"}, {"id": "a" * 64}):
        request = MagicMock(spec=web.Request)
        request.query = query
        with pytest.raises(web.HTTPNotFound):
            await provider._handle_speech_request(request)
