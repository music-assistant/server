"""
Tests for the order a play request plays an album or playlist in.

The queue plays a container in the order its tracks listing shows for the sort the request
names, with the typed sort fields or the deprecated sort key an outdated client sends.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE = "spotify_1"
ALBUM_ID = "al1"
PLAYLIST_ID = "pl1"
BATCH = datetime(2024, 3, 1, tzinfo=UTC)

# the keys the playlist page sends with a play request and the sort each one stands for
PLAYLIST_SORTS = {
    "position": (SortField.POSITION, SortDirection.ASC),
    "position_desc": (SortField.POSITION, SortDirection.DESC),
    "timestamp_added": (SortField.TIMESTAMP_ADDED, SortDirection.ASC),
    "timestamp_added_desc": (SortField.TIMESTAMP_ADDED, SortDirection.DESC),
    "name": (SortField.NAME, SortDirection.ASC),
    "artist": (SortField.ARTIST_NAME, SortDirection.ASC),
    "album": (SortField.ALBUM_NAME, SortDirection.ASC),
    "duration": (SortField.DURATION, SortDirection.ASC),
    "duration_desc": (SortField.DURATION, SortDirection.DESC),
}

# the order each of those keys plays the playlist of _playlist_tracks in
PLAYLIST_ORDERS = {
    "position": ["p1", "p2", "p3", "p4"],
    "position_desc": ["p4", "p3", "p2", "p1"],
    # undated tracks count as the oldest, tracks added together keep the playlist order
    "timestamp_added": ["p2", "p1", "p4", "p3"],
    "timestamp_added_desc": ["p3", "p1", "p4", "p2"],
    # the displayed name, a leading article included
    "name": ["p3", "p4", "p1", "p2"],
    "artist": ["p2", "p4", "p1", "p3"],
    # each album in disc and track order
    "album": ["p4", "p2", "p3", "p1"],
    "duration": ["p2", "p4", "p1", "p3"],
    "duration_desc": ["p3", "p1", "p4", "p2"],
}

# the keys the album page sends with a play request and the sort each one stands for
ALBUM_SORTS = {
    "track_number": (SortField.TRACK_NUMBER, SortDirection.ASC),
    "name": (SortField.NAME, SortDirection.ASC),
    "sort_name": (SortField.SORT_NAME, SortDirection.ASC),
    "duration": (SortField.DURATION, SortDirection.ASC),
    "duration_desc": (SortField.DURATION, SortDirection.DESC),
}

# the order each of those keys plays the album of _album_tracks in
ALBUM_ORDERS = {
    "track_number": ["a1", "a2", "a4", "a3"],
    "name": ["a3", "a4", "a1", "a2"],
    # a leading article moves to the end
    "sort_name": ["a3", "a4", "a2", "a1"],
    "duration": ["a2", "a4", "a1", "a3"],
    "duration_desc": ["a3", "a1", "a4", "a2"],
}


class _StubProvider(MusicProvider):
    """Music provider listing one album and one playlist, as the test hands them over."""

    def __init__(self, album_tracks: list[Track], playlist_tracks: list[Track]) -> None:
        """
        Initialize the stub provider.

        :param album_tracks: The tracks of the album, in the order the provider lists them.
        :param playlist_tracks: The tracks of the playlist, in playlist order.
        """
        self.album_tracks = album_tracks
        self.playlist_tracks = playlist_tracks
        self.config = MagicMock()
        self.config.instance_id = INSTANCE
        self.manifest = MagicMock()
        self.manifest.domain = "spotify"
        self.logger = MagicMock()
        self.available = True

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return the tracks of the album."""
        return self.album_tracks

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return the playlist."""
        return _playlist()

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return the playlist on its first page."""
        return [] if page else self.playlist_tracks


@pytest.fixture(name="mass")
async def mass_fixture(music_mass: MusicAssistant) -> AsyncIterator[MusicAssistant]:
    """Run on a library-only instance with a cache store and a stub provider."""
    await music_mass.cache._setup_database()
    music_mass.streams = MagicMock()
    music_mass.streams.audio_analysis.get_tracks_audio_metadata = AsyncMock(
        side_effect=lambda tracks: [None] * len(tracks)
    )
    provider = _StubProvider(_album_tracks(), _playlist_tracks())
    with (
        patch.object(
            music_mass,
            "get_provider",
            side_effect=lambda provider_id, *_args, **_kwargs: (
                provider if provider_id in (INSTANCE, "spotify") else None
            ),
        ),
        # the stub album tracks carry their album already
        patch.object(music_mass.music.albums, "_backfill_album_on_tracks", AsyncMock()),
    ):
        yield music_mass


