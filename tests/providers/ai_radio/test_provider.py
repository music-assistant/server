"""Unit tests for AI Radio provider helper logic."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import EventType, PlaybackState, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    SetupFailedError,
)

from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine
from music_assistant.providers.ai_radio import provider as ai_radio_provider
from music_assistant.providers.ai_radio.constants import (
    CONF_AI_ENGINE,
    CONF_TTS_ENGINE,
    ENGINE_RETRY_DELAY,
    MAX_FINISHED_SESSIONS,
)
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


@pytest.fixture
def provider(tmp_path: Path) -> AIRadioProvider:
    """Build a minimal AIRadioProvider instance for host/station CRUD tests."""
    instance = AIRadioProvider.__new__(AIRadioProvider)
    instance.logger = logging.getLogger("test.ai_radio.provider")
    instance._station_lock = asyncio.Lock()
    instance._stations = {}
    instance._hosts = {}
    instance._hosts_file = tmp_path / "hosts.json"
    instance._sections = {item["id"]: item for item in instance._default_sections_template()}
    return instance


def test_resolve_session_for_stop_by_session_id() -> None:
    """Resolve explicit session id directly."""
    provider = _make_provider()
    session = SessionState(session_id="s1", station_id="st")
    provider._sessions[session.session_id] = session

    resolved = provider._resolve_session_for_stop(session_id="s1", station_id=None)

    assert resolved is session


def test_resolve_session_for_stop_uses_latest_running_for_station() -> None:
    """Resolve latest running session for selected station."""
    provider = _make_provider()
    older = SessionState(
        session_id="s_old",
        station_id="station_a",
        created_at="2026-01-01T10:00:00+00:00",
    )
    newer = SessionState(
        session_id="s_new",
        station_id="station_a",
        created_at="2026-01-01T11:00:00+00:00",
    )
    other = SessionState(
        session_id="s_other",
        station_id="station_b",
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

    with pytest.raises(InvalidDataError, match="requires a target player"):
        await provider.start_run(station_id="station_a")


@pytest.mark.asyncio
async def test_start_run_rejects_unknown_station() -> None:
    """Reject starting a run for a station that does not exist."""
    provider = _make_provider()
    provider._stations = {}

    with pytest.raises(KeyError, match="Unknown station id: missing_station"):
        await provider.start_run(station_id="missing_station")


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_unavailable_player() -> None:
    """Reject dynamic run start when configured player is unavailable."""
    unavailable_player = SimpleNamespace(player_id="living_room", available=False, enabled=True)
    provider = _make_dynamic_provider(
        player_obj=unavailable_player,
        default_player_id="living_room",
    )

    with pytest.raises(InvalidDataError, match="Target player is unavailable"):
        await provider.start_run(station_id="station_a")


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_negative_source_playtime_cap_override() -> None:
    """Reject dynamic run start when source playtime cap override is negative."""
    provider = _make_dynamic_provider(player_obj=None, default_player_id="")

    with pytest.raises(InvalidDataError, match="dynamic_source_playtime_cap_override must be >= 0"):
        await provider.start_run(
            station_id="station_a",
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
        await provider.start_run(station_id="station_a")


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
    player = SimpleNamespace(player_id="living_room", available=True, enabled=True)
    provider._stations = {
        "station_a": {
            "id": "station_a",
            "name": "Station A",
            "source_playlist_id": "1",
            "source_playlist_provider": "library",
            "default_player_id": "living_room",
        }
    }
    provider.mass = cast(
        "Any",
        SimpleNamespace(
            players=SimpleNamespace(get_player=lambda _player_id: player),
            create_task=lambda coro, **_kw: coro.close(),
        ),
    )
    for index in range(MAX_FINISHED_SESSIONS + 5):
        session_id = f"s_{index}"
        provider._sessions[session_id] = SessionState(
            session_id=session_id,
            station_id="station_a",
            status="completed",
            created_at=f"2026-01-01T10:{index:02d}:00+00:00",
        )

    await provider.start_run(station_id="station_a")

    finished = [s for s in provider._sessions.values() if s.status != "running"]
    assert len(finished) == MAX_FINISHED_SESSIONS
    # the five oldest sessions are gone, the newest finished ones remain
    for index in range(5):
        assert f"s_{index}" not in provider._sessions
    assert f"s_{MAX_FINISHED_SESSIONS + 4}" in provider._sessions


@pytest.mark.asyncio
async def test_validate_station_does_not_mutate_shared_sections() -> None:
    """Keep shared hosts and sections untouched when a station payload is only validated."""
    provider = _make_provider()
    provider._stations = {}
    provider._sections = {}
    provider._hosts = {"host_a": {"id": "host_a", "name": "Host A"}}
    provider._station_lock = asyncio.Lock()
    station = {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "playlist-1",
        "source_playlist_provider": "library",
        "host_id": "host_a",
    }

    normalized = await provider.validate_station(station)

    assert normalized["host_id"] == "host_a"
    assert provider._sections == {}
    assert provider._hosts == {"host_a": {"id": "host_a", "name": "Host A"}}


@pytest.mark.asyncio
async def test_host_crud_roundtrip(provider: Any) -> None:
    """Create, list, fetch and delete a host through the public CRUD API."""
    template = await provider.host_template()
    saved = await provider.save_host(template)
    assert saved["id"] == "default_host"
    assert [h["id"] for h in await provider.list_hosts()] == ["default_host"]
    fetched = await provider.get_host("default_host")
    assert fetched["name"] == saved["name"]
    await provider.delete_host("default_host")
    assert await provider.list_hosts() == []


@pytest.mark.asyncio
async def test_delete_host_refuses_when_station_references_it(provider: Any) -> None:
    """Refuse to delete a host that a station still references."""
    saved = await provider.save_host(await provider.host_template())
    provider._stations["station_a"] = {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "p1",
        "source_playlist_provider": "library",
        "default_player_id": "",
        "max_duration_minutes": 0.0,
        "shuffle_source_tracks": True,
        "host_id": saved["id"],
    }
    with pytest.raises(InvalidDataError):
        await provider.delete_host(saved["id"])


@pytest.mark.asyncio
async def test_concurrent_start_run_calls_respect_the_run_limit() -> None:
    """
    Concurrent start_run calls must not both get past the concurrency guards.

    The guards and the session insert are one critical section; without it an await
    introduced between them would let both callers observe zero running sessions.
    """
    player = SimpleNamespace(player_id="living_room", available=True, enabled=True)
    provider = _make_provider()
    provider.logger = logging.getLogger("tests.ai_radio.provider")
    provider._stations = {
        "station_a": {
            "id": "station_a",
            "name": "Station A",
            "default_player_id": "living_room",
        },
        "station_b": {
            "id": "station_b",
            "name": "Station B",
            "default_player_id": "living_room",
        },
    }
    provider.mass = cast(
        "Any",
        SimpleNamespace(
            players=SimpleNamespace(get_player=lambda _player_id: player),
            create_task=lambda coro, **_kw: _close(coro),
        ),
    )

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
        status="running",
        queue_id="living_room",
    )

    result = await provider.stop_run(session_id="s_run")

    assert result["status"] == "stopped"


def _make_engine_provider(
    plugins: list[Any],
    setup_values: dict[str, Any] | None = None,
) -> tuple[AIRadioProvider, list[Any], dict[str, Any]]:
    """
    Create a provider whose mass serves the given plugins.

    :return: The provider, the list its event subscribers land in, and the (plain text)
        stand-in for its stored setup_data.
    """
    provider = _make_provider()
    provider.config = cast("Any", SimpleNamespace(instance_id="ai_radio", setup_data={}))
    subscribers: list[Any] = []
    stored = dict(setup_values or {})

    def _subscribe(callback: Any, *_args: Any, **_kwargs: Any) -> Any:
        subscribers.append(callback)
        return lambda: subscribers.remove(callback)

    def _providers(feature: ProviderFeature, **_kwargs: Any) -> list[Any]:
        attribute = "get_ai_engines" if feature == ProviderFeature.AI_QUERY else "get_tts_engines"
        return [plugin for plugin in plugins if getattr(plugin, attribute).return_value]

    mass = MagicMock()
    mass.closing = False
    mass.subscribe.side_effect = _subscribe
    # mirror mass.create_task, which runs the coroutine up to its first suspension
    # before handing back the task
    mass.create_task.side_effect = lambda coro, **_kwargs: asyncio.Task(
        coro, loop=asyncio.get_running_loop(), eager_start=True
    )
    mass.get_providers_supporting_feature.side_effect = _providers
    mass.config.get_provider_setup_value.side_effect = lambda _instance_id, key, default=None: (
        stored.get(key, default)
    )
    provider.mass = cast("Any", mass)
    provider.logger = logging.getLogger("test.ai_radio")
    provider._unloading = False
    provider._engine_recheck_task = None
    provider._unregister_handles = []
    provider._update_setup_data = cast(  # type: ignore[method-assign]
        "Any", lambda key, value, **_kwargs: stored.__setitem__(key, value)
    )
    return provider, subscribers, stored


def _record_unload_with_error(provider: AIRadioProvider) -> list[Any]:
    """Capture the errors the provider unloads itself with instead of really unloading."""
    errors: list[Any] = []
    provider.unload_with_error = cast(  # type: ignore[method-assign]
        "Any", errors.append
    )
    return errors


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


async def test_wait_for_engines_seeds_a_concrete_selection() -> None:
    """An instance without a stored selection adopts concrete engines and waits for nothing."""
    provider, subscribers, stored = _make_engine_provider(
        [_make_engine_plugin("p1", ["ai"], ["tts"])]
    )

    await provider._wait_for_engines()

    assert stored == {CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    assert subscribers == []


async def test_wait_for_engines_keeps_an_existing_selection() -> None:
    """A stored selection survives the load instead of being reseeded to the first engine."""
    provider, _, stored = _make_engine_provider(
        [_make_engine_plugin("p1", ["ai", "other"], ["tts"])],
        setup_values={CONF_AI_ENGINE: "p1/other"},
    )

    await provider._wait_for_engines()

    assert stored[CONF_AI_ENGINE] == "p1/other"


async def test_wait_for_engines_fails_the_load_when_no_engine_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider without engines refuses to load so the UI offers a reconfigure."""
    provider, subscribers, stored = _make_engine_provider([])
    monkeypatch.setattr(ai_radio_provider, "ENGINE_DISCOVERY_TIMEOUT", 0.05)

    with pytest.raises(SetupFailedError) as error:
        async with asyncio.timeout(1):
            await provider._wait_for_engines()

    assert error.value.translation_key == "ai_radio_no_ai_engine"
    assert stored == {}
    assert subscribers == []


