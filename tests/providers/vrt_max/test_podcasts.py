"""Tests for VRT MAX podcasts / radio programme archives and their episodes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.vrt_max import _MAX_TRACKLIST_EPISODES, VrtMaxProvider
from music_assistant.providers.vrt_max.helpers import (
    VrtApiError,
    VrtChapter,
    VrtEpisode,
    VrtNotFoundError,
    VrtProgram,
    VrtSeason,
)

from .conftest import async_gen, async_gen_then_raise

RADIO_PROGRAM_ID = "/vrtmax/luister/radio/s/show~1-2/"
PODCAST_PROGRAM_ID = "/vrtmax/podcasts/radio-1/h/pod/"


async def test_get_podcast_maps_program_fields(provider: VrtMaxProvider) -> None:
    """get_podcast prepends presenters to the description and copies the publisher/performers."""
    program = VrtProgram(
        RADIO_PROGRAM_ID,
        "Show",
        description="Desc",
        publisher="Radio 1",
        presenters=("Alice", "Bob"),
    )
    provider._client.get_program = AsyncMock(return_value=program)  # type: ignore[method-assign]

    podcast = await provider.get_podcast(RADIO_PROGRAM_ID)

    assert podcast.name == "Show"
    assert podcast.publisher == "Radio 1"
    assert podcast.metadata.performers == {"Alice", "Bob"}
    assert podcast.metadata.description is not None
    assert podcast.metadata.description.startswith("Presentatie: Alice, Bob")
    assert "Desc" in podcast.metadata.description


async def test_get_podcast_not_found_maps_to_media_not_found(provider: VrtMaxProvider) -> None:
    """A genuine not-found from the client maps to MediaNotFoundError."""
    provider._client.get_program = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtNotFoundError("nope")
    )

    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast(RADIO_PROGRAM_ID)


async def test_get_podcast_propagates_transient_api_error(provider: VrtMaxProvider) -> None:
    """A transient VrtApiError propagates so the library sync aborts instead of pruning."""
    provider._client.get_program = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtApiError("boom")
    )

    with pytest.raises(VrtApiError):
        await provider.get_podcast(RADIO_PROGRAM_ID)


async def test_get_podcast_episodes_radio_attaches_tracklist_chapters(
    provider: VrtMaxProvider,
) -> None:
    """Radio-archive episodes get their played-songs tracklist attached as chapters."""
    program = VrtProgram(
        RADIO_PROGRAM_ID, "Show", seasons=(VrtSeason(title="S1", component_id="comp-s1"),)
    )
    provider._client.get_program = AsyncMock(return_value=program)  # type: ignore[method-assign]
    episodes_in = [VrtEpisode(f"{RADIO_PROGRAM_ID}show~1-{i}-0/", f"Ep {i}") for i in range(3)]
    provider._client.iter_season_episodes = async_gen(  # type: ignore[method-assign]
        episodes_in
    )
    chapters = [VrtChapter(1, "Song A", 0.0), VrtChapter(2, "Song B", 120.0)]
    provider._client.get_episode_chapters = AsyncMock(  # type: ignore[method-assign]
        return_value=chapters
    )

    episodes = [ep async for ep in provider.get_podcast_episodes(RADIO_PROGRAM_ID)]

    assert len(episodes) == 3
    assert [ep.position for ep in episodes] == [1, 2, 3]
    for episode in episodes:
        assert episode.metadata.chapters is not None
        assert len(episode.metadata.chapters) == 2
        assert episode.metadata.chapters[0].name == "Song A"
        assert episode.metadata.chapters[1].name == "Song B"
    assert provider._client.get_episode_chapters.await_count == 3


async def test_get_podcast_episodes_podcast_has_no_tracklist(provider: VrtMaxProvider) -> None:
    """Podcast episodes never get a tracklist, and chapters are never fetched for them."""
    program = VrtProgram(
        PODCAST_PROGRAM_ID, "Pod", seasons=(VrtSeason(title="S1", component_id="comp-s1"),)
    )
    provider._client.get_program = AsyncMock(return_value=program)  # type: ignore[method-assign]
    episodes_in = [VrtEpisode(f"{PODCAST_PROGRAM_ID}1/1--ep-{i}/", f"Ep {i}") for i in range(3)]
    provider._client.iter_season_episodes = async_gen(  # type: ignore[method-assign]
        episodes_in
    )
    provider._client.get_episode_chapters = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )

    episodes = [ep async for ep in provider.get_podcast_episodes(PODCAST_PROGRAM_ID)]

    assert len(episodes) == 3
    assert all(episode.metadata.chapters is None for episode in episodes)
    provider._client.get_episode_chapters.assert_not_called()


async def test_get_podcast_episodes_caps_tracklist_fetches(provider: VrtMaxProvider) -> None:
    """Tracklist attachment is capped at _MAX_TRACKLIST_EPISODES, even for a long archive."""
    program = VrtProgram(
        RADIO_PROGRAM_ID, "Show", seasons=(VrtSeason(title="S1", component_id="comp-s1"),)
    )
    provider._client.get_program = AsyncMock(return_value=program)  # type: ignore[method-assign]
    episodes_in = [VrtEpisode(f"{RADIO_PROGRAM_ID}show~1-{i}-0/", f"Ep {i}") for i in range(60)]
    provider._client.iter_season_episodes = async_gen(  # type: ignore[method-assign]
        episodes_in
    )
    provider._client.get_episode_chapters = AsyncMock(  # type: ignore[method-assign]
        return_value=[VrtChapter(1, "Song", 0.0)]
    )

    episodes = [ep async for ep in provider.get_podcast_episodes(RADIO_PROGRAM_ID)]

    assert len(episodes) == 60
    assert provider._client.get_episode_chapters.call_count == _MAX_TRACKLIST_EPISODES
    for episode in episodes[:_MAX_TRACKLIST_EPISODES]:
        assert episode.metadata.chapters is not None
    for episode in episodes[_MAX_TRACKLIST_EPISODES:]:
        assert episode.metadata.chapters is None


async def test_get_podcast_episodes_stops_on_transient_mid_pagination_error(
    provider: VrtMaxProvider,
) -> None:
    """A transient failure mid-pagination stops the listing without dropping what was read."""
    program = VrtProgram(
        RADIO_PROGRAM_ID, "Show", seasons=(VrtSeason(title="S1", component_id="comp-s1"),)
    )
    provider._client.get_program = AsyncMock(return_value=program)  # type: ignore[method-assign]
    episode_0 = VrtEpisode(f"{RADIO_PROGRAM_ID}show~1-0-0/", "Ep 0")
    episode_1 = VrtEpisode(f"{RADIO_PROGRAM_ID}show~1-1-0/", "Ep 1")
    provider._client.iter_season_episodes = async_gen_then_raise(  # type: ignore[method-assign]
        [episode_0, episode_1], VrtApiError("boom")
    )
    provider._client.get_episode_chapters = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )

    episodes = [ep async for ep in provider.get_podcast_episodes(RADIO_PROGRAM_ID)]

    assert [ep.item_id for ep in episodes] == [episode_0.page_id, episode_1.page_id]


async def test_get_podcast_episode_radio_attaches_chapters(provider: VrtMaxProvider) -> None:
    """get_podcast_episode attaches the tracklist for a radio-archive episode."""
    radio_episode_id = f"{RADIO_PROGRAM_ID}show~1-3-0/"
    provider._client.get_episode = AsyncMock(  # type: ignore[method-assign]
        return_value=VrtEpisode(radio_episode_id, "Ep")
    )
    provider._client.get_episode_chapters = AsyncMock(  # type: ignore[method-assign]
        return_value=[VrtChapter(1, "S", 0.0)]
    )

    episode = await provider.get_podcast_episode(radio_episode_id)

    assert episode.metadata.chapters is not None
    assert len(episode.metadata.chapters) == 1


async def test_get_podcast_episode_podcast_has_no_chapters(provider: VrtMaxProvider) -> None:
    """get_podcast_episode never fetches chapters for a podcast episode."""
    podcast_episode_id = f"{PODCAST_PROGRAM_ID}1/1--ep/"
    provider._client.get_episode = AsyncMock(  # type: ignore[method-assign]
        return_value=VrtEpisode(podcast_episode_id, "Ep")
    )
    provider._client.get_episode_chapters = AsyncMock(  # type: ignore[method-assign]
        return_value=[VrtChapter(1, "S", 0.0)]
    )

    episode = await provider.get_podcast_episode(podcast_episode_id)

    assert episode.metadata.chapters is None
    provider._client.get_episode_chapters.assert_not_called()


async def test_get_podcast_episode_not_found(provider: VrtMaxProvider) -> None:
    """A client error on the single-episode fetch maps to MediaNotFoundError."""
    provider._client.get_episode = AsyncMock(  # type: ignore[method-assign]
        side_effect=VrtApiError("boom")
    )

    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode(f"{RADIO_PROGRAM_ID}show~1-3-0/")


def test_episode_item_prefixes_date_label_when_not_in_title(provider: VrtMaxProvider) -> None:
    """The date label is prepended to the episode name when not already part of it."""
    episode = VrtEpisode(
        f"{RADIO_PROGRAM_ID}show~1-3-0/",
        title="Sweet summer sundays",
        date_label="Zondag 9 augustus",
        duration=3600,
        fully_played=True,
        resume_position=42,
    )
    mapping = ItemMapping(
        media_type=MediaType.PODCAST,
        item_id=RADIO_PROGRAM_ID,
        provider=provider.instance_id,
        name="Show",
    )

    item = provider._episode_item(episode, mapping, 1)

    assert item.name == "Zondag 9 augustus - Sweet summer sundays"
    assert item.fully_played is True
    assert item.resume_position_ms == 42000


def test_episode_item_does_not_duplicate_date_label_already_in_title(
    provider: VrtMaxProvider,
) -> None:
    """The date label is not re-prepended when it is already a substring of the title."""
    episode = VrtEpisode(
        f"{RADIO_PROGRAM_ID}show~1-3-0/",
        title="Zondag 9 augustus - Sweet summer sundays",
        date_label="Zondag 9 augustus",
    )
    mapping = ItemMapping(
        media_type=MediaType.PODCAST,
        item_id=RADIO_PROGRAM_ID,
        provider=provider.instance_id,
        name="Show",
    )

    item = provider._episode_item(episode, mapping, 1)

    assert item.name == "Zondag 9 augustus - Sweet summer sundays"
