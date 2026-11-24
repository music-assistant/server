"""Test that Deezer lists audiobook authors as artists of their own."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, call

import pytest
from deezer_python_gql.generated.enums import AudiobookContributorRoles
from music_assistant_models.enums import ArtistType, MediaType

from music_assistant.providers.deezer import media
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.parsers import (
    parse_audiobook,
    parse_audiobook_from_album,
    parse_author,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Artist, Audiobook

    from music_assistant.providers.deezer.provider import DeezerProvider

AUTHOR = AudiobookContributorRoles.AUTHOR
NARRATOR = AudiobookContributorRoles.NARRATOR


def _unwrap(method: Any) -> Any:
    """Return the method without its cache."""
    return cast("Any", method).__wrapped__


def _artist_node(artist_id: str, name: str, *release_ids: str) -> SimpleNamespace:
    """Build an artist as Deezer returns it, with its newest releases."""
    edges = [SimpleNamespace(node=SimpleNamespace(id=release_id)) for release_id in release_ids]
    return SimpleNamespace(
        id=artist_id,
        name=name,
        picture=None,
        fans_count=10,
        albums=SimpleNamespace(edges=edges),
    )


def _album_node(album_id: str, *artists: SimpleNamespace) -> SimpleNamespace:
    """Build a release as Deezer lists it, with its main artists."""
    edges = [SimpleNamespace(node=artist) for artist in artists]
    return SimpleNamespace(
        id=album_id,
        display_title=f"Release {album_id}",
        cover=None,
        is_explicit=False,
        contributors=SimpleNamespace(edges=edges),
    )


def _search_hits(author: SimpleNamespace, *artists: SimpleNamespace) -> SimpleNamespace:
    """Build a search result with one audiobook by the author, and these artists."""
    return SimpleNamespace(
        results=SimpleNamespace(
            albums=SimpleNamespace(edges=[SimpleNamespace(node=_album_node("1", author))]),
            artists=SimpleNamespace(edges=[SimpleNamespace(node=artist) for artist in artists]),
        )
    )


@pytest.fixture
def gql(provider: DeezerProvider) -> AsyncMock:
    """Stub the GraphQL client and wire up a real media manager."""
    client = AsyncMock()
    provider.gql_client = client
    provider.media_manager = DeezerMediaManager(provider)
    return client


def test_audiobook_authors_are_artists(provider: DeezerProvider) -> None:
    """Authors become AUTHOR artists, narrators stay text and "Unknown" is left out."""
    contributors = [
        (AUTHOR, _artist_node("75255", "Conni")),
        (AUTHOR, _artist_node("75255", "Conni")),
        (AUTHOR, _artist_node("8592", "Unknown")),
        (NARRATOR, _artist_node("1", "Sprecherin")),
    ]
    audiobook = SimpleNamespace(
        id="220381052",
        display_title="Conni geht zum Arzt",
        cover=None,
        is_explicit=False,
        description=None,
        fans_count=0,
        contributors=SimpleNamespace(
            edges=[SimpleNamespace(roles=[role], node=node) for role, node in contributors]
        ),
        duration=3600,
        publisher=None,
        is_favorite=False,
    )

    parsed = parse_audiobook(provider, audiobook)  # type: ignore[arg-type]

    assert [(a.item_id, a.artist_type) for a in parsed.authors] == [  # type: ignore[union-attr]
        ("author_75255", ArtistType.AUTHOR)
    ]
    assert list(parsed.narrators) == ["Sprecherin"]
    provider._validate_audiobook_author_narrator_types(parsed)


def test_audiobook_from_a_release_has_artist_authors(provider: DeezerProvider) -> None:
    """An audiobook found among releases also lists its authors as artists."""
    release = _album_node("1", _artist_node("75255", "Conni"), _artist_node("8592", "Unknown"))

    parsed = parse_audiobook_from_album(provider, release)  # type: ignore[arg-type]

    assert [a.item_id for a in parsed.authors] == ["author_75255"]  # type: ignore[union-attr]
    provider._validate_audiobook_author_narrator_types(parsed)


async def test_author_page_shows_the_artist_as_author(
    provider: DeezerProvider, gql: AsyncMock
) -> None:
    """An author id fetches the real artist and returns it as author, favorite mark included."""
    gql.get_artist.return_value = SimpleNamespace(
        **vars(_artist_node("75255", "Conni")),
        url=SimpleNamespace(web_url="https://x/75255"),
        is_favorite=True,
    )

    artist = await _unwrap(DeezerMediaManager.get_artist)(provider.media_manager, "author_75255")

    gql.get_artist.assert_awaited_once_with(artist_id="75255")
    assert (artist.item_id, artist.artist_type) == ("author_75255", ArtistType.AUTHOR)
    assert [m.item_id for m in artist.provider_mappings] == ["author_75255"]
    assert artist.favorite is True


async def test_releases_are_split_into_albums_and_audiobooks(
    provider: DeezerProvider, gql: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artist page lists the albums, the author page the audiobooks."""
    monkeypatch.setattr(media, "parse_album", lambda _provider, node: node.id)
    monkeypatch.setattr(media, "parse_audiobook_from_album", lambda _provider, node: node.id)
    page_info = SimpleNamespace(has_next_page=False, end_cursor=None)
    edges = [SimpleNamespace(node=_album_node(album_id)) for album_id in ("1", "2", "3")]
    gql.get_artist.return_value = SimpleNamespace(
        albums=SimpleNamespace(edges=edges, page_info=page_info)
    )
    gql.check_audiobook_ids.return_value = {"2", "3"}
    manager = provider.media_manager

    albums = await _unwrap(DeezerMediaManager.get_artist_albums)(manager, "75255")
    audiobooks = await _unwrap(DeezerMediaManager.get_author_audiobooks)(manager, "author_75255")

    assert albums == ["1"]
    assert audiobooks == ["2", "3"]
    gql.get_artist.assert_awaited_with(artist_id="75255")


