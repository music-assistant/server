"""Tests for Music Quiz source resolution."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

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
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceRoundState,
    MusicQuizConfig,
    TimelineRoundState,
)
from music_assistant.providers.music_quiz.quiz_types.base import (
    GENRE_TRACK_PAGE_SIZE,
    MAX_GENRE_TRACK_COUNT,
)
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import GuessTheSongQuizType
from music_assistant.providers.music_quiz.quiz_types.music_timeline import MusicTimelineQuizType
from music_assistant.providers.music_quiz.quiz_types.trivia import (
    TriviaGeneration,
    TriviaQuizType,
)

SourceItem = Album | Artist | Genre | ItemMapping | Playlist | Track


def _track(
    item_id: str,
    *,
    year: int | None = None,
    available: bool = True,
    is_playable: bool = True,
) -> Track:
    """Return a minimal playable track."""
    provider = "provider"
    track = Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        duration=180,
        is_playable=is_playable,
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
                available=available,
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

    async def _get_item(
        *,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
        allow_update_metadata: bool,
    ) -> object:
        assert allow_update_metadata is False
        uri = f"{provider_instance_id_or_domain}://{media_type.value}/{item_id}"
        return source_items[uri]

    mass.music.get_item = AsyncMock(side_effect=_get_item)
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([]))
    mass.music.albums.tracks = AsyncMock(return_value=[])
    mass.music.artists.tracks = AsyncMock(return_value=[])
    mass.music.genres.tracks = AsyncMock(return_value=[])
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    mass.music.tracks.get = AsyncMock()
    mass.metadata.get_image_url_for_item = AsyncMock(return_value=None)
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    mass.get_provider = MagicMock(return_value=None)
    mass.player_queues = MagicMock()
    return mass


def _guess_quiz(
    mass: MagicMock,
    source_uris: list[str],
    *,
    round_count: int = 1,
    suggestion_count: int = 2,
    include_similar_music: bool = False,
) -> GuessTheSongQuizType:
    """Return a Guess the Song quiz for source-resolution tests."""
    return GuessTheSongQuizType(
        mass,
        MusicQuizConfig(
            round_count=round_count,
            suggestion_count=suggestion_count,
            source_uris=source_uris,
            include_similar_music=include_similar_music,
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
    mass.music.get_item.side_effect = [
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


@pytest.mark.asyncio
async def test_default_source_pool_does_not_request_similar_music() -> None:
    """Keep the complete exact playlist pool unchanged when expansion is disabled."""
    source = _playlist()
    tracks = [_track("one"), _track("two")]
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks(tracks))
    radio_provider = MagicMock()
    radio_provider.get_dynamic_tracks = AsyncMock(return_value=[_track("similar")])
    mass.get_provider.return_value = radio_provider

    pool = await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert list(pool.values()) == tracks
    mass.get_provider.assert_not_called()
    radio_provider.get_dynamic_tracks.assert_not_awaited()


@pytest.mark.parametrize(
    ("round_count", "suggestion_count", "expected_target"),
    [(1, 2, 25), (100, 12, 50)],
)
@pytest.mark.asyncio
async def test_similar_music_uses_resolved_seeds_and_bounded_target(
    round_count: int,
    suggestion_count: int,
    expected_target: int,
) -> None:
    """Call radio once with selected media seeds and no playback-queue state."""
    source = _playlist()
    exact = _track("exact")
    expanded = _track("expanded")
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([exact]))
    radio_provider = MagicMock()
    radio_provider.get_dynamic_tracks = AsyncMock(return_value=[expanded])
    mass.get_provider.return_value = radio_provider
    quiz = _guess_quiz(
        mass,
        [source_uri],
        round_count=round_count,
        suggestion_count=suggestion_count,
        include_similar_music=True,
    )

    pool = await quiz._get_source_track_pool()
    cached_pool = await quiz._get_source_track_pool()

    assert cached_pool is pool
    assert list(pool.values()) == [exact, expanded]
    radio_provider.get_dynamic_tracks.assert_awaited_once_with(
        [source],
        include_base_tracks=False,
        target_size=expected_target,
        preferred_provider_instances=["provider"],
    )
    assert mass.player_queues.mock_calls == []


@pytest.mark.asyncio
async def test_similar_music_preserves_exact_tracks_and_filters_expansion() -> None:
    """Retain exact tracks while deduplicating and filtering radio candidates."""
    source = _playlist()
    exact = _track("exact")
    duplicate = _track("exact")
    expanded = _track("expanded")
    unavailable = _track("unavailable", available=False)
    unplayable = _track("unplayable", is_playable=False)
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([exact]))
    radio_provider = MagicMock()
    radio_provider.get_dynamic_tracks = AsyncMock(
        return_value=[duplicate, expanded, unavailable, unplayable]
    )
    mass.get_provider.return_value = radio_provider

    pool = await _guess_quiz(
        mass,
        [source_uri],
        include_similar_music=True,
    )._get_source_track_pool()

    assert list(pool.values()) == [exact, expanded]
    assert pool[_uri(exact)] is exact


@pytest.mark.parametrize("radio_mode", ["missing", "empty", "filtered", "error"])
@pytest.mark.asyncio
async def test_similar_music_failure_falls_back_to_exact_pool(
    radio_mode: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Treat unavailable, empty, and failed radio expansion as optional."""
    caplog.set_level(logging.DEBUG)
    source = _playlist()
    exact = _track("exact")
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([exact]))
    if radio_mode != "missing":
        radio_provider = MagicMock()
        radio_provider.get_dynamic_tracks = AsyncMock(
            return_value=(
                [_track("unavailable", available=False)] if radio_mode == "filtered" else []
            ),
            side_effect=RuntimeError("radio failed") if radio_mode == "error" else None,
        )
        mass.get_provider.return_value = radio_provider

    pool = await _guess_quiz(
        mass,
        [source_uri],
        include_similar_music=True,
    )._get_source_track_pool()

    assert pool == {_uri(exact): exact}
    assert any(
        "similar" in record.message.lower() or "radio" in record.message.lower()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_guess_the_song_can_select_an_expanded_track() -> None:
    """Apply normal distractor validation when an expanded track is selected."""
    source = _playlist()
    exact = _track("exact")
    expanded = _track("expanded")
    distractor = _track("distractor")
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([exact]))
    mass.music.search.return_value = SimpleNamespace(tracks=[distractor])
    radio_provider = MagicMock()
    radio_provider.get_dynamic_tracks = AsyncMock(return_value=[expanded])
    mass.get_provider.return_value = radio_provider
    quiz = _guess_quiz(
        mass,
        [source_uri],
        include_similar_music=True,
    )

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
        return_value=expanded,
    ):
        game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    assert game_round.track_uri == expanded.uri
    assert len(game_round.answer_state.suggestions) == 2
    assert {suggestion.uri for suggestion in game_round.answer_state.suggestions} == {
        expanded.uri,
        distractor.uri,
    }


