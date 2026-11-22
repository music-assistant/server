"""Unit tests for the Apple Music media manager."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Track

from music_assistant.constants import DB_TABLE_CACHE
from music_assistant.mass import MusicAssistant
from music_assistant.providers.apple_music import media
from music_assistant.providers.apple_music.api_client import AppleMusicAPIClient
from music_assistant.providers.apple_music.library import AppleMusicLibraryManager
from music_assistant.providers.apple_music.media import AppleMusicMediaManager
from tests.common import use_real_create_task


def _catalog_song(catalog_id: str) -> dict[str, Any]:
    """Build a minimal catalog/songs response item."""
    return {
        "id": catalog_id,
        "type": "songs",
        "attributes": {
            "name": f"Catalog {catalog_id}",
            "artistName": "Artist",
            "albumName": "Album",
            "durationInMillis": 180000,
            "playParams": {"id": catalog_id},
        },
        "relationships": {},
    }


def _library_song(library_id: str, *, catalog_id: str | None = None) -> dict[str, Any]:
    """Build a me/library/songs response item, optionally carrying its catalog twin."""
    relationships: dict[str, Any] = {}
    if catalog_id is not None:
        relationships["catalog"] = {"data": [_catalog_song(catalog_id)]}
    return {
        "id": library_id,
        "type": "library-songs",
        "attributes": {
            "name": f"Library {library_id}",
            "artistName": "Artist",
            "albumName": "Album",
            "durationInMillis": 180000,
            "playParams": {"id": library_id},
        },
        "relationships": relationships,
    }


@pytest.fixture
def mock_api() -> MagicMock:
    """Return a MagicMock representing the Apple Music API client."""
    api_client = MagicMock()
    api_client.get_data = AsyncMock()
    return api_client


@pytest.fixture
def manager(mock_api: MagicMock) -> AppleMusicMediaManager:
    """Return an AppleMusicMediaManager wired to a mock API client and a always-miss cache."""
    provider = MagicMock()
    provider.instance_id = "apple_music_test"
    provider.domain = "apple_music"
    provider.storefront = "us"
    provider.logger = MagicMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    provider.library_manager.mark_purchases_on_sale = AsyncMock()
    use_real_create_task(provider.mass)
    provider.api_client = mock_api

    return AppleMusicMediaManager(provider)


@pytest.mark.asyncio
async def test_get_track_uses_catalog_endpoint_for_catalog_id(
    manager: AppleMusicMediaManager,
    mock_api: MagicMock,
) -> None:
    """get_track queries the catalog endpoint for a numeric adam id."""
    mock_api.get_data.return_value = {"data": [_catalog_song("1234567890")]}

    result = await manager.get_track("1234567890")

    mock_api.get_data.assert_called_once_with(
        "catalog/us/songs/1234567890",
        include="artists,albums",
    )
    assert isinstance(result, Track)
    assert result.item_id == "1234567890"


@pytest.mark.asyncio
async def test_get_track_uses_library_endpoint_for_library_id(
    manager: AppleMusicMediaManager,
    mock_api: MagicMock,
) -> None:
    """get_track queries me/library/songs for an 'i.' library id instead of 404ing."""
    mock_api.get_data.return_value = {"data": [_library_song("i.AWPNG58CL3m51X")]}

    result = await manager.get_track("i.AWPNG58CL3m51X")

    mock_api.get_data.assert_called_once_with(
        "me/library/songs/i.AWPNG58CL3m51X",
        include="catalog,artists,albums",
    )
    assert isinstance(result, Track)
    assert result.item_id == "i.AWPNG58CL3m51X"


@pytest.mark.asyncio
async def test_get_track_library_id_prefers_catalog_twin(
    manager: AppleMusicMediaManager,
    mock_api: MagicMock,
) -> None:
    """The included catalog relationship collapses a library id onto the catalog adam id."""

    async def _get_data(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        # Apple only embeds the catalog twin when it was actually requested, so a
        # dropped include= would silently leave the library id in place
        catalog_id = "1234567890" if "catalog" in kwargs.get("include", "") else None
        return {"data": [_library_song("i.AWPNG58CL3m51X", catalog_id=catalog_id)]}

    mock_api.get_data.side_effect = _get_data

    result = await manager.get_track("i.AWPNG58CL3m51X")

    assert result.item_id == "1234567890"
    assert result.name == "Catalog 1234567890"


@pytest.mark.asyncio
async def test_get_artist_albums_stops_paging_at_limit(
    manager: AppleMusicMediaManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large discography stops paging once the album limit is reached."""

    async def _get_data(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        offset, limit = kwargs["offset"], kwargs["limit"]
        page = [{"id": str(idx)} for idx in range(offset, min(offset + limit, 1000))]
        return {"data": page, "next": "more" if offset + limit < 1000 else None}

    monkeypatch.setattr(media, "parse_album", lambda _provider, album: album["id"])
    api = AppleMusicAPIClient(MagicMock())
    api.get_data = AsyncMock(side_effect=_get_data)  # type: ignore[method-assign]
    manager.api = api

    albums = await manager.get_artist_albums("123")

    assert albums == [str(idx) for idx in range(100)]
    assert api.get_data.await_count == 1


class _DictCache:
    """In-memory stand-in for the cache controller."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, int], Any] = {}

    async def get_with_freshness(
        self, key: str, category: int = 0, **_: Any
    ) -> tuple[Any, bool, bool]:
        return self.data.get((key, category)), True, (key, category) in self.data

    async def get(self, key: str, category: int = 0, **_: Any) -> Any:
        return self.data.get((key, category))

    async def set(self, key: str, data: Any, category: int = 0, **_: Any) -> None:
        self.data[(key, category)] = data


def _purchase(library_id: str, purchased_id: str) -> dict[str, Any]:
    """Build a me/library/songs item of a purchase without a catalog twin."""
    item = _library_song(library_id)
    item["attributes"]["playParams"]["purchasedId"] = purchased_id
    return item


SOLD = "397010985"
REVOKED = "915794195"


@pytest.fixture
def purchases_manager() -> tuple[AppleMusicMediaManager, MagicMock]:
    """Return a media manager with a real library manager, a dict cache and an api mock."""
    provider = MagicMock()
    provider.instance_id = "apple_music_test"
    provider.domain = "apple_music"
    provider.storefront = "us"
    provider.mass.cache = _DictCache()
    use_real_create_task(provider.mass)
    library_items = [_purchase("i.sold", SOLD), _purchase("i.revoked", REVOKED)]

    async def _get_data(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if endpoint == "catalog/us/songs":
            return {"data": [{"id": i} for i in kwargs["ids"].split(",") if i == SOLD]}
        if endpoint.startswith("me/library/songs/"):
            return {"data": [_purchase("i.sold", SOLD)]}
        if endpoint.endswith("/tracks"):
            return {"data": library_items}
        # the playlist row that carries the change marker
        return {"data": [{"id": "p.1", "attributes": {"lastModifiedDate": "2026-11-01"}}]}

    provider.api_client.get_data = AsyncMock(side_effect=_get_data)
    provider.library_manager = AppleMusicLibraryManager(provider)
    return AppleMusicMediaManager(provider), provider.api_client.get_data


def _availability(tracks: list[Track]) -> dict[str, bool]:
    return {
        track.item_id: all(mapping.available for mapping in track.provider_mappings)
        for track in tracks
    }


def _purchase_lookups(get_data: AsyncMock) -> list[str]:
    return [c.kwargs["ids"] for c in get_data.call_args_list if c.args[0] == "catalog/us/songs"]


async def test_library_playlist_offers_the_purchases_apple_still_sells(
    purchases_manager: tuple[AppleMusicMediaManager, AsyncMock],
) -> None:
    """A playlist lists a purchase still sold as available, and a revoked one as not."""
    manager, get_data = purchases_manager

    tracks = await manager.get_playlist_tracks("p.1")

    assert _availability(tracks) == {"i.sold": True, "i.revoked": False}
    assert _purchase_lookups(get_data) == [f"{SOLD},{REVOKED}"]


async def test_cached_playlist_listing_makes_no_purchase_request(
    purchases_manager: tuple[AppleMusicMediaManager, AsyncMock],
) -> None:
    """A listing served from its own cache keeps the answer without asking Apple again."""
    manager, get_data = purchases_manager
    await manager.get_playlist_tracks("p.1")
    get_data.reset_mock()

    tracks = await manager.get_playlist_tracks("p.1")

    assert _availability(tracks) == {"i.sold": True, "i.revoked": False}
    get_data.assert_not_called()


async def test_library_album_tracks_offer_the_purchases_apple_still_sells(
    purchases_manager: tuple[AppleMusicMediaManager, AsyncMock],
) -> None:
    """Library album tracks agree with the sync on which purchases are available."""
    manager, get_data = purchases_manager

    tracks = await manager.get_album_tracks("l.album")

    assert _availability(tracks) == {"i.sold": True, "i.revoked": False}
    assert _purchase_lookups(get_data) == [f"{SOLD},{REVOKED}"]


async def test_get_track_offers_a_purchase_apple_still_sells(
    purchases_manager: tuple[AppleMusicMediaManager, AsyncMock],
) -> None:
    """A refresh of a purchase still sold keeps it available."""
    manager, get_data = purchases_manager

    track = await manager.get_track("i.sold")

    assert _availability([track]) == {"i.sold": True}
    assert _purchase_lookups(get_data) == [SOLD]


@pytest.mark.parametrize("marker_moves", [True, False], ids=["marker moves", "marker stays"])
async def test_a_forgotten_playlist_is_read_anew(
    mass_minimal: MusicAssistant, marker_moves: bool
) -> None:
    """A forgotten playlist is read anew, also while Apple still reports its old marker."""
    await mass_minimal.cache._setup_database()
    playlist: dict[str, Any] = {"songs": ["i.1"], "marker": "2026-11-01", "marker_reads": 0}

    async def _get_data(endpoint: str, **_kwargs: Any) -> dict[str, Any]:
        if endpoint.endswith("/tracks"):
            return {"data": [_library_song(song) for song in playlist["songs"]]}
        playlist["marker_reads"] += 1
        return {"data": [{"id": "p.1", "attributes": {"lastModifiedDate": playlist["marker"]}}]}

    provider = MagicMock()
    provider.mass = mass_minimal
    provider.instance_id = "apple_music_test"
    provider.domain = "apple_music"
    provider.api_client.get_data = AsyncMock(side_effect=_get_data)
    provider.library_manager.mark_purchases_on_sale = AsyncMock()
    manager = AppleMusicMediaManager(provider)

    async def _listed() -> list[str]:
        return [track.item_id for track in await manager.get_playlist_tracks("p.1")]

    assert await _listed() == ["i.1"]
    # the change marker and the first page
    await _wait_for_kept(mass_minimal, "apple_music_test", 2)
    playlist["songs"].append("i.2")
    if marker_moves:
        playlist["marker"] = "2026-11-02"
    assert await _listed() == ["i.1"]
    await manager.forget_playlist_tracks("p.1")
    assert await _listed() == ["i.1", "i.2"]
    # the pages read anew are kept under the marker as it is now
    assert playlist["marker_reads"] == 2


async def _wait_for_kept(mass: MusicAssistant, provider_id: str, count: int) -> None:
    """Wait until the given number of results is kept in the cache for a provider."""
    assert mass.cache.database is not None
    query = f"SELECT key FROM {DB_TABLE_CACHE} WHERE provider = :provider"
    for _ in range(200):
        if (
            await mass.cache.database.get_count_from_query(query, {"provider": provider_id})
            == count
        ):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"{count} results were never kept for {provider_id}")
