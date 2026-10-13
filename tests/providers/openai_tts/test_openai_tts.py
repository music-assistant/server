"""Tests for the OpenAI Text-to-speech provider."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientError, ClientPayloadError, web
from music_assistant_models.enums import (
    ContentType,
    FlowStepType,
    MediaType,
    ProviderType,
    StreamType,
)

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.openai_tts import (
    CONF_BASE_URL,
    CONF_MODEL,
    CONF_RESPONSE_FORMAT,
    CONF_VOICES,
    DEFAULT_VOICES,
    SUPPORTED_FEATURES,
    OpenAITTSProvider,
    fetch_backend_voices,
    normalize_base_url,
)
from music_assistant.providers.openai_tts.setup_flow import run_setup

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

INSTANCE_ID = "openai_tts_instance"
MLX_MODEL = "mlx-community/Voxtral-4B-TTS-2603-mlx-bf16"


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


async def test_resolve_voices_splits_values_holding_commas() -> None:
    """A whole list pasted into one value is split into the separate voices."""
    provider = create_provider(**{CONF_VOICES: [" nova ,alloy", "echo", " nova"]})
    assert await provider._resolve_voices() == ["nova", "alloy", "echo"]


async def test_resolve_voices_accepts_a_hand_edited_string() -> None:
    """A raw string in the stored config is read as a list of voices."""
    provider = create_provider(**{CONF_VOICES: " nova ,alloy,, nova,echo "})
    assert await provider._resolve_voices() == ["nova", "alloy", "echo"]


async def test_resolve_voices_ignores_an_empty_override() -> None:
    """An empty override is the shape stored when nothing is configured."""
    provider = create_provider(**{CONF_VOICES: []})
    assert await provider._resolve_voices() == list(DEFAULT_VOICES)


async def test_voices_config_entry_accepts_a_cleared_field() -> None:
    """Clearing the field submits no value, which must still parse as a list."""
    provider = create_provider()
    entry = next(e for e in await provider.get_config_entries() if e.key == CONF_VOICES)
    assert entry.parse_value(None) == []


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


async def test_get_tts_message_reports_the_configured_format() -> None:
    """The clip is announced in the format it was requested in."""
    provider = create_provider(**{CONF_RESPONSE_FORMAT: "wav"})
    provider._voices = ["alloy"]
    with patch.object(provider, "_render_speech", AsyncMock(return_value="a" * 64)):
        streamdetails = await provider.get_tts_message("hello there")
    assert streamdetails.audio_format.content_type == ContentType.WAV


async def test_response_format_falls_back_on_an_unknown_value() -> None:
    """A stored format we cannot play back resolves to the default."""
    assert create_provider(**{CONF_RESPONSE_FORMAT: "pcm"})._response_format == "mp3"
    assert create_provider()._response_format == "mp3"


async def test_render_speech_requests_and_stores_the_configured_format(tmp_path: Path) -> None:
    """The format is sent to the backend, names the clip and is part of its identity."""
    provider = create_provider(**{CONF_RESPONSE_FORMAT: "wav"})
    provider._cache_dir = str(tmp_path)
    provider._render_lock = asyncio.Lock()
    provider._clips = {}
    response = MagicMock(status=200, read=AsyncMock(return_value=b"RIFF"))
    post = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False)
        )
    )
    with patch.object(provider.mass.http_session, "post", post):
        file_id = await provider._render_speech("hello there", "alloy")

    assert post.call_args.kwargs["json"]["response_format"] == "wav"
    assert provider._clips[file_id] == str(tmp_path / f"{file_id}.wav")
    mp3_provider = create_provider()
    mp3_provider._cache_dir = str(tmp_path)
    mp3_provider._render_lock = asyncio.Lock()
    mp3_provider._clips = {}
    with patch.object(mp3_provider.mass.http_session, "post", post):
        assert await mp3_provider._render_speech("hello there", "alloy") != file_id


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


async def test_fetch_backend_voices_requests_the_model_and_reads_data() -> None:
    """The model is passed along and an OpenAI style "data" listing is understood."""
    session = create_voices_session(
        {"object": "list", "model": MLX_MODEL, "data": [{"id": "casual_male", "name": "Casual"}]}
    )
    voices = await fetch_backend_voices(session, "http://localhost:8000/v1", model=MLX_MODEL)
    assert voices == ["casual_male"]
    assert session.get.call_args.kwargs["params"] == {"model": MLX_MODEL}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.168.1.47:8000", "http://192.168.1.47:8000/v1"),
        ("http://192.168.1.47:8000/v1", "http://192.168.1.47:8000/v1"),
        ("http://192.168.1.47:8000/v1/audio", "http://192.168.1.47:8000/v1"),
        ("http://192.168.1.47:8000/v1/audio/speech", "http://192.168.1.47:8000/v1"),
        ("http://192.168.1.47:8000/v1/audio/voices", "http://192.168.1.47:8000/v1"),
        (" http://192.168.1.47:8000/v1/ ", "http://192.168.1.47:8000/v1"),
        ("http://192.168.1.47:8000/", "http://192.168.1.47:8000/v1"),
        ("192.168.1.47:8000", "http://192.168.1.47:8000/v1"),
        ("http://[::1]:8000", "http://[::1]:8000/v1"),
        # malformed input is left for the request to reject, normalizing never raises
        ("http://[::1", "http://[::1/v1"),
        ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
    ],
)
def test_normalize_base_url(url: str, expected: str) -> None:
    """A bare server address or the url of an audio endpoint resolves to the base url."""
    assert normalize_base_url(url) == expected


@pytest.mark.parametrize(
    ("override", "setup_data", "discovered", "expected"),
    [
        (["override"], {CONF_VOICES: ["neutral_female"]}, ["af_bella"], ["override"]),
        ([], {CONF_VOICES: ["neutral_female"]}, ["af_bella"], ["neutral_female"]),
        ([], {CONF_VOICES: []}, ["af_bella"], ["af_bella"]),
        ([], {CONF_VOICES: []}, [], list(DEFAULT_VOICES)),
        # installs set up before voices could be entered during setup
        (["override"], {}, ["af_bella"], ["override"]),
        ([], {}, ["af_bella"], ["af_bella"]),
    ],
)
async def test_resolve_voices_order(
    override: list[str],
    setup_data: dict[str, list[str]],
    discovered: list[str],
    expected: list[str],
) -> None:
    """The override wins over the setup voices, which win over discovery and the defaults."""
    provider = create_provider(**{CONF_VOICES: override})
    mass: Any = provider.mass
    mass.config.get = MagicMock(return_value=setup_data)
    mass.http_session = create_voices_session({"voices": discovered})
    assert await provider._resolve_voices() == expected


def create_setup_session(
    voices_payload: object,
    speech_responses: list[MagicMock],
    finish_values: dict[str, Any],
    setup_data: dict[str, Any] | None = None,
) -> tuple[SetupSession, MagicMock]:
    """
    Return a setup session and its http session mock.

    The backend lists the given voices and answers the speech requests in turn.
    """

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        finish_values.update(values)
        return {"instance_id": INSTANCE_ID}

    context = SetupFlowContext(
        kind="reconfigure" if setup_data else "setup",
        reason="user",
        domain="openai_tts",
        setup_data=setup_data or {},
    )
    http_session = create_voices_session(voices_payload)
    http_session.post = MagicMock(
        side_effect=[
            MagicMock(
                __aenter__=AsyncMock(return_value=response),
                __aexit__=AsyncMock(return_value=False),
            )
            for response in speech_responses
        ]
    )
    mass = Mock()
    mass.http_session = http_session
    return SetupSession(mass, "flow-test", context, finish_handler), http_session


def create_speech_response(audio: bytes | Exception) -> MagicMock:
    """Return a 200 speech response whose body yields the audio or breaks off with the error."""
    if isinstance(audio, Exception):
        return MagicMock(status=200, read=AsyncMock(side_effect=audio))
    return MagicMock(status=200, read=AsyncMock(return_value=audio))


async def wait_for_form(session: SetupSession, previous: object = None) -> Any:
    """Wait until the flow publishes a FORM step other than the previous one."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        step = session.current_step
        if step and step.type == FlowStepType.FORM and step is not previous:
            return step
        await asyncio.sleep(0.01)
    raise AssertionError("no new form step within timeout")


