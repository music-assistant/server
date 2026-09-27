"""Tests for resolving a MusicBrainz release group to an album on the music providers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import Album

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.media.albums import (
    _MAX_EDITION_LOOKUPS,
    AlbumsController,
)
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzArtist,
    MusicBrainzArtistCredit,
    MusicBrainzBarcodeRelease,
    MusicBrainzMedia,
    MusicBrainzRelation,
    MusicBrainzRelease,
    MusicBrainzReleaseGroup,
    MusicBrainzUrl,
)

from .helpers import create_album

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from music_assistant.mass import MusicAssistant

RELEASE_GROUP_ID = "b1392450-e666-3926-a536-22c65f834433"
RELEASE_TITLE = "In Rainbows"
RELEASE_ARTIST = MusicBrainzArtist(
    id="a74b1b7f-71a5-4011-9441-d0b5e4122711", name="Radiohead", sort_name="Radiohead"
)
BARCODE = "634904032463"
SPOTIFY_ALBUM_ID = "7eyQXxuf2nGj9d2367Gi5f"
SPOTIFY_ALBUM_URL = f"https://open.spotify.com/album/{SPOTIFY_ALBUM_ID}"
TIDAL_ALBUM_ID = "79280548"
TIDAL_ALBUM_URL = f"https://tidal.com/album/{TIDAL_ALBUM_ID}"
QOBUZ_ALBUM_ID = "0634904032432"
QOBUZ_ALBUM_URL = f"https://open.qobuz.com/album/{QOBUZ_ALBUM_ID}"
DISCOGS_RELEASE_URL = "https://www.discogs.com/release/1157205"
# how an album is fetched from a provider: on that very instance, with no stand-in
STRICT_FETCH = {"allow_fallback": False, "strict_provider_instance": True}


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _relations(urls: Sequence[str]) -> list[MusicBrainzRelation]:
    """Return streaming URL relations for the given links."""
    return [
        MusicBrainzRelation(type="free streaming", url=MusicBrainzUrl(resource=url)) for url in urls
    ]


def _edition(
    release_id: str,
    *,
    status: str = "Official",
    media_format: str = "Digital Media",
    country: str | None = "XW",
    date: str | None = "2007-12-31",
    urls: Sequence[str] = (),
) -> MusicBrainzBarcodeRelease:
    """Return one release as the release group browse lists it."""
    return MusicBrainzBarcodeRelease(
        id=release_id,
        release_group=MusicBrainzReleaseGroup(id=RELEASE_GROUP_ID, title="In Rainbows"),
        status=status,
        country=country,
        date=date,
        media=[MusicBrainzMedia(format=media_format, track_count=10)],
        relations=_relations(urls),
    )


def _release(
    release_id: str, *, barcode: str | None = BARCODE, urls: Sequence[str] = ()
) -> MusicBrainzRelease:
    """Return the full lookup of a release, credited to the release artist."""
    return MusicBrainzRelease(
        id=release_id,
        title=RELEASE_TITLE,
        barcode=barcode,
        relations=_relations(urls),
        artist_credit=[MusicBrainzArtistCredit(name=RELEASE_ARTIST.name, artist=RELEASE_ARTIST)],
    )


def _instance(instance_id: str) -> Mock:
    """Return a loaded, available provider instance stub."""
    instance = Mock()
    instance.instance_id = instance_id
    instance.available = True
    return instance


def _music_provider(
    instance_id: str, *, album: Album | None = None, error: Exception | None = None
) -> Mock:
    """Return a music provider that answers a barcode lookup with the album (or the error)."""
    provider = Mock()
    provider.instance_id = instance_id
    provider.domain = instance_id.rsplit("_", 1)[0]
    provider.name = provider.domain.title()
    provider.supported_features = {ProviderFeature.ALBUM_BY_EXTERNAL_ID}
    provider.supports_feature = lambda feature: feature in provider.supported_features
    provider.get_album_by_external_id = AsyncMock(return_value=album, side_effect=error)
    return provider


def _album(
    instance_id: str,
    item_id: str,
    *,
    name: str = RELEASE_TITLE,
    artist_name: str = RELEASE_ARTIST.name,
) -> Album:
    """Return an album as a provider lists it; the release under test unless told otherwise."""
    return create_album(instance_id, item_id, name=name, artist_name=artist_name)


@dataclass
class _Harness:
    """An albums controller under test together with its mocked IO boundaries."""

    ctrl: AlbumsController
    musicbrainz: Mock
    get: AsyncMock
    get_provider_item: AsyncMock

    async def resolve(self) -> Album:
        """Resolve the release group under test."""
        return await self.ctrl.resolve_musicbrainz_release_group(RELEASE_GROUP_ID)


@contextmanager
def _harness(
    *,
    editions: Sequence[MusicBrainzBarcodeRelease] = (),
    releases: Sequence[MusicBrainzRelease] = (),
    loaded: dict[str, list[str]] | None = None,
    providers: Sequence[Mock] | None = None,
    albums: dict[tuple[str, str], Album] | None = None,
    library: dict[tuple[str, str], Album] | None = None,
) -> Iterator[_Harness]:
    """
    Yield an AlbumsController with every IO boundary mocked.

    :param editions: The releases MusicBrainz lists for the release group.
    :param releases: The full release lookups MusicBrainz answers, by release id.
    :param loaded: Provider instance ids per loaded provider domain, for the links.
    :param providers: The music providers the user may see, asked by barcode; every loaded
        instance unless given.
    :param albums: The album each (provider instance, item id) is on its provider; others are
        missing.
    :param library: The library album each (provider instance, item id) is in the library as.
    """
    releases_by_id = {release.id: release for release in releases}
    musicbrainz = Mock()
    musicbrainz.browse_releases_by_release_group = AsyncMock(return_value=list(editions))
    musicbrainz.get_release_details = AsyncMock(
        side_effect=lambda release_id: releases_by_id[release_id]
    )
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.logger = logging.getLogger("test.albums.release_group")
    ctrl.mass = Mock()
    ctrl.mass.get_provider = Mock(
        side_effect=lambda domain, **_kwargs: musicbrainz if domain == "musicbrainz" else None
    )
    ctrl.mass.music.get_provider_instances = Mock(
        side_effect=lambda domain, **_kwargs: [
            _instance(instance_id) for instance_id in (loaded or {}).get(domain, [])
        ]
    )
    if providers is None:
        providers = [
            _music_provider(instance_id)
            for instance_ids in (loaded or {}).values()
            for instance_id in instance_ids
        ]
    ctrl.mass.music.providers = list(providers)

    library_albums = library or {}

    async def _get_library_item(item_id: str, instance: str) -> Album | None:
        return library_albums.get((instance, item_id))

    async def _get(item_id: str, instance: str, **_kwargs: object) -> Album:
        # a resolved album is only ever fetched again as the library album
        assert instance == "library"
        return next(album for album in library_albums.values() if album.item_id == item_id)

    async def _get_provider_item(item_id: str, instance: str, **_kwargs: object) -> Album:
        if album := (albums or {}).get((instance, item_id)):
            return album
        raise MediaNotFoundError(f"{item_id} is not on {instance}")

    get = AsyncMock(side_effect=_get)
    get_provider_item = AsyncMock(side_effect=_get_provider_item)
    with patch.multiple(
        ctrl,
        get=get,
        get_provider_item=get_provider_item,
        get_library_item_by_prov_id=AsyncMock(side_effect=_get_library_item),
    ):
        yield _Harness(ctrl, musicbrainz, get, get_provider_item)


def _music_controller(album: Album | None = None) -> tuple[MusicController, AsyncMock]:
    """Return a music controller, with the release group resolver that answers with the album."""
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    controller.mass.get_provider.return_value = None
    resolve = AsyncMock(return_value=album)
    controller.albums = MagicMock(resolve_musicbrainz_release_group=resolve)
    return controller, resolve


# ---------------------------------------------------------------------------
# resolve_musicbrainz_release_group
# ---------------------------------------------------------------------------


async def test_resolve_takes_the_links_of_the_official_digital_edition() -> None:
    """Of a group's editions, the official worldwide digital one with links names the album."""
    editions = [
        _edition("rel-cd", media_format="CD", urls=[SPOTIFY_ALBUM_URL]),
        _edition("rel-bootleg", status="Bootleg", urls=[SPOTIFY_ALBUM_URL]),
        _edition("rel-promo", status="Promotion", urls=[SPOTIFY_ALBUM_URL]),
        _edition("rel-digital-unlinked", date="2007-10-10"),
        _edition("rel-digital", urls=[SPOTIFY_ALBUM_URL]),
        _edition("rel-digital-jp", country="JP", urls=[SPOTIFY_ALBUM_URL]),
    ]
    spotify_album = _album("spotify_1", SPOTIFY_ALBUM_ID)
    with _harness(
        editions=editions,
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL, TIDAL_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"]},
        albums={("spotify_1", SPOTIFY_ALBUM_ID): spotify_album},
    ) as harness:
        album = await harness.resolve()

    assert album is spotify_album
    harness.musicbrainz.browse_releases_by_release_group.assert_awaited_once_with(
        RELEASE_GROUP_ID, complete=False
    )
    harness.musicbrainz.get_release_details.assert_awaited_once_with("rel-digital")
    harness.get_provider_item.assert_awaited_once_with(
        SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH
    )


