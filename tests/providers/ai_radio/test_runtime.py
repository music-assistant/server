"""Unit tests for AI Radio runtime session flow and logging."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.helpers.datetime import now as host_now
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine
from music_assistant.providers.ai_radio import runtime as runtime_module
from music_assistant.providers.ai_radio.constants import (
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_SESSION_ID,
    ATTR_STATION_ID,
    ATTR_WEB_SEARCH_MODE,
    CONF_AI_ENGINE,
    CONF_TTS_ENGINE,
    CONF_WEATHER_PROVIDER,
    TTS_PRONUNCIATION_INSTRUCTIONS,
)
from music_assistant.providers.ai_radio.models import PlannedSection, SessionState, Slot
from music_assistant.providers.ai_radio.queue_dj import AIRadioQueueDJMixin
from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class StubConfig:
    """Minimal ProviderConfig stand-in exposing get_value."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        """Initialize with an optional map of config key -> value."""
        self._values = values or {}

    def get_value(self, key: str, default: Any = None) -> Any:
        """Return the stubbed value for key, or default when absent."""
        return self._values.get(key, default)


class DummyRuntime(AIRadioRuntimeMixin):
    """Minimal runtime harness for testing mixin behavior."""

    def __init__(self, setup_values: dict[str, Any] | None = None) -> None:
        """Initialize minimal state for runtime tests."""
        self.logger = logging.getLogger("tests.ai_radio.runtime")
        self._sessions: dict[str, SessionState] = {}
        self._sections: dict[str, dict[str, Any]] = {}
        self.config = cast("Any", StubConfig())
        self.instance_id = "ai_radio_test"
        self.domain = "ai_radio"
        self._setup_values = setup_values or {}

    def get_setup_value(self, key: str, default: Any = None) -> Any:
        """Return the stubbed setup flow value for key, or default when absent."""
        return self._setup_values.get(key, default)

    def _schedule_replan(self, queue_id: str) -> None:
        """No-op stand-in for the queue DJ mixin's replan scheduling."""

    async def set_queue_dj(self, queue_id: str, host_id: str | None) -> dict[str, str]:
        """No-op stand-in for the queue DJ mixin's set_queue_dj."""
        return {}

    def _materialize_sections(
        self, section_ids: list[str], sections_map: dict[str, dict[str, Any]] | None = None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve section ids against self._sections, mirroring the storage mixin."""
        source = self._sections if sections_map is None else sections_map
        sections: list[dict[str, Any]] = []
        missing: list[str] = []
        for section_id in section_ids:
            section = source.get(section_id)
            if section is None:
                missing.append(section_id)
                continue
            sections.append(deepcopy(section))
        return sections, missing


class FailingRuntime(DummyRuntime):
    """Runtime harness that forces show execution failure."""

    async def _run_show(
        self,
        session: SessionState,
        station: dict[str, Any],
    ) -> dict[str, Any]:
        """Raise to test failed-session behavior."""
        raise RuntimeError("boom")


def _set_runtime_mass(runtime: AIRadioRuntimeMixin, mass: Any) -> None:
    """Attach lightweight test mass object while bypassing strict runtime typing."""
    cast("Any", runtime).mass = mass


def _create_ai_plugin(instance_id: str, *engine_ids: str) -> MagicMock:
    """Create a mock plugin provider exposing the given AI engines."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.get_ai_engines = AsyncMock(
        return_value=[
            AIEngine(id=engine_id, name=engine_id, provider=provider) for engine_id in engine_ids
        ]
    )
    return provider


