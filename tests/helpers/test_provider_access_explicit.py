"""Tests for the rule that keeps a member who may not get explicit content from it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    ItemMapping,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
)

from music_assistant.helpers.provider_access import (
    ensure_explicit_allowed,
    explicit_allowed,
    explicit_allowed_by_flag,
    restricts_explicit,
    without_disallowed_explicit,
)

PROVIDER = "deezer--member"
KID = User(user_id="kid", username="kid", role=UserRole.USER, allow_explicit=False)
PARENT = User(user_id="parent", username="parent", role=UserRole.USER)


def _mappings(item_id: str) -> set[ProviderMapping]:
    """Return the provider mapping of an item on the test provider."""
    return {ProviderMapping(item_id=item_id, provider_domain="deezer", provider_instance=PROVIDER)}


def _track(item_id: str, explicit: bool | None) -> Track:
    """Return a track with the given explicit flag."""
    return Track(
        item_id=item_id,
        provider=PROVIDER,
        name=item_id,
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings=_mappings(item_id),
    )


def _podcast(explicit: bool | None) -> Podcast:
    """Return a podcast with the given explicit flag."""
    return Podcast(
        item_id="show",
        provider=PROVIDER,
        name="Show",
        metadata=MediaItemMetadata(explicit=explicit),
        provider_mappings=_mappings("show"),
    )


def _episode(item_id: str, podcast: Podcast | ItemMapping) -> PodcastEpisode:
    """Return an episode of the given podcast."""
    return PodcastEpisode(
        item_id=item_id,
        provider=PROVIDER,
        name=item_id,
        podcast=podcast,
        position=1,
        duration=600,
        provider_mappings=_mappings(item_id),
    )


PODCAST_MAPPING = ItemMapping(
    media_type=MediaType.PODCAST, item_id="show", provider=PROVIDER, name="Show"
)
CLEAN = _track("clean", False)
UNFLAGGED = _track("unflagged", None)
EXPLICIT = _track("explicit", True)


def _mass(podcast: Podcast | None = None, library_track: Track | None = None) -> MagicMock:
    """Return a server whose music controller returns the given podcast and library track."""
    mass = MagicMock()
    mass.music.podcasts.get = AsyncMock(return_value=podcast)
    mass.music.get_controller.return_value.get_library_item = AsyncMock(return_value=library_track)
    return mass


def test_only_a_member_without_allow_explicit_is_restricted() -> None:
    """Anonymous use, and everyone allowed, which is every user by default, is not restricted."""
    assert restricts_explicit(KID)
    assert not restricts_explicit(PARENT)
    assert not restricts_explicit(None)


@pytest.mark.parametrize(
    "item",
    [
        EXPLICIT,
        Album(
            item_id="album",
            provider=PROVIDER,
            name="Album",
            metadata=MediaItemMetadata(explicit=True),
            provider_mappings=_mappings("album"),
        ),
        _podcast(True),
        Audiobook(
            item_id="book",
            provider=PROVIDER,
            name="Book",
            metadata=MediaItemMetadata(explicit=True),
            provider_mappings=_mappings("book"),
        ),
    ],
    ids=["track", "album", "podcast", "audiobook"],
)
async def test_explicit_items_by_their_own_flag(item: object) -> None:
    """Tracks, albums, podcasts and audiobooks marked explicit are kept from the member only."""
    assert not await explicit_allowed(_mass(), item, KID)
    assert not explicit_allowed_by_flag(item, KID)
    assert await explicit_allowed(_mass(), item, PARENT)
    assert await explicit_allowed(_mass(), item, None)


async def test_unflagged_and_clean_items_are_allowed() -> None:
    """An item without a flag counts as allowed, as does one marked clean."""
    assert await explicit_allowed(_mass(), CLEAN, KID)
    assert await explicit_allowed(_mass(), UNFLAGGED, KID)


async def test_an_artist_is_never_kept_from_the_member() -> None:
    """Artists carry no flag of their own."""
    artist = Artist(
        item_id="artist",
        provider=PROVIDER,
        name="Artist",
        metadata=MediaItemMetadata(explicit=True),
        provider_mappings=_mappings("artist"),
    )
    assert await explicit_allowed(_mass(), artist, KID)


async def test_an_episode_follows_its_full_podcast() -> None:
    """An episode carrying its podcast is judged by it, without a look up."""
    mass = _mass()

    assert not await explicit_allowed(mass, _episode("ep", _podcast(True)), KID)
    assert await explicit_allowed(mass, _episode("ep", _podcast(False)), KID)
    mass.music.podcasts.get.assert_not_awaited()


@pytest.mark.parametrize(("podcast_explicit", "allowed"), [(True, False), (False, True)])
async def test_an_episode_looks_its_podcast_up(podcast_explicit: bool, allowed: bool) -> None:
    """An episode that only maps its podcast is judged by the podcast the library returns."""
    mass = _mass(podcast=_podcast(podcast_explicit))

    assert await explicit_allowed(mass, _episode("ep", PODCAST_MAPPING), KID) is allowed
    mass.music.podcasts.get.assert_awaited_once_with("show", PROVIDER)


async def test_a_listing_looks_each_podcast_up_once() -> None:
    """The episodes of one podcast in a listing share a single look up."""
    mass = _mass(podcast=_podcast(True))
    episodes = [_episode(f"ep{index}", PODCAST_MAPPING) for index in range(3)]

    assert await without_disallowed_explicit(mass, episodes, KID) == []
    mass.music.podcasts.get.assert_awaited_once()


async def test_an_unreachable_podcast_keeps_its_episodes_allowed() -> None:
    """A podcast that can not be looked up does not keep its episodes from the member."""
    mass = _mass()
    mass.music.podcasts.get = AsyncMock(side_effect=MediaNotFoundError("gone"))

    assert await explicit_allowed(mass, _episode("ep", PODCAST_MAPPING), KID)


async def test_a_library_reference_is_judged_by_its_library_item() -> None:
    """A recently played library entry is looked up, any other reference counts as allowed."""
    mass = _mass(library_track=EXPLICIT)
    library_ref = ItemMapping(media_type=MediaType.TRACK, item_id="7", provider="library", name="x")
    provider_ref = ItemMapping(media_type=MediaType.TRACK, item_id="7", provider=PROVIDER, name="x")

    assert not await explicit_allowed(mass, library_ref, KID)
    assert await explicit_allowed(mass, provider_ref, KID)
    # without a look up, a reference counts as allowed
    assert explicit_allowed_by_flag(library_ref, KID)


async def test_without_disallowed_explicit_keeps_the_order() -> None:
    """Only the explicit items are left out, the rest keep their order."""
    items = [CLEAN, EXPLICIT, UNFLAGGED]

    assert await without_disallowed_explicit(_mass(), items, KID) == [CLEAN, UNFLAGGED]
    assert await without_disallowed_explicit(_mass(), items, PARENT) == items


async def test_ensure_explicit_allowed_refuses_with_its_own_message() -> None:
    """Playing an explicit item as a restricted member is refused with a clear error."""
    with pytest.raises(MediaNotFoundError) as err:
        await ensure_explicit_allowed(_mass(), EXPLICIT, KID)

    assert err.value.translation_key == "media_explicit_not_allowed"
    await ensure_explicit_allowed(_mass(), EXPLICIT, PARENT)
    await ensure_explicit_allowed(_mass(), CLEAN, KID)
