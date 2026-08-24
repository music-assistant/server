"""Tests that HTTP error responses do not leak exception details to the client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.plex_connect.playback import PlaybackMixin
from music_assistant.providers.plex_connect.queue_commands import QueueCommandsMixin


class _PlaybackHandler(PlaybackMixin):
    """PlaybackMixin with the host-class attributes mocked."""

    def __init__(self) -> None:
        self.provider = Mock()
        self._ma_player_id = "player1"
        self._updating_from_plex = False
        self.play_queue_id = None
        self.play_queue_item_ids: dict[int, int] = {}
        self._broadcast_timeline = AsyncMock()  # type: ignore[method-assign]


class _QueueHandler(QueueCommandsMixin):
    """QueueCommandsMixin with the host-class attributes mocked."""

    def __init__(self) -> None:
        self.provider = Mock()
        self._ma_player_id = "player1"
        self._updating_from_plex = False
        self.play_queue_id = None
        self.play_queue_version = 1
        self.play_queue_item_ids: dict[int, int] = {}
        self._broadcast_timeline = AsyncMock()  # type: ignore[method-assign]


def _request_with_query(query: dict[str, str]) -> Any:
    request = Mock()
    request.query = query
    return request


@pytest.mark.asyncio
async def test_skip_to_error_does_not_leak_exception() -> None:
    """An internal error in skipTo must return a generic 500 body."""
    handler = _PlaybackHandler()
    provider_mock = Mock()
    provider_mock.mass.player_queues.items = Mock(
        side_effect=RuntimeError("secret internal details")
    )
    handler.provider = provider_mock

    response = await handler.handle_skip_to(_request_with_query({"key": "/library/item/1"}))

    assert response.status == 500
    assert "secret internal details" not in (response.text or "")


@pytest.mark.asyncio
async def test_create_play_queue_error_does_not_leak_exception() -> None:
    """An internal error in createPlayQueue must return a generic 500 body."""
    handler = _QueueHandler()
    provider_mock = Mock()
    provider_mock._plex_server.fetchItem = Mock(side_effect=RuntimeError("secret internal details"))
    handler.provider = provider_mock

    response = await handler.handle_create_play_queue(
        _request_with_query({"uri": "server://xyz/library/item/1"})
    )

    assert response.status == 500
    assert "secret internal details" not in (response.text or "")