async def test_resolve_returns_the_library_album_of_a_linked_one() -> None:
    """A linked album already in the library is returned as the library album."""
    library_album = replace(_album("spotify_1", SPOTIFY_ALBUM_ID), item_id="42", provider="library")
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"]},
        library={("spotify_1", SPOTIFY_ALBUM_ID): library_album},
    ) as harness:
        album = await harness.resolve()

    assert album is library_album
    harness.get.assert_awaited_once_with("42", "library", allow_update_metadata=True)
    harness.get_provider_item.assert_not_awaited()


async def test_resolve_moves_on_to_the_next_linked_provider() -> None:
    """A link the provider no longer serves is skipped for the next linked provider."""
    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID)
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL, TIDAL_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"]},
        albums={("tidal_1", TIDAL_ALBUM_ID): tidal_album},
    ) as harness:
        album = await harness.resolve()

    assert album is tidal_album
    assert harness.get_provider_item.await_args_list == [
        call(SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH),
        call(TIDAL_ALBUM_ID, "tidal_1", **STRICT_FETCH),
    ]


async def test_resolve_moves_on_to_the_next_edition() -> None:
    """An edition no provider has, by link or by barcode, gives way to the next likeliest one."""
    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID)
    spotify = _music_provider("spotify_1")
    tidal = _music_provider("tidal_1")
    with _harness(
        editions=[
            _edition("rel-first", urls=[SPOTIFY_ALBUM_URL]),
            _edition("rel-second", date="2008-01-01", urls=[TIDAL_ALBUM_URL]),
        ],
        releases=[
            _release("rel-first", urls=[SPOTIFY_ALBUM_URL]),
            _release("rel-second", urls=[TIDAL_ALBUM_URL]),
        ],
        loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"]},
        providers=[spotify, tidal],
        albums={("tidal_1", TIDAL_ALBUM_ID): tidal_album},
    ) as harness:
        album = await harness.resolve()

    assert album is tidal_album
    assert harness.musicbrainz.get_release_details.await_args_list == [
        call("rel-first"),
        call("rel-second"),
    ]
    # the first edition's barcode is spent before the next edition is looked up at all
    tidal.get_album_by_external_id.assert_awaited_once_with(BARCODE, ExternalID.BARCODE)
    assert harness.get_provider_item.await_args_list == [
        call(SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH),
        call(TIDAL_ALBUM_ID, "tidal_1", **STRICT_FETCH),
    ]


