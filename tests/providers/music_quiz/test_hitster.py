"""Tests for the Hitster Music Quiz type."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.models import (
    MusicQuizConfig,
    MusicQuizDifficulty,
    TimelineBonusMode,
    TimelineBonusType,
    TimelineFreeTextBonusDefinition,
    TimelineMultipleChoiceBonusDefinition,
    TimelineRoundState,
)
from music_assistant.providers.music_quiz.quiz_types.hitster import (
    AI_QUERY_TIMEOUT_SECONDS,
    DEFAULT_BONUS_OPTION_COUNT,
    TRACK_ENRICHMENT_CONCURRENCY,
    HitsterQuizType,
)
from music_assistant.providers.music_quiz.suggestions import SuggestionCandidate


def _track(
    item_id: str,
    name: str,
    artist: str,
    *,
    album_year: int | None = None,
    release_year: int | None = None,
    available: bool = True,
    is_playable: bool = True,
) -> Track:
    """Return a track with configurable album and metadata years."""
    provider = "prov"
    track = Track(
        item_id=item_id,
        provider=provider,
        name=name,
        is_playable=is_playable,
        duration=180,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=f"artist-{item_id}",
                    provider=provider,
                    name=artist,
                )
            ]
        ),
        album=ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=f"album-{item_id}",
            provider=provider,
            name=f"Album {item_id}",
            year=album_year,
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
    if release_year is not None:
        track.metadata.release_date = datetime(release_year, 1, 1, tzinfo=UTC)
    return track


def _artist(item_id: str, name: str) -> ItemMapping:
    """Return a catalog artist mapping."""
    return ItemMapping(
        media_type=MediaType.ARTIST,
        item_id=item_id,
        provider="prov",
        name=name,
    )


def _ai_provider(
    response: str | None = None,
    error: Exception | None = None,
) -> MagicMock:
    """Return a mock AI-query plugin provider."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "ai--test"
    provider.ai_query = AsyncMock(return_value=response, side_effect=error)
    return provider


def _mass() -> MagicMock:
    """Return a mock MusicAssistant for Hitster tests."""
    mass = MagicMock()
    mass.metadata.get_image_url_for_item = AsyncMock(
        side_effect=lambda track: f"https://img/{track.item_id}"
    )
    mass.music.tracks.get = AsyncMock()
    mass.music.tracks.similar_tracks = AsyncMock(return_value=[])
    mass.music.artists.similar_artists = AsyncMock(return_value=[])
    mass.music.artists.tracks = AsyncMock(return_value=[])
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    return mass


def _quiz(
    tracks: list[Track],
    *,
    round_count: int = 1,
    artist_mode: TimelineBonusMode = TimelineBonusMode.OFF,
    title_mode: TimelineBonusMode = TimelineBonusMode.OFF,
    use_ai: bool = False,
) -> tuple[HitsterQuizType, MagicMock]:
    """Return a Hitster type backed by a deterministic source pool."""
    mass = _mass()
    config = MusicQuizConfig(
        round_count=round_count,
        source_uris=["prov://playlist/1"],
        artist_bonus_mode=artist_mode,
        title_bonus_mode=title_mode,
        use_ai_distractors=use_ai,
    )
    quiz = HitsterQuizType(mass, config)
    quiz._source_track_pool = {track.uri: track for track in tracks if track.uri}
    return quiz, mass


def test_config_normalization_and_validation_are_hitster_specific() -> None:
    """Use fixed option defaults without requiring guess-the-song settings."""
    config = MusicQuizConfig(
        round_count=2,
        suggestion_count=9,
        source_uris=["prov://playlist/1"],
        difficulty="not-used",
        use_ai_distractors=True,
        artist_bonus_mode=TimelineBonusMode.FREE_TEXT,
        title_bonus_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )

    normalized = HitsterQuizType.normalize_config(config)
    HitsterQuizType.validate_config(normalized)

    assert normalized.suggestion_count == DEFAULT_BONUS_OPTION_COUNT
    assert normalized.difficulty == MusicQuizDifficulty.NORMAL.value
    assert normalized.use_ai_distractors is True
    with pytest.raises(InvalidDataError, match="bonus mode"):
        HitsterQuizType.validate_config(
            MusicQuizConfig(
                source_uris=["prov://playlist/1"],
                artist_bonus_mode=cast("TimelineBonusMode", "invalid"),
            )
        )


