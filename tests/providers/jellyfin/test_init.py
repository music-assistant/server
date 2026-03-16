"""Tests for the Jellyfin provider."""

from collections.abc import AsyncGenerator
from unittest import mock

import pytest
from aiojellyfin.testing import FixtureBuilder
from music_assistant_models.config_entries import ProviderConfig

from music_assistant.mass import MusicAssistant
from tests.common import get_fixtures_dir, wait_for_sync_completion


@pytest.fixture
async def jellyfin_provider(mass: MusicAssistant) -> AsyncGenerator[ProviderConfig, None]:
    """Configure an aiojellyfin test fixture, and add a provider to mass that uses it."""
    f = FixtureBuilder()
    async for _, artist in get_fixtures_dir("artists", "jellyfin"):
        f.add_json_bytes(artist)

    async for _, album in get_fixtures_dir("albums", "jellyfin"):
        f.add_json_bytes(album)

    async for _, track in get_fixtures_dir("tracks", "jellyfin"):
        f.add_json_bytes(track)

    authenticate_by_name = f.to_authenticate_by_name()

    with mock.patch(
        "music_assistant.providers.jellyfin.authenticate_by_name", authenticate_by_name
    ):
        async with wait_for_sync_completion(mass):
            config = await mass.config.save_provider_config(
                "jellyfin",
                {
                    "url": "http://localhost",
                    "username": "username",
                    "password": "password",
                },
            )
            await mass.music.start_sync()

        yield config


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
