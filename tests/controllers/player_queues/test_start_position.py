"""
An audiobook played from a chosen position, such as a bookmark, starts there.

Episodes and podcasts are covered in test_podcast_start_position.py.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Audiobook, ProviderMapping

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver

ABS = "audiobookshelf--abs"
MAPPING = ProviderMapping(item_id="item1", provider_domain="audiobookshelf", provider_instance=ABS)


@pytest.mark.parametrize(("start_position", "resume_position_ms"), [(754.5, 754500), (None, 60000)])
async def test_audiobook_starts_at_the_requested_position(
    start_position: float | None, resume_position_ms: int
) -> None:
    """A requested position replaces the resume position, which applies otherwise."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.logger = Mock()
    resolver.mass = Mock()
    resolver.mass.music.get_resume_position = AsyncMock(return_value=(False, 60000))
    book = Audiobook(item_id="item1", provider=ABS, name="Dune", provider_mappings={MAPPING})

    [resolved] = await resolver._resolve_media_items(book, None, start_position=start_position)

    assert isinstance(resolved.item, Audiobook)
    assert resolved.item.resume_position_ms == resume_position_ms


@pytest.mark.parametrize(
    ("media", "start_item", "start_position", "start_from_beginning", "played_at"),
    [
        ("library://audiobook/1", None, 30.0, False, 30.0),
        ("library://podcast/1", "latest", 30.0, False, 30.0),
        (f"{ABS}://podcast_episode/item1", None, 30.0, False, 30.0),
        # the deprecated flag of an outdated client starts at 0, an explicit position wins
        (f"{ABS}://podcast_episode/item1", None, None, True, 0),
        (f"{ABS}://podcast_episode/item1", None, 30.0, True, 30.0),
        # refused before anything plays
        (["library://audiobook/1", "library://audiobook/2"], None, 30.0, False, None),
        ("library://track/1", None, 30.0, False, None),
        ("library://folder/1", None, None, True, None),
        ("library://audiobook/1", "2", 30.0, False, None),
    ],
)
async def test_a_start_position_needs_one_item_it_can_start(
    media: str | list[str],
    start_item: str | None,
    start_position: float | None,
    start_from_beginning: bool,
    played_at: float | None,
) -> None:
    """A position plays one audiobook, podcast or episode, a chapter of an audiobook aside."""
    ctrl: Any = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player.return_value.offline_by_power_control = False
    ctrl.mass.players._resolve_playback_owner = lambda player: player  # plays on its own
    ctrl._check_player_permission = MagicMock()
    ctrl.get = MagicMock(return_value=MagicMock())
    ctrl._handle_play_media = AsyncMock()

    with pytest.raises(InvalidDataError) if played_at is None else nullcontext():
        await ctrl.play_media(
            "q1",
            media,
            start_item=start_item,
            start_position=start_position,
            start_from_beginning=start_from_beginning,
        )

    played = ctrl._handle_play_media.await_args
    assert (played.args[-1] if played else None) == played_at
