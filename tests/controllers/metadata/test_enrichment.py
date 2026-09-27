"""Tests for metadata enrichment resilience to provider failures."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ExternalID, ProviderFeature
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata

from music_assistant.constants import VARIOUS_ARTISTS_MBID
from music_assistant.controllers.metadata.constants import CONF_ENABLE_ONLINE_METADATA
from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin


def _online_metadata_only(key: str, *_args: Any, **_kwargs: Any) -> bool:
    """Config stub: only online metadata is enabled (prefer-local-genres off)."""
    return key == CONF_ENABLE_ONLINE_METADATA


def _metadata_provider(name: str, feature: ProviderFeature) -> MagicMock:
    """Return a mock metadata provider advertising a single metadata feature."""
    provider = MagicMock()
    provider.name = name
    provider.priority = 0
    provider.supported_features = {feature}
    return provider


@pytest.mark.asyncio
async def test_album_enrichment_survives_provider_error() -> None:
    """A raising album metadata provider is logged and skipped; later providers still run."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.mass.music.albums.update_item_in_library = AsyncMock()

    boom = _metadata_provider("boom", ProviderFeature.ALBUM_METADATA)
    boom.get_album_metadata = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    good = _metadata_provider("good", ProviderFeature.ALBUM_METADATA)
    good.get_album_metadata = AsyncMock(return_value=None)
    enrichment.providers = [boom, good]  # type: ignore[misc]

    album = Album(
        item_id="1",
        provider="library",
        name="Test Album",
        provider_mappings=set(),
        metadata=MediaItemMetadata(),
    )

    # a transient provider error must not abort enrichment
    await enrichment._update_album_metadata(album, force_refresh=True)

    boom.get_album_metadata.assert_awaited_once()
    good.get_album_metadata.assert_awaited_once()  # loop continued past the failing provider
    enrichment.logger.warning.assert_called_once()
    enrichment.mass.music.albums.update_item_in_library.assert_awaited_once()


@pytest.mark.asyncio
async def test_album_enrichment_backfills_external_ids() -> None:
    """External ids fetched from the provider item (e.g. a barcode) reach the album."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.providers = []  # type: ignore[misc]
    enrichment.mass.music.albums.update_item_in_library = AsyncMock()

    prov_item = Album(
        item_id="prov1",
        provider="spotify_1",
        name="Test Album",
        provider_mappings=set(),
        external_ids={(ExternalID.BARCODE, "0602577915181")},
        metadata=MediaItemMetadata(),
    )
    enrichment.mass.music.albums.get_provider_item = AsyncMock(return_value=prov_item)

    album = Album(
        item_id="1",
        provider="library",
        name="Test Album",
        provider_mappings={
            ProviderMapping(
                item_id="prov1", provider_domain="spotify", provider_instance="spotify_1"
            )
        },
        metadata=MediaItemMetadata(),
    )

    await enrichment._update_album_metadata(album, force_refresh=True)

    assert (ExternalID.BARCODE, "0602577915181") in album.external_ids


@pytest.mark.asyncio
async def test_track_enrichment_survives_provider_error() -> None:
    """A raising track metadata provider is logged and skipped; later providers still run."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.mass.music.tracks.update_item_in_library = AsyncMock()

    boom = _metadata_provider("boom", ProviderFeature.TRACK_METADATA)
    boom.get_track_metadata = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    good = _metadata_provider("good", ProviderFeature.TRACK_METADATA)
    good.get_track_metadata = AsyncMock(return_value=None)
    enrichment.providers = [boom, good]  # type: ignore[misc]

    track = Track(
        item_id="1",
        provider="library",
        name="Test Track",
        provider_mappings=set(),
        metadata=MediaItemMetadata(),
    )

    await enrichment._update_track_metadata(track, force_refresh=True)

    boom.get_track_metadata.assert_awaited_once()
    good.get_track_metadata.assert_awaited_once()
    enrichment.logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_artist_enrichment_survives_provider_error() -> None:
    """A raising artist metadata provider is logged and skipped; later providers still run."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.preferred_language = "en"  # type: ignore[misc]
    enrichment.mass.music.artists.update_item_in_library = AsyncMock()

    boom = _metadata_provider("boom", ProviderFeature.ARTIST_METADATA)
    boom.get_artist_metadata = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    good = _metadata_provider("good", ProviderFeature.ARTIST_METADATA)
    good.get_artist_metadata = AsyncMock(return_value=None)
    enrichment.providers = [boom, good]  # type: ignore[misc]

    artist = Artist(
        item_id="1",
        provider="library",
        name="Test Artist",
        provider_mappings=set(),
        external_ids={(ExternalID.MB_ARTIST, "11111111-1111-1111-1111-111111111111")},
        metadata=MediaItemMetadata(),
    )

    await enrichment._update_artist_metadata(artist, force_refresh=True)

    boom.get_artist_metadata.assert_awaited_once()
    good.get_artist_metadata.assert_awaited_once()
    enrichment.logger.warning.assert_called_once()


MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
MB_ARTIST = MagicMock(id=MBID)


def _library_album(item_id: str, external_ids: set[tuple[ExternalID, str]] | None = None) -> Album:
    """Return a library album with the given external ids."""
    return Album(
        item_id=item_id,
        provider="library",
        name=f"Album {item_id}",
        provider_mappings=set(),
        external_ids=external_ids or set(),
    )


def _library_track(item_id: str, external_ids: set[tuple[ExternalID, str]] | None = None) -> Track:
    """Return a library track with the given external ids."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings=set(),
        external_ids=external_ids or set(),
    )


