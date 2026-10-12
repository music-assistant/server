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
    QueueEmpty,
)
from music_assistant_models.media_items import (
    AudioFormat,
    ProviderMapping,
    SoundEffect,
    Track,
)
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails, TailOverlap

from music_assistant.constants import (
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
    CONF_VOLUME_NORMALIZATION_TARGET,
    CONF_VOLUME_NORMALIZATION_TRACKS,
)
from music_assistant.helpers.tags import AudioTags
from music_assistant.helpers.tts import TTSLanguageNotSupportedError
from music_assistant.models.plugin import PluginProvider, TTSEngine
from music_assistant.providers.ai_radio.constants import (
    ATTR_ALLOW_POST,
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_QUEUE_DJ,
    ATTR_RENDERED_TEXT,
    ATTR_SESSION_ID,
    ATTR_STATION_ID,
    ATTR_WEATHER_REQUIRED,
    ATTR_WEB_SEARCH_MODE,
    CLIP_STREAMDETAILS_EXPIRATION,
    CONF_TTS_ENGINE,
    CONF_TTS_LOUDNESS_BOOST,
    DEFAULT_LLM_INSTRUCTIONS,
    MIN_LOUDNESS_REFERENCE_SECONDS,
    NO_WEATHER_DATA_INSTRUCTION,
    TTS_CLIP_PCM_FORMAT,
    TTS_PEAK_CEILING_DB,
    TTS_SPEECHNORM_FILTER,
)
from music_assistant.providers.ai_radio.models import DJQueueState, RenderHealth
from music_assistant.providers.ai_radio.rendering import AIRadioRenderMixin

from .events import ProviderEventRecorder


class DummyRenderer(ProviderEventRecorder, AIRadioRenderMixin):
    """Minimal harness exposing the render path."""

    domain = "ai_radio"
    instance_id = "ai_radio--test"

    def __init__(self) -> None:
        """Initialize the harness with recording stubs."""
        self.logger = logging.getLogger("tests.ai_radio.rendering")
        self._hosts: dict[str, dict[str, Any]] = {}
        self._dj_queues: dict[str, DJQueueState] = {}
        self.paused: list[tuple[str, str]] = []
        self.llm_calls = 0
        self.llm_prompts: list[str] = []
        self.tts_texts: list[str] = []
        self.tts_options: list[dict[str, Any] | None] = []
        self.weather_calls = 0
        self.fail_generation = False
        self.fail_tts = False
        self.unavailable_tts_engines: set[str] = set()
        self.measure_calls: list[str] = []
        self.measured_loudness: float | None = None

    def get_setup_value(self, key: str, default: Any = None) -> Any:
        """Return the engine every setup value names."""
        return "engine-x"

    def _configured_now(self) -> Any:
        return __import__("datetime").datetime(2026, 7, 30, 18, 30)

    def _pause_queue_dj(self, queue_id: str, failed_item_id: str) -> None:
        """Record the queue whose DJ would pause instead of pausing it."""
        self.paused.append((queue_id, failed_item_id))

    async def _generate_text(
        self, instructions: str, prompt: str, web_mode: str, language: str | None = None
    ) -> str:
        # a real suspension point so concurrent callers actually interleave under
        # asyncio.gather, otherwise the lock in get_stream_details is never exercised
        await asyncio.sleep(0)
        self.llm_calls += 1
        if self.fail_generation:
            raise RuntimeError("llm down")
        self.llm_prompts.append(prompt)
        return "Good evening, it is warm out."

    async def _prepare_weather_tokens(self) -> dict[str, str]:
        self.weather_calls += 1
        return {"<weather_hourly>": f"fresh weather {self.weather_calls}"}

    async def _get_tts_engine(self, engine_uid: str | None = None) -> TTSEngine:
        """Return a stand-in for the first available engine: the host's, then the default."""
        for uid in (engine_uid, self.get_setup_value(CONF_TTS_ENGINE)):
            if uid and uid not in self.unavailable_tts_engines:
                engine = MagicMock(spec=TTSEngine)
                engine.uid = uid
                return engine
        raise MusicAssistantError("No text-to-speech engine available")

    async def _render_tts_media(
        self,
        text: str,
        engine: TTSEngine,
        language: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str, StreamType, AudioFormat]:
        self.tts_texts.append(text)
        self.tts_options.append(options)
        if self.fail_tts:
            raise RuntimeError("tts quota exceeded")
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
            TTSLanguageNotSupportedError(
                "TTS engine 'tts.cloud' does not support language 'en-US'"
            ),
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


