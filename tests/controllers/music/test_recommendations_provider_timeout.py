"""A slow or failing provider must never stall or break the recommendations endpoint."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.enums import ProviderType

import music_assistant.controllers.music.recommendations.controller as rec_controller
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.media_items import RecommendationFolder


class _HangingProvider(MusicProvider):
    async def recommendations(self) -> list[RecommendationFolder]:
        await asyncio.sleep(3600)
        return []


class _RaisingProvider(MusicProvider):
    async def recommendations(self) -> list[RecommendationFolder]:
        raise RuntimeError("provider boom")


def _build(provider_cls: type[MusicProvider]) -> MusicProvider:
    """Construct a minimal provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "fake"
    config = MagicMock()
    config.name = "Fake Provider"
    config.instance_id = "fake_instance"
    config.get_value = MagicMock(return_value="GLOBAL")
    return provider_cls(mass, manifest, config, supported_features=set())


async def test_provider_recommendations_timeout_returns_empty(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that never returns recommendations is skipped once the timeout elapses."""
    monkeypatch.setattr(rec_controller, "RECOMMENDATIONS_PROVIDER_TIMEOUT", 0.05)
    result = await mass.music.recommendations._provider_recommendations(_build(_HangingProvider))
    assert result == []


async def test_provider_recommendations_error_isolated(mass: MusicAssistant) -> None:
    """A raising provider is isolated to an empty list, not propagated."""
    result = await mass.music.recommendations._provider_recommendations(_build(_RaisingProvider))
    assert result == []