async def test_wait_for_engines_fails_when_only_the_tts_engine_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported error names the engine kind that is actually missing."""
    provider, _, _ = _make_engine_provider([_make_engine_plugin("p1", ["ai"], [])])
    monkeypatch.setattr(ai_radio_provider, "ENGINE_DISCOVERY_TIMEOUT", 0.05)

    with pytest.raises(SetupFailedError) as error:
        async with asyncio.timeout(1):
            await provider._wait_for_engines()

    assert error.value.translation_key == "ai_radio_no_tts_engine"


async def test_wait_for_engines_resumes_when_a_supplier_loads_later() -> None:
    """A plugin that finishes loading after AI Radio still satisfies the bounded wait."""
    plugins: list[Any] = []
    provider, subscribers, stored = _make_engine_provider(plugins)

    task = asyncio.create_task(provider._wait_for_engines())
    while not subscribers:
        await asyncio.sleep(0)
    plugins.append(_make_engine_plugin("p1", ["ai"], ["tts"]))
    for callback in list(subscribers):
        callback(MagicMock())

    await task

    assert stored == {CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    assert subscribers == []


async def test_wait_for_engines_rejects_a_configured_engine_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concrete selection that no longer exists is never substituted by another engine."""
    provider, _, stored = _make_engine_provider(
        [_make_engine_plugin("p1", ["ai"], ["tts"])],
        setup_values={CONF_AI_ENGINE: "p1/gone"},
    )
    monkeypatch.setattr(ai_radio_provider, "ENGINE_DISCOVERY_TIMEOUT", 0.05)

    with pytest.raises(SetupFailedError) as error:
        async with asyncio.timeout(1):
            await provider._wait_for_engines()

    assert error.value.translation_key == "ai_radio_no_ai_engine"
    assert stored[CONF_AI_ENGINE] == "p1/gone"


