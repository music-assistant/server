"""Unit tests for AI Radio provider helper logic."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    SetupFailedError,
)

from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine
from music_assistant.providers.ai_radio import provider as ai_radio_provider
from music_assistant.providers.ai_radio.constants import CONF_AI_ENGINE, MAX_FINISHED_SESSIONS
from music_assistant.providers.ai_radio.models import SessionState
from music_assistant.providers.ai_radio.provider import AIRadioProvider


def _close(coro: Any) -> None:
    """Close an un-awaited coroutine so the test does not warn."""
    coro.close()


def _recording_stop(recorder: list[str]) -> Any:
    """Return an async queue-stop stub that records the queue ids it was called with."""

    async def _stop(queue_id: str) -> None:
        recorder.append(queue_id)

    return _stop


def _make_provider() -> AIRadioProvider:
    """Create a minimal AIRadioProvider object without full init."""
    provider = AIRadioProvider.__new__(AIRadioProvider)
    provider._sessions = {}
    provider._session_lock = asyncio.Lock()
    return provider


def _make_dynamic_provider(player_obj: object | None, default_player_id: str) -> AIRadioProvider:
    """Create a minimal provider object suitable for dynamic-mode validation tests."""
    provider = _make_provider()
    provider._stations = {
        "station_a": {
            "id": "station_a",
            "name": "Station A",
            "source_playlist_id": "1",
            "source_playlist_provider": "library",
            "default_player_id": default_player_id,
        }
    }
    provider.mass = cast(
        "Any",
        SimpleNamespace(
            players=SimpleNamespace(get_player=lambda _player_id: player_obj),
        ),
    )
    return provider


def test_resolve_session_for_stop_by_session_id() -> None:
    """Resolve explicit session id directly."""
    provider = _make_provider()
    session = SessionState(session_id="s1", station_id="st", mode="playlist")
    provider._sessions[session.session_id] = session

    resolved = provider._resolve_session_for_stop(session_id="s1", station_id=None)

    assert resolved is session


def test_resolve_session_for_stop_uses_latest_running_for_station() -> None:
    """Resolve latest running session for selected station."""
    provider = _make_provider()
    older = SessionState(
        session_id="s_old",
        station_id="station_a",
        mode="playlist",
        created_at="2026-01-01T10:00:00+00:00",
    )
    newer = SessionState(
        session_id="s_new",
        station_id="station_a",
        mode="dynamic",
        created_at="2026-01-01T11:00:00+00:00",
    )
    other = SessionState(
        session_id="s_other",
        station_id="station_b",
        mode="playlist",
        created_at="2026-01-01T12:00:00+00:00",
    )
    provider._sessions = {s.session_id: s for s in (older, newer, other)}

    resolved = provider._resolve_session_for_stop(session_id=None, station_id="station_a")

    assert resolved.session_id == "s_new"


def test_resolve_session_for_stop_raises_when_nothing_running() -> None:
    """Raise when no running sessions exist."""
    provider = _make_provider()

    with pytest.raises(KeyError, match="No active AI Radio run found"):
        provider._resolve_session_for_stop(session_id=None, station_id=None)


@pytest.mark.asyncio
async def test_start_run_dynamic_requires_player_id() -> None:
    """Reject dynamic run start when no player is configured."""
    provider = _make_dynamic_provider(player_obj=None, default_player_id="")

    with pytest.raises(InvalidDataError, match="requires a target player_id"):
        await provider.start_run(station_id="station_a", mode="dynamic")


@pytest.mark.asyncio
async def test_start_run_rejects_unknown_station() -> None:
    """Reject starting a run for a station that does not exist."""
    provider = _make_provider()
    provider._stations = {}

    with pytest.raises(KeyError, match="Unknown station id: missing_station"):
        await provider.start_run(station_id="missing_station", mode="playlist")


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_unavailable_player() -> None:
    """Reject dynamic run start when configured player is unavailable."""
    unavailable_player = SimpleNamespace(player_id="living_room", available=False, enabled=True)
    provider = _make_dynamic_provider(
        player_obj=unavailable_player,
        default_player_id="living_room",
    )

    with pytest.raises(InvalidDataError, match="Target player is unavailable"):
        await provider.start_run(station_id="station_a", mode="dynamic")


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_negative_source_playtime_cap_override() -> None:
    """Reject dynamic run start when source playtime cap override is negative."""
    provider = _make_dynamic_provider(player_obj=None, default_player_id="")

    with pytest.raises(InvalidDataError, match="dynamic_source_playtime_cap_override must be >= 0"):
        await provider.start_run(
            station_id="station_a",
            mode="dynamic",
            dynamic_source_playtime_cap_override=-1,
        )


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_disabled_player() -> None:
    """Reject dynamic run start when target player is disabled."""
    disabled_player = SimpleNamespace(player_id="living_room", available=True, enabled=False)
    provider = _make_dynamic_provider(
        player_obj=disabled_player,
        default_player_id="living_room",
    )

    with pytest.raises(InvalidDataError, match="Target player is disabled"):
        await provider.start_run(station_id="station_a", mode="dynamic")


@pytest.mark.asyncio
async def test_stop_run_rejects_already_completed_session() -> None:
    """Reject stopping a session that is already completed."""
    provider = _make_provider()
    provider.logger = cast(
        "Any",
        SimpleNamespace(debug=lambda *_a, **_kw: None, info=lambda *_a, **_kw: None),
    )
    provider._sessions["s_done"] = SessionState(
        session_id="s_done",
        station_id="st",
        mode="playlist",
        status="completed",
        ended_at="2026-01-01T10:00:00+00:00",
    )

    with pytest.raises(InvalidDataError):
        await provider.stop_run(session_id="s_done")


@pytest.mark.asyncio
async def test_stop_run_accepts_running_session_by_id() -> None:
    """Stop a running session resolved by explicit session id."""
    provider = _make_provider()
    provider.logger = cast(
        "Any",
        SimpleNamespace(debug=lambda *_a, **_kw: None, info=lambda *_a, **_kw: None),
    )
    provider._sessions["s_run"] = SessionState(
        session_id="s_run",
        station_id="st",
        mode="playlist",
        status="running",
    )

    result = await provider.stop_run(session_id="s_run")

    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_start_run_prunes_oldest_finished_sessions() -> None:
    """Drop the oldest finished sessions beyond the retention limit on run start."""
    provider = _make_provider()
    provider.logger = cast(
        "Any",
        SimpleNamespace(debug=lambda *_a, **_kw: None, info=lambda *_a, **_kw: None),
    )
    provider._stations = {
        "station_a": {
            "id": "station_a",
            "name": "Station A",
            "source_playlist_id": "1",
            "source_playlist_provider": "library",
        }
    }
    provider.mass = cast(
        "Any",
        SimpleNamespace(create_task=lambda coro, **_kw: coro.close()),
    )
    for index in range(MAX_FINISHED_SESSIONS + 5):
        session_id = f"s_{index}"
        provider._sessions[session_id] = SessionState(
            session_id=session_id,
            station_id="station_a",
            mode="playlist",
            status="completed",
            created_at=f"2026-01-01T10:{index:02d}:00+00:00",
        )

    await provider.start_run(station_id="station_a", mode="playlist")

    finished = [s for s in provider._sessions.values() if s.status != "running"]
    assert len(finished) == MAX_FINISHED_SESSIONS
    # the five oldest sessions are gone, the newest finished ones remain
    for index in range(5):
        assert f"s_{index}" not in provider._sessions
    assert f"s_{MAX_FINISHED_SESSIONS + 4}" in provider._sessions


@pytest.mark.asyncio
async def test_validate_station_does_not_mutate_shared_sections() -> None:
    """Keep shared sections untouched when a station payload is only validated."""
    provider = _make_provider()
    provider._stations = {}
    provider._sections = {}
    provider._station_lock = asyncio.Lock()
    station = {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "playlist-1",
        "sections": [{"id": "s1", "name": "S1", "type": "ai_text", "prompt": "Prompt"}],
        "section_order": [{"when": "between_songs", "flow": [{"MUST": "s1"}]}],
    }

    normalized = await provider.validate_station(station)

    assert normalized["section_ids"] == ["s1"]
    assert provider._sections == {}


@pytest.mark.asyncio
async def test_save_station_discards_section_changes_when_station_invalid() -> None:
    """Roll back section upserts when the station payload fails validation."""
    provider = _make_provider()
    provider._stations = {}
    provider._sections = {
        "s1": {"id": "s1", "name": "S1", "type": "ai_text", "prompt": "Original prompt"}
    }
    provider._station_lock = asyncio.Lock()
    invalid_station = {
        "id": "station_a",
        "name": "",
        "source_playlist_id": "playlist-1",
        "sections": [{"id": "s1", "name": "S1", "type": "ai_text", "prompt": "Changed prompt"}],
        "section_order": [{"when": "between_songs", "flow": [{"MUST": "s1"}]}],
    }

    with pytest.raises(InvalidDataError, match="Station name is required"):
        await provider.save_station(invalid_station)

    assert provider._sections["s1"]["prompt"] == "Original prompt"


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_zero_batch_size_override() -> None:
    """Reject dynamic run start when batch size override is zero."""
    player = SimpleNamespace(player_id="living_room", available=True, enabled=True)
    provider = _make_dynamic_provider(player_obj=player, default_player_id="living_room")
    provider.logger = cast(
        "Any",
        SimpleNamespace(debug=lambda *_a, **_kw: None, info=lambda *_a, **_kw: None),
    )
    provider.mass = cast(
        "Any",
        SimpleNamespace(
            players=SimpleNamespace(get_player=lambda _player_id: player),
            create_task=lambda *_a, **_kw: None,
        ),
    )

    with pytest.raises(InvalidDataError, match="dynamic_batch_size_override"):
        await provider.start_run(
            station_id="station_a",
            mode="dynamic",
            dynamic_batch_size_override=0,
        )


@pytest.mark.asyncio
async def test_concurrent_start_run_calls_respect_the_run_limit() -> None:
    """
    Concurrent start_run calls must not both get past the concurrency guards.

    The guards and the session insert are one critical section; without it an await
    introduced between them would let both callers observe zero running sessions.
    """
    provider = _make_provider()
    provider.logger = logging.getLogger("tests.ai_radio.provider")
    provider._stations = {
        "station_a": {"id": "station_a", "name": "Station A"},
        "station_b": {"id": "station_b", "name": "Station B"},
    }
    provider.mass = cast("Any", SimpleNamespace(create_task=lambda coro, **_kw: _close(coro)))

    results = await asyncio.gather(
        provider.start_run(station_id="station_a"),
        provider.start_run(station_id="station_b"),
        return_exceptions=True,
    )

    started = [item for item in results if isinstance(item, dict)]
    rejected = [item for item in results if isinstance(item, InvalidDataError)]
    assert len(started) == 1
    assert len(rejected) == 1
    assert "Max concurrent runs reached" in str(rejected[0])


@pytest.mark.asyncio
async def test_stop_run_stops_the_queue_it_owns() -> None:
    """Stop playback on the target queue when the show is stopped from the UI."""
    provider = _make_provider()
    provider.logger = cast(
        "Any",
        SimpleNamespace(debug=lambda *_a, **_kw: None, info=lambda *_a, **_kw: None),
    )
    stopped: list[str] = []
    provider.mass = cast(
        "Any",
        SimpleNamespace(
            player_queues=SimpleNamespace(
                get=lambda _queue_id: SimpleNamespace(state=PlaybackState.PLAYING, current_index=3),
                stop=_recording_stop(stopped),
            )
        ),
    )
    provider._sessions["s_run"] = SessionState(
        session_id="s_run",
        station_id="st",
        mode="dynamic",
        status="running",
        queue_id="living_room",
    )

    result = await provider.stop_run(session_id="s_run")

    assert result["status"] == "stopped"
    assert stopped == ["living_room"]


@pytest.mark.asyncio
async def test_stop_run_survives_an_unavailable_player() -> None:
    """Still mark the session stopped when the target player has gone away."""
    provider = _make_provider()
    provider.logger = cast(
        "Any",
        SimpleNamespace(debug=lambda *_a, **_kw: None, info=lambda *_a, **_kw: None),
    )

    async def _raise(queue_id: str) -> None:
        raise PlayerUnavailableError(f"Player {queue_id} is not available")

    provider.mass = cast(
        "Any",
        SimpleNamespace(
            player_queues=SimpleNamespace(
                get=lambda _queue_id: SimpleNamespace(state=PlaybackState.PLAYING, current_index=3),
                stop=_raise,
            )
        ),
    )
    provider._sessions["s_run"] = SessionState(
        session_id="s_run",
        station_id="st",
        mode="dynamic",
        status="running",
        queue_id="living_room",
    )

    result = await provider.stop_run(session_id="s_run")

    assert result["status"] == "stopped"


def _make_engine_provider(
    plugins: list[Any],
    setup_values: dict[str, Any] | None = None,
) -> tuple[AIRadioProvider, list[Any]]:
    """Create a provider whose mass serves the given plugins, plus its event subscribers."""
    provider = _make_provider()
    subscribers: list[Any] = []

    def _subscribe(callback: Any, *_args: Any, **_kwargs: Any) -> Any:
        subscribers.append(callback)
        return lambda: subscribers.remove(callback)

    def _providers(feature: ProviderFeature, **_kwargs: Any) -> list[Any]:
        attribute = "get_ai_engines" if feature == ProviderFeature.AI_QUERY else "get_tts_engines"
        return [plugin for plugin in plugins if getattr(plugin, attribute).return_value]

    mass = MagicMock()
    mass.subscribe.side_effect = _subscribe
    mass.get_providers_supporting_feature.side_effect = _providers
    provider.mass = cast("Any", mass)
    values = setup_values or {}
    provider.get_setup_value = cast(  # type: ignore[method-assign]
        "Any", lambda key, default=None: values.get(key, default)
    )
    return provider, subscribers


def _make_engine_plugin(instance_id: str, ai_ids: list[str], tts_ids: list[str]) -> MagicMock:
    """Create a mock plugin provider exposing the given AI and TTS engines."""
    plugin = MagicMock(spec=PluginProvider)
    plugin.instance_id = instance_id
    plugin.get_ai_engines = AsyncMock(
        return_value=[AIEngine(id=engine, name=engine, provider=plugin) for engine in ai_ids]
    )
    plugin.get_tts_engines = AsyncMock(
        return_value=[TTSEngine(id=engine, name=engine, provider=plugin) for engine in tts_ids]
    )
    return plugin


async def test_wait_for_engines_returns_immediately_when_both_resolve() -> None:
    """An already-loaded engine supplier costs no wait and leaves no subscription behind."""
    provider, subscribers = _make_engine_provider([_make_engine_plugin("p1", ["ai"], ["tts"])])

    await provider._wait_for_engines()

    assert subscribers == []


async def test_wait_for_engines_fails_the_load_when_no_engine_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider without engines refuses to load so the UI offers a reconfigure."""
    provider, subscribers = _make_engine_provider([])
    monkeypatch.setattr(ai_radio_provider, "ENGINE_DISCOVERY_TIMEOUT", 0.05)

    with pytest.raises(SetupFailedError) as error:
        await provider._wait_for_engines()

    assert error.value.translation_key == "ai_radio_no_ai_engine"
    assert subscribers == []


