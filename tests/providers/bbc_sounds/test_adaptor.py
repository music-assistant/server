"""Test BBC Sounds provider adaptor that converts auntie-sounds objects to Music Assistant objects."""

from music_assistant_models.media_items import PodcastEpisode, Track
from sounds import Podcast
from sounds.models import RadioShow

from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.bbc_sounds.constants import _Constants


class TestMenuLoading:
    """Tests for initial/refresh menu loading."""

    async def test_radioshow_is_converted_to_track(self, provider: BBCSoundsProvider) -> None:
        """Test a RadioShow equal to the TRACK_DURATION_THRESHOLD is converted to a Track."""
        show = RadioShow(
            id="id",
            duration={"value": _Constants.TRACK_DURATION_THRESHOLD},
            titles={"entity_title": "Track name"},
            pid="pid",
        )
        assert type(await provider.adaptor.new_object(show)) is Track

    async def test_long_radioshow_is_converted_to_podcast_episode(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a RadioShow over the TRACK_DURATION_THRESHOLD is converted to a PodcastEpisode."""
        show = RadioShow(
            id="id",
            duration={"value": _Constants.TRACK_DURATION_THRESHOLD + 1},
            titles={"entity_title": "Track name"},
            pid="pid",
            container=Podcast(
                type="brand",
                id="p07f4d9w",
                title="Podcast",
                description=None,
                image_url=None,
                synopses={},
                titles={},
                urn="urn:bbc:radio:brand:p07f4d9w",
            ),
        )
        assert type(await provider.adaptor.new_object(show)) is PodcastEpisode

    async def test_long_radioshow_with_no_container_is_converted_to_track(
        self, provider: BBCSoundsProvider
    ) -> None:
        """Test a RadioShow over the TRACK_DURATION_THRESHOLD without a container is converted to a Track."""
        show = RadioShow(
            id="id",
            duration={"value": _Constants.TRACK_DURATION_THRESHOLD + 1},
            titles={"entity_title": "Track name"},
            pid="pid",
        )
        assert type(await provider.adaptor.new_object(show)) is Track