async def test_top_tracks_leave_out_chapters(
    provider: DeezerProvider, gql: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapters of an audiobook are not top tracks of the artist."""
    monkeypatch.setattr(media, "parse_track", lambda _provider, node: node.id)
    tracks = [
        SimpleNamespace(id="song", album=SimpleNamespace(id="album")),
        SimpleNamespace(id="chapter", album=SimpleNamespace(id="audiobook")),
    ]
    page_info = SimpleNamespace(has_next_page=False, end_cursor=None)
    gql.get_artist.return_value = SimpleNamespace(
        top_tracks=SimpleNamespace(
            edges=[SimpleNamespace(node=track) for track in tracks], page_info=page_info
        )
    )
    gql.check_audiobook_ids.return_value = {"audiobook"}

    top = await _unwrap(DeezerMediaManager.get_artist_toptracks)(provider.media_manager, "1")

    assert top == ["song"]


@pytest.mark.parametrize("method", ["get_artist_albums", "get_artist_toptracks"])
async def test_author_has_no_music(provider: DeezerProvider, gql: AsyncMock, method: str) -> None:
    """An author has no albums or top tracks of its own."""
    result = await _unwrap(getattr(DeezerMediaManager, method))(
        provider.media_manager, "author_75255"
    )

    assert result == []
    gql.get_artist.assert_not_called()


@pytest.mark.parametrize(
    "media_types", [[MediaType.ARTIST, MediaType.AUDIOBOOK], [MediaType.ARTIST]]
)
async def test_search_lists_the_author_after_the_artist(
    provider: DeezerProvider,
    gql: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    media_types: list[MediaType],
) -> None:
    """An artist who wrote an audiobook hit is listed a second time, as author."""
    conni, other = _artist_node("75255", "Conni"), _artist_node("2", "Conny")
    gql.search.return_value = _search_hits(conni, conni, other)
    gql.check_audiobook_ids.return_value = {"1"}
    monkeypatch.setattr(provider.media_manager, "_is_author_only", AsyncMock(return_value=False))

    result = await _unwrap(DeezerMediaManager.search)(provider.media_manager, "conni", media_types)

    artists = cast("list[Artist]", result.artists)
    assert [(a.item_id, a.artist_type) for a in artists] == [
        ("75255", ArtistType.SINGER),
        ("author_75255", ArtistType.AUTHOR),
        ("2", ArtistType.SINGER),
    ]
    assert len(result.audiobooks) == (MediaType.AUDIOBOOK in media_types)


@pytest.mark.parametrize(
    ("media_types", "albums"),
    [
        ([MediaType.ALBUM, MediaType.ARTIST], ["1"]),
        ([MediaType.ALBUM, MediaType.ARTIST, MediaType.AUDIOBOOK], []),
    ],
    ids=["without audiobooks", "with audiobooks"],
)
async def test_search_keeps_audiobooks_among_albums_unless_searched(
    provider: DeezerProvider,
    gql: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    media_types: list[MediaType],
    albums: list[str],
) -> None:
    """Audiobooks leave the album results only when audiobooks are searched too."""
    gql.search.return_value = _search_hits(_artist_node("75255", "Conni"))
    gql.check_audiobook_ids.return_value = {"1"}
    monkeypatch.setattr(media, "parse_album", lambda _provider, node: node.id)

    result = await _unwrap(DeezerMediaManager.search)(provider.media_manager, "conni", media_types)

    assert result.albums == albums


@pytest.mark.parametrize(
    ("author_only", "listed"),
    [
        (AsyncMock(return_value=True), ["author_32067711"]),
        (AsyncMock(side_effect=TimeoutError), ["32067711", "author_32067711"]),
    ],
    ids=["no music", "check fails"],
)
async def test_search_lists_an_author_without_music_only_as_author(
    provider: DeezerProvider,
    gql: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    author_only: AsyncMock,
    listed: list[str],
) -> None:
    """An author is only listed as artist too if the artist page has music."""
    tilda = _artist_node("32067711", "Tilda Apfelkern")
    gql.search.return_value = _search_hits(tilda, tilda)
    gql.check_audiobook_ids.return_value = {"1"}
    monkeypatch.setattr(provider.media_manager, "_is_author_only", author_only)

    result = await _unwrap(DeezerMediaManager.search)(
        provider.media_manager, "tilda", [MediaType.ARTIST]
    )

    assert [artist.item_id for artist in result.artists] == listed
    author_only.assert_awaited_once_with("32067711")


@pytest.mark.parametrize(
    ("audiobooks", "author_only"),
    [({"1", "2"}, True), ({"2"}, False), ({"1"}, False), (set(), False)],
    ids=["all audiobooks", "newest is music", "older music", "only music"],
)
async def test_an_author_is_told_from_the_newest_releases(
    provider: DeezerProvider, gql: AsyncMock, audiobooks: set[str], author_only: bool
) -> None:
    """An artist is an author without music if all of its newest releases are audiobooks."""
    gql.get_artist.return_value = _artist_node("32067711", "Tilda Apfelkern", "1", "2")
    gql.check_audiobook_ids.side_effect = lambda ids: set(ids) & audiobooks

    result = await _unwrap(DeezerMediaManager._is_author_only)(provider.media_manager, "32067711")

    assert result is author_only
    gql.get_artist.assert_awaited_once_with(artist_id="32067711")


async def test_library_lists_the_authors_of_its_audiobooks(
    provider: DeezerProvider, gql: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every author of a library audiobook is a library artist, once."""
    conni, liane = _artist_node("75255", "Conni"), _artist_node("6449846", "Liane Schneider")
    audiobooks = [
        parse_audiobook_from_album(provider, _album_node("1", conni, liane)),  # type: ignore[arg-type]
        parse_audiobook_from_album(provider, _album_node("2", conni)),  # type: ignore[arg-type]
    ]

    async def library_audiobooks() -> AsyncGenerator[Audiobook]:
        for audiobook in audiobooks:
            yield audiobook

    manager = provider.media_manager
    gql.get_favorite_artists.return_value = None
    monkeypatch.setattr(manager, "_get_personal_songs", AsyncMock(return_value=[]))
    monkeypatch.setattr(manager, "get_library_audiobooks", library_audiobooks)

    artists = [artist async for artist in manager.get_library_artists()]

    assert [(a.item_id, a.artist_type) for a in artists] == [
        ("author_75255", ArtistType.AUTHOR),
        ("author_6449846", ArtistType.AUTHOR),
    ]


async def test_author_favorites_go_to_deezer(provider: DeezerProvider, gql: AsyncMock) -> None:
    """An author is the same Deezer artist: adding and a dislike reach Deezer."""
    manager = provider.media_manager

    added = await manager.library_add(parse_author(provider, "75255", "Conni"))
    await manager.set_favorite("author_75255", MediaType.ARTIST, False)

    assert added is True
    gql.add_artist_to_favorite.assert_awaited_once_with(artist_id="75255")
    gql.ban_artist_from_recommendation.assert_awaited_once_with(artist_id="75255")


@pytest.mark.parametrize(
    ("is_favorite", "audiobooks", "unfavorited"),
    [(True, {"1"}, True), (True, set(), False), (False, {"1"}, False)],
    ids=["favorite author", "favorite music artist", "no favorite"],
)
async def test_removing_an_author_unfavorites_only_a_favorite_author(
    provider: DeezerProvider,
    gql: AsyncMock,
    is_favorite: bool,
    audiobooks: set[str],
    unfavorited: bool,
) -> None:
    """Removing an author unfavorites the Deezer artist only if that favorite lists the author."""
    artist = _artist_node("75255", "Conni", "1", "2")
    artist.is_favorite = is_favorite
    gql.get_artist.return_value = artist
    gql.check_audiobook_ids.side_effect = lambda ids: set(ids) & audiobooks

    removed = await provider.media_manager.library_remove("author_75255", MediaType.ARTIST)

    assert removed is True
    expected = [call(artist_id="75255")] if unfavorited else []
    assert gql.remove_artist_from_favorite.await_args_list == expected


@pytest.mark.parametrize(
    ("check_fails", "listed"),
    [(False, ["1", "author_32067711", "3", "author_3"]), (True, ["author_32067711"])],
    ids=["checked", "check fails"],
)
async def test_library_lists_favorite_authors(
    provider: DeezerProvider,
    gql: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    check_fails: bool,
    listed: list[str],
) -> None:
    """A favorite artist is an author with an audiobook among its newest releases, an artist with music."""
    band = _artist_node("1", "Band", "10", "11", "12", "13")
    tilda = _artist_node("32067711", "Tilda Apfelkern", "20", "21", "22", "23")
    conni = _artist_node("3", "Conni", "30", "31", "32", "33")
    for node in (band, tilda, conni):
        node.is_favorite = True
    page_info = SimpleNamespace(has_next_page=False, end_cursor=None)
    edges = [SimpleNamespace(node=node, favorited_at=None) for node in (band, tilda, conni)]
    gql.get_favorite_artists.return_value = SimpleNamespace(
        user_favorites=SimpleNamespace(artists=SimpleNamespace(edges=edges, page_info=page_info))
    )
    audiobook_ids = {"20", "21", "22", "23", "32"}
    gql.check_audiobook_ids.side_effect = (
        TimeoutError if check_fails else lambda ids: set(ids) & audiobook_ids
    )
    audiobook = parse_audiobook_from_album(provider, _album_node("2", tilda))  # type: ignore[arg-type]

    async def library_audiobooks() -> AsyncGenerator[Audiobook]:
        yield audiobook

    manager = provider.media_manager
    report_skipped = Mock()
    monkeypatch.setattr(provider, "report_skipped_sync_item", report_skipped)
    monkeypatch.setattr(manager, "_get_personal_songs", AsyncMock(return_value=[]))
    monkeypatch.setattr(manager, "get_library_audiobooks", library_audiobooks)

    artists = [artist async for artist in manager.get_library_artists()]

    assert [artist.item_id for artist in artists] == listed
    assert report_skipped.call_count == check_fails
    if not check_fails:
        assert all(artist.favorite for artist in artists)
        # only the authors get their older releases checked
        assert gql.check_audiobook_ids.await_args_list == [
            call(["10", "11", "12", "20", "21", "22", "30", "31", "32"]),
            call(["23", "33"]),
        ]
