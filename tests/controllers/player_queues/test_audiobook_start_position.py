"""An audiobook played from a chosen position, such as a bookmark, starts there."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.controllers.player_queues.media_resolver import MediaResolver


@pytest.mark.parametrize(("start_position", "resume_position_ms"), [(754.5, 754500), (None, 60000)])
async def test_audiobook_starts_at_the_requested_position(
    start_position: float | None, resume_position_ms: int
) -> None:
    """A requested position replaces the resume position, which applies otherwise."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.logger = Mock()
    resolver.mass = Mock()
    resolver.mass.music.get_resume_position = AsyncMock(return_value=(False, 60000))
    book = Audiobook(
        item_id="book1",
        provider="audiobookshelf--abs",
        name="Dune",
        provider_mappings={
            ProviderMapping(
                item_id="book1",
                provider_domain="audiobookshelf",
                provider_instance="audiobookshelf--abs",
            )
        },
    )

    [resolved] = await resolver._resolve_media_items(book, None, start_position=start_position)

    assert isinstance(resolved.item, Audiobook)
    assert resolved.item.resume_position_ms == resume_position_ms