def _create_tts_plugin(instance_id: str, *engine_ids: str) -> MagicMock:
    """Create a mock plugin provider exposing the given TTS engines."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = instance_id
    provider.get_tts_engines = AsyncMock(
        return_value=[
            TTSEngine(id=engine_id, name=engine_id, provider=provider) for engine_id in engine_ids
        ]
    )
    return provider


def _create_engine_mass(feature: ProviderFeature, *providers: Any, **attrs: Any) -> Any:
    """Create a lightweight mass stand-in serving the given plugins for one feature."""

    class DummyMass:
        def get_providers_supporting_feature(
            self,
            requested: ProviderFeature,
            priority: tuple[ProviderType, ...] = (),
        ) -> list[Any]:
            return list(providers) if requested == feature else []

    mass = DummyMass()
    for key, value in attrs.items():
        setattr(mass, key, value)
    return mass


async def test_run_session_sets_completed_and_logs(caplog: Any) -> None:
    """Complete a session and emit start/completion logs."""

    class SuccessfulRuntime(DummyRuntime):
        async def _run_show(
            self,
            session: SessionState,
            station: dict[str, Any],
        ) -> dict[str, Any]:
            """Return a successful show run result."""
            return {"ok": True}

    runtime = SuccessfulRuntime()
    session = SessionState(session_id="s1", station_id="station_a")
    runtime._sessions[session.session_id] = session

    with caplog.at_level(logging.INFO):
        await runtime._run_session(session.session_id, {"id": "station_a"})

    assert session.status == "completed"
    assert session.result == {"ok": True}
    assert any("AI Radio run started" in message for message in caplog.messages)
    assert any("AI Radio run completed" in message for message in caplog.messages)


async def test_run_session_sets_failed_state(caplog: Any) -> None:
    """Fail a session and keep the error message in state."""
    runtime = FailingRuntime()
    session = SessionState(session_id="s2", station_id="station_b")
    runtime._sessions[session.session_id] = session

    with caplog.at_level(logging.ERROR):
        await runtime._run_session(session.session_id, {"id": "station_b"})

    assert session.status == "failed"
    assert session.error == "boom"


async def test_run_session_sets_failed_state_with_empty_exception_message() -> None:
    """Store exception class name when failure has no message."""

    class EmptyError(Exception):
        """Exception with empty default message."""

    class EmptyFailingRuntime(DummyRuntime):
        async def _run_show(
            self,
            session: SessionState,
            station: dict[str, Any],
        ) -> dict[str, Any]:
            raise EmptyError

    runtime = EmptyFailingRuntime()
    session = SessionState(session_id="s2b", station_id="station_b")
    runtime._sessions[session.session_id] = session

    await runtime._run_session(session.session_id, {"id": "station_b"})

    assert session.status == "failed"
    assert session.error == "EmptyError"


async def test_run_session_sets_stopped_state_on_cancellation(caplog: Any) -> None:
    """Mark session as stopped when runtime execution is cancelled."""

    class CancelledRuntime(DummyRuntime):
        async def _run_show(
            self,
            session: SessionState,
            station: dict[str, Any],
        ) -> dict[str, Any]:
            raise asyncio.CancelledError

    runtime = CancelledRuntime()
    session = SessionState(session_id="s3", station_id="station_c")
    runtime._sessions[session.session_id] = session

    with caplog.at_level(logging.INFO), suppress(asyncio.CancelledError):
        await runtime._run_session(session.session_id, {"id": "station_c"})

    assert session.status == "stopped"
    assert any("AI Radio run cancelled" in message for message in caplog.messages)


async def test_run_session_reschedules_a_replan_for_the_session_queue() -> None:
    """Re-arm the queue DJ for the session's queue once its show session ends."""

    class ReplanTrackingRuntime(FailingRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.replanned_queue_ids: list[str] = []

        def _schedule_replan(self, queue_id: str) -> None:
            self.replanned_queue_ids.append(queue_id)

    runtime = ReplanTrackingRuntime()
    session = SessionState(session_id="s4", station_id="station_d")
    session.queue_id = "queue_1"
    runtime._sessions[session.session_id] = session

    await runtime._run_session(session.session_id, {"id": "station_d"})

    assert runtime.replanned_queue_ids == ["queue_1"]


async def test_prepare_runtime_tokens_logs_unsupported_weather_provider(caplog: Any) -> None:
    """Warn when weather placeholders are used with unsupported provider."""
    runtime = DummyRuntime()
    station = {
        "sections": [
            {
                "id": "Weather_Short",
                "type": "ai_text",
                "prompt": "Forecast: <weather_hourly>",
            }
        ],
        "section_order": [],
    }
    runtime.config = cast(
        "Any",
        StubConfig(
            {
                "weather_city": "Berlin",
                "weather_country": "DE",
                CONF_WEATHER_PROVIDER: "unsupported_provider",
            }
        ),
    )

    with caplog.at_level(logging.WARNING):
        tokens = await runtime._prepare_runtime_tokens(station)

    assert tokens == {}
    assert any("Unsupported weather provider" in message for message in caplog.messages)


def _weather_program() -> dict[str, Any]:
    """Return a program whose only section references the hourly weather token."""
    return {
        "sections": [
            {
                "id": "Weather_Short",
                "type": "ai_text",
                "prompt": "Forecast: <weather_hourly>",
            }
        ],
        "section_order": [],
    }


def _count_weather_fetches(runtime: DummyRuntime) -> list[str]:
    """Replace the forecast lookup with a stub and return the list it records into."""
    fetches: list[str] = []

    async def _fetch(city: str, **_kwargs: Any) -> tuple[str, str]:
        fetches.append(city)
        return "12 degrees", "mild"

    runtime._fetch_open_meteo_weather = _fetch  # type: ignore[method-assign, assignment]
    return fetches


async def test_prepare_runtime_tokens_reuses_the_cached_weather_within_the_ttl() -> None:
    """A second pass inside the cache window reuses the tokens instead of refetching."""
    runtime = DummyRuntime()
    runtime.config = cast("Any", StubConfig({"weather_city": "Berlin", "weather_country": "DE"}))
    fetches = _count_weather_fetches(runtime)

    first = await runtime._prepare_runtime_tokens(_weather_program())
    second = await runtime._prepare_runtime_tokens(_weather_program())

    assert first == {"<weather_hourly>": "12 degrees", "<weather_daily>": "mild"}
    assert second == first
    assert fetches == ["Berlin"]


async def test_prepare_runtime_tokens_refetches_the_weather_once_the_ttl_expired() -> None:
    """An expired cache entry is refetched rather than served stale forever."""
    runtime = DummyRuntime()
    runtime.config = cast("Any", StubConfig({"weather_city": "Berlin", "weather_country": "DE"}))
    fetches = _count_weather_fetches(runtime)

    await runtime._prepare_runtime_tokens(_weather_program())
    assert runtime._weather_tokens_cache is not None
    fetched_at, tokens = runtime._weather_tokens_cache
    runtime._weather_tokens_cache = (
        fetched_at - runtime_module.WEATHER_TOKENS_CACHE_SECONDS - 1,
        tokens,
    )
    await runtime._prepare_runtime_tokens(_weather_program())

    assert fetches == ["Berlin", "Berlin"]


def test_weather_strings_are_rounded_to_whole_numbers() -> None:
    """A host reads the forecast out loud, so it says 19 degrees and never 19.2."""
    runtime = DummyRuntime()
    payload = {
        "current": {
            "time": "2026-08-10T09:00",
            "temperature_2m": 19.2,
            "apparent_temperature": 18.7,
        },
        "hourly": {
            "time": ["2026-08-10T09:00", "2026-08-10T10:00"],
            "temperature_2m": [19.2, 20.6],
            "precipitation_probability": [12.4, 0],
        },
        "daily": {
            "time": ["2026-08-10"],
            "temperature_2m_min": [11.4],
            "temperature_2m_max": [21.49],
            "precipitation_probability_max": [30.6],
        },
    }

    hourly, daily = runtime._format_weather_strings(payload)

    assert hourly == (
        "now 19C (feels 19C); 2026-08-10 09:00: 19C, rain 12%; 2026-08-10 10:00: 21C, rain 0%"
    )
    assert daily == "2026-08-10: 11-21C, rain 31%"


async def test_prepare_runtime_tokens_ignores_missing_location(caplog: Any) -> None:
    """Skip weather preparation when the configured location is incomplete."""
    runtime = DummyRuntime()
    station = {
        "sections": [
            {
                "id": "Weather_Short",
                "type": "ai_text",
                "prompt": "Forecast: <weather_hourly>",
            }
        ],
        "section_order": [],
    }
    runtime.config = cast("Any", StubConfig({"weather_city": "", "weather_country": "DE"}))

    with caplog.at_level(logging.DEBUG):
        tokens = await runtime._prepare_runtime_tokens(station)

    assert tokens == {}
    assert any("no location configured" in message for message in caplog.messages)


def test_extract_location_reads_provider_config() -> None:
    """Weather location comes from the provider config, not the station."""
    runtime = DummyRuntime()
    runtime.config = cast("Any", StubConfig({"weather_city": "Berlin", "weather_country": "DE"}))

    assert runtime._extract_location() == ("Berlin", "DE")


def test_extract_location_defaults_to_empty_when_unset() -> None:
    """An unconfigured weather location resolves to empty strings, not an error."""
    runtime = DummyRuntime()

    assert runtime._extract_location() == ("", "")


@pytest.mark.parametrize("timezone_value", ["Asia/Tokyo", "  Asia/Tokyo  "])
def test_configured_now_uses_valid_configured_timezone(timezone_value: str) -> None:
    """A valid configured IANA timezone name is honored, surrounding whitespace included."""
    runtime = DummyRuntime()
    runtime.config = cast("Any", StubConfig({"timezone": timezone_value}))

    result = runtime._configured_now()

    assert str(result.tzinfo) == "Asia/Tokyo"


@pytest.mark.parametrize(
    "timezone_value",
    ["", "not-a-real-zone", "CEST", "../../etc/passwd"],
)
def test_configured_now_falls_back_when_timezone_blank_or_invalid(timezone_value: str) -> None:
    """A blank or invalid configured timezone falls back to the host local time."""
    runtime = DummyRuntime()
    runtime.config = cast("Any", StubConfig({"timezone": timezone_value}))

    result = runtime._configured_now()

    assert result.utcoffset() == host_now().utcoffset()


def test_plan_sections_ignores_invalid_optional_chance() -> None:
    """Treat non-numeric OPTIONAL chance values as zero during planning."""
    runtime = DummyRuntime()
    station = {
        "sections": [
            {
                "id": "Song_Transition",
                "name": "Song Transition",
                "type": "ai_text",
                "prompt": "Transition from <prev_songinfo> to <next_songinfo>",
            }
        ],
        "section_order": [
            {
                "when": "between_songs",
                "flow": [
                    {
                        "OPTIONAL": {
                            "section": "Song_Transition",
                            "chance": "not-a-number",
                        }
                    }
                ],
            }
        ],
        "general": {"timezone": "UTC"},
    }
    tracks = [
        {"name": "A", "artist": "Artist A", "songinfo": "Artist A - A", "duration": 180},
        {"name": "B", "artist": "Artist B", "songinfo": "Artist B - B", "duration": 180},
    ]

    planned, _history = runtime._plan_sections(
        session_id="sess",
        tracks=tracks,
        program=station,
        track_index_offset=0,
        minute_offset=0.0,
        history_state={},
        allowed_slot_when=["between_songs"],
        runtime_tokens={},
    )

    assert planned == []


async def test_generate_text_wraps_not_connected_error() -> None:
    """Raise an actionable MusicAssistantError when the AI engine is disconnected."""

    class NotConnected(Exception):
        """Match hass_client NotConnected exception name."""

    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(side_effect=NotConnected)
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="en_US")
        ),
    )

    with pytest.raises(MusicAssistantError) as error:
        await runtime._generate_text(
            instructions="test",
            prompt="test prompt",
            web_mode="disabled",
        )
    assert "not connected" in str(error.value).lower()
    assert "hass_1/ai_task.default" in str(error.value)