async def test_clip_is_found_by_scanning_every_queue() -> None:
    """Every queue is a candidate, so the clip is located wherever it is persisted."""
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


async def test_generation_failure_logs_the_skip(caplog: pytest.LogCaptureFixture) -> None:
    """A clip that fails to generate is skipped and logged, without failing the run."""
    renderer = DummyRenderer()
    _attach_queue(renderer, [_clip_item("sess_001")])
    renderer.fail_generation = True

    with caplog.at_level(logging.WARNING), pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert any("failed to generate" in message for message in caplog.messages)


async def test_tts_failure_raises_media_not_found_and_logs_the_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TTS failure surfaces as missing media and is logged."""

    class UnspeakableRenderer(DummyRenderer):
        async def _render_tts_media(
            self,
            text: str,
            engine: TTSEngine,
            language: str | None = None,
            options: dict[str, Any] | None = None,
        ) -> tuple[str, StreamType, AudioFormat]:
            raise RuntimeError("tts down")

    renderer = UnspeakableRenderer()
    _attach_queue(renderer, [_clip_item("sess_001")])

    with caplog.at_level(logging.WARNING), pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert any("failed TTS" in message for message in caplog.messages)


class NoWeatherRenderer(DummyRenderer):
    """Renderer whose weather fetch always fails, leaving deferred weather tokens empty."""

    async def _prepare_weather_tokens(self) -> dict[str, str]:
        self.weather_calls += 1
        return {}


async def test_weather_required_clip_is_skipped_when_weather_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A weather-required clip with no forecast data is skipped instead of airing a guess."""
    renderer = NoWeatherRenderer()
    item = _clip_item("sess_001")
    item.extra_attributes[ATTR_WEATHER_REQUIRED] = True
    _attach_queue(renderer, [item])

    with caplog.at_level(logging.WARNING), pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert any("weather data unavailable" in message for message in caplog.messages)
    assert renderer.llm_prompts == []


async def test_non_weather_required_clip_renders_with_no_data_instruction() -> None:
    """A non-weather-required clip still airs, told to leave the forecast out rather than guess."""
    renderer = NoWeatherRenderer()
    item = _clip_item("sess_001")
    item.extra_attributes[ATTR_WEATHER_REQUIRED] = False
    _attach_queue(renderer, [item])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert NO_WEATHER_DATA_INSTRUCTION in renderer.llm_prompts[0]
    assert "<weather_hourly>" not in renderer.llm_prompts[0]


async def test_missing_weather_required_attribute_defaults_to_not_required() -> None:
    """An older queue item with no weather_required attribute must not be treated as required."""
    renderer = NoWeatherRenderer()
    item = _clip_item("sess_001")
    assert ATTR_WEATHER_REQUIRED not in item.extra_attributes
    _attach_queue(renderer, [item])

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert NO_WEATHER_DATA_INSTRUCTION in renderer.llm_prompts[0]


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
    _attach_queue(renderer, [_clip_item("sess_001")])
    return renderer


