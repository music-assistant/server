"""Tests for author audiobook listings when one of the author's providers fails."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import patch

import pytest
from music_assistant_models.enums import ArtistType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    Artist,
    Audiobook,
    ProviderMapping,
    UniqueList,
)

from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

_PROVIDERS = ["local_inst", "streaming_inst"]


def _mapping(provider_instance: str, item_id: str, in_library: bool = True) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=in_library,
    )


async def _seed_author(mass: MusicAssistant, *, with_library_audiobook: bool) -> Artist:
    """Seed a library author mapped to a local provider and a non-library streaming provider."""
    author = Artist(
        item_id="0",
        provider="library",
        name="Test Author",
        artist_type=ArtistType.AUTHOR,
        provider_mappings={
            _mapping("local_inst", "author_local"),
            _mapping("streaming_inst", "author_streaming", in_library=False),
        },
    )
    db_author = await mass.music.artists.add_item_to_library(author)
    if with_library_audiobook:
        await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id="0",
                provider="library",
                name="Local Book",
                provider_mappings={_mapping("local_inst", "book_local")},
                authors=UniqueList([db_author]),
            )
        )
    return db_author


def _failing_provider_fetch(
    error: Exception,
) -> Callable[[str, str], Awaitable[list[Audiobook]]]:
    """Return a fake provider audiobook fetch that fails for the streaming provider only."""

    async def _fetch(_item_id: str, provider_instance_id_or_domain: str) -> list[Audiobook]:
        if provider_instance_id_or_domain == "streaming_inst":
            raise error
        return []

    return _fetch


async def test_author_audiobooks_skip_failing_provider(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing secondary provider is skipped (and logged) so the library audiobooks still list."""
    db_author = await _seed_author(mass, with_library_audiobook=True)
    # both providers are loaded and available; the streaming one merely errors on the fetch
    await set_global_cache_values({"available_providers": set(_PROVIDERS)})
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
    ):
        audiobooks = await mass.music.artists.audiobooks(db_author.item_id, "library")
    assert [item.name for item in audiobooks] == ["Local Book"]
    assert "Unable to fetch audiobooks for Test Author from provider streaming_inst" in caplog.text


async def test_author_audiobooks_raise_when_nothing_playable(mass: MusicAssistant) -> None:
    """Pin that the guard does not over-suppress: with nothing playable, the error still surfaces."""
    db_author = await _seed_author(mass, with_library_audiobook=False)
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.audiobooks(db_author.item_id, "library")


async def test_author_audiobooks_raise_when_library_items_unavailable(
    mass: MusicAssistant,
) -> None:
    """Library audiobooks that are all unavailable do not count as playable: the error surfaces."""
    db_author = await _seed_author(mass, with_library_audiobook=True)
    # only the (failing) streaming provider is available, so the library audiobook is unplayable
    await set_global_cache_values({"available_providers": {"streaming_inst"}})
    with (
        patch.object(mass.music, "get_unique_providers", return_value=_PROVIDERS),
        patch.object(
            mass.music.artists,
            "get_provider_author_audiobooks",
            side_effect=_failing_provider_fetch(MediaNotFoundError("provider failed")),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.music.artists.audiobooks(db_author.item_id, "library")