async def test_generate_text_fails_the_section_when_the_engine_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled AI engine fails the section instead of hanging the session."""
    monkeypatch.setattr("music_assistant.providers.ai_radio.runtime.AI_QUERY_TIMEOUT_SECONDS", 0.01)

    async def _answers_too_late(*_args: Any, **_kwargs: Any) -> str:
        await asyncio.sleep(5)
        return "section text"

    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(side_effect=_answers_too_late)
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="en_US")
        ),
    )

    with pytest.raises(MusicAssistantError) as error:
        await runtime._generate_text(
            instructions="test",
            prompt="test prompt",
            web_mode="disabled",
        )
    assert "did not respond within" in str(error.value)


async def test_generate_text_reports_an_engine_side_timeout_as_a_query_failure() -> None:
    """A timeout raised by the engine itself is reported as a query failure, not our cap."""
    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(side_effect=TimeoutError)
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="en_US")
        ),
    )

    with pytest.raises(MusicAssistantError) as error:
        await runtime._generate_text(
            instructions="test",
            prompt="test prompt",
            web_mode="disabled",
        )
    assert "query failed: TimeoutError" in str(error.value)


async def test_generate_text_asks_for_the_system_locale_language() -> None:
    """The AI query states the server locale so sections are written in that language."""
    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(return_value="section text")
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="nl_NL")
        ),
    )

    await runtime._generate_text(
        instructions="test",
        prompt="test prompt",
        web_mode="disabled",
    )

    assert "nl_NL" in plugin.ai_query.await_args.args[0]
    assert plugin.ai_query.await_args.kwargs == {"engine_id": "ai_task.default"}


async def test_generate_text_prefers_the_hosts_language_over_the_system_locale() -> None:
    """An explicit host language wins over the server locale in the AI query."""
    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(return_value="section text")
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="nl_NL")
        ),
    )

    await runtime._generate_text(
        instructions="test",
        prompt="test prompt",
        web_mode="disabled",
        language="fr_FR",
    )

    assert "fr_FR" in plugin.ai_query.await_args.args[0]
    assert "nl_NL" not in plugin.ai_query.await_args.args[0]


async def test_generate_text_falls_back_to_the_system_locale_when_language_is_empty() -> None:
    """An unset host language keeps asking for the server locale, exactly as before."""
    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(return_value="section text")
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="nl_NL")
        ),
    )

    await runtime._generate_text(
        instructions="test",
        prompt="test prompt",
        web_mode="disabled",
        language="",
    )

    assert "nl_NL" in plugin.ai_query.await_args.args[0]


@pytest.mark.parametrize("general", [{"instructions": "Host personality: minimal DJ."}, {}])
async def test_generate_text_always_states_the_pronunciation_rules(
    general: dict[str, Any],
) -> None:
    """Every query carries the TTS pronunciation rules, with or without station instructions."""
    plugin = _create_ai_plugin("hass_1", "ai_task.default")
    plugin.ai_query = AsyncMock(return_value="section text")
    runtime = DummyRuntime({CONF_AI_ENGINE: "hass_1/ai_task.default"})
    _set_runtime_mass(
        runtime,
        _create_engine_mass(
            ProviderFeature.AI_QUERY, plugin, metadata=SimpleNamespace(locale="en_US")
        ),
    )

    await runtime._generate_text(
        instructions=str(general.get("instructions", "")), prompt="test prompt", web_mode="allow"
    )

    assert TTS_PRONUNCIATION_INSTRUCTIONS in plugin.ai_query.await_args.args[0]


def test_resolve_placeholders_keeps_time_and_weather_deferred() -> None:
    """Static track placeholders resolve at plan time; time and weather stay deferred."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    tracks = [
        {"index": 0, "songinfo": "A - One", "duration": 200},
        {"index": 1, "songinfo": "B - Two", "duration": 200},
    ]
    slot = Slot(
        when="between_songs",
        at_index=1,
        prev_index=0,
        next_index=1,
        very_next_index=None,
        minute_mark=3.3,
    )

    static, deferred = runtime._resolve_placeholders(
        program={},
        tracks=tracks,
        slot=slot,
        runtime_tokens={"<weather_hourly>": "12 degrees"},
    )

    assert static["<prev_songinfo>"] == "A - One"
    assert static["<next_songinfo>"] == "B - Two"
    assert "<timestamp>" not in static
    assert "<weather_hourly>" not in static
    assert deferred["<weather_hourly>"] == "12 degrees"
    assert "<timestamp>" in deferred