@pytest.mark.parametrize(
    "server_error",
    ["Server returned 5XX Server Error reply", "HTTP error 500 Internal Server Error"],
)
async def test_tts_server_error_fails_the_clip_with_an_actionable_message(
    server_error: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An engine that hands out a URL it cannot render fails the clip, not the playback."""
    renderer = _failing_probe(
        f"Unable to retrieve info for http://ha.invalid/api/tts_proxy/1.mp3 ({server_error})",
        monkeypatch,
    )

    with caplog.at_level(logging.WARNING), pytest.raises(MediaNotFoundError):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert "Check the logs of the TTS engine" in caplog.text
    # the hint is a guess, so the whole probe message travels with it - the url included,
    # since that is what tells a failing engine apart from a failing tts server behind it
    assert "http://ha.invalid/api/tts_proxy/1.mp3" in caplog.text
    assert server_error in caplog.text


async def test_unmeasurable_clip_still_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that only fails to measure the audio leaves the clip playable."""
    renderer = _failing_probe(
        "Unable to retrieve info for http://ha.invalid/api/tts_proxy/1.mp3 "
        "(Invalid or unsupported media file)",
        monkeypatch,
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.duration is None


def _dj_clip_item(session_id: str, index: int, **overrides: Any) -> QueueItem:
    """Build a queue item for a pending clip of the given queue DJ session, hosted by rick."""
    item = _clip_item(
        f"{session_id}_{index:03d}",
        **{ATTR_SESSION_ID: session_id, ATTR_HOST_ID: "rick", **overrides},
    )
    item.extra_attributes[ATTR_QUEUE_DJ] = True
    return item


def _failing_dj_renderer(clips: int, **overrides: Any) -> tuple[DummyRenderer, DJQueueState]:
    """Build a renderer whose TTS engine fails, with the given number of pending DJ clips."""
    renderer = DummyRenderer()
    renderer.fail_tts = True
    state = DJQueueState(queue_id="player_a", host_id="rick", dj_session_id="djlive")
    renderer._dj_queues = {"player_a": state}
    _attach_queue(
        renderer, [_dj_clip_item("djlive", index, **overrides) for index in range(1, clips + 1)]
    )
    return renderer, state


async def _fail_renders(renderer: DummyRenderer, *clip_ids: str) -> None:
    """Request each of the given clips, every one of them failing to render."""
    for clip_id in clip_ids:
        with pytest.raises(MediaNotFoundError):
            await renderer.get_stream_details(clip_id, MediaType.SOUND_EFFECT)


def _pause_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the messages of the warnings that announce paused breaks."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "pauses the breaks" in record.getMessage()
    ]


async def test_a_failed_clip_is_not_rendered_again() -> None:
    """The player's own request after a failed preload does not render the clip a second time."""
    renderer, state = _failing_dj_renderer(0)
    item = _dj_clip_item("djlive", 1)
    signals = _attach_queue(renderer, [item])

    await _fail_renders(renderer, "djlive_001", "djlive_001")

    assert renderer.llm_calls == 1
    assert len(renderer.tts_texts) == 1
    assert item.available is False
    assert state.render_health.failures == 1
    # one update stores the rendered script, the other publishes the clip as unplayable
    assert signals == [True, True]


async def test_a_request_waiting_behind_a_failed_render_does_not_render_again() -> None:
    """Two simultaneous requests for a clip that fails cost the engines a single attempt."""
    renderer, state = _failing_dj_renderer(1)

    results = await asyncio.gather(
        renderer.get_stream_details("djlive_001", MediaType.SOUND_EFFECT),
        renderer.get_stream_details("djlive_001", MediaType.SOUND_EFFECT),
        return_exceptions=True,
    )

    assert all(isinstance(result, MediaNotFoundError) for result in results)
    assert len(renderer.tts_texts) == 1
    assert state.render_health.failures == 1


@pytest.mark.parametrize(
    ("host_engine", "named_engine"),
    [("", "TTS engine 'engine-x'"), ("tts.rick_voice", "TTS engine 'tts.rick_voice'")],
)
async def test_three_failed_renders_in_a_row_pause_the_breaks(
    host_engine: str, named_engine: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A DJ stops calling the engines after three failures, pauses, and says so once."""
    renderer, state = _failing_dj_renderer(4, **{ATTR_HOST_ID: "rick"})
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": host_engine}}

    with caplog.at_level(logging.DEBUG, logger="tests.ai_radio.rendering"):
        await _fail_renders(renderer, "djlive_001", "djlive_002")
        assert renderer.paused == []
        await _fail_renders(renderer, "djlive_003")
        assert state.render_health.cooling_down(asyncio.get_running_loop().time())
        await _fail_renders(renderer, "djlive_004")

    assert renderer.llm_calls == 3
    assert len(renderer.tts_texts) == 3
    assert renderer.paused == [("player_a", "qi_djlive_003")]
    warnings = _pause_warnings(caplog)
    assert len(warnings) == 1
    assert named_engine in warnings[0]
    assert any(
        record.levelno == logging.DEBUG and "breaks are paused" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("unavailable", "named_engine"),
    [
        # the default stands in for the host's engine and is the one named
        ({"tts.rick_voice"}, "TTS engine 'engine-x'"),
        # nothing resolves, so the engine the host asked for is named
        ({"tts.rick_voice", "engine-x"}, "TTS engine 'tts.rick_voice'"),
    ],
)
async def test_the_pause_names_the_engine_that_resolved(
    unavailable: set[str], named_engine: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The pause names the engine that stood in for the host's, or the host's own when none did."""
    renderer, state = _failing_dj_renderer(3, **{ATTR_HOST_ID: "rick"})
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": "tts.rick_voice"}}
    renderer.unavailable_tts_engines = unavailable

    with caplog.at_level(logging.WARNING, logger="tests.ai_radio.rendering"):
        await _fail_renders(renderer, "djlive_001", "djlive_002", "djlive_003")

    assert state.render_health.cooling_down(asyncio.get_running_loop().time())
    warnings = _pause_warnings(caplog)
    assert len(warnings) == 1
    assert named_engine in warnings[0]


async def test_a_successful_render_resets_the_failure_count() -> None:
    """Failures only pause the breaks when they come in a row."""
    renderer, state = _failing_dj_renderer(5)

    await _fail_renders(renderer, "djlive_001", "djlive_002")
    renderer.fail_tts = False
    await renderer.get_stream_details("djlive_003", MediaType.SOUND_EFFECT)
    assert state.render_health.failures == 0
    renderer.fail_tts = True
    await _fail_renders(renderer, "djlive_004", "djlive_005")

    assert state.render_health.failures == 2
    assert not state.render_health.cooling_down(asyncio.get_running_loop().time())


async def test_ai_and_tts_failures_count_alike(caplog: pytest.LogCaptureFixture) -> None:
    """A failed AI query and a failed TTS call both count towards the pause."""
    renderer, state = _failing_dj_renderer(3)
    renderer.fail_tts = False
    renderer.fail_generation = True
    await _fail_renders(renderer, "djlive_001")
    renderer.fail_generation = False
    renderer.fail_tts = True
    await _fail_renders(renderer, "djlive_002")
    renderer.fail_tts = False
    renderer.fail_generation = True

    with caplog.at_level(logging.WARNING, logger="tests.ai_radio.rendering"):
        await _fail_renders(renderer, "djlive_003")

    assert state.render_health.cooling_down(asyncio.get_running_loop().time())
    warnings = _pause_warnings(caplog)
    assert len(warnings) == 1
    assert "AI engine 'engine-x'" in warnings[0]


async def test_a_failure_after_the_cool_down_restarts_it(caplog: pytest.LogCaptureFixture) -> None:
    """Once the cool-down is over one render is tried, and its failure pauses the breaks again."""
    renderer, state = _failing_dj_renderer(4)
    loop = asyncio.get_running_loop()

    with caplog.at_level(logging.WARNING, logger="tests.ai_radio.rendering"):
        await _fail_renders(renderer, "djlive_001", "djlive_002", "djlive_003")
        state.render_health.cooldown_until = loop.time() - 1
        await _fail_renders(renderer, "djlive_004")

    assert renderer.llm_calls == 4
    assert state.render_health.cooling_down(loop.time())
    assert len(_pause_warnings(caplog)) == 2
    assert [item_id for _queue_id, item_id in renderer.paused] == ["qi_djlive_003", "qi_djlive_004"]


def test_a_late_result_during_the_cool_down_leaves_it_alone() -> None:
    """A render that was in flight when the pause started neither extends nor ends the pause."""
    health = RenderHealth()
    assert [health.record_failure(100.0) for _ in range(3)] == [False, False, True]
    paused_until = health.cooldown_until

    assert health.record_failure(105.0) is False
    assert health.failures == 4
    assert health.cooldown_until == paused_until
    health.record_success(110.0)
    assert health.failures == 4
    assert health.cooldown_until == paused_until
    # once the pause is over the next render decides: a failure starts a new pause, a
    # success lifts it
    assert paused_until is not None
    assert health.record_failure(paused_until) is True
    restarted_until = health.cooldown_until
    assert restarted_until is not None
    assert restarted_until > paused_until
    health.record_success(restarted_until)
    assert health.failures == 0
    assert health.cooldown_until is None


async def test_a_success_after_the_cool_down_lifts_it() -> None:
    """Once the cool-down is over a render that succeeds lets the DJ render freely again."""
    renderer, state = _failing_dj_renderer(4)
    await _fail_renders(renderer, "djlive_001", "djlive_002", "djlive_003")
    state.render_health.cooldown_until = asyncio.get_running_loop().time() - 1
    renderer.fail_tts = False

    await renderer.get_stream_details("djlive_004", MediaType.SOUND_EFFECT)

    assert renderer.llm_calls == 4
    assert state.render_health.failures == 0
    assert state.render_health.cooldown_until is None


async def test_the_queue_update_of_the_pausing_failure_finds_the_dj_paused() -> None:
    """The DJ replan that the mark's queue update starts sees the cool-down already in place."""
    renderer, state = _failing_dj_renderer(3)
    loop = asyncio.get_running_loop()
    paused_at_update: list[bool] = []

    def _record_pause_state(*_args: Any, **_kwargs: Any) -> None:
        paused_at_update.append(state.render_health.cooling_down(loop.time()))

    cast("Any", renderer).mass.player_queues.signal_update = _record_pause_state

    await _fail_renders(renderer, "djlive_001", "djlive_002", "djlive_003")

    assert paused_at_update[-1] is True


async def test_a_clip_left_by_an_earlier_dj_session_counts_against_the_live_dj() -> None:
    """A clip persisted by the DJ session before a restart talks through the same engines."""
    renderer, state = _failing_dj_renderer(0)
    _attach_queue(renderer, [_dj_clip_item("djold", index) for index in range(1, 4)])

    await _fail_renders(renderer, "djold_001", "djold_002", "djold_003")

    assert state.render_health.cooling_down(asyncio.get_running_loop().time())
    assert renderer.paused == [("player_a", "qi_djold_003")]


async def test_a_clip_of_a_host_switched_away_mid_render_counts_nowhere() -> None:
    """A clip of the previous host, still rendering after a switch, leaves the new host alone."""
    renderer, state = _failing_dj_renderer(0)
    _attach_queue(
        renderer,
        [_dj_clip_item("djold", index, **{ATTR_HOST_ID: "daisy"}) for index in range(1, 4)],
    )

    await _fail_renders(renderer, "djold_001", "djold_002", "djold_003")

    assert state.render_health.failures == 0
    assert renderer.paused == []


async def test_a_clip_on_a_queue_without_a_dj_counts_nowhere() -> None:
    """A queue that runs no DJ has no failure bookkeeping, so its clips are never paused."""
    renderer, _state = _failing_dj_renderer(3)
    renderer._dj_queues = {}

    await _fail_renders(renderer, "djlive_001", "djlive_002", "djlive_003")

    assert renderer.llm_calls == 3
    assert renderer.paused == []


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


async def test_resolve_deferred_placeholders_timestamp_spells_out_weekday() -> None:
    """The <timestamp> value names the weekday so the LLM never has to derive it."""
    renderer = DummyRenderer()

    values = await renderer._resolve_deferred_placeholders("Just plain text, no tokens.")

    assert "Thursday" in values["<timestamp>"]
    assert "July" in values["<timestamp>"]


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
    engine = cast("Any", renderer)._get_tts_engine.return_value

    path, stream_type, audio_format = await renderer._render_tts_media("hello world", engine)

    assert path == "http://example.test/api/tts_proxy/abc123.mp3"
    assert stream_type == StreamType.HTTP
    assert audio_format.content_type == ContentType.MP3
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
    engine = cast("Any", renderer)._get_tts_engine.return_value

    path, stream_type, _ = await renderer._render_tts_media("hello world", engine)

    assert path == str(clip)
    assert stream_type == StreamType.LOCAL_FILE


async def test_render_tts_media_keeps_a_declared_audio_format(tmp_path: Path) -> None:
    """A TTS engine that declares its own format has it carried into the clip streamdetails."""
    clip = tmp_path / "section.wav"
    clip.write_bytes(b"")
    renderer = _tts_renderer(str(clip), AudioFormat(content_type=ContentType.WAV))
    engine = cast("Any", renderer)._get_tts_engine.return_value

    _, _, audio_format = await renderer._render_tts_media("hello world", engine)

    assert audio_format.content_type == ContentType.WAV


@pytest.mark.parametrize("path", ["", "section.mp3", "/does/not/exist.mp3"])
async def test_render_tts_media_rejects_an_unplayable_path(path: str) -> None:
    """A path that is neither a URL nor an existing file fails loudly instead of degrading."""
    renderer = _tts_renderer(path)
    engine = cast("Any", renderer)._get_tts_engine.return_value

    with pytest.raises(InvalidDataError, match="unusable stream path"):
        await renderer._render_tts_media("hello world", engine)


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
        await renderer._render_tts_media("hello world", engine)


async def test_render_tts_media_reports_an_engine_side_timeout_as_is() -> None:
    """A timeout raised by the TTS engine itself is not reported as our own cap."""
    renderer = _tts_renderer("http://example.test/late.mp3")
    engine = cast("Any", renderer)._get_tts_engine.return_value
    engine.provider.get_tts_message = AsyncMock(side_effect=TimeoutError)

    with pytest.raises(TimeoutError) as error:
        await renderer._render_tts_media("hello world", engine)
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


async def test_a_host_whose_engine_fell_back_shares_the_default_reference() -> None:
    """A host whose engine fell back to the default levels against the default's measurement."""
    renderer = DummyRenderer()
    renderer.measured_loudness = -18.0
    renderer._hosts = {"rick": {"id": "rick", "tts_engine": "tts.rick_voice"}}
    renderer.unavailable_tts_engines = {"tts.rick_voice"}
    _attach_queue(
        renderer, [_clip_item("sess_001"), _clip_item("sess_002", **{ATTR_HOST_ID: "rick"})]
    )
    _attach_normalization(renderer)

    await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    await renderer.get_stream_details("sess_002", MediaType.SOUND_EFFECT)

    assert len(renderer.measure_calls) == 1


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


def _track_item(queue_item_id: str = "qi_track", lrc_lyrics: str | None = None) -> QueueItem:
    """Build a queue item for the record that follows a clip."""
    track = Track(
        item_id="lib_1",
        provider="library",
        name="Song",
        provider_mappings={
            ProviderMapping(item_id="tidal_1", provider_domain="tidal", provider_instance="tidal")
        },
    )
    track.metadata.lrc_lyrics = lrc_lyrics
    return QueueItem(
        queue_id="player_a",
        queue_item_id=queue_item_id,
        name="Song",
        duration=200,
        media_item=track,
    )


def _posting_clip_item() -> QueueItem:
    """Build a pending clip from a section that lets its break carry over."""
    item = _clip_item("sess_001")
    item.extra_attributes[ATTR_ALLOW_POST] = True
    return item


def _resolved(provider: str, item_id: str) -> StreamDetails:
    """Build the stream details of the copy a track resolves to."""
    return StreamDetails(
        provider=provider,
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.FLAC),
    )


def _attach_post(
    renderer: DummyRenderer,
    next_item: QueueItem | None,
    *,
    analysis_onset: float | None = None,
    lyrics: tuple[str | None, str | None] = (None, None),
    flow_mode: bool = True,
    flow_mode_required: bool = False,
    boundary_mix_required: bool = False,
    mixes_item_boundaries: bool = False,
    load_next: QueueItem | Exception | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    """
    Wire the next queue item, how the queue mixes it, its stored analysis and its lyrics.

    The queue's load of the next item yields ``load_next``, or raises it; by default it
    is the next item itself, resolved to its own provider mapping.
    """
    mass = cast("Any", renderer).mass
    mass.player_queues.get_next_item = lambda _queue_id, _item_id: next_item

    async def load_next_queue_item(_queue_id: str, _item_id: str) -> QueueItem:
        if isinstance(load_next, Exception):
            raise load_next
        loaded = load_next or next_item
        assert loaded is not None
        if loaded.streamdetails is None:
            mapping = next(iter(cast("Track", loaded.media_item).provider_mappings))
            loaded.streamdetails = _resolved(mapping.provider_instance, mapping.item_id)
        return loaded

    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=load_next_queue_item)
    mass.player_queues.get = lambda queue_id: SimpleNamespace(
        queue_id=queue_id, flow_mode=flow_mode
    )
    mass.streams.flow_mode_required = MagicMock(return_value=flow_mode_required)
    mass.streams.boundary_mix_required = MagicMock(return_value=boundary_mix_required)
    mass.streams.mixes_item_boundaries = MagicMock(return_value=mixes_item_boundaries)
    get_vocal_onset = AsyncMock(return_value=analysis_onset)
    mass.streams.audio_analysis = SimpleNamespace(get_vocal_onset=get_vocal_onset)
    get_track_lyrics = AsyncMock(return_value=lyrics)
    mass.metadata.get_track_lyrics = get_track_lyrics
    return get_vocal_onset, get_track_lyrics


async def test_a_posting_clip_declares_its_overlap_from_the_stored_analysis() -> None:
    """The analysed vocal onset sets how far the break carries over the next track."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    get_vocal_onset, get_track_lyrics = _attach_post(renderer, _track_item(), analysis_onset=6.0)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=5.6, next_queue_item_id="qi_track")
    load_next_queue_item = cast("Any", renderer).mass.player_queues.load_next_queue_item
    load_next_queue_item.assert_awaited_once_with("player_a", "qi_sess_001")
    get_vocal_onset.assert_awaited_once_with("tidal_1", "tidal")
    get_track_lyrics.assert_not_awaited()


async def test_the_analysis_is_read_for_the_copy_that_streams() -> None:
    """The queue's load keeps valid streamdetails, and they name the copy whose analysis is read."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    next_item = _track_item()
    next_item.streamdetails = _resolved("qobuz", "qobuz_1")
    get_vocal_onset, _ = _attach_post(renderer, next_item, analysis_onset=4.0)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap is not None
    assert streamdetails.tail_overlap.duration == 3.6
    get_vocal_onset.assert_awaited_once_with("qobuz_1", "qobuz")
    load_next_queue_item = cast("Any", renderer).mass.player_queues.load_next_queue_item
    load_next_queue_item.assert_awaited_once_with("player_a", "qi_sess_001")


async def test_the_queue_resolves_the_next_track_before_its_analysis_is_read() -> None:
    """A track without streamdetails is loaded first, so its mappings never pick the copy."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    resolved = _track_item()
    resolved.streamdetails = _resolved("qobuz", "qobuz_1")
    get_vocal_onset, _ = _attach_post(
        renderer, _track_item(), analysis_onset=4.0, load_next=resolved
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=3.6, next_queue_item_id="qi_track")
    get_vocal_onset.assert_awaited_once_with("qobuz_1", "qobuz")


async def test_the_post_follows_the_track_the_queue_loads_past_an_unplayable_one() -> None:
    """When the queue moves past a track it cannot play, the post targets the one it loaded."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    get_vocal_onset, _ = _attach_post(
        renderer, _track_item(), analysis_onset=6.0, load_next=_track_item("qi_track_2")
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=5.6, next_queue_item_id="qi_track_2")
    get_vocal_onset.assert_awaited_once_with("tidal_1", "tidal")


