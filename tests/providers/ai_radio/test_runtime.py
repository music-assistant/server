"""Unit tests for AI Radio runtime planning and rendering helpers."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.datetime import now as host_now
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine
from music_assistant.providers.ai_radio import runtime as runtime_module
from music_assistant.providers.ai_radio.constants import (
    ATTR_HOST_ID,
    ATTR_SESSION_ID,
    ATTR_WEATHER_REQUIRED,
    CONF_AI_ENGINE,
    CONF_TTS_ENGINE,
    CONF_WEATHER_PROVIDER,
    TTS_PRONUNCIATION_INSTRUCTIONS,
)
from music_assistant.providers.ai_radio.models import PlannedSection, Slot
from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin


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

    async def set_queue_dj(self, queue_id: str, host_id: str | None) -> dict[str, dict[str, str]]:
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


def test_weather_strings_hourly_window_starts_at_the_first_upcoming_hour() -> None:
    """current.time sits on a 15-minute grid; the hourly window starts at the first non-past hour."""
    runtime = DummyRuntime()
    hours = [f"2026-08-19T{hour:02d}:00" for hour in range(24)]
    payload = {
        "current": {
            "time": "2026-08-19T15:45",
            "temperature_2m": 20.0,
            "apparent_temperature": 19.0,
        },
        "hourly": {
            "time": hours,
            "temperature_2m": [15.0] * 24,
            "precipitation_probability": [0] * 24,
        },
        "daily": {
            "time": [],
            "temperature_2m_min": [],
            "temperature_2m_max": [],
            "precipitation_probability_max": [],
        },
    }

    hourly, _daily = runtime._format_weather_strings(payload)

    assert hourly.split("; ")[1].startswith("2026-08-19 16:00")
    assert "2026-08-19 15:00" not in hourly
    assert "2026-08-19 00:00" not in hourly


def test_format_weather_strings_uses_the_requested_unit_suffix() -> None:
    """The unit suffix passed in replaces the default C in every emitted string."""
    runtime = DummyRuntime()
    payload = {
        "current": {
            "time": "2026-08-10T09:00",
            "temperature_2m": 70.0,
            "apparent_temperature": 68.0,
        },
        "hourly": {
            "time": ["2026-08-10T09:00"],
            "temperature_2m": [70.0],
            "precipitation_probability": [10],
        },
        "daily": {
            "time": ["2026-08-10"],
            "temperature_2m_min": [60.0],
            "temperature_2m_max": [75.0],
            "precipitation_probability_max": [20],
        },
    }

    hourly, daily = runtime._format_weather_strings(payload, unit_suffix="F")

    assert hourly == "now 70F (feels 68F); 2026-08-10 09:00: 70F, rain 10%"
    assert daily == "2026-08-10: 60-75F, rain 20%"


def _stub_open_meteo_responses(
    calls: list[tuple[str, dict[str, Any]]],
    country_code: str = "US",
) -> Callable[[str, dict[str, Any], int], Awaitable[dict[str, Any]]]:
    """Return an ``_open_meteo_get_json`` stand-in recording calls and faking both endpoints."""

    async def _get_json(
        base_url: str, params: dict[str, Any], _timeout_seconds: int
    ) -> dict[str, Any]:
        calls.append((base_url, params))
        if "geocoding" in base_url:
            return {
                "results": [
                    {
                        "latitude": 40.71,
                        "longitude": -74.01,
                        "timezone": "America/New_York",
                        "country": "",
                        "country_code": country_code,
                    }
                ]
            }
        return {
            "current": {
                "time": "2026-08-10T09:00",
                "temperature_2m": 70.0,
                "apparent_temperature": 68.0,
            },
            "hourly": {
                "time": ["2026-08-10T09:00"],
                "temperature_2m": [70.0],
                "precipitation_probability": [10],
            },
            "daily": {
                "time": ["2026-08-10"],
                "temperature_2m_min": [60.0],
                "temperature_2m_max": [75.0],
                "precipitation_probability_max": [20],
            },
        }

    return _get_json


async def test_fetch_open_meteo_weather_requests_fahrenheit_for_a_us_location() -> None:
    """A US-configured location asks Open-Meteo for Fahrenheit and formats with an F suffix."""
    runtime = DummyRuntime()
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime._open_meteo_get_json = _stub_open_meteo_responses(  # type: ignore[method-assign, assignment]
        calls
    )

    hourly, daily = await runtime._fetch_open_meteo_weather(
        city="New York", country="US", timeout_seconds=20
    )

    forecast_params = calls[1][1]
    assert forecast_params["temperature_unit"] == "fahrenheit"
    assert "70F" in hourly
    assert daily.endswith("F, rain 20%")


async def test_fetch_open_meteo_weather_omits_temperature_unit_for_a_nl_location() -> None:
    """A non-Fahrenheit country sends no temperature_unit param and formats with a C suffix."""
    runtime = DummyRuntime()
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime._open_meteo_get_json = _stub_open_meteo_responses(  # type: ignore[method-assign, assignment]
        calls, country_code="NL"
    )

    hourly, daily = await runtime._fetch_open_meteo_weather(
        city="Amsterdam", country="NL", timeout_seconds=20
    )

    forecast_params = calls[1][1]
    assert "temperature_unit" not in forecast_params
    assert "70C" in hourly
    assert daily.endswith("C, rain 20%")


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


def _stub_open_meteo_get_json(
    calls: list[tuple[str, dict[str, Any]]],
    geocode_results: list[dict[str, Any]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Stub _open_meteo_get_json, recording every call and answering the geocoding request."""

    async def _fake(base_url: str, params: dict[str, Any], _timeout_seconds: int) -> dict[str, Any]:
        calls.append((base_url, dict(params)))
        if "geocoding-api" in base_url:
            return {"results": geocode_results}
        return {"hourly": {}, "daily": {}, "current": {}}

    return _fake


