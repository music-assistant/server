"""Tests for the metadata enrichment steps and their resilience to provider failures."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import AlbumType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import (
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata

from music_assistant.constants import VARIOUS_ARTISTS_MBID
from music_assistant.controllers.metadata.constants import (
    CONF_ENABLE_ONLINE_METADATA,
    REFRESH_INTERVAL,
    REFRESH_RETRY_INTERVAL,
)
from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin
from music_assistant.controllers.music.helpers import fill_track_from_recording
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzRecording,
    MusicBrainzRelation,
    MusicBrainzRelease,
    MusicBrainzReleaseGroup,
    MusicBrainzUrl,
)

_ENRICHMENT_TIME = "music_assistant.controllers.metadata.enrichment.time"
NOW = 1_700_000_000
# the provider method and metadata feature each refreshed media type is enriched through
_REFRESHES = {
    MediaType.ARTIST: (ProviderFeature.ARTIST_METADATA, "get_artist_metadata"),
    MediaType.ALBUM: (ProviderFeature.ALBUM_METADATA, "get_album_metadata"),
    MediaType.TRACK: (ProviderFeature.TRACK_METADATA, "get_track_metadata"),
}


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


def _enrichment(musicbrainz: MagicMock | None = None) -> MetadataEnrichmentMixin:
    """
    Return an enrichment mixin with its library and providers stubbed.

    :param musicbrainz: The MusicBrainz provider, or None when none is loaded.
    """
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.preferred_language = "en"  # type: ignore[misc]
    enrichment.providers = []  # type: ignore[misc]
    enrichment.link_providers_via_musicbrainz = True  # type: ignore[misc]
    # every music provider resolves to a stub, MusicBrainz only when it is loaded
    enrichment.mass.get_provider = MagicMock(
        side_effect=lambda domain, **_kwargs: (
            musicbrainz if domain == "musicbrainz" else MagicMock()
        )
    )
    music = enrichment.mass.music
    for controller in (music.artists, music.albums, music.tracks):
        controller.update_item_in_library = AsyncMock()
        controller.link_musicbrainz_mappings = AsyncMock(return_value=[])
    music.albums.get_library_album_tracks = AsyncMock(return_value=[])
    music.albums.link_album_tracks = AsyncMock()
    return enrichment


def _mass(enrichment: MetadataEnrichmentMixin) -> MagicMock:
    """Return the stubbed MusicAssistant instance of an enrichment mixin."""
    return cast("MagicMock", enrichment.mass)


def _logger(enrichment: MetadataEnrichmentMixin) -> MagicMock:
    """Return the stubbed logger of an enrichment mixin."""
    return cast("MagicMock", enrichment.logger)


@pytest.mark.asyncio
async def test_album_enrichment_survives_provider_error() -> None:
    """A raising album metadata provider is logged and skipped; later providers still run."""
    enrichment = _enrichment()

    boom = _metadata_provider("boom", ProviderFeature.ALBUM_METADATA)
    boom.get_album_metadata = AsyncMock(side_effect=ValueError("bad payload"))
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

    # an unexpected provider error must not abort enrichment
    with patch(_ENRICHMENT_TIME, return_value=NOW):
        await enrichment._update_album_metadata(album, force_refresh=True)

    boom.get_album_metadata.assert_awaited_once()
    good.get_album_metadata.assert_awaited_once()  # loop continued past the failing provider
    _logger(enrichment).warning.assert_called_once()
    _mass(enrichment).music.albums.update_item_in_library.assert_awaited_once()
    assert album.metadata.last_refresh == NOW


@pytest.mark.asyncio
async def test_album_enrichment_backfills_external_ids() -> None:
    """External ids fetched from the provider item (e.g. a barcode) reach the album."""
    enrichment = _enrichment()

    prov_item = Album(
        item_id="prov1",
        provider="spotify_1",
        name="Test Album",
        provider_mappings=set(),
        external_ids={(ExternalID.BARCODE, "0602577915181")},
        metadata=MediaItemMetadata(),
    )
    _mass(enrichment).music.albums.get_provider_item = AsyncMock(return_value=prov_item)

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
    enrichment = _enrichment()

    boom = _metadata_provider("boom", ProviderFeature.TRACK_METADATA)
    boom.get_track_metadata = AsyncMock(side_effect=ValueError("bad payload"))
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

    with patch(_ENRICHMENT_TIME, return_value=NOW):
        await enrichment._update_track_metadata(track, force_refresh=True)

    boom.get_track_metadata.assert_awaited_once()
    good.get_track_metadata.assert_awaited_once()
    _logger(enrichment).warning.assert_called_once()
    _mass(enrichment).music.tracks.update_item_in_library.assert_awaited_once()
    assert track.metadata.last_refresh == NOW


@pytest.mark.asyncio
async def test_artist_enrichment_survives_provider_error() -> None:
    """A raising artist metadata provider is logged and skipped; later providers still run."""
    enrichment = _enrichment()

    boom = _metadata_provider("boom", ProviderFeature.ARTIST_METADATA)
    boom.get_artist_metadata = AsyncMock(side_effect=ValueError("bad payload"))
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

    with patch(_ENRICHMENT_TIME, return_value=NOW):
        await enrichment._update_artist_metadata(artist, force_refresh=True)

    boom.get_artist_metadata.assert_awaited_once()
    good.get_artist_metadata.assert_awaited_once()
    _logger(enrichment).warning.assert_called_once()
    _mass(enrichment).music.artists.update_item_in_library.assert_awaited_once()
    assert artist.metadata.last_refresh == NOW


async def _refresh(
    enrichment: MetadataEnrichmentMixin, media_type: MediaType
) -> Artist | Album | Track:
    """
    Refresh the metadata of a new library item of the given media type and return the item.

    :param enrichment: The enrichment mixin to refresh the item with.
    :param media_type: Artist, album or track.
    """
    if media_type == MediaType.ARTIST:
        # the metadata providers are only asked about an artist with a MusicBrainz id
        artist = Artist(
            item_id="1",
            provider="library",
            name="Test Artist",
            provider_mappings=set(),
            external_ids={(ExternalID.MB_ARTIST, "11111111-1111-1111-1111-111111111111")},
        )
        await enrichment._update_artist_metadata(artist, force_refresh=True)
        return artist
    if media_type == MediaType.ALBUM:
        album = Album(item_id="1", provider="library", name="Test Album", provider_mappings=set())
        await enrichment._update_album_metadata(album, force_refresh=True)
        return album
    track = Track(item_id="1", provider="library", name="Test Track", provider_mappings=set())
    await enrichment._update_track_metadata(track, force_refresh=True)
    return track


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", list(_REFRESHES))
@pytest.mark.parametrize(
    "error",
    [
        RetriesExhausted("retries exhausted"),
        RateLimited("rate limited", backoff_time=120),
        ResourceTemporarilyUnavailable("backend overloaded"),
        ProviderUnavailableError("provider unavailable"),
        aiohttp.ClientError("network down"),
        TimeoutError(),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_enrichment_is_due_again_soon_after_a_temporary_provider_error(
    media_type: MediaType, error: Exception
) -> None:
    """A provider failing temporarily is skipped quietly and the item is due again soon."""
    enrichment = _enrichment()
    feature, method = _REFRESHES[media_type]
    failing = _metadata_provider("failing", feature)
    setattr(failing, method, AsyncMock(side_effect=error))
    good = _metadata_provider("good", feature)
    setattr(good, method, AsyncMock(return_value=None))
    enrichment.providers = [failing, good]  # type: ignore[misc]

    with patch(_ENRICHMENT_TIME, return_value=NOW):
        item = await _refresh(enrichment, media_type)

    getattr(good, method).assert_awaited_once()  # loop continued past the failing provider
    logger = _logger(enrichment)
    logger.warning.assert_not_called()
    assert any(
        "not available from provider" in call.args[0] for call in logger.debug.call_args_list
    )
    library = getattr(_mass(enrichment).music, f"{media_type.value}s")
    library.update_item_in_library.assert_awaited_once()
    assert item.metadata.last_refresh == NOW - REFRESH_INTERVAL + REFRESH_RETRY_INTERVAL


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", list(_REFRESHES))
async def test_enrichment_without_provider_errors_is_due_after_the_refresh_interval(
    media_type: MediaType,
) -> None:
    """A refresh no provider failed is stamped with the current time."""
    enrichment = _enrichment()
    feature, method = _REFRESHES[media_type]
    good = _metadata_provider("good", feature)
    setattr(good, method, AsyncMock(return_value=None))
    enrichment.providers = [good]  # type: ignore[misc]

    with patch(_ENRICHMENT_TIME, return_value=NOW):
        item = await _refresh(enrichment, media_type)

    getattr(good, method).assert_awaited_once()
    assert item.metadata.last_refresh == NOW


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


# ---------------------------------------------------------------------------
# MusicBrainz identity steps
# ---------------------------------------------------------------------------

RELEASE_ID = "5d9c6e8a-1c3b-4f2e-9a7d-2b8c4e6f0a11"
RELEASE_GROUP_ID = "7f1e2d3c-4b5a-4c6d-8e9f-0a1b2c3d4e55"
RECORDING_ID = "0c1d2e3f-4a5b-4c6d-9e8f-7a6b5c4d3e22"
OTHER_MBID = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c33"
BARCODE = "0634904032463"
SPOTIFY_ALBUM_URL = "https://open.spotify.com/album/7eyQXxuf2nGj9d2367Gi5f"
SPOTIFY_TRACK_URL = "https://open.spotify.com/track/2Ex8hBvUhZjXjJpZjJZ0aA"
SPOTIFY_ARTIST_URL = "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb"
DISCOGS_RELEASE_URL = "https://www.discogs.com/release/1119453"
DISCOGS_ARTIST_URL = "https://www.discogs.com/artist/3840"
SPOTIFY_MAPPING = ProviderMapping(
    item_id="7eyQXxuf2nGj9d2367Gi5f", provider_domain="spotify", provider_instance="spotify_1"
)


def _relation(url: str, ended: bool = False) -> MusicBrainzRelation:
    """Return a URL relation to the given link."""
    return MusicBrainzRelation(type="free streaming", url=MusicBrainzUrl(resource=url), ended=ended)


def _release(**overrides: Any) -> MusicBrainzRelease:
    """Return a resolved MusicBrainz release, an official 2016 reissue of a 2007 album."""
    release = MusicBrainzRelease(
        id=RELEASE_ID,
        title="In Rainbows",
        status="Official",
        date="2016-05-06",
        barcode=BARCODE,
        asin="B000TDA9YM",
        release_group=MusicBrainzReleaseGroup(
            id=RELEASE_GROUP_ID,
            title="In Rainbows",
            primary_type="Album",
            first_release_date="2007-10-10",
        ),
        relations=[_relation(SPOTIFY_ALBUM_URL), _relation(DISCOGS_RELEASE_URL)],
    )
    for key, value in overrides.items():
        setattr(release, key, value)
    return release


def _musicbrainz(
    release: MusicBrainzRelease | None = None,
    recording: MusicBrainzRecording | None = None,
    artist: MusicBrainzArtist | None = None,
) -> MagicMock:
    """Return a MusicBrainz provider stub that answers the identity lookups as given."""
    musicbrainz = MagicMock()
    musicbrainz.resolve_release = AsyncMock(return_value=release)
    musicbrainz.resolve_recording = AsyncMock(return_value=recording)
    musicbrainz.resolve_artist = AsyncMock(return_value=None)
    musicbrainz.get_artist_details = AsyncMock(return_value=artist)
    return musicbrainz


def _album(external_ids: set[tuple[ExternalID, str]] | None = None, **kwargs: Any) -> Album:
    """Return a library album without provider mappings."""
    return Album(
        item_id="1",
        provider="library",
        name="In Rainbows",
        provider_mappings=set(),
        external_ids=external_ids or set(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_album_identity_fills_the_blanks_and_keeps_existing_values() -> None:
    """MusicBrainz fills in what the album lacks; ids, year and type it has are kept."""
    enrichment = _enrichment(_musicbrainz(release=_release()))
    album = _album(
        {(ExternalID.MB_ALBUM, OTHER_MBID), (ExternalID.BARCODE, "0000000000000")},
        year=2001,
        album_type=AlbumType.EP,
    )

    await enrichment._update_album_metadata(album, force_refresh=True)

    assert album.mbid == OTHER_MBID
    assert album.get_external_id(ExternalID.BARCODE) == "0000000000000"
    assert (album.year, album.album_type) == (2001, AlbumType.EP)
    assert album.get_external_id(ExternalID.MB_RELEASEGROUP) == RELEASE_GROUP_ID
    assert album.get_external_id(ExternalID.ASIN) == "B000TDA9YM"
    assert album.get_external_id(ExternalID.DISCOGS) == "1119453"
    assert album.metadata.last_musicbrainz_lookup is not None
    _mass(enrichment).music.albums.update_item_in_library.assert_awaited_once_with("1", album)


@pytest.mark.asyncio
async def test_album_identity_fills_ids_year_and_type_of_a_bare_album() -> None:
    """A bare album gets the release id, barcode, first release year and type of its group."""
    enrichment = _enrichment(_musicbrainz(release=_release()))
    album = _album()

    await enrichment._update_album_metadata(album, force_refresh=True)

    assert album.mbid == RELEASE_ID
    assert album.get_external_id(ExternalID.BARCODE) == BARCODE
    assert (album.year, album.album_type) == (2007, AlbumType.ALBUM)


@pytest.mark.asyncio
async def test_album_identity_links_the_album_and_its_tracks() -> None:
    """The release's links go to the album linker, the loaded tracks on to the track linker."""
    release = _release()
    enrichment = _enrichment(_musicbrainz(release=release))
    albums = _mass(enrichment).music.albums
    albums.link_musicbrainz_mappings = AsyncMock(return_value=[SPOTIFY_MAPPING])
    db_tracks = [_track()]
    albums.get_library_album_tracks = AsyncMock(return_value=db_tracks)
    album = _album()

    await enrichment._update_album_metadata(album, force_refresh=True)

    albums.link_musicbrainz_mappings.assert_awaited_once_with(
        album, [SPOTIFY_ALBUM_URL, DISCOGS_RELEASE_URL]
    )
    albums.link_album_tracks.assert_awaited_once_with(
        album, db_tracks, release, link_providers=True
    )


