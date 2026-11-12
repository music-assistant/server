"""
Tests that the music controller keeps a restricted member from explicit content.

The library and the shared caches keep everything: the member's setting narrows library views
and counts in SQL, cached listings after the cache read, and search, browse, recommendations
and recently played as they are handed out.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ListingType, MediaType, ProviderFeature
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaItemMetadata,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_MEDIA_PROGRESS
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.listing import apply_listing
from music_assistant.controllers.music.listing_cache import Listing, cached_listing
from music_assistant.controllers.music.recommendations.controller import (
    RecommendationsController,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

PROVIDER = "qobuz_1"
BROWSE_PATH = f"{PROVIDER}://music"
KID = User(user_id="kid", username="kid", role=UserRole.USER, allow_explicit=False)
PARENT = User(user_id="parent", username="parent", role=UserRole.USER)
# every module that asks who the calling user is on the paths under test
CURRENT_USER_LOOKUPS = (
    "music_assistant.controllers.music.media.base.get_current_user",
    "music_assistant.controllers.music.listing.get_current_user",
    "music_assistant.controllers.music.listing_cache.get_current_user",
    "music_assistant.controllers.music.controller.get_current_user",
    "music_assistant.controllers.music.recommendations.controller.get_current_user",
)


@contextmanager
def _as(user: User) -> Iterator[None]:
    """Run the block as the given calling user."""
    with ExitStack() as stack:
        for target in CURRENT_USER_LOOKUPS:
            stack.enter_context(patch(target, return_value=user))
        yield


def _mapping(item_id: str) -> ProviderMapping:
    """Return the mapping of an item in the library of the test provider."""
    return ProviderMapping(
        item_id=item_id, provider_domain="qobuz", provider_instance=PROVIDER, in_library=True
    )


def _track(name: str, explicit: bool | None) -> Track:
    """Return a provider track with the given explicit flag."""
    return Track(
        item_id=name,
        provider=PROVIDER,
        name=name,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={_mapping(name)},
    )


def _album(name: str, explicit: bool | None) -> Album:
    """Return a provider album with the given explicit flag."""
    return Album(
        item_id=name,
        provider=PROVIDER,
        name=name,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings={_mapping(name)},
    )


def _tracks() -> list[Track]:
    """Return a clean, an explicit and an unflagged track."""
    return [_track("clean", False), _track("explicit", True), _track("unflagged", None)]


EVERYTHING = ["clean", "explicit", "unflagged"]
ALLOWED = ["clean", "unflagged"]


@pytest.fixture(name="mass")
def mass_fixture(
    music_mass_with_cache: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> MusicAssistant:
    """Run on a library instance with a cache store and the test provider visible to all."""
    monkeypatch.setattr(
        music_mass_with_cache.music, "get_active_provider_instances", lambda: [PROVIDER]
    )
    set_music_source_access(music_mass_with_cache, {PROVIDER: None})
    return music_mass_with_cache


async def _artist(mass: MusicAssistant) -> Artist:
    """Add the artist the library items are credited to."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="artist", provider=PROVIDER, name="Artist", provider_mappings={_mapping("a")}
        )
    )


async def _add_library_tracks(mass: MusicAssistant) -> dict[str, int]:
    """Add the test tracks to the library and return their library ids by name."""
    artist = await _artist(mass)
    ids = {}
    for track in _tracks():
        track.artists = UniqueList([artist])
        ids[track.name] = int((await mass.music.tracks.add_item_to_library(track)).item_id)
    return ids


@pytest.mark.parametrize(("user", "expected"), [(PARENT, EVERYTHING), (KID, ALLOWED)])
async def test_library_tracks_count_and_search(
    mass: MusicAssistant, user: User, expected: list[str]
) -> None:
    """The library view, its count and a library search leave the explicit tracks out."""
    await _add_library_tracks(mass)

    with _as(user):
        items = await mass.music.tracks.library_items()
        count = await mass.music.tracks.library_count()
        found = await mass.music.tracks.library_items(search="explicit")

    assert sorted(track.name for track in items) == expected
    assert count == len(expected)
    assert [track.name for track in found] == (["explicit"] if "explicit" in expected else [])


async def test_library_albums(mass: MusicAssistant) -> None:
    """An album marked explicit is left out of a restricted member's library."""
    artist = await _artist(mass)
    for album in (_album("clean album", False), _album("explicit album", True)):
        album.artists = UniqueList([artist])
        await mass.music.albums.add_item_to_library(album)

    with _as(KID):
        items = await mass.music.albums.library_items()

    assert [album.name for album in items] == ["clean album"]


async def test_an_explicit_audiobook_is_not_found_by_its_author(mass: MusicAssistant) -> None:
    """The author search of the audiobook library leaves the explicit audiobooks out too."""
    for name, explicit in (("Clean Book", False), ("Explicit Book", True)):
        await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id=name,
                provider=PROVIDER,
                name=name,
                authors=UniqueList(["Jane Writer"]),
                metadata=MediaItemMetadata(explicit=explicit),
                provider_mappings={_mapping(name)},
            )
        )

    with _as(KID):
        found = await mass.music.audiobooks.library_items(search="Jane Writer")

    assert [book.name for book in found] == ["Clean Book"]


