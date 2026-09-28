"""Tests for an artist's discography as MusicBrainz knows it."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID, ImageType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, Artist, ItemMapping, ProviderMapping

from music_assistant.constants import VARIOUS_ARTISTS_MBID
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.providers.musicbrainz.models import MusicBrainzArtist, MusicBrainzReleaseGroup
from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

ARTIST_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
LIBRARY_ARTIST_ID = "12"
RG_IN_RAINBOWS = "b1392450-e666-3926-a536-22c65f834433"
RG_OK_COMPUTER = "b1392450-e666-3926-a536-22c65f834401"
RG_OK_COMPUTER_LIVE = "b1392450-e666-3926-a536-22c65f834402"
RG_OK_COMPUTER_REISSUE = "b1392450-e666-3926-a536-22c65f834405"
RG_KARMA_POLICE = "b1392450-e666-3926-a536-22c65f834403"
RG_BEST_OF = "b1392450-e666-3926-a536-22c65f834404"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _artist(mbid: str | None = ARTIST_MBID) -> Artist:
    """Return the library artist whose discography is listed."""
    return Artist(
        item_id=LIBRARY_ARTIST_ID,
        provider="library",
        name="Radiohead",
        external_ids={(ExternalID.MB_ARTIST, mbid)} if mbid else set(),
        provider_mappings={
            ProviderMapping(
                item_id="4Z8W4fKeB5YxbusRsdQVPb",
                provider_domain="spotify",
                provider_instance="spotify_1",
            )
        },
    )


def _library_album(
    item_id: str,
    name: str,
    release_group_id: str | None = None,
    *,
    year: int | None = None,
    album_type: AlbumType = AlbumType.UNKNOWN,
) -> Album:
    """Return a library album, optionally carrying a MusicBrainz release group id."""
    return Album(
        item_id=item_id,
        provider="library",
        name=name,
        year=year,
        album_type=album_type,
        external_ids=(
            {(ExternalID.MB_RELEASEGROUP, release_group_id)} if release_group_id else set()
        ),
        provider_mappings={
            ProviderMapping(
                item_id=f"sp-{item_id}", provider_domain="spotify", provider_instance="spotify_1"
            )
        },
    )


def _release_group(
    group_id: str,
    title: str,
    *,
    primary_type: str = "Album",
    secondary_types: list[str] | None = None,
    first_release_date: str | None = "2007-10-10",
) -> MusicBrainzReleaseGroup:
    """Return a release group as the artist browse lists it."""
    return MusicBrainzReleaseGroup(
        id=group_id,
        title=title,
        primary_type=primary_type,
        secondary_types=secondary_types,
        first_release_date=first_release_date,
    )


@dataclass
class _Harness:
    """An artists controller under test together with its mocked IO boundaries."""

    ctrl: ArtistsController
    mass: Mock
    musicbrainz: Mock
    get_library_item: AsyncMock
    update_item_in_library: AsyncMock

    async def discography(self, provider: str = "library") -> list[Album]:
        """Return the discography of the artist under test."""
        return await self.ctrl.discography(LIBRARY_ARTIST_ID, provider)

    def assert_nothing_written(self) -> None:
        """Assert the listing stored nothing in the library."""
        self.mass.metadata.link_item_to_musicbrainz.assert_not_awaited()
        self.update_item_in_library.assert_not_awaited()
        self.mass.music.albums.update_item_in_library.assert_not_called()
        self.mass.music.albums.add_provider_mappings.assert_not_called()


@contextmanager
def _harness(
    artist: Artist,
    *,
    release_groups: Sequence[MusicBrainzReleaseGroup] = (),
    artist_albums: Sequence[Album] = (),
    resolved_mbid: str | None = None,
    musicbrainz_loaded: bool = True,
    coverartarchive_loaded: bool = True,
) -> Iterator[_Harness]:
    """
    Yield an ArtistsController with every IO boundary mocked.

    :param artist: The library artist under test.
    :param release_groups: What MusicBrainz lists as the artist's discography.
    :param artist_albums: The library albums credited to the artist.
    :param resolved_mbid: The MusicBrainz id the identity lookup finds for an artist without one.
    :param musicbrainz_loaded: Whether the MusicBrainz provider is loaded.
    :param coverartarchive_loaded: Whether the Cover Art Archive provider is loaded.
    """
    musicbrainz = Mock()
    musicbrainz.browse_release_groups_by_artist = AsyncMock(return_value=list(release_groups))
    musicbrainz.album_type_from_release_group = MusicbrainzProvider.album_type_from_release_group
    musicbrainz.resolve_artist = AsyncMock(
        return_value=(
            MusicBrainzArtist(id=resolved_mbid, name=artist.name, sort_name=artist.name)
            if resolved_mbid
            else None
        )
    )

    # loaded, yet nothing on it may be called: a listing costs the archive no lookups
    coverartarchive = Mock(spec=[])
    providers = {
        "musicbrainz": musicbrainz if musicbrainz_loaded else None,
        "coverartarchive": coverartarchive if coverartarchive_loaded else None,
    }
    mass = Mock()
    mass.get_provider = Mock(side_effect=lambda domain, **_kwargs: providers.get(domain))
    mass.metadata.link_item_to_musicbrainz = AsyncMock()
    ctrl = ArtistsController.__new__(ArtistsController)
    ctrl.logger = logging.getLogger("test.artists.discography")
    ctrl.mass = mass
    get_library_item = AsyncMock(return_value=artist)
    update_item_in_library = AsyncMock()
    with patch.multiple(
        ctrl,
        get_library_item=get_library_item,
        albums=AsyncMock(return_value=list(artist_albums)),
        update_item_in_library=update_item_in_library,
    ):
        yield _Harness(ctrl, mass, musicbrainz, get_library_item, update_item_in_library)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


async def test_discography_lists_release_groups_the_library_lacks_as_musicbrainz_albums() -> None:
    """An album not in the library carries what MusicBrainz knows, ready to resolve on demand."""
    groups = [
        _release_group(RG_IN_RAINBOWS, "In Rainbows"),
        _release_group(
            RG_KARMA_POLICE, "Karma Police", primary_type="Single", first_release_date="1997-08"
        ),
        _release_group(
            RG_BEST_OF, "The Best Of", secondary_types=["Compilation"], first_release_date=None
        ),
    ]
    with _harness(_artist(), release_groups=groups) as harness:
        discography = await harness.discography()

    assert [album.name for album in discography] == ["In Rainbows", "Karma Police", "The Best Of"]
    assert [album.year for album in discography] == [2007, 1997, None]
    assert [album.album_type for album in discography] == [
        AlbumType.ALBUM,
        AlbumType.SINGLE,
        AlbumType.COMPILATION,
    ]
    in_rainbows = discography[0]
    assert in_rainbows.provider == "musicbrainz"
    assert in_rainbows.item_id == RG_IN_RAINBOWS
    assert in_rainbows.uri == f"musicbrainz://album/{RG_IN_RAINBOWS}"
    assert in_rainbows.provider_mappings == set()
    assert in_rainbows.external_ids == {(ExternalID.MB_RELEASEGROUP, RG_IN_RAINBOWS)}
    assert [type(credit) for credit in in_rainbows.artists] == [ItemMapping]
    assert [credit.uri for credit in in_rainbows.artists] == [
        f"library://artist/{LIBRARY_ARTIST_ID}"
    ]
    harness.musicbrainz.browse_release_groups_by_artist.assert_awaited_once_with(ARTIST_MBID)
    harness.musicbrainz.resolve_artist.assert_not_awaited()
    harness.assert_nothing_written()


async def test_discography_lists_a_musicbrainz_album_as_not_playable() -> None:
    """A release no music service has yet cannot be played; a library album keeps its own flag."""
    library_album = _library_album("7", "OK Computer", RG_OK_COMPUTER)
    with _harness(
        _artist(),
        release_groups=[
            _release_group(RG_OK_COMPUTER, "OK Computer"),
            _release_group(RG_IN_RAINBOWS, "In Rainbows"),
        ],
        artist_albums=[library_album],
    ) as harness:
        ok_computer, in_rainbows = await harness.discography()

    assert ok_computer is library_album
    assert ok_computer.is_playable is True
    assert in_rainbows.provider == "musicbrainz"
    assert in_rainbows.is_playable is False


async def test_discography_hands_each_musicbrainz_album_a_cover_the_image_proxy_resolves() -> None:
    """A MusicBrainz album's cover names its release group, for the archive to resolve when shown."""
    groups = [
        _release_group(RG_IN_RAINBOWS, "In Rainbows"),
        _release_group(RG_KARMA_POLICE, "Karma Police", primary_type="Single"),
    ]
    with _harness(_artist(), release_groups=groups) as harness:
        discography = await harness.discography()

    for album, group in zip(discography, groups, strict=True):
        assert album.image is not None
        assert album.image.type == ImageType.THUMB
        assert album.image.path == group.id
        assert album.image.provider == "coverartarchive"
        assert album.image.remotely_accessible is False