@pytest.mark.asyncio
async def test_album_identity_with_linking_disabled_fills_ids_but_links_nothing() -> None:
    """With provider linking off the album still gets its ids; no provider is linked."""
    release = _release()
    enrichment = _enrichment(_musicbrainz(release=release))
    enrichment.link_providers_via_musicbrainz = False  # type: ignore[misc]
    albums = _mass(enrichment).music.albums
    db_tracks = [_track()]
    albums.get_library_album_tracks = AsyncMock(return_value=db_tracks)
    album = _album()

    await enrichment._link_album_to_musicbrainz(album)

    assert album.mbid == RELEASE_ID
    assert album.metadata.last_musicbrainz_lookup is not None
    albums.link_musicbrainz_mappings.assert_not_awaited()
    # the release still identifies the album's tracks, only the providers are left alone
    albums.link_album_tracks.assert_awaited_once_with(
        album, db_tracks, release, link_providers=False
    )


@pytest.mark.asyncio
async def test_album_identity_tells_musicbrainz_the_library_track_count() -> None:
    """The library's own track count tells the editions of a release group apart."""
    musicbrainz = _musicbrainz()
    enrichment = _enrichment(musicbrainz)
    _mass(enrichment).music.albums.get_library_album_tracks = AsyncMock(
        return_value=[MagicMock(), MagicMock(), MagicMock()]
    )
    album = _album()

    await enrichment._update_album_metadata(album, force_refresh=True)

    musicbrainz.resolve_release.assert_awaited_once_with(album, library_track_count=3)


