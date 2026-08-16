"""A slow or failing provider must never stall or break the recommendations endpoints."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import ProviderType
from music_assistant_models.media_items import RecommendationFolder, UniqueList

import music_assistant.controllers.music.recommendations.controller as rec_controller
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin
from music_assistant.providers.recommendations import LibraryRecommendationsProvider

if TYPE_CHECKING:
    from typing import Any

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


class _PayloadRowsProvider(MusicProvider, RecommendationPayloadMixin):
    """A fake provider serving rows via RecommendationPayloadMixin, fetch gated by an event."""

    gate: asyncio.Event
    payload: list[RecommendationFolder]
    fetch_count: int = 0

    async def get_recommendations(self) -> list[RecommendationFolder]:
        return await self._recommendation_rows_from_payload()

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        self.fetch_count += 1
        await self.gate.wait()
        return self.payload


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
    recommendations_provider = mass.get_provider("recommendations")
    assert isinstance(recommendations_provider, LibraryRecommendationsProvider)
    monkeypatch.setattr(
        mass,
        "get_providers_supporting_feature",
        lambda *_a, **_k: [hanging, healthy, recommendations_provider],
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
    recommendations_provider = mass.get_provider("recommendations")
    assert isinstance(recommendations_provider, LibraryRecommendationsProvider)
    monkeypatch.setattr(
        mass,
        "get_providers_supporting_feature",
        lambda *_a, **_k: [raising, recommendations_provider],
    )
    folders = await mass.music.recommendations.get_recommendations()
    assert "recently_played" in {f.item_id for f in folders}
    assert not any(f.provider == "raising" for f in folders)


async def test_provider_items_timeout_returns_empty(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that never returns its items yields an empty list once the timeout elapses."""
    monkeypatch.setattr(rec_controller, "RECOMMENDATIONS_ITEMS_TIMEOUT", 0.05)
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


async def test_payload_mixin_rows_timeout_does_not_cancel_shared_fetch(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The controller's rows timeout degrades gracefully without killing the shared fetch.

    A RecommendationPayloadMixin provider's shared payload fetch outlives the controller's
    own RECOMMENDATIONS_ROWS_TIMEOUT: the timed-out rows call returns without this provider's
    rows, but the fetch keeps running in the background, warms the cache, and a later rows
    call serves it without hitting the backend again.
    """
    monkeypatch.setattr(rec_controller, "RECOMMENDATIONS_ROWS_TIMEOUT", 0.05)
    gate = asyncio.Event()
    provider = _build(_PayloadRowsProvider, instance_id="payload_prov")
    assert isinstance(provider, _PayloadRowsProvider)
    provider.gate = gate
    provider.payload = [
        RecommendationFolder(
            item_id="payload_row",
            provider="payload_prov",
            name="Payload Row",
            translation_key="payload_row_key",
            icon="mdi-payload",
        )
    ]

    cache_store: dict[str, Any] = {}

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    background_tasks: list[asyncio.Future[Any]] = []

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    recommendations_provider = mass.get_provider("recommendations")
    assert isinstance(recommendations_provider, LibraryRecommendationsProvider)
    monkeypatch.setattr(
        mass,
        "get_providers_supporting_feature",
        lambda *_a, **_k: [provider, recommendations_provider],
    )

    folders = await mass.music.recommendations.get_recommendations()
    assert not any(f.provider == "payload_prov" for f in folders)
    assert any(f.provider == "recommendations" for f in folders)  # other rows unaffected
    assert provider.fetch_count == 1  # the fetch started but did not finish in time

    # release the gate: the shared fetch, still running in the background, completes
    gate.set()
    payload_task = provider._recommendation_payload_task
    assert payload_task is not None
    await payload_task
    await asyncio.gather(*background_tasks)  # let the cache-store task land

    # cache is warmed; a later rows call serves it without re-fetching the backend
    folders_after = await mass.music.recommendations.get_recommendations()
    assert any(f.provider == "payload_prov" and f.item_id == "payload_row" for f in folders_after)
    assert provider.fetch_count == 1
