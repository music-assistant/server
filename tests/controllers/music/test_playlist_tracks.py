"""Tests for the paged playlist tracks listing and the playlist iterator."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import ListingType, MediaType, SortDirection, SortField
from music_assistant_models.errors import (
    InvalidDataError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import (
    Audiobook,
    AudioMetadata,
    ItemMapping,
    Playlist,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    Track,
)

from music_assistant.constants import PlaylistPlayableItem
from music_assistant.controllers.cache.constants import BYPASS_CACHE
from music_assistant.controllers.music.constants import CACHE_CATEGORY_LISTINGS
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE = "spotify_1"
PLAYLIST_ID = "pl1"
USER_A = User(user_id="user-a", username="user-a", role=UserRole.USER)
USER_B = User(user_id="user-b", username="user-b", role=UserRole.USER)


class _StubPlaylistProvider(MusicProvider):
    """Music provider listing one playlist in pages, counting the pages it is asked for."""

    def __init__(
        self,
        tracks: Sequence[PlaylistPlayableItem],
        page_size: int = 2,
        instance_id: str = INSTANCE,
    ) -> None:
        """
        Initialize the stub provider.

        :param tracks: The tracks of the playlist, in playlist order.
        :param page_size: The number of tracks per page.
        :param instance_id: The instance id, whose first part is the domain.
        """
        self.tracks = list(tracks)
        self.page_size = page_size
        self.is_dynamic = False
        self.details_error: Exception | None = None
        self.failing_page: int | None = None
        self.details_fetched = 0
        self.pages_fetched = 0
        # whether each page was asked for past the caches
        self.bypassed: list[bool] = []
        self.config = MagicMock()
        self.config.instance_id = instance_id
        self.manifest = MagicMock()
        self.manifest.domain = instance_id.split("_", maxsplit=1)[0]
        self.logger = MagicMock()
        self.available = True

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return the playlist, dynamic when the test says so."""
        self.details_fetched += 1
        if self.details_error:
            raise self.details_error
        return _playlist(self.instance_id, prov_playlist_id, is_dynamic=self.is_dynamic)

    async def get_playlist_tracks(
        self, prov_playlist_id: str, page: int = 0
    ) -> list[PlaylistPlayableItem]:
        """Return one page of the playlist."""
        self.pages_fetched += 1
        self.bypassed.append(BYPASS_CACHE.get())
        if page == self.failing_page:
            raise ResourceTemporarilyUnavailable("rate limited")
        return self.tracks[page * self.page_size : (page + 1) * self.page_size]


class _StubPlaylistPlugin(PluginProvider):
    """Plugin composing a playlist itself, like the smart playlists do."""

    def __init__(self, tracks: list[Track]) -> None:
        """
        Initialize the stub plugin.

        :param tracks: The tracks the plugin composes.
        """
        self.tracks = tracks
        self.pages_fetched = 0
        self.config = MagicMock()
        self.config.instance_id = "smart_playlist"
        self.manifest = MagicMock()
        self.manifest.domain = "smart_playlist"
        self.logger = MagicMock()
        self.available = True

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return the composed tracks on the first page."""
        self.pages_fetched += 1
        return [] if page else self.tracks


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_audio_analysis: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store and the stored audio analysis."""
    return music_mass_with_audio_analysis


def _playlist(provider: str, item_id: str, is_dynamic: bool = False) -> Playlist:
    domain = provider.split("_", maxsplit=1)[0]
    return Playlist(
        item_id=item_id,
        provider=provider,
        name=f"Playlist {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=domain, provider_instance=provider)
        },
        is_dynamic=is_dynamic,
    )


def _playlist_tracks(count: int) -> list[Track]:
    """Return the tracks of a playlist: odd names, durations and albums out of their order."""
    tracks = []
    for position in range(1, count + 1):
        track = create_track(INSTANCE, f"t{position}", name=f"Song {(position * 7) % count:02d}")
        track.duration = 100 + (position * 3) % count
        track.position = position
        track.album = create_album(INSTANCE, f"a{position % 2}", name=f"Album {position % 2}")
        track.disc_number, track.track_number = 1, count - position
        tracks.append(track)
    return tracks


def _with_provider(mass: MusicAssistant, provider: MusicProvider | PluginProvider) -> Any:
    """Load the given provider as the only one, under its instance id and its domain."""

    def _get_provider(provider_id: str, *_args: Any, **_kwargs: Any) -> Any:
        return provider if provider_id in (provider.instance_id, provider.domain) else None

    return patch.object(mass, "get_provider", side_effect=_get_provider)


def _ids(items: list[Any]) -> list[str]:
    return [item.item_id for item in items]


