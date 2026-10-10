"""Tests for how a user's music sources narrow the single-item read commands."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import ExitStack
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    ArtistType,
    ExternalID,
    MediaType,
    ProviderFeature,
    ProviderSharing,
    ProviderType,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaItemCollection,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)

from music_assistant.controllers.music import MusicController
from music_assistant.helpers.collections import get_collection_item_id
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests.common import set_music_source_access

from .helpers import ISRC, create_album, create_track

# the owner's Spotify and the member's Tidal; a Spotify account of the member is added
# where a test needs the member's own account of the owner's service
THEIRS = "spotify_theirs"
MINE = "tidal_mine"
MY_SPOTIFY = "spotify_mine"
SHARED_SPOTIFY = "spotify_shared"
OWNER = User(user_id="user-owner", username="owner", role=UserRole.USER)
MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)
GUEST = User(user_id="user-guest", username="guest", role=UserRole.GUEST)


def _provider(instance_id: str) -> MagicMock:
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance_id
    provider.domain = instance_id.split("_", maxsplit=1)[0]
    provider.type = ProviderType.MUSIC
    provider.available = True
    provider.is_streaming_provider = True
    provider.supported_features = set()
    provider.get_track = AsyncMock(return_value=create_track(instance_id, "t1"))
    provider.get_track_by_external_id = AsyncMock(return_value=None)
    provider.get_playlist_tracks = AsyncMock(return_value=[])
    return provider


def _mock(music: MusicController, instance_id: str) -> MagicMock:
    """Return the mocked provider instance loaded under the given id."""
    return cast("MagicMock", music.mass._providers[instance_id])


def _add_my_spotify(music: MusicController) -> MagicMock:
    """Give the member an account of the owner's service, loaded after the library was seeded."""
    provider = _provider(MY_SPOTIFY)
    music.mass._providers[MY_SPOTIFY] = provider
    set_music_source_access(
        music.mass,
        {MY_SPOTIFY: ProviderAccess(owner=MEMBER.user_id, sharing=ProviderSharing.PRIVATE)},
    )
    return provider


def _add_shared_spotify(music: MusicController) -> MagicMock:
    """Give the home a shared account of the owner's service."""
    provider = _provider(SHARED_SPOTIFY)
    music.mass._providers[SHARED_SPOTIFY] = provider
    set_music_source_access(music.mass, {SHARED_SPOTIFY: None})
    return provider


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller whose server has a private source of each of two members."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    mass_minimal.streams = MagicMock()
    mass_minimal.streams.audio_analysis.delete_audio_analysis = AsyncMock()
    mass_minimal.streams.audio_analysis.get_track_audio_metadata = AsyncMock(return_value=None)
    mass_minimal.metadata = MagicMock()
    mass_minimal.webserver = MagicMock()
    mass_minimal.webserver.auth.get_user = AsyncMock(return_value=None)
    mass_minimal.webserver.auth.list_users = AsyncMock(return_value=[])
    # the minimal server runs no cache database
    mass_minimal.cache.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    mass_minimal.cache.set = AsyncMock()  # type: ignore[method-assign]
    mass_minimal.cache.delete = AsyncMock()  # type: ignore[method-assign]
    await controller._setup_database()
    mass_minimal._providers = {THEIRS: _provider(THEIRS), MINE: _provider(MINE)}
    set_music_source_access(
        mass_minimal,
        {
            THEIRS: ProviderAccess(owner=OWNER.user_id, sharing=ProviderSharing.PRIVATE),
            MINE: ProviderAccess(owner=MEMBER.user_id, sharing=ProviderSharing.PRIVATE),
        },
    )
    yield controller
    if controller._database:
        await controller._database.close()


def _artist(provider_instance: str, item_id: str) -> Artist:
    return Artist(
        item_id=item_id,
        provider=provider_instance,
        name="Test Artist",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_instance.split("_", maxsplit=1)[0],
                provider_instance=provider_instance,
            )
        },
    )