async def test_resolve_skips_an_edition_musicbrainz_cannot_look_up() -> None:
    """A stale edition costs its turn only; the next likeliest edition is still tried."""
    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID)
    with _harness(
        editions=[
            _edition("rel-stale", urls=[SPOTIFY_ALBUM_URL]),
            _edition("rel-second", date="2008-01-01", urls=[TIDAL_ALBUM_URL]),
        ],
        releases=[_release("rel-second", urls=[TIDAL_ALBUM_URL])],
        loaded={"tidal": ["tidal_1"]},
        providers=[_music_provider("tidal_1")],
        albums={("tidal_1", TIDAL_ALBUM_ID): tidal_album},
    ) as harness:
        lookup = harness.musicbrainz.get_release_details.side_effect

        async def _details(release_id: str) -> MusicBrainzRelease:
            if release_id == "rel-stale":
                raise InvalidDataError("Invalid MusicBrainz Album ID provided")
            return cast("MusicBrainzRelease", lookup(release_id))

        harness.musicbrainz.get_release_details.side_effect = _details
        album = await harness.resolve()

    assert album is tidal_album
    assert harness.musicbrainz.get_release_details.await_args_list == [
        call("rel-stale"),
        call("rel-second"),
    ]


async def test_resolve_looks_up_the_likeliest_few_editions_only() -> None:
    """A group with more editions than the bound has the ones past it left unfetched."""
    editions = [
        _edition(f"rel-{index}", date=f"2007-12-{index + 1:02d}", urls=[SPOTIFY_ALBUM_URL])
        for index in range(_MAX_EDITION_LOOKUPS + 1)
    ]
    with (
        _harness(
            editions=editions,
            releases=[_release(edition.id, urls=[SPOTIFY_ALBUM_URL]) for edition in editions],
            loaded={"spotify": ["spotify_1"]},
        ) as harness,
        pytest.raises(MediaNotFoundError),
    ):
        await harness.resolve()

    assert harness.musicbrainz.get_release_details.await_args_list == [
        call(edition.id) for edition in editions[:_MAX_EDITION_LOOKUPS]
    ]