def test_plan_sections_leaves_deferred_tokens_in_the_prompt() -> None:
    """A planned section's prompt keeps its deferred tokens verbatim."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    station = {
        "sections": [
            {
                "id": "Weather",
                "name": "Weather",
                "prompt": "It is <timestamp>. Weather: <weather_hourly>. Next: <next_songinfo>.",
                "constraints": {"max_chars": 300},
                "web_search": "disabled",
            }
        ],
        "section_order": [{"when": "between_songs", "flow": [{"MUST": "Weather"}]}],
    }
    tracks = [
        {"index": 0, "songinfo": "A - One", "duration": 200},
        {"index": 1, "songinfo": "B - Two", "duration": 200},
    ]

    planned, _history = runtime._plan_sections(
        session_id="sess",
        tracks=tracks,
        program=station,
        track_index_offset=0,
        minute_offset=0.0,
        history_state={},
        allowed_slot_when=["between_songs"],
        runtime_tokens={"<weather_hourly>": "12 degrees"},
    )

    assert planned
    prompt = planned[0].prompt
    assert "<timestamp>" in prompt
    assert "<weather_hourly>" in prompt
    assert "B - Two" in prompt


def _weather_guarded_station() -> dict[str, Any]:
    """Return a station whose only section requires the weather-hourly token to be present."""
    return {
        "sections": [
            {
                "id": "Weather",
                "name": "Weather",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "Current weather: <weather_hourly>.",
                "constraints": {"max_chars": 200},
            }
        ],
        "section_order": [
            {
                "when": "between_songs",
                "flow": [
                    {
                        "OPTIONAL": {
                            "section": "Weather",
                            "chance": 100,
                            "guards": {"require_placeholders_present": ["<weather_hourly>"]},
                        }
                    }
                ],
            }
        ],
    }


def test_plan_sections_suppresses_section_when_required_placeholder_is_missing() -> None:
    """A guarded section plans zero entries when its required placeholder never resolved."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    tracks = [
        {"index": 0, "songinfo": "A - One", "duration": 200},
        {"index": 1, "songinfo": "B - Two", "duration": 200},
    ]

    planned, _history = runtime._plan_sections(
        session_id="sess",
        tracks=tracks,
        program=_weather_guarded_station(),
        track_index_offset=0,
        minute_offset=0.0,
        history_state={},
        allowed_slot_when=["between_songs"],
        runtime_tokens={},
    )

    assert planned == []


def test_plan_sections_includes_section_when_required_placeholder_is_present() -> None:
    """The same guarded section plans normally once its required placeholder resolved."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    tracks = [
        {"index": 0, "songinfo": "A - One", "duration": 200},
        {"index": 1, "songinfo": "B - Two", "duration": 200},
    ]

    planned, _history = runtime._plan_sections(
        session_id="sess",
        tracks=tracks,
        program=_weather_guarded_station(),
        track_index_offset=0,
        minute_offset=0.0,
        history_state={},
        allowed_slot_when=["between_songs"],
        runtime_tokens={"<weather_hourly>": "12 degrees"},
    )

    assert len(planned) == 1
    assert planned[0].section_id == "Weather"


def _stub_track(item_id: str) -> Track:
    """Build a minimal Track with one available ProviderMapping, for build_queue_item."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="library",
                provider_instance="library",
            )
        },
    )


def test_compose_queue_items_places_clips_at_planned_indices() -> None:
    """Clips are interleaved at their planned indices, carrying their render state."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace())
    tracks = [
        {"index": 0, "uri": "library://track/1", "media_item": _stub_track("1")},
        {"index": 1, "uri": "library://track/2", "media_item": _stub_track("2")},
    ]
    sections = [
        PlannedSection(
            order=0,
            clip_id="sess_000",
            section_id="Intro",
            section_name="Intro",
            when="start_of_playlist",
            insert_at_index=0,
            prompt="hello <timestamp>",
            max_chars=200,
            web_search_mode="disabled",
        ),
        PlannedSection(
            order=1,
            clip_id="sess_001",
            section_id="Between",
            section_name="Between",
            when="between_songs",
            insert_at_index=1,
            prompt="middle <weather_hourly>",
            max_chars=200,
            web_search_mode="allow",
        ),
    ]

    items = runtime._compose_queue_items(
        queue_id="player_a",
        session=SessionState(session_id="sess", station_id="st"),
        program={"id": "st"},
        tracks=tracks,
        sections=sections,
    )

    assert [item.media_item.item_id for item in items if item.media_item is not None] == [
        "sess_000",
        "1",
        "sess_001",
        "2",
    ]
    intro = items[0]
    assert intro.media_item is not None
    assert intro.media_item.media_type == MediaType.SOUND_EFFECT
    assert intro.extra_attributes[ATTR_PROMPT] == "hello <timestamp>"
    assert intro.extra_attributes[ATTR_SESSION_ID] == "sess"
    assert intro.extra_attributes[ATTR_STATION_ID] == "st"
    assert intro.extra_attributes[ATTR_MAX_CHARS] == 200
    assert items[2].extra_attributes[ATTR_WEB_SEARCH_MODE] == "allow"
    # the section name travels as the item's own name, not as an attribute
    assert intro.name == "Intro"
    assert "ai_radio_section_name" not in intro.extra_attributes
    # track items carry no AI Radio state
    assert items[1].extra_attributes == {}


def test_build_program_merges_host_into_station() -> None:
    """The merged program carries the host's persona, sections and section_order."""
    runtime = DummyRuntime()
    runtime._sections = {
        "Song_Transition": {
            "id": "Song_Transition",
            "name": "Song Transition",
            "type": "ai_text",
            "prompt": "Prompt",
            "web_search": "disabled",
        }
    }
    host = {
        "id": "rick",
        "name": "Rick",
        "instructions": "Persona.",
        "tts_engine": "engine-1",
        "language": "fr_FR",
        "section_ids": ["Song_Transition"],
        "section_order": [{"when": "between_songs", "flow": [{"MUST": "Song_Transition"}]}],
        "merge_section_id": "",
    }
    station = {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "p1",
        "source_playlist_provider": "library",
        "default_player_id": "",
        "max_duration_minutes": 0.0,
        "shuffle_source_tracks": True,
        "host_id": "rick",
    }

    program = runtime._build_program(station, host)

    assert program["instructions"] == "Persona."
    assert program["tts_engine"] == "engine-1"
    assert program["language"] == "fr_FR"
    assert [s["id"] for s in program["sections"]] == ["Song_Transition"]
    assert program["section_order"] == host["section_order"]
    assert program["source_playlist_id"] == "p1"


def test_clip_item_carries_host_id() -> None:
    """A planned clip's queue item stamps both the station id and the host id."""
    runtime = DummyRuntime()
    section = PlannedSection(
        order=0,
        clip_id="sess_000",
        section_id="Song_Transition",
        section_name="Song Transition",
        when="between_songs",
        insert_at_index=1,
        prompt="p",
        max_chars=0,
        web_search_mode="disabled",
    )
    program = {"id": "station_a", "host_id": "rick"}

    item = runtime._section_to_clip_item("queue-1", "sess", program, section)

    assert item.extra_attributes[ATTR_HOST_ID] == "rick"
    assert item.extra_attributes[ATTR_SESSION_ID] == "sess"