async def test_setup_asks_for_voices_when_the_render_fails_without_listed_voices() -> None:
    """A server that lists no voices and rejects the guessed one gets its voices named."""
    finish_values: dict[str, Any] = {}
    broken = ClientPayloadError("Response payload is not completed")
    session, http_session = create_setup_session(
        {"object": "list", "model": MLX_MODEL, "data": []},
        [create_speech_response(broken), create_speech_response(b"ID3")],
        finish_values,
    )
    task = asyncio.create_task(run_setup(session))
    step = await wait_for_form(session)
    assert CONF_VOICES not in {entry.key for entry in step.entries}
    form_values: dict[str, ConfigValueType] = {
        CONF_BASE_URL: "192.168.1.47:8000",
        CONF_MODEL: MLX_MODEL,
        CONF_RESPONSE_FORMAT: "mp3",
    }

    session.handle_submit(form_values)
    step = await wait_for_form(session, step)
    assert step.error_translations["base"].key == "render_incomplete"
    assert step.error_translations[CONF_VOICES].key == "voices_not_listed"
    assert CONF_VOICES in {entry.key for entry in step.entries}
    session.handle_submit({**form_values, CONF_VOICES: ["neutral_female"]})
    await task

    post = http_session.post
    assert post.call_args_list[0].args[0] == "http://192.168.1.47:8000/v1/audio/speech"
    assert post.call_args_list[0].kwargs["json"]["voice"] == DEFAULT_VOICES[0]
    assert post.call_args_list[1].kwargs["json"]["voice"] == "neutral_female"
    assert http_session.get.call_args.kwargs["params"] == {"model": MLX_MODEL}
    assert finish_values[CONF_BASE_URL] == "http://192.168.1.47:8000/v1"
    assert finish_values[CONF_VOICES] == ["neutral_female"]


