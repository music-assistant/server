"""Tests for browsing the playlists and albums of the users a Soundcloud account follows."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.soundcloud import (
    FOLLOWING_PAGE_SIZE,
    USER_SETS_LIMIT,
    SoundcloudMusicProvider,
)

BASE = "soundcloud--test123://"


def _user_obj(user_id: int | None, permalink: str = "muloka", **extra: Any) -> dict[str, Any]:
    """Build a Soundcloud API user object."""
    return {
        "id": user_id,
        "username": permalink.capitalize(),
        "permalink": permalink,
        "avatar_url": "https://i1.sndcdn.com/avatars-abc-large.jpg",
        **extra,
    }


def _playlist_obj(playlist_id: int, title: str, created_at: str = "") -> dict[str, Any]:
    """Build a Soundcloud API playlist object."""
    return {"id": playlist_id, "title": title, "created_at": created_at}


@pytest.fixture
def uncached(provider: SoundcloudMusicProvider) -> SoundcloudMusicProvider:
    """Make the cached browse helpers call straight through to the api client."""
    followed: Any = SoundcloudMusicProvider._get_followed_users.__wrapped__  # type: ignore[attr-defined]
    user_sets: Any = SoundcloudMusicProvider._get_user_sets.__wrapped__  # type: ignore[attr-defined]
    provider._get_followed_users = lambda: followed(provider)  # type: ignore[method-assign]
    provider._get_user_sets = lambda *args: user_sets(provider, *args)  # type: ignore[method-assign]
    return provider


async def test_root_adds_following_folder(
    provider: SoundcloudMusicProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root listing keeps the standard folders and adds the Following folder."""
    library_folder = BrowseFolder(item_id="artists", provider="x", path=f"{BASE}artists", name="")
    monkeypatch.setattr(MusicProvider, "browse", AsyncMock(return_value=[library_folder]))

    result = await provider.browse(BASE)

    assert result[0] is library_folder
    following = result[-1]
    assert isinstance(following, BrowseFolder)
    assert following.path == f"{BASE}following"
    assert following.translation_key == "following"


async def test_root_keeps_the_standard_folders(provider: SoundcloudMusicProvider) -> None:
    """The standard root folders stay listed, in front of the Following folder."""
    result = await provider.browse(BASE)

    assert [folder.path for folder in result if isinstance(folder, BrowseFolder)] == [
        f"{BASE}artists",
        f"{BASE}tracks",
        f"{BASE}playlists",
        f"{BASE}recommendations",
        f"{BASE}following",
    ]