async def test_get_ai_engine_requires_a_configured_selection() -> None:
    """Without a stored selection no engine is picked, so the run fails with a clear error."""
    runtime = DummyRuntime()
    _set_runtime_mass(
        runtime, _create_engine_mass(ProviderFeature.AI_QUERY, _create_ai_plugin("hass_1", "one"))
    )

    with pytest.raises(MusicAssistantError, match="No AI engine available"):
        await runtime._get_ai_engine()


async def test_get_ai_engine_uses_the_configured_selection() -> None:
    """A configured engine uid wins over the first available engine."""
    high_priority = _create_ai_plugin("zz_high", "engine")
    low_priority = _create_ai_plugin("aa_low", "engine")
    runtime = DummyRuntime({CONF_AI_ENGINE: "aa_low/engine"})
    _set_runtime_mass(
        runtime, _create_engine_mass(ProviderFeature.AI_QUERY, high_priority, low_priority)
    )

    assert (await runtime._get_ai_engine()).uid == "aa_low/engine"


async def test_get_ai_engine_refuses_a_configured_engine_that_disappeared() -> None:
    """A concrete AI selection is never silently replaced by another available engine."""
    runtime = DummyRuntime({CONF_AI_ENGINE: "gone/engine"})
    _set_runtime_mass(
        runtime, _create_engine_mass(ProviderFeature.AI_QUERY, _create_ai_plugin("hass_1", "one"))
    )

    with pytest.raises(MusicAssistantError, match="No AI engine available"):
        await runtime._get_ai_engine()


async def test_get_tts_engine_uses_the_configured_selection() -> None:
    """The stored TTS uid selects its engine, whatever order the plugins are served in."""
    high_priority = _create_tts_plugin("zz_high", "engine")
    low_priority = _create_tts_plugin("aa_low", "engine")
    runtime = DummyRuntime({CONF_TTS_ENGINE: "aa_low/engine"})
    _set_runtime_mass(
        runtime, _create_engine_mass(ProviderFeature.TTS, high_priority, low_priority)
    )

    assert (await runtime._get_tts_engine()).uid == "aa_low/engine"


async def test_get_tts_engine_refuses_a_configured_engine_that_disappeared() -> None:
    """A concrete TTS selection is never silently replaced by another available engine."""
    runtime = DummyRuntime({CONF_TTS_ENGINE: "gone/engine"})
    _set_runtime_mass(
        runtime, _create_engine_mass(ProviderFeature.TTS, _create_tts_plugin("hass_1", "one"))
    )

    with pytest.raises(MusicAssistantError, match="No text-to-speech engine available"):
        await runtime._get_tts_engine()


async def test_get_tts_engine_falls_back_to_provider_selection_when_host_uid_is_unresolvable(
    caplog: Any,
) -> None:
    """A host engine_uid that no longer resolves falls back to the provider's TTS selection."""
    runtime = DummyRuntime({CONF_TTS_ENGINE: "aa_low/engine"})
    _set_runtime_mass(
        runtime, _create_engine_mass(ProviderFeature.TTS, _create_tts_plugin("aa_low", "engine"))
    )

    with caplog.at_level(logging.WARNING):
        engine = await runtime._get_tts_engine("gone/engine")

    assert engine.uid == "aa_low/engine"
    assert any("unavailable" in message for message in caplog.messages)


