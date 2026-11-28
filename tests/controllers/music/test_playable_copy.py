"""Tests for finding the copy another streaming service has of an item that can not be played."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.constants import MATCH_SEARCH_LIMIT
from music_assistant.controllers.music.controller import MusicController
from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.mass import MusicAssistant

IHEART = "iheartradio--abc"
SPOTIFY = "spotify--def"
QOBUZ = "qobuz--ghi"
FILES = "filesystem_local--jkl"
FEATURES = {ProviderFeature.SEARCH}
MEDIA_TYPES = {MediaType.ALBUM, MediaType.TRACK}


def _provider(
    instance_id: str, streaming: bool = True, features: set[ProviderFeature] = FEATURES
) -> SimpleNamespace:
    """Return a loaded music source."""
    return SimpleNamespace(
        instance_id=instance_id,
        domain=instance_id.split("--", maxsplit=1)[0],
        name=instance_id,
        is_streaming_provider=streaming,
        supported_features=features,
        supported_media_types=MEDIA_TYPES,
    )


PROVIDERS = [
    _provider(IHEART),
    _provider(FILES, streaming=False),
    _provider(SPOTIFY),
    _provider(QOBUZ),
]


def _mapping(instance_id: str, item_id: str, available: bool = True) -> ProviderMapping:
    """Return a mapping of an item on the given provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance_id.split("--", maxsplit=1)[0],
        provider_instance=instance_id,
        available=available,
    )