async def _drain(items: AsyncGenerator[object]) -> list[object]:
    return [item async for item in items]


def _as_user(user: User | None) -> ExitStack:
    """Run the enclosed block as the given user (None for an internal caller)."""
    stack = ExitStack()
    for module in ("media.base", "controller"):
        stack.enter_context(
            patch(f"music_assistant.controllers.music.{module}.get_current_user", return_value=user)
        )
    return stack


async def test_resolve_visible_provider(music: MusicController) -> None:
    """A source is served to its owner, swapped for the member's own account, or refused."""
    with _as_user(OWNER):
        assert music.resolve_visible_provider(THEIRS).instance_id == THEIRS
    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        music.resolve_visible_provider(THEIRS)
    with _as_user(GUEST), pytest.raises(ProviderUnavailableError):
        music.resolve_visible_provider("unknown")

    # a shared account of the service stands in for the hidden one...
    _add_shared_spotify(music)
    with _as_user(MEMBER):
        assert music.resolve_visible_provider(THEIRS).instance_id == SHARED_SPOTIFY
    # ...the member's own account comes first, although it was loaded after the shared one...
    _add_my_spotify(music)
    with _as_user(MEMBER):
        assert music.resolve_visible_provider(THEIRS).instance_id == MY_SPOTIFY
        # ...unless exactly the hidden account is required
        with pytest.raises(InsufficientPermissions):
            music.resolve_visible_provider(THEIRS, strict=True)
    # a visible source that is not loaded is unavailable, not forbidden
    _mock(music, MINE).available = False
    with _as_user(MEMBER), pytest.raises(ProviderUnavailableError):
        music.resolve_visible_provider(MINE, strict=True)


async def test_get_serves_a_library_item_only_from_a_visible_source(
    music: MusicController,
) -> None:
    """A library item on a hidden source is fetched from the user's own account instead."""
    library_track = await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    my_spotify = _add_my_spotify(music)

    with _as_user(OWNER):
        assert (await music.tracks.get("t1", THEIRS)).item_id == library_track.item_id
    with _as_user(MEMBER):
        assert (await music.tracks.get("t1", THEIRS)).provider == MY_SPOTIFY
    my_spotify.get_track.assert_awaited_once_with("t1")
    with _as_user(GUEST), pytest.raises(InsufficientPermissions):
        await music.tracks.get("t1", THEIRS)


async def test_get_does_not_fall_back_to_a_library_item_of_a_hidden_source(
    music: MusicController,
) -> None:
    """When the user's own account misses the id, the hidden library item is not handed out."""
    await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    _add_my_spotify(music).get_track.side_effect = MediaNotFoundError("not on this account")

    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        await music.tracks.get("t1", THEIRS)


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda music, item_id: music.tracks.get(item_id, "library"), id="get"),
        pytest.param(
            lambda music, item_id: music.get_item(MediaType.TRACK, item_id, "library"), id="item"
        ),
        pytest.param(
            lambda music, item_id: music.get_item_by_uri(f"library://track/{item_id}"),
            id="item_by_uri",
        ),
    ],
)
async def test_library_reads_hide_an_item_of_a_hidden_source(
    music: MusicController, read: Callable[[MusicController, str], Awaitable[object]]
) -> None:
    """A library item whose only source is hidden is not found when read as a library item."""
    library_track = await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))

    with _as_user(OWNER):
        assert await read(music, library_track.item_id) is not None
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        await read(music, library_track.item_id)


async def test_get_library_item_command_hides_items_of_hidden_sources(
    music: MusicController,
) -> None:
    """The library lookup command only hands out an item on one of the user's sources."""
    library_track = await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    lookup = music.get_library_item_by_prov_id

    with _as_user(OWNER):
        found = await lookup(MediaType.TRACK, "t1", THEIRS)
        assert found is not None
        assert found.item_id == library_track.item_id
    with _as_user(MEMBER):
        assert await lookup(MediaType.TRACK, "t1", THEIRS) is None
    with _as_user(None):
        assert await lookup(MediaType.TRACK, "t1", THEIRS) is not None


