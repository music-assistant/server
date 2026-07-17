"""Tests for the `wanted` row filter on music/recommendations."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import ItemMapping, RecommendationFolder

from music_assistant.controllers.music.recommendations.sources.base import (
    CallableRecommendationSource,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import pytest


class _RowProviderA(MusicProvider):
    """A fake provider whose recommendations() returns a single row."""

    async def recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id="row1",
                provider=self.instance_id,
                name="Row 1",
                translation_key="row1_key",
                icon="mdi-row1",
            )
        ]


class _OptInProvider(MusicProvider):
    """A fake provider that opts in to the per-provider `wanted` item_id set."""

    received_wanted: set[str] | None | bool = False

    async def recommendations(self, wanted: set[str] | None = None) -> list[RecommendationFolder]:
        self.received_wanted = wanted
        return []


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


async def test_wanted_skips_provider_with_no_wanted_rows(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider with no wanted rows is filtered out before it can be awaited at all."""
    provider_a = _build(_RowProviderA, instance_id="prov_a")
    provider_b = _build(_RowProviderA, instance_id="prov_b")
    provider_b.recommendations = AsyncMock()  # type: ignore[method-assign]

    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [provider_a, provider_b]
    )

    wanted_uri = create_uri(MediaType.FOLDER, "prov_a", "row1")
    folders = await mass.music.recommendations.get_recommendations(wanted=[wanted_uri])

    provider_b.recommendations.assert_not_awaited()
    assert [f.uri for f in folders] == [wanted_uri]


async def test_wanted_passes_per_provider_item_ids_to_opt_in_provider(
    mass: MusicAssistant,
) -> None:
    """A provider whose recommendations() accepts `wanted` receives the per-provider item_id set."""
    provider = cast("_OptInProvider", _build(_OptInProvider, instance_id="opt_in"))
    wanted_uri = create_uri(MediaType.FOLDER, "opt_in", "home")

    await mass.music.recommendations._provider_recommendations(provider, {wanted_uri})

    assert provider.received_wanted == {"home"}


async def test_wanted_filters_library_sources(mass: MusicAssistant) -> None:
    """Only the wanted library source is built; other sources' factories are never called."""
    factory_calls = {"x": 0, "y": 0}

    async def _factory_x() -> list[ItemMapping]:
        factory_calls["x"] += 1
        return []

    async def _factory_y() -> list[ItemMapping]:
        factory_calls["y"] += 1
        return []

    source_x = CallableRecommendationSource(
        mass,
        item_id="wanted_row",
        name="Wanted",
        translation_key="wanted_key",
        icon="mdi-wanted",
        items_factory=_factory_x,
    )
    source_y = CallableRecommendationSource(
        mass,
        item_id="unwanted_row",
        name="Unwanted",
        translation_key="unwanted_key",
        icon="mdi-unwanted",
        items_factory=_factory_y,
    )
    mass.music.recommendations.register(source_x)
    mass.music.recommendations.register(source_y)

    wanted_uri = create_uri(MediaType.FOLDER, "library", "wanted_row")
    folders = await mass.music.recommendations.get_recommendations(wanted=[wanted_uri])

    assert factory_calls == {"x": 1, "y": 0}
    assert [f.item_id for f in folders] == ["wanted_row"]