async def test_a_post_is_skipped_when_the_next_track_cannot_be_loaded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A next track the queue cannot load gets no post, and no lyrics are looked up for it."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    get_vocal_onset, get_track_lyrics = _attach_post(
        renderer,
        _track_item(),
        analysis_onset=6.0,
        load_next=QueueEmpty("No more (playable) tracks left in the queue."),
    )

    with caplog.at_level(logging.INFO, logger="tests.ai_radio.rendering"):
        streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap is None
    get_vocal_onset.assert_not_awaited()
    get_track_lyrics.assert_not_awaited()
    assert any("the next track could not be loaded" in r.getMessage() for r in caplog.records)


async def test_a_post_is_skipped_when_loading_the_next_track_takes_too_long(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A load that outlasts its budget is given up on, so the clip can still air on time."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    get_vocal_onset, get_track_lyrics = _attach_post(renderer, _track_item(), analysis_onset=6.0)

    async def _slow_load(_queue_id: str, _item_id: str) -> QueueItem:
        await asyncio.sleep(5)
        raise AssertionError("the load should have been given up on")

    mass = cast("Any", renderer).mass
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=_slow_load)
    monkeypatch.setattr("music_assistant.providers.ai_radio.rendering.POST_LOAD_TIMEOUT", 0.01)

    with caplog.at_level(logging.INFO, logger="tests.ai_radio.rendering"):
        streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap is None
    get_vocal_onset.assert_not_awaited()
    get_track_lyrics.assert_not_awaited()
    assert any("loading the next track took longer" in r.getMessage() for r in caplog.records)


