"""Unit tests for AI Radio just-in-time clip rendering."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import (
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
)
from music_assistant.helpers.tags import AudioTags
from music_assistant.models.plugin import PluginProvider, TTSEngine
from music_assistant.providers.ai_radio.constants import (
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_RENDERED_TEXT,
    ATTR_SESSION_ID,
    ATTR_STATION_ID,
    ATTR_WEB_SEARCH_MODE,
    CLIP_STREAMDETAILS_EXPIRATION,
    CONF_TTS_LOUDNESS_BOOST,
    DEFAULT_LLM_INSTRUCTIONS,
    MIN_LOUDNESS_REFERENCE_SECONDS,
    TTS_CLIP_PCM_FORMAT,
    TTS_PEAK_CEILING_DB,
    TTS_SPEECHNORM_FILTER,
)
from music_assistant.providers.ai_radio.models import SessionState
from music_assistant.providers.ai_radio.rendering import AIRadioRenderMixin


class DummyRenderer(AIRadioRenderMixin):
    """Minimal harness exposing the render path."""

    domain = "ai_radio"
    instance_id = "ai_radio--test"

    def __init__(self) -> None:
        """Initialize the harness with recording stubs."""
        self.logger = logging.getLogger("tests.ai_radio.rendering")
        self._sessions: dict[str, Any] = {}
        self._hosts: dict[str, dict[str, Any]] = {}
        self.llm_prompts: list[str] = []
        self.tts_texts: list[str] = []
        self.tts_options: list[dict[str, Any] | None] = []
        self.weather_calls = 0
        self.fail_generation = False
        self.measure_calls: list[str] = []
        self.measured_loudness: float | None = None

    def _configured_now(self) -> Any:
        return __import__("datetime").datetime(2026, 7, 30, 18, 30)

    async def _generate_text(
        self, instructions: str, prompt: str, web_mode: str, language: str | None = None
    ) -> str:
        # a real suspension point so concurrent callers actually interleave under
        # asyncio.gather, otherwise the lock in get_stream_details is never exercised
        await asyncio.sleep(0)
        if self.fail_generation:
            raise RuntimeError("llm down")
        self.llm_prompts.append(prompt)
        return "Good evening, it is warm out."

    async def _prepare_weather_tokens(self) -> dict[str, str]:
        self.weather_calls += 1
        return {"<weather_hourly>": f"fresh weather {self.weather_calls}"}

    async def _render_tts_media(
        self,
        text: str,
        engine_uid: str | None = None,
        language: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str, StreamType, AudioFormat]:
        self.tts_texts.append(text)
        self.tts_options.append(options)
        return (
            f"http://ha.invalid/api/tts_proxy/{len(self.tts_texts)}.mp3",
            StreamType.HTTP,
            AudioFormat(content_type=ContentType.MP3),
        )

    async def _probe_duration(self, path: str) -> int | None:
        return 9

    async def _measure_loudness(self, path: str) -> float | None:
        self.measure_calls.append(path)
        return self.measured_loudness


class RealTtsRenderer(DummyRenderer):
    """Harness that exercises the mixin's real TTS path instead of the DummyRenderer stub."""

    _render_tts_media = AIRadioRenderMixin._render_tts_media


def _tts_renderer(path: str, audio_format: AudioFormat | None = None) -> RealTtsRenderer:
    """Build a renderer whose TTS engine returns StreamDetails carrying the given path."""
    renderer = RealTtsRenderer()
    plugin = MagicMock(spec=PluginProvider)
    plugin.instance_id = "hass_1"
    plugin.get_tts_message = AsyncMock(
        return_value=SimpleNamespace(
            path=path, audio_format=audio_format or AudioFormat(content_type=ContentType.UNKNOWN)
        )
    )
    engine = TTSEngine(id="tts.cloud", name="Cloud", provider=plugin)
    cast("Any", renderer)._get_tts_engine = AsyncMock(return_value=engine)
    return renderer