def _show_mass_stub(**handlers: Any) -> SimpleNamespace:
    """
    Build a minimal mass stub for exercising _run_show.

    Any player_queues/players/music/metadata handler not passed gets a no-op default.
    """

    async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _noop_sync(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _noop_get_active_queue(_player_id: str) -> Any:
        return None

    def _noop_get_player(_player_id: str) -> Any:
        return object()

    def _noop_items(_queue_id: str, limit: int = 500, offset: int = 0) -> list[Any]:  # noqa: ARG001
        return []

    subscribers: list[Callable[[Any], None]] = []

    def _recording_subscribe(
        cb_func: Callable[[Any], None],
        event_filter: Any = None,  # noqa: ARG001
        id_filter: Any = None,  # noqa: ARG001
    ) -> Callable[[], None]:
        subscribers.append(cb_func)

        def _unsubscribe() -> None:
            subscribers.remove(cb_func)

        return _unsubscribe

    def _emit_queue_updated(queue_id: str) -> None:
        event = MassEvent(event=EventType.QUEUE_UPDATED, object_id=queue_id)
        for cb_func in subscribers:
            cb_func(event)

    def _emit_player_removed(player_id: str) -> None:
        event = MassEvent(event=EventType.PLAYER_REMOVED, object_id=player_id)
        for cb_func in subscribers:
            cb_func(event)

    player_queues = SimpleNamespace(
        clear=handlers.get("clear", _noop_sync),
        get=handlers.get("get", lambda _queue_id: None),
        get_active_queue=handlers.get("get_active_queue", _noop_get_active_queue),
        set_shuffle=handlers.get("set_shuffle", _noop_async),
        load=handlers.get("load", _noop_async),
        play_index=handlers.get("play_index", _noop_async),
        items=handlers.get("items", _noop_items),
        signal_update=handlers.get("signal_update", _noop_sync),
        stop=handlers.get("stop", _noop_async),
    )
    return SimpleNamespace(
        player_queues=player_queues,
        players=SimpleNamespace(get_player=handlers.get("get_player", _noop_get_player)),
        music=SimpleNamespace(playlists=handlers.get("playlists", SimpleNamespace())),
        metadata=SimpleNamespace(locale=handlers.get("locale", "en_US")),
        create_task=handlers.get("create_task", _noop_sync),
        subscribe=handlers.get("subscribe", _recording_subscribe),
        emit_queue_updated=_emit_queue_updated,
        emit_player_removed=_emit_player_removed,
    )


def _stub_queue(state: PlaybackState, current_index: int | None) -> SimpleNamespace:
    """Build a mutable queue stand-in exposing the fields _await_show_end reads."""
    return SimpleNamespace(state=state, current_index=current_index)


def _stub_clip_queue_item(clip_id: str, session_id: str) -> SimpleNamespace:
    """Build a queue item stand-in whose extra_attributes carry a session id."""
    return SimpleNamespace(extra_attributes={ATTR_SESSION_ID: session_id}, item_id=clip_id)


def _recording_set_shuffle(log: list[str]) -> Callable[[str, bool], Awaitable[None]]:
    """Return an async set_shuffle stub that appends "set_shuffle" to the given call-order log."""

    async def _set_shuffle(_queue_id: str, _shuffle_enabled: bool) -> None:
        log.append("set_shuffle")

    return _set_shuffle


def _show_station() -> dict[str, Any]:
    """Return a station config for _run_show tests, whose section_order yields clips."""
    return {
        "id": "st",
        "name": "Show Station",
        "default_player_id": "living_room",
        "source_playlist_id": "playlist-1",
        "source_playlist_provider": "library",
        "shuffle_source_tracks": False,
        "general": {"timezone": "UTC"},
        "sections": [
            {
                "id": "Song_Introduction_Start",
                "name": "Intro",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "Welcome, next up is <next_songinfo>.",
                "constraints": {"max_chars": 200},
            },
            {
                "id": "Song_Transition",
                "name": "Transition",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "From <prev_songinfo> to <next_songinfo>.",
                "constraints": {"max_chars": 200},
            },
        ],
        "section_order": [
            {"when": "start_of_playlist", "flow": [{"MUST": "Song_Introduction_Start"}]},
            {"when": "between_songs", "flow": [{"MUST": "Song_Transition"}]},
        ],
    }


class ShowRuntime(DummyRuntime):
    """Runtime harness exercising the real _run_show with stubbed track sourcing."""

    async def _fetch_source_tracks(
        self, station: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str]:
        """Return two fixed tracks, each carrying its resolved media item."""
        return [
            {"index": 0, "songinfo": "A - One", "duration": 200, "media_item": _stub_track("1")},
            {"index": 1, "songinfo": "B - Two", "duration": 200, "media_item": _stub_track("2")},
        ], "Source Playlist"

    async def _prepare_runtime_tokens(self, station: dict[str, Any]) -> dict[str, str]:
        """Skip the weather lookup; runtime tokens are irrelevant to these tests."""
        return {}


class ShowRuntimeWithDJ(AIRadioQueueDJMixin, AIRadioStorageMixin, ShowRuntime):
    """ShowRuntime harness that also carries sticky queue DJ state."""

    def __init__(self, tmp_path: Path) -> None:
        """Initialize show runtime state plus queue DJ bookkeeping."""
        super().__init__()
        self._hosts: dict[str, dict[str, Any]] = {
            "rick": {"id": "rick", "name": "Rick", "instructions": "x", "tts_engine": ""},
        }
        self._dj_queues: dict[str, Any] = {}
        self._dj_file = tmp_path / "queue_dj.json"
        self._dj_lock = asyncio.Lock()
        self._unloading = False


def _recording_create_task(scheduled: list[str]) -> Callable[..., None]:
    """Return a create_task stub that records the task id and discards the coroutine."""

    def _create_task(coro: Any, task_id: str | None = None, **_kwargs: Any) -> None:
        if task_id:
            scheduled.append(task_id)
        coro.close()

    return _create_task


async def test_run_show_loads_the_whole_show_then_plays_index_zero() -> None:
    """The show is loaded in one call, fully stamped, before playback is started."""
    runtime = ShowRuntime()
    call_order: list[str] = []
    loaded: list[tuple[Any, dict[str, Any]]] = []

    async def _load(_queue_id: str, queue_items: list[Any], **kwargs: Any) -> None:
        call_order.append("load")
        # snapshot media_item + extra_attributes now: asserting on the live queue_item
        # objects after _run_show returns would pass even if stamping happened later
        loaded.extend((item.media_item, dict(item.extra_attributes)) for item in queue_items)
        assert kwargs["shuffle"] is False
        assert kwargs["keep_remaining"] is False
        assert kwargs["keep_played"] is False

    async def _play_index(_queue_id: str, index: int) -> None:
        call_order.append(f"play_index:{index}")

    _set_runtime_mass(
        runtime,
        _show_mass_stub(
            load=_load,
            play_index=_play_index,
            clear=lambda _queue_id: call_order.append("clear"),
            set_shuffle=_recording_set_shuffle(call_order),
        ),
    )

    await runtime._run_show(SessionState(session_id="sess", station_id="st"), _show_station())

    assert call_order == ["clear", "set_shuffle", "load", "play_index:0"]
    clips = [
        (media_item, attrs)
        for media_item, attrs in loaded
        if media_item.media_type == MediaType.SOUND_EFFECT
    ]
    assert clips
    # every clip was already fully stamped at the moment load() was called
    assert all(attrs[ATTR_PROMPT] for _media_item, attrs in clips)
    assert all(attrs[ATTR_SESSION_ID] == "sess" for _media_item, attrs in clips)


async def test_run_show_targets_active_group_queue() -> None:
    """Queue and start the show on the active (group) queue when the player is grouped."""
    runtime = ShowRuntime()
    clear_calls: list[str] = []
    load_queue_ids: list[str] = []
    play_index_queue_ids: list[str] = []

    async def _load(queue_id: str, **_kwargs: Any) -> None:
        load_queue_ids.append(queue_id)

    async def _play_index(queue_id: str, _index: int) -> None:
        play_index_queue_ids.append(queue_id)

    _set_runtime_mass(
        runtime,
        _show_mass_stub(
            get_active_queue=lambda _player_id: SimpleNamespace(queue_id="group_1"),
            clear=clear_calls.append,
            load=_load,
            play_index=_play_index,
        ),
    )
    session = SessionState(session_id="s1", station_id="st")

    result = await runtime._run_show(session, _show_station())

    assert result["queue_id"] == "group_1"
    assert clear_calls == ["group_1"]
    assert load_queue_ids == ["group_1"]
    assert play_index_queue_ids == ["group_1"]
    assert session.queue_id == "group_1"


async def test_run_show_clears_the_queues_sticky_dj(tmp_path: Path) -> None:
    """Starting a show drops that queue's existing sticky DJ assignment."""
    runtime = ShowRuntimeWithDJ(tmp_path)
    _set_runtime_mass(runtime, _show_mass_stub())
    await runtime.set_queue_dj("living_room", "rick")
    assert "living_room" in runtime._dj_queues

    await runtime._run_show(SessionState(session_id="sess", station_id="st"), _show_station())

    assert "living_room" not in runtime._dj_queues
    persisted = json.loads(runtime._dj_file.read_text())
    assert persisted["queues"] == {}


async def test_run_show_clears_the_dj_on_the_resolved_group_queue(tmp_path: Path) -> None:
    """A grouped player's DJ is cleared on the active (group) queue, not the raw player id."""
    runtime = ShowRuntimeWithDJ(tmp_path)
    _set_runtime_mass(
        runtime,
        _show_mass_stub(get_active_queue=lambda _player_id: SimpleNamespace(queue_id="group_1")),
    )
    # a stale assignment on the raw player id must survive untouched: the show never
    # played there, only on the resolved group queue
    await runtime.set_queue_dj("living_room", "rick")
    await runtime.set_queue_dj("group_1", "rick")

    await runtime._run_show(SessionState(session_id="s1", station_id="st"), _show_station())

    assert "group_1" not in runtime._dj_queues
    assert "living_room" in runtime._dj_queues


async def test_run_session_finally_replans_a_dj_armed_mid_show(tmp_path: Path) -> None:
    """A DJ armed via the menu while a show plays still gets scheduled once the show ends."""
    runtime = ShowRuntimeWithDJ(tmp_path)
    scheduled: list[str] = []
    _set_runtime_mass(runtime, _show_mass_stub(create_task=_recording_create_task(scheduled)))
    session = SessionState(session_id="sess", station_id="st", queue_id="living_room")
    runtime._sessions[session.session_id] = session

    # arming mid-show already requested a pass; that pass would drain against the running-show
    # guard in _replan_queue and clear replan_pending without planning anything, so reset it
    # here to isolate the finally block's own request instead of piggybacking on this one
    await runtime.set_queue_dj("living_room", "rick")
    runtime._dj_queues["living_room"].replan_pending = False
    scheduled.clear()

    async def _run_show_stub(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("show over")

    runtime._run_show = _run_show_stub  # type: ignore[method-assign]

    await runtime._run_session(session.session_id, {"id": "st"})

    assert scheduled == ["ai_radio_dj_replan_living_room"]
    assert runtime._dj_queues["living_room"].ready is True


async def test_run_show_ends_as_stopped_when_the_user_stops_the_queue() -> None:
    """A queue stopped part-way through the show ends the run as a user stop."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=0)
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    await asyncio.sleep(0)
    assert not task.done()

    queue.state = PlaybackState.IDLE
    mass.emit_queue_updated("player_a")

    assert await asyncio.wait_for(task, timeout=1) == "queue_stopped"


async def test_run_show_ends_as_exhausted_when_the_show_plays_out() -> None:
    """Reaching the last enqueued entry ends the run as a normal completion."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=5)
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    await asyncio.sleep(0)

    queue.state = PlaybackState.IDLE
    mass.emit_queue_updated("player_a")

    assert await asyncio.wait_for(task, timeout=1) == "source_exhausted"


async def test_run_show_ignores_an_idle_queue_before_playback_starts() -> None:
    """A queue that has not started yet is not mistaken for a stopped one."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.IDLE, current_index=None)
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    mass.emit_queue_updated("player_a")
    await asyncio.sleep(0)

    assert not task.done()
    task.cancel()


async def test_run_show_keeps_a_paused_queue_on_air() -> None:
    """A paused queue keeps the show running."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=1)
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    await asyncio.sleep(0)

    queue.state = PlaybackState.PAUSED
    mass.emit_queue_updated("player_a")
    await asyncio.sleep(0)

    assert not task.done()
    task.cancel()


async def test_run_show_ends_when_the_queue_no_longer_holds_its_clips() -> None:
    """A queue cleared or taken over by other playback ends the run."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=1)
    queue_items = [_stub_clip_queue_item("sess_000", session_id="sess")]
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: queue_items[offset : offset + limit],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    await asyncio.sleep(0)

    queue_items.clear()
    mass.emit_queue_updated("player_a")

    assert await asyncio.wait_for(task, timeout=1) == "queue_stopped"


async def test_run_show_ends_when_its_player_is_removed() -> None:
    """Removing the target player must not pin the session's slot forever."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=1)
    queue_holder: list[Any] = [queue]
    mass = _show_mass_stub(
        get=lambda _queue_id: queue_holder[0],
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    await asyncio.sleep(0)
    assert not task.done()

    # on_player_remove pops the queue data before PLAYER_REMOVED is signaled
    queue_holder[0] = None
    mass.emit_player_removed("player_a")

    assert await asyncio.wait_for(task, timeout=1) == "queue_stopped"


async def test_run_show_keeps_waiting_when_the_show_has_no_clips_to_lose() -> None:
    """A clip-free show is not mistaken for one whose clips got cleared out."""
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=0)
    # track-only queue: no item carries ATTR_SESSION_ID, exactly like a show whose
    # section rules never selected anything to insert
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            SimpleNamespace(extra_attributes={})
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=False)
    )
    await asyncio.sleep(0)
    assert not task.done()

    queue.current_index = 5
    queue.state = PlaybackState.IDLE
    mass.emit_queue_updated("player_a")

    assert await asyncio.wait_for(task, timeout=1) == "source_exhausted"


