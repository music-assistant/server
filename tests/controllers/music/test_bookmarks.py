"""Tests for the user's bookmarks: where each is stored and which copies list it."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import EventType, MediaType, ProviderFeature, ProviderSharing
from music_assistant_models.errors import InsufficientPermissions, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    MediaItemBookmark,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
)

from music_assistant.constants import DB_TABLE_BOOKMARKS
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ABS = "audiobookshelf--abs"
LOCAL = "filesystem_local--nas"
LISTENER = User(user_id="listener", username="listener", role=UserRole.USER)
EXACT_PROVIDER = "music_assistant.controllers.music.bookmarks.exact_provider"
AUDIOBOOK = MediaType.AUDIOBOOK
EPISODE = MediaType.PODCAST_EPISODE
READ = ProviderFeature.AUDIOBOOK_BOOKMARKS
EDIT = ProviderFeature.AUDIOBOOK_BOOKMARKS_EDIT


def _provider(features: set[ProviderFeature]) -> Mock:
    """Build a provider with the given bookmark features, keeping its bookmarks in a dict."""
    stored: dict[float, str] = {}
    provider = Mock(spec=MusicProvider)
    provider.instance_id = ABS
    provider.supports_feature.side_effect = lambda feature: feature in features
    provider.get_bookmarks = AsyncMock(
        side_effect=lambda _media_type, item_id: [
            MediaItemBookmark(
                name=name,
                start=start,
                provider_instance_id=ABS,
                provider_item_id=item_id,
            )
            for start, name in stored.items()
        ]
    )
    provider.set_bookmark = AsyncMock(
        side_effect=lambda _media_type, _item_id, start, name: stored.__setitem__(start, name)
    )
    provider.get_podcast_episode = AsyncMock(
        return_value=PodcastEpisode(
            item_id="episode1",
            provider=ABS,
            name="Episode",
            provider_mappings={
                ProviderMapping(
                    item_id="episode1", provider_domain="audiobookshelf", provider_instance=ABS
                )
            },
            position=1,
            podcast=ItemMapping(
                media_type=MediaType.PODCAST, item_id="show1", provider=ABS, name="Show"
            ),
        )
    )
    return provider


async def _library_book(mass: MusicAssistant) -> Audiobook:
    return await mass.music.audiobooks.add_item_to_library(
        Audiobook(
            item_id="0",
            provider="library",
            name="Dune",
            provider_mappings={
                ProviderMapping(
                    item_id="book1", provider_domain="audiobookshelf", provider_instance=ABS
                ),
                ProviderMapping(
                    item_id="dune.m4b", provider_domain="filesystem_local", provider_instance=LOCAL
                ),
            },
        )
    )


@pytest.mark.parametrize(
    ("features", "write_target", "stored_by_music_assistant"),
    [
        ({READ, EDIT}, True, False),
        ({READ, EDIT}, False, None),
        ({READ}, True, True),
        (set(), True, True),
    ],
)
async def test_bookmark_goes_to_the_provider_only_when_it_may_edit(
    music_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    features: set[ProviderFeature],
    write_target: bool,
    stored_by_music_assistant: bool | None,
) -> None:
    """
    A provider that can edit bookmarks keeps them, MA keeps them for any other provider.

    An account of such a provider the user may not write to takes none, not even through MA.
    """
    set_current_user(LISTENER)
    providers = {ABS: _provider(features), LOCAL: _provider(set())}
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, instance_id: providers[instance_id])
    monkeypatch.setattr(
        music_mass.music,
        "_write_target_mappings",
        lambda mappings: list(mappings) if write_target else [],
    )
    book = await _library_book(music_mass)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        music_mass,
        "signal_event",
        lambda event, object_id=None, data=None: (
            events.append((object_id, data)) if event == EventType.BOOKMARKS_UPDATED else None
        ),
    )

    refused = stored_by_music_assistant is None

    with pytest.raises(InsufficientPermissions) if refused else nullcontext():
        await music_mass.music.bookmarks.set_bookmark(AUDIOBOOK, ABS, "book1", 754.5, "Arrakis")

    # the event names the copy and the user whose bookmarks changed
    assert events == ([] if refused else [(f"{ABS}://audiobook/book1", LISTENER.user_id)])

    assert await music_mass.music.bookmarks.bookmarks(AUDIOBOOK, book.item_id, "library") == (
        []
        if refused
        else [
            MediaItemBookmark(
                name="Arrakis",
                start=754.5,
                provider_instance_id=ABS,
                provider_item_id="book1",
                stored_by_music_assistant=bool(stored_by_music_assistant),
            )
        ]
    )


async def test_bookmarks_stored_by_music_assistant(
    music_mass_with_audio_analysis: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MA keeps bookmarks per user and copy, and drops them with the copy."""
    mass = music_mass_with_audio_analysis
    set_current_user(LISTENER)
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, _instance_id: _provider(set()))
    book = await _library_book(mass)
    audiobooks = mass.music.audiobooks
    bookmarks = mass.music.bookmarks

    await bookmarks.set_bookmark(AUDIOBOOK, LOCAL, "dune.m4b", 90.0, "Later")
    await bookmarks.set_bookmark(AUDIOBOOK, LOCAL, "dune.m4b", 30.0, "Typo")
    await bookmarks.set_bookmark(AUDIOBOOK, LOCAL, "dune.m4b", 30.0, "Earlier")
    await bookmarks.set_bookmark(AUDIOBOOK, LOCAL, "dune.m4b", 60.0, "Removed")
    await bookmarks.set_bookmark(AUDIOBOOK, ABS, "book1", 30.0, "Other copy")
    await bookmarks.remove_bookmark(AUDIOBOOK, LOCAL, "dune.m4b", 60.0)
    with pytest.raises(MediaNotFoundError):
        await bookmarks.remove_bookmark(AUDIOBOOK, LOCAL, "dune.m4b", 60.0)

    listed = [
        (x.provider_instance_id, x.start, x.name)
        for x in await bookmarks.bookmarks(AUDIOBOOK, book.item_id, "library")
    ]
    assert listed == [
        (ABS, 30.0, "Other copy"),
        (LOCAL, 30.0, "Earlier"),
        (LOCAL, 90.0, "Later"),
    ]

    set_current_user(User(user_id="other", username="other", role=UserRole.USER))
    assert await bookmarks.bookmarks(AUDIOBOOK, book.item_id, "library") == []

    set_current_user(LISTENER)
    await audiobooks.remove_provider_mapping(book.item_id, LOCAL, "dune.m4b")
    await mass.music._cleanup_database()
    await audiobooks.add_provider_mappings(
        book.item_id,
        [
            ProviderMapping(
                item_id="dune.m4b", provider_domain="filesystem_local", provider_instance=LOCAL
            )
        ],
    )
    listed_names = [x.name for x in await bookmarks.bookmarks(AUDIOBOOK, book.item_id, "library")]
    assert listed_names == ["Other copy"]