@pytest.mark.asyncio
async def test_album_identity_runs_before_the_metadata_providers() -> None:
    """The metadata providers already see the MusicBrainz id the identity step found."""
    enrichment = _enrichment(_musicbrainz(release=_release()))
    seen_mbids: list[str | None] = []
    provider = _metadata_provider("tadb", ProviderFeature.ALBUM_METADATA)
    provider.get_album_metadata = AsyncMock(side_effect=lambda album: seen_mbids.append(album.mbid))
    enrichment.providers = [provider]  # type: ignore[misc]

    await enrichment._update_album_metadata(_album(), force_refresh=True)

    assert seen_mbids == [RELEASE_ID]


@pytest.mark.asyncio
async def test_album_identity_marks_a_miss_without_linking() -> None:
    """An album MusicBrainz does not know is marked as looked up, and nothing is linked."""
    enrichment = _enrichment(_musicbrainz(release=None))
    album = _album()

    await enrichment._update_album_metadata(album, force_refresh=True)

    assert album.metadata.last_musicbrainz_lookup is not None
    assert album.external_ids == set()
    _mass(enrichment).music.albums.link_musicbrainz_mappings.assert_not_awaited()
    _mass(enrichment).music.albums.link_album_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_album_identity_is_skipped_without_a_musicbrainz_provider() -> None:
    """Without MusicBrainz there is no lookup and no marker, but enrichment completes."""
    enrichment = _enrichment(musicbrainz=None)
    album = _album()

    await enrichment._update_album_metadata(album, force_refresh=True)

    assert album.metadata.last_musicbrainz_lookup is None
    assert album.metadata.last_refresh is not None
    _mass(enrichment).music.albums.update_item_in_library.assert_awaited_once()


