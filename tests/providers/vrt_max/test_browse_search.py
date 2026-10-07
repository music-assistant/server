"""Tests for VRT MAX browse and search."""

from __future__ import annotations

from unittest.mock import AsyncMock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import BrowseFolder, Podcast, Radio

from music_assistant.providers.vrt_max.constants import (
    BROWSE_PODCASTS,
    BROWSE_RADIO_PROGRAMS,
    BROWSE_RADIOS,
    STATIONS,
)
from music_assistant.providers.vrt_max.models import (
    VrtEpisode,
    VrtProgramTile,
    VrtRow,
)
from music_assistant.providers.vrt_max.provider import (
    VrtMaxProvider,
    _encode,
    _program_id_from_episode,
)

from .conftest import async_gen


async def test_browse_root_with_account(provider: VrtMaxProvider) -> None:
    """With an account, browsing the root returns the three top-level folders."""
    provider._auth.enabled = True  # type: ignore[misc]

    result = await provider.browse(f"{provider.instance_id}://")

    assert len(result) == 3
    assert all(isinstance(item, BrowseFolder) for item in result)
    assert {item.item_id for item in result} == {
        BROWSE_RADIOS,
        BROWSE_RADIO_PROGRAMS,
        BROWSE_PODCASTS,
    }


async def test_browse_root_without_account_offers_only_live_radio(
    provider: VrtMaxProvider,
) -> None:
    """Without an account only live radio is playable, so only that folder is offered."""
    provider._auth.enabled = False  # type: ignore[misc]

    result = await provider.browse(f"{provider.instance_id}://")

    assert [item.item_id for item in result] == [BROWSE_RADIOS]


async def test_browse_radios(provider: VrtMaxProvider) -> None:
    """Browsing the radios folder returns one Radio item per station."""
    result = await provider.browse(f"{provider.instance_id}://{BROWSE_RADIOS}")

    assert len(result) == len(STATIONS)
    assert all(isinstance(item, Radio) for item in result)


async def test_browse_radio_landing_filters_rows(provider: VrtMaxProvider) -> None:
    """Only titled RadioProgramTile rows become folders on the radio landing page."""
    provider._client.get_landing_rows = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            VrtRow("Show A", "comp1", "RadioProgramTile"),
            VrtRow("", "comp2", "RadioProgramTile"),
            VrtRow("Other", "comp3", "SomethingElse"),
        ]
    )

    result = await provider.browse(f"{provider.instance_id}://{BROWSE_RADIO_PROGRAMS}")

    assert len(result) == 1
    folder = result[0]
    assert isinstance(folder, BrowseFolder)
    assert folder.name == "Show A"
    encoded = _encode("comp1")
    assert folder.path == f"{provider.instance_id}://{BROWSE_RADIO_PROGRAMS}/{encoded}"


async def test_browse_programs_of_a_landing_row(provider: VrtMaxProvider) -> None:
    """Browsing a landing-row folder lists its programs as Podcast items."""
    tile = VrtProgramTile("/p/1", "Prog One", "desc", "http://img")
    provider._client.iter_programs = async_gen([tile])  # type: ignore[method-assign]
    encoded = _encode("comp1")

    result = await provider.browse(f"{provider.instance_id}://{BROWSE_RADIO_PROGRAMS}/{encoded}")

    assert len(result) == 1
    podcast = result[0]
    assert isinstance(podcast, Podcast)
    assert podcast.name == "Prog One"
    assert podcast.metadata.description == "desc"
    assert podcast.metadata.images is not None
    assert len(podcast.metadata.images) == 1


async def test_search_radio_matches_by_name(provider: VrtMaxProvider) -> None:
    """A RADIO search returns stations whose name contains the query, case-insensitively."""
    results = await provider.search("radio", [MediaType.RADIO], limit=5)

    assert results.radio
    for station in results.radio:
        assert "radio" in station.name.lower()


async def test_search_podcast_folds_episodes_to_parent_program(provider: VrtMaxProvider) -> None:
    """PODCAST search combines podcast tiles with radio episodes folded to their program."""
    tile = VrtProgramTile("/vrtmax/podcasts/x", "Pod X")
    episode = VrtEpisode("/vrtmax/luister/radio/s/show~1-2/show~1-3-0/", "Show Ep")
    provider._client.search_podcast_programs = AsyncMock(  # type: ignore[method-assign]
        return_value=[tile]
    )
    provider._client.search_radio_episodes = AsyncMock(  # type: ignore[method-assign]
        return_value=[episode]
    )

    results = await provider.search("show", [MediaType.PODCAST], limit=5)

    assert len(results.podcasts) == 2
    ids = {podcast.item_id for podcast in results.podcasts}
    assert tile.page_id in ids
    assert _program_id_from_episode(episode.page_id) in ids


async def test_search_podcast_dedupes_episodes_from_the_same_program(
    provider: VrtMaxProvider,
) -> None:
    """Two radio episodes of the same program fold up to a single Podcast result."""
    ep1 = VrtEpisode("/vrtmax/luister/radio/s/show~1-2/show~1-3-0/", "Ep 1")
    ep2 = VrtEpisode("/vrtmax/luister/radio/s/show~1-2/show~1-4-0/", "Ep 2")
    provider._client.search_podcast_programs = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )
    provider._client.search_radio_episodes = AsyncMock(  # type: ignore[method-assign]
        return_value=[ep1, ep2]
    )

    results = await provider.search("show", [MediaType.PODCAST], limit=5)

    assert len(results.podcasts) == 1