async def test_a_post_is_skipped_when_the_item_that_plays_next_is_not_a_track(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the queue moves past an unplayable track onto another clip, nothing is posted."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    get_vocal_onset, _ = _attach_post(
        renderer, _track_item(), analysis_onset=6.0, load_next=_clip_item("sess_002")
    )

    with caplog.at_level(logging.INFO, logger="tests.ai_radio.rendering"):
        streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap is None
    get_vocal_onset.assert_not_awaited()
    assert any("is not a track" in r.getMessage() for r in caplog.records)


async def test_synced_lyrics_time_the_post_when_the_analysis_has_nothing() -> None:
    """Without an analysed onset, the first sung lyric line times the post."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _, get_track_lyrics = _attach_post(
        renderer, _track_item(), lyrics=(None, "[00:04.00]Hello there")
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=3.6, next_queue_item_id="qi_track")
    get_track_lyrics.assert_awaited_once()


async def test_a_repeated_resolve_does_not_look_the_lyrics_up_again() -> None:
    """Every path that resolves the clip gets the same post without a second lookup."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _, get_track_lyrics = _attach_post(
        renderer, _track_item(), lyrics=(None, "[00:04.00]Hello there")
    )

    first = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    second = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert first.tail_overlap == second.tail_overlap
    get_track_lyrics.assert_awaited_once()


