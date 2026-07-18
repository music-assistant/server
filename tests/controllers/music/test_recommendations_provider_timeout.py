"""A slow or failing provider must never stall or break the recommendations endpoints."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.enums import ProviderType
from music_assistant_models.media_items import RecommendationFolder, UniqueList

import music_assistant.controllers.music.recommendations.controller as rec_controller
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType


class _HangingRowsProvider(MusicProvider):
    async def get_recommendations(self) -> list[RecommendationFolder]:
        await asyncio.sleep(3600)
        return []


class _RaisingRowsProvider(MusicProvider):
    async def get_recommendations(self) -> list[RecommendationFolder]:
        raise RuntimeError("provider boom")


class _HealthyRowsProvider(MusicProvider):
    async def get_recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id="healthy_row",
                provider=self.instance_id,
                name="Healthy Row",
                translation_key="healthy_row_key",
                icon="mdi-healthy",
            )
        ]


class _HangingItemsProvider(MusicProvider):
    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        await asyncio.sleep(3600)
        return UniqueList()


class _RaisingItemsProvider(MusicProvider):
    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        raise RuntimeError("provider boom")


def _build(provider_cls: type[MusicProvider], instance_id: str = "fake_instance") -> MusicProvider:
    """Construct a minimal provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "fake"
    config = MagicMock()
    config.name = "Fake Provider"
    config.instance_id = instance_id
    config.get_value = MagicMock(return_value="GLOBAL")
    return provider_cls(mass, manifest, config, supported_features=set())


async def test_hanging_provider_rows_dropped_healthy_rows_kept(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that never returns its rows is skipped; healthy rows still return."""
    monkeypatch.setattr(rec_controller, "RECOMMENDATIONS_ROWS_TIMEOUT", 0.05)
    hanging = _build(_HangingRowsProvider, instance_id="hanging")
    healthy = _build(_HealthyRowsProvider, instance_id="healthy")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [hanging, healthy]
    )
    folders = await mass.music.recommendations.get_recommendations()
    item_ids = {f.item_id for f in folders}
    assert "healthy_row" in item_ids
    assert "recently_played" in item_ids  # builtin rows unaffected
    assert not any(f.provider == "hanging" for f in folders)


async def test_raising_provider_rows_isolated(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider whose rows call raises is isolated; other rows still return."""
    raising = _build(_RaisingRowsProvider, instance_id="raising")
    monkeypatch.setattr(mass, "get_providers_supporting_feature", lambda *_a, **_k: [raising])
    folders = await mass.music.recommendations.get_recommendations()
    assert "recently_played" in {f.item_id for f in folders}
    assert not any(f.provider == "raising" for f in folders)


async def test_provider_items_timeout_returns_empty(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that never returns its items yields an empty list once the timeout elapses."""
    monkeypatch.setattr(rec_controller, "RECOMMENDATIONS_PROVIDER_TIMEOUT", 0.05)
    hanging = _build(_HangingItemsProvider)
    monkeypatch.setattr(mass, "get_provider", lambda *_a, **_k: hanging)
    items = await mass.music.recommendations.get_recommendation_items("fake_instance", "row1")
    assert items == []


async def test_provider_items_error_returns_empty(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider whose items call raises yields an empty list, not an error."""
    raising = _build(_RaisingItemsProvider)
    monkeypatch.setattr(mass, "get_provider", lambda *_a, **_k: raising)
    items = await mass.music.recommendations.get_recommendation_items("fake_instance", "row1")
    assert items == []
