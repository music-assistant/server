"""Tests for coalescing Audiobookshelf user_updated socket events."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.schema.user import User

from music_assistant.providers.audiobookshelf import Audiobookshelf


@pytest.fixture
def debounce_provider(provider: Audiobookshelf) -> Audiobookshelf:
    """Return a provider whose user updates run through a real task."""
    provider.abs_user_id = "user1"
    provider._pending_user_update = None
    provider._user_update_task = None
    provider.mass.create_task = Mock(side_effect=asyncio.ensure_future)  # type: ignore[method-assign]
    provider._set_playlog_from_user = AsyncMock()  # type: ignore[method-assign]
    return provider


def _user(marker: Any) -> User:
    user = Mock(spec=User)
    user.id_ = "user1"
    user.media_progress = marker
    return user


async def _drain(provider: Audiobookshelf) -> None:
    if provider._user_update_task is not None:
        await provider._user_update_task


async def test_burst_syncs_once_with_the_latest_payload(
    debounce_provider: Audiobookshelf,
) -> None:
    """Abs patches progress every few seconds; that must not sync the playlog each time."""
    with patch("music_assistant.providers.audiobookshelf.USER_UPDATED_DEBOUNCE_SECONDS", 0):
        for marker in range(5):
            await debounce_provider._socket_abs_user_updated(_user(marker))
        await _drain(debounce_provider)

    sync = cast("AsyncMock", debounce_provider._set_playlog_from_user)
    assert [call.args[0].media_progress for call in sync.await_args_list] == [4]


async def test_update_after_a_burst_is_not_lost(debounce_provider: Audiobookshelf) -> None:
    """An event arriving after the previous sync ran still reaches the playlog."""
    with patch("music_assistant.providers.audiobookshelf.USER_UPDATED_DEBOUNCE_SECONDS", 0):
        await debounce_provider._socket_abs_user_updated(_user("first"))
        await _drain(debounce_provider)
        await debounce_provider._socket_abs_user_updated(_user("second"))
        await _drain(debounce_provider)

    sync = cast("AsyncMock", debounce_provider._set_playlog_from_user)
    assert [call.args[0].media_progress for call in sync.await_args_list] == ["first", "second"]


async def test_update_of_other_user_starts_no_task(debounce_provider: Audiobookshelf) -> None:
    """An admin receiving another user's update does not schedule a sync."""
    other = _user("ignored")
    other.id_ = "user2"

    await debounce_provider._socket_abs_user_updated(other)

    assert debounce_provider._user_update_task is None
    assert debounce_provider._pending_user_update is None