async def test_get_item_by_external_id_skips_a_library_item_of_a_hidden_source(
    music: MusicController,
) -> None:
    """An external id lookup passes over a hidden library item and asks the user's sources."""
    library_track = await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    for instance_id in (THEIRS, MINE):
        provider = _mock(music, instance_id)
        provider.supported_features = {ProviderFeature.TRACK_BY_EXTERNAL_ID}
        provider.get_track_by_external_id.return_value = create_track(instance_id, "t2")

    with _as_user(OWNER):
        found = await music.tracks.get_item_by_external_id(ISRC, ExternalID.ISRC)
        assert found is not None
        assert found.item_id == library_track.item_id
    with _as_user(MEMBER):
        found = await music.tracks.get_item_by_external_id(ISRC, ExternalID.ISRC)
        assert found is not None
        assert found.provider == MINE
    _mock(music, THEIRS).get_track_by_external_id.assert_not_awaited()


async def test_get_item_by_external_id_takes_the_first_library_item_the_user_may_see(
    music: MusicController,
) -> None:
    """Two library items share an external id; a hidden one does not shadow the visible one."""
    await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    mine = await music.tracks.add_item_to_library(
        create_track(MINE, "t2", name="Zz Track", isrc="GBUM71505078")
    )
    # a second row with the same identifier, as a match that was not made leaves behind
    await music.tracks.set_external_ids(mine.item_id, {(ExternalID.ISRC, ISRC)})

    with _as_user(MEMBER):
        found = await music.tracks.get_item_by_external_id(ISRC, ExternalID.ISRC)
    assert found is not None
    assert (found.provider, found.item_id) == ("library", mine.item_id)
    _mock(music, MINE).get_track_by_external_id.assert_not_awaited()


async def test_get_takes_the_first_library_row_the_user_may_see_for_a_provider_id(
    music: MusicController,
) -> None:
    """Two library rows carry the same catalog id; the hidden one does not shadow the other."""
    await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    mine = await music.tracks.add_item_to_library(
        create_track(MY_SPOTIFY, "t2", name="Zz Track", isrc="GBUM71505078")
    )
    # a second row with the same catalog id on the member's account, as a missed match leaves
    await music.tracks.set_provider_mappings(
        mine.item_id,
        {ProviderMapping(item_id="t1", provider_domain="spotify", provider_instance=MY_SPOTIFY)},
    )
    _add_my_spotify(music)

    with _as_user(MEMBER):
        found = await music.tracks.get("t1", "spotify")
    assert (found.provider, found.item_id) == ("library", mine.item_id)


async def test_get_collection_needs_a_visible_source(music: MusicController) -> None:
    """A collection whose books are all on hidden sources does not exist for the user."""
    book = Audiobook(
        item_id="b1",
        provider=THEIRS,
        name="Book",
        provider_mappings={
            ProviderMapping(
                item_id="b1", provider_domain="spotify", provider_instance=THEIRS, in_library=True
            )
        },
    )
    book.metadata.collections = UniqueList([MediaItemCollection(title="Saga")])
    await music.audiobooks.add_item_to_library(book)
    collection_id = get_collection_item_id("Saga", MediaType.AUDIOBOOK)

    with _as_user(OWNER):
        assert (await music.audiobooks.get_collection(collection_id)).name == "Saga"
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        await music.audiobooks.get_collection(collection_id)


async def test_playlist_tracks_are_served_by_one_visible_provider(music: MusicController) -> None:
    """Playlist pages come from the resolved visible account; a hidden one is refused."""
    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        _ = [track async for track in music.playlists.tracks("p1", THEIRS)]

    my_spotify = _add_my_spotify(music)
    my_spotify.get_playlist_tracks.side_effect = [[create_track(MY_SPOTIFY, "t1")], []]
    with _as_user(MEMBER):
        tracks = [track async for track in music.playlists.tracks("p1", THEIRS)]
    assert [track.item_id for track in tracks] == ["t1"]
    assert my_spotify.get_playlist_tracks.await_count == 2
    _mock(music, THEIRS).get_playlist_tracks.assert_not_awaited()