async def test_resolve_reads_a_reissued_group_past_its_first_page_of_editions() -> None:
    """A group with more editions than one page holds is browsed for its likeliest, not given up."""
    editions = [
        _edition(f"rel-cd-{index:03d}", media_format="CD", date=f"{1990 + index % 30}")
        for index in range(149)
    ]
    editions.append(_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL]))
    spotify_album = _album("spotify_1", SPOTIFY_ALBUM_ID)
    with _harness(
        editions=editions,
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"]},
        albums={("spotify_1", SPOTIFY_ALBUM_ID): spotify_album},
    ) as harness:
        album = await harness.resolve()

    assert album is spotify_album
    harness.musicbrainz.browse_releases_by_release_group.assert_awaited_once_with(
        RELEASE_GROUP_ID, complete=False
    )
    harness.musicbrainz.get_release_details.assert_awaited_once_with("rel-digital")


async def test_resolve_ranks_an_edition_by_any_link_a_music_source_can_take() -> None:
    """An edition linked to a music service, Qobuz say, outranks one linked to a catalog site."""
    qobuz_album = _album("qobuz_1", QOBUZ_ALBUM_ID)
    with _harness(
        editions=[
            _edition("rel-discogs", urls=[DISCOGS_RELEASE_URL]),
            _edition("rel-qobuz", date="2008-01-01", urls=[QOBUZ_ALBUM_URL]),
        ],
        releases=[
            _release("rel-discogs", urls=[DISCOGS_RELEASE_URL]),
            _release("rel-qobuz", urls=[QOBUZ_ALBUM_URL]),
        ],
        loaded={"qobuz": ["qobuz_1"]},
        albums={("qobuz_1", QOBUZ_ALBUM_ID): qobuz_album},
    ) as harness:
        album = await harness.resolve()

    assert album is qobuz_album
    harness.musicbrainz.get_release_details.assert_awaited_once_with("rel-qobuz")


async def test_resolve_ranks_an_edition_by_its_music_service_links_only() -> None:
    """A link to a site that is no music service does not make an edition the likelier one."""
    spotify_album = _album("spotify_1", SPOTIFY_ALBUM_ID)
    with _harness(
        editions=[
            _edition("rel-discogs", urls=[DISCOGS_RELEASE_URL]),
            _edition("rel-spotify", date="2008-01-01", urls=[SPOTIFY_ALBUM_URL]),
        ],
        releases=[
            _release("rel-discogs", urls=[DISCOGS_RELEASE_URL]),
            _release("rel-spotify", urls=[SPOTIFY_ALBUM_URL]),
        ],
        loaded={"spotify": ["spotify_1"]},
        albums={("spotify_1", SPOTIFY_ALBUM_ID): spotify_album},
    ) as harness:
        album = await harness.resolve()

    assert album is spotify_album
    harness.musicbrainz.get_release_details.assert_awaited_once_with("rel-spotify")


