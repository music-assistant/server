"""Tests for the Spotify podcast episode resume position."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, RetriesExhausted

from music_assistant.providers.spotify.constants import CONF_SYNC_PODCAST_PROGRESS
from music_assistant.providers.spotify.provider import SpotifyProvider


def _make_provider(episode: dict[str, Any]) -> tuple[SpotifyProvider, AsyncMock]:
    """Return a Spotify provider whose mocked Spotify API serves the given episode."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id="spotify--test")
    provider.config.get_value.side_effect = {CONF_SYNC_PODCAST_PROGRESS: True}.get
    provider.logger = MagicMock()
    get_data = AsyncMock(return_value=episode)
    provider._get_data = get_data  # type: ignore[method-assign]
    return provider, get_data


async def test_resume_position_reads_live_episode() -> None:
    """The resume point of the episode is read live from Spotify."""
    provider, get_data = _make_provider(
        {"resume_point": {"fully_played": False, "resume_position_ms": 15000}}
    )

    result = await provider.get_resume_position("episode1", MediaType.PODCAST_EPISODE)

    assert result == (False, 15000, None)
    get_data.assert_awaited_once_with("episodes/episode1", market="from_token")


@pytest.mark.parametrize(
    "error",
    [None, MediaNotFoundError("Episode not found"), RetriesExhausted("Retries exhausted")],
    ids=["no_resume_point", "not_found", "retries_exhausted"],
)
async def test_resume_position_unavailable(error: Exception | None) -> None:
    """An episode without a resume point or an unreachable episode leaves the position to MA."""
    provider, get_data = _make_provider({})
    if error is not None:
        get_data.side_effect = error

    with pytest.raises(NotImplementedError):
        await provider.get_resume_position("episode1", MediaType.PODCAST_EPISODE)
    get_data.assert_awaited_once()
