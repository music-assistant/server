"""Tests for the app token the Apple Music provider loads with."""

import base64
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.providers.apple_music.constants import CONF_MUSIC_APP_TOKEN
from music_assistant.providers.apple_music.provider import AppleMusicProvider

_OLD = 1792000000
_NEW = 1801000000


def _token(exp: int) -> str:
    """Return an unsigned JWT that only carries the given expiry."""

    def segment(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'ES256'})}.{segment({'exp': exp})}.sig"


async def _loaded_app_token(stored: str | None, bundled: str) -> str | None:
    """Run the provider init with the given stored and bundled app tokens."""
    provider = object.__new__(AppleMusicProvider)
    setup_data = {CONF_MUSIC_APP_TOKEN: stored}
    provider.get_setup_value = Mock(side_effect=setup_data.get)  # type: ignore[method-assign]
    provider.api_client = Mock(get_user_storefront=AsyncMock(return_value="us"))
    provider.streaming_manager = Mock(initialize=AsyncMock())
    with patch("music_assistant.providers.apple_music.provider.MUSIC_APP_TOKEN", bundled):
        await provider.handle_async_init()
    return provider._music_app_token


@pytest.mark.parametrize(
    ("stored", "bundled", "expected"),
    [
        # a pre-2.10 install keeps a copy of the token bundled back then
        (_token(_OLD), _token(_NEW), _token(_NEW)),
        # a token typed in because the bundled one was rejected
        (_token(_NEW), _token(_OLD), _token(_NEW)),
        # a development build ships no token
        (_token(_OLD), "", _token(_OLD)),
        (None, _token(_NEW), _token(_NEW)),
    ],
    ids=["stale_stored", "manual_stored", "no_bundled", "no_stored"],
)
async def test_provider_loads_with_the_longest_lived_app_token(
    stored: str | None, bundled: str, expected: str
) -> None:
    """The provider uses whichever of the stored and bundled app tokens expires last."""
    assert await _loaded_app_token(stored, bundled) == expected