async def test_a_post_is_skipped_when_the_player_neither_plays_gapless_nor_streams_in_flow_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing would mix the overlap on such a player, so the onset is never looked up."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    get_vocal_onset, get_track_lyrics = _attach_post(
        renderer, _track_item(), analysis_onset=6.0, flow_mode=False
    )

    with caplog.at_level(logging.INFO, logger="tests.ai_radio.rendering"):
        first = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
        second = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert first.tail_overlap is None
    assert second.tail_overlap is None
    cast("Any", renderer).mass.player_queues.load_next_queue_item.assert_not_awaited()
    get_vocal_onset.assert_not_awaited()
    get_track_lyrics.assert_not_awaited()
    skips = [
        r
        for r in caplog.records
        if "neither plays gapless nor streams in flow mode" in r.getMessage()
    ]
    assert len(skips) == 1


async def test_a_missing_next_track_is_logged_once_until_the_queue_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated resolves of a clip with nothing after it report the skip only once."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _attach_post(renderer, None)

    with caplog.at_level(logging.INFO, logger="tests.ai_radio.rendering"):
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
        await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    skips = [r for r in caplog.records if "no next track in the queue" in r.getMessage()]
    assert len(skips) == 1


async def test_a_show_that_holds_the_queue_in_flow_mode_posts_before_it_streams() -> None:
    """A queue not yet streaming in flow mode posts when a show requires flow mode for it."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _attach_post(
        renderer, _track_item(), analysis_onset=6.0, flow_mode=False, flow_mode_required=True
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=5.6, next_queue_item_id="qi_track")
    cast("Any", renderer).mass.streams.flow_mode_required.assert_called_once_with("player_a")


async def test_a_post_is_armed_on_a_gapless_player_outside_flow_mode() -> None:
    """A player that plays gapless gets the overlap mixed between its item streams."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _attach_post(
        renderer, _track_item(), analysis_onset=6.0, flow_mode=False, mixes_item_boundaries=True
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=5.6, next_queue_item_id="qi_track")
    cast("Any", renderer).mass.streams.mixes_item_boundaries.assert_called_once_with("player_a")