def _clip_item(clip_id: str, queue_id: str = "player_a", **overrides: Any) -> QueueItem:
    """Build a queue item for a pending AI Radio clip."""
    attributes: dict[str, Any] = {
        ATTR_SESSION_ID: "sess",
        ATTR_STATION_ID: "st",
        ATTR_PROMPT: "It is <timestamp>. Weather: <weather_hourly>.",
        ATTR_MAX_CHARS: 300,
        ATTR_WEB_SEARCH_MODE: "disabled",
    }
    attributes.update(overrides)
    media_item = SoundEffect(
        item_id=clip_id,
        provider="ai_radio--test",
        name="Weather",
        provider_mappings={
            ProviderMapping(
                item_id=clip_id,
                provider_domain="ai_radio",
                provider_instance="ai_radio--test",
            )
        },
    )
    return QueueItem(
        queue_id=queue_id,
        queue_item_id=f"qi_{clip_id}",
        name="Weather",
        duration=None,
        media_item=media_item,
        extra_attributes=attributes,
    )


def _attach_queues(renderer: DummyRenderer, queues: dict[str, list[QueueItem]]) -> list[bool]:
    """Wire a minimal player_queues stub and return the signal_update call log."""
    signals: list[bool] = []
    cast("Any", renderer).mass = SimpleNamespace(
        player_queues=SimpleNamespace(
            all=lambda: tuple(SimpleNamespace(queue_id=queue_id) for queue_id in queues),
            items=lambda queue_id, limit=500, offset=0: queues.get(queue_id, [])[
                offset : offset + limit
            ],
            signal_update=lambda _queue_id, items_changed=False: signals.append(items_changed),
        ),
        metadata=SimpleNamespace(locale="en_US"),
    )
    _attach_normalization(renderer, queue_ids=tuple(queues))
    return signals


def _attach_queue(renderer: DummyRenderer, items: list[QueueItem]) -> list[bool]:
    """Wire a single-queue player_queues stub and return the signal_update call log."""
    return _attach_queues(renderer, {"player_a": items})


def _attach_normalization(
    renderer: DummyRenderer,
    *,
    enabled: bool = True,
    target: int = -14,
    boost: int = 3,
    tracks_mode: str = VolumeNormalizationMode.FALLBACK_DYNAMIC.value,
    queue_ids: tuple[str, ...] = ("player_a",),
) -> None:
    """Wire the queue, streams and provider config that decide the clip's loudness gain."""

    def queue_setting(queue_id: str, key: str, default: str) -> str:
        assert queue_id in queue_ids
        assert key == CONF_VOLUME_NORMALIZATION
        assert default == CONF_VALUE_ENABLED
        return CONF_VALUE_ENABLED if enabled else CONF_VALUE_DISABLED

    def streams_setting(key: str, **_kwargs: Any) -> str | int:
        if key == CONF_VOLUME_NORMALIZATION_TRACKS:
            return tracks_mode
        assert key == CONF_VOLUME_NORMALIZATION_TARGET
        return target

    def provider_setting(key: str) -> int:
        assert key == CONF_TTS_LOUDNESS_BOOST
        return boost

    mass = cast("Any", renderer).mass
    mass.config = SimpleNamespace(get_effective_player_queue_config_value=queue_setting)
    mass.streams = SimpleNamespace(get_config_value=streams_setting)
    cast("Any", renderer).config = SimpleNamespace(get_value=provider_setting)


async def test_render_resolves_deferred_placeholders_at_render_time() -> None:
    """The prompt sent to the LLM carries render-time weather, not plan-time."""
    renderer = DummyRenderer()
    item = _clip_item("sess_001")
    _attach_queue(renderer, [item])

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert renderer.weather_calls == 1
    assert "fresh weather 1" in renderer.llm_prompts[0]
    assert "<timestamp>" not in renderer.llm_prompts[0]
    assert streamdetails.media_type == MediaType.SOUND_EFFECT
    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.duration == 9
    assert streamdetails.expiration == 60
    assert streamdetails.can_seek is False
    assert streamdetails.allow_seek is False


async def test_render_caches_the_script_and_the_minted_media() -> None:
    """A second render within the cache window reuses both the stored script and media."""
    renderer = DummyRenderer()
    item = _clip_item("sess_001")
    signals = _attach_queue(renderer, [item])

    first = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    second = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert len(renderer.llm_prompts) == 1
    assert renderer.tts_texts == ["Good evening, it is warm out."]
    assert first.path == second.path
    assert item.extra_attributes[ATTR_RENDERED_TEXT] == "Good evening, it is warm out."
    assert signals == [True]


