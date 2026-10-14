"""Tests for the collection helpers."""

from __future__ import annotations

import pytest
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    Audiobook,
    MediaCollection,
    Podcast,
    ProviderMapping,
    UniqueList,
)

from music_assistant.helpers.collections import has_available_item

pytestmark = pytest.mark.asyncio


def _mapping(provider_instance: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=f"item_{provider_instance}",
        provider_domain=provider_instance,
        provider_instance=provider_instance,
    )


def _audiobook(name: str, provider_instance: str) -> Audiobook:
    return Audiobook(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={_mapping(provider_instance)},
    )


def _podcast(name: str, provider_instance: str) -> Podcast:
    return Podcast(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={_mapping(provider_instance)},
    )


async def test_has_available_item_plain_items() -> None:
    """Plain items count as available through their own provider mappings."""
    await set_global_cache_values({"available_providers": {"online"}})
    assert not has_available_item([])
    assert not has_available_item([_audiobook("Book", "offline")])
    assert has_available_item([_audiobook("Book", "offline"), _audiobook("Other", "online")])


async def test_has_available_item_looks_inside_collections() -> None:
    """A collection is available when any of its members is, regardless of its own mappings."""
    await set_global_cache_values({"available_providers": {"online"}})
    collection = MediaCollection[Podcast](
        item_id="podcast_collection",
        name="Series",
        provider="library",
        provider_mappings=set(),
        items=UniqueList([_podcast("Episode 1", "offline"), _podcast("Episode 2", "online")]),
    )
    # the collection itself has no provider mappings, so its inherited availability is false
    assert not collection.available
    assert has_available_item([collection])


async def test_has_available_item_unavailable_collection() -> None:
    """A collection whose members are all unavailable does not count as available."""
    await set_global_cache_values({"available_providers": {"online"}})
    collection = MediaCollection[Audiobook](
        item_id="book_collection",
        name="Series",
        provider="library",
        provider_mappings=set(),
        items=UniqueList([_audiobook("Book 1", "offline"), _audiobook("Book 2", "offline")]),
    )
    assert not has_available_item([collection])
    assert not has_available_item([collection, _audiobook("Book 3", "offline")])
