"""Unit tests for AI Radio runtime session flow and logging."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.background_task import BackgroundTask
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    QueueOption,
    TaskStatus,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import SoundEffect

from music_assistant.helpers.datetime import now as host_now
from music_assistant.helpers.playlists import PlaylistItem, parse_m3u, parse_m3u_playlist_image
from music_assistant.helpers.uri import create_uri
from music_assistant.providers.ai_radio.models import AudioSection, SessionState, Slot
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

    def __init__(self) -> None:
        """Initialize minimal state for runtime tests."""
        self.logger = logging.getLogger("tests.ai_radio.runtime")
        self._sessions: dict[str, SessionState] = {}
        self.config = StubConfig()

    async def _run_playlist_mode(
        self,
        session: SessionState,
        station: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a successful playlist-mode result."""
        return {"ok": True}

    async def _run_dynamic_mode(
        self,
        session: SessionState,
        station: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a successful dynamic-mode result."""
        return {"ok": True}


class FailingRuntime(DummyRuntime):
    """Runtime harness that forces playlist execution failure."""

    async def _run_playlist_mode(
        self,
        session: SessionState,
        station: dict[str, Any],
    ) -> dict[str, Any]:
        """Raise to test failed-session behavior."""
        raise RuntimeError("boom")


def _set_runtime_mass(runtime: AIRadioRuntimeMixin, mass: Any) -> None:
    """Attach lightweight test mass object while bypassing strict runtime typing."""
    cast("Any", runtime).mass = mass


async def test_run_session_sets_completed_and_logs(caplog: Any) -> None:
    """Complete a session and emit start/completion logs."""
    runtime = DummyRuntime()
    session = SessionState(session_id="s1", station_id="station_a", mode="playlist")
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
    session = SessionState(session_id="s2", station_id="station_b", mode="playlist")
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
        async def _run_playlist_mode(
            self,
            session: SessionState,
            station: dict[str, Any],
        ) -> dict[str, Any]:
            raise EmptyError

    runtime = EmptyFailingRuntime()
    session = SessionState(session_id="s2b", station_id="station_b", mode="playlist")
    runtime._sessions[session.session_id] = session

    await runtime._run_session(session.session_id, {"id": "station_b"})

    assert session.status == "failed"
    assert session.error == "EmptyError"


async def test_run_session_sets_stopped_state_on_cancellation(caplog: Any) -> None:
    """Mark session as stopped when runtime execution is cancelled."""

    class CancelledRuntime(DummyRuntime):
        async def _run_playlist_mode(
            self,
            session: SessionState,
            station: dict[str, Any],
        ) -> dict[str, Any]:
            raise asyncio.CancelledError

    runtime = CancelledRuntime()
    session = SessionState(session_id="s3", station_id="station_c", mode="playlist")
    runtime._sessions[session.session_id] = session

    with caplog.at_level(logging.INFO), suppress(asyncio.CancelledError):
        await runtime._run_session(session.session_id, {"id": "station_c"})

    assert session.status == "stopped"
    assert any("AI Radio run cancelled" in message for message in caplog.messages)


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
        "general": {"weather_provider": "unsupported_provider"},
    }
    runtime.config = StubConfig({"weather_city": "Berlin", "weather_country": "DE"})

    with caplog.at_level(logging.WARNING):
        tokens = await runtime._prepare_runtime_tokens(station)

    assert tokens == {}
    assert any("Unsupported weather provider" in message for message in caplog.messages)


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
        "general": {"weather_provider": "open_meteo"},
    }
    runtime.config = StubConfig({"weather_city": "", "weather_country": "DE"})

    with caplog.at_level(logging.DEBUG):
        tokens = await runtime._prepare_runtime_tokens(station)

    assert tokens == {}
    assert any("no location configured" in message for message in caplog.messages)


def test_extract_location_reads_provider_config() -> None:
    """Weather location comes from the provider config, not the station."""
    runtime = DummyRuntime()
    runtime.config = StubConfig({"weather_city": "Berlin", "weather_country": "DE"})

    assert runtime._extract_location() == ("Berlin", "DE")


def test_extract_location_defaults_to_empty_when_unset() -> None:
    """An unconfigured weather location resolves to empty strings, not an error."""
    runtime = DummyRuntime()

    assert runtime._extract_location() == ("", "")


def test_configured_now_uses_valid_configured_timezone() -> None:
    """A valid configured IANA timezone name is honored for the current time."""
    runtime = DummyRuntime()
    runtime.config = StubConfig({"timezone": "Asia/Tokyo"})

    result = runtime._configured_now()

    assert str(result.tzinfo) == "Asia/Tokyo"


@pytest.mark.parametrize(
    "timezone_value",
    ["", "not-a-real-zone", "CEST", "../../etc/passwd", "Europe/Amsterdam "],
)
def test_configured_now_falls_back_when_timezone_blank_or_invalid(timezone_value: str) -> None:
    """A blank or invalid configured timezone falls back to the host local time."""
    runtime = DummyRuntime()
    runtime.config = StubConfig({"timezone": timezone_value})

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
        tracks=tracks,
        station=station,
        track_index_offset=0,
        minute_offset=0.0,
        history_state={},
        allowed_slot_when=["between_songs"],
        runtime_tokens={},
    )

    assert planned == []


async def test_render_tts_converts_raw_http_path_to_builtin_sound_effect_uri() -> None:
    """Wrap raw TTS HTTP paths as builtin sound effect URIs for queue progression."""

    class DummyStreamDetails:
        def __init__(self, path: str) -> None:
            self.path = path

    class DummyTTSPlugin:
        instance_id = "hass_1"

        async def get_tts_message(self, message: str) -> DummyStreamDetails:
            return DummyStreamDetails("http://example.test/api/tts_proxy/abc123.mp3")

    class DummyMass:
        def get_providers_supporting_feature(
            self,
            feature: ProviderFeature,
            priority: tuple[ProviderType, ...] = (),
        ) -> list[Any]:
            if feature == ProviderFeature.TTS:
                return [DummyTTSPlugin()]
            return []

        def get_provider(self, provider: str) -> Any:
            if provider == "builtin":

                class DummyBuiltinProvider:
                    instance_id = "builtin_1"

                return DummyBuiltinProvider()
            return None

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())
    runtime._warm_builtin_duration_cache = AsyncMock(return_value=11)  # type: ignore[method-assign]

    uri, duration = await runtime._render_tts("hello world", "Test Section")

    assert uri == create_uri(
        MediaType.SOUND_EFFECT,
        "builtin_1",
        "http://example.test/api/tts_proxy/abc123.mp3",
    )
    assert duration == 11
    runtime._warm_builtin_duration_cache.assert_awaited_once()


async def test_generate_text_wraps_not_connected_error() -> None:
    """Raise an actionable MusicAssistantError when AI provider is disconnected."""

    class NotConnected(Exception):
        """Match hass_client NotConnected exception name."""

    class DummyAIPlugin:
        instance_id = "hass_1"

        async def ai_query(self, query: str) -> str:
            raise NotConnected

    class DummyMass:
        metadata = SimpleNamespace(locale="en_US")

        def get_providers_supporting_feature(
            self,
            feature: ProviderFeature,
            priority: tuple[ProviderType, ...] = (),
        ) -> list[Any]:
            if feature == ProviderFeature.AI_QUERY:
                return [DummyAIPlugin()]
            return []

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    with pytest.raises(MusicAssistantError) as error:
        await runtime._generate_text(
            station={"general": {"instructions": "test"}},
            prompt="test prompt",
            web_mode="disabled",
        )
    assert "not connected" in str(error.value).lower()
    assert "hass_1" in str(error.value)


async def test_generate_text_asks_for_the_system_locale_language() -> None:
    """The AI query states the server locale so sections are written in that language."""
    captured: list[str] = []

    class DummyAIPlugin:
        instance_id = "hass_1"

        async def ai_query(self, query: str) -> str:
            captured.append(query)
            return "section text"

    class DummyMass:
        metadata = SimpleNamespace(locale="nl_NL")

        def get_providers_supporting_feature(
            self,
            feature: ProviderFeature,
            priority: tuple[ProviderType, ...] = (),
        ) -> list[Any]:
            if feature == ProviderFeature.AI_QUERY:
                return [DummyAIPlugin()]
            return []

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    await runtime._generate_text(
        station={"general": {"instructions": "test"}},
        prompt="test prompt",
        web_mode="disabled",
    )

    assert "nl_NL" in captured[0]


def test_compose_builtin_playlist_items_preserves_section_metadata() -> None:
    """Compose builtin M3U items without relying on provider-side title parsing."""

    class DummyProvider:
        domain = "builtin"
        instance_id = "builtin_1"

    class DummyMass:
        def get_provider(self, provider: str) -> Any:
            if provider in {"builtin", "builtin_1"}:
                return DummyProvider()
            return None

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())
    section_url = "http://example.test/api/tts_proxy/section-random-id.mp3"
    source_item = PlaylistItem(
        path=create_uri(MediaType.TRACK, "library", "123"),
        title="Artist - Song",
        metadata={"media_type": MediaType.TRACK.value, "name": "Song"},
    )

    items, track_count = runtime._compose_builtin_playlist_items(
        tracks=[
            {
                "uri": source_item.path,
                "name": "Song",
                "songinfo": "Artist - Song",
                "duration": 180,
                "playlist_item": source_item,
            }
        ],
        sections=[
            AudioSection(
                order=1,
                section_id="intro",
                section_name="Intro",
                insert_at_index=0,
                uri=create_uri(MediaType.SOUND_EFFECT, "builtin_1", section_url),
            )
        ],
    )

    assert track_count == 1
    assert len(items) == 2
    assert items[0].title == "Intro"
    assert items[0].metadata == {
        "media_type": MediaType.SOUND_EFFECT.value,
        "name": "Intro",
    }
    assert items[0].images
    assert items[0].images[0].path == runtime._ai_radio_cover_image_path()
    assert items[0].providers
    assert items[0].providers[0].domain == "builtin"
    assert items[0].providers[0].instance_id == "builtin_1"
    assert items[0].providers[0].item_id == section_url
    assert items[0].artists
    assert items[0].artists[0].name == "AI Radio"
    assert items[1] == source_item


def test_section_to_queue_sound_effect_preserves_display_data() -> None:
    """Build a rich SoundEffect for dynamic-mode queue inserts."""

    class DummyProvider:
        domain = "builtin"
        instance_id = "builtin_1"

    class DummyMass:
        def get_provider(self, provider: str) -> Any:
            if provider in {"builtin", "builtin_1"}:
                return DummyProvider()
            return None

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())
    section = AudioSection(
        order=1,
        section_id="intro",
        section_name="Intro",
        insert_at_index=0,
        uri=create_uri(MediaType.SOUND_EFFECT, "builtin_1", "http://example.test/intro.mp3"),
        duration=12,
    )

    item = runtime._section_to_queue_sound_effect(section)

    assert isinstance(item, SoundEffect)
    assert item.name == "Intro"
    assert item.duration == 12
    assert item.provider_mappings
    mapping = next(iter(item.provider_mappings))
    assert mapping.provider_domain == "builtin"
    assert mapping.provider_instance == "builtin_1"
    assert mapping.item_id == "http://example.test/intro.mp3"
    assert item.metadata.images
    assert item.metadata.images[0].path == runtime._ai_radio_cover_image_path()


async def test_import_builtin_playlist_preserves_m3u_metadata() -> None:
    """Import builtin playlist from a fully described M3U payload."""

    class DummyPlaylistsController:
        def __init__(self) -> None:
            self.m3u_data = ""

        async def import_playlist(self, m3u_data: str) -> dict[str, str]:
            self.m3u_data = m3u_data
            return {"item_id": "42", "name": "AI Radio: Test"}

    class DummyMass:
        def __init__(self) -> None:
            self.music = type("DummyMusic", (), {})()
            self.music.playlists = DummyPlaylistsController()

        def get_provider(self, provider: str) -> None:
            return None

    runtime = DummyRuntime()
    mass = DummyMass()
    _set_runtime_mass(runtime, mass)
    item = runtime._section_to_playlist_item(
        AudioSection(
            order=1,
            section_id="intro",
            section_name="Intro",
            insert_at_index=0,
            uri=create_uri(MediaType.SOUND_EFFECT, "builtin", "http://example.test/intro.mp3"),
        )
    )

    playlist = await runtime._import_builtin_playlist("AI Radio: Test", [item])

    assert playlist == {"item_id": "42", "name": "AI Radio: Test"}
    assert parse_m3u_playlist_image(mass.music.playlists.m3u_data) == (
        runtime._ai_radio_cover_image_path()
    )
    parsed = parse_m3u(mass.music.playlists.m3u_data)
    assert parsed[0].title == "Intro"
    assert parsed[0].metadata == {
        "media_type": MediaType.SOUND_EFFECT.value,
        "name": "Intro",
    }
    assert parsed[0].images[0].path == runtime._ai_radio_cover_image_path()
    assert parsed[0].artists
    assert parsed[0].artists[0].name == "AI Radio"


async def test_wait_for_background_task_completion_returns_on_success() -> None:
    """Return immediately when background task is already successful."""

    class DummyTasks:
        def get_task(self, task_id: str) -> BackgroundTask:
            return BackgroundTask(name="add", id=task_id, status=TaskStatus.SUCCESS)

    class DummyMass:
        tasks = DummyTasks()

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    await runtime._wait_for_background_task_completion("task_1", timeout_seconds=1)


async def test_wait_for_background_task_completion_waits_until_success() -> None:
    """Poll task status until completion."""
    statuses = [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.SUCCESS]

    class DummyTasks:
        def __init__(self) -> None:
            self._index = 0

        def get_task(self, task_id: str) -> BackgroundTask:
            status = statuses[self._index]
            if self._index < len(statuses) - 1:
                self._index += 1
            return BackgroundTask(name="add", id=task_id, status=status)

    class DummyMass:
        tasks = DummyTasks()

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    await runtime._wait_for_background_task_completion("task_2", timeout_seconds=1)


def test_compose_entries_returns_correct_source_track_positions() -> None:
    """_compose_entries returns explicit source-track positions, not substring-derived (C1)."""
    runtime = DummyRuntime()
    tracks = [
        {
            "uri": "library://track/1",
            "name": "T1",
            "songinfo": "A - T1",
            "duration": 180,
            "playlist_item": None,
            "item_id": "1",
            "artist": "A",
            "index": 0,
        },
        {
            "uri": "library://track/2",
            "name": "T2",
            "songinfo": "A - T2",
            "duration": 180,
            "playlist_item": None,
            "item_id": "2",
            "artist": "A",
            "index": 1,
        },
    ]
    sections = [
        AudioSection(
            order=1,
            section_id="intro",
            section_name="Intro",
            insert_at_index=0,
            uri=create_uri(MediaType.SOUND_EFFECT, "builtin_1", "http://x/intro.mp3"),
        ),
        AudioSection(
            order=1,
            section_id="outro",
            section_name="Outro",
            insert_at_index=2,
            uri=create_uri(MediaType.SOUND_EFFECT, "builtin_1", "http://x/outro.mp3"),
        ),
    ]

    entries, source_positions, _track_count = runtime._compose_entries(tracks, sections)

    source_uris = {cast("str", track["uri"]) for track in tracks}
    expected_source_indices = [i for i, uri in enumerate(entries) if uri in source_uris]

    assert source_positions == expected_source_indices
    assert source_positions == [1, 2]


@pytest.mark.parametrize(
    "stub_status",
    [TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PENDING],
)
async def test_wait_for_background_task_completion_raises_on_failure(
    stub_status: TaskStatus,
) -> None:
    """Surface failures, cancellations and timeouts as MusicAssistantError (P4)."""

    class DummyTasks:
        def get_task(self, task_id: str) -> BackgroundTask:
            return BackgroundTask(name="add", id=task_id, status=stub_status)

    class DummyMass:
        tasks = DummyTasks()

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    with pytest.raises(MusicAssistantError):
        await runtime._wait_for_background_task_completion("task_x", timeout_seconds=1)


def test_get_ai_plugin_preserves_priority_order() -> None:
    """Honor provider priority order returned by mass over alphabetical sort (P5)."""

    class DummyPlugin:
        def __init__(self, instance_id: str) -> None:
            self.instance_id = instance_id

    high_priority = DummyPlugin("zz_high")
    low_priority = DummyPlugin("aa_low")

    class DummyMass:
        def get_providers_supporting_feature(
            self,
            feature: ProviderFeature,
            priority: tuple[ProviderType, ...] = (),
        ) -> list[Any]:
            if feature == ProviderFeature.AI_QUERY:
                return [high_priority, low_priority]
            return []

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    plugin = runtime._get_ai_plugin()

    assert plugin.instance_id == "zz_high"


def test_get_tts_plugin_preserves_priority_order() -> None:
    """Honor provider priority order returned by mass over alphabetical sort (P5)."""

    class DummyPlugin:
        def __init__(self, instance_id: str) -> None:
            self.instance_id = instance_id

    high_priority = DummyPlugin("zz_high")
    low_priority = DummyPlugin("aa_low")

    class DummyMass:
        def get_providers_supporting_feature(
            self,
            feature: ProviderFeature,
            priority: tuple[ProviderType, ...] = (),
        ) -> list[Any]:
            if feature == ProviderFeature.TTS:
                return [high_priority, low_priority]
            return []

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    plugin = runtime._get_tts_plugin()

    assert plugin.instance_id == "zz_high"


async def test_run_dynamic_mode_has_watchdog_for_stalled_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surface stalled dynamic playback (queue not advancing) as MusicAssistantError (P7)."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS",
        0.2,
    )

    class StalledRuntime(AIRadioRuntimeMixin):
        def __init__(self) -> None:
            self.logger = logging.getLogger("tests.ai_radio.runtime.p7")
            self._sessions: dict[str, SessionState] = {}

        async def _fetch_source_tracks(
            self, station: dict[str, Any]
        ) -> tuple[list[dict[str, Any]], str]:
            tracks = [
                {
                    "uri": "library://track/1",
                    "duration": 180,
                    "name": "T1",
                    "artist": "A",
                    "songinfo": "A - T1",
                    "index": 0,
                    "playlist_item": None,
                    "item_id": "1",
                },
                {
                    "uri": "library://track/2",
                    "duration": 180,
                    "name": "T2",
                    "artist": "A",
                    "songinfo": "A - T2",
                    "index": 1,
                    "playlist_item": None,
                    "item_id": "2",
                },
            ]
            return tracks, "Source"

        def _apply_track_duration_limit(
            self, tracks: list[dict[str, Any]], station: dict[str, Any]
        ) -> list[dict[str, Any]]:
            return tracks

        def _plan_sections(self, *args: Any, **kwargs: Any) -> tuple[list[Any], dict[str, Any]]:
            return [], {}

        async def _generate_sections(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def _synthesize_sections(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def _prepare_runtime_tokens(self, station: dict[str, Any]) -> dict[str, str]:
            return {}

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    class DummyQueue:
        current_index: int | None = None

    class DummyPlayerQueues:
        def clear(self, queue_id: str) -> None:
            return None

        async def play_media(self, **kwargs: Any) -> None:
            return None

        async def play_index(self, queue_id: str, index: int) -> None:
            return None

        def get_active_queue(self, player_id: str) -> Any:
            return None

        def get(self, queue_id: str) -> Any:
            return DummyQueue()

    class DummyMass:
        players = DummyPlayers()
        player_queues = DummyPlayerQueues()

    runtime = StalledRuntime()
    _set_runtime_mass(runtime, DummyMass())
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session
    station = {
        "id": "st",
        "name": "Stalled Station",
        "default_player_id": "living_room",
        "dynamic_batch_size": 1,
        "dynamic_poll_seconds": 1,
        "dynamic_prefetch_remaining_tracks": 1,
        "clear_queue_on_start": False,
        "general": {"timezone": "UTC"},
        "sections": [],
        "section_order": [],
    }

    with pytest.raises(MusicAssistantError, match=r"stall|stopped advancing|watchdog|inactive"):
        await asyncio.wait_for(runtime._run_dynamic_mode(session, station), timeout=3.0)


@pytest.mark.parametrize(
    ("batch_size", "prefetch_config", "expected_prefetch"),
    [
        (3, 2, 2),
        (3, 5, 2),
        (1, 2, 1),
        (5, 1, 1),
    ],
)
async def test_run_dynamic_mode_clamps_prefetch_to_batch_size(
    batch_size: int, prefetch_config: int, expected_prefetch: int
) -> None:
    """Clamp dynamic_prefetch_remaining_tracks so the trigger falls inside the batch.

    Only exercises the setup segment of _run_dynamic_mode: the clamped value is
    captured via the "initializing_queue" progress update, short-circuiting before
    the batch-queueing loop runs. This does NOT cover trigger_position math further
    down in the loop, nor the storage-layer normalization clamp in storage.py.
    """

    class ProbeStop(Exception):
        """Raised to short-circuit _run_dynamic_mode once the clamp is captured."""

        def __init__(self, prefetch_remaining_tracks: int) -> None:
            self.prefetch_remaining_tracks = prefetch_remaining_tracks

    class ProbeRuntime(AIRadioRuntimeMixin):
        def __init__(self) -> None:
            self.logger = logging.getLogger("tests.ai_radio.runtime.clamp_probe")
            self._sessions: dict[str, SessionState] = {}

        async def _fetch_source_tracks(
            self, station: dict[str, Any]
        ) -> tuple[list[dict[str, Any]], str]:
            return [{"uri": "library://track/1", "duration": 180}], "Source"

        def _apply_track_duration_limit(
            self, tracks: list[dict[str, Any]], station: dict[str, Any]
        ) -> list[dict[str, Any]]:
            return tracks

        async def _prepare_runtime_tokens(self, station: dict[str, Any]) -> dict[str, str]:
            return {}

        def _set_session_progress(
            self, session: SessionState, phase: str, **details: Any
        ) -> None:
            if phase == "initializing_queue":
                raise ProbeStop(cast("int", details["prefetch_remaining_tracks"]))

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    class DummyPlayerQueues:
        def get_active_queue(self, player_id: str) -> Any:
            return None

    class DummyMass:
        players = DummyPlayers()
        player_queues = DummyPlayerQueues()

    runtime = ProbeRuntime()
    _set_runtime_mass(runtime, DummyMass())
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session
    station = {
        "id": "st",
        "name": "Probe Station",
        "default_player_id": "living_room",
        "dynamic_batch_size": batch_size,
        "dynamic_prefetch_remaining_tracks": prefetch_config,
        "general": {"timezone": "UTC"},
    }

    with pytest.raises(ProbeStop) as excinfo:
        await runtime._run_dynamic_mode(session, station)

    assert excinfo.value.prefetch_remaining_tracks == expected_prefetch


def _make_watchdog_runtime_classes() -> tuple[type, type]:
    """Build the reusable dynamic-mode runtime and player-queues test harness."""

    class WatchdogRuntime(AIRadioRuntimeMixin):
        def __init__(self) -> None:
            self.logger = logging.getLogger("tests.ai_radio.runtime.watchdog")
            self._sessions: dict[str, SessionState] = {}

        async def _fetch_source_tracks(
            self, station: dict[str, Any]
        ) -> tuple[list[dict[str, Any]], str]:
            tracks = []
            for index in (1, 2, 3):
                tracks.append(
                    {
                        "uri": f"library://track/{index}",
                        "duration": 400,
                        "name": f"T{index}",
                        "artist": "A",
                        "songinfo": f"A - T{index}",
                        "index": index - 1,
                        "playlist_item": None,
                        "item_id": str(index),
                    }
                )
            return tracks, "Source"

        def _apply_track_duration_limit(
            self, tracks: list[dict[str, Any]], station: dict[str, Any]
        ) -> list[dict[str, Any]]:
            return tracks

        def _plan_sections(self, *args: Any, **kwargs: Any) -> tuple[list[Any], dict[str, Any]]:
            return [], {}

        async def _generate_sections(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def _synthesize_sections(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def _prepare_runtime_tokens(self, station: dict[str, Any]) -> dict[str, str]:
            return {}

    class SteppingPlayerQueues:
        """Player queues stub whose queue reports a scripted playback state."""

        def __init__(self, queue: Any) -> None:
            self.queue = queue

        def clear(self, queue_id: str) -> None:
            return None

        async def play_media(self, **kwargs: Any) -> None:
            return None

        def get_active_queue(self, player_id: str) -> Any:
            return None

        def get(self, queue_id: str) -> Any:
            self.queue.polls += 1
            return self.queue

    return WatchdogRuntime, SteppingPlayerQueues


def _watchdog_station() -> dict[str, Any]:
    """Return a dynamic-mode station config for watchdog tests."""
    return {
        "id": "st",
        "name": "Watchdog Station",
        "default_player_id": "living_room",
        "dynamic_batch_size": 2,
        "dynamic_poll_seconds": 1,
        "dynamic_prefetch_remaining_tracks": 1,
        "clear_queue_on_start": False,
        "general": {"timezone": "UTC"},
        "sections": [],
        "section_order": [],
    }


async def test_dynamic_watchdog_tolerates_long_track_while_playing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep waiting while a long track plays and elapsed time keeps advancing."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS",
        0.2,
    )
    watchdog_runtime_cls, stepping_queues_cls = _make_watchdog_runtime_classes()

    class PlayingQueue:
        """Queue stuck on index 0 (long track) but with advancing elapsed time."""

        polls = 0
        state = PlaybackState.PLAYING

        @property
        def current_index(self) -> int:
            return 0 if self.polls < 4 else 1

        @property
        def elapsed_time(self) -> float:
            return float(self.polls * 10)

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    class DummyMass:
        players = DummyPlayers()
        player_queues = stepping_queues_cls(PlayingQueue())

    runtime = watchdog_runtime_cls()
    _set_runtime_mass(runtime, DummyMass())
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session

    result = await asyncio.wait_for(
        runtime._run_dynamic_mode(session, _watchdog_station()), timeout=10.0
    )

    assert result["queued_tracks"] == 3


class RecordingPlayerQueues:
    """Player queues stub that records queue mutations for assertions."""

    def __init__(self, queue: Any) -> None:
        """Initialize recorder around one fake queue object."""
        self.queue = queue
        self.clear_calls: list[str] = []
        self.play_media_calls: list[tuple[Any, int]] = []
        self.play_index_calls: list[tuple[str, int]] = []

    def clear(self, queue_id: str) -> None:
        """Record a queue clear call."""
        self.clear_calls.append(queue_id)

    async def play_media(self, **kwargs: Any) -> None:
        """Record a play_media call with the poll count at call time."""
        self.play_media_calls.append((kwargs.get("option"), self.queue.polls))

    async def play_index(self, queue_id: str, index: int) -> None:
        """Record a play_index call and mark the queue as started."""
        self.play_index_calls.append((queue_id, index))
        self.queue.started = True

    def get_active_queue(self, player_id: str) -> Any:
        """Return no active queue to force the direct queue lookup."""
        return None

    def get(self, queue_id: str) -> Any:
        """Return the fake queue and count the poll."""
        self.queue.polls += 1
        return self.queue


async def test_dynamic_mode_appends_to_playing_queue_when_clear_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Append batches after the existing items when clear_queue_on_start is off."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS",
        0.2,
    )
    watchdog_runtime_cls, _ = _make_watchdog_runtime_classes()

    class BusyQueue:
        """Queue already playing 3 pre-existing items."""

        polls = 0
        items = 3
        state = PlaybackState.PLAYING
        started = False

        @property
        def current_index(self) -> int:
            # trigger for the first radio batch sits at global index 4
            # (3 existing items + 1); stay on the old items for a few polls
            return 2 if self.polls < 4 else 4

        @property
        def elapsed_time(self) -> float:
            return float(self.polls * 10)

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    player_queues = RecordingPlayerQueues(BusyQueue())

    class DummyMass:
        def __init__(self) -> None:
            self.players = DummyPlayers()
            self.player_queues = player_queues

    mass = DummyMass()
    runtime = watchdog_runtime_cls()
    _set_runtime_mass(runtime, mass)
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session

    result = await asyncio.wait_for(
        runtime._run_dynamic_mode(session, _watchdog_station()), timeout=10.0
    )

    assert result["queued_tracks"] == 3
    assert player_queues.clear_calls == []
    options = [option for option, _ in player_queues.play_media_calls]
    assert options == [QueueOption.ADD, QueueOption.ADD]
    # second batch must wait for the trigger beyond the pre-existing items
    assert player_queues.play_media_calls[1][1] >= 4
    # queue was already playing: no forced playback start
    assert player_queues.play_index_calls == []


async def test_dynamic_mode_starts_playback_on_idle_queue_when_clear_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start playback at the first appended entry when the target queue is idle."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS",
        0.2,
    )
    watchdog_runtime_cls, _ = _make_watchdog_runtime_classes()

    class IdleQueue:
        """Idle queue with 3 leftover items and playback starting on demand."""

        polls = 0
        items = 3
        state = PlaybackState.IDLE
        elapsed_time = 0.0
        started = False

        @property
        def current_index(self) -> int | None:
            return 4 if self.started else None

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    player_queues = RecordingPlayerQueues(IdleQueue())

    class DummyMass:
        def __init__(self) -> None:
            self.players = DummyPlayers()
            self.player_queues = player_queues

    mass = DummyMass()
    runtime = watchdog_runtime_cls()
    _set_runtime_mass(runtime, mass)
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session

    result = await asyncio.wait_for(
        runtime._run_dynamic_mode(session, _watchdog_station()), timeout=10.0
    )

    assert result["queued_tracks"] == 3
    assert player_queues.play_index_calls == [("living_room", 3)]


async def test_dynamic_mode_targets_active_group_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue all batches on the active (group) queue when the player is a group member."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS",
        0.2,
    )
    watchdog_runtime_cls, _ = _make_watchdog_runtime_classes()

    class GroupQueue:
        """Group queue owned by the sync-group leader, not the target player."""

        queue_id = "group_1"
        polls = 0
        items = 0
        state = PlaybackState.PLAYING
        started = False
        current_index = 4
        elapsed_time = 10.0

    class GroupPlayerQueues:
        """Player queues stub where the player's active queue is a group queue."""

        def __init__(self, queue: Any) -> None:
            """Initialize recorder around the fake group queue."""
            self.queue = queue
            self.clear_calls: list[str] = []
            self.play_media_queue_ids: list[str] = []

        def clear(self, queue_id: str) -> None:
            """Record a queue clear call."""
            self.clear_calls.append(queue_id)

        async def play_media(self, **kwargs: Any) -> None:
            """Record the queue targeted by a play_media call."""
            self.play_media_queue_ids.append(kwargs.get("queue_id", ""))

        async def play_index(self, queue_id: str, index: int) -> None:
            """Start playback on the fake queue."""
            self.queue.started = True

        def get_active_queue(self, player_id: str) -> Any:
            """Return the group queue as the player's active queue."""
            return self.queue

        def get(self, queue_id: str) -> Any:
            """Return the fake group queue."""
            self.queue.polls += 1
            return self.queue

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    player_queues = GroupPlayerQueues(GroupQueue())

    class DummyMass:
        def __init__(self) -> None:
            self.players = DummyPlayers()
            self.player_queues = player_queues

    mass = DummyMass()
    runtime = watchdog_runtime_cls()
    _set_runtime_mass(runtime, mass)
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session
    station = {**_watchdog_station(), "clear_queue_on_start": True}

    result = await asyncio.wait_for(runtime._run_dynamic_mode(session, station), timeout=10.0)

    assert result["queue_id"] == "group_1"
    assert player_queues.clear_calls == ["group_1"]
    assert player_queues.play_media_queue_ids == ["group_1", "group_1"]


async def test_dynamic_watchdog_tolerates_paused_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep waiting while the queue is deliberately paused by the user."""
    monkeypatch.setattr(
        "music_assistant.providers.ai_radio.runtime.DEFAULT_DYNAMIC_STALL_TIMEOUT_SECONDS",
        0.2,
    )
    watchdog_runtime_cls, stepping_queues_cls = _make_watchdog_runtime_classes()

    class PausedQueue:
        """Queue paused on index 0 with frozen elapsed time, resuming later."""

        polls = 0
        elapsed_time = 42.0

        @property
        def state(self) -> PlaybackState:
            return PlaybackState.PAUSED if self.polls < 4 else PlaybackState.PLAYING

        @property
        def current_index(self) -> int:
            return 0 if self.polls < 4 else 1

    class DummyPlayers:
        def get_player(self, player_id: str) -> Any:
            return object()

    class DummyMass:
        players = DummyPlayers()
        player_queues = stepping_queues_cls(PausedQueue())

    runtime = watchdog_runtime_cls()
    _set_runtime_mass(runtime, DummyMass())
    session = SessionState(session_id="s1", station_id="st", mode="dynamic")
    runtime._sessions[session.session_id] = session

    result = await asyncio.wait_for(
        runtime._run_dynamic_mode(session, _watchdog_station()), timeout=10.0
    )

    assert result["queued_tracks"] == 3


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
    assert any("Track Two" in record.message for record in caplog.records)