async def test_concurrent_renders_call_the_llm_once() -> None:
    """Two simultaneous requests for one clip render a single script."""
    renderer = DummyRenderer()
    _attach_queue(renderer, [_clip_item("sess_001")])

    await asyncio.gather(
        renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT),
        renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT),
    )

    assert len(renderer.llm_prompts) == 1


async def test_concurrent_renders_mint_the_clip_only_once() -> None:
    """Three simultaneous requests for one clip share a single minted TTS render."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])

    results = await asyncio.gather(
        renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT),
        renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT),
        renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT),
    )

    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message.assert_awaited_once()
    assert len({result.path for result in results}) == 1


async def test_cached_media_remints_once_it_expires() -> None:
    """A render requested after the cache window elapses mints a fresh clip."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    cached = cast("Any", renderer)._media_cache["sess_001"]
    cached.minted_at -= CLIP_STREAMDETAILS_EXPIRATION + 1

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    assert engine.provider.get_tts_message.await_count == 2


async def test_a_late_cache_hit_expires_with_the_url_it_serves() -> None:
    """A hit late in the window hands out the url's remaining life, not a fresh full window."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    cached = cast("Any", renderer)._media_cache["sess_001"]
    cached.minted_at -= 45

    late = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    assert engine.provider.get_tts_message.await_count == 1
    assert 14 <= late.expiration <= 15


async def test_a_cache_hit_with_no_useful_life_left_remints() -> None:
    """A hit in the last seconds of the window mints again instead of serving a dying url."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    cached = cast("Any", renderer)._media_cache["sess_001"]
    cached.minted_at -= CLIP_STREAMDETAILS_EXPIRATION - 2

    fresh = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    assert engine.provider.get_tts_message.await_count == 2
    assert fresh.expiration == CLIP_STREAMDETAILS_EXPIRATION


async def test_expired_cache_entries_are_pruned_on_the_next_mint() -> None:
    """Minting a clip drops the entries whose urls died, so the cache cannot grow forever."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001"), _clip_item("sess_002")])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    media_cache = cast("Any", renderer)._media_cache
    media_cache["sess_001"].minted_at -= CLIP_STREAMDETAILS_EXPIRATION + 1
    await renderer.get_stream_details("sess_002", MediaType.SOUND_EFFECT)

    assert set(media_cache) == {"sess_002"}


async def test_render_tts_media_passes_the_locale_as_language() -> None:
    """The DJ script's locale reaches the TTS engine as a hyphenated language code."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message.assert_awaited_once_with(
        "Good evening, it is warm out.", language="en-US", engine_id="tts.cloud", options={}
    )


async def test_render_tts_media_falls_back_without_language_on_rejection() -> None:
    """An engine that rejects the requested language is retried once without it."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])
    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message = AsyncMock(
        side_effect=[
            Exception("unsupported language"),
            SimpleNamespace(
                path="http://example.test/api/tts_proxy/abc123.mp3",
                audio_format=AudioFormat(content_type=ContentType.MP3),
            ),
        ]
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.path == "http://example.test/api/tts_proxy/abc123.mp3"
    assert engine.provider.get_tts_message.await_count == 2
    first_call, second_call = engine.provider.get_tts_message.await_args_list
    assert first_call.kwargs["language"] == "en-US"
    assert second_call.kwargs["language"] is None


async def test_render_tts_media_does_not_retry_after_a_timeout_style_failure() -> None:
    """A structured MusicAssistantError is not a language rejection, so it skips the retry."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    _attach_queue(renderer, [_clip_item("sess_001")])
    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message = AsyncMock(
        side_effect=MusicAssistantError("engine did not respond within 5s")
    )

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine.provider.get_tts_message.assert_awaited_once()


