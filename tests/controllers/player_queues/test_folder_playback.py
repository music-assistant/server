"""Tests for resolving a browse folder into playable queue items."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    BrowseFolder,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
)

from music_assistant.mass import MusicAssistant

PROVIDER = "test_folder_prov"


def _provider_mapping() -> set[ProviderMapping]:
    """Create a single provider mapping with a unique item id."""
    return {
        ProviderMapping(item_id=uuid4().hex, provider_domain=PROVIDER, provider_instance=PROVIDER)
    }


async def test_folder_plays_every_playable_item(mass: MusicAssistant) -> None:
    """Episodes and radios in a folder are queued alongside tracks, with resume applied."""
    user = await mass.webserver.auth.create_user("folderplayback")
    podcast = Podcast(
        item_id="show-1", provider=PROVIDER, name="Show", provider_mappings=_provider_mapping()
    )
    episode = PodcastEpisode(
        item_id="ep-1",
        provider=PROVIDER,
        name="Episode 1",
        provider_mappings=_provider_mapping(),
        position=1,
        podcast=podcast,
    )
    radio = Radio(
        item_id="radio-1", provider=PROVIDER, name="Radio", provider_mappings=_provider_mapping()
    )
    track = Track(
        item_id="track-1", provider=PROVIDER, name="Track", provider_mappings=_provider_mapping()
    )
    subfolder = BrowseFolder(item_id="sub", provider=PROVIDER, name="Sub")
    await mass.music.mark_item_played(
        episode,
        fully_played=False,
        seconds_played=90,
        user_initiated=True,
        userid=user.user_id,
    )
    folder = BrowseFolder(item_id="up_next", provider=PROVIDER, name="Up Next")

    with patch.object(
        mass.music, "browse", AsyncMock(return_value=[subfolder, episode, radio, track])
    ):
        items = await mass.player_queues._media_resolver._resolve_media_items(
            folder, userid=user.user_id
        )

    assert [x.media_type for x in items] == [
        MediaType.PODCAST_EPISODE,
        MediaType.RADIO,
        MediaType.TRACK,
    ]
    assert isinstance(items[0], PodcastEpisode)
    assert items[0].resume_position_ms == 90_000