@pytest.mark.asyncio
async def test_initialize_requires_anchor_plus_unique_scored_tracks() -> None:
    """Validate the complete pool without retaining an ephemeral track sequence."""
    tracks = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Genesis", "Justice", album_year=2007),
        _track("three", "Lisztomania", "Phoenix", album_year=2009),
        _track("four", "Sexy Boy", "Air", album_year=1998),
    ]
    quiz, _ = _quiz(tracks, round_count=3)

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    assert len(quiz._eligible_tracks) == 4
    assert not hasattr(quiz, "_selected_tracks")


@pytest.mark.asyncio
async def test_initialize_rejects_missing_or_insufficient_dated_tracks() -> None:
    """Raise localized errors when real release years cannot fill the game."""
    missing = _track("missing", "Unknown", "Artist")
    quiz, mass = _quiz([missing])
    mass.music.tracks.get.return_value = missing

    with pytest.raises(InvalidDataError) as no_dated:
        await quiz.initialize()
    assert no_dated.value.translation_key == "music_quiz_no_dated_tracks"

    tracks = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Genesis", "Justice", album_year=2007),
    ]
    quiz, _ = _quiz(tracks, round_count=2)
    with pytest.raises(InvalidDataError) as insufficient:
        await quiz.initialize()
    assert insufficient.value.translation_key == "music_quiz_not_enough_dated_tracks"


@pytest.mark.asyncio
async def test_initialize_excludes_unavailable_and_unplayable_dated_tracks() -> None:
    """Never count or select dated tracks that cannot actually be played."""
    playable = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Genesis", "Justice", album_year=2007),
    ]
    unavailable = _track(
        "unavailable",
        "Unavailable",
        "Artist",
        album_year=2001,
        available=False,
    )
    unplayable = _track(
        "unplayable",
        "Unplayable",
        "Artist",
        album_year=2002,
        is_playable=False,
    )
    quiz, mass = _quiz([*playable, unavailable, unplayable])

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    assert {track.item_id for track in quiz._eligible_tracks} == {"one", "two"}
    mass.music.tracks.get.assert_not_awaited()

    insufficient_quiz, _ = _quiz([playable[0], unavailable, unplayable])
    with pytest.raises(InvalidDataError) as insufficient:
        await insufficient_quiz.initialize()
    assert insufficient.value.translation_key == "music_quiz_not_enough_dated_tracks"


@pytest.mark.asyncio
async def test_partial_playlist_tracks_are_enriched_through_track_api() -> None:
    """Fetch full track details when a playlist item lacks release metadata."""
    partial = _track("partial", "Teardrop", "Massive Attack")
    enriched = _track("partial", "Teardrop", "Massive Attack", album_year=1998)
    other = _track("other", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([partial, other])
    mass.music.tracks.get.return_value = enriched

    await quiz.initialize()

    mass.music.tracks.get.assert_awaited_once_with(partial.item_id, partial.provider)
    assert quiz._eligible_tracks is not None
    assert {track.item_id for track in quiz._eligible_tracks} == {"partial", "other"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available", "is_playable"),
    [(False, True), (True, False)],
)
async def test_enriched_tracks_must_still_be_available_and_playable(
    available: bool,
    is_playable: bool,
) -> None:
    """Exclude enriched details when they reveal an unusable source track."""
    partial = _track("partial", "Partial", "Artist")
    enriched = _track(
        "partial",
        "Partial",
        "Artist",
        album_year=2000,
        available=available,
        is_playable=is_playable,
    )
    playable = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Genesis", "Justice", album_year=2007),
    ]
    quiz, mass = _quiz([partial, *playable])
    mass.music.tracks.get.return_value = enriched

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    assert {track.item_id for track in quiz._eligible_tracks} == {"one", "two"}


