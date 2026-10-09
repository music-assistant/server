"""Unit tests for Apple Music ID helpers."""

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.apple_music.helpers.utils import is_apple_id, is_library_id
from music_assistant.providers.apple_music.provider import AppleMusicProvider


def test_is_library_id_accepts_library_prefixes() -> None:
    """Confirm expected library prefixes are accepted."""
    for prefix in ("a.", "i.", "l.", "p."):
        assert is_library_id(f"{prefix}ABC123")


def test_is_library_id_rejects_pl_u_prefix() -> None:
    """Reject the invalid pl.u- prefix."""
    assert not is_library_id("pl.u-ABC123")
    assert not is_library_id("pl.u-1")


def test_is_library_id_rejects_invalid_values() -> None:
    """Reject malformed values and non-string inputs."""
    for value in ("", "a.", "x.123", "pl.123", "p.123-456"):
        assert not is_library_id(value)
    invalid_non_str: list[Any] = [None, 123, 12.3]
    for value in invalid_non_str:
        assert not is_library_id(value)


def test_is_apple_id_accepts_catalog_and_prefixed_ids() -> None:
    """Catalog, library, playlist and station ids are recognised."""
    for value in ("1613600188", "i.ABC123", "l.abc", "r.XyZ", "pl.u-ABC_1", "ra.1498157166"):
        assert is_apple_id(value)


def test_is_apple_id_rejects_names_standing_in_as_ids() -> None:
    """Album and artist names used as stand-in ids are never sent to the API."""
    for value in ("Rebelution", "High Hopes / Low Expectations - EP", "", "a.", "x y"):
        assert not is_apple_id(value)
    assert not is_apple_id(None)


def _provider() -> AppleMusicProvider:
    manifest = Mock()
    manifest.domain = "apple_music"
    config = Mock()
    config.instance_id = "apple_music--test1"
    config.name = "Apple Music Test"
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    provider = AppleMusicProvider(Mock(), manifest, config)
    provider.api_client.get_data = AsyncMock()  # type: ignore[method-assign]
    return provider


@pytest.mark.asyncio
async def test_stand_in_ids_are_not_looked_up() -> None:
    """A name standing in as an id is answered locally, without an API request."""
    provider = _provider()
    with pytest.raises(MediaNotFoundError):
        await provider.get_album("High Hopes / Low Expectations - EP")
    assert await provider.get_album_tracks("High Hopes / Low Expectations - EP") == []
    assert await provider.get_artist_toptracks("Rebelution") == []
    assert await provider.get_similar_artists("Rebelution") == []
    cast("AsyncMock", provider.api_client.get_data).assert_not_awaited()
