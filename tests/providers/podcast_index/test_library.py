"""Tests for Podcast Index library operations."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock, patch

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderType
from music_assistant_models.media_items import Podcast

from music_assistant.providers.podcast_index.constants import CONF_STORED_PODCASTS
from music_assistant.providers.podcast_index.provider import PodcastIndexProvider


async def test_concurrent_add_and_remove_preserve_both_updates() -> None:
    """Concurrent library changes retain every intended stored-podcast update."""
    removed_feed = "https://example.com/removed.xml"
    added_feed = "https://example.com/added.xml"
    provider, config_store = _provider([removed_feed])
    lookup_barrier = _LookupBarrier(
        {"remove": removed_feed, "add": added_feed},
        operation_count=2,
    )
    with patch.object(
        PodcastIndexProvider,
        "_get_feed_url_for_podcast",
        side_effect=lookup_barrier.lookup,
    ):
        remove_task = asyncio.create_task(provider.library_remove("remove", MediaType.PODCAST))
        add_task = asyncio.create_task(provider.library_add(_podcast("add")))

        await lookup_barrier.wait_until_blocked()
        lookup_barrier.release()

        remove_result, add_result = await asyncio.gather(remove_task, add_task)
        assert remove_result is True
        assert add_result is True
    assert config_store.stored_podcasts == [added_feed]


class _ConfigStore:
    """Store Podcast Index configuration values for provider tests."""

    def __init__(self, stored_podcasts: list[str]) -> None:
        self.stored_podcasts = list(stored_podcasts)

    def get_raw_provider_config_value(
        self, provider_instance: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """Return the current stored configuration value."""
        assert provider_instance == "podcast_index--test"
        assert key == CONF_STORED_PODCASTS
        return list(self.stored_podcasts)

    def set_raw_provider_config_value(
        self,
        provider_instance: str,
        key: str,
        value: ConfigValueType,
        encrypted: bool = False,
        immediate: bool = False,
    ) -> None:
        """Persist a provider configuration value."""
        assert provider_instance == "podcast_index--test"
        assert key == CONF_STORED_PODCASTS
        assert isinstance(value, list)
        assert all(isinstance(item, str) for item in value)
        self.stored_podcasts = cast("list[str]", value)


class _LookupBarrier:
    """Block feed lookups until every concurrent operation has reached the lookup."""

    def __init__(self, feed_urls: dict[str, str], operation_count: int) -> None:
        self._feed_urls = feed_urls
        self._operation_count = operation_count
        self._blocked_count = 0
        self._all_blocked = asyncio.Event()
        self._release = asyncio.Event()

    async def lookup(self, podcast_id: str) -> str:
        """Return a feed URL after all expected lookups are blocked."""
        self._blocked_count += 1
        if self._blocked_count == self._operation_count:
            self._all_blocked.set()
        await self._release.wait()
        return self._feed_urls[podcast_id]

    async def wait_until_blocked(self) -> None:
        """Wait until all expected operations are blocked in their feed lookup."""
        await asyncio.wait_for(self._all_blocked.wait(), timeout=1)

    def release(self) -> None:
        """Release every blocked feed lookup."""
        self._release.set()


def _provider(stored_podcasts: list[str]) -> tuple[PodcastIndexProvider, _ConfigStore]:
    """Create a Podcast Index provider backed by an in-memory configuration store."""
    config_store = _ConfigStore(stored_podcasts)
    config = ProviderConfig(
        values={
            CONF_STORED_PODCASTS: ConfigEntry(
                key=CONF_STORED_PODCASTS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                value=list(stored_podcasts),
            )
        },
        type=ProviderType.MUSIC,
        domain="podcast_index",
        instance_id="podcast_index--test",
    )
    provider = object.__new__(PodcastIndexProvider)
    provider.mass = MagicMock()
    provider.mass.config = config_store
    provider.config = config
    provider.logger = MagicMock()
    return provider, config_store


def _podcast(item_id: str) -> Podcast:
    """Create a Podcast Index podcast for library tests."""
    return Podcast(
        item_id=item_id,
        provider="podcast_index--test",
        name=f"Podcast {item_id}",
        provider_mappings=set(),
    )
