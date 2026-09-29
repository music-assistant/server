"""Tests for applying Audiobookshelf progresses against the state mass already recorded."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.schema.media_progress import MediaProgress
from music_assistant_models.enums import MediaType

from music_assistant.controllers.music.controller import PlaylogProviderItem
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
    provider._playlog_state = {}
    provider.mass.music.mark_item_played = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.mark_item_unplayed = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock())  # type: ignore[method-assign]
    provider.mass.music.get_playlog_provider_items = AsyncMock(return_value=[])  # type: ignore[method-assign]
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


def _played_calls(provider: Audiobookshelf) -> list[bool]:
    played = cast("AsyncMock", provider.mass.music.mark_item_played)
    return [call.kwargs["fully_played"] for call in played.await_args_list]


async def test_finished_book_counted_once_across_syncs(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """The playlog sync runs four times per cycle; a finished book must count as one play."""
    progress = _progress(last_update=1000, is_finished=True)

    for _ in range(4):
        await playlog_provider._set_playlog_from_user_sync([progress])
        clock.now += 3600
        # what mass recorded in the first run is what it reports back on every later one
        cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_items).return_value = [
            PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", True, 3600)
        ]

    assert _played_calls(playlog_provider) == [True]


async def test_finished_book_not_recounted_after_restart(playlog_provider: Audiobookshelf) -> None:
    """A restart empties the in-memory state; mass' own playlog still prevents a second count."""
    cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_items).return_value = [
        PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", True, 3600)
    ]
    playlog_provider._playlog_state = {}

    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=1000, is_finished=True)]
    )

    assert _played_calls(playlog_provider) == []


async def test_book_finished_again_after_being_reset_counts_again(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """A book restarted and finished a second time is a second play."""
    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=1000, is_finished=True)]
    )
    clock.now += 3600
    cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_items).return_value = [
        PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", False, 60)
    ]

    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=2000, is_finished=True)]
    )

    assert _played_calls(playlog_provider) == [True, True]


async def test_position_update_of_a_finished_book_is_applied(
    playlog_provider: Audiobookshelf,
) -> None:
    """Resuming a finished book reaches mass; only a repeated finish is suppressed."""
    cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_items).return_value = [
        PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", True, 3600)
    ]

    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=1000, is_finished=False, current_time=120)]
    )

    assert _played_calls(playlog_provider) == [False]


async def test_failed_report_is_retried(playlog_provider: Audiobookshelf) -> None:
    """A play mass never recorded must not be treated as applied."""
    played = cast("AsyncMock", playlog_provider.mass.music.mark_item_played)
    played.side_effect = [RuntimeError("abs unreachable"), None]
    progress = _progress(last_update=1000, is_finished=True)

    with pytest.raises(RuntimeError):
        await playlog_provider._update_playlog_book(progress)
    await playlog_provider._update_playlog_book(progress)

    assert played.await_count == 2
