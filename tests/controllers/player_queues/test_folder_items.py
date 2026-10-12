"""Tests for the media resolver's playback of a browse folder."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import BrowseFolder, ProviderMapping, Track

from music_assistant.controllers.player_queues.media_resolver import MediaResolver


def _track(item_id: str) -> Track:
    mapping = ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
    return Track(item_id=item_id, provider="test", name=item_id, provider_mappings={mapping})


async def test_folder_plays_every_item_of_the_folder() -> None:
    """A folder is browsed whole for playback, not as the first page of its listing."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.mass = MagicMock()
    resolver.logger = logging.getLogger("test.folder_items")
    tracks = [_track("a"), _track("b")]
    resolver.mass.music.browse = AsyncMock(return_value=tracks)

    async def _resolve(item: Any, **_kwargs: Any) -> list[Any]:
        return [item]

    resolver._resolve_media_items = AsyncMock(side_effect=_resolve)  # type: ignore[method-assign]
    folder = BrowseFolder(item_id="music", provider="test", path="test://music", name="Music")

    assert await resolver._get_folder_items(folder) == tracks
    resolver.mass.music.browse.assert_awaited_once_with("test://music", limit=None)