async def test_a_post_is_armed_when_the_show_requires_mixed_boundaries() -> None:
    """A show's requirement settles the player's side, whatever its protocol looks like now."""
    renderer = DummyRenderer()
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _attach_post(
        renderer, _track_item(), analysis_onset=6.0, flow_mode=False, boundary_mix_required=True
    )

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap == TailOverlap(duration=5.6, next_queue_item_id="qi_track")
    cast("Any", renderer).mass.streams.mixes_item_boundaries.assert_not_called()


@pytest.mark.parametrize(
    ("next_item", "analysis_onset", "lyrics", "clip_seconds"),
    [
        # no next track
        (None, None, (None, None), 9),
        # no vocal onset anywhere
        (_track_item(), None, ("plain words", None), 9),
        # the vocal enters too early for the intro to carry anything
        (_track_item(), 1.5, (None, None), 9),
        # the break is too short to keep a head and still carry over
        (_track_item(), 6.0, (None, None), 2),
    ],
)
async def test_a_post_is_skipped_when_it_cannot_carry_over(
    next_item: QueueItem | None,
    analysis_onset: float | None,
    lyrics: tuple[str | None, str | None],
    clip_seconds: int,
) -> None:
    """A clip that cannot carry over the next track declares no overlap."""
    renderer = DummyRenderer()
    cast("Any", renderer)._probe_duration = AsyncMock(return_value=clip_seconds)
    clip = _posting_clip_item()
    _attach_queue(renderer, [clip])
    _attach_post(renderer, next_item, analysis_onset=analysis_onset, lyrics=lyrics)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap is None


