"""Tests for routing and security on the music/recommendations/items endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder, UniqueList

from music_assistant.controllers.music.recommendations.library import library_rows
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


class _FourRowsProvider(MusicProvider):
    """A fake provider exposing four recommendation rows."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id=f"four_row_{i}",
                provider=self.instance_id,
                name=f"Four Row {i}",
                translation_key=f"four_row_{i}_key",
                icon="mdi-four",
            )
            for i in range(4)
        ]


class _OneRowProvider(MusicProvider):
    """A fake provider exposing a single recommendation row."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        return [
            RecommendationFolder(
                item_id="one_row_0",
                provider=self.instance_id,
                name="One Row 0",
                translation_key="one_row_0_key",
                icon="mdi-one",
            )
        ]


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
    return provider_cls(
        mass, manifest, config, supported_features={ProviderFeature.RECOMMENDATIONS}
    )


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


async def test_rows_interleave_uneven_provider_lengths_no_tail_dropped(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Providers with fewer rows than the builtin sources still contribute all their rows."""
    four = _build(_FourRowsProvider, instance_id="four")
    one = _build(_OneRowProvider, instance_id="one")
    monkeypatch.setattr(mass, "get_providers_supporting_feature", lambda *_a, **_k: [four, one])

    folders = await mass.music.recommendations.get_recommendations()

    builtin_ids = [f.item_id for f in library_rows()]
    expected: list[tuple[str, str]] = []
    for i, builtin_id in enumerate(builtin_ids):
        expected.append(("library", builtin_id))
        if i < 4:
            expected.append(("four", f"four_row_{i}"))
        if i < 1:
            expected.append(("one", "one_row_0"))

    assert [(f.provider, f.item_id) for f in folders] == expected
    assert len(folders) == len(builtin_ids) + 4 + 1


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


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_rows_restricted_provider_returns_no_rows(
    mock_get_user: Mock, mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's provider filter excludes a restricted music provider's rows from the listing."""
    mock_get_user.return_value = Mock(provider_filter=["allowed_instance"])
    restricted = _build(_RowsProvider, instance_id="restricted_instance")
    monkeypatch.setattr(mass, "get_providers_supporting_feature", lambda *_a, **_k: [restricted])
    folders = await mass.music.recommendations.get_recommendations()
    assert not any(f.provider == "restricted_instance" for f in folders)
