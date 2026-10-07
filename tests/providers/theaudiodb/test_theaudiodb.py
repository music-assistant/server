"""Tests for the identifier backfill of TheAudioDB metadata provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import AlbumType, ExternalID
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.providers.theaudiodb import (
    CONF_ENABLE_ALBUM_METADATA,
    CONF_ENABLE_IMAGES,
    CONF_ENABLE_TRACK_METADATA,
    SUPPORTED_FEATURES,
    AudioDbMetadataProvider,
)

ARTIST_MBID = "7cf0ea9d-86b9-4dad-ba9e-2355a64899ea"
RELEASEGROUP_MBID = "1b022e01-4da6-387b-8658-8678046e4cef"
RECORDING_MBID = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"


async def test_album_backfill_runs_with_images_disabled() -> None:
    """Album and artist identifiers are backfilled even when images are turned off."""
    provider = _provider(images_enabled=False)
    artist = _artist("Nirvana")
    album = Album(
        item_id="1",
        provider="library",
        name="Nevermind",
        artists=UniqueList([artist]),
        external_ids={(ExternalID.MB_RELEASEGROUP, RELEASEGROUP_MBID)},
        provider_mappings=_mappings("1"),
    )
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"album": [_adb_album()]}
    )

    metadata = await provider.get_album_metadata(album)

    assert metadata is not None
    assert metadata.images is None
    assert album.year == 1991
    assert album.album_type == AlbumType.ALBUM
    assert artist.mbid == ARTIST_MBID
    cast("AsyncMock", provider.mass.music.artists.update_item_in_library).assert_awaited_once()


@pytest.mark.parametrize(
    "library_album",
    [
        "Nevermind",
        # the retail suffix Apple Music appends carries no identity of its own
        "Nevermind - EP",
    ],
)
async def test_track_release_group_written_when_album_matches(library_album: str) -> None:
    """The album gets TheAudioDB's release group id when the matched record is that album."""
    # images off, so this covers the track backfill not being tied to the images option
    provider = _provider(images_enabled=False)
    track = _track(library_album)
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"track": [_adb_track()]}
    )

    await provider.get_track_metadata(track)

    cast("AsyncMock", provider.mass.music.albums.set_release_group).assert_awaited_once_with(
        1, RELEASEGROUP_MBID
    )


@pytest.mark.parametrize(
    ("library_album", "adb_album"),
    [
        ("Nirvana: The Best Of", "Nevermind"),
        # near-identical titles are still separate releases
        ("Vol. 1", "Vol. 2"),
    ],
)
async def test_track_release_group_skipped_for_another_release(
    library_album: str, adb_album: str
) -> None:
    """A recording shared with another release must not stamp its id on our album."""
    provider = _provider()
    track = _track(library_album)
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"track": [_adb_track(adb_album)]}
    )

    await provider.get_track_metadata(track)

    cast("AsyncMock", provider.mass.music.albums.set_release_group).assert_not_awaited()
    # the artist backfill is independent of the album, so it still happens
    assert track.artists[0].mbid == ARTIST_MBID


def _provider(*, images_enabled: bool = True) -> AudioDbMetadataProvider:
    """
    Return a provider with mocked dependencies and every metadata lookup enabled.

    :param images_enabled: Whether the provider's image option is turned on.
    """
    mass = AsyncMock()
    mass.metadata.locale = "en_US"
    manifest = MagicMock()
    manifest.domain = "theaudiodb"
    config = MagicMock()
    flags = {
        CONF_ENABLE_ALBUM_METADATA: True,
        CONF_ENABLE_TRACK_METADATA: True,
        CONF_ENABLE_IMAGES: images_enabled,
    }
    config.get_value.side_effect = lambda key: flags.get(key, "GLOBAL")
    return AudioDbMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _mappings(item_id: str) -> set[ProviderMapping]:
    """Return a single library provider mapping for the given item id."""
    return {ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")}


def _artist(name: str) -> Artist:
    """Return a library artist without a MusicBrainz id."""
    return Artist(
        item_id="artist",
        provider="library",
        name=name,
        provider_mappings=_mappings("artist"),
    )


def _track(album_name: str) -> Track:
    """Return a library track matched by its MusicBrainz recording id."""
    return Track(
        item_id="track",
        provider="library",
        name="Come as You Are",
        external_ids={(ExternalID.MB_RECORDING, RECORDING_MBID)},
        artists=UniqueList([_artist("Nirvana")]),
        album=Album(
            item_id="1",
            provider="library",
            name=album_name,
            provider_mappings=_mappings("1"),
        ),
        provider_mappings=_mappings("track"),
    )


def _adb_album() -> dict[str, Any]:
    """Return a minimal TheAudioDB album record."""
    return {
        "strArtist": "Nirvana",
        "strAlbum": "Nevermind",
        "strMusicBrainzArtistID": ARTIST_MBID,
        "intYearReleased": "1991",
        "strReleaseFormat": "Album",
    }


def _adb_track(album_name: str = "Nevermind") -> dict[str, Any]:
    """Return a minimal TheAudioDB track record."""
    return {
        "strArtist": "Nirvana",
        "strAlbum": album_name,
        "strTrack": "Come as You Are",
        "strMusicBrainzArtistID": ARTIST_MBID,
        "strMusicBrainzAlbumID": RELEASEGROUP_MBID,
    }