async def test_clip_is_found_in_the_owning_sessions_queue() -> None:
    """The session registry points the lookup at the queue that holds the clip."""
    renderer = DummyRenderer()
    session = SessionState(session_id="sess", station_id="st", queue_id="player_b")
    renderer._sessions = {"sess": session}
    _attach_queues(
        renderer,
        {"player_a": [], "player_b": [_clip_item("sess_001", queue_id="player_b")]},
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.item_id == "sess_001"


async def test_clip_is_found_by_scanning_every_queue_after_a_restart() -> None:
    """With the session registry gone, the clip is still located in its persisted queue."""
    renderer = DummyRenderer()
    _attach_queues(
        renderer,
        {"player_a": [], "player_b": [_clip_item("sess_001", queue_id="player_b")]},
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.item_id == "sess_001"


async def test_unknown_clip_raises_media_not_found() -> None:
    """A clip id that is not in any queue is reported as missing media."""
    renderer = DummyRenderer()
    _attach_queue(renderer, [_clip_item("sess_001")])

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_999", MediaType.SOUND_EFFECT)


async def test_clip_without_prompt_raises_media_not_found() -> None:
    """A clip whose attributes were lost is reported as missing media."""
    renderer = DummyRenderer()
    item = _clip_item("sess_001")
    item.extra_attributes.pop(ATTR_PROMPT)
    _attach_queue(renderer, [item])

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)


async def test_llm_failure_raises_media_not_found() -> None:
    """An LLM failure surfaces as missing media so the core skips the clip."""
    renderer = DummyRenderer()
    renderer.fail_generation = True
    _attach_queue(renderer, [_clip_item("sess_001")])

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)


async def test_render_failure_increments_the_session_skip_counter() -> None:
    """A skipped clip is recorded on its session without failing the run."""
    renderer = DummyRenderer()
    session = SessionState(session_id="sess", station_id="st")
    renderer._sessions = {"sess": session}
    _attach_queue(renderer, [_clip_item("sess_001")])
    renderer.fail_generation = True

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert session.skipped_sections == 1
    assert session.last_render_error


async def test_tts_failure_raises_media_not_found_and_records_skip() -> None:
    """A TTS failure surfaces as missing media and is recorded on the owning session."""

    class UnspeakableRenderer(DummyRenderer):
        async def _render_tts_media(
            self,
            text: str,
            engine_uid: str | None = None,
            language: str | None = None,
            options: dict[str, Any] | None = None,
        ) -> tuple[str, StreamType, AudioFormat]:
            raise RuntimeError("tts down")

    renderer = UnspeakableRenderer()
    session = SessionState(session_id="sess", station_id="st")
    renderer._sessions = {"sess": session}
    _attach_queue(renderer, [_clip_item("sess_001")])

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert session.skipped_sections == 1
    assert session.last_render_error


async def test_probe_failure_is_not_fatal() -> None:
    """A failed duration probe yields streamdetails without a duration."""

    class UnprobableRenderer(DummyRenderer):
        async def _probe_duration(self, path: str) -> int | None:
            return None

    renderer = UnprobableRenderer()
    _attach_queue(renderer, [_clip_item("sess_001")])

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.duration is None


class RealProbeRenderer(DummyRenderer):
    """Harness that exercises the mixin's real duration probe instead of the stub."""

    _probe_duration = AIRadioRenderMixin._probe_duration


def _failing_probe(message: str, monkeypatch: pytest.MonkeyPatch) -> RealProbeRenderer:
    """Build a renderer whose duration probe fails with the given ffprobe message."""

    async def _raise(*_args: Any, **_kwargs: Any) -> AudioTags:
        raise InvalidDataError(message)

    monkeypatch.setattr("music_assistant.providers.ai_radio.rendering.async_parse_tags", _raise)
    renderer = RealProbeRenderer()
    renderer._sessions = {"sess": SessionState(session_id="sess", station_id="st")}
    _attach_queue(renderer, [_clip_item("sess_001")])
    return renderer


async def test_tts_server_error_fails_the_clip_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine that hands out a URL it cannot render fails the clip, not the playback."""
    renderer = _failing_probe(
        "Unable to retrieve info for http://ha.invalid/api/tts_proxy/1.mp3 "
        "(Server returned 5XX Server Error reply)",
        monkeypatch,
    )

    with pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    session = renderer._sessions["sess"]
    assert session.skipped_sections == 1
    assert "enough credit" in session.last_render_error


async def test_unmeasurable_clip_still_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that only fails to measure the audio leaves the clip playable."""
    renderer = _failing_probe(
        "Unable to retrieve info for http://ha.invalid/api/tts_proxy/1.mp3 "
        "(Invalid or unsupported media file)",
        monkeypatch,
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.duration is None
    assert renderer._sessions["sess"].skipped_sections == 0


async def test_generate_script_uses_host_instructions() -> None:
    """The prompt sent to the LLM carries the resolved host's persona instructions."""
    renderer = DummyRenderer()
    renderer._hosts = {"rick": {"id": "rick", "instructions": "Persona text.", "tts_engine": ""}}
    captured: dict[str, str] = {}

    async def fake_generate_text(
        instructions: str,
        prompt: str,  # noqa: ARG001
        web_mode: str,  # noqa: ARG001
        language: str | None = None,  # noqa: ARG001
    ) -> str:
        captured["instructions"] = instructions
        return "script"

    cast("Any", renderer)._generate_text = fake_generate_text
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "rick", ATTR_PROMPT: "p"})

    text = await renderer._generate_script(item, "p", "clip_1")

    assert text == "script"
    assert captured["instructions"] == "Persona text."


