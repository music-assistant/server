"""Tests for Music Quiz source resolution."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.music_quiz import MusicQuizPlugin
from music_assistant.providers.music_quiz.models import MusicQuizConfig
from music_assistant.providers.music_quiz.quiz_types.base import (
    GENRE_TRACK_PAGE_SIZE,
    MAX_GENRE_TRACK_COUNT,
)
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import GuessTheSongQuizType
from music_assistant.providers.music_quiz.quiz_types.hitster import HitsterQuizType

SourceItem = Album | Artist | Genre | ItemMapping | Playlist | Track


def _track(item_id: str, *, year: int | None = None) -> Track:
    """Return a minimal playable track."""
    provider = "provider"
    track = Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=f"artist-{item_id}",
                    provider=provider,
                    name=f"Artist {item_id}",
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
            )
        },
    )
    if year is not None:
        track.metadata.release_date = datetime(year, 1, 1, tzinfo=UTC)
    return track


def _uri(item: SourceItem) -> str:
    """Return the generated URI of a test media item."""
    assert item.uri is not None
    return item.uri


def _playlist(item_id: str = "playlist") -> Playlist:
    """Return a minimal playlist source."""
    return Playlist(
        item_id=item_id,
        provider="provider",
        name="Playlist",
        provider_mappings=set(),
    )


def _album(item_id: str = "album") -> Album:
    """Return a minimal album source."""
    return Album(
        item_id=item_id,
        provider="provider",
        name="Album",
        provider_mappings=set(),
    )


def _artist(item_id: str = "artist") -> Artist:
    """Return a minimal artist source."""
    return Artist(
        item_id=item_id,
        provider="provider",
        name="Artist",
        provider_mappings=set(),
    )


def _genre(item_id: str = "42") -> Genre:
    """Return a minimal library genre source."""
    return Genre(
        item_id=item_id,
        provider="library",
        name="Genre",
        provider_mappings=set(),
    )


async def _yield_tracks(tracks: list[Track]) -> AsyncGenerator[Track]:
    """Yield the provided playlist tracks."""
    for track in tracks:
        yield track


def _mass(source_items: Mapping[str, object]) -> MagicMock:
    """Return a Music Assistant mock with empty source controllers."""
    mass = MagicMock()
    mass.music.get_item_by_uri = AsyncMock(side_effect=source_items.__getitem__)
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([]))
    mass.music.albums.tracks = AsyncMock(return_value=[])
    mass.music.artists.tracks = AsyncMock(return_value=[])
    mass.music.genres.tracks = AsyncMock(return_value=[])
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    mass.music.tracks.get = AsyncMock()
    mass.metadata.get_image_url_for_item = AsyncMock(return_value=None)
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    return mass


def _guess_quiz(mass: MagicMock, source_uris: list[str]) -> GuessTheSongQuizType:
    """Return a Guess the Song quiz for source-resolution tests."""
    return GuessTheSongQuizType(
        mass,
        MusicQuizConfig(
            round_count=1,
            suggestion_count=2,
            source_uris=source_uris,
        ),
    )


@pytest.mark.asyncio
async def test_track_source_resolves_to_itself() -> None:
    """Use a selected track directly without traversing a collection controller."""
    track = _track("one")
    track_uri = _uri(track)
    mass = _mass({track_uri: track})

    pool = await _guess_quiz(mass, [track_uri])._get_source_track_pool()

    assert pool == {track_uri: track}
    mass.music.playlists.tracks.assert_not_called()
    mass.music.albums.tracks.assert_not_awaited()
    mass.music.artists.tracks.assert_not_awaited()
    mass.music.genres.tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_playlist_source_uses_playlist_tracks() -> None:
    """Resolve playlist sources through the playlist track iterator."""
    source = _playlist()
    track = _track("one")
    source_uri = _uri(source)
    track_uri = _uri(track)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([track]))

    pool = await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert pool == {track_uri: track}
    mass.music.playlists.tracks.assert_called_once_with(
        item_id=source.item_id,
        provider_instance_id_or_domain=source.provider,
    )


@pytest.mark.asyncio
async def test_album_source_uses_exact_album_tracks() -> None:
    """Resolve album sources through the album controller."""
    source = _album()
    tracks = [_track("one"), _track("two")]
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.albums.tracks.return_value = tracks

    pool = await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert list(pool.values()) == tracks
    mass.music.albums.tracks.assert_awaited_once_with(
        item_id=source.item_id,
        provider_instance_id_or_domain=source.provider,
    )


@pytest.mark.asyncio
async def test_artist_source_uses_all_artist_tracks() -> None:
    """Resolve artist sources through the finite artist track controller."""
    source = _artist()
    tracks = [_track("one"), _track("two")]
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.artists.tracks.return_value = tracks

    pool = await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert list(pool.values()) == tracks
    mass.music.artists.tracks.assert_awaited_once_with(
        item_id=source.item_id,
        provider_instance_id_or_domain=source.provider,
    )


@pytest.mark.asyncio
async def test_genre_source_uses_exact_genre_tracks_with_bounded_pages() -> None:
    """Resolve exact genre membership in bounded pages."""
    source = _genre()
    tracks = [_track(str(index)) for index in range(GENRE_TRACK_PAGE_SIZE + 1)]
    source_uri = _uri(source)
    mass = _mass({source_uri: source})

    async def _genre_tracks(*, item_id: str, limit: int, offset: int) -> list[Track]:
        assert item_id == source.item_id
        return tracks[offset : offset + limit]

    mass.music.genres.tracks.side_effect = _genre_tracks

    pool = await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert list(pool.values()) == tracks
    assert mass.music.genres.tracks.await_args_list == [
        call(item_id=source.item_id, limit=GENRE_TRACK_PAGE_SIZE, offset=0),
        call(
            item_id=source.item_id,
            limit=GENRE_TRACK_PAGE_SIZE,
            offset=GENRE_TRACK_PAGE_SIZE,
        ),
    ]


@pytest.mark.asyncio
async def test_genre_source_stops_at_controller_default_bound() -> None:
    """Never load more than the established bounded genre result size."""
    source = _genre()
    tracks = [_track(str(index)) for index in range(MAX_GENRE_TRACK_COUNT + 1)]
    source_uri = _uri(source)
    mass = _mass({source_uri: source})

    async def _genre_tracks(*, item_id: str, limit: int, offset: int) -> list[Track]:
        assert item_id == source.item_id
        return tracks[offset : offset + limit]

    mass.music.genres.tracks.side_effect = _genre_tracks

    pool = await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert len(pool) == MAX_GENRE_TRACK_COUNT
    assert mass.music.genres.tracks.await_count == (MAX_GENRE_TRACK_COUNT // GENRE_TRACK_PAGE_SIZE)
    mass.music.genres.tracks.assert_awaited_with(
        item_id=source.item_id,
        limit=GENRE_TRACK_PAGE_SIZE,
        offset=MAX_GENRE_TRACK_COUNT - GENRE_TRACK_PAGE_SIZE,
    )


@pytest.mark.asyncio
async def test_mixed_sources_deduplicate_and_skip_failure() -> None:
    """Deduplicate track URIs while allowing another source to fail."""
    direct = _track("shared")
    album = _album()
    extra = _track("extra")
    direct_uri = _uri(direct)
    album_uri = _uri(album)
    extra_uri = _uri(extra)
    mass = _mass({direct_uri: direct, album_uri: album})
    mass.music.get_item_by_uri.side_effect = [
        RuntimeError("unavailable"),
        direct,
        album,
    ]
    mass.music.albums.tracks.return_value = [direct, extra]

    pool = await _guess_quiz(
        mass,
        ["provider://playlist/unavailable", direct_uri, album_uri],
    )._get_source_track_pool()

    assert list(pool) == [direct_uri, extra_uri]


@pytest.mark.parametrize(
    "source_uris",
    [
        [],
        [
            "provider://radio/one",
            "provider://podcast/two",
            "provider://podcast_episode/three",
            "provider://audiobook/four",
        ],
    ],
)
@pytest.mark.asyncio
async def test_empty_or_unsupported_sources_raise_localized_error(source_uris: list[str]) -> None:
    """Reject unsupported-only selections without traversing their controllers."""
    source_items = {
        source_uri: ItemMapping(
            media_type=media_type,
            item_id=str(index),
            provider="provider",
            name=media_type.value,
        )
        for index, (source_uri, media_type) in enumerate(
            zip(
                source_uris,
                (
                    MediaType.RADIO,
                    MediaType.PODCAST,
                    MediaType.PODCAST_EPISODE,
                    MediaType.AUDIOBOOK,
                ),
                strict=False,
            )
        )
    }
    mass = _mass(source_items)

    with pytest.raises(InvalidDataError) as err:
        await _guess_quiz(mass, source_uris)._get_source_track_pool()

    assert err.value.translation_key == "music_quiz_sources_unavailable"
    mass.music.get_item_by_uri.assert_not_awaited()
    mass.music.playlists.tracks.assert_not_called()
    mass.music.albums.tracks.assert_not_awaited()
    mass.music.artists.tracks.assert_not_awaited()
    mass.music.genres.tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_source_metadata_ignores_unsupported_types() -> None:
    """Expose metadata only for accepted Music Quiz source types."""
    playlist = _playlist()
    radio = ItemMapping(
        media_type=MediaType.RADIO,
        item_id="radio",
        provider="provider",
        name="Radio",
    )
    plugin = MusicQuizPlugin.__new__(MusicQuizPlugin)
    playlist_uri = _uri(playlist)
    radio_uri = _uri(radio)
    plugin.mass = _mass({playlist_uri: playlist, radio_uri: radio})
    plugin.logger = MagicMock()

    sources = await plugin._resolve_sources(
        [playlist_uri, radio_uri, "provider://podcast/unavailable", "not-a-uri"]
    )

    assert [source.to_dict() for source in sources] == [
        {
            "uri": playlist_uri,
            "name": playlist.name,
            "media_type": MediaType.PLAYLIST.value,
        }
    ]
    plugin.mass.music.get_item_by_uri.assert_awaited_once_with(playlist_uri)


@pytest.mark.asyncio
async def test_guess_the_song_prepares_from_album_source() -> None:
    """Prepare Guess the Song unchanged from an expanded source collection."""
    album = _album()
    source_track = _track("source")
    distractor = _track("distractor")
    album_uri = _uri(album)
    source_track_uri = _uri(source_track)
    mass = _mass({album_uri: album})
    mass.music.albums.tracks.return_value = [source_track]
    mass.music.search.return_value = SimpleNamespace(tracks=[distractor])
    quiz = _guess_quiz(mass, [album_uri])

    game_round = await quiz.prepare_round(0, [])

    assert game_round.track_uri == source_track_uri
    assert game_round.answer_label == f"{source_track.artist_str} - {source_track.name}"


@pytest.mark.asyncio
async def test_hitster_prepares_from_artist_source() -> None:
    """Prepare Hitster unchanged from an expanded source collection."""
    artist = _artist()
    tracks = [_track("one", year=1990), _track("two", year=2000)]
    artist_uri = _uri(artist)
    mass = _mass({artist_uri: artist})
    mass.music.artists.tracks.return_value = tracks
    quiz = HitsterQuizType(
        mass,
        MusicQuizConfig(round_count=1, source_uris=[artist_uri]),
    )

    await quiz.initialize()
    game_round = await quiz.prepare_round(0, [])

    assert game_round.track_uri in {track.uri for track in tracks}
    mass.music.tracks.get.assert_not_awaited()
