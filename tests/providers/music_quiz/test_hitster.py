"""Tests for the Hitster Music Quiz type."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    DEFAULT_BONUS_OPTION_COUNT,
    HitsterQuizType,
)


def _track(
    item_id: str,
    name: str,
    artist: str,
    *,
    album_year: int | None = None,
    release_year: int | None = None,
) -> Track:
    """Return a track with configurable album and metadata years."""
    provider = "prov"
    track = Track(
        item_id=item_id,
        provider=provider,
        name=name,
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
            )
        },
    )
    if release_year is not None:
        track.metadata.release_date = datetime(release_year, 1, 1, tzinfo=UTC)
    return track


def _mass() -> MagicMock:
    """Return a mock MusicAssistant for Hitster tests."""
    mass = MagicMock()
    mass.metadata.get_image_url_for_item = AsyncMock(
        side_effect=lambda track: f"https://img/{track.item_id}"
    )
    mass.music.tracks.get = AsyncMock()
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    return mass


def _quiz(
    tracks: list[Track],
    *,
    round_count: int = 1,
    artist_mode: str = TimelineBonusMode.OFF.value,
    title_mode: str = TimelineBonusMode.OFF.value,
) -> tuple[HitsterQuizType, MagicMock]:
    """Return a Hitster type backed by a deterministic source pool."""
    mass = _mass()
    config = MusicQuizConfig(
        round_count=round_count,
        source_uris=["prov://playlist/1"],
        artist_bonus_mode=artist_mode,
        title_bonus_mode=title_mode,
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
        artist_bonus_mode=TimelineBonusMode.FREE_TEXT.value,
        title_bonus_mode=TimelineBonusMode.MULTIPLE_CHOICE.value,
    )

    normalized = HitsterQuizType.normalize_config(config)
    HitsterQuizType.validate_config(normalized)

    assert normalized.suggestion_count == DEFAULT_BONUS_OPTION_COUNT
    assert normalized.difficulty == MusicQuizDifficulty.NORMAL.value
    assert normalized.use_ai_distractors is False
    with pytest.raises(InvalidDataError, match="bonus mode"):
        HitsterQuizType.validate_config(
            MusicQuizConfig(
                source_uris=["prov://playlist/1"],
                artist_bonus_mode="invalid",
            )
        )


@pytest.mark.asyncio
async def test_initialize_selects_anchor_plus_unique_scored_tracks() -> None:
    """Reserve one extra unique dated track for the unscored anchor."""
    tracks = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Genesis", "Justice", album_year=2007),
        _track("three", "Lisztomania", "Phoenix", album_year=2009),
        _track("four", "Sexy Boy", "Air", album_year=1998),
    ]
    quiz, _ = _quiz(tracks, round_count=3)

    await quiz.initialize()

    assert quiz._selected_tracks is not None
    assert len(quiz._selected_tracks) == 4
    assert len({track.uri for track in quiz._selected_tracks}) == 4


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
    quiz._selected_tracks = [anchor, first, second]

    first_round = await quiz.prepare_round(0, [])
    second_round = await quiz.prepare_round(1, [first_round])

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
                    second_round.answer_state.current_entry,
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
        artist_mode=TimelineBonusMode.FREE_TEXT.value,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE.value,
    )
    quiz._eligible_tracks = tracks
    quiz._selected_tracks = tracks[:2]

    game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    artist_definition, title_definition = game_round.answer_state.bonus_definitions
    assert isinstance(artist_definition, TimelineFreeTextBonusDefinition)
    assert artist_definition.bonus_type == TimelineBonusType.ARTIST
    assert artist_definition.correct_value == "Justice"
    assert isinstance(title_definition, TimelineMultipleChoiceBonusDefinition)
    assert title_definition.bonus_type == TimelineBonusType.TITLE
    assert len(title_definition.options) == 4
    assert sum(option.is_correct for option in title_definition.options) == 1
    assert len({option.option_id for option in title_definition.options}) == 4
    assert all("correct" not in option.option_id for option in title_definition.options)
    mass.music.search.assert_not_awaited()