@pytest.mark.asyncio
async def test_album_identity_failure_is_logged_and_left_for_the_next_run() -> None:
    """A failing lookup is a warning, not the end of the enrichment, and is not marked done."""
    musicbrainz = _musicbrainz()
    musicbrainz.resolve_release = AsyncMock(side_effect=aiohttp.ClientError("mirror down"))
    enrichment = _enrichment(musicbrainz)
    album = _album()

    await enrichment._update_album_metadata(album, force_refresh=True)

    _logger(enrichment).warning.assert_called_once()
    assert album.metadata.last_musicbrainz_lookup is None
    _mass(enrichment).music.albums.update_item_in_library.assert_awaited_once()


def _track(external_ids: set[tuple[ExternalID, str]] | None = None) -> Track:
    """Return a library track without provider mappings."""
    return Track(
        item_id="1",
        provider="library",
        name="15 Step",
        duration=237,
        provider_mappings=set(),
        external_ids=external_ids or set(),
    )


@pytest.mark.asyncio
async def test_track_identity_fills_recording_id_and_isrcs_and_links() -> None:
    """The recording's id and every ISRC the track lacks are filled in, then it is linked."""
    recording = MusicBrainzRecording(
        id=RECORDING_ID,
        title="15 Step",
        isrcs=["GBSTK0700001", "GBSTK0700002"],
        relations=[_relation(SPOTIFY_TRACK_URL)],
    )
    enrichment = _enrichment(_musicbrainz(recording=recording))
    track = _track({(ExternalID.ISRC, "GBSTK0700001")})

    await enrichment._update_track_metadata(track, force_refresh=True)

    assert track.mbid == RECORDING_ID
    assert {value for kind, value in track.external_ids if kind == ExternalID.ISRC} == {
        "GBSTK0700001",
        "GBSTK0700002",
    }
    assert track.metadata.last_musicbrainz_lookup is not None
    _mass(enrichment).music.tracks.link_musicbrainz_mappings.assert_awaited_once_with(
        track, [SPOTIFY_TRACK_URL]
    )
    _mass(enrichment).music.tracks.update_item_in_library.assert_awaited_once_with("1", track)