def _enrichment_with_musicbrainz(
    albums: list[Album],
    tracks: list[Track],
    top_tracks: list[Track],
    *resolutions: MagicMock | None,
) -> tuple[MetadataEnrichmentMixin, AsyncMock]:
    """
    Return an enrichment mixin whose library and MusicBrainz provider are stubbed.

    :param albums: Library albums of the artist.
    :param tracks: Library tracks of the artist.
    :param top_tracks: Top tracks the providers list for the artist.
    :param resolutions: What MusicBrainz answers each successive resolve_artist call with.
    :return: The mixin and the resolve_artist mock.
    """
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.mass.music.artists.albums = AsyncMock(return_value=albums)
    enrichment.mass.music.artists.tracks = AsyncMock(return_value=tracks)
    enrichment.mass.music.artists.top_tracks = AsyncMock(return_value=top_tracks)
    resolve_artist = AsyncMock(side_effect=list(resolutions))
    enrichment.mass.get_provider.return_value.resolve_artist = resolve_artist
    return enrichment, resolve_artist


def _library(enrichment: MetadataEnrichmentMixin) -> MagicMock:
    """Return the stubbed artists controller of an enrichment mixin."""
    return cast("MagicMock", enrichment.mass.music.artists)


def _artist(name: str = "Radiohead") -> Artist:
    """Return a library artist without a MusicBrainz id."""
    return Artist(item_id="1", provider="library", name=name, provider_mappings=set())


@pytest.mark.asyncio
async def test_artist_mbid_by_streaming_link_reads_nothing_else() -> None:
    """An artist MusicBrainz knows by its streaming links costs no library or provider read."""
    enrichment, resolve_artist = _enrichment_with_musicbrainz(
        [_library_album("a")], [_library_track("t")], [], MB_ARTIST
    )
    artist = _artist()

    assert await enrichment._get_artist_mbid(artist) == MBID

    resolve_artist.assert_awaited_once_with(artist, [], [])
    _library(enrichment).albums.assert_not_awaited()
    _library(enrichment).tracks.assert_not_awaited()
    _library(enrichment).top_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_mbid_is_resolved_through_the_best_library_items() -> None:
    """Past the links, MusicBrainz gets the few library items that identify the artist best."""
    albums = [
        _library_album("plain-1"),
        _library_album("barcode-1", {(ExternalID.BARCODE, "0634904032463")}),
        _library_album("plain-2"),
        _library_album("rg", {(ExternalID.MB_RELEASEGROUP, MBID)}),
        _library_album("barcode-2", {(ExternalID.BARCODE, "0634904032432")}),
    ]
    tracks = [
        _library_track("plain"),
        # a track id from a cue sheet is not looked up, so it ranks with the plain tracks
        _library_track("cue", {(ExternalID.MB_TRACK, MBID)}),
        _library_track("isrc", {(ExternalID.ISRC, "GBSTK0700001")}),
        _library_track("recording", {(ExternalID.MB_RECORDING, MBID)}),
        _library_track("plain-2"),
    ]
    enrichment, resolve_artist = _enrichment_with_musicbrainz(albums, tracks, [], None, MB_ARTIST)
    artist = _artist()

    assert await enrichment._get_artist_mbid(artist) == MBID

    assert resolve_artist.await_count == 2
    called_artist, ref_albums, ref_tracks = resolve_artist.await_args_list[1].args
    assert called_artist is artist
    assert [album.item_id for album in ref_albums] == ["rg", "barcode-1", "barcode-2"]
    assert [track.item_id for track in ref_tracks] == ["recording", "isrc", "plain"]
    _library(enrichment).top_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_mbid_falls_back_to_the_providers_top_tracks() -> None:
    """Only once the library items fail are the providers asked for top tracks, untried ones."""
    tracks = [_library_track("plain")]
    top = [_library_track("plain"), *(_library_track(f"top-{i}") for i in range(4))]
    enrichment, resolve_artist = _enrichment_with_musicbrainz(
        [], tracks, top, None, None, MB_ARTIST
    )

    assert await enrichment._get_artist_mbid(_artist()) == MBID

    _library(enrichment).top_tracks.assert_awaited_once()
    assert resolve_artist.await_count == 3
    _, ref_albums, ref_tracks = resolve_artist.await_args_list[2].args
    assert ref_albums == []
    assert [track.item_id for track in ref_tracks] == ["top-0", "top-1", "top-2"]


@pytest.mark.asyncio
async def test_artist_mbid_is_none_when_musicbrainz_does_not_know_the_artist() -> None:
    """An unresolved artist yields no id and is logged; tracks already tried are not resent."""
    track = _library_track("t")
    enrichment, resolve_artist = _enrichment_with_musicbrainz(
        [_library_album("a")], [track], [track], None, None
    )

    assert await enrichment._get_artist_mbid(_artist()) is None

    assert resolve_artist.await_count == 2
    cast("MagicMock", enrichment.logger).debug.assert_called_once()


@pytest.mark.asyncio
async def test_artist_mbid_shortcuts_skip_musicbrainz() -> None:
    """A known id or the Various Artists name never reaches MusicBrainz."""
    enrichment, resolve_artist = _enrichment_with_musicbrainz([], [], [])
    known = _artist()
    known.mbid = MBID

    assert await enrichment._get_artist_mbid(known) == MBID
    assert await enrichment._get_artist_mbid(_artist("Various Artists")) == VARIOUS_ARTISTS_MBID
    resolve_artist.assert_not_awaited()
