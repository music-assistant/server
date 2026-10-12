"""Tests for streaming from the Audiobookshelf provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant.providers.audiobookshelf import Audiobookshelf


@pytest.fixture
def stream_provider(provider: Audiobookshelf) -> Audiobookshelf:
    """Return a provider whose abs server opens a session with one part."""
    provider.create_session_lock = asyncio.Lock()
    provider.sessions = {}
    provider.is_token_user = False
    provider.mass.streams = Mock(base_url="http://ma.local:8097")
    session = Mock(id_="session1", duration=60, audio_tracks=[Mock(duration=60, metadata=None)])
    provider._client.get_playback_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    return provider


async def test_stream_details_keyed_on_the_item(stream_provider: Audiobookshelf) -> None:
    """Stream details carry the item id, so loudness measured once is reused."""
    with patch.object(stream_provider, "get_setup_value", return_value="http://abs.local"):
        details = await stream_provider.get_stream_details("book1", MediaType.AUDIOBOOK)

    assert details.item_id == "book1"