@pytest.mark.asyncio
async def test_track_identity_keeps_an_existing_recording_id_and_marks_a_miss() -> None:
    """A recording id the track carries wins; a track MusicBrainz does not know is marked."""
    recording = MusicBrainzRecording(id=RECORDING_ID, title="15 Step")
    enrichment = _enrichment(_musicbrainz(recording=recording))
    known = _track({(ExternalID.MB_RECORDING, OTHER_MBID)})
    await enrichment._update_track_metadata(known, force_refresh=True)
    assert known.mbid == OTHER_MBID

    enrichment = _enrichment(_musicbrainz(recording=None))
    unknown = _track()
    await enrichment._update_track_metadata(unknown, force_refresh=True)
    assert unknown.metadata.last_musicbrainz_lookup is not None
    assert unknown.external_ids == set()
    _mass(enrichment).music.tracks.link_musicbrainz_mappings.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_identity_failure_is_left_for_the_next_run() -> None:
    """A track whose lookup fails is not marked as looked up, unlike an authoritative miss."""
    musicbrainz = _musicbrainz()
    musicbrainz.resolve_recording = AsyncMock(side_effect=aiohttp.ClientError("mirror down"))
    enrichment = _enrichment(musicbrainz)
    track = _track()

    await enrichment._update_track_metadata(track, force_refresh=True)

    _logger(enrichment).warning.assert_called_once()
    assert track.metadata.last_musicbrainz_lookup is None
    _mass(enrichment).music.tracks.update_item_in_library.assert_awaited_once_with("1", track)