async def test_episode_bookmarks_leave_with_the_podcast(
    music_mass_with_audio_analysis: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An episode is bookmarked only in a library podcast, and loses its bookmarks with it."""
    mass = music_mass_with_audio_analysis
    set_current_user(LISTENER)
    provider = _provider(set())
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, _instance_id: provider)
    monkeypatch.setattr(mass, "get_provider", lambda *_args, **_kwargs: provider)
    bookmarks = mass.music.bookmarks
    with pytest.raises(MediaNotFoundError):
        await bookmarks.set_bookmark(EPISODE, ABS, "episode1", 30.0, "Not in the library")
    podcast = await mass.music.podcasts.add_item_to_library(
        Podcast(
            item_id="0",
            provider="library",
            name="Show",
            provider_mappings={
                ProviderMapping(
                    item_id="show1", provider_domain="audiobookshelf", provider_instance=ABS
                )
            },
        )
    )

    await bookmarks.set_bookmark(EPISODE, ABS, "episode1", 30.0, "Intro")
    await bookmarks.set_bookmark(EPISODE, ABS, "episode1", 60.0, "Removed")
    await bookmarks.remove_bookmark(EPISODE, ABS, "episode1", 60.0)
    assert [x.name for x in await bookmarks.bookmarks(EPISODE, "episode1", ABS)] == ["Intro"]

    await mass.music.podcasts.remove_provider_mapping(podcast.item_id, ABS, "show1")
    await mass.music._cleanup_database()
    assert await bookmarks.bookmarks(EPISODE, "episode1", ABS) == []


async def test_bookmarks_of_a_hidden_copy(
    music_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy on a source the user may not see is neither listed nor bookmarked."""
    set_current_user(LISTENER)
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, _instance_id: _provider(set()))
    book = await _library_book(music_mass)
    bookmarks = music_mass.music.bookmarks
    await bookmarks.set_bookmark(AUDIOBOOK, ABS, "book1", 30.0, "Before hiding")
    set_music_source_access(
        music_mass,
        {ABS: ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE), LOCAL: None},
    )

    assert await bookmarks.bookmarks(AUDIOBOOK, book.item_id, "library") == []
    with pytest.raises(MediaNotFoundError):
        await bookmarks.set_bookmark(AUDIOBOOK, ABS, "book1", 60.0, "Hidden")