@pytest.mark.asyncio
async def test_track_enrichment_concurrency_is_bounded() -> None:
    """Process large undated pools in bounded batches."""
    tracks = [_track(f"track-{index}", f"Track {index}", f"Artist {index}") for index in range(25)]
    tracks_by_id = {track.item_id: track for track in tracks}
    quiz, mass = _quiz(tracks)
    active_calls = 0
    max_active_calls = 0

    async def _get_track(item_id: str, _provider: str) -> Track:
        nonlocal active_calls, max_active_calls
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0)
        active_calls -= 1
        return tracks_by_id[item_id]

    mass.music.tracks.get.side_effect = _get_track

    with pytest.raises(InvalidDataError):
        await quiz.initialize()

    assert mass.music.tracks.get.await_count == len(tracks)
    assert max_active_calls <= TRACK_ENRICHMENT_CONCURRENCY


def test_release_year_prefers_album_and_falls_back_to_track_metadata() -> None:
    """Resolve usable years from Album, ItemMapping and track release metadata."""
    mapped = _track("mapped", "Mapped", "Artist", album_year=1999, release_year=2001)
    full_album = Album(
        item_id="album",
        provider="prov",
        name="Full Album",
        year=2000,
        provider_mappings={
            ProviderMapping(
                item_id="album",
                provider_domain="prov",
                provider_instance="prov",
            )
        },
    )
    full = _track("full", "Full", "Artist", release_year=2002)
    full.album = full_album
    fallback = _track("fallback", "Fallback", "Artist", release_year=2003)
    future = _track(
        "future",
        "Future",
        "Artist",
        album_year=datetime.now(tz=UTC).year + 1,
        release_year=2004,
    )

    assert HitsterQuizType._release_year(mapped) == 1999
    assert HitsterQuizType._release_year(full) == 2000
    assert HitsterQuizType._release_year(fallback) == 2003
    assert HitsterQuizType._release_year(future) == 2004


@pytest.mark.asyncio
async def test_prepare_round_seeds_anchor_and_prefetches_guaranteed_future_snapshot() -> None:
    """Build the next snapshot with the current song before that song is revealed."""
    anchor = _track("anchor", "Teardrop", "Massive Attack", album_year=1998)
    first = _track("first", "Genesis", "Justice", album_year=2007)
    second = _track("second", "Lisztomania", "Phoenix", album_year=2009)
    quiz, _ = _quiz([anchor, first, second], round_count=2)
    quiz._eligible_tracks = [anchor, first, second]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.hitster.secrets.choice",
        side_effect=[anchor, first],
    ):
        first_round = await quiz.prepare_round(0, [])
    reconnected_quiz, _ = _quiz([anchor, first, second], round_count=2)
    reconnected_quiz._eligible_tracks = [anchor, first, second]
    with patch(
        "music_assistant.providers.music_quiz.quiz_types.hitster.secrets.choice",
        return_value=second,
    ):
        second_round = await reconnected_quiz.prepare_round(1, [first_round])

    assert isinstance(first_round.answer_state, TimelineRoundState)
    assert isinstance(second_round.answer_state, TimelineRoundState)
    anchor_entry = first_round.answer_state.placement_snapshot[0]
    assert anchor_entry.is_anchor is True
    assert anchor_entry.track_uri == anchor.uri
    assert first_round.track_uri == first.uri
    assert [entry.track_uri for entry in second_round.answer_state.placement_snapshot] == [
        anchor.uri,
        first.uri,
    ]
    assert second_round.track_uri == second.uri
    assert (
        len(
            {
                entry.entry_id
                for entry in [
                    *second_round.answer_state.placement_snapshot,
                    second_round.answer_state.candidate.entry,
                ]
            }
        )
        == 3
    )