async def test_await_show_end_fails_when_playback_never_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A show whose playback never starts is declared failed instead of waiting forever."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.SHOW_START_TIMEOUT_SECONDS", 0.05
    )
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.IDLE, current_index=None)
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    with pytest.raises(MusicAssistantError, match="did not start"):
        await runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)


async def test_await_show_end_does_not_time_out_once_playback_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playback starting before the start-timeout elapses lets the show proceed as normal."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.SHOW_START_TIMEOUT_SECONDS", 0.2
    )
    runtime = DummyRuntime()
    session = SessionState(session_id="sess", station_id="st")
    queue = _stub_queue(state=PlaybackState.IDLE, current_index=None)
    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="sess")
        ],
    )
    _set_runtime_mass(runtime, mass)

    task = asyncio.create_task(
        runtime._await_show_end(session, "player_a", last_index=5, has_clips=True)
    )
    await asyncio.sleep(0)
    assert not task.done()

    queue.state = PlaybackState.PLAYING
    queue.current_index = 0
    mass.emit_queue_updated("player_a")
    await asyncio.sleep(0)
    assert not task.done()

    queue.current_index = 5
    queue.state = PlaybackState.IDLE
    mass.emit_queue_updated("player_a")

    assert await asyncio.wait_for(task, timeout=1) == "source_exhausted"


def test_passes_optional_guards_handles_non_numeric_guard_values() -> None:
    """Treat non-numeric guard values as disabled instead of raising ValueError."""
    runtime = DummyRuntime()
    slot = Slot(
        when="between_songs",
        at_index=1,
        prev_index=0,
        next_index=1,
        very_next_index=2,
        minute_mark=5.0,
    )

    result = runtime._passes_optional_guards(
        section_id="Weather_Short",
        guards={"min_gap_songs": "abc", "max_per_60min": "xyz"},
        history={},
        slot=slot,
        tracks=[{}, {}, {}],
        placeholders={},
        track_index_offset=0,
        minute_offset=0.0,
    )

    assert result is True