def _mapping(item_id: str) -> set[ProviderMapping]:
    return {ProviderMapping(item_id=item_id, provider_domain="spotify", provider_instance=INSTANCE)}


def _reference(item_id: str, name: str, media_type: MediaType) -> ItemMapping:
    return ItemMapping(item_id=item_id, provider=INSTANCE, name=name, media_type=media_type)


def _track(
    item_id: str,
    name: str,
    *,
    disc_number: int = 1,
    track_number: int = 1,
    duration: int = 200,
    position: int | None = None,
    artist: str = "Artist",
    album: tuple[str, str] = (ALBUM_ID, "Album"),
    date_added: datetime | None = None,
) -> Track:
    return Track(
        item_id=item_id,
        provider=INSTANCE,
        name=name,
        duration=duration,
        position=position,
        disc_number=disc_number,
        track_number=track_number,
        date_added=date_added,
        artists=UniqueList([_reference(artist.lower(), artist, MediaType.ARTIST)]),
        album=_reference(*album, MediaType.ALBUM),
        provider_mappings=_mapping(item_id),
    )


def _album_tracks() -> list[Track]:
    """Return the tracks of the album, listed out of their disc and track order."""
    return [
        _track("a3", "Alpha Song", disc_number=2, track_number=1, duration=500),
        _track("a1", "The Zebra", track_number=1, duration=300),
        _track("a4", "Middle", track_number=3, duration=200),
        _track("a2", "Umbrella", track_number=2, duration=100),
    ]


def _playlist_tracks() -> list[Track]:
    """Return the tracks of the playlist, in playlist order."""
    alpha, beta = ("alpha", "Alpha"), ("beta", "Beta")
    return [
        _track(
            "p1",
            "The Zebra",
            position=1,
            artist="Metallica",
            album=beta,
            track_number=2,
            duration=300,
            date_added=BATCH,
        ),
        _track(
            "p2", "Umbrella", position=2, artist="ABBA", album=alpha, disc_number=2, duration=100
        ),
        _track(
            "p3",
            "Alpha Song",
            position=3,
            artist="Zeppelin",
            album=beta,
            duration=500,
            date_added=datetime(2024, 5, 10, tzinfo=UTC),
        ),
        _track(
            "p4",
            "Middle",
            position=4,
            artist="Coldplay",
            album=alpha,
            track_number=5,
            duration=200,
            date_added=BATCH,
        ),
    ]


def _album() -> Album:
    return Album(
        item_id=ALBUM_ID, provider=INSTANCE, name="Album", provider_mappings=_mapping(ALBUM_ID)
    )


def _playlist() -> Playlist:
    return Playlist(
        item_id=PLAYLIST_ID,
        provider=INSTANCE,
        name="Playlist",
        provider_mappings=_mapping(PLAYLIST_ID),
    )