async def test_resolve_surfaces_an_unexpected_provider_error() -> None:
    """An error that is no plain miss, an expired login say, is raised rather than logged away."""
    for error_type in (LoginFailed, InvalidDataError):
        with _harness(
            editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
            releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL, TIDAL_ALBUM_URL])],
            loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"]},
        ) as harness:
            harness.get_provider_item.side_effect = error_type("the provider refused")
            with pytest.raises(error_type):
                await harness.resolve()

        harness.get_provider_item.assert_awaited_once_with(
            SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH
        )


async def test_resolve_keeps_to_the_music_sources_the_user_may_see() -> None:
    """A link to a source the user may not see is passed over, however well it resolves."""
    spotify_album = _album("spotify_1", SPOTIFY_ALBUM_ID)
    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID)
    albums = {
        ("spotify_1", SPOTIFY_ALBUM_ID): spotify_album,
        ("tidal_1", TIDAL_ALBUM_ID): tidal_album,
    }
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL, TIDAL_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"]},
        providers=[_music_provider("tidal_1")],
        albums=albums,
    ) as harness:
        album = await harness.resolve()

    assert album is tidal_album
    harness.get_provider_item.assert_awaited_once_with(TIDAL_ALBUM_ID, "tidal_1", **STRICT_FETCH)

    with (
        _harness(
            editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
            releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
            loaded={"spotify": ["spotify_1"]},
            providers=[],
            albums=albums,
        ) as harness,
        pytest.raises(MediaNotFoundError),
    ):
        await harness.resolve()

    harness.get_provider_item.assert_not_awaited()


async def test_resolve_takes_a_link_on_the_instance_the_user_may_see() -> None:
    """A link to an instance the user may not see is taken on its own instance of that service."""
    spotify_album = _album("spotify_2", SPOTIFY_ALBUM_ID)
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_1", "spotify_2"]},
        providers=[_music_provider("spotify_2")],
        albums={("spotify_2", SPOTIFY_ALBUM_ID): spotify_album},
    ) as harness:
        album = await harness.resolve()

    assert album is spotify_album
    harness.get_provider_item.assert_awaited_once_with(
        SPOTIFY_ALBUM_ID, "spotify_2", **STRICT_FETCH
    )


async def test_resolve_asks_by_barcode_only_when_no_link_resolves() -> None:
    """The barcode fan-out over the providers is spent only when none of the links resolves."""
    spotify_album = _album("spotify_1", SPOTIFY_ALBUM_ID)
    tidal = _music_provider("tidal_1", album=_album("tidal_1", TIDAL_ALBUM_ID))
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"]},
        providers=[_music_provider("spotify_1"), tidal],
        albums={("spotify_1", SPOTIFY_ALBUM_ID): spotify_album},
    ) as harness:
        album = await harness.resolve()

    assert album is spotify_album
    tidal.get_album_by_external_id.assert_not_awaited()


async def test_resolve_asks_every_provider_by_barcode() -> None:
    """Every provider that can is asked for the edition's barcode, the linked one included."""
    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID)
    spotify = _music_provider("spotify_1")
    deezer = _music_provider("deezer_1", error=ProviderUnavailableError("offline"))
    tidal = _music_provider("tidal_1", album=tidal_album)
    qobuz = _music_provider("qobuz_1")
    qobuz.supported_features = set()
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"]},
        providers=[spotify, deezer, tidal, qobuz],
        albums={("tidal_1", TIDAL_ALBUM_ID): tidal_album},
    ) as harness:
        album = await harness.resolve()

    assert album is tidal_album
    spotify.get_album_by_external_id.assert_awaited_once_with(BARCODE, ExternalID.BARCODE)
    qobuz.get_album_by_external_id.assert_not_awaited()
    deezer.get_album_by_external_id.assert_awaited_once_with(BARCODE, ExternalID.BARCODE)
    tidal.get_album_by_external_id.assert_awaited_once_with(BARCODE, ExternalID.BARCODE)
    assert harness.get_provider_item.await_args_list == [
        call(SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH),
        call(TIDAL_ALBUM_ID, "tidal_1", **STRICT_FETCH),
    ]