async def test_providers_updated_leaves_a_healthy_selection_alone() -> None:
    """A providers change that does not affect the engines is a no-op."""
    provider, _, _ = _make_engine_provider(
        [_make_engine_plugin("p1", ["ai"], ["tts"])],
        setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"},
    )
    errors = _record_unload_with_error(provider)

    await provider._on_providers_updated(MagicMock())

    assert provider._engine_recheck_task is None
    assert errors == []


async def test_providers_updated_unloads_when_an_engine_stays_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine removed after the load surfaces as a provider error instead of at playtime."""
    plugins: list[Any] = [_make_engine_plugin("p1", ["ai"], ["tts"])]
    provider, _, _ = _make_engine_provider(
        plugins,
        setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"},
    )
    errors = _record_unload_with_error(provider)
    monkeypatch.setattr(ai_radio_provider, "ENGINE_RECHECK_GRACE", 0.05)
    plugins.clear()

    await provider._on_providers_updated(MagicMock())
    assert provider._engine_recheck_task is not None
    async with asyncio.timeout(1):
        await provider._engine_recheck_task

    assert [error.translation_key for error in errors] == ["ai_radio_no_ai_engine"]


async def test_providers_updated_survives_a_supplier_reload() -> None:
    """A plugin reload briefly takes its engines with it, which must not unload AI Radio."""
    plugins: list[Any] = []
    provider, subscribers, _ = _make_engine_provider(
        plugins,
        setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"},
    )
    errors = _record_unload_with_error(provider)

    await provider._on_providers_updated(MagicMock())
    assert provider._engine_recheck_task is not None
    assert subscribers
    plugins.append(_make_engine_plugin("p1", ["ai"], ["tts"]))
    for callback in list(subscribers):
        callback(MagicMock())
    await provider._engine_recheck_task

    assert errors == []
    assert subscribers == []


async def test_providers_updated_ignored_while_closing() -> None:
    """The watch does not act on a providers change while the server is closing."""
    provider, _, _ = _make_engine_provider(
        [], setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    )
    errors = _record_unload_with_error(provider)
    cast("Any", provider.mass).closing = True

    await provider._on_providers_updated(MagicMock())

    assert provider._engine_recheck_task is None
    assert errors == []


async def test_providers_updated_ignored_while_unloading() -> None:
    """A providers change landing during our own unload is not acted on."""
    provider, _, _ = _make_engine_provider(
        [], setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    )
    errors = _record_unload_with_error(provider)
    provider._unloading = True

    await provider._on_providers_updated(MagicMock())

    assert provider._engine_recheck_task is None
    assert errors == []


async def test_providers_updated_keeps_a_single_recheck_in_flight() -> None:
    """Providers changes arriving during the grace period do not stack up rechecks."""
    plugins: list[Any] = []
    provider, subscribers, _ = _make_engine_provider(
        plugins,
        setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"},
    )
    _record_unload_with_error(provider)

    await provider._on_providers_updated(MagicMock())
    first_task = provider._engine_recheck_task
    assert first_task is not None
    assert subscribers
    await provider._on_providers_updated(MagicMock())

    assert provider._engine_recheck_task is first_task
    assert cast("Any", provider.mass).create_task.call_count == 1
    plugins.append(_make_engine_plugin("p1", ["ai"], ["tts"]))
    for callback in list(subscribers):
        callback(MagicMock())
    await first_task


async def test_loaded_in_mass_watches_the_loaded_providers() -> None:
    """Loading the provider wires up the engine watch, and unloading tears it down."""
    provider, subscribers, _ = _make_engine_provider(
        [_make_engine_plugin("p1", ["ai"], ["tts"])],
        setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"},
    )

    await provider.loaded_in_mass()

    assert cast("Any", provider.mass).subscribe.call_args.args == (
        provider._on_providers_updated,
        EventType.PROVIDERS_UPDATED,
    )
    assert subscribers == [provider._on_providers_updated]

    await provider.unload()

    assert subscribers == []


async def test_unload_cancels_an_in_flight_engine_recheck() -> None:
    """Unloading stops a running grace period instead of letting it report an error."""
    provider, subscribers, _ = _make_engine_provider(
        [], setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    )
    errors = _record_unload_with_error(provider)

    await provider._on_providers_updated(MagicMock())
    recheck_task = provider._engine_recheck_task
    assert recheck_task is not None
    assert subscribers
    await provider.unload()

    with pytest.raises(asyncio.CancelledError):
        await recheck_task
    assert provider._unloading is True
    assert errors == []


async def test_engine_recheck_stays_silent_when_the_provider_unloads_during_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unload surfacing as the wait's timeout is not reported as an engine error."""
    provider, _, _ = _make_engine_provider(
        [], setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    )
    errors = _record_unload_with_error(provider)
    monkeypatch.setattr(ai_radio_provider, "ENGINE_RECHECK_GRACE", 0.05)

    await provider._on_providers_updated(MagicMock())
    recheck_task = provider._engine_recheck_task
    assert recheck_task is not None
    provider._unloading = True
    await recheck_task

    assert errors == []
    assert cast("Any", provider.mass).call_later.call_count == 0


