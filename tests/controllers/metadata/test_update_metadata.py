"""Tests for the metadata update of a library item."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Artist, ProviderMapping

from music_assistant.controllers.metadata import MetaDataController
from music_assistant.mass import MusicAssistant
from music_assistant.providers.musicbrainz.models import MusicBrainzArtist

ARTIST_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"


@pytest.fixture(name="mass")
async def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a real metadata controller."""
    # the stored audio analysis a library removal also drops
    music_mass.streams = Mock()
    music_mass.streams.audio_analysis.delete_audio_analysis = AsyncMock()
    music_mass.metadata = MetaDataController(music_mass)
    await music_mass.metadata.setup(
        await music_mass.config.get_core_config(music_mass.metadata.domain)
    )
    return music_mass


async def _add_artist(mass: MusicAssistant) -> Artist:
    """Add an identified library artist mapped to qobuz."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Berliner Philharmoniker",
            provider_mappings={
                ProviderMapping(item_id="bp", provider_domain="qobuz", provider_instance="qobuz_1")
            },
            external_ids={(ExternalID.MB_ARTIST, ARTIST_MBID)},
        )
    )


async def test_update_of_an_item_removed_from_the_library_is_skipped(
    mass: MusicAssistant,
) -> None:
    """An item removed from the library while its update was queued ends the update quietly."""
    artist = await _add_artist(mass)
    await mass.music.artists.remove_item_from_library(artist.item_id)

    with patch.object(mass.metadata, "logger") as logger:
        assert await mass.metadata.update_metadata(artist, force_refresh=True) is artist

    logger.warning.assert_not_called()
    assert "was removed from the library" in logger.debug.call_args.args[0]


async def test_update_by_uri_of_an_item_removed_from_the_library_is_skipped(
    mass: MusicAssistant,
) -> None:
    """An item removed from the library while its update by uri was queued ends quietly."""
    artist = await _add_artist(mass)
    assert artist.uri is not None
    await mass.music.artists.remove_item_from_library(artist.item_id)

    with patch.object(mass.metadata, "logger") as logger:
        assert await mass.metadata.update_metadata(artist.uri, force_refresh=True) is None

    logger.warning.assert_not_called()
    assert "was removed from the library" in logger.debug.call_args.args[0]


async def test_update_by_uri_still_fails_on_a_missing_provider_item(
    mass: MusicAssistant,
) -> None:
    """A provider uri that is not found is raised, as only a library item can be removed."""
    with (
        patch.object(
            mass.music, "get_item_by_uri", AsyncMock(side_effect=MediaNotFoundError("missing"))
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.metadata.update_metadata("qobuz://artist/missing")


async def test_link_of_an_item_removed_from_the_library_is_skipped(
    mass: MusicAssistant,
) -> None:
    """An item removed from the library while it is linked through MusicBrainz is skipped."""
    artist = await _add_artist(mass)
    musicbrainz = Mock()
    musicbrainz.get_artist_details = AsyncMock(
        return_value=MusicBrainzArtist(id=ARTIST_MBID, name=artist.name, sort_name=artist.name)
    )

    async def _removed_meanwhile(db_item: Artist, _urls: list[str]) -> None:
        await mass.music.artists.remove_item_from_library(db_item.item_id)
        await mass.music.artists.get_library_item(db_item.item_id)

    with (
        patch.object(mass.metadata, "logger") as logger,
        patch.object(mass.metadata, "_musicbrainz_provider", return_value=musicbrainz),
        patch.object(
            mass.music.artists, "link_musicbrainz_mappings", side_effect=_removed_meanwhile
        ),
    ):
        await mass.metadata.update_metadata(artist, force_refresh=True)

    logger.warning.assert_not_called()
    assert "was removed from the library" in logger.debug.call_args.args[0]


async def test_update_still_fails_on_a_missing_item_while_the_item_exists(
    mass: MusicAssistant,
) -> None:
    """Something else not found during the update of an item still in the library is raised."""
    artist = await _add_artist(mass)

    with (
        patch.object(
            mass.metadata,
            "_update_artist_metadata",
            AsyncMock(side_effect=MediaNotFoundError("other item")),
        ),
        pytest.raises(MediaNotFoundError),
    ):
        await mass.metadata.update_metadata(artist, force_refresh=True)