async def test_resolve_asks_a_linked_provider_by_barcode_when_its_link_is_stale() -> None:
    """A provider whose link no longer resolves may still carry the album under another id."""
    spotify_album = _album("spotify_1", "new-spotify-id")
    spotify = _music_provider("spotify_1", album=spotify_album)
    with _harness(
        editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
        loaded={"spotify": ["spotify_1"]},
        providers=[spotify],
        albums={("spotify_1", "new-spotify-id"): spotify_album},
    ) as harness:
        album = await harness.resolve()

    assert album is spotify_album
    spotify.get_album_by_external_id.assert_awaited_once_with(BARCODE, ExternalID.BARCODE)
    assert harness.get_provider_item.await_args_list == [
        call(SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH),
        call("new-spotify-id", "spotify_1", **STRICT_FETCH),
    ]


async def test_resolve_takes_a_barcode_hit_only_when_it_is_the_release() -> None:
    """A barcode hit that is another album, by title or by artist, is passed over."""
    for other_album in (
        _album("tidal_1", TIDAL_ALBUM_ID, name="Some Other Album"),
        _album("tidal_1", TIDAL_ALBUM_ID, artist_name="Some Other Artist"),
    ):
        with (
            _harness(
                editions=[_edition("rel-digital")],
                releases=[_release("rel-digital")],
                providers=[_music_provider("tidal_1", album=other_album)],
                albums={("tidal_1", TIDAL_ALBUM_ID): other_album},
            ) as harness,
            pytest.raises(MediaNotFoundError),
        ):
            await harness.resolve()
        harness.get_provider_item.assert_not_awaited()

    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID, name=RELEASE_TITLE.upper())
    with _harness(
        editions=[_edition("rel-digital")],
        releases=[_release("rel-digital")],
        providers=[_music_provider("tidal_1", album=tidal_album)],
        albums={("tidal_1", TIDAL_ALBUM_ID): tidal_album},
    ) as harness:
        assert await harness.resolve() is tidal_album


async def test_resolve_asks_the_providers_by_barcode_at_once() -> None:
    """The providers are asked by barcode together, their answers taken in provider order."""
    spotify_album = _album("spotify_1", SPOTIFY_ALBUM_ID)
    tidal_album = _album("tidal_1", TIDAL_ALBUM_ID)
    tidal_asked = asyncio.Event()

    async def _spotify_lookup(*_args: object) -> Album:
        # answers only once Tidal has been asked as well, so the lookups must run together
        await asyncio.wait_for(tidal_asked.wait(), timeout=1)
        return spotify_album

    async def _tidal_lookup(*_args: object) -> Album:
        tidal_asked.set()
        return tidal_album

    deezer = _music_provider("deezer_1", error=ProviderUnavailableError("offline"))
    spotify = _music_provider("spotify_1")
    spotify.get_album_by_external_id = AsyncMock(side_effect=_spotify_lookup)
    tidal = _music_provider("tidal_1")
    tidal.get_album_by_external_id = AsyncMock(side_effect=_tidal_lookup)
    with _harness(
        editions=[_edition("rel-digital")],
        releases=[_release("rel-digital")],
        providers=[deezer, spotify, tidal],
        albums={
            ("spotify_1", SPOTIFY_ALBUM_ID): spotify_album,
            ("tidal_1", TIDAL_ALBUM_ID): tidal_album,
        },
    ) as harness:
        album = await harness.resolve()

    # Spotify answered after Tidal, yet its album comes first as the providers are ordered
    assert album is spotify_album
    harness.get_provider_item.assert_awaited_once_with(
        SPOTIFY_ALBUM_ID, "spotify_1", **STRICT_FETCH
    )


