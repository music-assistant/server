"""Tests for the global search on the music controller."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import ProviderMapping, SearchResults, Track

from music_assistant.controllers.music import MusicController
from music_assistant.models.music_provider import MusicProvider


def _make_track(item_id: str, provider: str, name: str) -> Track:
    """Return a minimal Track for the given provider."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
            )
        },
    )


def _make_search_provider(instance_id: str) -> Mock:
    """Return a mocked music provider that supports search."""
    prov = Mock(spec=MusicProvider)
    prov.instance_id = instance_id
    prov.domain = instance_id
    prov.name = instance_id
    prov.supported_features = {ProviderFeature.SEARCH}
    prov.search = AsyncMock(return_value=SearchResults())
    return prov


def _make_controller(providers: list[Mock]) -> MusicController:
    """Return a music controller wired to the given mocked providers."""
    mass = Mock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock(return_value=None)
    mass.get_providers_supporting_feature.return_value = []
    mass.get_provider = Mock(
        side_effect=lambda instance_id, **_kwargs: next(
            (p for p in providers if p.instance_id == instance_id), None
        )
    )
    controller = MusicController.__new__(MusicController)
    controller.mass = mass
    controller.domain = "music"
    controller.logger = logging.getLogger(__name__)
    controller.get_unique_providers = Mock(  # type: ignore[method-assign]
        return_value=[p.instance_id for p in providers]
    )
    controller.search_library = AsyncMock(return_value=SearchResults())  # type: ignore[method-assign]
    return controller


async def test_search_provider_returns_empty_on_provider_error() -> None:
    """A provider error during search yields empty results instead of raising."""
    prov = _make_search_provider("prov_a")
    controller = _make_controller([prov])
    for error in (MusicAssistantError("rate limited"), ValueError("unexpected")):
        prov.search.side_effect = error
        result = await controller._search_provider("query", "prov_a", [MediaType.TRACK])
        assert result == SearchResults()


async def test_global_search_returns_partial_results_when_provider_fails() -> None:
    """One failing provider must not break the entire global search."""
    prov_ok = _make_search_provider("prov_ok")
    prov_ok.search.return_value = SearchResults(
        tracks=[_make_track("track1", "prov_ok", "My Song")]
    )
    prov_bad = _make_search_provider("prov_bad")
    prov_bad.search.side_effect = MusicAssistantError("provider down")
    controller = _make_controller([prov_ok, prov_bad])

    result = await controller.search("My Song", media_types=[MediaType.TRACK], limit=5)

    assert [track.item_id for track in result.tracks] == ["track1"]
    prov_bad.search.assert_awaited_once()