async def test_discography_has_no_cover_art_without_the_cover_art_archive() -> None:
    """Without the Cover Art Archive provider nothing could resolve a cover, the albums stay bare."""
    with _harness(
        _artist(),
        release_groups=[_release_group(RG_IN_RAINBOWS, "In Rainbows")],
        coverartarchive_loaded=False,
    ) as harness:
        (in_rainbows,) = await harness.discography()

    assert in_rainbows.image is None


async def test_discography_returns_the_library_album_carrying_the_release_group_id() -> None:
    """A library album identified as the release group is listed as that library album."""
    library_album = _library_album("7", "In Rainbows (Disk 2 edition)", RG_IN_RAINBOWS)
    with _harness(
        _artist(),
        release_groups=[_release_group(RG_IN_RAINBOWS, "In Rainbows")],
        artist_albums=[library_album],
    ) as harness:
        discography = await harness.discography()

    assert discography == [library_album]
    assert discography[0] is library_album
    # the artist's albums, loaded once, identify the groups; nothing is queried per group
    harness.mass.music.albums.get_library_items_by_external_ids.assert_not_called()
    harness.assert_nothing_written()


async def test_discography_returns_the_artists_library_album_of_the_same_name() -> None:
    """Without a release group id, a library album of the artist matches by name."""
    library_album = _library_album("7", "OK Computer")
    with _harness(
        _artist(),
        release_groups=[
            _release_group(RG_OK_COMPUTER, "OK Computer"),
            _release_group(RG_IN_RAINBOWS, "In Rainbows"),
        ],
        artist_albums=[library_album],
    ) as harness:
        discography = await harness.discography()

    assert discography[0] is library_album
    assert discography[1].provider == "musicbrainz"
    assert discography[1].image is not None
    assert discography[1].image.path == RG_IN_RAINBOWS