@pytest.mark.parametrize(("write_target", "refused"), [(True, False), (False, True)])
async def test_provider_keeps_bookmarks_of_a_copy_outside_the_library(
    music_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    write_target: bool,
    refused: bool,
) -> None:
    """A provider that can edit bookmarks takes them for any copy of an account the user owns."""
    set_current_user(LISTENER)
    provider = _provider({READ, EDIT})
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, _instance_id: provider)
    monkeypatch.setattr(
        music_mass.music,
        "_write_target_mappings",
        lambda mappings: list(mappings) if write_target else [],
    )

    with pytest.raises(InsufficientPermissions) if refused else nullcontext():
        await music_mass.music.bookmarks.set_bookmark(AUDIOBOOK, ABS, "book1", 754.5, "Arrakis")

    assert provider.set_bookmark.await_count == (0 if refused else 1)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        # the unavailable account lists its own stored bookmarks, never another account's
        (ABS, [(ABS, "Mine")]),
        # a domain names any of its accounts, the available one first
        ("audiobookshelf", [("audiobookshelf--other", "Other account")]),
    ],
)
async def test_episode_bookmarks_of_an_unavailable_account(
    music_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    expected: list[tuple[str, str]],
) -> None:
    """An episode lists the bookmarks of the account it names, available or not."""
    set_current_user(LISTENER)
    other_account = _provider({ProviderFeature.PODCAST_EPISODE_BOOKMARKS})
    other_account.instance_id = "audiobookshelf--other"
    other_account.get_bookmarks = AsyncMock(
        return_value=[
            MediaItemBookmark(
                name="Other account",
                start=10.0,
                provider_instance_id=other_account.instance_id,
                provider_item_id="episode1",
            )
        ]
    )
    unavailable = _provider(set())

    def get_provider(instance_id_or_domain: str, return_unavailable: bool = False) -> Mock:
        # like a streaming provider: an unavailable instance widens to another of its domain
        if return_unavailable and instance_id_or_domain in (ABS, "audiobookshelf"):
            return unavailable
        return other_account

    monkeypatch.setattr(music_mass, "get_provider", get_provider)
    monkeypatch.setattr(
        EXACT_PROVIDER,
        lambda _mass, instance_id: (
            other_account if instance_id == other_account.instance_id else None
        ),
    )
    await music_mass.music.database.insert(
        DB_TABLE_BOOKMARKS,
        {
            "user_id": LISTENER.user_id,
            "media_type": EPISODE,
            "provider_instance": ABS,
            "provider_item_id": "episode1",
            "podcast_item_id": "show1",
            "start": 30.0,
            "name": "Mine",
        },
    )

    listed = [
        (x.provider_instance_id, x.name)
        for x in await music_mass.music.bookmarks.bookmarks(EPISODE, "episode1", requested)
    ]

    assert listed == expected
