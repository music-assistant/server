"""Tests for the synopsis to description mapping of the BBC Sounds converters."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.media_items import PodcastEpisode as MAPodcastEpisode
from sounds.models import Podcast, PodcastEpisode, RadioClip, RadioShow

from music_assistant.providers.bbc_sounds.adaptor import Context, PodcastConverter
from music_assistant.providers.bbc_sounds.constants import _Constants

if TYPE_CHECKING:
    from music_assistant.providers.bbc_sounds import BBCSoundsProvider

SYNOPSES = {
    "short": "Short synopsis",
    "medium": "Medium synopsis",
    "long": "Long synopsis",
}


@pytest.fixture
def converter(provider: BBCSoundsProvider) -> PodcastConverter:
    """Create a PodcastConverter bound to the test provider."""
    return PodcastConverter(Context(provider=provider, provider_domain="bbc_sounds"))


def _podcast() -> Podcast:
    return Podcast(id="p01", titles={"primary": "A podcast"}, synopses=SYNOPSES)


@pytest.mark.parametrize(
    ("synopses", "expected"),
    [
        (SYNOPSES, "Long synopsis"),
        ({"short": "Short synopsis", "medium": "Medium synopsis"}, "Medium synopsis"),
        ({"short": "Short synopsis"}, "Short synopsis"),
        ({}, None),
    ],
)
def test_get_synopsis_prefers_the_longest_available(
    converter: PodcastConverter, synopses: dict[str, str], expected: str | None
) -> None:
    """The fullest synopsis wins, and nothing is returned when there is none."""
    assert converter._get_synopsis(Podcast(id="p01", synopses=synopses)) == expected


async def test_podcast_episode_gets_the_long_synopsis(converter: PodcastConverter) -> None:
    """A podcast episode description comes from the long synopsis."""
    episode = PodcastEpisode(
        id="e01",
        pid="e01",
        titles={"primary": "A podcast", "secondary": "An episode"},
        synopses=SYNOPSES,
        container=_podcast(),
    )

    result = await converter.convert(episode)

    assert isinstance(result, MAPodcastEpisode)
    assert result.metadata.description == "Long synopsis"


async def test_radio_show_episode_gets_a_description(converter: PodcastConverter) -> None:
    """A radio show served as an episode carries its synopsis as the description."""
    show = RadioShow(
        id="s01",
        pid="s01",
        duration={"value": _Constants.TRACK_DURATION_THRESHOLD + 1},
        titles={"primary": "A show", "secondary": "An edition"},
        synopses=SYNOPSES,
        container=_podcast(),
    )

    result = await converter.convert(show)

    assert isinstance(result, MAPodcastEpisode)
    assert result.metadata.description == "Long synopsis"


async def test_stream_title_is_the_episode_name(converter: PodcastConverter) -> None:
    """The now playing title is the episode name, never its synopsis."""
    episode = PodcastEpisode(
        id="e01",
        pid="e01",
        titles={"primary": "A podcast", "secondary": "An episode"},
        synopses=SYNOPSES,
        container=_podcast(),
        stream="https://example.invalid/e01.m3u8",
    )

    converter.context.provider.stream_format = _Constants.HLS

    stream_details = await converter.get_stream_details(episode)

    assert stream_details is not None
    assert stream_details.stream_metadata is not None
    assert stream_details.stream_metadata.title == "An episode"


async def test_radio_clip_description_is_a_synopsis(converter: PodcastConverter) -> None:
    """A clip description is its synopsis rather than the name of its network."""
    clip = RadioClip(
        id="c01",
        pid="c01",
        duration={"value": 60},
        titles={"entity_title": "A clip"},
        synopses=SYNOPSES,
    )

    result = await converter.convert(clip)

    assert result.metadata.description == "Long synopsis"