async def test_discography_keeps_every_same_titled_release_group() -> None:
    """An identified library album is only the group it carries; same-titled groups stay listed."""
    library_album = _library_album("7", "OK Computer", RG_OK_COMPUTER)
    groups = [
        _release_group(RG_OK_COMPUTER, "OK Computer", first_release_date="1997-05-21"),
        _release_group(
            RG_OK_COMPUTER_LIVE, "OK Computer", secondary_types=["Live"], first_release_date="1998"
        ),
        _release_group(RG_OK_COMPUTER_REISSUE, "OK Computer", first_release_date="2017-06-23"),
    ]
    with _harness(_artist(), release_groups=groups, artist_albums=[library_album]) as harness:
        discography = await harness.discography()

    assert discography[0] is library_album
    assert [album.uri for album in discography] == [
        "library://album/7",
        f"musicbrainz://album/{RG_OK_COMPUTER_LIVE}",
        f"musicbrainz://album/{RG_OK_COMPUTER_REISSUE}",
    ]
    assert [album.album_type for album in discography[1:]] == [AlbumType.LIVE, AlbumType.ALBUM]


async def test_discography_name_matches_same_titled_release_groups_by_year() -> None:
    """Of same-titled groups of one kind, a library album is the one released in its year."""
    library_album = _library_album("7", "OK Computer", year=1997)
    groups = [
        _release_group(RG_OK_COMPUTER_REISSUE, "OK Computer", first_release_date="2017-06-23"),
        _release_group(RG_OK_COMPUTER, "OK Computer", first_release_date="1997-05-21"),
    ]
    with _harness(_artist(), release_groups=groups, artist_albums=[library_album]) as harness:
        discography = await harness.discography()

    assert discography[0].uri == f"musicbrainz://album/{RG_OK_COMPUTER_REISSUE}"
    assert discography[1] is library_album