def _album(instance_id: str = IHEART, item_id: str = "al1", available: bool = True) -> Album:
    """Return the album as the given service lists it."""
    return Album(
        item_id=item_id,
        provider=instance_id,
        name="Kind of Blue",
        artists=UniqueList(
            [
                ItemMapping(
                    item_id="ar1",
                    provider=instance_id,
                    name="Miles Davis",
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={_mapping(instance_id, item_id, available)},
    )


def _track(instance_id: str = IHEART, item_id: str = "t1", available: bool = True) -> Track:
    """Return the track as the given service lists it."""
    return Track(
        item_id=item_id,
        provider=instance_id,
        name="So What",
        provider_mappings={_mapping(instance_id, item_id, available)},
    )


@pytest.fixture(autouse=True)
def _memory_cache(music_mass: MusicAssistant) -> Iterator[None]:
    """Keep what is cached in memory, for the length of a test."""
    store: dict[str, Any] = {}

    async def _set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    with (
        patch.object(
            music_mass.cache, "get", AsyncMock(side_effect=lambda key, **_kwargs: store.get(key))
        ),
        patch.object(music_mass.cache, "set", AsyncMock(side_effect=_set)),
        patch.object(
            music_mass.cache,
            "delete",
            AsyncMock(side_effect=lambda key, **_kwargs: store.pop(key, None)),
        ),
    ):
        yield


@contextmanager
def _sources(providers: list[SimpleNamespace] = PROVIDERS) -> Iterator[None]:
    """Serve the given music sources as those of the current user."""
    with patch.object(
        MusicController, "providers", new_callable=PropertyMock, return_value=providers
    ):
        yield


@contextmanager
def _album_matches(
    matches: dict[str, Any], versions: dict[str, int] | None = None
) -> Iterator[AsyncMock]:
    """
    Match the album on each service as given, and serve the copies found.

    :param matches: The album id each service domain matches, or the error its search raises.
    :param versions: The other versions of the album, by id on Qobuz, each with its track
        count; the album itself lists ten tracks.
    """
    track_counts = {"al1": 10, **(versions or {})}

    async def _match(
        _album: Album, provider: SimpleNamespace, *_args: Any
    ) -> list[ProviderMapping]:
        found = matches.get(provider.domain)
        if isinstance(found, Exception):
            raise found
        return [_mapping(provider.instance_id, found)] if found else []

    match = AsyncMock(side_effect=_match)
    with (
        patch.object(AlbumsController, "_match_provider", match),
        patch.object(
            AlbumsController,
            "_versions_on_provider",
            AsyncMock(
                side_effect=lambda _album_arg, provider, *_args: (
                    [_album(QOBUZ, item_id) for item_id in versions or {}]
                    if provider.instance_id == QOBUZ
                    else []
                )
            ),
        ),
        patch.object(
            AlbumsController,
            "tracks",
            AsyncMock(
                side_effect=lambda item_id, *_args, **_kwargs: [_track()] * track_counts[item_id]
            ),
        ),
        patch.object(
            AlbumsController,
            "get_provider_item",
            AsyncMock(
                side_effect=lambda item_id, instance_id, **_kwargs: _album(instance_id, item_id)
            ),
        ),
    ):
        yield match


async def test_the_first_other_streaming_service_with_the_album_hands_out_its_copy(
    music_mass: MusicAssistant,
) -> None:
    """The album's own service and local sources are not searched; the first match wins."""
    with _sources(), _album_matches({"spotify": None, "qobuz": "q-al"}) as match:
        copy = await music_mass.music.albums.playable_copy(_album())

    assert copy is not None
    assert (copy.provider, copy.item_id) == (QOBUZ, "q-al")
    assert [call.args[1].instance_id for call in match.await_args_list] == [SPOTIFY, QOBUZ]


async def test_only_the_sources_that_may_serve_the_playback_are_searched(
    music_mass: MusicAssistant,
) -> None:
    """A source the playback user may not play from is left out."""
    with _sources(), _album_matches({"spotify": "s-al", "qobuz": "q-al"}) as match:
        copy = await music_mass.music.albums.playable_copy(_album(), allowed=[IHEART, QOBUZ])

    assert copy is not None
    assert copy.provider == QOBUZ
    assert [call.args[1].instance_id for call in match.await_args_list] == [QOBUZ]


async def test_what_a_service_was_found_to_have_is_kept(music_mass: MusicAssistant) -> None:
    """A second request does not search again, neither for a match nor for no match."""
    with _sources(), _album_matches({"spotify": None, "qobuz": "q-al"}) as match:
        await music_mass.music.albums.playable_copy(_album())
        copy = await music_mass.music.albums.playable_copy(_album())

    assert copy is not None
    assert copy.provider == QOBUZ
    assert match.await_count == 2


async def test_a_service_that_fails_is_skipped_and_asked_again_next_time(
    music_mass: MusicAssistant,
) -> None:
    """A failed search is not remembered as no match."""
    with _sources(), _album_matches({"spotify": TimeoutError(), "qobuz": None}) as match:
        assert await music_mass.music.albums.playable_copy(_album()) is None
        assert await music_mass.music.albums.playable_copy(_album()) is None

    assert [call.args[1].instance_id for call in match.await_args_list] == [SPOTIFY, QOBUZ, SPOTIFY]


async def test_a_copy_the_service_no_longer_has_is_searched_for_again(
    music_mass: MusicAssistant,
) -> None:
    """A kept copy that is gone is forgotten, so the service is searched again next time."""
    with _sources([_provider(QOBUZ)]), _album_matches({"qobuz": "q-al"}) as match:
        await music_mass.music.albums.playable_copy(_album())
        with patch.object(
            AlbumsController, "get_provider_item", AsyncMock(side_effect=MediaNotFoundError("gone"))
        ):
            assert await music_mass.music.albums.playable_copy(_album()) is None
        copy = await music_mass.music.albums.playable_copy(_album())

    assert copy is not None
    assert match.await_count == 2


async def test_a_copy_that_can_not_be_played_either_is_passed_over(
    music_mass: MusicAssistant,
) -> None:
    """The next service is tried when the copy found can not be played either."""
    with (
        _sources(),
        _album_matches({"spotify": "s-al", "qobuz": "q-al"}),
        patch.object(
            AlbumsController,
            "get_provider_item",
            AsyncMock(
                side_effect=lambda item_id, instance_id, **_kwargs: _album(
                    instance_id, item_id, available=instance_id != SPOTIFY
                )
            ),
        ),
    ):
        copy = await music_mass.music.albums.playable_copy(_album())

    assert copy is not None
    assert copy.provider == QOBUZ


async def test_a_version_with_as_many_tracks_is_taken_when_no_copy_matches(
    music_mass: MusicAssistant,
) -> None:
    """Without a strict match, the first other version with as many tracks is handed out."""
    with _sources(), _album_matches({}, versions={"deluxe": 14, "remaster": 10}):
        copy = await music_mass.music.albums.playable_copy(_album())
        searched = cast("AsyncMock", AlbumsController._versions_on_provider).await_args_list

    assert copy is not None
    assert (copy.provider, copy.item_id) == (QOBUZ, "remaster")
    # each service is asked for its own versions only, with the strict match's search size
    assert [(call.args[1].instance_id, call.args[2]) for call in searched] == [
        (SPOTIFY, MATCH_SEARCH_LIMIT),
        (QOBUZ, MATCH_SEARCH_LIMIT),
    ]


async def test_no_version_with_as_many_tracks_leaves_the_album_without_a_copy(
    music_mass: MusicAssistant,
) -> None:
    """A version with another track count is a different edition and is not taken."""
    with _sources(), _album_matches({}, versions={"deluxe": 14}):
        assert await music_mass.music.albums.playable_copy(_album()) is None


async def test_an_album_without_artists_is_not_searched_for(music_mass: MusicAssistant) -> None:
    """Without an artist the album can not be matched."""
    album = _album()
    album.artists.clear()
    with _sources(), _album_matches({"qobuz": "q-al"}) as match:
        assert await music_mass.music.albums.playable_copy(album) is None

    match.assert_not_awaited()


async def test_a_track_is_matched_against_its_albums(music_mass: MusicAssistant) -> None:
    """The track is matched as strictly as when it is linked, with the albums it is on."""
    ref_albums = [_album()]
    match = AsyncMock(
        side_effect=lambda _track, provider, *_args: [_mapping(provider.instance_id, "s1")]
    )
    with (
        _sources(),
        patch.object(TracksController, "albums", AsyncMock(return_value=ref_albums)),
        patch.object(TracksController, "match_provider", match),
        patch.object(
            TracksController,
            "get_provider_item",
            AsyncMock(
                side_effect=lambda item_id, instance_id, **_kwargs: _track(instance_id, item_id)
            ),
        ),
    ):
        copy = await music_mass.music.tracks.playable_copy(_track())

    assert copy is not None
    assert (copy.provider, copy.item_id) == (SPOTIFY, "s1")
    assert match.await_args_list[0].args[2:] == (True, ref_albums)


def _album_track(
    instance_id: str, item_id: str, number: int, duration: int, version: str = ""
) -> Track:
    """Return Bad Moon Rising at the given place on Green River, as the given service lists it."""
    track = _track(instance_id, item_id, available=instance_id != IHEART)
    track.name = "Bad Moon Rising"
    track.version = version
    track.duration = duration
    track.track_number = number
    track.disc_number = 1
    track.album = ItemMapping(
        item_id="al1", provider=instance_id, name="Green River", media_type=MediaType.ALBUM
    )
    return track


# iHeartRadio lists the live bonus track without its version, Qobuz with it in the title
IHEART_GREEN_RIVER = [_album_track(IHEART, "t5", 5, 141), _album_track(IHEART, "t12", 12, 126)]
QOBUZ_GREEN_RIVER = [
    _album_track(QOBUZ, "q5", 5, 141),
    _album_track(QOBUZ, "q12", 12, 126),
]
QOBUZ_GREEN_RIVER[1].name = "Bad Moon Rising - Live In Berlin"


@contextmanager
def _album_copy_listing(
    copy_tracks: list[Track], own_tracks: list[Track] = IHEART_GREEN_RIVER
) -> Iterator[AsyncMock]:
    """
    Serve the copy Qobuz has of the track's album.

    :param copy_tracks: The tracks the copy lists.
    :param own_tracks: The tracks the album lists on its own service.
    """
    search = AsyncMock(return_value=[])
    with (
        _sources(),
        patch.object(AlbumsController, "get_provider_item", AsyncMock(return_value=_album())),
        patch.object(
            AlbumsController, "playable_copy", AsyncMock(return_value=_album(QOBUZ, "q-al"))
        ),
        patch.object(
            AlbumsController,
            "tracks",
            AsyncMock(
                side_effect=lambda _item_id, provider, **_kwargs: (
                    copy_tracks if provider == QOBUZ else own_tracks
                )
            ),
        ),
        patch.object(TracksController, "albums", AsyncMock(return_value=[])),
        patch.object(TracksController, "match_provider", search),
    ):
        yield search


async def test_a_track_is_found_at_its_place_on_the_albums_copy(
    music_mass: MusicAssistant,
) -> None:
    """The track at the same place on the album's copy is handed out, whatever its title."""
    with _album_copy_listing(QOBUZ_GREEN_RIVER) as search:
        copy = await music_mass.music.tracks.playable_copy(IHEART_GREEN_RIVER[1])

    assert copy is not None
    assert (copy.provider, copy.item_id) == (QOBUZ, "q12")
    search.assert_not_awaited()


@pytest.mark.parametrize(
    ("track", "copy_tracks"),
    [
        pytest.param(_album_track(IHEART, "t12", 12, 200), QOBUZ_GREEN_RIVER, id="another length"),
        pytest.param(_album_track(IHEART, "t3", 3, 126), QOBUZ_GREEN_RIVER, id="another place"),
        pytest.param(IHEART_GREEN_RIVER[1], QOBUZ_GREEN_RIVER[1:], id="another track count"),
    ],
)
async def test_a_track_the_albums_copy_does_not_confirm_is_searched_for(
    music_mass: MusicAssistant, track: Track, copy_tracks: list[Track]
) -> None:
    """Without the same track count, place and length the track is searched for on its own."""
    with _album_copy_listing(copy_tracks) as search:
        assert await music_mass.music.tracks.playable_copy(track) is None

    assert search.await_count == 2


async def test_an_album_that_can_not_be_looked_up_falls_back_to_the_track_search(
    music_mass: MusicAssistant,
) -> None:
    """When the track's own album can not be fetched, the track is still searched for."""
    with (
        _album_copy_listing(QOBUZ_GREEN_RIVER) as search,
        patch.object(
            AlbumsController,
            "get_provider_item",
            AsyncMock(side_effect=MediaNotFoundError("gone")),
        ),
    ):
        assert await music_mass.music.tracks.playable_copy(IHEART_GREEN_RIVER[1]) is None

    assert search.await_count == 2


@contextmanager
def _refreshed_from_iheart(
    music_mass: MusicAssistant, fetched: Track, copy: Track | None
) -> Iterator[AsyncMock]:
    """
    Refresh with iHeartRadio answering the given track, and the given copy elsewhere.

    :param fetched: The track as iHeartRadio answers it on the refresh.
    :param copy: The copy another streaming service has, None when none has it.
    """
    music_mass.streams = MagicMock()
    music_mass.streams.audio_analysis.delete_measured_loudness = AsyncMock()
    playable_copy = AsyncMock(return_value=copy)
    with (
        patch.object(MusicController, "_visible_provider_for", return_value=PROVIDERS[0]),
        patch.object(TracksController, "get_provider_item", AsyncMock(return_value=fetched)),
        patch.object(TracksController, "playable_copy", playable_copy),
        patch("music_assistant.controllers.music.controller.invalidate_listings", AsyncMock()),
    ):
        yield playable_copy


async def test_refreshing_a_track_its_service_can_not_play_hands_out_another_services_copy(
    music_mass: MusicAssistant,
) -> None:
    """A browsed track its own service lists but can not play is offered from another service."""
    copy = _track(SPOTIFY, "s1")
    with _refreshed_from_iheart(music_mass, _track(available=False), copy):
        assert await music_mass.music.refresh_item(_track(available=False)) is copy


async def test_refreshing_a_track_no_other_service_has_says_so(music_mass: MusicAssistant) -> None:
    """Without a copy elsewhere the refresh tells the user so, rather than doing nothing."""
    with (
        _refreshed_from_iheart(music_mass, _track(available=False), None),
        pytest.raises(MediaNotFoundError) as err,
    ):
        await music_mass.music.refresh_item(_track(available=False))

    assert err.value.translation_key == "no_playable_copy"


async def test_refreshing_a_track_its_service_can_play_searches_nowhere_else(
    music_mass: MusicAssistant,
) -> None:
    """A playable track is refreshed from its own service, as before."""
    with _refreshed_from_iheart(music_mass, _track(), _track(SPOTIFY, "s1")) as playable_copy:
        refreshed = await music_mass.music.refresh_item(_track())

    assert refreshed is not None
    assert refreshed.provider == IHEART
    playable_copy.assert_not_awaited()
