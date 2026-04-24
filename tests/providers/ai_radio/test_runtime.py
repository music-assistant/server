"""Unit tests for AI Radio runtime session flow and logging."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, cast

import pytest
from music_assistant_models.background_task import BackgroundTask
from music_assistant_models.enums import MediaType, ProviderFeature, TaskStatus

from music_assistant.helpers.playlists import PlaylistItem, parse_m3u, parse_m3u_playlist_image
from music_assistant.helpers.uri import create_uri
from music_assistant.providers.ai_radio.models import AIRadioError, AudioSection, SessionState
from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin


class DummyRuntime(AIRadioRuntimeMixin):
    """Minimal runtime harness for testing mixin behavior."""

    def __init__(self) -> None:
        """Initialize minimal state for runtime tests."""
        self.logger = logging.getLogger("tests.ai_radio.runtime")
        self._sessions: dict[str, SessionState] = {}

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


def _set_runtime_mass(runtime: DummyRuntime, mass: Any) -> None:
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
        "general": {
            "weather_provider": "unsupported_provider",
            "location": {"city": "Berlin", "country": "DE"},
        },
    }

    with caplog.at_level(logging.WARNING):
        tokens = await runtime._prepare_runtime_tokens(station)

    assert tokens == {}
    assert any("Unsupported weather provider" in message for message in caplog.messages)


async def test_prepare_runtime_tokens_ignores_missing_location(caplog: Any) -> None:
    """Skip weather preparation when location data is incomplete."""
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
        "general": {
            "weather_provider": "open_meteo",
            "location": {"city": "", "country": "DE"},
        },
    }

    with caplog.at_level(logging.DEBUG):
        tokens = await runtime._prepare_runtime_tokens(station)

    assert tokens == {}
    assert any("no location configured" in message for message in caplog.messages)


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


async def test_render_tts_converts_raw_http_path_to_builtin_track_uri() -> None:
    """Wrap raw TTS HTTP paths as builtin track URIs for queue progression."""

    class DummyStreamDetails:
        def __init__(self, path: str) -> None:
            self.path = path

    class DummyTTSPlugin:
        instance_id = "hass_1"

        async def get_tts_message(self, message: str) -> DummyStreamDetails:
            return DummyStreamDetails("http://example.test/api/tts_proxy/abc123.mp3")

    class DummyMass:
        def get_plugins_by_feature(self, feature: ProviderFeature) -> list[Any]:
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

    uri = await runtime._render_tts("hello world")

    assert uri == create_uri(
        MediaType.TRACK,
        "builtin_1",
        "http://example.test/api/tts_proxy/abc123.mp3",
    )


async def test_generate_text_wraps_not_connected_error() -> None:
    """Raise an actionable AIRadioError when AI provider is disconnected."""

    class NotConnected(Exception):
        """Match hass_client NotConnected exception name."""

    class DummyAIPlugin:
        instance_id = "hass_1"

        async def ai_query(self, query: str) -> str:
            raise NotConnected

    class DummyMass:
        def get_plugins_by_feature(self, feature: ProviderFeature) -> list[Any]:
            if feature == ProviderFeature.AI_QUERY:
                return [DummyAIPlugin()]
            return []

    runtime = DummyRuntime()
    _set_runtime_mass(runtime, DummyMass())

    with pytest.raises(AIRadioError) as error:
        await runtime._generate_text(
            station={"general": {"instructions": "test"}},
            prompt="test prompt",
            web_mode="disabled",
        )
    assert "not connected" in str(error.value).lower()
    assert "hass_1" in str(error.value)


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
                uri=create_uri(MediaType.TRACK, "builtin_1", section_url),
            )
        ],
    )

    assert track_count == 1
    assert len(items) == 2
    assert items[0].title == "AI Radio: Intro"
    assert items[0].metadata == {
        "media_type": MediaType.TRACK.value,
        "name": "AI Radio: Intro",
    }
    assert items[0].images
    assert items[0].images[0].path == runtime._ai_radio_cover_image_path()
    assert items[0].providers
    assert items[0].providers[0].domain == "builtin"
    assert items[0].providers[0].instance_id == "builtin_1"
    assert items[0].providers[0].item_id == section_url
    assert items[1] == source_item


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
            uri=create_uri(MediaType.TRACK, "builtin", "http://example.test/intro.mp3"),
        )
    )

    playlist = await runtime._import_builtin_playlist("AI Radio: Test", [item])

    assert playlist == {"item_id": "42", "name": "AI Radio: Test"}
    assert parse_m3u_playlist_image(mass.music.playlists.m3u_data) == (
        runtime._ai_radio_cover_image_path()
    )
    parsed = parse_m3u(mass.music.playlists.m3u_data)
    assert parsed[0].title == "AI Radio: Intro"
    assert parsed[0].metadata == {
        "media_type": MediaType.TRACK.value,
        "name": "AI Radio: Intro",
    }
    assert parsed[0].images[0].path == runtime._ai_radio_cover_image_path()


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