async def test_discography_leaves_an_ambiguous_name_match_unresolved() -> None:
    """A library album without a year matching same-titled groups of one kind is none of them."""
    library_album = _library_album("7", "OK Computer")
    groups = [
        _release_group(RG_OK_COMPUTER_REISSUE, "OK Computer", first_release_date="2017-06-23"),
        _release_group(RG_OK_COMPUTER, "OK Computer", first_release_date="1997-05-21"),
    ]
    with _harness(_artist(), release_groups=groups, artist_albums=[library_album]) as harness:
        discography = await harness.discography()

    assert [album.uri for album in discography] == [
        f"musicbrainz://album/{RG_OK_COMPUTER_REISSUE}",
        f"musicbrainz://album/{RG_OK_COMPUTER}",
    ]


async def test_discography_name_matches_the_only_group_of_a_name_whatever_its_type() -> None:
    """An EP typed "album" by its music service still is the one EP group of its name."""
    library_album = _library_album(
        "7", "Airbag / How Am I Driving? - EP", album_type=AlbumType.ALBUM
    )
    groups = [
        _release_group(RG_OK_COMPUTER, "OK Computer"),
        _release_group(RG_KARMA_POLICE, "Airbag / How Am I Driving?", primary_type="EP"),
    ]
    with _harness(_artist(), release_groups=groups, artist_albums=[library_album]) as harness:
        discography = await harness.discography()

    assert discography[1] is library_album


@pytest.mark.parametrize(
    ("album_type", "expected_group"),
    [(AlbumType.ALBUM, RG_OK_COMPUTER), (AlbumType.SINGLE, RG_KARMA_POLICE)],
)
async def test_discography_name_matches_an_album_to_a_group_of_its_kind(
    album_type: AlbumType, expected_group: str
) -> None:
    """A same-titled single never takes the library album, nor the album the single."""
    library_album = _library_album("7", "OK Computer", album_type=album_type)
    groups = [
        _release_group(RG_KARMA_POLICE, "OK Computer", primary_type="Single"),
        _release_group(RG_OK_COMPUTER, "OK Computer"),
    ]
    with _harness(_artist(), release_groups=groups, artist_albums=[library_album]) as harness:
        discography = await harness.discography()

    matched = [album for album in discography if album is library_album]
    assert len(matched) == 1
    assert discography.index(library_album) == [group.id for group in groups].index(expected_group)


async def test_discography_identifies_an_artist_without_a_musicbrainz_id_in_memory() -> None:
    """An artist not yet identified on MusicBrainz is identified for the listing, storing nothing."""
    artist = _artist(mbid=None)
    library_album = _library_album("7", "OK Computer")
    with _harness(
        artist,
        release_groups=[_release_group(RG_IN_RAINBOWS, "In Rainbows")],
        artist_albums=[library_album],
        resolved_mbid=ARTIST_MBID,
    ) as harness:
        discography = await harness.discography()

    harness.musicbrainz.resolve_artist.assert_awaited_once_with(artist, [library_album], [])
    harness.musicbrainz.browse_release_groups_by_artist.assert_awaited_once_with(ARTIST_MBID)
    assert artist.mbid is None
    assert harness.get_library_item.await_count == 1
    assert len(discography) == 1
    harness.assert_nothing_written()


async def test_discography_is_empty_for_an_artist_musicbrainz_does_not_know() -> None:
    """An artist that cannot be identified on MusicBrainz has no discography to show."""
    with _harness(_artist(mbid=None)) as harness:
        assert await harness.discography() == []

    harness.musicbrainz.resolve_artist.assert_awaited_once()
    harness.musicbrainz.browse_release_groups_by_artist.assert_not_awaited()
    harness.assert_nothing_written()


async def test_discography_is_empty_for_various_artists() -> None:
    """The Various Artists entity is nobody's discography."""
    with _harness(_artist(mbid=VARIOUS_ARTISTS_MBID)) as harness:
        assert await harness.discography() == []

    harness.musicbrainz.browse_release_groups_by_artist.assert_not_awaited()


async def test_discography_is_empty_without_the_musicbrainz_provider() -> None:
    """Without MusicBrainz there is nothing to browse, and nothing is looked up either."""
    with _harness(_artist(mbid=None), musicbrainz_loaded=False) as harness:
        assert await harness.discography() == []

    harness.musicbrainz.resolve_artist.assert_not_awaited()
    harness.assert_nothing_written()


async def test_discography_needs_a_library_artist() -> None:
    """A discography is only kept for artists in the library."""
    with _harness(_artist()) as harness, pytest.raises(InvalidDataError):
        await harness.discography("spotify_1")

    harness.musicbrainz.browse_release_groups_by_artist.assert_not_awaited()
