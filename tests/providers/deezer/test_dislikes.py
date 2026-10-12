"""Test that likes and dislikes reach the user's Deezer recommendations."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.provider import DeezerProvider


@pytest.fixture
def gql(provider: DeezerProvider) -> AsyncMock:
    """Stub the GraphQL client and wire up a real media manager."""
    client = AsyncMock()
    provider.gql_client = client
    provider.media_manager = DeezerMediaManager(provider)
    return client


async def test_disliked_track_is_banned(provider: DeezerProvider, gql: AsyncMock) -> None:
    """A dislike keeps the track out of the user's Deezer recommendations."""
    await provider.set_favorite("3135556", MediaType.TRACK, False)

    gql.ban_track_from_recommendation.assert_awaited_once_with(track_id="3135556")
    gql.unban_track_from_recommendation.assert_not_called()


@pytest.mark.parametrize("favorite", [True, None])
async def test_liked_or_cleared_track_is_unbanned(
    provider: DeezerProvider, gql: AsyncMock, favorite: bool | None
) -> None:
    """A like or an unset lifts the ban and adds nothing to the favorites itself."""
    await provider.set_favorite("3135556", MediaType.TRACK, favorite)

    gql.unban_track_from_recommendation.assert_awaited_once_with(track_id="3135556")
    gql.ban_track_from_recommendation.assert_not_called()
    gql.add_track_to_favorite.assert_not_called()


async def test_disliked_artist_is_banned(provider: DeezerProvider, gql: AsyncMock) -> None:
    """A dislike keeps the artist out of the user's Deezer recommendations."""
    await provider.set_favorite("27", MediaType.ARTIST, False)

    gql.ban_artist_from_recommendation.assert_awaited_once_with(artist_id="27")


async def test_liked_artist_is_unbanned(provider: DeezerProvider, gql: AsyncMock) -> None:
    """A like lifts an earlier ban on the artist."""
    await provider.set_favorite("27", MediaType.ARTIST, True)

    gql.unban_artist_from_recommendation.assert_awaited_once_with(artist_id="27")


async def test_album_is_left_alone(provider: DeezerProvider, gql: AsyncMock) -> None:
    """Deezer has no ban for albums, so nothing is sent."""
    await provider.set_favorite("302127", MediaType.ALBUM, False)

    assert not gql.mock_calls