async def test_other_paths_go_to_the_standard_browse(
    provider: SoundcloudMusicProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path other than the library folders is left to the base implementation."""
    base_browse = AsyncMock(return_value=[])
    monkeypatch.setattr(MusicProvider, "browse", base_browse)

    assert await provider.browse(f"{BASE}recommendations") == []
    base_browse.assert_awaited_once_with(f"{BASE}recommendations")


@pytest.mark.parametrize("folder", ["artists", "tracks", "playlists"])
async def test_library_folder_lists_every_library_item(
    provider: SoundcloudMusicProvider, folder: str
) -> None:
    """A library folder lists all of its library items, not just the first 500."""
    items = [Mock() for _ in range(501)]
    library_items = AsyncMock(return_value=items)
    getattr(provider.mass.music, folder).library_items = library_items

    assert await provider.browse(f"{BASE}{folder}") == items
    library_items.assert_awaited_once_with(provider=provider.instance_id, summary=False, limit=0)


async def test_library_folder_is_listed_live_before_the_library_holds_it(
    provider: SoundcloudMusicProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the library holds any of the folder's items, Soundcloud lists them itself."""
    monkeypatch.setattr(provider.mass.music.playlists, "library_items", AsyncMock(return_value=[]))
    live = Mock()

    async def _live_playlists() -> Any:
        yield live

    monkeypatch.setattr(provider, "get_library_playlists", _live_playlists)

    assert await provider.browse(f"{BASE}playlists") == [live]


async def test_following_lists_a_folder_per_user(uncached: SoundcloudMusicProvider) -> None:
    """Each followed user becomes a folder with the user's avatar; unusable users are skipped."""
    uncached._soundcloud.get_following.return_value = {
        "collection": [_user_obj(None), _user_obj(42)]
    }

    result = await uncached.browse(f"{BASE}following")

    assert len(result) == 1
    folder = result[0]
    assert isinstance(folder, BrowseFolder)
    assert folder.name == "Muloka"
    # the permalink keeps the breadcrumb readable
    assert folder.path == f"{BASE}following/muloka"
    assert folder.image is not None
    assert folder.image.path.endswith("avatars-abc-t500x500.jpg")
    uncached._soundcloud.get_following.assert_awaited_once_with("1", FOLLOWING_PAGE_SIZE)


async def test_following_leaves_out_users_without_sets(uncached: SoundcloudMusicProvider) -> None:
    """A user without playlists or albums is left out, as the folder would be empty."""
    uncached._soundcloud.get_following.return_value = {
        "collection": [
            _user_obj(1, "empty", playlist_count=0),
            _user_obj(2, "curator", playlist_count=3),
            _user_obj(3, "unknown"),
        ],
    }

    result = await uncached.browse(f"{BASE}following")

    # a user whose count is missing is kept, rather than hidden on a guess
    assert [item.name for item in result] == ["Curator", "Unknown"]


async def test_user_folder_lists_playlists_and_albums_newest_first(
    uncached: SoundcloudMusicProvider,
) -> None:
    """A user's playlists and albums are merged into one list, newest first."""
    uncached._soundcloud.get_following.return_value = {"collection": [_user_obj(42)]}
    uncached._soundcloud.get_playlists_from_user.return_value = {
        "collection": [_playlist_obj(1, "Old Mix", "2020-01-01T00:00:00Z"), {"id": 3}]
    }
    uncached._soundcloud.get_album_from_user.return_value = {
        "collection": [_playlist_obj(2, "New Album", "2024-01-01T00:00:00Z")]
    }

    result = await uncached.browse(f"{BASE}following/muloka")

    # the set without a title can not be parsed and is skipped
    assert [item.name for item in result] == ["New Album", "Old Mix"]
    uncached._soundcloud.get_playlists_from_user.assert_awaited_once_with("42", USER_SETS_LIMIT)
    uncached._soundcloud.get_album_from_user.assert_awaited_once_with("42", USER_SETS_LIMIT)


async def test_numeric_user_id_is_accepted(uncached: SoundcloudMusicProvider) -> None:
    """A numeric user id in the path works for a user the account does not follow."""
    uncached._soundcloud.get_following.return_value = {"collection": []}
    uncached._soundcloud.get_playlists_from_user.return_value = {
        "collection": [_playlist_obj(1, "Burning Man 2024")]
    }
    uncached._soundcloud.get_album_from_user.return_value = {"collection": []}

    result = await uncached.browse(f"{BASE}following/999")

    assert [item.name for item in result] == ["Burning Man 2024"]
    uncached._soundcloud.get_playlists_from_user.assert_awaited_once_with("999", USER_SETS_LIMIT)


async def test_unknown_permalink_is_not_found(uncached: SoundcloudMusicProvider) -> None:
    """A permalink of a user the account does not follow can not be resolved."""
    uncached._soundcloud.get_following.return_value = {"collection": [_user_obj(42)]}

    with pytest.raises(MediaNotFoundError):
        await uncached.browse(f"{BASE}following/someone-else")


async def test_unknown_user_is_not_found(uncached: SoundcloudMusicProvider) -> None:
    """A user Soundcloud does not know answers with a 404 error object."""
    error = {"errors": [{"error_message": "404 - Not Found"}]}
    uncached._soundcloud.get_following.return_value = {"collection": []}
    uncached._soundcloud.get_playlists_from_user.return_value = error
    uncached._soundcloud.get_album_from_user.return_value = error

    with pytest.raises(MediaNotFoundError):
        await uncached.browse(f"{BASE}following/999")


async def test_following_is_paged_through(uncached: SoundcloudMusicProvider) -> None:
    """The followed users of every page are listed, not just those of the first."""
    next_href = "https://api-v2.soundcloud.com/users/1/followings?offset=200"
    uncached._soundcloud.get_following.return_value = {
        "collection": [_user_obj(1, "first", playlist_count=0)],
        "next_href": next_href,
    }
    uncached._soundcloud.get.return_value = {
        "collection": [_user_obj(2, "second", playlist_count=2)],
        "next_href": None,
    }

    result = await uncached.browse(f"{BASE}following")

    # the only user with sets is on the second page
    assert [item.name for item in result] == ["Second"]
    uncached._soundcloud.get.assert_awaited_once_with(
        next_href, headers=uncached._soundcloud.headers
    )


@pytest.mark.parametrize(
    "error",
    [{"errors": [{"error_message": "401 - Unauthorized"}]}, {}, None],
)
async def test_error_answer_raises_instead_of_listing_nothing(
    uncached: SoundcloudMusicProvider, error: Any
) -> None:
    """An error answer raises, so it is not cached as an empty listing."""
    uncached._soundcloud.get_following.return_value = error

    with pytest.raises(InvalidDataError):
        await uncached.browse(f"{BASE}following")


async def test_error_answer_for_a_users_sets_raises(uncached: SoundcloudMusicProvider) -> None:
    """An error answer for a user's albums raises, rather than listing only the playlists."""
    uncached._soundcloud.get_following.return_value = {"collection": []}
    uncached._soundcloud.get_playlists_from_user.return_value = {
        "collection": [_playlist_obj(1, "Mix")]
    }
    uncached._soundcloud.get_album_from_user.return_value = {
        "errors": [{"error_message": "401 - Unauthorized"}]
    }

    with pytest.raises(InvalidDataError):
        await uncached.browse(f"{BASE}following/999")


async def test_path_below_a_user_is_rejected(provider: SoundcloudMusicProvider) -> None:
    """A path deeper than a user's folder raises an error."""
    with pytest.raises(InvalidDataError):
        await provider.browse(f"{BASE}following/muloka/playlists")