async def test_engine_recheck_stays_silent_when_the_server_closes_during_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown surfacing as the wait's timeout is not reported as an engine error."""
    provider, _, _ = _make_engine_provider(
        [], setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    )
    errors = _record_unload_with_error(provider)
    monkeypatch.setattr(ai_radio_provider, "ENGINE_RECHECK_GRACE", 0.05)

    await provider._on_providers_updated(MagicMock())
    recheck_task = provider._engine_recheck_task
    assert recheck_task is not None
    cast("Any", provider.mass).closing = True
    await recheck_task

    assert errors == []
    assert cast("Any", provider.mass).call_later.call_count == 0


async def test_engine_watchdog_arms_a_reload_after_unloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unload arms the reload that picks the provider back up once engines return."""
    provider, _, _ = _make_engine_provider(
        [], setup_values={CONF_AI_ENGINE: "p1/ai", CONF_TTS_ENGINE: "p1/tts"}
    )
    errors = _record_unload_with_error(provider)
    monkeypatch.setattr(ai_radio_provider, "ENGINE_RECHECK_GRACE", 0.05)

    await provider._on_providers_updated(MagicMock())
    recheck_task = provider._engine_recheck_task
    assert recheck_task is not None
    # the watch waits out the grace, not the (much shorter) discovery timeout of the load
    async with asyncio.timeout(1):
        await recheck_task

    assert [error.translation_key for error in errors] == ["ai_radio_no_ai_engine"]
    retry = cast("Any", provider.mass).call_later.call_args
    assert retry.args == (ENGINE_RETRY_DELAY, provider.mass.load_provider, "ai_radio")
    assert retry.kwargs == {"allow_retry": True, "task_id": "load_provider_ai_radio"}
