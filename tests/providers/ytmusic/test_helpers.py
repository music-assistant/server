"""Test the ytmusicapi call wrapper's handling of a signed-out session."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import ytmusicapi
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.ytmusic import helpers

# A trimmed but realistic nav() KeyError: YouTube answered with its signed-out
# page instead of an auth error, so the renderer lookup fails deep in that payload.
SIGNED_OUT_PAYLOAD_ERROR = KeyError(
    "Unable to find 'twoColumnBrowseResultsRenderer' using path "
    "['contents', 'twoColumnBrowseResultsRenderer', 'tabs', 0] on "
    "{'singleColumnBrowseResultsRenderer': {'tabs': [{'tabRenderer': {'content': "
    "{'sectionListRenderer': {'contents': [{'itemSectionRenderer': {'contents': "
    "[{'buttonRenderer': {'text': {'runs': [{'text': 'Sign in'}]}, "
    "'navigationEndpoint': {'clickTrackingParams': '...', "
    "'signInEndpoint': {'hack': True}}}}]}}]}}}}]}}, "
    "exception: 'twoColumnBrowseResultsRenderer'"
)


def _patch_get_home(error: Exception) -> Any:
    """Patch ytmusicapi.YTMusic so get_home() raises the given error."""
    mock_ytm = MagicMock()
    mock_ytm.get_home.side_effect = error
    return patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm)


async def test_signed_out_payload_is_translated_to_login_failed() -> None:
    """A KeyError carrying the signed-out page must surface as LoginFailed."""
    with _patch_get_home(SIGNED_OUT_PAYLOAD_ERROR), pytest.raises(LoginFailed):
        await helpers.get_home(headers={})


async def test_unrelated_key_error_still_propagates() -> None:
    """A KeyError unrelated to a signed-out session must propagate as-is."""
    with _patch_get_home(KeyError("some_other_key")), pytest.raises(KeyError):
        await helpers.get_home(headers={})


async def test_get_artist_does_not_hide_signed_out_behind_its_fallback() -> None:
    """get_artist's channel fallback must not turn a signed-out session into an artist."""
    mock_ytm = MagicMock()
    mock_ytm.get_artist.side_effect = SIGNED_OUT_PAYLOAD_ERROR
    mock_ytm.get_user.side_effect = SIGNED_OUT_PAYLOAD_ERROR
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(LoginFailed),
    ):
        await helpers.get_artist(prov_artist_id="UC123", headers={})


async def test_get_artist_fallback_still_returns_unknown() -> None:
    """An artist that is neither a channel nor a user still falls back to Unknown."""
    mock_ytm = MagicMock()
    mock_ytm.get_artist.side_effect = KeyError("header")
    mock_ytm.get_user.side_effect = KeyError("name")
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        artist = await helpers.get_artist(prov_artist_id="UC123", headers={})
    assert artist == {"channelId": "UC123", "name": "Unknown"}


async def test_search_passes_auth_headers_and_user() -> None:
    """search() must authenticate its YTMusic client so results respect account context."""
    mock_ytm = MagicMock()
    mock_ytm.search.return_value = []
    headers = {"cookie": "abc"}
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await helpers.search(query="test", headers=headers, user="123")
    mock_ytmusic.assert_called_once_with(auth=headers, language="en", user="123")