def _resolver(mass: MusicAssistant | MagicMock) -> MediaResolver:
    """Create a resolver on the given instance, whose albums play without choosing a source."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.mass = mass
    resolver.queues = MagicMock(domain="player_queues")
    resolver.logger = MagicMock()
    resolver._album_source = AsyncMock(return_value=(None, {}))  # type: ignore[method-assign]
    return resolver


def _ids(items: list[Any]) -> list[str]:
    return [getattr(item, "item", item).item_id for item in items]


@pytest.mark.parametrize(("sort_key", "expected"), PLAYLIST_ORDERS.items())
async def test_a_playlist_plays_in_the_order_its_listing_shows(
    mass: MusicAssistant, sort_key: str, expected: list[str]
) -> None:
    """Every key the playlist page sends plays the playlist as its tracks listing shows it."""
    sort_field, sort_direction = PLAYLIST_SORTS[sort_key]

    played = await _resolver(mass).get_playlist_tracks(
        _playlist(), None, sort_field, sort_direction
    )
    listed = await mass.music.playlists.tracks(
        PLAYLIST_ID, INSTANCE, sort_field=sort_field, sort_direction=sort_direction, limit=0
    )

    assert _ids(played) == _ids(listed) == expected


@pytest.mark.parametrize(("sort_key", "expected"), ALBUM_ORDERS.items())
async def test_an_album_plays_in_the_order_its_listing_shows(
    mass: MusicAssistant, sort_key: str, expected: list[str]
) -> None:
    """Every key the album page sends plays the album as its tracks listing shows it."""
    sort_field, sort_direction = ALBUM_SORTS[sort_key]

    played = await _resolver(mass)._resolve_media_items(
        _album(), sort_field=sort_field, sort_direction=sort_direction
    )
    listed = await mass.music.albums.tracks(
        ALBUM_ID, INSTANCE, sort_field=sort_field, sort_direction=sort_direction, limit=0
    )

    assert _ids(played) == _ids(listed) == expected


@pytest.mark.parametrize(
    ("keep_preceding_items", "expected"),
    [(False, ["p4", "p1", "p3"]), (True, ["p4", "p1", "p3", "p2"])],
)
async def test_a_sorted_playlist_starts_at_the_chosen_track(
    mass: MusicAssistant, keep_preceding_items: bool, expected: list[str]
) -> None:
    """The chosen track is looked up in the sorted playlist, the tracks before it dropped or last."""
    played = await _resolver(mass).get_playlist_tracks(
        _playlist(),
        "p4",
        SortField.DURATION,
        keep_preceding_items=keep_preceding_items,
    )

    assert _ids(played) == expected


@pytest.mark.parametrize(
    ("keep_preceding_items", "expected"),
    [(False, ["a4", "a2", "a1"]), (True, ["a4", "a2", "a1", "a3"])],
)
async def test_a_sorted_album_starts_at_the_chosen_track(
    mass: MusicAssistant, keep_preceding_items: bool, expected: list[str]
) -> None:
    """The chosen track is looked up in the sorted album, the tracks before it dropped or last."""
    played = await _resolver(mass)._resolve_media_items(
        _album(),
        start_item="a4",
        sort_field=SortField.TRACK_NUMBER,
        sort_direction=SortDirection.DESC,
        keep_preceding_items=keep_preceding_items,
    )

    assert _ids(played) == expected


def _streaming_resolver(tracks: list[Track]) -> tuple[MediaResolver, list[str], AsyncMock]:
    """
    Create a resolver whose playlist streams the given tracks.

    :return: The resolver, the ids of the tracks streamed so far and the playlist's tracks listing.
    """
    mass = MagicMock()
    streamed: list[str] = []

    async def _iter_tracks(*_args: Any, **_kwargs: Any) -> AsyncIterator[Track]:
        for track in tracks:
            streamed.append(track.item_id)
            yield track

    mass.music.playlists.iter_tracks = _iter_tracks
    mass.music.playlists.tracks = listing = AsyncMock(return_value=tracks)
    return _resolver(mass), streamed, listing


@pytest.mark.parametrize(
    ("sort_field", "sort_direction"),
    [(None, None), (SortField.POSITION, None), PLAYLIST_SORTS["position"]],
)
async def test_a_playlist_in_its_own_order_is_streamed(
    sort_field: SortField | None, sort_direction: SortDirection | None
) -> None:
    """The playlist order is walked as the provider lists it, never as a whole listing."""
    resolver, streamed, listing = _streaming_resolver(_playlist_tracks())

    played = await resolver.get_playlist_tracks(_playlist(), "p3", sort_field, sort_direction)

    assert _ids(played) == ["p3", "p4"]
    assert streamed == ["p1", "p2", "p3", "p4"]
    listing.assert_not_awaited()


async def test_a_sorted_playlist_is_played_from_its_listing() -> None:
    """Any other order is asked of the playlist's tracks listing, which serves it whole."""
    resolver, streamed, listing = _streaming_resolver(_playlist_tracks())

    await resolver.get_playlist_tracks(_playlist(), None, SortField.POSITION, SortDirection.DESC)

    assert streamed == []
    listing.assert_awaited_once_with(
        PLAYLIST_ID,
        INSTANCE,
        force_refresh=False,
        allow_dynamic_tracks=False,
        sort_field=SortField.POSITION,
        sort_direction=SortDirection.DESC,
        limit=None,
    )


async def test_a_sort_the_playlist_does_not_offer_is_rejected_before_it_is_fetched() -> None:
    """A field the playlist tracks listing does not offer fails before any track is fetched."""
    resolver, streamed, listing = _streaming_resolver(_playlist_tracks())

    with pytest.raises(InvalidDataError):
        await resolver.get_playlist_tracks(_playlist(), None, SortField.TRACK_NUMBER)

    assert streamed == []
    listing.assert_not_awaited()


async def test_a_sort_the_album_does_not_offer_is_rejected_before_it_is_fetched(
    mass: MusicAssistant,
) -> None:
    """A field the album tracks listing does not offer fails before any track is fetched."""
    with (
        patch.object(mass.music.albums, "_get_provider_album_tracks", AsyncMock()) as fetch,
        pytest.raises(InvalidDataError),
    ):
        await _resolver(mass)._resolve_media_items(_album(), sort_field=SortField.ALBUM_NAME)

    fetch.assert_not_awaited()


