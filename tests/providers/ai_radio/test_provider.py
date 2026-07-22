"""Unit tests for AI Radio provider helper logic."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.ai_radio.constants import MAX_FINISHED_SESSIONS
from music_assistant.providers.ai_radio.models import SessionState
from music_assistant.providers.ai_radio.provider import AIRadioProvider


def _make_provider() -> AIRadioProvider:
    """Create a minimal AIRadioProvider object without full init."""
    provider = AIRadioProvider.__new__(AIRadioProvider)
    provider._sessions = {}
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
async def test_get_ui_settings_returns_default_refresh_interval() -> None:
    """Return sane default interval when setting is missing."""
    provider = _make_provider()
    provider.config = cast("Any", SimpleNamespace(get_value=lambda _key: None))

    result = await provider.get_ui_settings()

    assert result["auto_refresh_seconds"] == 2


@pytest.mark.asyncio
async def test_get_ui_settings_clamps_interval_to_minimum() -> None:
    """Clamp invalid refresh values to one second."""
    provider = _make_provider()
    provider.config = cast("Any", SimpleNamespace(get_value=lambda _key: 0))

    result = await provider.get_ui_settings()

    assert result["auto_refresh_seconds"] == 1


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
