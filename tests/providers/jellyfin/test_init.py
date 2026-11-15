"""Tests for the Jellyfin provider."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import aiofiles
import pytest
from aiojellyfin.testing import FixtureBuilder
from music_assistant_models.enums import MediaType

from music_assistant.mass import MusicAssistant
from music_assistant.providers.jellyfin import JellyfinProvider
from tests.common import get_fixtures_dir, wait_for_sync_completion

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.streamdetails import StreamDetails

TRACK_FIXTURE = Path(__file__).parent / "fixtures" / "tracks" / "do_i_wanna_know.json"
TRACK_ID = "da9c458e425584680765ddc3a89cbc0c"


@pytest.fixture
async def jellyfin_provider(mass: MusicAssistant) -> AsyncGenerator[ProviderConfig]:
    """Configure an aiojellyfin test fixture, and add a provider to mass that uses it."""
    f = FixtureBuilder()
    async for _, artist in get_fixtures_dir("artists", "jellyfin"):
        f.add_json_bytes(artist)

    async for _, album in get_fixtures_dir("albums", "jellyfin"):
        f.add_json_bytes(album)

    async for _, track in get_fixtures_dir("tracks", "jellyfin"):
        f.add_json_bytes(track)

    async with _setup_provider(mass, f) as config:
        yield config


@pytest.mark.usefixtures("jellyfin_provider")
async def test_get_artist_albums(mass: MusicAssistant) -> None:
    """Test that get_artist_albums returns albums for a real artist ID."""
    artists = await mass.music.artists.library_items(search="Ash", summary=False)
    ash = artists[0]
    prov_mapping = next(m for m in ash.provider_mappings if m.provider_domain == "jellyfin")
    albums = await mass.music.artists.get_provider_artist_albums(
        prov_mapping.item_id, prov_mapping.provider_instance
    )
    assert any(album.name == "Nu-Clear Sounds" for album in albums)


@pytest.mark.usefixtures("jellyfin_provider")
async def test_initial_sync(mass: MusicAssistant) -> None:
    """Test that initial sync worked."""
    artists = await mass.music.artists.library_items(search="Ash")
    assert artists[0].name == "Ash"

    albums = await mass.music.albums.library_items(search="christmas")
    assert albums[0].name == "This Is Christmas"

    tracks = await mass.music.tracks.library_items(search="where the bands are")
    assert tracks[0].name == "Where the Bands Are"
    assert tracks[0].version == "2018 Version"


async def test_get_stream_details_loudness(
    mass: MusicAssistant, jellyfin_provider: ProviderConfig
) -> None:
    """Test stream details carry and store loudness derived from Jellyfin's ReplayGain."""
    stream_details, set_track_loudness = await _get_stream_details_with_loudness(
        mass, jellyfin_provider, TRACK_ID
    )

    assert stream_details.loudness == pytest.approx(-9.6)
    assert stream_details.loudness_album is None
    set_track_loudness.assert_awaited_once_with(
        item_id=TRACK_ID,
        provider_instance_id_or_domain=jellyfin_provider.instance_id,
        loudness=stream_details.loudness,
        loudness_album=None,
    )


async def test_get_stream_details_album_loudness(mass: MusicAssistant) -> None:
    """Test album loudness from Jellyfin's AlbumNormalizationGain reaches stream details."""
    async with aiofiles.open(TRACK_FIXTURE, encoding="utf-8") as fp:
        track = json.loads(await fp.read())
    track["Id"] = "album-gain-track"
    track["AlbumNormalizationGain"] = -10.0
    fixture = FixtureBuilder()
    fixture.add_json_bytes(json.dumps(track))

    async with _setup_provider(mass, fixture) as config:
        stream_details, set_track_loudness = await _get_stream_details_with_loudness(
            mass, config, "album-gain-track"
        )

    assert stream_details.loudness == pytest.approx(-9.6)
    assert stream_details.loudness_album == pytest.approx(-8.0)
    set_track_loudness.assert_awaited_once_with(
        item_id="album-gain-track",
        provider_instance_id_or_domain=config.instance_id,
        loudness=stream_details.loudness,
        loudness_album=stream_details.loudness_album,
    )


@asynccontextmanager
async def _setup_provider(
    mass: MusicAssistant, fixture: FixtureBuilder
) -> AsyncIterator[ProviderConfig]:
    """Add a Jellyfin provider backed by the given aiojellyfin fixture and sync it."""
    with mock.patch(
        "music_assistant.providers.jellyfin.authenticate_by_name",
        fixture.to_authenticate_by_name(),
    ):
        async with wait_for_sync_completion(mass):
            config = await mass.config._create_provider_instance(
                "jellyfin",
                {},
                # connection details are collected by the setup flow and live in setup_data
                setup_data=mass.config._encrypt_values(
                    {
                        "url": "http://localhost",
                        "username": "username",
                        "password": "password",
                    }
                ),
            )
            await mass.music.start_sync()

        yield config


async def _get_stream_details_with_loudness(
    mass: MusicAssistant, config: ProviderConfig, item_id: str
) -> tuple[StreamDetails, mock.AsyncMock]:
    """Get stream details and await the loudness store task, returning its mock."""
    provider = mass.get_provider(config.instance_id)
    assert isinstance(provider, JellyfinProvider)
    set_track_loudness = mock.AsyncMock()
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(target: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any) -> None:
        tasks.append(asyncio.create_task(target))

    with (
        mock.patch.object(mass.streams.audio_analysis, "set_track_loudness", set_track_loudness),
        mock.patch.object(mass, "create_task", _create_task),
    ):
        stream_details = await provider.get_stream_details(item_id, MediaType.TRACK)
        await asyncio.gather(*tasks)
    return stream_details, set_track_loudness