async def test_fetch_source_tracks_skips_tracks_with_no_resolvable_uri(caplog: Any) -> None:
    """Skip and warn about source tracks with no resolvable uri instead of queuing a dead entry."""

    class DummyPlaylist:
        name = "Source Playlist"

    class DummyPlaylistsController:
        def __init__(self, tracks: list[Any]) -> None:
            self._tracks = tracks

        async def get(self, playlist_id: str, provider: str) -> Any:
            return DummyPlaylist()

        async def tracks(self, playlist_id: str, provider: str) -> Any:
            for track in self._tracks:
                yield track

    class DummyTrack:
        def __init__(self, item_id: str, name: str, uri: str = "") -> None:
            self.item_id = item_id
            self.name = name
            self.artists: list[Any] = []
            self.duration = 180
            self.uri = uri
            self.provider_mappings: list[Any] = []

    good_track_1 = DummyTrack("1", "Track One", uri="library://track/1")
    unresolvable_track = DummyTrack("2", "Track Two")
    good_track_2 = DummyTrack("3", "Track Three", uri="library://track/3")

    class DummyMusic:
        playlists = DummyPlaylistsController([good_track_1, unresolvable_track, good_track_2])

    class DummyMass:
        music = DummyMusic()

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())
    station = {"source_playlist_id": "playlist-1", "source_playlist_provider": "library"}

    with caplog.at_level(logging.WARNING):
        tracks, playlist_name = await runtime._fetch_source_tracks(station)

    assert playlist_name == "Source Playlist"
    assert [track["item_id"] for track in tracks] == ["1", "3"]
    assert [track["index"] for track in tracks] == [0, 1]
    # the resolved media item travels on the normalized dict, unchanged
    assert [track["media_item"] for track in tracks] == [good_track_1, good_track_2]
    assert any("Track Two" in record.message for record in caplog.records)


def test_apply_source_shuffle_returns_unchanged_when_disabled() -> None:
    """Leave the source list untouched when the station does not request shuffling."""
    runtime = DummyRuntime()
    tracks = [{"index": 0, "uri": "a"}, {"index": 1, "uri": "b"}]
    station = {"shuffle_source_tracks": False}

    result = runtime._apply_source_shuffle(tracks, station)

    assert result is tracks


def test_apply_source_shuffle_reorders_and_records_source_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shuffle every track into a new order while keeping all of them and their origin."""
    # capture the real class before patching it away: runtime.random is the same shared
    # stdlib module object, so the lambda below would otherwise re-look-up itself
    original_random_cls = random.Random
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.random.Random",
        lambda: original_random_cls(1234),
    )
    runtime = DummyRuntime()
    tracks = [{"uri": f"track/{i}"} for i in range(5)]
    station = {"shuffle_source_tracks": True}

    result = runtime._apply_source_shuffle(tracks, station)

    assert [track["index"] for track in result] == list(range(len(tracks)))
    assert {track["uri"] for track in result} == {track["uri"] for track in tracks}
    for track in result:
        assert tracks[track["source_index"]]["uri"] == track["uri"]
    # a seeded shuffle must actually reorder, not silently pass through in place
    assert [track["source_index"] for track in result] != list(range(len(tracks)))


def test_apply_track_duration_limit_keeps_prefix_of_given_order() -> None:
    """Truncate to the playtime cap by walking the given order, no shuffling."""
    runtime = DummyRuntime()
    tracks = [
        {"uri": "a", "duration": 120},
        {"uri": "b", "duration": 120},
        {"uri": "c", "duration": 120},
        {"uri": "d", "duration": 120},
    ]
    station = {"max_duration_minutes": 3}

    result = runtime._apply_track_duration_limit(tracks, station)

    assert [track["uri"] for track in result] == ["a", "b"]
    assert [track["index"] for track in result] == [0, 1]
    assert [track["source_index"] for track in result] == [0, 1]


def test_apply_track_duration_limit_zero_cap_is_noop() -> None:
    """A cap of 0 disables truncation entirely."""
    runtime = DummyRuntime()
    tracks = [{"uri": "a", "duration": 120}, {"uri": "b", "duration": 120}]
    station = {"max_duration_minutes": 0}

    result = runtime._apply_track_duration_limit(tracks, station)

    assert result is tracks


async def test_run_show_disables_shuffle_before_load() -> None:
    """Disable queue shuffle before the items are loaded, so sections keep their planned order."""
    runtime = ShowRuntime()
    call_order: list[str] = []
    set_shuffle_calls: list[tuple[str, bool]] = []

    async def _set_shuffle(queue_id: str, shuffle_enabled: bool) -> None:
        set_shuffle_calls.append((queue_id, shuffle_enabled))
        call_order.append("set_shuffle")

    async def _load(_queue_id: str, **_kwargs: Any) -> None:
        call_order.append("load")

    _set_runtime_mass(runtime, _show_mass_stub(set_shuffle=_set_shuffle, load=_load))
    session = SessionState(session_id="s1", station_id="st")

    await runtime._run_show(session, _show_station())

    assert set_shuffle_calls == [("living_room", False)]
    assert call_order.index("set_shuffle") < call_order.index("load")


async def test_run_show_stays_running_while_the_queue_plays_and_stop_cancels_it() -> None:
    """The session stays 'running' for as long as the show plays; a stop cancels it mid-show."""
    runtime = ShowRuntime()
    queue = _stub_queue(state=PlaybackState.PLAYING, current_index=0)
    unsubscribed = False

    def _subscribe(
        cb_func: Callable[[Any], None],  # noqa: ARG001
        event_filter: Any = None,  # noqa: ARG001
        id_filter: Any = None,  # noqa: ARG001
    ) -> Callable[[], None]:
        def _unsubscribe() -> None:
            nonlocal unsubscribed
            unsubscribed = True

        return _unsubscribe

    mass = _show_mass_stub(
        get=lambda _queue_id: queue,
        items=lambda _queue_id, limit=500, offset=0: [  # noqa: ARG005
            _stub_clip_queue_item("sess_000", session_id="s1")
        ],
        subscribe=_subscribe,
    )
    _set_runtime_mass(runtime, mass)
    session = SessionState(session_id="s1", station_id="st")
    runtime._sessions[session.session_id] = session

    task = asyncio.create_task(runtime._run_session(session.session_id, _show_station()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # the show is on air: the session must stay running, exactly like start_run's
    # max-concurrent-runs and station-already-active guards require
    assert session.status == "running"
    assert not task.done()

    # this is what stop_run does to end a run mid-show
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.status == "stopped"
    assert unsubscribed


async def test_run_show_binds_the_session_to_the_target_queue() -> None:
    """Record the queue a show plays on so stopping the show can stop it."""
    runtime = ShowRuntime()
    _set_runtime_mass(runtime, _show_mass_stub())
    session = SessionState(session_id="s1", station_id="st")

    await runtime._run_show(session, _show_station())

    assert session.queue_id == "living_room"


async def test_run_session_reports_a_queue_stop_as_stopped() -> None:
    """Report a run that ended because the queue was stopped as stopped, not completed."""

    class QueueStoppedRuntime(AIRadioRuntimeMixin):
        def __init__(self) -> None:
            self.logger = logging.getLogger("tests.ai_radio.runtime.queue_stopped")
            self._sessions: dict[str, SessionState] = {}

        async def _run_show(self, session: SessionState, station: dict[str, Any]) -> dict[str, Any]:
            return {"ended_reason": "queue_stopped"}

    runtime = QueueStoppedRuntime()
    session = SessionState(session_id="s1", station_id="st")
    runtime._sessions[session.session_id] = session

    await runtime._run_session(session.session_id, {"id": "st"})

    assert session.status == "stopped"
    assert session.ended_at is not None
