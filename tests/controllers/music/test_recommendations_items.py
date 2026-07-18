"""Tests for routing and security on the music/recommendations/items endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder, UniqueList

from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.media_items import BrowseFolder, MediaItemType


class _RowsProvider(MusicProvider):
    """A fake provider exposing one recommendation row with one item."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id="row1",
                provider=self.instance_id,
                name="Row 1",
                translation_key="row1_key",
                icon="mdi-row1",
            )
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        if item_id != "row1":
            return UniqueList()
        return UniqueList(
            [
                ItemMapping.from_dict(
                    {
                        "item_id": "prov-item",
                        "provider": self.instance_id,
                        "media_type": MediaType.TRACK.value,
                        "name": "Provider Item",
                    }
                )
            ]
        )


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


async def test_rows_interleaved_builtin_first(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builtin and provider rows are interleaved one per source per pass, builtin first."""
    prov_a = _build(_RowsProvider, instance_id="prov_a")
    prov_b = _build(_RowsProvider, instance_id="prov_b")
    monkeypatch.setattr(
        mass, "get_providers_supporting_feature", lambda *_a, **_k: [prov_a, prov_b]
    )
    folders = await mass.music.recommendations.get_recommendations()
    assert [f.provider for f in folders[:3]] == ["library", "prov_a", "prov_b"]
    assert all(f.provider == "library" for f in folders[3:])


async def test_items_routed_to_provider(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Items for a provider row are fetched from that provider."""
    provider = _build(_RowsProvider)
    monkeypatch.setattr(mass, "get_provider", lambda *_a, **_k: provider)
    items = await mass.music.recommendations.get_recommendation_items("fake_instance", "row1")
    assert [item.item_id for item in items] == ["prov-item"]


async def test_items_unknown_provider_row_returns_empty(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown row id on a known provider returns an empty list."""
    provider = _build(_RowsProvider)
    monkeypatch.setattr(mass, "get_provider", lambda *_a, **_k: provider)
    items = await mass.music.recommendations.get_recommendation_items(
        "fake_instance", "no_such_row"
    )
    assert items == []


async def test_items_unknown_provider_returns_empty(mass: MusicAssistant) -> None:
    """An unknown provider instance returns an empty list."""
    items = await mass.music.recommendations.get_recommendation_items("no_such_provider", "row1")
    assert items == []


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_items_restricted_provider_returns_empty(
    mock_get_user: Mock, mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's provider filter blocks fetching items from a restricted music provider."""
    mock_get_user.return_value = Mock(provider_filter=["allowed_instance"])
    provider = _build(_RowsProvider, instance_id="restricted_instance")
    provider.get_recommendation_items = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(mass, "get_provider", lambda *_a, **_k: provider)
    items = await mass.music.recommendations.get_recommendation_items("restricted_instance", "row1")
    assert items == []
    provider.get_recommendation_items.assert_not_awaited()
