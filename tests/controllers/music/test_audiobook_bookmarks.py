"""Tests for the user's audiobook bookmarks: where each is stored and which copies list it."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import ProviderFeature, ProviderSharing
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Audiobook, MediaItemBookmark, ProviderMapping

from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ABS = "audiobookshelf--abs"
LOCAL = "filesystem_local--nas"
LISTENER = User(user_id="listener", username="listener", role=UserRole.USER)
EXACT_PROVIDER = "music_assistant.controllers.music.media.audiobooks.exact_provider"
READ = ProviderFeature.AUDIOBOOK_BOOKMARKS
EDIT = ProviderFeature.AUDIOBOOK_BOOKMARKS_EDIT


def _provider(features: set[ProviderFeature]) -> Mock:
    """Build a provider with the given bookmark features, keeping its bookmarks in a dict."""
    stored: dict[float, str] = {}
    provider = Mock(spec=MusicProvider)
    provider.supports_feature.side_effect = lambda feature: feature in features
    provider.get_audiobook_bookmarks = AsyncMock(
        side_effect=lambda item_id: [
            MediaItemBookmark(
                name=name,
                start=start,
                provider_instance_id=ABS,
                provider_item_id=item_id,
            )
            for start, name in stored.items()
        ]
    )
    provider.set_audiobook_bookmark = AsyncMock(
        side_effect=lambda _item_id, start, name: stored.__setitem__(start, name)
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
        ({READ, EDIT}, False, True),
        ({READ}, True, True),
        (set(), True, True),
    ],
)
async def test_bookmark_goes_to_the_provider_only_when_it_may_edit(
    music_mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    features: set[ProviderFeature],
    write_target: bool,
    stored_by_music_assistant: bool,
) -> None:
    """A provider keeps a bookmark if it can edit them for this user, otherwise MA does."""
    set_current_user(LISTENER)
    providers = {ABS: _provider(features), LOCAL: _provider(set())}
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, instance_id: providers[instance_id])
    monkeypatch.setattr(
        music_mass.music,
        "_write_target_mappings",
        lambda mappings: list(mappings) if write_target else [],
    )
    book = await _library_book(music_mass)

    await music_mass.music.audiobooks.set_bookmark(ABS, "book1", 754.5, "Arrakis")

    assert await music_mass.music.audiobooks.bookmarks(book.item_id, "library") == [
        MediaItemBookmark(
            name="Arrakis",
            start=754.5,
            provider_instance_id=ABS,
            provider_item_id="book1",
            stored_by_music_assistant=stored_by_music_assistant,
        )
    ]


async def test_bookmarks_stored_by_music_assistant(
    music_mass_with_audio_analysis: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MA keeps bookmarks per user and copy, and drops them with the copy."""
    mass = music_mass_with_audio_analysis
    set_current_user(LISTENER)
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, _instance_id: _provider(set()))
    book = await _library_book(mass)
    audiobooks = mass.music.audiobooks

    await audiobooks.set_bookmark(LOCAL, "dune.m4b", 90.0, "Later")
    await audiobooks.set_bookmark(LOCAL, "dune.m4b", 30.0, "Typo")
    await audiobooks.set_bookmark(LOCAL, "dune.m4b", 30.0, "Earlier")
    await audiobooks.set_bookmark(LOCAL, "dune.m4b", 60.0, "Removed")
    await audiobooks.set_bookmark(ABS, "book1", 30.0, "Other copy")
    await audiobooks.remove_bookmark(LOCAL, "dune.m4b", 60.0)
    with pytest.raises(MediaNotFoundError):
        await audiobooks.remove_bookmark(LOCAL, "dune.m4b", 60.0)

    listed = [
        (x.provider_instance_id, x.start, x.name)
        for x in await audiobooks.bookmarks(book.item_id, "library")
    ]
    assert listed == [
        (ABS, 30.0, "Other copy"),
        (LOCAL, 30.0, "Earlier"),
        (LOCAL, 90.0, "Later"),
    ]

    set_current_user(User(user_id="other", username="other", role=UserRole.USER))
    assert await audiobooks.bookmarks(book.item_id, "library") == []

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
    assert [x.name for x in await audiobooks.bookmarks(book.item_id, "library")] == ["Other copy"]


async def test_bookmarks_of_a_hidden_copy(
    music_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy on a source the user may not see is neither listed nor bookmarked."""
    set_current_user(LISTENER)
    monkeypatch.setattr(EXACT_PROVIDER, lambda _mass, _instance_id: _provider(set()))
    book = await _library_book(music_mass)
    await music_mass.music.audiobooks.set_bookmark(ABS, "book1", 30.0, "Before hiding")
    set_music_source_access(
        music_mass,
        {ABS: ProviderAccess(owner="other", sharing=ProviderSharing.PRIVATE), LOCAL: None},
    )

    assert await music_mass.music.audiobooks.bookmarks(book.item_id, "library") == []
    with pytest.raises(MediaNotFoundError):
        await music_mass.music.audiobooks.set_bookmark(ABS, "book1", 60.0, "Hidden")