def _controller() -> Any:
    """Build a bare controller with a queue, whose play requests stop at the queue loader."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.logger = MagicMock()
    ctrl._check_player_permission = MagicMock()  # type: ignore[method-assign]
    ctrl.get = MagicMock()  # type: ignore[method-assign]
    ctrl._handle_play_media = AsyncMock()  # type: ignore[method-assign]
    return ctrl


def _requested_sort(ctrl: Any) -> tuple[SortField | None, SortDirection | None]:
    """Return the sort the controller handed on to the queue loader."""
    call = ctrl._handle_play_media.await_args
    handed_on = inspect.signature(PlayerQueuesController._handle_play_media).bind(
        ctrl, *call.args, **call.kwargs
    )
    return handed_on.arguments["sort_field"], handed_on.arguments["sort_direction"]


@pytest.fixture(name="media")
def media_fixture() -> dict[str, MediaItemType | str]:
    """Return the media to play by name, as uris and as items."""
    return {
        "album_uri": "library://album/1",
        "playlist_uri": f"{INSTANCE}://playlist/{PLAYLIST_ID}",
        "track_uri": "library://track/1",
        "artist_uri": "library://artist/1",
        "album": _album(),
        "playlist": _playlist(),
    }


@pytest.mark.parametrize(
    ("media_name", "sort_key", "expected"),
    [
        *(("album_uri", key, sort) for key, sort in ALBUM_SORTS.items()),
        *(("playlist_uri", key, sort) for key, sort in PLAYLIST_SORTS.items()),
    ],
)
async def test_a_legacy_sort_key_is_played_as_its_typed_sort(
    media: dict[str, MediaItemType | str],
    media_name: str,
    sort_key: str,
    expected: tuple[SortField, SortDirection],
) -> None:
    """The keys the album and playlist pages send map to the sort fields of their listings."""
    ctrl = _controller()

    await ctrl.play_media("q1", media[media_name], sort_by=sort_key)

    assert _requested_sort(ctrl) == expected


@pytest.mark.parametrize(
    ("media_name", "sort_field", "sort_direction"),
    [
        ("album", SortField.DURATION, SortDirection.DESC),
        ("playlist", SortField.ALBUM_NAME, None),
        ("playlist", None, SortDirection.DESC),
    ],
)
async def test_a_typed_sort_is_played(
    media: dict[str, MediaItemType | str],
    media_name: str,
    sort_field: SortField | None,
    sort_direction: SortDirection | None,
) -> None:
    """The typed sort goes to the queue loader as it is requested."""
    ctrl = _controller()

    await ctrl.play_media(
        "q1", media[media_name], sort_field=sort_field, sort_direction=sort_direction
    )

    assert _requested_sort(ctrl) == (sort_field, sort_direction)


async def test_a_typed_sort_field_wins_over_the_legacy_key(
    media: dict[str, MediaItemType | str],
) -> None:
    """The deprecated key only counts when no sort field is requested."""
    ctrl = _controller()

    await ctrl.play_media(
        "q1", media["playlist"], sort_by="duration_desc", sort_field=SortField.NAME
    )

    assert _requested_sort(ctrl) == (SortField.NAME, None)


@pytest.mark.parametrize(
    ("media_names", "sort_by", "sort_field"),
    [
        (["album_uri"], "nonexistent", None),
        (["album_uri"], None, SortField.ALBUM_NAME),
        (["album"], "position_desc", None),
        (["playlist_uri"], "track_number", None),
        (["track_uri", "playlist"], None, SortField.SORT_NAME),
    ],
)
async def test_a_sort_that_is_not_offered_is_rejected_before_anything_plays(
    media: dict[str, MediaItemType | str],
    media_names: list[str],
    sort_by: str | None,
    sort_field: SortField | None,
) -> None:
    """An unknown key, or a field an album or playlist does not offer, fails the whole request."""
    ctrl = _controller()

    with pytest.raises(InvalidDataError):
        await ctrl.play_media(
            "q1", [media[name] for name in media_names], sort_by=sort_by, sort_field=sort_field
        )

    ctrl._handle_play_media.assert_not_awaited()


@pytest.mark.parametrize("media_name", ["track_uri", "artist_uri"])
async def test_media_without_a_tracks_listing_ignores_the_sort(
    media: dict[str, MediaItemType | str], media_name: str
) -> None:
    """A sort for media that plays in an order of its own is handed on unchecked."""
    ctrl = _controller()

    await ctrl.play_media("q1", media[media_name], sort_field=SortField.ALBUM_NAME)

    assert _requested_sort(ctrl) == (SortField.ALBUM_NAME, None)
