"""An audiobook or episode played from a chosen position, such as a bookmark, starts there."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver

ABS = "audiobookshelf--abs"
MAPPING = ProviderMapping(item_id="item1", provider_domain="audiobookshelf", provider_instance=ABS)


@pytest.mark.parametrize("media_type", [MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE])
@pytest.mark.parametrize(("start_position", "resume_position_ms"), [(754.5, 754500), (None, 60000)])
async def test_item_starts_at_the_requested_position(
    media_type: MediaType, start_position: float | None, resume_position_ms: int
) -> None:
    """A requested position replaces the resume position, which applies otherwise."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.logger = Mock()
    resolver.mass = Mock()
    resolver.mass.music.get_resume_position = AsyncMock(return_value=(False, 60000))
    item: Audiobook | PodcastEpisode
    if media_type == MediaType.AUDIOBOOK:
        item = Audiobook(item_id="item1", provider=ABS, name="Dune", provider_mappings={MAPPING})
    else:
        item = PodcastEpisode(
            item_id="item1",
            provider=ABS,
            name="Episode",
            provider_mappings={MAPPING},
            position=1,
            podcast=ItemMapping(
                media_type=MediaType.PODCAST, item_id="show1", provider=ABS, name="Show"
            ),
            resume_position_ms=60000,
        )
        resolver.get_next_podcast_episodes = AsyncMock(  # type: ignore[method-assign]
            return_value=UniqueList([item])
        )

    [resolved] = await resolver._resolve_media_items(item, None, start_position=start_position)

    assert isinstance(resolved.item, Audiobook | PodcastEpisode)
    assert resolved.item.resume_position_ms == resume_position_ms


async def test_a_start_position_for_several_items_is_refused() -> None:
    """A position fits a single item, so a list with one is refused before anything plays."""
    ctrl: Any = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._check_player_permission = MagicMock()
    ctrl.get = MagicMock(return_value=MagicMock())
    ctrl._handle_play_media = AsyncMock()

    with pytest.raises(InvalidDataError):
        await ctrl.play_media(
            "q1", ["library://audiobook/1", "library://audiobook/2"], start_position=30.0
        )

    ctrl._handle_play_media.assert_not_awaited()