async def test_generate_script_falls_back_to_default_instructions() -> None:
    """A clip whose host is gone by render time still generates, using the default persona."""
    renderer = DummyRenderer()
    renderer._hosts = {}
    captured: dict[str, str] = {}

    async def fake_generate_text(
        instructions: str,
        prompt: str,  # noqa: ARG001
        web_mode: str,  # noqa: ARG001
        language: str | None = None,  # noqa: ARG001
    ) -> str:
        # mirrors the empty-to-default fallback the real _generate_text applies (runtime.py)
        captured["instructions"] = instructions.strip() or DEFAULT_LLM_INSTRUCTIONS
        return "script"

    cast("Any", renderer)._generate_text = fake_generate_text
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "gone", ATTR_PROMPT: "p"})

    await renderer._generate_script(item, "p", "clip_1")

    assert captured["instructions"] == DEFAULT_LLM_INSTRUCTIONS


async def test_generate_script_forwards_the_hosts_language() -> None:
    """The host's configured language reaches _generate_text, ready to override the locale."""
    renderer = DummyRenderer()
    renderer._hosts = {
        "rick": {
            "id": "rick",
            "instructions": "Persona text.",
            "tts_engine": "",
            "language": "fr_FR",
        }
    }
    captured: dict[str, str | None] = {}

    async def fake_generate_text(
        instructions: str,  # noqa: ARG001
        prompt: str,  # noqa: ARG001
        web_mode: str,  # noqa: ARG001
        language: str | None = None,
    ) -> str:
        captured["language"] = language
        return "script"

    cast("Any", renderer)._generate_text = fake_generate_text
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "rick", ATTR_PROMPT: "p"})

    await renderer._generate_script(item, "p", "clip_1")

    assert captured["language"] == "fr_FR"


async def test_generate_script_forwards_empty_language_when_host_has_none() -> None:
    """A host with no configured language forwards an empty string, not None."""
    renderer = DummyRenderer()
    renderer._hosts = {"rick": {"id": "rick", "instructions": "Persona text.", "tts_engine": ""}}
    captured: dict[str, str | None] = {}

    async def fake_generate_text(
        instructions: str,  # noqa: ARG001
        prompt: str,  # noqa: ARG001
        web_mode: str,  # noqa: ARG001
        language: str | None = None,
    ) -> str:
        captured["language"] = language
        return "script"

    cast("Any", renderer)._generate_text = fake_generate_text
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "rick", ATTR_PROMPT: "p"})

    await renderer._generate_script(item, "p", "clip_1")

    assert captured["language"] == ""


async def test_render_tts_media_prefers_the_hosts_language_over_the_locale() -> None:
    """A host's configured language reaches the TTS engine, overriding the server locale."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": "", "language": "fr_FR"}}
    _attach_queue(renderer, [_clip_item("sess_001", **{ATTR_HOST_ID: "rick"})])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message.assert_awaited_once_with(
        "Good evening, it is warm out.", language="fr-FR", engine_id="tts.cloud", options={}
    )


async def test_render_tts_media_falls_back_to_locale_when_host_language_is_empty() -> None:
    """A host with no configured language falls back to the server locale for the TTS call."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": ""}}
    _attach_queue(renderer, [_clip_item("sess_001", **{ATTR_HOST_ID: "rick"})])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message.assert_awaited_once_with(
        "Good evening, it is warm out.", language="en-US", engine_id="tts.cloud", options={}
    )