def test_fill_track_from_recording_reports_whether_the_track_gained_an_id() -> None:
    """The recording id and valid ISRCs count as a change; nothing new or invalid does not."""
    recording = MusicBrainzRecording(id=RECORDING_ID, title="15 Step", isrcs=["GBSTK0700001"])

    assert fill_track_from_recording(_track(), recording) is True

    known = _track({(ExternalID.MB_RECORDING, OTHER_MBID)})
    assert fill_track_from_recording(known, recording) is True
    assert known.mbid == OTHER_MBID

    complete = _track({(ExternalID.MB_RECORDING, RECORDING_ID), (ExternalID.ISRC, "GBSTK0700001")})
    assert fill_track_from_recording(complete, recording) is False

    placeholder = MusicBrainzRecording(id=RECORDING_ID, title="15 Step", isrcs=["unknown"])
    assert fill_track_from_recording(complete, placeholder) is False
    assert (ExternalID.ISRC, "unknown") not in complete.external_ids


@pytest.mark.asyncio
async def test_artist_identity_fills_discogs_and_links() -> None:
    """A known artist gets its Discogs id from MusicBrainz and is linked through its links."""
    details = MusicBrainzArtist(
        id=OTHER_MBID,
        name="Radiohead",
        sort_name="Radiohead",
        relations=[
            _relation(SPOTIFY_ARTIST_URL),
            _relation(DISCOGS_ARTIST_URL),
            _relation("https://www.deezer.com/artist/399", ended=True),
        ],
    )
    musicbrainz = _musicbrainz(artist=details)
    enrichment = _enrichment(musicbrainz)
    artist = Artist(
        item_id="1",
        provider="library",
        name="Radiohead",
        provider_mappings=set(),
        external_ids={(ExternalID.MB_ARTIST, OTHER_MBID)},
    )

    await enrichment._update_artist_metadata(artist, force_refresh=True)

    musicbrainz.get_artist_details.assert_awaited_once_with(OTHER_MBID)
    assert artist.get_external_id(ExternalID.DISCOGS) == "3840"
    assert artist.metadata.last_musicbrainz_lookup is not None
    _mass(enrichment).music.artists.link_musicbrainz_mappings.assert_awaited_once_with(
        artist, [SPOTIFY_ARTIST_URL, DISCOGS_ARTIST_URL]
    )


