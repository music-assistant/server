"""Tests for ArtistsController provider matching (explicit, IO-capable enrichment)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import ArtistType, ExternalID, MediaType
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

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

MB_ARTIST_ID = "11111111-1111-1111-1111-111111111111"
OTHER_MB_ARTIST_ID = "22222222-2222-2222-2222-222222222222"
LIBRARY_ITEM_ID = "lib1"
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
) -> Iterator[_Harness]:
    """
    Yield an ArtistsController with every IO boundary mocked.

    :param ref_tracks: Reference tracks of the library artist.
    :param track_results: Track search results returned by the matched provider.
    :param ref_albums: Reference albums of the library artist.
    :param album_results: Album search results returned by the matched provider.
    :param provider_artists: Full provider artists, resolved by their item id.
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


async def test_reference_album_recording_conflict_does_not_match() -> None:
    """A live edition of the reference album is a different record, so it cannot confirm."""
    with _harness(
        ref_albums=[_album("lib-album", "library", mappings=(BASE_MAPPING,))],
        album_results=[_album("s1", "spotify_1", version="Live")],
        provider_artists=[_provider_artist()],
    ) as harness:
        matches = await harness.match(_library_artist())

    assert matches == []
    harness.get_provider_item.assert_not_awaited()


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
