"""Test BBC Sounds provider adaptor that converts auntie-sounds objects to Music Assistant objects."""

import pytest
from auntie_sounds.models import (
    Duration,
    Playlist,
    Podcast,
    PodcastEpisode,
    RadioShow,
    Synopses,
    Titles,
)
from music_assistant_models.media_items import BrowseFolder, Track, media_from_dict
from music_assistant_models.media_items import PodcastEpisode as MAPodcastEpisode

from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.constants import Constants

PODCAST = Podcast(id="brand", title="Podcast", titles=Titles(primary="Podcast"))


class TestMenuLoading:
    """Tests for initial/refresh menu loading."""

    async def test_radioshow_is_converted_to_track(self, provider: BBCSoundsProvider) -> None:
        """Test a RadioShow equal to the TRACK_DURATION_THRESHOLD is converted to a Track."""
        show = RadioShow(
            id="id",
            duration=Duration(label="", value=Constants.TRACK_DURATION_THRESHOLD),
            titles=Titles(entity_title="Track name"),
            pid="pid",
        )
        assert type(await provider.adaptor.new_object(show)) is Track

    async def test_long_radioshow_is_converted_to_podcast_episode(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a RadioShow over the TRACK_DURATION_THRESHOLD is converted to a PodcastEpisode."""
        show = RadioShow(
            id="id",
            duration=Duration(label="", value=Constants.TRACK_DURATION_THRESHOLD + 1),
            titles=Titles(entity_title="Track name"),
            pid="pid",
            container=Podcast(
                type="brand",
                id="p07f4d9w",
                title="Podcast",
                description=None,
                image_url=None,
                synopses=Synopses(),
                titles=Titles(),
                urn="urn:bbc:radio:brand:p07f4d9w",
            ),
        )
        assert type(await provider.adaptor.new_object(show)) is MAPodcastEpisode

    async def test_long_radioshow_with_no_container_is_converted_to_track(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a RadioShow over the TRACK_DURATION_THRESHOLD without a container is converted to a Track."""
        show = RadioShow(
            id="id",
            duration=Duration(label="", value=Constants.TRACK_DURATION_THRESHOLD + 1),
            titles=Titles(entity_title="Track name"),
            pid="pid",
        )
        assert type(await provider.adaptor.new_object(show)) is Track

    async def test_short_radio_show_with_podcast_container_can_be_forced_to_podcast_episode(
        self, provider: BBCSoundsProvider
    ) -> None:
        """
        Regression test for forced podcast episode.

        Ensure a show radio show with a valid podcast container doesn't get converted to
        a Track when it's force type is a podcast episode.
        """
        show = RadioShow(
            id="id",
            duration=Duration(label="", value=Constants.TRACK_DURATION_THRESHOLD - 1),
            titles=Titles(entity_title="Track name"),
            pid="pid",
            container=Podcast(id="podcast"),
        )
        assert type(await provider.adaptor.new_object(show, force_type=MAPodcastEpisode))

    async def test_playlist_is_converted_to_browse_folder(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test that a Playlist object is converted to a BrowseFolder by default."""
        playlist = Playlist(
            id="playlist",
        )
        assert type(await provider.adaptor.new_object(playlist)) is BrowseFolder


class TestRoundTrip:
    """Tests for items that are serialised, e.g. into a saved queue, and rebuilt."""

    @pytest.mark.parametrize(
        "item",
        [
            pytest.param(
                RadioShow(id="vpid", pid="pid", titles=Titles(primary="Show"), container=PODCAST),
                id="radio-show",
            ),
            pytest.param(
                PodcastEpisode(
                    id="vpid", pid="pid", titles=Titles(primary="Episode"), container=PODCAST
                ),
                id="podcast-episode",
            ),
        ],
    )
    async def test_episode_without_a_duration_can_be_rebuilt(
        self, provider: BBCSoundsProvider, item: RadioShow | PodcastEpisode
    ) -> None:
        """Test an episode the BBC gives no duration for survives a round trip."""
        episode = await provider.adaptor.new_object(item, force_type=MAPodcastEpisode)

        assert isinstance(episode, MAPodcastEpisode)
        assert media_from_dict(episode.to_dict()) == episode