async def test_playlist_tracks_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The items are searched, sorted and paged on the server, from one walk."""
    provider = _StubPlaylistProvider(_playlist_tracks(6))
    db_playlist = await mass.music.playlists.add_item_to_library(_playlist(INSTANCE, PLAYLIST_ID))

    async def _tracks(**kwargs: Any) -> list[str]:
        return _ids(await mass.music.playlists.tracks(db_playlist.item_id, "library", **kwargs))

    with _with_provider(mass, provider):
        assert await _tracks() == ["t1", "t2", "t3", "t4", "t5", "t6"]
        assert await _tracks(sort_direction=SortDirection.DESC) == [
            "t6",
            "t5",
            "t4",
            "t3",
            "t2",
            "t1",
        ]
        assert await _tracks(sort_field=SortField.NAME) == ["t6", "t1", "t2", "t3", "t4", "t5"]
        assert await _tracks(sort_field=SortField.DURATION, limit=2, offset=1) == ["t4", "t6"]
        assert await _tracks(search="song 01") == ["t1"]
        assert await _tracks(search="album 0") == ["t2", "t4", "t6"]
        assert await _tracks(limit=0) == ["t1", "t2", "t3", "t4", "t5", "t6"]
        with pytest.raises(InvalidDataError):
            await _tracks(sort_field=SortField.YEAR)
    # three pages and the empty one that ends the playlist
    assert provider.pages_fetched == 4


async def test_playlist_tracks_sorted_by_album_keep_the_album_order(mass: MusicAssistant) -> None:
    """A playlist sorted on the album lists the tracks of each album in disc and track order."""
    provider = _StubPlaylistProvider(_playlist_tracks(4))
    with _with_provider(mass, provider):
        tracks = await mass.music.playlists.tracks(
            PLAYLIST_ID, INSTANCE, sort_field=SortField.ALBUM_NAME
        )
    assert _ids(tracks) == ["t4", "t2", "t3", "t1"]


async def test_playlist_tracks_serve_a_first_page_of_500(mass: MusicAssistant) -> None:
    """Without a limit a caller gets the first 500 items; 0 returns the whole playlist."""
    provider = _StubPlaylistProvider(_playlist_tracks(1200), page_size=500)
    with _with_provider(mass, provider):
        first_page = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
        last_page = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, offset=1000)
        everything = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, limit=0)
    assert _ids(first_page) == [f"t{position}" for position in range(1, 501)]
    assert _ids(last_page) == [f"t{position}" for position in range(1001, 1201)]
    assert len(everything) == 1200
    assert provider.pages_fetched == 4


async def test_playlist_tracks_are_keyed_on_the_instance_of_the_provider(
    mass: MusicAssistant,
) -> None:
    """The domain and the instance it resolves to share one listing, under the instance's uri."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    with _with_provider(mass, provider):
        await mass.music.playlists.tracks(PLAYLIST_ID, "spotify")
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
    assert provider.pages_fetched == 2
    cached = await mass.cache.get(
        ListingType.PLAYLIST_TRACKS.value,
        provider=create_uri(MediaType.PLAYLIST, INSTANCE, PLAYLIST_ID),
        category=CACHE_CATEGORY_LISTINGS,
    )
    assert [item["item_id"] for item in cached] == ["t1", "t2"]


async def test_a_cached_playlist_is_not_looked_up_again(mass: MusicAssistant) -> None:
    """Whether a playlist outside the library is dynamic is looked up when it is fetched only."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    with _with_provider(mass, provider):
        for offset in (0, 1, 0):
            await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, limit=1, offset=offset)
    assert provider.details_fetched == 1
    assert provider.pages_fetched == 2


@pytest.mark.parametrize(
    "details_error",
    # a provider that offers no playlist details (Audiobookshelf) raises NotImplementedError
    [ResourceTemporarilyUnavailable("rate limited"), NotImplementedError()],
)
async def test_a_playlist_without_details_is_served_but_not_kept(
    mass: MusicAssistant, details_error: Exception
) -> None:
    """A playlist whose details can not be fetched may be dynamic: it is listed, not kept."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    provider.details_error = details_error
    with _with_provider(mass, provider):
        for _ in range(2):
            assert _ids(await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)) == ["t1", "t2"]
    assert provider.pages_fetched == 4