async def test_resolve_deferred_placeholders_skips_weather_without_token() -> None:
    """A prompt with no weather placeholder never triggers a weather fetch."""
    renderer = DummyRenderer()

    values = await renderer._resolve_deferred_placeholders("Just plain text, no tokens.")

    assert renderer.weather_calls == 0
    assert values["<weather_hourly>"] == ""
    assert values["<weather_daily>"] == ""


async def test_mint_clip_media_resolves_host_tts_engine() -> None:
    """A clip whose host declares a tts_engine reaches _get_tts_engine with that override."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": "tts.rick_voice"}}
    cast("Any", renderer).mass = SimpleNamespace(metadata=SimpleNamespace(locale="en_US"))
    _attach_normalization(renderer)
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "rick"})

    await renderer._mint_clip_media(item, "hello world", "clip_1")

    cast("Any", renderer)._get_tts_engine.assert_awaited_once_with("tts.rick_voice")


async def test_mint_clip_media_forwards_the_hosts_options() -> None:
    """A host's configured TTS options are forwarded into the render call."""
    renderer = DummyRenderer()
    renderer._hosts = {
        "rick": {
            "id": "rick",
            "tts_engine": "",
            "options": {"voice": "en_US-lessac-medium", "length_scale": 1.2},
        }
    }
    cast("Any", renderer).mass = SimpleNamespace(metadata=SimpleNamespace(locale="en_US"))
    _attach_normalization(renderer)
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "rick"})

    await renderer._mint_clip_media(item, "hello world", "clip_1")

    assert renderer.tts_options == [{"voice": "en_US-lessac-medium", "length_scale": 1.2}]


async def test_mint_clip_media_sends_no_options_for_a_host_without_any() -> None:
    """A host with no configured options forwards an empty dict, not None."""
    renderer = DummyRenderer()
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": ""}}
    cast("Any", renderer).mass = SimpleNamespace(metadata=SimpleNamespace(locale="en_US"))
    _attach_normalization(renderer)
    item = _clip_item("sess_001", **{ATTR_HOST_ID: "rick"})

    await renderer._mint_clip_media(item, "hello world", "clip_1")

    assert renderer.tts_options == [{}]


