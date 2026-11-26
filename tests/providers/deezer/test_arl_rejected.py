"""Test that Deezer hands the provider back to the user when the ARL stops working."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from deezer_python_gql import GraphQLClientAuthError, GraphQLClientHttpError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import AudioError, LoginFailed

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.deezer.gw_client import DeezerGWAuthError, DeezerGWError
from music_assistant.providers.deezer.provider import DeezerProvider
from music_assistant.providers.deezer.streaming import DeezerStreamingManager

AUTH_ERRORS = [GraphQLClientAuthError("rejected"), DeezerGWAuthError("no user")]
# the same keys as for these errors during setup
LOGIN_KEYS = [(AUTH_ERRORS[0], "arl_rejected"), (AUTH_ERRORS[1], "gw_no_session")]


def _loaded(provider: DeezerProvider) -> Mock:
    """Mark the provider as loaded and return its mocked unload_with_error."""
    provider.available = True
    unload = Mock()
    provider.unload_with_error = unload  # type: ignore[method-assign]
    return unload


@pytest.mark.parametrize(("error", "key"), LOGIN_KEYS)
async def test_rejected_arl_during_sync_unloads_the_provider(
    provider: DeezerProvider, error: Exception, key: str
) -> None:
    """A sync that fails on the ARL unloads the provider with a login error."""
    unload = _loaded(provider)

    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=error)),
        pytest.raises(LoginFailed) as raised,
    ):
        await provider.sync_library(MediaType.TRACK)

    assert raised.value.translation_key == key
    assert raised.value.__cause__ is error
    unload.assert_called_once_with(raised.value)


@pytest.mark.parametrize(
    "error", [GraphQLClientHttpError(status_code=502, response=Mock()), DeezerGWError("boom")]
)
async def test_other_sync_errors_keep_the_provider(
    provider: DeezerProvider, error: Exception
) -> None:
    """Server errors and failures of single items do not unload the provider."""
    unload = _loaded(provider)

    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=error)),
        pytest.raises(type(error)),
    ):
        await provider.sync_library(MediaType.TRACK)

    unload.assert_not_called()


@pytest.mark.parametrize("error", AUTH_ERRORS)
def test_rejected_arl_is_not_skipped_as_an_item(provider: DeezerProvider, error: Exception) -> None:
    """A rejected ARL ends the sync instead of being logged as a skipped item."""
    with pytest.raises(type(error)):
        provider.report_skipped_sync_item(MediaType.AUDIOBOOK, "1", error)


async def test_rejected_arl_during_playback_unloads_the_provider(provider: DeezerProvider) -> None:
    """A track that fails on the ARL unloads the provider with a login error."""
    unload = _loaded(provider)
    provider.gw_client = Mock(get_deezer_track_urls=AsyncMock(side_effect=AUTH_ERRORS[1]))
    provider.streaming_manager = DeezerStreamingManager(provider)

    with pytest.raises(LoginFailed):
        await provider.get_stream_details("1", MediaType.TRACK)

    unload.assert_called_once()


async def test_unavailable_track_keeps_the_provider(provider: DeezerProvider) -> None:
    """Other GW errors while resolving a track stay a playback error."""
    unload = _loaded(provider)
    provider.gw_client = Mock(get_deezer_track_urls=AsyncMock(side_effect=DeezerGWError("boom")))
    provider.streaming_manager = DeezerStreamingManager(provider)

    with pytest.raises(AudioError):
        await provider.get_stream_details("1", MediaType.TRACK)

    unload.assert_not_called()


async def test_unavailable_provider_is_not_unloaded_again(provider: DeezerProvider) -> None:
    """Once the provider is no longer loaded, a later failure does not unload it again."""
    unload = _loaded(provider)
    provider.available = False

    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=AUTH_ERRORS[0])),
        pytest.raises(LoginFailed),
    ):
        await provider.sync_library(MediaType.TRACK)

    unload.assert_not_called()