@pytest.mark.asyncio
async def test_music_timeline_applies_date_rules_to_expanded_tracks() -> None:
    """Select dated expansion while excluding expanded tracks without a usable year."""
    source = _playlist()
    exact = _track("exact", year=1990)
    expanded = _track("expanded", year=2000)
    undated = _track("undated")
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([exact]))
    mass.music.tracks.get.return_value = undated
    radio_provider = MagicMock()
    radio_provider.get_dynamic_tracks = AsyncMock(return_value=[expanded, undated])
    mass.get_provider.return_value = radio_provider
    quiz = MusicTimelineQuizType(
        mass,
        MusicQuizConfig(
            round_count=1,
            source_uris=[source_uri],
            include_similar_music=True,
        ),
    )

    await quiz.initialize()
    eligible_tracks = await quiz._get_eligible_tracks()
    game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert {track.uri for track in eligible_tracks} == {exact.uri, expanded.uri}
    assert quiz._source_track_pool is not None
    assert undated.uri in quiz._source_track_pool
    assert {
        game_round.answer_state.placement_snapshot[0].track_uri,
        game_round.answer_state.candidate.entry.track_uri,
    } == {exact.uri, expanded.uri}


@pytest.mark.asyncio
async def test_trivia_applies_grounding_rules_to_expanded_tracks() -> None:
    """Select grounded expansion while excluding candidates without factual context."""
    source = _playlist()
    exact = _track("exact")
    expanded = _track("expanded")
    ungrounded = Track(
        item_id="ungrounded",
        provider="provider",
        name="Ungrounded",
        provider_mappings={
            ProviderMapping(
                item_id="ungrounded",
                provider_domain="provider",
                provider_instance="provider",
            )
        },
    )
    source_uri = _uri(source)
    mass = _mass({source_uri: source})
    mass.music.playlists.tracks = MagicMock(return_value=_yield_tracks([exact]))
    radio_provider = MagicMock()
    radio_provider.get_dynamic_tracks = AsyncMock(return_value=[expanded, ungrounded])
    mass.get_provider.return_value = radio_provider
    quiz = TriviaQuizType(
        mass,
        MusicQuizConfig(
            round_count=1,
            suggestion_count=2,
            source_uris=[source_uri],
            include_similar_music=True,
        ),
    )

    eligible_tracks = await quiz._get_eligible_tracks()
    generation = TriviaGeneration(
        question="Which artist recorded this track?",
        wrong_answers=("Different Artist",),
    )
    with (
        patch.object(quiz, "_generate_question", new=AsyncMock(return_value=generation)),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.trivia.SYSTEM_RANDOM.choice",
            return_value=eligible_tracks[_uri(expanded)],
        ),
    ):
        game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    assert set(eligible_tracks) == {exact.uri, expanded.uri}
    assert game_round.track_uri == expanded.uri
    correct = next(
        suggestion for suggestion in game_round.answer_state.suggestions if suggestion.is_correct
    )
    assert correct.uri == expanded.uri


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
    mass.music.get_item.assert_not_awaited()
    mass.music.playlists.tracks.assert_not_called()
    mass.music.albums.tracks.assert_not_awaited()
    mass.music.artists.tracks.assert_not_awaited()
    mass.music.genres.tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_library_genre_source_raises_localized_error() -> None:
    """Reject provider genre URIs before resolving or traversing them."""
    source_uri = "provider://genre/42"
    mass = _mass({})

    with pytest.raises(InvalidDataError) as err:
        await _guess_quiz(mass, [source_uri])._get_source_track_pool()

    assert err.value.translation_key == "music_quiz_sources_unavailable"
    mass.music.get_item.assert_not_awaited()
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
        [
            playlist_uri,
            radio_uri,
            "provider://genre/42",
            "provider://podcast/unavailable",
            "not-a-uri",
        ]
    )

    assert [source.to_dict() for source in sources] == [
        {
            "uri": playlist_uri,
            "name": playlist.name,
            "media_type": MediaType.PLAYLIST.value,
        }
    ]
    plugin.mass.music.get_item.assert_awaited_once_with(
        media_type=MediaType.PLAYLIST,
        item_id=playlist.item_id,
        provider_instance_id_or_domain=playlist.provider,
        allow_update_metadata=False,
    )


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
async def test_music_timeline_prepares_from_artist_source() -> None:
    """Prepare Music Timeline unchanged from an expanded source collection."""
    artist = _artist()
    tracks = [_track("one", year=1990), _track("two", year=2000)]
    artist_uri = _uri(artist)
    mass = _mass({artist_uri: artist})
    mass.music.artists.tracks.return_value = tracks
    quiz = MusicTimelineQuizType(
        mass,
        MusicQuizConfig(round_count=1, source_uris=[artist_uri]),
    )

    await quiz.initialize()
    game_round = await quiz.prepare_round(0, [])

    assert game_round.track_uri in {track.uri for track in tracks}
    mass.music.tracks.get.assert_not_awaited()