async def test_fetch_open_meteo_weather_sends_country_code_not_country() -> None:
    """The geocoding request filters by countryCode, the API's real parameter name."""
    runtime = DummyRuntime()
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime._open_meteo_get_json = _stub_open_meteo_get_json(  # type: ignore[method-assign, assignment]
        calls,
        [
            {
                "latitude": 52.37,
                "longitude": 4.9,
                "country": "Netherlands",
                "country_code": "NL",
                "timezone": "Europe/Amsterdam",
            }
        ],
    )

    await runtime._fetch_open_meteo_weather(city="Amsterdam", country="NL", timeout_seconds=10)

    _geocode_url, geocode_params = next(call for call in calls if "geocoding-api" in call[0])
    assert geocode_params["countryCode"] == "NL"
    assert "country" not in geocode_params


async def test_fetch_open_meteo_weather_raises_when_no_result_matches_the_country() -> None:
    """A same-named city in the wrong country must raise, never silently pick results[0]."""
    runtime = DummyRuntime()
    calls: list[tuple[str, dict[str, Any]]] = []
    # every candidate is a Cambridge, but none of them is in New Zealand
    runtime._open_meteo_get_json = _stub_open_meteo_get_json(  # type: ignore[method-assign, assignment]
        calls,
        [
            {
                "latitude": 52.2,
                "longitude": 0.12,
                "country": "United Kingdom",
                "country_code": "GB",
                "timezone": "Europe/London",
            }
        ],
    )

    with pytest.raises(MusicAssistantError, match="Cambridge"):
        await runtime._fetch_open_meteo_weather(city="Cambridge", country="NZ", timeout_seconds=10)


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


def test_resolve_placeholders_timestamp_spells_out_weekday() -> None:
    """The deferred <timestamp> value names the weekday so the LLM never has to derive it."""
    runtime = DummyRuntime()
    moment = datetime.datetime(2026, 8, 22, 16, 20, tzinfo=datetime.UTC)
    runtime._configured_now = lambda: moment  # type: ignore[method-assign]
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

    _static, deferred = runtime._resolve_placeholders(
        program={},
        tracks=tracks,
        slot=slot,
        runtime_tokens={},
    )

    assert deferred["<timestamp>"] == "Saturday 22 August 2026, 16:20 UTC"


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