@pytest.mark.asyncio
async def test_prepare_round_builds_free_text_and_opaque_multiple_choice_bonuses() -> None:
    """Build independent bonus modes with four opaque, distinct MC options."""
    tracks = [
        _track("anchor", "Teardrop", "Massive Attack", album_year=1998),
        _track("current", "Genesis", "Justice", album_year=2007),
        _track("one", "Lisztomania", "Phoenix", album_year=2009),
        _track("two", "Sexy Boy", "Air", album_year=1998),
        _track("three", "Glory Box", "Portishead", album_year=1994),
    ]
    quiz, mass = _quiz(
        tracks,
        artist_mode=TimelineBonusMode.FREE_TEXT,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = tracks

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.hitster.secrets.choice",
        side_effect=tracks[:2],
    ):
        game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    artist_definition, title_definition = game_round.answer_state.bonus_definitions
    assert isinstance(artist_definition, TimelineFreeTextBonusDefinition)
    assert artist_definition.bonus_type == TimelineBonusType.ARTIST
    assert game_round.answer_state.candidate.artist_answers == ["Justice"]
    assert game_round.answer_state.candidate.title_answers == ["Genesis"]
    assert isinstance(title_definition, TimelineMultipleChoiceBonusDefinition)
    assert title_definition.bonus_type == TimelineBonusType.TITLE
    assert len(title_definition.options) == 4
    assert sum(option.is_correct for option in title_definition.options) == 1
    assert len({option.option_id for option in title_definition.options}) == 4
    assert all("correct" not in option.option_id for option in title_definition.options)
    mass.music.artists.tracks.assert_awaited_once()
    mass.music.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_artist_bonus_prefers_similar_artists_and_excludes_aliases() -> None:
    """Build artist options from similar artists without accepted aliases."""
    current = _track("current", "Get Lucky", "Daft Punk", album_year=2013)
    current.artists[0].sort_name = "Punk, Daft"
    current.artists.append(_artist("pharrell", "Pharrell Williams"))
    fallback_tracks = [
        _track("anchor", "Teardrop", "Massive Attack", album_year=1998),
        _track("fallback", "Glory Box", "Portishead", album_year=1994),
    ]
    quiz, mass = _quiz(
        [current, *fallback_tracks],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, *fallback_tracks]
    mass.music.artists.similar_artists.return_value = [
        _artist("alias-sort", "Punk, Daft"),
        _artist("alias-feature", "Pharrell Williams"),
        _artist("justice", "Justice"),
        _artist("air", "Air"),
        _artist("phoenix", "Phoenix"),
        _artist("duplicate-air", "air"),
    ]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Justice",
        "Air",
        "Phoenix",
    }
    assert len({option.option_id for option in options}) == DEFAULT_BONUS_OPTION_COUNT
    mass.music.artists.similar_artists.assert_awaited_once_with(
        item_id=current.artists[0].item_id,
        provider_instance_id_or_domain=current.artists[0].provider,
        limit=24,
    )
    mass.music.tracks.similar_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_artist_bonus_uses_similar_tracks_before_source_fallback() -> None:
    """Supplement similar artists with related-track artists before source artists."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_fallback = [
        _track("fallback-one", "Teardrop", "Massive Attack", album_year=1998),
        _track("fallback-two", "Glory Box", "Portishead", album_year=1994),
    ]
    quiz, mass = _quiz(
        [current, *source_fallback],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, *source_fallback]
    mass.music.artists.similar_artists.return_value = [_artist("daft-punk", "Daft Punk")]
    mass.music.tracks.similar_tracks.return_value = [
        _track("similar-track", "Sexy Boy", "Air"),
    ]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    wrong_labels = {option.label for option in options if not option.is_correct}
    assert {"Daft Punk", "Air"} <= wrong_labels
    assert len(wrong_labels & {"Massive Attack", "Portishead"}) == 1
    mass.music.tracks.similar_tracks.assert_awaited_once()


@pytest.mark.asyncio
async def test_artist_bonus_supplements_pairwise_close_similar_artists() -> None:
    """Do not count mutually ambiguous artist names as distinct options."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_fallback = _track("fallback", "Glory Box", "Portishead", album_year=1994)
    quiz, mass = _quiz(
        [current, source_fallback],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, source_fallback]
    mass.music.artists.similar_artists.return_value = [
        _artist("massive-attack", "Massive Attack"),
        _artist("massive-attack-collective", "Massive Attack Collective"),
        _artist("daft-punk", "Daft Punk"),
    ]
    mass.music.tracks.similar_tracks.return_value = [
        _track("similar-track", "Sexy Boy", "Air"),
    ]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    wrong_labels = {option.label for option in options if not option.is_correct}
    assert "Air" in wrong_labels
    assert "Portishead" not in wrong_labels
    assert len(wrong_labels & {"Massive Attack", "Massive Attack Collective"}) == 1
    mass.music.tracks.similar_tracks.assert_awaited_once()


