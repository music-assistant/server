"""Tests for applying Audiobookshelf progresses to the Music Assistant playlog."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.schema.media_progress import MediaProgress
from aioaudiobookshelf.schema.user import User
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook

from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import ProgressGuard


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Iterator[_Clock]:
    """Patch the clock used by the progress guard."""
    clock = _Clock()
    with patch("music_assistant.providers.audiobookshelf.helpers.time.time", clock):
        yield clock


@pytest.fixture
def playlog_provider(provider: Audiobookshelf) -> Audiobookshelf:
    """Return a provider with a known audiobook and a mocked playlog."""
    provider.libraries.audiobooks["lib1"].item_ids.add("book1")
    provider.progress_guard = ProgressGuard()
    provider.sessions = {}
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.mark_item_played = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.mark_item_unplayed = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock())  # type: ignore[method-assign]
    provider.mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[])  # type: ignore[method-assign]
    provider._client.update_my_media_progress = AsyncMock()  # type: ignore[method-assign]
    return provider


def _progress(last_update: int, is_finished: bool, current_time: float = 3600) -> MediaProgress:
    return MediaProgress(
        id_="progress1",
        library_item_id="book1",
        duration=3600,
        current_time=current_time,
        is_finished=is_finished,
        hide_from_continue_listening=False,
        last_update=last_update,
        started_at=100,
    )


def _fully_played_calls(provider: Audiobookshelf) -> list[bool]:
    return [
        call.kwargs["fully_played"]
        for call in cast("AsyncMock", provider.mass.music.mark_item_played).call_args_list
    ]


async def test_finished_progress_applied_once(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """Repeated syncs of an unchanged finished progress mark the item played only once."""
    progress = _progress(last_update=1000, is_finished=True)
    for _ in range(4):
        await playlog_provider._set_playlog_from_user_sync([progress])
        clock.now += 3600

    assert _fully_played_calls(playlog_provider) == [True]


async def test_applied_progress_survives_restart(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """A progress applied before a restart is not applied again after it."""
    progress = _progress(last_update=1000, is_finished=True)
    await playlog_provider._set_playlog_from_user_sync([progress])
    cached = cast("AsyncMock", playlog_provider.mass.cache.set).call_args.kwargs["data"]

    playlog_provider.progress_guard = ProgressGuard.from_applied_dict(cached)
    clock.now += 3600
    await playlog_provider._set_playlog_from_user_sync([progress])

    assert _fully_played_calls(playlog_provider) == [True]


async def test_finish_reported_by_mass_not_applied_back(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """A finish reported by mass to abs does not count as another play on the next sync."""
    audiobook = Mock(spec=Audiobook)
    audiobook.duration = 3600
    audiobook.name = "Book"
    await playlog_provider.on_played(
        media_type=MediaType.AUDIOBOOK,
        prov_item_id="book1",
        fully_played=True,
        position=3600,
        media_item=audiobook,
    )
    clock.now += 3600
    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=2000, is_finished=True)]
    )

    cast("AsyncMock", playlog_provider.mass.music.mark_item_played).assert_not_called()


async def test_relistened_item_counted_again(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """An item listened again in abs after being finished is marked played again."""
    progresses = [
        _progress(last_update=1000, is_finished=True),
        _progress(last_update=2000, is_finished=False, current_time=600),
        _progress(last_update=3000, is_finished=True),
    ]
    for progress in progresses:
        await playlog_provider._set_playlog_from_user_sync([progress])
        clock.now += 3600

    assert _fully_played_calls(playlog_provider) == [True, False, True]


async def test_progress_of_web_ui_applied(playlog_provider: Audiobookshelf) -> None:
    """A progress changed by another abs client, only sent as user update, is applied."""
    playlog_provider.abs_user_id = "user1"
    user = Mock(spec=User)
    user.id_ = "user1"
    user.media_progress = [_progress(last_update=1000, is_finished=True)]

    await playlog_provider._socket_abs_user_updated(user)

    assert _fully_played_calls(playlog_provider) == [True]


async def test_progress_of_other_user_ignored(playlog_provider: Audiobookshelf) -> None:
    """An admin receiving another user's update does not apply its progresses."""
    playlog_provider.abs_user_id = "user1"
    user = Mock(spec=User)
    user.id_ = "user2"
    user.media_progress = [_progress(last_update=1000, is_finished=True)]

    await playlog_provider._socket_abs_user_updated(user)

    cast("AsyncMock", playlog_provider.mass.music.mark_item_played).assert_not_called()
