"""Tests for the per-provider search helper shared by the media controllers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderFeature, ProviderType
from music_assistant_models.media_items import SearchResults

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_album

if TYPE_CHECKING:
    import pytest


def _albums_controller(provider: Mock) -> AlbumsController:
    """Return an albums controller whose only provider is the given mock."""
    mass = Mock()
    mass.get_provider = Mock(return_value=provider)
    controller = AlbumsController.__new__(AlbumsController)
    controller.mass = mass
    controller.logger = logging.getLogger("test.albums.search")
    return controller


def _search_provider(result: SearchResults | None) -> Mock:
    """Return a music provider that answers a search with the given result."""
    provider = Mock(spec=MusicProvider)
    provider.instance_id = "demo_1"
    provider.domain = "demo"
    provider.name = "Demo Music Provider"
    provider.type = ProviderType.MUSIC
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.ALBUM}
    provider.search = AsyncMock(return_value=result)
    return provider


async def test_search_returns_what_the_provider_found() -> None:
    """The results of the searched media type are handed back."""
    album = create_album("demo_1", "album1")
    controller = _albums_controller(_search_provider(SearchResults(albums=[album])))

    assert await controller.search("Test Album", "demo_1") == [album]


async def test_search_survives_a_provider_returning_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A provider reporting SEARCH without implementing it must not raise.

    Every lookup that searches all providers at once - the versions of an album or
    a track, the cross-provider matching during a sync - would otherwise be taken
    down by that one provider.
    """
    controller = _albums_controller(_search_provider(None))

    with caplog.at_level(logging.WARNING):
        assert await controller.search("Test Album", "demo_1") == []

    assert "Demo Music Provider" in caplog.text