async def test_a_refresh_fetches_the_playlist_anew_and_keeps_that(mass: MusicAssistant) -> None:
    """force_refresh walks the provider again and what it fetched is served from then on."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    with _with_provider(mass, provider):
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
        provider.tracks = provider.tracks[:1]
        assert _ids(await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)) == ["t1", "t2"]
        refreshed = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, force_refresh=True)
        served = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
    assert _ids(refreshed) == _ids(served) == ["t1"]
    assert provider.pages_fetched == 4


async def test_a_refresh_of_the_listing_bypasses_the_caches_of_the_provider(
    mass: MusicAssistant,
) -> None:
    """force_refresh and allow_dynamic_tracks fetch the pages past the caches of the provider."""
    provider = _StubPlaylistProvider(_playlist_tracks(1))
    with _with_provider(mass, provider):
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, force_refresh=True)
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, allow_dynamic_tracks=True)
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
    # two pages a walk; the last request is served from what the refresh kept
    assert provider.bypassed == [False, False, True, True, True, True]


async def test_the_audio_analysis_of_a_cached_playlist_is_kept(mass: MusicAssistant) -> None:
    """The bpm and key the stored audio analysis adds come with the cached playlist too."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    analysis = AudioMetadata(bpm=120.0, musical_key="F# minor", camelot_key="11A")
    lookup = AsyncMock(return_value=[analysis, None])
    with (
        _with_provider(mass, provider),
        patch.object(mass.streams.audio_analysis, "get_tracks_audio_metadata", lookup),
    ):
        filled = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
        cached = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
    assert cached[0] is not filled[0]
    for listing in (filled, cached):
        assert [getattr(item, "audio_metadata", None) for item in listing] == [analysis, None]
    lookup.assert_awaited_once()


async def test_a_cached_playlist_rebuilds_every_kind_of_item(mass: MusicAssistant) -> None:
    """Radio stations, podcast episodes and audiobooks in a playlist come back as themselves."""
    podcast = ItemMapping(
        media_type=MediaType.PODCAST, item_id="show", provider=INSTANCE, name="Show"
    )

    def _mappings(item_id: str) -> set[ProviderMapping]:
        return {
            ProviderMapping(item_id=item_id, provider_domain="spotify", provider_instance=INSTANCE)
        }

    items: list[PlaylistPlayableItem] = [
        create_track(INSTANCE, "track"),
        Radio(
            item_id="radio", provider=INSTANCE, name="Radio", provider_mappings=_mappings("radio")
        ),
        PodcastEpisode(
            item_id="episode",
            provider=INSTANCE,
            name="Episode",
            provider_mappings=_mappings("episode"),
            position=1,
            podcast=podcast,
        ),
        Audiobook(
            item_id="book", provider=INSTANCE, name="Book", provider_mappings=_mappings("book")
        ),
    ]
    for position, item in enumerate(items, start=1):
        item.position = position
    provider = _StubPlaylistProvider(items, page_size=10)
    with _with_provider(mass, provider):
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
        cached = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
    assert [type(item) for item in cached] == [Track, Radio, PodcastEpisode, Audiobook]
    assert _ids(cached) == ["track", "radio", "episode", "book"]
    assert cast("PodcastEpisode", cached[2]).podcast.item_id == "show"
    assert provider.pages_fetched == 2