async def test_internal_library_lookups_are_not_narrowed(mass: MusicAssistant) -> None:
    """The lookups the sync and matching rely on find every item, whoever is asking."""
    await _add_library_tracks(mass)

    with _as(KID):
        found = await mass.music.tracks.get_library_item_by_prov_id("explicit", PROVIDER)
        everything = await mass.music.tracks.get_library_items_by_query(limit=10)

    assert found is not None
    assert sorted(track.name for track in everything) == EVERYTHING


def test_an_in_memory_listing_is_filtered_before_paging() -> None:
    """The explicit items are left out first, so a page is still full."""
    with _as(KID):
        page = apply_listing(_tracks(), ListingType.ALBUM_TRACKS, limit=2)
        rest = apply_listing(_tracks(), ListingType.ALBUM_TRACKS, offset=2)

    assert [track.name for track in page] == ALLOWED
    assert rest == []


async def test_a_cached_listing_is_filtered_after_the_cache_read(mass: MusicAssistant) -> None:
    """A listing is assembled and cached whole, and narrowed for each member reading it."""
    assemble = AsyncMock(return_value=Listing(_tracks()))

    async def _read(user: User) -> list[str]:
        with _as(user):
            items = await cached_listing(
                mass,
                ListingType.ALBUM_TRACKS,
                f"{PROVIDER}://album/1",
                assemble,
                item_type=Track,
            )
        return [track.name for track in items]

    assert await _read(KID) == ALLOWED
    # the cache was not narrowed for the member who filled it
    assert await _read(PARENT) == EVERYTHING
    assemble.assert_awaited_once()


async def test_search_results(mass: MusicAssistant) -> None:
    """Search results leave the explicit tracks and albums out."""
    results = SearchResults(
        tracks=_tracks(),
        albums=[_album("clean album", False), _album("explicit album", True)],
    )

    with _as(KID):
        visible = await mass.music._visible_search_results(results)

    assert [track.name for track in visible.tracks] == ALLOWED
    assert [album.name for album in visible.albums] == ["clean album"]


@pytest.mark.parametrize("streaming", [True, False], ids=["cached", "read_every_time"])
async def test_browse(mass: MusicAssistant, streaming: bool) -> None:
    """A browsed folder leaves the explicit items out, whether it is cached or read anew."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = PROVIDER
    provider.name = "Qobuz"
    provider.is_streaming_provider = streaming
    provider.browse = AsyncMock(return_value=_tracks())

    with patch.object(mass, "get_provider", return_value=provider), _as(KID):
        items = await mass.music.browse(BROWSE_PATH)

    assert [item.name for item in items if item.name != ".."] == ALLOWED


def _recommendations() -> RecommendationsController:
    """Return a recommendations controller over one provider with a row of the test tracks."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = PROVIDER
    provider.domain = "qobuz"
    provider.supported_features = {ProviderFeature.RECOMMENDATIONS}
    provider.get_recommendations = AsyncMock(
        side_effect=lambda: [
            RecommendationFolder(
                item_id="row", provider=PROVIDER, name="Row", items=UniqueList(_tracks())
            )
        ]
    )
    provider.get_recommendation_items = AsyncMock(side_effect=lambda _id: UniqueList(_tracks()))
    music = MagicMock(spec=MusicController)
    music.get_visible_provider.return_value = provider
    music._apply_user_provider_filter.side_effect = lambda providers: providers
    stand_in = MagicMock()
    stand_in.music = music
    stand_in.get_providers_supporting_feature.return_value = [provider]
    set_music_source_access(stand_in, {PROVIDER: None})
    return RecommendationsController(stand_in)


@pytest.mark.parametrize(("user", "expected"), [(PARENT, EVERYTHING), (KID, ALLOWED)])
async def test_recommendations(user: User, expected: list[str]) -> None:
    """Recommendation rows and their items leave the explicit items out."""
    controller = _recommendations()

    with _as(user):
        rows = await controller.get_recommendations()
        items = await controller.get_recommendation_items(PROVIDER, "row")

    assert [item.name for item in rows[0].items] == expected
    assert [item.name for item in items] == expected


async def test_recently_played(mass: MusicAssistant) -> None:
    """Recently played library tracks are judged by their library item."""
    library_ids = await _add_library_tracks(mass)
    for timestamp, (name, library_id) in enumerate(library_ids.items(), start=1):
        await mass.music.database.insert(
            DB_TABLE_MEDIA_PROGRESS,
            {
                "item_id": str(library_id),
                "provider": "library",
                "media_type": MediaType.TRACK.value,
                "name": name,
                "timestamp": timestamp,
                "fully_played": True,
                "seconds_played": 180,
                "userid": KID.user_id,
                "user_initiated": True,
            },
        )

    with _as(KID):
        played = await mass.music.recently_played(media_types=[MediaType.TRACK])

    assert sorted(item.name for item in played) == ALLOWED