async def test_wait_for_engines_fails_when_only_the_tts_engine_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported error names the engine kind that is actually missing."""
    provider, _ = _make_engine_provider([_make_engine_plugin("p1", ["ai"], [])])
    monkeypatch.setattr(ai_radio_provider, "ENGINE_DISCOVERY_TIMEOUT", 0.05)

    with pytest.raises(SetupFailedError) as error:
        await provider._wait_for_engines()

    assert error.value.translation_key == "ai_radio_no_tts_engine"


async def test_wait_for_engines_resumes_when_a_supplier_loads_later() -> None:
    """A plugin that finishes loading after AI Radio still satisfies the bounded wait."""
    plugins: list[Any] = []
    provider, subscribers = _make_engine_provider(plugins)

    task = asyncio.create_task(provider._wait_for_engines())
    while not subscribers:
        await asyncio.sleep(0)
    plugins.append(_make_engine_plugin("p1", ["ai"], ["tts"]))
    for callback in list(subscribers):
        callback(MagicMock())

    await task

    assert subscribers == []


async def test_wait_for_engines_rejects_a_configured_engine_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concrete selection that no longer exists is never substituted by another engine."""
    provider, _ = _make_engine_provider(
        [_make_engine_plugin("p1", ["ai"], ["tts"])],
        setup_values={CONF_AI_ENGINE: "p1/gone"},
    )
    monkeypatch.setattr(ai_radio_provider, "ENGINE_DISCOVERY_TIMEOUT", 0.05)

    with pytest.raises(SetupFailedError) as error:
        await provider._wait_for_engines()

    assert error.value.translation_key == "ai_radio_no_ai_engine"