async def test_sound_effect_of_a_hidden_source_is_not_found(music: MusicController) -> None:
    """A sound effect is only resolved through a music source the user may see."""
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        await music.get_item(MediaType.SOUND_EFFECT, "rain", THEIRS)


def _mapping(instance_id: str, item_id: str) -> ProviderMapping:
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("_", maxsplit=1)[0],
        provider_instance=instance_id,
        in_library=True,
    )


async def test_search_asks_only_a_provider_the_user_may_see(music: MusicController) -> None:
    """The search behind version scans is answered by a visible account or not at all."""
    theirs = _mock(music, THEIRS)
    theirs.supported_features = {ProviderFeature.SEARCH}
    theirs.supported_media_types = {MediaType.TRACK}
    theirs.search = AsyncMock(return_value=SearchResults())

    with _as_user(MEMBER):
        assert await music.tracks.search("query", THEIRS) == []
    theirs.search.assert_not_awaited()
    with _as_user(OWNER):
        await music.tracks.search("query", THEIRS)
    theirs.search.assert_awaited_once()


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param(lambda music: music.albums.tracks("a1", THEIRS), id="album_tracks"),
        pytest.param(lambda music: music.artists.albums("ar1", THEIRS), id="artist_albums"),
        pytest.param(lambda music: music.artists.tracks("ar1", THEIRS), id="artist_tracks"),
        pytest.param(lambda music: music.artists.top_tracks("ar1", THEIRS), id="top_tracks"),
        pytest.param(lambda music: music.artists.top_albums("ar1", THEIRS), id="top_albums"),
        pytest.param(
            lambda music: music.artists.similar_artists("ar1", THEIRS), id="similar_artists"
        ),
        pytest.param(
            lambda music: music.artists.audiobooks("ar1", THEIRS, ArtistType.AUTHOR),
            id="artist_audiobooks",
        ),
        pytest.param(
            lambda music: _drain(music.podcasts.episodes("p1", THEIRS)), id="podcast_episodes"
        ),
        pytest.param(lambda music: music.podcasts.episode("e1", THEIRS), id="podcast_episode"),
        pytest.param(
            lambda music: music.podcasts.episode_transcript("e1", THEIRS),
            id="podcast_episode_transcript",
        ),
    ],
)
async def test_provider_listings_refuse_a_hidden_source(
    music: MusicController, listing: Callable[[MusicController], Awaitable[object]]
) -> None:
    """A listing asked of a source the user may not see is refused, not served empty."""
    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        await listing(music)
    _mock(music, THEIRS).assert_not_called()


async def test_album_tracks_come_from_the_resolved_visible_account(
    music: MusicController,
) -> None:
    """The member's own account serves the tracks, and the album they are backfilled from."""
    my_spotify = _add_my_spotify(music)
    my_spotify.get_album_tracks = AsyncMock(return_value=[create_track(MY_SPOTIFY, "t1")])
    my_spotify.get_album = AsyncMock(
        return_value=Album(item_id="a1", provider=MY_SPOTIFY, name="Album", provider_mappings=set())
    )

    with _as_user(MEMBER):
        tracks = await music.albums.tracks("a1", THEIRS)

    assert [track.item_id for track in tracks] == ["t1"]
    assert tracks[0].album is not None
    my_spotify.get_album_tracks.assert_awaited_once_with("a1")
    my_spotify.get_album.assert_awaited_once_with("a1")
    _mock(music, THEIRS).get_album_tracks.assert_not_called()