async def test_resolve_skips_the_barcode_lookup_without_a_valid_barcode() -> None:
    """An edition without a usable barcode is not looked up on the providers by it."""
    tidal = _music_provider("tidal_1")
    for barcode in (None, "", "not-a-barcode"):
        with (
            _harness(
                editions=[_edition("rel-digital")],
                releases=[_release("rel-digital", barcode=barcode)],
                providers=[tidal],
            ) as harness,
            pytest.raises(MediaNotFoundError),
        ):
            await harness.resolve()

    tidal.get_album_by_external_id.assert_not_awaited()


async def test_resolve_tells_the_user_when_no_music_service_has_the_album() -> None:
    """An album none of the providers has fails with its own, translated message."""
    with (
        _harness(
            editions=[_edition("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
            releases=[_release("rel-digital", urls=[SPOTIFY_ALBUM_URL])],
            loaded={"spotify": ["spotify_1"]},
        ) as harness,
        pytest.raises(MediaNotFoundError) as excinfo,
    ):
        await harness.resolve()

    assert excinfo.value.translation_key == "album_not_available_on_music_services"
    assert excinfo.value.translation_owner == "core.music"


async def test_resolve_needs_an_official_edition() -> None:
    """A group of bootlegs only, or an unknown group, is not looked up any further."""
    for editions in ([], [_edition("rel-bootleg", status="Bootleg", urls=[SPOTIFY_ALBUM_URL])]):
        with (
            _harness(editions=editions, loaded={"spotify": ["spotify_1"]}) as harness,
            pytest.raises(MediaNotFoundError),
        ):
            await harness.resolve()
        harness.musicbrainz.get_release_details.assert_not_awaited()


# ---------------------------------------------------------------------------
# MusicController.get_item
# ---------------------------------------------------------------------------


async def test_get_item_resolves_a_musicbrainz_album_on_demand() -> None:
    """A MusicBrainz album, by id or by uri, is the album its release group resolves to."""
    album = create_album("spotify_1", SPOTIFY_ALBUM_ID)
    controller, resolve = _music_controller(album)

    assert await controller.get_item(MediaType.ALBUM, RELEASE_GROUP_ID, "musicbrainz") is album
    assert await controller.get_item_by_uri(f"musicbrainz://album/{RELEASE_GROUP_ID}") is album

    # the metadata flag is handed on: a lookup by id refreshes by default, one by uri does not
    assert resolve.await_args_list == [
        call(RELEASE_GROUP_ID, allow_update_metadata=True),
        call(RELEASE_GROUP_ID, allow_update_metadata=False),
    ]


async def test_get_item_knows_no_other_musicbrainz_items() -> None:
    """Only albums can be resolved from MusicBrainz; any other item is not found."""
    controller, resolve = _music_controller()

    for media_type in (MediaType.ARTIST, MediaType.TRACK, MediaType.PLAYLIST):
        with pytest.raises(MediaNotFoundError):
            await controller.get_item(media_type, RELEASE_GROUP_ID, "musicbrainz")

    resolve.assert_not_awaited()


async def test_add_item_to_library_takes_a_musicbrainz_album_uri(mass: MusicAssistant) -> None:
    """Adding a discography album by its MusicBrainz uri adds the album the group resolves to."""
    provider_album = create_album("spotify_1", SPOTIFY_ALBUM_ID)
    with (
        patch.object(
            mass.music.albums,
            "resolve_musicbrainz_release_group",
            AsyncMock(return_value=provider_album),
        ) as resolve,
        patch.object(mass.metadata, "update_metadata", AsyncMock()),
    ):
        library_album = await mass.music.add_item_to_library(
            f"musicbrainz://album/{RELEASE_GROUP_ID}"
        )

    resolve.assert_awaited_once_with(RELEASE_GROUP_ID, allow_update_metadata=False)
    assert isinstance(library_album, Album)
    assert library_album.provider == "library"
    assert {(m.provider_domain, m.item_id) for m in library_album.provider_mappings} == {
        ("spotify", SPOTIFY_ALBUM_ID)
    }
    assert (
        await mass.music.albums.get_library_item_by_prov_id(SPOTIFY_ALBUM_ID, "spotify_1")
        is not None
    )
