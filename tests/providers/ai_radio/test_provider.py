"""Unit tests for AI Radio provider helper logic."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from music_assistant_models.errors import InvalidDataError

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
    provider.logger = cast("Any", SimpleNamespace(info=lambda *_a, **_kw: None))
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
    provider.logger = cast("Any", SimpleNamespace(info=lambda *_a, **_kw: None))
    provider._sessions["s_run"] = SessionState(
        session_id="s_run",
        station_id="st",
        mode="playlist",
        status="running",
    )

    result = await provider.stop_run(session_id="s_run")

    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_start_run_dynamic_rejects_zero_batch_size_override() -> None:
    """Reject dynamic run start when batch size override is zero."""
    player = SimpleNamespace(player_id="living_room", available=True, enabled=True)
    provider = _make_dynamic_provider(player_obj=player, default_player_id="living_room")
    provider.logger = cast("Any", SimpleNamespace(info=lambda *_a, **_kw: None))
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