@pytest.mark.asyncio
async def test_title_bonus_prefers_same_artist_source_tracks_and_excludes_versions() -> None:
    """Use same-artist source titles while excluding the current song and close versions."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    same_artist_tracks = [
        _track("close", "Genesis (Remix)", "Justice", album_year=2008),
        _track("one", "D.A.N.C.E.", "Justice", album_year=2007),
        _track("two", "Phantom", "Justice", album_year=2007),
        _track("three", "Audio, Video, Disco", "Justice", album_year=2011),
    ]
    unrelated = _track("unrelated", "Teardrop", "Massive Attack", album_year=1998)
    quiz, mass = _quiz(
        [current, *same_artist_tracks, unrelated],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, *same_artist_tracks, unrelated]

    options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "D.A.N.C.E.",
        "Phantom",
        "Audio, Video, Disco",
    }
    mass.music.artists.tracks.assert_not_awaited()
    mass.music.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_title_bonus_uses_artist_catalog_before_unrelated_source_tracks() -> None:
    """Fill sparse same-artist source titles from the artist controller first."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_same_artist = _track("source-same", "D.A.N.C.E.", "Justice", album_year=2007)
    unrelated = _track("unrelated", "Teardrop", "Massive Attack", album_year=1998)
    quiz, mass = _quiz(
        [current, source_same_artist, unrelated],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, source_same_artist, unrelated]
    mass.music.artists.tracks.return_value = [
        _track("catalog-one", "Phantom", "Justice"),
        _track("catalog-two", "Audio, Video, Disco", "Justice"),
    ]

    options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "D.A.N.C.E.",
        "Phantom",
        "Audio, Video, Disco",
    }
    assert "Teardrop" not in {option.label for option in options}
    mass.music.artists.tracks.assert_awaited_once()
    mass.music.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_ranking_only_reorders_grounded_bonus_candidates() -> None:
    """Map a strict AI ranking back to the supplied catalog candidates."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([current], use_ai=True)
    provider = _ai_provider('{"ranked_ids":["candidate_2","candidate_0","candidate_1"]}')
    mass.get_providers_supporting_feature.return_value = [provider]
    candidates = [
        SuggestionCandidate("Daft Punk"),
        SuggestionCandidate("Air"),
        SuggestionCandidate("Phoenix"),
    ]

    ranked = await quiz._rank_bonus_candidates(
        current,
        TimelineBonusType.ARTIST,
        candidates,
    )

    assert [candidate.label for candidate in ranked] == ["Phoenix", "Daft Punk", "Air"]
    prompt = provider.ai_query.await_args.args[0]
    assert all(candidate.label in prompt for candidate in candidates)
    assert "candidate_3" not in prompt


@pytest.mark.asyncio
async def test_ai_ranking_cannot_replace_sufficient_similar_artists_with_fallback() -> None:
    """Keep sufficient catalog ordering when AI ranking makes it ambiguous."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_fallback = _track("fallback", "Glory Box", "Portishead", album_year=1994)
    quiz, mass = _quiz(
        [current, source_fallback],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current, source_fallback]
    mass.music.artists.similar_artists.return_value = [
        _artist("massive-attack", "Massive Attack"),
        _artist("attack-collective", "Attack Collective"),
        _artist("daft-punk", "Daft Punk"),
        _artist("massive-attack-collective", "Massive Attack Collective"),
    ]
    provider = _ai_provider(
        '{"ranked_ids":["candidate_3","candidate_0","candidate_1","candidate_2"]}'
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Massive Attack",
        "Attack Collective",
        "Daft Punk",
    }
    assert "Portishead" not in {option.label for option in options}
    mass.music.tracks.similar_tracks.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"ranked_ids":["candidate_0","candidate_1","invented"]}',
        '{"ranked_ids":["candidate_0","candidate_1"]}',
    ],
)
async def test_invalid_ai_ranking_falls_back_to_catalog_order(response: str) -> None:
    """Keep deterministic catalog ordering when AI output is invalid."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([current], use_ai=True)
    mass.get_providers_supporting_feature.return_value = [_ai_provider(response)]
    candidates = [
        SuggestionCandidate("Daft Punk"),
        SuggestionCandidate("Air"),
        SuggestionCandidate("Phoenix"),
    ]

    ranked = await quiz._rank_bonus_candidates(
        current,
        TimelineBonusType.ARTIST,
        candidates,
    )

    assert ranked == candidates


@pytest.mark.asyncio
async def test_invalid_primary_ai_provider_does_not_try_another_provider() -> None:
    """Keep catalog order immediately when the deterministic AI provider fails."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([current], use_ai=True)
    invalid = _ai_provider("not json")
    invalid.instance_id = "ai--a"
    later = _ai_provider('{"ranked_ids":["candidate_1","candidate_0"]}')
    later.instance_id = "ai--b"
    mass.get_providers_supporting_feature.return_value = [later, invalid]
    candidates = [SuggestionCandidate("Daft Punk"), SuggestionCandidate("Air")]

    ranked = await quiz._rank_bonus_candidates(
        current,
        TimelineBonusType.ARTIST,
        candidates,
    )

    assert ranked == candidates
    invalid.ai_query.assert_awaited_once()
    later.ai_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_ranking_timeout_falls_back_to_catalog_order() -> None:
    """Keep catalog ordering when an AI provider exceeds the ranking deadline."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([current], use_ai=True)
    provider = _ai_provider()

    async def _stall(_prompt: str) -> str:
        await asyncio.sleep(1)
        return '{"ranked_ids":["candidate_1","candidate_0"]}'

    provider.ai_query.side_effect = _stall
    mass.get_providers_supporting_feature.return_value = [provider]
    candidates = [SuggestionCandidate("Daft Punk"), SuggestionCandidate("Air")]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.hitster.AI_QUERY_TIMEOUT_SECONDS",
        AI_QUERY_TIMEOUT_SECONDS / 30_000,
    ):
        ranked = await quiz._rank_bonus_candidates(
            current,
            TimelineBonusType.ARTIST,
            candidates,
        )

    assert ranked == candidates


@pytest.mark.asyncio
async def test_candidate_persists_display_artist_contributors_and_sort_aliases() -> None:
    """Persist justified artist truths while keeping one deterministic display value."""
    anchor = _track("anchor", "Teardrop", "Massive Attack", album_year=1998)
    current = _track("current", "Get Lucky", "Daft Punk", album_year=2013)
    current.artists[0].sort_name = "Punk, Daft"
    current.artists.append(
        ItemMapping(
            media_type=MediaType.ARTIST,
            item_id="artist-pharrell",
            provider="prov",
            name="Pharrell Williams",
        )
    )
    quiz, _ = _quiz([anchor, current])
    quiz._eligible_tracks = [anchor, current]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.hitster.secrets.choice",
        side_effect=[anchor, current],
    ):
        game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    candidate = game_round.answer_state.candidate
    assert candidate.entry.artist == current.artist_str
    assert candidate.artist_answers == [
        current.artist_str,
        "Daft Punk",
        "Pharrell Williams",
        "Punk, Daft",
    ]
    assert candidate.title_answers == ["Get Lucky"]
