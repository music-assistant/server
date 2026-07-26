"""Tests for MusicAssistant.unload_provider_with_error error preservation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.errors import LoginFailed

from music_assistant.mass import MusicAssistant

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.config_entries import ProviderError


def _make_mass(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MusicAssistant, list[ProviderError], AsyncMock]:
    """Return a bare MusicAssistant (bypassing __init__) recording last-error writes."""
    mass = object.__new__(MusicAssistant)
    recorded: list[ProviderError] = []
    config = MagicMock()
    config.update_provider_last_error = MagicMock(
        side_effect=lambda _instance_id, error: recorded.append(error)
    )
    unload = AsyncMock()
    monkeypatch.setattr(mass, "config", config, raising=False)
    monkeypatch.setattr(mass, "unload_provider", unload)
    return mass, recorded, unload


async def test_unload_provider_with_error_preserves_auth_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LoginFailed keeps its error code + translation so the provider shows AUTH_REQUIRED."""
    mass, recorded, unload = _make_mass(monkeypatch)
    await mass.unload_provider_with_error("spotify--1", LoginFailed("token revoked"))
    assert recorded[0].error_code == LoginFailed.error_code
    assert recorded[0].translation_key == LoginFailed.translation_key
    unload.assert_awaited_once_with("spotify--1")


async def test_unload_provider_with_error_string_is_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain string message is recorded as a generic error (code 999)."""
    mass, recorded, _unload = _make_mass(monkeypatch)
    await mass.unload_provider_with_error("airplay--1", "daemon failed to start")
    assert recorded[0].error_code == 999
    assert recorded[0].message == "daemon failed to start"