@pytest.mark.asyncio
async def test_artist_identity_failure_is_left_for_the_next_run() -> None:
    """An artist whose MusicBrainz details cannot be fetched is not marked as looked up."""
    musicbrainz = _musicbrainz()
    musicbrainz.get_artist_details = AsyncMock(side_effect=aiohttp.ClientError("mirror down"))
    enrichment = _enrichment(musicbrainz)
    artist = Artist(
        item_id="1",
        provider="library",
        name="Radiohead",
        provider_mappings=set(),
        external_ids={(ExternalID.MB_ARTIST, OTHER_MBID)},
    )

    await enrichment._update_artist_metadata(artist, force_refresh=True)

    _logger(enrichment).warning.assert_called_once()
    assert artist.metadata.last_musicbrainz_lookup is None
    _mass(enrichment).music.artists.link_musicbrainz_mappings.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_identity_marks_various_artists_without_looking_it_up() -> None:
    """Various Artists is nobody's discography: marked as looked up, never linked."""
    musicbrainz = _musicbrainz()
    enrichment = _enrichment(musicbrainz)
    artist = Artist(
        item_id="1", provider="library", name="Various Artists", provider_mappings=set()
    )

    await enrichment._update_artist_metadata(artist, force_refresh=True)

    assert artist.mbid == VARIOUS_ARTISTS_MBID
    assert artist.metadata.last_musicbrainz_lookup is not None
    musicbrainz.get_artist_details.assert_not_awaited()
    _mass(enrichment).music.artists.link_musicbrainz_mappings.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_identity_marks_an_artist_musicbrainz_does_not_know() -> None:
    """An artist that stays without an id is marked as looked up and not linked."""
    musicbrainz = _musicbrainz()
    enrichment = _enrichment(musicbrainz)
    for listing in ("albums", "tracks", "top_tracks"):
        setattr(_mass(enrichment).music.artists, listing, AsyncMock(return_value=[]))
    artist = Artist(item_id="1", provider="library", name="Nobody", provider_mappings=set())

    await enrichment._update_artist_metadata(artist, force_refresh=True)

    assert artist.mbid is None
    assert artist.metadata.last_musicbrainz_lookup is not None
    musicbrainz.get_artist_details.assert_not_awaited()
    _mass(enrichment).music.artists.link_musicbrainz_mappings.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_identity_with_linking_disabled_fills_discogs_but_links_nothing() -> None:
    """With provider linking off the artist still gets its Discogs id; no provider is linked."""
    details = MusicBrainzArtist(
        id=OTHER_MBID,
        name="Radiohead",
        sort_name="Radiohead",
        relations=[_relation(SPOTIFY_ARTIST_URL), _relation(DISCOGS_ARTIST_URL)],
    )
    enrichment = _enrichment(_musicbrainz(artist=details))
    enrichment.link_providers_via_musicbrainz = False  # type: ignore[misc]
    artist = Artist(
        item_id="1",
        provider="library",
        name="Radiohead",
        provider_mappings=set(),
        external_ids={(ExternalID.MB_ARTIST, OTHER_MBID)},
    )

    await enrichment._link_artist_to_musicbrainz(artist)

    assert artist.get_external_id(ExternalID.DISCOGS) == "3840"
    assert artist.metadata.last_musicbrainz_lookup is not None
    _mass(enrichment).music.artists.link_musicbrainz_mappings.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_identity_with_linking_disabled_fills_ids_but_links_nothing() -> None:
    """With provider linking off the track still gets its recording id; no provider is linked."""
    recording = MusicBrainzRecording(
        id=RECORDING_ID,
        title="15 Step",
        isrcs=["GBSTK0700001"],
        relations=[_relation(SPOTIFY_TRACK_URL)],
    )
    enrichment = _enrichment(_musicbrainz(recording=recording))
    enrichment.link_providers_via_musicbrainz = False  # type: ignore[misc]
    track = _track()

    await enrichment._link_track_to_musicbrainz(track)

    assert track.mbid == RECORDING_ID
    assert (ExternalID.ISRC, "GBSTK0700001") in track.external_ids
    assert track.metadata.last_musicbrainz_lookup is not None
    _mass(enrichment).music.tracks.link_musicbrainz_mappings.assert_not_awaited()