def test_plan_sections_can_defer_the_song_tokens() -> None:
    """With deferred song tokens the prompt keeps them verbatim while guards still see them."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    station = {
        "sections": [
            {
                "id": "Intro",
                "name": "Intro",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "First up <next_songinfo>, then <very_next_songinfo>. <weather_hourly>",
                "constraints": {"max_chars": 200},
            }
        ],
        "section_order": [
            {
                "when": "start_of_playlist",
                "flow": [
                    {
                        "OPTIONAL": {
                            "section": "Intro",
                            "chance": 100,
                            "guards": {"require_placeholders_present": ["<next_songinfo>"]},
                        }
                    }
                ],
            }
        ],
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
        allowed_slot_when=["start_of_playlist"],
        runtime_tokens={"<weather_hourly>": "12 degrees"},
        defer_song_tokens=True,
    )

    assert len(planned) == 1
    prompt = planned[0].prompt
    assert prompt.startswith(
        "First up <next_songinfo>, then <very_next_songinfo>. <weather_hourly>"
    )
    assert "A - One" not in prompt


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


def test_standalone_weather_section_is_weather_required() -> None:
    """A section that only speaks weather is flagged so a failed fetch skips it, not fakes it."""
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
    assert planned[0].weather_required is True


def _merge_weather_news_station() -> dict[str, Any]:
    """Return a station whose between-songs slot merges a weather-guarded section with news."""
    return {
        "sections": [
            {
                "id": "Weather",
                "name": "Weather",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "Current weather: <weather_hourly>.",
                "constraints": {"max_chars": 200},
            },
            {
                "id": "News",
                "name": "News",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "Give the headlines.",
                "constraints": {"max_chars": 200},
            },
            {
                "id": "Smoother",
                "name": "Between Songs Mix",
                "type": "ai_meta",
                "prompt": "Combine these: <section_drafts>",
            },
        ],
        "section_order": [
            {
                "when": "between_songs",
                "flow": [
                    {
                        "OPTIONAL": {
                            "section": "Weather",
                            "chance": 1.0,
                            "guards": {"require_placeholders_present": ["<weather_hourly>"]},
                        }
                    },
                    {"OPTIONAL": {"section": "News", "chance": 1.0, "guards": {}}},
                ],
            }
        ],
        "merge_section_id": "Smoother",
    }


def test_merged_weather_and_news_clip_is_not_weather_required() -> None:
    """A merged clip must still carry the news half even when weather data is missing."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    tracks = [
        {"index": 0, "songinfo": "A - One", "duration": 200},
        {"index": 1, "songinfo": "B - Two", "duration": 200},
    ]

    planned, _history = runtime._plan_sections(
        session_id="sess",
        tracks=tracks,
        program=_merge_weather_news_station(),
        track_index_offset=0,
        minute_offset=0.0,
        history_state={},
        allowed_slot_when=["between_songs"],
        runtime_tokens={"<weather_hourly>": "12 degrees"},
    )

    assert len(planned) == 1
    assert planned[0].weather_required is False


def test_mixed_purpose_section_without_a_weather_guard_is_not_weather_required() -> None:
    """A prompt that just mentions the weather must not skip the whole clip on a failed fetch."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    station = {
        "sections": [
            {
                "id": "Intro",
                "name": "Intro",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "Introduce <next_songinfo> and mention the weather <weather_hourly>.",
                "constraints": {"max_chars": 200},
            }
        ],
        "section_order": [{"when": "between_songs", "flow": [{"MUST": "Intro"}]}],
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

    assert len(planned) == 1
    assert planned[0].weather_required is False


def test_alternative_weather_section_is_not_weather_required() -> None:
    """An ALTERNATIVE section carries no guards, so it never blocks a clip on weather data."""
    runtime = DummyRuntime()
    _set_runtime_mass(runtime, SimpleNamespace(metadata=SimpleNamespace(locale="en_US")))
    station = {
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
                "flow": [{"ALTERNATIVE": {"choices": [{"section": "Weather", "weight": 100}]}}],
            }
        ],
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

    assert len(planned) == 1
    assert planned[0].weather_required is False


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


def test_clip_item_carries_weather_required_flag() -> None:
    """A planned clip's weather_required flag travels onto the queue item's attributes."""
    runtime = DummyRuntime()
    section = PlannedSection(
        order=0,
        clip_id="sess_000",
        section_id="Weather",
        section_name="Weather",
        when="between_songs",
        insert_at_index=1,
        prompt="Current weather: <weather_hourly>.",
        max_chars=0,
        web_search_mode="disabled",
        weather_required=True,
    )
    program = {"id": "station_a", "host_id": "rick"}

    item = runtime._section_to_clip_item("queue-1", "sess", program, section)

    assert item.extra_attributes[ATTR_WEATHER_REQUIRED] is True


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
