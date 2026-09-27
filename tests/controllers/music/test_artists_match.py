"""Tests for ArtistsController provider matching (explicit, IO-capable enrichment)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from music_assistant_models.enums import ArtistType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzRelation,
    MusicBrainzUrl,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

MB_ARTIST_ID = "11111111-1111-1111-1111-111111111111"
OTHER_MB_ARTIST_ID = "22222222-2222-2222-2222-222222222222"
LIBRARY_ITEM_ID = "lib1"
SPOTIFY_ARTIST_URL = "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb"
# the base library artist is linked to one existing (already-loaded) provider
BASE_MAPPING = ProviderMapping(
    item_id="base-prov", provider_domain="tidal", provider_instance="tidal_1"
)
CANDIDATE_MAPPING = ProviderMapping(
    item_id="cand", provider_domain="spotify", provider_instance="spotify_1"
)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _artist_id(name: str) -> str:
    """Return the provider item id used for an artist with the given name."""
    return name.lower().replace(" ", "-")


def _credit(name: str, provider: str) -> ItemMapping:
    """Build the simplified artist credit as it appears on a media item."""
    return ItemMapping(
        media_type=MediaType.ARTIST,
        item_id=_artist_id(name),
        provider=provider,
        name=name,
    )


def _library_artist(
    *,
    name: str = "Main Artist",
    artist_type: ArtistType = ArtistType.SINGER,
    external_ids: set[tuple[ExternalID, str]] | None = None,
) -> Artist:
    """Build the base (library) artist under match."""
    return Artist(
        item_id=LIBRARY_ITEM_ID,
        provider="library",
        name=name,
        artist_type=artist_type,
        external_ids=external_ids or set(),
        provider_mappings={BASE_MAPPING},
    )


def _provider_artist(
    *,
    name: str = "Main Artist",
    artist_type: ArtistType = ArtistType.SINGER,
    external_ids: set[tuple[ExternalID, str]] | None = None,
) -> Artist:
    """Build the full provider artist returned for a credited candidate."""
    return Artist(
        item_id=_artist_id(name),
        provider="spotify_1",
        name=name,
        artist_type=artist_type,
        external_ids=external_ids or set(),
        provider_mappings={CANDIDATE_MAPPING},
    )


def _track(
    item_id: str,
    provider: str,
    *,
    name: str = "Track One",
    album_name: str = "Album X",
    duration: int = 200,
    artist_names: Sequence[str] = ("Main Artist",),
    mappings: Sequence[ProviderMapping] | None = None,
) -> Track:
    """Build a track for the reference-track leg, with its artists credited by name."""
    if mappings is None:
        mappings = [
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        ]
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        duration=duration,
        disc_number=1,
        track_number=1,
        artists=UniqueList([_credit(artist_name, provider) for artist_name in artist_names]),
        album=ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=f"{item_id}-album",
            provider=provider,
            name=album_name,
        ),
        provider_mappings=set(mappings),
    )


def _album(
    item_id: str,
    provider: str,
    *,
    name: str = "Album X",
    version: str = "",
    artist_names: Sequence[str] = ("Main Artist",),
    mappings: Sequence[ProviderMapping] | None = None,
) -> Album:
    """Build an album for the reference-album leg, with its artists credited by name."""
    if mappings is None:
        mappings = [
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        ]
    return Album(
        item_id=item_id,
        provider=provider,
        name=name,
        version=version,
        artists=UniqueList([_credit(artist_name, provider) for artist_name in artist_names]),
        provider_mappings=set(mappings),
    )


def _provider() -> Mock:
    """Return a mock streaming MusicProvider for matching."""
    provider = Mock()
    provider.name = "Spotify"
    provider.instance_id = "spotify_1"
    provider.domain = "spotify"
    return provider


def _streaming_provider(instance_id: str) -> Mock:
    """Return a mock streaming provider instance eligible for artist matching."""
    provider = Mock()
    provider.instance_id = instance_id
    provider.domain = instance_id.rsplit("_", 1)[0]
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.ARTIST}
    provider.is_streaming_provider = True
    return provider


def _musicbrainz(*urls: str) -> Mock:
    """Return a mock MusicBrainz provider resolving every artist to one linked to the given URLs."""
    musicbrainz = Mock()
    musicbrainz.resolve_artist = AsyncMock(
        return_value=MusicBrainzArtist(
            id=MB_ARTIST_ID,
            name="Main Artist",
            sort_name="Main Artist",
            relations=[
                MusicBrainzRelation(type="free streaming", url=MusicBrainzUrl(resource=url))
                for url in urls
            ],
        )
    )
    return musicbrainz


@dataclass
class _Harness:
    """A controller under test together with its mocked IO boundaries."""

    ctrl: ArtistsController
    track_search: AsyncMock
    album_search: AsyncMock
    get_provider_item: AsyncMock
    provider: Mock

    async def match(self, db_artist: Artist, *, strict: bool = True) -> list[ProviderMapping]:
        """Match against the single (streaming) provider the harness owns."""
        return await self.ctrl.match_provider(db_artist, self.provider, strict)


@contextmanager
def _harness(
    *,
    ref_tracks: Sequence[Track] = (),
    track_results: Sequence[Track] = (),
    ref_albums: Sequence[Album] = (),
    album_results: Sequence[Album] = (),
    provider_artists: Sequence[Artist] = (),
    musicbrainz: Mock | None = None,
    providers: Sequence[Mock] = (),
) -> Iterator[_Harness]:
    """
    Yield an ArtistsController with every IO boundary mocked.

    :param ref_tracks: Reference tracks of the library artist.
    :param track_results: Track search results returned by the matched provider.
    :param ref_albums: Reference albums of the library artist.
    :param album_results: Album search results returned by the matched provider.
    :param provider_artists: Full provider artists, resolved by their item id.
    :param musicbrainz: Optional mock MusicBrainz provider.
    :param providers: Loaded music provider instances that match_providers iterates.
    """
    full_artists = {artist.item_id: artist for artist in provider_artists}

    async def _artist_tracks(item_id: str, _provider: str) -> list[Track]:
        return list(ref_tracks) if item_id == LIBRARY_ITEM_ID else []

    async def _artist_albums(item_id: str, _provider: str) -> list[Album]:
        return list(ref_albums) if item_id == LIBRARY_ITEM_ID else []

    track_search = AsyncMock(return_value=list(track_results))
    album_search = AsyncMock(return_value=list(album_results))
    mass = Mock()
    mass.music.artists.tracks = AsyncMock(side_effect=_artist_tracks)
    mass.music.artists.albums = AsyncMock(side_effect=_artist_albums)
    mass.music.tracks.search = track_search
    mass.music.albums.search = album_search
    mass.music.providers = list(providers)
    mass.get_provider = Mock(
        side_effect=lambda domain, **_kwargs: musicbrainz if domain == "musicbrainz" else None
    )
    ctrl = ArtistsController.__new__(ArtistsController)
    ctrl.logger = logging.getLogger("test.artists.match")
    ctrl.mass = mass

    async def _full_artist(item_id: str, _provider: str, **_kwargs: object) -> Artist:
        if item_id not in full_artists:
            raise MediaNotFoundError(item_id)
        return full_artists[item_id]

    get_provider_item = AsyncMock(side_effect=_full_artist)
    with patch.multiple(ctrl, get_provider_item=get_provider_item):
        yield _Harness(ctrl, track_search, album_search, get_provider_item, _provider())


# ---------------------------------------------------------------------------
# reference-track leg
# ---------------------------------------------------------------------------


async def test_corroborating_reference_track_matches() -> None:
    """A search result the reference track corroborates yields the candidate's mappings."""
    with _harness(
        ref_tracks=[_track("lib-track", "library", mappings=(BASE_MAPPING,))],
        track_results=[_track("s1", "spotify_1")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == [CANDIDATE_MAPPING]


async def test_same_title_on_another_recording_does_not_match() -> None:
    """A result that only shares the track title is not corroboration."""
    with _harness(
        ref_tracks=[_track("lib-track", "library", mappings=(BASE_MAPPING,))],
        track_results=[_track("s1", "spotify_1", album_name="Other Album", duration=260)],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == []
    harness.get_provider_item.assert_not_awaited()


async def test_featuring_artist_difference_does_not_block_match() -> None:
    """A result crediting only the main artist still corroborates a featured-artist track."""
    with _harness(
        ref_tracks=[
            _track(
                "lib-track",
                "library",
                artist_names=("Main Artist", "Feat Artist"),
                mappings=(BASE_MAPPING,),
            )
        ],
        track_results=[_track("s1", "spotify_1")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == [CANDIDATE_MAPPING]


async def test_accent_drift_on_artist_name_matches() -> None:
    """An accented library artist matches its unaccented spelling on the provider."""
    with _harness(
        ref_tracks=[
            _track("lib-track", "library", artist_names=("Sigur Rós",), mappings=(BASE_MAPPING,))
        ],
        track_results=[_track("s1", "spotify_1", artist_names=("Sigur Ros",))],
        provider_artists=[_provider_artist(name="Sigur Ros")],
    ) as harness:
        matches = await harness.match(_library_artist(name="Sigur Rós"))

    assert matches == [CANDIDATE_MAPPING]


# ---------------------------------------------------------------------------
# full-artist confirmation
# ---------------------------------------------------------------------------


async def test_conflicting_musicbrainz_id_on_full_artist_rejects() -> None:
    """A same-named candidate whose full artist is a different MusicBrainz artist is rejected."""
    with _harness(
        ref_tracks=[_track("lib-track", "library", mappings=(BASE_MAPPING,))],
        track_results=[_track("s1", "spotify_1")],
        provider_artists=[
            _provider_artist(external_ids={(ExternalID.MB_ARTIST, OTHER_MB_ARTIST_ID)})
        ],
    ) as harness:
        matches = await harness.match(
            _library_artist(external_ids={(ExternalID.MB_ARTIST, MB_ARTIST_ID)})
        )

    assert matches == []
    # the credit itself carries no external ids, so only the full artist can reject it
    harness.get_provider_item.assert_awaited_once()


async def test_conflicting_artist_type_on_full_artist_rejects() -> None:
    """A same-named candidate whose full artist is a different artist type is rejected."""
    with _harness(
        ref_tracks=[_track("lib-track", "library", mappings=(BASE_MAPPING,))],
        track_results=[_track("s1", "spotify_1")],
        provider_artists=[_provider_artist(artist_type=ArtistType.SINGER)],
    ) as harness:
        matches = await harness.match(_library_artist(artist_type=ArtistType.AUTHOR))

    assert matches == []
    harness.get_provider_item.assert_awaited_once()


# ---------------------------------------------------------------------------
# reference-album leg
# ---------------------------------------------------------------------------


async def test_reference_album_edition_difference_confirms_artist() -> None:
    """An edition difference between the reference album and the result still confirms."""
    with _harness(
        ref_albums=[_album("lib-album", "library", mappings=(BASE_MAPPING,))],
        album_results=[_album("s1", "spotify_1", version="Deluxe Edition")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == [CANDIDATE_MAPPING]


async def test_reference_album_packaging_editions_confirm_artist() -> None:
    """Two non-overlapping packaging editions are still the same record by the same artist."""
    with _harness(
        ref_albums=[
            _album("lib-album", "library", version="2009 Remaster", mappings=(BASE_MAPPING,))
        ],
        album_results=[_album("s1", "spotify_1", version="Deluxe Edition")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == [CANDIDATE_MAPPING]


async def test_reference_album_retail_suffix_confirms_artist() -> None:
    """An Apple-style retail suffix on the provider title does not block the match."""
    with _harness(
        ref_albums=[_album("lib-album", "library", mappings=(BASE_MAPPING,))],
        album_results=[_album("s1", "spotify_1", name="Album X - EP")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == [CANDIDATE_MAPPING]


async def test_reference_album_different_title_does_not_match() -> None:
    """A search result for another album cannot confirm the artist."""
    with _harness(
        ref_albums=[_album("lib-album", "library", mappings=(BASE_MAPPING,))],
        album_results=[_album("s1", "spotify_1", name="Another Album")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == []
    harness.get_provider_item.assert_not_awaited()


async def test_album_credit_beyond_the_first_confirms_artist() -> None:
    """A collaboration album crediting the artist second still confirms it."""
    with _harness(
        ref_albums=[_album("lib-album", "library", mappings=(BASE_MAPPING,))],
        album_results=[_album("s1", "spotify_1", artist_names=("Other Artist", "Main Artist"))],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == [CANDIDATE_MAPPING]


async def test_unresolvable_credit_does_not_match() -> None:
    """A credit the provider cannot resolve to a full artist confirms nothing."""
    with _harness(
        ref_tracks=[_track("lib-track", "library", mappings=(BASE_MAPPING,))],
        track_results=[_track("s1", "spotify_1")],
        provider_artists=[],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == []


async def test_credit_owned_by_another_library_artist_does_not_match() -> None:
    """A credit that resolves to a library item belongs to another artist, so it cannot confirm."""
    other_library_artist = Artist(
        item_id="lib2",
        provider="library",
        name="Main Artist",
        provider_mappings={BASE_MAPPING, CANDIDATE_MAPPING},
    )
    with _harness(
        ref_tracks=[_track("lib-track", "library", mappings=(BASE_MAPPING,))],
        track_results=[_track("s1", "spotify_1")],
    ) as harness:
        harness.get_provider_item.side_effect = None
        harness.get_provider_item.return_value = other_library_artist
        matches = await harness.match(_library_artist())

    assert matches == []


# ---------------------------------------------------------------------------
# match_providers
# ---------------------------------------------------------------------------


def _searched_instances(match_provider: AsyncMock) -> list[str]:
    """Return the provider instances the search leg was run against, in order."""
    return [call.args[1].instance_id for call in match_provider.await_args_list]


async def test_match_providers_links_musicbrainz_providers_before_searching() -> None:
    """The providers MusicBrainz links the artist to are linked first and not searched."""
    base = _library_artist()
    musicbrainz = _musicbrainz(SPOTIFY_ARTIST_URL)
    providers = [_streaming_provider("spotify_1"), _streaming_provider("deezer_1")]
    link = AsyncMock(return_value=[CANDIDATE_MAPPING])
    match_provider = AsyncMock(return_value=[])
    with (
        _harness(musicbrainz=musicbrainz, providers=providers) as harness,
        patch.multiple(harness.ctrl, link_musicbrainz_mappings=link, match_provider=match_provider),
    ):
        await harness.ctrl.match_providers(base)

    musicbrainz.resolve_artist.assert_awaited_once_with(base, [], [])
    link.assert_awaited_once_with(base, [SPOTIFY_ARTIST_URL])
    assert _searched_instances(match_provider) == ["deezer_1"]


async def test_match_providers_searches_every_provider_with_linking_disabled() -> None:
    """With the linking toggle off MusicBrainz is not consulted at all."""
    musicbrainz = _musicbrainz(SPOTIFY_ARTIST_URL)
    providers = [_streaming_provider("spotify_1"), _streaming_provider("deezer_1")]
    link = AsyncMock(return_value=[CANDIDATE_MAPPING])
    match_provider = AsyncMock(return_value=[])
    with (
        _harness(musicbrainz=musicbrainz, providers=providers) as harness,
        patch.multiple(harness.ctrl, link_musicbrainz_mappings=link, match_provider=match_provider),
    ):
        cast("Mock", harness.ctrl.mass).metadata.link_providers_via_musicbrainz = False
        await harness.ctrl.match_providers(_library_artist())

    musicbrainz.resolve_artist.assert_not_awaited()
    link.assert_not_awaited()
    assert _searched_instances(match_provider) == ["spotify_1", "deezer_1"]


async def test_match_providers_searches_every_provider_without_musicbrainz() -> None:
    """Without a MusicBrainz provider the search leg behaves as before."""
    providers = [_streaming_provider("spotify_1"), _streaming_provider("deezer_1")]
    link = AsyncMock(return_value=[])
    match_provider = AsyncMock(return_value=[])
    with (
        _harness(providers=providers) as harness,
        patch.multiple(harness.ctrl, link_musicbrainz_mappings=link, match_provider=match_provider),
    ):
        await harness.ctrl.match_providers(_library_artist())

    link.assert_not_awaited()
    assert _searched_instances(match_provider) == ["spotify_1", "deezer_1"]


async def test_match_providers_falls_back_to_searching_when_musicbrainz_fails() -> None:
    """MusicBrainz trouble only costs the shortcut; every provider is still searched."""
    musicbrainz = Mock()
    musicbrainz.resolve_artist = AsyncMock(side_effect=aiohttp.ClientError("mirror down"))
    providers = [_streaming_provider("spotify_1"), _streaming_provider("deezer_1")]
    match_provider = AsyncMock(return_value=[])
    with (
        _harness(musicbrainz=musicbrainz, providers=providers) as harness,
        patch.multiple(harness.ctrl, match_provider=match_provider),
    ):
        await harness.ctrl.match_providers(_library_artist())

    assert _searched_instances(match_provider) == ["spotify_1", "deezer_1"]
