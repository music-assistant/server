"""Tests for browsing: searched, sorted and paged on the server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    UniqueList,
)

from music_assistant.controllers.music.constants import BROWSE_CACHE_EXPIRATION
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PATH = "qobuz_1://playlists"


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: streaming folders are cached."""
    return music_mass_with_cache


def _folder(name: str) -> BrowseFolder:
    return BrowseFolder(
        item_id=name.lower(), provider="qobuz_1", path=f"{PATH}/{name.lower()}", name=name
    )


def _contents() -> list[Any]:
    """Return a folder as the provider lists it: sub folders between its items."""
    playlist = Playlist(
        item_id="zebra",
        provider="qobuz_1",
        name="Zebra",
        provider_mappings={
            ProviderMapping(item_id="zebra", provider_domain="qobuz", provider_instance="qobuz_1")
        },
    )
    apple = ItemMapping(
        media_type=MediaType.TRACK, item_id="apple", provider="qobuz_1", name="Apple"
    )
    return [playlist, _folder("Mango"), apple, _folder("Banana")]


def _provider(streaming: bool = True) -> MagicMock:
    """Return the music provider the folder belongs to."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = "qobuz_1"
    provider.name = "Qobuz"
    provider.is_streaming_provider = streaming
    provider.browse = AsyncMock(return_value=_contents())
    return provider


def _names(items: Any) -> list[str]:
    return [item.name for item in items]


async def test_streaming_folder_is_browsed_once(mass: MusicAssistant) -> None:
    """A streaming provider's folder is browsed once, then searched, sorted and paged."""
    provider = _provider()

    async def _browse(**kwargs: Any) -> list[str]:
        return _names(await mass.music.browse(PATH, **kwargs))

    with patch.object(mass, "get_provider", return_value=provider):
        # the way back up first, then the folder in the provider's order
        assert await _browse() == ["..", "Zebra", "Mango", "Apple", "Banana"]
        # by name, the folders come before the other items
        assert await _browse(sort_field=SortField.NAME) == [
            "..",
            "Banana",
            "Mango",
            "Apple",
            "Zebra",
        ]
        assert await _browse(sort_field=SortField.NAME, sort_direction=SortDirection.DESC) == [
            "..",
            "Mango",
            "Banana",
            "Zebra",
            "Apple",
        ]
        assert await _browse(search="an") == ["..", "Mango", "Banana"]
        # the way back up counts as the first entry of the listing
        assert await _browse(limit=2) == ["..", "Zebra"]
        assert await _browse(limit=2, offset=2) == ["Mango", "Apple"]
        assert await _browse(limit=0, offset=4) == ["Banana"]
        items = await mass.music.browse(PATH)
    provider.browse.assert_awaited_once_with(path=PATH)
    # served from the cache, every item comes back as the kind it was listed as
    assert [type(item) for item in items] == [
        BrowseFolder,
        Playlist,
        BrowseFolder,
        ItemMapping,
        BrowseFolder,
    ]
    assert isinstance(items[2], BrowseFolder)
    assert items[2].path == f"{PATH}/mango"


async def test_cached_folder_keeps_every_kind_of_item(mass: MusicAssistant) -> None:
    """A folder served from the cache holds the same kinds of items, with all they carry."""
    mix = RecommendationFolder(
        item_id="mix",
        provider="qobuz_1",
        name="Mix",
        items=UniqueList(
            [ItemMapping(media_type=MediaType.TRACK, item_id="t1", provider="qobuz_1", name="One")]
        ),
    )
    albums = BrowseFolder(
        item_id="albums",
        provider="qobuz_1",
        path=f"{PATH}/albums",
        name="",
        translation_key="albums",
    )
    provider = _provider()
    provider.browse = AsyncMock(return_value=[mix, albums])
    with patch.object(mass, "get_provider", return_value=provider):
        await mass.music.browse(PATH)
        items = await mass.music.browse(PATH)
    provider.browse.assert_awaited_once()
    assert isinstance(items[1], RecommendationFolder)
    assert _names(items[1].items) == ["One"]
    assert isinstance(items[2], BrowseFolder)
    assert items[2].translation_key == "albums"


async def test_streaming_folder_is_kept_just_long_enough_to_page(mass: MusicAssistant) -> None:
    """A folder can show what is live right now, so it is kept for a short while only."""
    with (
        patch.object(mass, "get_provider", return_value=_provider()),
        patch.object(mass.cache, "set", AsyncMock()) as cache_set,
    ):
        await mass.music.browse(PATH)
    assert cache_set.await_args is not None
    assert cache_set.await_args.kwargs["expiration"] == BROWSE_CACHE_EXPIRATION


async def test_library_folder_of_a_streaming_provider_is_browsed_every_time(
    mass: MusicAssistant,
) -> None:
    """A service's own library folder comes from the database, so it is read anew."""
    saved = Playlist(item_id="1", provider="library", name="Saved", provider_mappings=set())
    provider = _provider()
    provider.browse = AsyncMock(return_value=[saved])
    with patch.object(mass, "get_provider", return_value=provider):
        for _ in range(2):
            assert _names(await mass.music.browse(PATH)) == ["..", "Saved"]
    assert provider.browse.await_count == 2


async def test_folder_another_account_answers_for_is_not_kept(mass: MusicAssistant) -> None:
    """While the requested account is unavailable, the folder another account lists is not kept."""
    provider = _provider()
    provider.instance_id = "qobuz_2"
    with patch.object(mass, "get_provider", return_value=provider):
        for _ in range(2):
            await mass.music.browse(PATH)
    assert provider.browse.await_count == 2


async def test_local_folder_is_browsed_every_time(mass: MusicAssistant) -> None:
    """A local source's folder is read anew, so a change on disk shows right away."""
    provider = _provider(streaming=False)
    with patch.object(mass, "get_provider", return_value=provider):
        for _ in range(2):
            assert _names(await mass.music.browse(PATH, search="zeb")) == ["..", "Zebra"]
    assert provider.browse.await_count == 2


async def test_browse_rejects_an_unsupported_sort_before_browsing(mass: MusicAssistant) -> None:
    """A sort browsing does not offer fails before the provider is asked anything."""
    provider = _provider()
    with (
        patch.object(mass, "get_provider", return_value=provider),
        pytest.raises(InvalidDataError),
    ):
        await mass.music.browse(PATH, sort_field=SortField.DURATION)
    provider.browse.assert_not_awaited()


async def test_browse_root_is_searched_sorted_and_paged(mass: MusicAssistant) -> None:
    """The providers listed at the root are searched, sorted and paged like any folder."""
    providers = [
        SimpleNamespace(domain=domain, instance_id=f"{domain}_1", name=name)
        for domain, name in (("zvuk", "Zvuk"), ("apple", "Apple Music"), ("deezer", "Deezer"))
    ]

    def _supporting(feature: ProviderFeature, **_kwargs: Any) -> list[Any]:
        return providers if feature == ProviderFeature.BROWSE else []

    async def _root(**kwargs: Any) -> list[str]:
        return _names(await mass.music.browse(**kwargs))

    with patch.object(mass, "get_providers_supporting_feature", side_effect=_supporting):
        assert await _root() == ["Zvuk", "Apple Music", "Deezer"]
        assert await _root(sort_field=SortField.NAME) == ["Apple Music", "Deezer", "Zvuk"]
        assert await _root(search="deez") == ["Deezer"]
        assert await _root(limit=1, offset=1) == ["Apple Music"]
