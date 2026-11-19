"""Tests for iTunes Podcasts episode ids whose episode part holds a space."""

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType

from music_assistant.providers.itunes_podcasts import SUPPORTED_FEATURES, ITunesPodcastsProvider

FEED = "https://example.com/feed.xml"
STREAM_URL = "https://example.com/Episode54 Indy 1.mp3"


async def test_episode_with_a_space_in_guid_and_stream_url() -> None:
    """The id falls back to the stream url, which still resolves to the episode."""
    config = Mock(instance_id="itunes_podcasts--test")
    config.get_value.side_effect = lambda key, default=None: {
        "num_episodes": 0,
        "log_level": "INFO",
    }.get(key, default)
    provider = ITunesPodcastsProvider(
        Mock(), Mock(domain="itunes_podcasts"), config, SUPPORTED_FEATURES
    )
    await provider.handle_async_init()
    podcast = {
        "title": "Podcast",
        "episodes": [
            {
                "title": "Episode 54",
                "guid": "Episode54 Indy 1.mp3",
                "enclosures": [{"url": STREAM_URL, "mime_type": "audio/mpeg"}],
                "published": 1_700_000_000,
                "total_time": 1200,
            }
        ],
    }
    provider._cache_get_podcast = AsyncMock(return_value=podcast)  # type: ignore[method-assign]
    item_id = f"{FEED} {STREAM_URL}"

    episode = await provider.get_podcast_episode(item_id)
    details = await provider.get_stream_details(item_id, MediaType.PODCAST_EPISODE)

    assert episode.item_id == item_id
    assert details.path == STREAM_URL
