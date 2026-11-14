"""Test the formats a Deezer account asks for."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from music_assistant.providers.deezer.constants import CONF_QUALITY
from music_assistant.providers.deezer.gw_client import GWClient
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider


async def _formats(user_data: dict[str, Any], max_format: str = "FLAC_24") -> list[str]:
    """Return the formats a client asks for after setup with the given user data."""
    client = GWClient(Mock(), "arl", max_format=max_format)
    with patch.object(GWClient, "_gw_api_call", AsyncMock(return_value=user_data)):
        await client.setup()
    return [fmt["format"] for fmt in client.formats]


@pytest.mark.parametrize(
    ("max_format", "expected"),
    [
        ("FLAC_24", ["FLAC_24", "FLAC", "MP3_320", "MP3_128"]),
        ("FLAC", ["FLAC", "MP3_320", "MP3_128"]),
        ("MP3_320", ["MP3_320", "MP3_128"]),
    ],
)
async def test_quality_setting_caps_the_formats(
    gw_user_data: dict[str, Any], max_format: str, expected: list[str]
) -> None:
    """A lossless account asks for the best format the setting allows, Deezer falls back."""
    assert await _formats(gw_user_data, max_format) == expected


async def test_account_without_lossless_keeps_its_formats(gw_user_data: dict[str, Any]) -> None:
    """The setting never adds a format the account does not have."""
    for qualities in ("web_sound_quality", "mobile_sound_quality"):
        gw_user_data["results"]["USER"]["OPTIONS"][qualities]["lossless"] = False

    assert await _formats(gw_user_data) == ["MP3_320", "MP3_128"]


async def test_provider_passes_the_quality_setting() -> None:
    """The client gets the quality chosen in the settings."""
    mass = Mock()
    mass.config.get.return_value = {}
    manifest = Mock()
    manifest.domain = "deezer"
    config = Mock()
    config.instance_id = "deezer--test"
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_QUALITY: "FLAC",
    }.get(key, default)
    provider = DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)
    with (
        patch("music_assistant.providers.deezer.provider.ThrottledGQLClient") as gql_client,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id="user123"))
        gw_client.return_value.setup = AsyncMock()
        await provider.handle_async_init()

    assert gw_client.call_args.kwargs["max_format"] == "FLAC"