async def test_render_tts_media_forwards_the_hosts_tts_options() -> None:
    """A host's configured TTS options reach the engine's get_tts_message call."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")
    renderer._hosts = {
        "rick": {
            "id": "rick",
            "tts_engine": "",
            "options": {"voice": "en_US-lessac-medium", "length_scale": 1.2},
        }
    }
    _attach_queue(renderer, [_clip_item("sess_001", **{ATTR_HOST_ID: "rick"})])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message.assert_awaited_once_with(
        "Good evening, it is warm out.",
        language="en-US",
        engine_id="tts.cloud",
        options={"voice": "en_US-lessac-medium", "length_scale": 1.2},
    )


async def test_render_tts_media_streams_a_url_over_http() -> None:
    """A TTS engine returning a proxy URL yields an HTTP stream with the MP3 default format."""
    renderer = _tts_renderer("http://example.test/api/tts_proxy/abc123.mp3")

    path, stream_type, audio_format = await renderer._render_tts_media("hello world")

    assert path == "http://example.test/api/tts_proxy/abc123.mp3"
    assert stream_type == StreamType.HTTP
    assert audio_format.content_type == ContentType.MP3
    engine = cast("Any", renderer)._get_tts_engine.return_value
    # the provider-scoped engine.id, never engine.uid, and never omitted
    engine.provider.get_tts_message.assert_awaited_once_with(
        "hello world", language=None, engine_id="tts.cloud", options=None
    )


async def test_render_tts_media_streams_a_local_file_from_disk(tmp_path: Path) -> None:
    """A TTS engine that renders to disk is played as a local file rather than fetched."""
    clip = tmp_path / "section.mp3"
    clip.write_bytes(b"")
    renderer = _tts_renderer(str(clip))
    _attach_queue(renderer, [_clip_item("sess_001")])

    path, stream_type, _ = await renderer._render_tts_media("hello world")

    assert path == str(clip)
    assert stream_type == StreamType.LOCAL_FILE


async def test_render_tts_media_keeps_a_declared_audio_format(tmp_path: Path) -> None:
    """A TTS engine that declares its own format has it carried into the clip streamdetails."""
    clip = tmp_path / "section.wav"
    clip.write_bytes(b"")
    renderer = _tts_renderer(str(clip), AudioFormat(content_type=ContentType.WAV))

    _, _, audio_format = await renderer._render_tts_media("hello world")

    assert audio_format.content_type == ContentType.WAV


@pytest.mark.parametrize("path", ["", "section.mp3", "/does/not/exist.mp3"])
async def test_render_tts_media_rejects_an_unplayable_path(path: str) -> None:
    """A path that is neither a URL nor an existing file fails loudly instead of degrading."""
    renderer = _tts_renderer(path)

    with pytest.raises(InvalidDataError, match="unusable stream path"):
        await renderer._render_tts_media("hello world")


async def test_render_tts_media_gives_up_on_a_stalled_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled TTS engine fails the clip instead of pinning the render path."""
    monkeypatch.setattr("music_assistant.helpers.tts.TTS_QUERY_TIMEOUT_SECONDS", 0.01)

    async def _answers_too_late(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        await asyncio.sleep(5)
        return SimpleNamespace(
            path="http://example.test/late.mp3",
            audio_format=AudioFormat(content_type=ContentType.MP3),
        )

    renderer = _tts_renderer("http://example.test/late.mp3")
    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message = AsyncMock(side_effect=_answers_too_late)

    with pytest.raises(MusicAssistantError, match="did not respond within"):
        await renderer._render_tts_media("hello world")


async def test_render_tts_media_reports_an_engine_side_timeout_as_is() -> None:
    """A timeout raised by the TTS engine itself is not reported as our own cap."""
    renderer = _tts_renderer("http://example.test/late.mp3")
    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message = AsyncMock(side_effect=TimeoutError)

    with pytest.raises(TimeoutError) as error:
        await renderer._render_tts_media("hello world")
    assert "did not respond within" not in str(error.value)


async def test_local_file_clip_yields_local_file_streamdetails(tmp_path: Path) -> None:
    """A disk-rendered clip reaches the core as LOCAL_FILE streamdetails."""
    clip = tmp_path / "section.mp3"
    clip.write_bytes(b"")
    renderer = _tts_renderer(str(clip))
    _attach_queue(renderer, [_clip_item("sess_001")])

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.stream_type == StreamType.LOCAL_FILE
    assert streamdetails.path == str(clip)


async def test_a_measured_clip_is_lifted_to_the_target_plus_the_boost() -> None:
    """A clip quieter than the levelled music is served through the provider's own chain."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer, target=-14, boost=3)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.stream_type == StreamType.CUSTOM
    assert streamdetails.decoded_audio_format == TTS_CLIP_PCM_FORMAT
    assert streamdetails.audio_format.content_type == ContentType.MP3
    assert streamdetails.data.gain_db == pytest.approx(7.0)


async def test_the_clip_is_evened_out_and_lifted_before_it_is_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter chain reaches ffmpeg as speechnorm, then gain, then the peak limiter."""
    captured: dict[str, Any] = {}

    async def fake_ffmpeg_stream(**kwargs: Any) -> AsyncGenerator[bytes]:
        captured.update(kwargs)
        yield b"pcm"

    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.rendering.get_ffmpeg_stream", fake_ffmpeg_stream
    )
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer, target=-14, boost=3)
    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    chunks = [chunk async for chunk in renderer.get_audio_stream(streamdetails)]

    assert chunks == [b"pcm"]
    assert captured["audio_input"] == streamdetails.path
    assert captured["input_format"].content_type == ContentType.MP3
    assert captured["output_format"] == TTS_CLIP_PCM_FORMAT
    assert captured["filter_params"] == [
        TTS_SPEECHNORM_FILTER,
        "volume=7.0dB",
        f"alimiter=limit={TTS_PEAK_CEILING_DB}dB:level=false:latency=true",
    ]