async def test_a_clip_from_a_section_without_posts_never_declares_one() -> None:
    """Only sections that opted in look at the next track at all."""
    renderer = DummyRenderer()
    clip = _clip_item("sess_001")
    _attach_queue(renderer, [clip])
    get_vocal_onset, _ = _attach_post(renderer, _track_item(), analysis_onset=6.0)

    streamdetails = await renderer.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert streamdetails.tail_overlap is None
    get_vocal_onset.assert_not_awaited()


async def test_a_posting_clip_is_served_whole() -> None:
    """The declaration leaves the clip's own length and stream untouched."""
    plain = DummyRenderer()
    plain.measured_loudness = -17.0
    _attach_queue(plain, [_clip_item("sess_001")])
    posting = DummyRenderer()
    posting.measured_loudness = -17.0
    _attach_queue(posting, [_posting_clip_item()])
    _attach_post(posting, _track_item(), analysis_onset=6.0)

    without = await plain.get_stream_details("sess_001", MediaType.SOUND_EFFECT)
    with_post = await posting.get_stream_details("sess_001", MediaType.SOUND_EFFECT)

    assert with_post.tail_overlap is not None
    assert with_post.duration == without.duration == 9
    assert with_post.stream_type == without.stream_type == StreamType.CUSTOM
    assert with_post.path == without.path