async def test_a_dynamic_playlist_is_never_cached(mass: MusicAssistant) -> None:
    """A dynamic playlist hands out a fresh sample on every request, by provider or library id."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    provider.is_dynamic = True
    db_playlist = await mass.music.playlists.add_item_to_library(
        _playlist(INSTANCE, "dyn_lib", is_dynamic=True)
    )
    with _with_provider(mass, provider):
        for _ in range(2):
            assert _ids(await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)) == ["t1", "t2"]
            await mass.music.playlists.tracks(db_playlist.item_id, "library")
    assert provider.pages_fetched == 8
    assert (
        await mass.cache.get(
            ListingType.PLAYLIST_TRACKS.value,
            provider=create_uri(MediaType.PLAYLIST, INSTANCE, PLAYLIST_ID),
            category=CACHE_CATEGORY_LISTINGS,
        )
        is None
    )


async def test_playlists_music_assistant_composes_are_never_cached(mass: MusicAssistant) -> None:
    """The builtin provider's and the plugins' playlists are composed anew for every request."""
    builtin = _StubPlaylistProvider(_playlist_tracks(2), instance_id="builtin")
    plugin = _StubPlaylistPlugin(_playlist_tracks(2))
    for provider in (builtin, plugin):
        with _with_provider(mass, provider):
            for _ in range(2):
                tracks = await mass.music.playlists.tracks("composed", provider.instance_id)
                assert _ids(tracks) == ["t1", "t2"]
    assert builtin.pages_fetched == plugin.pages_fetched == 4


async def test_the_favorite_state_of_a_playlist_is_the_calling_users(
    mass: MusicAssistant,
) -> None:
    """A provider track that is in the library carries the state of the user asking only."""
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    library_track = await mass.music.tracks.add_item_to_library(create_track(INSTANCE, "t1"))
    await mass.music.favorites.set(
        MediaType.TRACK, int(library_track.item_id), True, [USER_A.user_id]
    )
    await mass.music.favorites.set(
        MediaType.TRACK, int(library_track.item_id), False, [USER_B.user_id]
    )

    async def _states(user: User, **kwargs: Any) -> list[tuple[str, bool | None]]:
        with patch(
            "music_assistant.controllers.music.listing_cache.get_current_user", return_value=user
        ):
            items = await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, **kwargs)
        return [(item.item_id, item.favorite) for item in items]

    with _with_provider(mass, provider):
        assert await _states(USER_A) == [("t1", True), ("t2", None)]
        assert await _states(USER_B) == [("t1", False), ("t2", None)]
        assert await _states(USER_A, favorite=True) == [("t1", True)]
        assert await _states(USER_B, favorite=True) == []
        assert await _states(USER_B, favorite=False) == [("t1", False)]
    cached = await mass.cache.get(
        ListingType.PLAYLIST_TRACKS.value,
        provider=create_uri(MediaType.PLAYLIST, INSTANCE, PLAYLIST_ID),
        category=CACHE_CATEGORY_LISTINGS,
    )
    # the listing is kept as the provider lists it, without any user's state
    assert [item["favorite"] for item in cached] == [None, None]
    assert provider.pages_fetched == 2


async def test_favorite_filter_on_a_composed_playlist_uses_the_users_likes(
    mass: MusicAssistant,
) -> None:
    """The library tracks of a playlist that is not cached carry the calling user's likes."""
    library_track = await mass.music.tracks.add_item_to_library(create_track(INSTANCE, "liked"))
    await mass.music.favorites.set(MediaType.TRACK, int(library_track.item_id), True, ["user-a"])
    # what a cached page of another user carries
    library_track.favorite = None
    provider = _StubPlaylistProvider([library_track], instance_id="builtin")
    user_a = MagicMock(user_id="user-a")
    with (
        _with_provider(mass, provider),
        patch(
            "music_assistant.controllers.music.media.playlists.get_current_user",
            return_value=user_a,
        ),
    ):
        liked = await mass.music.playlists.tracks("composed", "builtin", favorite=True)
    assert _ids(liked) == [library_track.item_id]


async def test_a_playlist_a_provider_delivers_in_part_is_not_kept(mass: MusicAssistant) -> None:
    """A walk that fails part way is served as far as it got, and walked again next time."""
    provider = _StubPlaylistProvider(_playlist_tracks(4))
    provider.failing_page = 1
    with _with_provider(mass, provider):
        for _ in range(2):
            assert _ids(await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)) == ["t1", "t2"]
        # without anything to serve the failure surfaces
        provider.failing_page = 0
        with pytest.raises(ResourceTemporarilyUnavailable):
            await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
    assert provider.pages_fetched == 5


async def test_playlist_tracks_of_a_provider_not_loaded(mass: MusicAssistant) -> None:
    """Nothing is listed (or kept) without the provider; a strict request says it is missing."""
    assert await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE) == []
    with pytest.raises(ProviderUnavailableError):
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE, strict_provider_instance=True)
    provider = _StubPlaylistProvider(_playlist_tracks(2))
    with _with_provider(mass, provider):
        assert _ids(await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)) == ["t1", "t2"]


async def test_iter_tracks_walks_every_page_past_the_listing_cache(mass: MusicAssistant) -> None:
    """The iterator yields the whole playlist in provider order, from the provider every time."""
    provider = _StubPlaylistProvider(_playlist_tracks(5))
    db_playlist = await mass.music.playlists.add_item_to_library(_playlist(INSTANCE, PLAYLIST_ID))
    with _with_provider(mass, provider):
        await mass.music.playlists.tracks(PLAYLIST_ID, INSTANCE)
        walked = [x async for x in mass.music.playlists.iter_tracks(db_playlist.item_id, "library")]
    assert _ids(walked) == ["t1", "t2", "t3", "t4", "t5"]
    assert provider.pages_fetched == 8


async def test_iter_tracks_asks_a_dynamic_playlist_for_a_fresh_sample(
    mass: MusicAssistant,
) -> None:
    """allow_dynamic_tracks bypasses the caches of the provider, as playback needs."""
    provider = _StubPlaylistProvider(_playlist_tracks(1))
    with _with_provider(mass, provider):
        _ = [x async for x in mass.music.playlists.iter_tracks(PLAYLIST_ID, INSTANCE)]
        _ = [
            x
            async for x in mass.music.playlists.iter_tracks(
                PLAYLIST_ID, INSTANCE, allow_dynamic_tracks=True
            )
        ]
    assert provider.bypassed == [False, False, True, True]