async def test_album_tracks_of_a_hidden_library_album_come_from_the_users_account(
    music: MusicController,
) -> None:
    """A library album held only by a hidden account does not turn the listing empty."""
    await music.albums.add_item_to_library(create_album(THEIRS, "a1"))
    my_spotify = _add_my_spotify(music)
    my_spotify.get_album_tracks = AsyncMock(return_value=[create_track(MY_SPOTIFY, "t1")])
    my_spotify.get_album = AsyncMock(return_value=create_album(MY_SPOTIFY, "a1"))

    with _as_user(MEMBER):
        tracks = await music.albums.tracks("a1", THEIRS)

    assert [track.item_id for track in tracks] == ["t1"]


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param(lambda music, prov: music.artists.top_tracks("ar1", prov), id="top_tracks"),
        pytest.param(lambda music, prov: music.artists.top_albums("ar1", prov), id="top_albums"),
        pytest.param(
            lambda music, prov: music.artists.similar_artists("ar1", prov), id="similar_artists"
        ),
    ],
)
async def test_provider_listings_keep_the_served_item_over_a_hidden_library_copy(
    music: MusicController,
    listing: Callable[[MusicController, str], Awaitable[Sequence[MediaItemType]]],
) -> None:
    """A library copy held only by a hidden account is not swapped in for the served item."""
    await music.tracks.add_item_to_library(create_track(THEIRS, "t1"))
    await music.albums.add_item_to_library(create_album(THEIRS, "a1"))
    await music.artists.add_item_to_library(_artist(THEIRS, "ar2"))
    for account in (_add_my_spotify(music), _mock(music, THEIRS)):
        # a streaming provider stamps its domain on the items it serves
        account.get_artist_toptracks = AsyncMock(return_value=[create_track("spotify", "t1")])
        account.get_artist_topalbums = AsyncMock(return_value=[create_album("spotify", "a1")])
        account.get_similar_artists = AsyncMock(return_value=[_artist("spotify", "ar2")])

    with _as_user(MEMBER):
        assert [item.provider for item in await listing(music, MY_SPOTIFY)] == ["spotify"]
    with _as_user(OWNER):
        assert [item.provider for item in await listing(music, THEIRS)] == ["library"]


async def test_similar_tracks_skip_a_mapping_on_a_hidden_source(music: MusicController) -> None:
    """Only the mappings on the user's sources are asked for similar tracks."""
    track = create_track(THEIRS, "t1")
    track.provider_mappings.add(_mapping(MINE, "t1-mine"))
    library_track = await music.tracks.add_item_to_library(track)
    for instance_id in (THEIRS, MINE):
        provider = _mock(music, instance_id)
        provider.supported_features = {ProviderFeature.SIMILAR_TRACKS}
        provider.get_similar_tracks = AsyncMock(return_value=[create_track(instance_id, "s1")])

    with _as_user(MEMBER):
        similar = await music.tracks.similar_tracks(library_track.item_id, "library")

    assert [item.provider for item in similar] == [MINE]
    _mock(music, THEIRS).get_similar_tracks.assert_not_awaited()


async def test_export_radios_lists_only_the_users_sources(music: MusicController) -> None:
    """The radio export carries the stations of the user's sources only."""
    for instance_id, name in ((THEIRS, "Their Station"), (MINE, "My Station")):
        await music.radio.add_item_to_library(
            Radio(
                item_id=f"r-{instance_id}",
                provider=instance_id,
                name=name,
                provider_mappings={_mapping(instance_id, f"r-{instance_id}")},
            )
        )

    with _as_user(MEMBER):
        export = await music.radio.export_radios()

    assert "My Station" in export
    assert "Their Station" not in export


async def test_library_artist_types_follow_the_users_sources(music: MusicController) -> None:
    """The artist types listed are those of artists on the user's sources."""
    await music.artists.add_item_to_library(
        Artist(
            item_id="au1",
            provider=THEIRS,
            name="Author",
            artist_type=ArtistType.AUTHOR,
            provider_mappings={_mapping(THEIRS, "au1")},
        )
    )

    with _as_user(OWNER):
        assert await music.artists.get_library_artist_types() == [ArtistType.AUTHOR]
    with _as_user(MEMBER):
        assert await music.artists.get_library_artist_types() == []