async def test_setup_reports_a_broken_stream_as_a_render_failure() -> None:
    """A body that breaks off is a failed render, not an unreachable server."""
    session, _ = create_setup_session(
        {"voices": ["af_bella"]},
        [create_speech_response(ClientPayloadError("Response payload is not completed"))],
        {},
    )
    task = asyncio.create_task(run_setup(session))
    step = await wait_for_form(session)
    session.handle_submit(
        {
            CONF_BASE_URL: "http://localhost:8880/v1",
            CONF_MODEL: "kokoro",
            CONF_RESPONSE_FORMAT: "mp3",
        }
    )
    step = await wait_for_form(session, step)
    assert step.error_translations["base"].key == "render_incomplete"
    # the server listed its voices, so there is nothing to ask for
    assert CONF_VOICES not in {entry.key for entry in step.entries}
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_setup_reports_the_server_error_of_a_failed_render() -> None:
    """A rejected render shows the server's own (trimmed) error text."""
    rejected = MagicMock(status=400, text=AsyncMock(return_value=f"  {'x' * 300}  "))
    session, _ = create_setup_session({"voices": ["af_bella"]}, [rejected], {})
    task = asyncio.create_task(run_setup(session))
    step = await wait_for_form(session)
    session.handle_submit(
        {
            CONF_BASE_URL: "http://localhost:8880/v1",
            CONF_MODEL: "kokoro",
            CONF_RESPONSE_FORMAT: "mp3",
        }
    )
    step = await wait_for_form(session, step)
    assert step.error_translations["base"].key == "render_failed"
    assert step.error_translations["base"].args == ["x" * 200]
    assert CONF_VOICES not in step.error_translations
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_reconfigure_keeps_asking_for_stored_voices_and_clears_them() -> None:
    """Stored setup voices are shown on a reconfigure, and an emptied field clears them."""
    finish_values: dict[str, Any] = {}
    session, http_session = create_setup_session(
        {"voices": ["af_bella"]},
        [create_speech_response(b"ID3")],
        finish_values,
        setup_data={CONF_BASE_URL: "http://localhost:8880/v1", CONF_VOICES: ["neutral_female"]},
    )
    task = asyncio.create_task(run_setup(session))
    step = await wait_for_form(session)
    entry = next(entry for entry in step.entries if entry.key == CONF_VOICES)
    assert entry.value == ["neutral_female"]
    session.handle_submit(
        {
            CONF_BASE_URL: "http://localhost:8880/v1",
            CONF_MODEL: "kokoro",
            CONF_RESPONSE_FORMAT: "mp3",
            CONF_VOICES: [],
        }
    )
    await task
    assert http_session.post.call_args.kwargs["json"]["voice"] == "af_bella"
    assert finish_values[CONF_VOICES] == []


async def test_index_cache_adopts_only_rendered_clips(tmp_path: Path) -> None:
    """Anything this provider did not write itself stays unreachable through the route."""
    provider = create_provider()
    provider._cache_dir = str(tmp_path)
    clip = tmp_path / f"{'b' * 64}.mp3"
    clip.write_bytes(b"clip")
    (tmp_path / "not-a-hash.mp3").write_bytes(b"clip")
    (tmp_path / f"{'b' * 64}.txt").write_bytes(b"clip")
    (tmp_path / f"{'c' * 64}.mp3").symlink_to(clip)
    wav_clip = tmp_path / f"{'d' * 64}.wav"
    wav_clip.write_bytes(b"clip")
    (tmp_path / f"{'e' * 64}.pcm").write_bytes(b"clip")

    assert await provider._index_cache() == {"b" * 64: str(clip), "d" * 64: str(wav_clip)}


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