async def test_the_engine_reference_is_measured_once_and_reused() -> None:
    """A second clip from the same voice levels itself against the stored measurement."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001"), _clip_item("sess_002")])
    _attach_normalization(renderer)

    first = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    second = await renderer.get_stream_details("sess_002", MediaType.SOUND_EFFECT)

    assert len(renderer.measure_calls) == 1
    assert first.data.gain_db == second.data.gain_db


async def test_a_short_clip_levels_itself_but_never_becomes_the_reference() -> None:
    """A clip of a few words is too thin a sample to speak for the rest of the engine."""

    class BriefRenderer(DummyRenderer):
        async def _probe_duration(self, path: str) -> int | None:
            return MIN_LOUDNESS_REFERENCE_SECONDS - 1

    renderer = BriefRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001"), _clip_item("sess_002")])
    _attach_normalization(renderer)

    first = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    await renderer.get_stream_details("sess_002", MediaType.SOUND_EFFECT)

    assert first.stream_type == StreamType.CUSTOM
    assert len(renderer.measure_calls) == 2
    assert cast("Any", renderer)._engine_loudness == {}


async def test_clip_plays_untouched_when_the_queue_does_not_normalize() -> None:
    """With the music unlevelled there is nothing to match, so the clip airs as rendered."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer, enabled=False)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.decoded_audio_format is None
    assert streamdetails.data is None


async def test_clip_plays_untouched_when_it_could_not_be_measured() -> None:
    """A failed measurement leaves the clip playable at its own level."""
    renderer = DummyRenderer()
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert renderer.measure_calls
    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.data is None


async def test_a_clip_above_the_wanted_level_is_trimmed_back_down() -> None:
    """The trim runs in either direction, so a loud voice is brought down to the target."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -8.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer, target=-14, boost=3)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.stream_type == StreamType.CUSTOM
    assert streamdetails.data.gain_db == pytest.approx(-3.0)


async def test_the_levelled_clip_does_not_hand_out_the_shared_pcm_format() -> None:
    """Core writes what ffmpeg reports onto this format, so it may not be the shared one."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.decoded_audio_format == TTS_CLIP_PCM_FORMAT
    assert streamdetails.decoded_audio_format is not TTS_CLIP_PCM_FORMAT


# verbatim ffmpeg 7.1 output, so the parsing this depends on is covered for real
FFMPEG_LOUDNORM_OUTPUT = b"""[Parsed_loudnorm_0 @ 0x93b41d440] \n{
\t"input_i" : "-18.37",
\t"input_tp" : "-1.89",
\t"input_lra" : "0.40",
\t"input_thresh" : "-27.86",
\t"normalization_type" : "dynamic"
}
"""


async def test_the_measurement_reads_the_level_out_of_ffmpegs_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine reference comes from loudnorm's report on the rendered clip."""
    captured: dict[str, Any] = {}

    async def fake_check_output(*args: str, **_kwargs: Any) -> tuple[int, bytes]:
        captured["args"] = args
        return 0, FFMPEG_LOUDNORM_OUTPUT

    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.rendering.check_output", fake_check_output
    )
    renderer = DummyRenderer()

    loudness = await AIRadioRenderMixin._measure_loudness(renderer, "http://ha.invalid/clip.mp3")

    assert loudness == -18.37
    assert "http://ha.invalid/clip.mp3" in captured["args"]
    # the reading has to come from behind speechnorm, or the gain corrects for a level
    # that never reaches it
    assert f"{TTS_SPEECHNORM_FILTER},loudnorm=print_format=json" in captured["args"]


async def test_a_failed_measurement_leaves_the_level_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ffmpeg run that did not succeed yields no reference rather than a wrong one."""

    async def fake_check_output(*_args: str, **_kwargs: Any) -> tuple[int, bytes]:
        return 1, b"ffmpeg: Invalid data found when processing input"

    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.rendering.check_output", fake_check_output
    )
    renderer = DummyRenderer()

    assert (
        await AIRadioRenderMixin._measure_loudness(renderer, "http://ha.invalid/clip.mp3") is None
    )


async def test_clip_plays_untouched_when_tracks_are_not_normalized() -> None:
    """The queue switch alone does not mean the music around the clip is levelled."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer, tracks_mode=VolumeNormalizationMode.DISABLED.value)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.stream_type == StreamType.HTTP
    assert streamdetails.data is None


async def test_no_measurement_is_taken_when_the_reading_has_nowhere_to_go() -> None:
    """Measuring costs a fetch and a decode, so a queue that will not use it is not charged."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    _attach_queue(renderer, [_clip_item("sess_001")])
    _attach_normalization(renderer, enabled=False)

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert renderer.measure_calls == []
