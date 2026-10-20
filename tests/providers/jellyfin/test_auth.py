"""Authentication compatibility tests for the Jellyfin provider."""

from typing import cast
from unittest import mock

import pytest
from music_assistant_models.enums import MediaType, StreamType

from music_assistant.mass import MusicAssistant
from music_assistant.providers.jellyfin import JellyfinProvider
from music_assistant.providers.jellyfin.const import (
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_CHANNELS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_SOURCES,
    ITEM_KEY_MEDIA_STREAM_TYPE,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_RUNTIME_TICKS,
)


@pytest.fixture
async def jellyfin_provider(mass: MusicAssistant) -> JellyfinProvider:
    """Load a Jellyfin provider with a mocked aiojellyfin client."""
    client = mock.Mock()
    client.get_track = mock.AsyncMock()
    client.audio_url = mock.Mock()

    with mock.patch(
        "music_assistant.providers.jellyfin.authenticate_by_name",
        mock.AsyncMock(return_value=client),
    ):
        await mass.config._create_provider_instance(
            "jellyfin",
            {},
            setup_data=mass.config._encrypt_values(
                {
                    "url": "http://localhost",
                    "username": "username",
                    "password": "password",
                }
            ),
        )

    provider = mass.get_provider("jellyfin", return_unavailable=True)
    assert provider is not None
    assert isinstance(provider, JellyfinProvider)
    return provider


async def test_get_stream_details_normalizes_legacy_audio_url(
    jellyfin_provider: JellyfinProvider,
) -> None:
    """Return normalized stream details for legacy Jellyfin audio URLs."""
    client = cast("mock.Mock", jellyfin_provider._client)
    track = _track_payload("track-1")
    client.get_track.return_value = track
    client.audio_url.return_value = (
        "https://jellyfin.example.com/emby/Items/track-1/stream.mp3"
        "?static=true&api_key=a%2Fb%3D&foo=bar#cover"
    )

    details = await jellyfin_provider.get_stream_details("track-1", MediaType.TRACK)

    assert details.item_id == "track-1"
    assert details.path == (
        "https://jellyfin.example.com/emby/Items/track-1/stream.mp3"
        "?static=true&apiKey=a%2Fb%3D&foo=bar#cover"
    )
    assert details.stream_type is StreamType.HTTP
    assert details.can_seek is True
    assert details.allow_seek is True
    assert details.duration == 180


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?api_key=a%2Fb%3D&x=1",
            "https://jellyfin.example.com/Items/track-1/art.jpg?apiKey=a%2Fb%3D&x=1",
        ),
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?foo&api_key=a%2Fb%3D&foo=bar&foo=",
            "https://jellyfin.example.com/Items/track-1/art.jpg?foo=&apiKey=a%2Fb%3D&foo=bar&foo=",
        ),
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?apiKey=a%2Fb%3D&x=1",
            "https://jellyfin.example.com/Items/track-1/art.jpg?apiKey=a%2Fb%3D&x=1",
        ),
        (
            "https://jellyfin.example.com/Items/track-1/art.jpg?x=1",
            "https://jellyfin.example.com/Items/track-1/art.jpg?x=1",
        ),
    ],
)
async def test_resolve_image_normalizes_legacy_urls(
    jellyfin_provider: JellyfinProvider, raw_url: str, expected_url: str
) -> None:
    """Return artwork URLs with Jellyfin's supported auth parameter name."""
    assert await jellyfin_provider.resolve_image(raw_url) == expected_url


def _track_payload(track_id: str) -> dict[str, object]:
    """Build a minimal Jellyfin track payload."""
    return {
        ITEM_KEY_ID: track_id,
        ITEM_KEY_RUNTIME_TICKS: 1_800_000_000,
        ITEM_KEY_MEDIA_SOURCES: [{"Container": "mp3"}],
        ITEM_KEY_MEDIA_STREAMS: [
            {
                ITEM_KEY_MEDIA_STREAM_TYPE: "Audio",
                ITEM_KEY_MEDIA_CODEC: "mp3",
                ITEM_KEY_MEDIA_CHANNELS: 2,
                "SampleRate": 48_000,
                "BitDepth": 16,
            }
        ],
    }
