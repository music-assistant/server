"""Tests for the Music Timeline Music Quiz type."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID, MediaType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.controllers.music.recency import RecencySnapshot
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.ai_distractors import AI_QUERY_TIMEOUT_SECONDS
from music_assistant.providers.music_quiz.models import (
    MusicQuizConfig,
    MusicQuizDifficulty,
    MusicQuizRound,
    TimelineBonusMode,
    TimelineBonusType,
    TimelineFreeTextBonusDefinition,
    TimelineMultipleChoiceBonusDefinition,
    TimelineRoundState,
)
from music_assistant.providers.music_quiz.quiz_types.base import PLAYBACK_REPLACEMENT_RESERVE
from music_assistant.providers.music_quiz.quiz_types.music_timeline import (
    BONUS_CANDIDATE_LIMIT,
    DEFAULT_BONUS_OPTION_COUNT,
    TRACK_ENRICHMENT_CONCURRENCY,
    MusicTimelineQuizType,
)
from music_assistant.providers.music_quiz.suggestions import (
    SuggestionCandidate,
    answer_labels_are_too_close,
)

# album types whose year describes a reissue rather than an original release
UNTRUSTED_ALBUM_TYPES = frozenset({AlbumType.COMPILATION, AlbumType.LIVE, AlbumType.SOUNDTRACK})


def _track(
    item_id: str,
    name: str,
    artist: str,
    *,
    album_year: int | None = None,
    release_year: int | None = None,
    available: bool = True,
    is_playable: bool = True,
    album_mapping: bool = False,
) -> Track:
    """Return a track with a resolved or unresolved album and configurable years."""
    provider = "prov"
    album: Album | ItemMapping = (
        ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=f"album-{item_id}",
            provider=provider,
            name=f"Album {item_id}",
            year=album_year,
        )
        if album_mapping
        else _full_album(f"album-{item_id}", f"Album {item_id}", year=album_year)
    )
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
        album=album,
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


def _full_album(
    item_id: str,
    name: str,
    *,
    album_type: AlbumType = AlbumType.ALBUM,
    artists: Sequence[Artist | ItemMapping] = (),
    year: int | None = None,
    provider: str = "prov",
) -> Album:
    """Return a full album with configurable compilation evidence."""
    return Album(
        item_id=item_id,
        provider=provider,
        name=name,
        album_type=album_type,
        artists=UniqueList(artists),
        year=year,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
            )
        },
    )


def _album_artist(
    item_id: str,
    name: str,
    *,
    mbid: str | None = None,
    provider: str = "prov",
) -> Artist:
    """Return a full album artist with optional MusicBrainz identity."""
    return Artist(
        item_id=item_id,
        provider=provider,
        name=name,
        external_ids={(ExternalID.MB_ARTIST, mbid)} if mbid else set(),
        provider_mappings=set(),
    )


def _artist(item_id: str, name: str) -> ItemMapping:
    """Return a catalog artist mapping."""
    return ItemMapping(
        media_type=MediaType.ARTIST,
        item_id=item_id,
        provider="prov",
        name=name,
    )


def _ai_provider(
    response: object = None,
    error: Exception | None = None,
) -> MagicMock:
    """Return a mock AI-query plugin provider."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "ai--test"
    provider.ai_query = AsyncMock(return_value=response, side_effect=error)
    return provider


def _ai_response(
    ranked_ids: list[str],
    synthetic: list[tuple[str, str]],
    **extra: object,
) -> str:
    """Return a structured AI distractor response."""
    return json.dumps(
        {
            "ranked_ids": ranked_ids,
            "synthetic": [{"kind": kind, "label": label} for kind, label in synthetic],
            **extra,
        }
    )


def _mass() -> MagicMock:
    """Return a mock MusicAssistant for Music Timeline tests."""
    mass = MagicMock()
    mass.metadata.get_image_url_for_item = AsyncMock(
        side_effect=lambda track: f"https://img/{track.item_id}"
    )
    mass.music.tracks.get = AsyncMock()
    mass.music.tracks.similar_tracks = AsyncMock(return_value=[])
    mass.music.artists.similar_artists = AsyncMock(return_value=[])
    mass.music.artists.top_tracks = AsyncMock(return_value=[])
    mass.music.artists.tracks = AsyncMock(return_value=[])
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    mass.music.recency.snapshot = AsyncMock(return_value=RecencySnapshot(now=0))
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    return mass


def _quiz(
    tracks: list[Track],
    *,
    round_count: int = 1,
    artist_mode: TimelineBonusMode = TimelineBonusMode.OFF,
    title_mode: TimelineBonusMode = TimelineBonusMode.OFF,
    use_ai: bool = False,
) -> tuple[MusicTimelineQuizType, MagicMock]:
    """Return a Music Timeline type backed by a deterministic source pool."""
    mass = _mass()
    config = MusicQuizConfig(
        round_count=round_count,
        source_uris=["prov://playlist/1"],
        artist_bonus_mode=artist_mode,
        title_bonus_mode=title_mode,
        use_ai_distractors=use_ai,
    )
    quiz = MusicTimelineQuizType(mass, config)
    quiz._source_track_pool = {track.uri: track for track in tracks if track.uri}
    return quiz, mass


def _with_musicbrainz(
    mass: MagicMock,
    years: dict[str, int] | None = None,
    error: Exception | None = None,
) -> MagicMock:
    """Attach a MusicBrainz provider that dates the given ISRCs to the mock MusicAssistant."""
    provider = MagicMock()
    provider.get_release_year_by_isrc = AsyncMock(
        side_effect=error if error is not None else lambda isrc: (years or {}).get(isrc)
    )
    mass.get_provider = MagicMock(
        side_effect=lambda domain: provider if domain == "musicbrainz" else None
    )
    return provider


def _with_isrc(track: Track, isrc: str) -> Track:
    """Return the given track carrying an ISRC."""
    track.add_external_id(ExternalID.ISRC, isrc)
    return track


async def _prepare_round_with_tracks(
    quiz: MusicTimelineQuizType,
    tracks: list[Track],
) -> MusicQuizRound:
    """Prepare the first round with the given tracks selected as anchor and candidate."""
    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.secrets.choice",
        side_effect=tracks,
    ):
        return await quiz.prepare_round(0, [])


def test_config_normalization_and_validation_are_music_timeline_specific() -> None:
    """Use fixed option defaults without requiring guess-the-song settings."""
    config = MusicQuizConfig(
        round_count=2,
        suggestion_count=9,
        source_uris=["prov://playlist/1"],
        include_similar_music=True,
        difficulty="not-used",
        use_ai_distractors=True,
        artist_bonus_mode=TimelineBonusMode.FREE_TEXT,
        title_bonus_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )

    normalized = MusicTimelineQuizType.normalize_config(config)
    MusicTimelineQuizType.validate_config(normalized)

    assert normalized.suggestion_count == DEFAULT_BONUS_OPTION_COUNT
    assert normalized.difficulty == MusicQuizDifficulty.NORMAL.value
    assert normalized.use_ai_distractors is True
    assert normalized.include_similar_music is True
    with pytest.raises(InvalidDataError, match="bonus mode"):
        MusicTimelineQuizType.validate_config(
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
async def test_large_eligible_pool_skips_enrichment_and_remains_available() -> None:
    """Preserve every ready source track without requesting redundant details."""
    tracks = [
        _track(
            f"track-{index}",
            f"Track {index}",
            f"Artist {index}",
            album_year=1980 + index,
        )
        for index in range(40)
    ]
    quiz, mass = _quiz(tracks, round_count=5)

    await quiz.initialize()

    mass.music.tracks.get.assert_not_awaited()
    assert quiz._eligible_tracks == tracks


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
async def test_track_enrichment_stops_after_game_requirement_and_reserve() -> None:
    """Stop requesting details once the complete game has replacement capacity."""
    ready = [
        _track("ready-one", "Ready One", "Artist One", album_year=1990),
        _track("ready-two", "Ready Two", "Artist Two", album_year=1991),
    ]
    partial = [
        _track(f"partial-{index}", f"Partial {index}", f"Artist {index}") for index in range(20)
    ]
    enriched = {
        track.item_id: _track(
            track.item_id,
            track.name,
            track.artist_str,
            album_year=2000 + index,
        )
        for index, track in enumerate(partial)
    }
    round_count = 3
    target_count = round_count + 1 + PLAYBACK_REPLACEMENT_RESERVE
    expected_enrichments = target_count - len(ready)
    quiz, mass = _quiz([*ready, *partial], round_count=round_count)
    mass.music.tracks.get.side_effect = lambda item_id, _provider: enriched[item_id]

    await quiz.initialize()

    assert mass.music.tracks.get.await_count == expected_enrichments
    enriched_ids = [await_call.args[0] for await_call in mass.music.tracks.get.await_args_list]
    assert len(set(enriched_ids)) == expected_enrichments
    assert set(enriched_ids) <= {track.item_id for track in partial}
    assert quiz._eligible_tracks is not None
    assert len(quiz._eligible_tracks) == target_count
    assert quiz._eligible_tracks[: len(ready)] == ready


@pytest.mark.asyncio
async def test_bounded_enrichment_samples_across_the_whole_source_pool() -> None:
    """Draw the bounded enrichment sample from anywhere in the source pool."""
    partial = [
        _track(f"partial-{index}", f"Partial {index}", f"Artist {index}") for index in range(20)
    ]
    enriched = {
        track.item_id: _track(
            track.item_id,
            track.name,
            track.artist_str,
            album_year=2000 + index,
        )
        for index, track in enumerate(partial)
    }
    round_count = 3
    target_count = round_count + 1 + PLAYBACK_REPLACEMENT_RESERVE
    quiz, mass = _quiz(partial, round_count=round_count)
    mass.music.tracks.get.side_effect = lambda item_id, _provider: enriched[item_id]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.SYSTEM_RANDOM.shuffle",
        side_effect=list.reverse,
    ) as shuffle:
        await quiz.initialize()

    shuffle.assert_called_once()
    enriched_ids = [await_call.args[0] for await_call in mass.music.tracks.get.await_args_list]
    assert enriched_ids == [track.item_id for track in reversed(partial[-target_count:])]
    assert quiz._eligible_tracks is not None
    assert len(quiz._eligible_tracks) == target_count


def test_only_unresolved_album_mapping_years_require_enrichment() -> None:
    """Require album resolution only for a year that can come from an album mapping."""
    resolved = _track("resolved", "Resolved", "Artist", album_year=1998)
    mapped = _track("mapped", "Mapped", "Artist", album_year=1998, album_mapping=True)
    mapped_with_track_year = _track(
        "own-year",
        "Own Year",
        "Artist",
        album_year=1998,
        release_year=1962,
        album_mapping=True,
    )
    mapped_without_album_year = _track(
        "undated-album",
        "Undated Album",
        "Artist",
        release_year=1975,
        album_mapping=True,
    )
    undated = _track("undated", "Undated", "Artist")

    assert MusicTimelineQuizType._track_is_resolved(resolved) is True
    assert MusicTimelineQuizType._track_is_resolved(mapped) is False
    assert MusicTimelineQuizType._track_is_resolved(mapped_with_track_year) is True
    assert MusicTimelineQuizType._track_is_resolved(mapped_without_album_year) is True
    assert MusicTimelineQuizType._track_is_resolved(undated) is False


@pytest.mark.asyncio
async def test_album_mapping_years_are_enriched_before_they_are_trusted() -> None:
    """Resolve an album mapping before accepting the release year it reports."""
    mapped = _track("mapped", "Mapped Track", "Artist", album_year=1998, album_mapping=True)
    enriched = _track("mapped", "Mapped Track", "Artist")
    enriched.album = _full_album("album-mapped", "Real Album", year=1998)
    resolved = _track("resolved", "Resolved Track", "Other Artist", album_year=2007)
    quiz, mass = _quiz([mapped, resolved])
    mass.music.tracks.get.return_value = enriched

    await quiz.initialize()

    enriched_ids = [await_call.args[0] for await_call in mass.music.tracks.get.await_args_list]
    assert enriched_ids == [mapped.item_id]
    assert quiz._eligible_tracks is not None
    assert {track.item_id for track in quiz._eligible_tracks} == {"mapped", "resolved"}


@pytest.mark.asyncio
async def test_enrichment_excludes_tracks_revealed_as_compilations() -> None:
    """Drop a track once enrichment reveals its album is a compilation."""
    mapped = _track("mapped", "Mapped Track", "Artist", album_year=1998, album_mapping=True)
    enriched = _track("mapped", "Mapped Track", "Artist")
    enriched.album = _full_album(
        "album-mapped",
        "Party Hits",
        album_type=AlbumType.COMPILATION,
        year=1998,
    )
    resolved = [
        _track(f"resolved-{index}", f"Resolved {index}", "Artist", album_year=1990 + index)
        for index in range(2)
    ]
    quiz, mass = _quiz([mapped, *resolved])
    mass.music.tracks.get.return_value = enriched

    await quiz.initialize()

    mass.music.tracks.get.assert_awaited_once_with(mapped.item_id, mapped.provider)
    assert quiz._eligible_tracks is not None
    assert {track.item_id for track in quiz._eligible_tracks} == {"resolved-0", "resolved-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("album_type", [AlbumType.ALBUM, AlbumType.SINGLE])
async def test_enrichment_keeps_tracks_revealed_as_original_releases(
    album_type: AlbumType,
) -> None:
    """Keep the resolved year of a track whose album dates an original release."""
    mapped = _track("mapped", "Mapped Track", "Artist", album_year=1998, album_mapping=True)
    enriched = _track("mapped", "Mapped Track", "Artist")
    enriched.album = _full_album(
        "album-mapped",
        "Real Album",
        album_type=album_type,
        year=1971,
    )
    resolved = [
        _track(f"resolved-{index}", f"Resolved {index}", "Artist", album_year=1990 + index)
        for index in range(2)
    ]
    quiz, mass = _quiz([mapped, *resolved])
    mass.music.tracks.get.return_value = enriched

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    eligible_tracks = {track.item_id: track for track in quiz._eligible_tracks}
    assert set(eligible_tracks) == {"mapped", "resolved-0", "resolved-1"}
    assert MusicTimelineQuizType._release_year(eligible_tracks["mapped"]) == 1971


@pytest.mark.asyncio
async def test_album_mappings_stay_eligible_when_enrichment_fails() -> None:
    """Trust an album mapping year when its resolution raises."""
    mapped = [
        _track(
            f"mapped-{index}",
            f"Mapped {index}",
            f"Artist {index}",
            album_year=1990 + index,
            album_mapping=True,
        )
        for index in range(12)
    ]
    round_count = 3
    target_count = round_count + 1 + PLAYBACK_REPLACEMENT_RESERVE
    quiz, mass = _quiz(mapped, round_count=round_count)
    mass.music.tracks.get.side_effect = MediaNotFoundError("provider unavailable")

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    assert len(quiz._eligible_tracks) == target_count


@pytest.mark.asyncio
async def test_album_mappings_stay_eligible_when_enrichment_cannot_resolve_them() -> None:
    """Trust an album mapping year once its resolution has been attempted."""
    mapped = [
        _track(
            f"mapped-{index}",
            f"Mapped {index}",
            f"Artist {index}",
            album_year=1990 + index,
            album_mapping=True,
        )
        for index in range(12)
    ]
    round_count = 3
    target_count = round_count + 1 + PLAYBACK_REPLACEMENT_RESERVE
    tracks_by_id = {track.item_id: track for track in mapped}
    quiz, mass = _quiz(mapped, round_count=round_count)
    mass.music.tracks.get.side_effect = lambda item_id, _provider: tracks_by_id[item_id]

    await quiz.initialize()

    assert mass.music.tracks.get.await_count == target_count
    assert quiz._eligible_tracks is not None
    assert len(quiz._eligible_tracks) == target_count
    assert [MusicTimelineQuizType._release_year(track) for track in quiz._eligible_tracks] == [
        1990 + int(track.item_id.removeprefix("mapped-")) for track in quiz._eligible_tracks
    ]


@pytest.mark.asyncio
async def test_insufficient_enrichment_exhausts_unresolved_tracks() -> None:
    """Check every unresolved source track before reporting an insufficient pool."""
    ready = [
        _track("ready-one", "Ready One", "Artist One", album_year=1990),
        _track("ready-two", "Ready Two", "Artist Two", album_year=1991),
    ]
    unresolved = [
        _track(f"partial-{index}", f"Partial {index}", f"Artist {index}") for index in range(12)
    ]
    quiz, mass = _quiz([*ready, *unresolved], round_count=4)
    mass.music.tracks.get.side_effect = lambda item_id, _provider: next(
        track for track in unresolved if track.item_id == item_id
    )

    with pytest.raises(InvalidDataError) as insufficient:
        await quiz.initialize()

    assert insufficient.value.translation_key == "music_quiz_not_enough_dated_tracks"
    assert mass.music.tracks.get.await_count == len(unresolved)


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


@pytest.mark.asyncio
async def test_musicbrainz_year_only_wins_when_it_predates_the_library_year() -> None:
    """Keep the earliest of the library and MusicBrainz release year."""
    reissue = _with_isrc(
        _track("reissue", "Immigrant Song", "Led Zeppelin", album_year=2018),
        "ISRC-REISSUE",
    )
    # remasters get their own ISRC, so MusicBrainz can date a song later than the library does
    remaster = _with_isrc(
        _track("remaster", "Penny Lane", "The Beatles", album_year=1967),
        "ISRC-REMASTER",
    )
    quiz, mass = _quiz([reissue, remaster])
    _with_musicbrainz(mass, {"ISRC-REISSUE": 1970, "ISRC-REMASTER": 2017})

    await quiz.initialize()
    game_round = await _prepare_round_with_tracks(quiz, [reissue, remaster])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].release_year == 1970
    assert game_round.answer_state.candidate.entry.release_year == 1967


@pytest.mark.asyncio
async def test_an_earlier_track_release_date_survives_a_later_musicbrainz_year() -> None:
    """Never push a release date the track already carries forward to a later year."""
    dated = _with_isrc(
        _track("dated", "Suspicious Minds", "Elvis Presley", release_year=1969),
        "ISRC-DATED",
    )
    other = _track("other", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([dated, other])
    _with_musicbrainz(mass, {"ISRC-DATED": 1990})

    await quiz.initialize()
    game_round = await _prepare_round_with_tracks(quiz, [dated, other])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].release_year == 1969


@pytest.mark.asyncio
async def test_musicbrainz_lookups_do_not_scale_with_the_source_pool() -> None:
    """Date only the tracks that reach the timeline, never the complete eligible pool."""
    tracks = [
        _with_isrc(
            _track(f"track-{index}", f"Song {index}", f"Artist {index}", album_year=1990),
            f"ISRC-{index}",
        )
        for index in range(60)
    ]
    quiz, mass = _quiz(tracks)
    musicbrainz = _with_musicbrainz(mass, {f"ISRC-{index}": 1970 for index in range(60)})

    await quiz.initialize()
    game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.candidate.entry.release_year == 1970
    assert musicbrainz.get_release_year_by_isrc.await_count <= quiz.config.round_count + 1


@pytest.mark.asyncio
async def test_musicbrainz_dates_tracks_dropped_as_compilations() -> None:
    """Restore a compilation track once MusicBrainz dates its recording."""
    compilation = _with_isrc(_track("compilation", "Africa", "Toto"), "ISRC-COMPILATION")
    compilation.album = _full_album(
        "album-compilation",
        "Party Hits",
        album_type=AlbumType.COMPILATION,
        year=1998,
    )
    resolved = _track("resolved", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz([compilation, resolved])
    mass.music.tracks.get.return_value = compilation
    _with_musicbrainz(mass, {"ISRC-COMPILATION": 1982})

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    eligible_tracks = {track.item_id: track for track in quiz._eligible_tracks}
    assert set(eligible_tracks) == {"compilation", "resolved"}
    assert MusicTimelineQuizType._release_year(eligible_tracks["compilation"]) == 1982


@pytest.mark.asyncio
async def test_library_years_are_kept_without_musicbrainz() -> None:
    """Place tracks on the timeline when no MusicBrainz provider is configured."""
    tracks = [
        _with_isrc(_track("one", "Teardrop", "Massive Attack", album_year=1998), "ISRC-ONE"),
        _with_isrc(_track("two", "Genesis", "Justice", album_year=2007), "ISRC-TWO"),
    ]
    quiz, mass = _quiz(tracks)
    mass.get_provider = MagicMock(return_value=None)

    await quiz.initialize()
    game_round = await _prepare_round_with_tracks(quiz, tracks)

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].release_year == 1998
    assert game_round.answer_state.candidate.entry.release_year == 2007


@pytest.mark.asyncio
async def test_tracks_without_an_isrc_are_not_looked_up() -> None:
    """Never ask MusicBrainz about a track that carries no ISRC."""
    tracks = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Genesis", "Justice", album_year=2007),
    ]
    quiz, mass = _quiz(tracks)
    musicbrainz = _with_musicbrainz(mass, {"ISRC-ONE": 1970})

    await quiz.initialize()
    game_round = await _prepare_round_with_tracks(quiz, tracks)

    musicbrainz.get_release_year_by_isrc.assert_not_awaited()
    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].release_year == 1998
    assert game_round.answer_state.candidate.entry.release_year == 2007


@pytest.mark.asyncio
async def test_failing_musicbrainz_lookups_keep_the_library_year() -> None:
    """Start the game on library years when the MusicBrainz lookup fails."""
    tracks = [
        _with_isrc(_track("one", "Teardrop", "Massive Attack", album_year=1998), "ISRC-ONE"),
        _with_isrc(_track("two", "Genesis", "Justice", album_year=2007), "ISRC-TWO"),
    ]
    quiz, mass = _quiz(tracks)
    _with_musicbrainz(mass, error=RuntimeError("musicbrainz unavailable"))

    await quiz.initialize()
    game_round = await _prepare_round_with_tracks(quiz, tracks)

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].release_year == 1998
    assert game_round.answer_state.candidate.entry.release_year == 2007


@pytest.mark.asyncio
async def test_stalled_musicbrainz_lookups_keep_the_library_year() -> None:
    """Never let a MusicBrainz lookup that does not answer hold up a round."""
    tracks = [
        _with_isrc(_track("one", "Teardrop", "Massive Attack", album_year=1998), "ISRC-ONE"),
        _with_isrc(_track("two", "Genesis", "Justice", album_year=2007), "ISRC-TWO"),
    ]
    quiz, mass = _quiz(tracks)
    musicbrainz = _with_musicbrainz(mass)

    async def _never_answer(_isrc: str) -> int:
        await asyncio.sleep(30)
        return 1970

    musicbrainz.get_release_year_by_isrc.side_effect = _never_answer

    await quiz.initialize()
    with patch(
        "music_assistant.providers.music_quiz.quiz_types."
        "music_timeline.RELEASE_YEAR_LOOKUP_BUDGET_SECONDS",
        0.01,
    ):
        game_round = await _prepare_round_with_tracks(quiz, tracks)

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].release_year == 1998
    assert game_round.answer_state.candidate.entry.release_year == 2007


def test_release_year_uses_earliest_valid_track_or_album_year() -> None:
    """Resolve the earliest usable year from album and track metadata."""
    track_first = _track(
        "track-first",
        "Track First",
        "Artist",
        album_year=2005,
        release_year=1999,
    )
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
    album_first = _track("album-first", "Album First", "Artist", release_year=2002)
    album_first.album = full_album
    album_only = _track("album-only", "Album Only", "Artist", album_year=2001)
    track_only = _track("track-only", "Track Only", "Artist", release_year=2003)
    future_album = _track(
        "future-album",
        "Future Album",
        "Artist",
        album_year=datetime.now(tz=UTC).year + 1,
        release_year=2004,
    )
    too_old_track = _track(
        "too-old-track",
        "Too Old Track",
        "Artist",
        album_year=2006,
        release_year=999,
    )
    invalid = _track(
        "invalid",
        "Invalid",
        "Artist",
        album_year=datetime.now(tz=UTC).year + 1,
        release_year=999,
    )

    assert MusicTimelineQuizType._release_year(track_first) == 1999
    assert MusicTimelineQuizType._release_year(album_first) == 2000
    assert MusicTimelineQuizType._release_year(album_only) == 2001
    assert MusicTimelineQuizType._release_year(track_only) == 2003
    assert MusicTimelineQuizType._release_year(future_album) == 2004
    assert MusicTimelineQuizType._release_year(too_old_track) == 2006
    assert MusicTimelineQuizType._release_year(invalid) is None


@pytest.mark.parametrize("album_type", list(AlbumType), ids=lambda value: value.value)
def test_release_year_trusts_only_original_release_album_types(album_type: AlbumType) -> None:
    """Accept album years only from album types that date an original release."""
    track = _track("typed", "Typed Track", "Artist")
    track.album = _full_album("album", "Typed Album", album_type=album_type, year=1998)
    expected_year = None if album_type in UNTRUSTED_ALBUM_TYPES else 1998

    assert MusicTimelineQuizType._release_year(track) == expected_year


@pytest.mark.parametrize(
    "album_artists",
    [
        [_album_artist("va-mbid", "Artistes divers", mbid=VARIOUS_ARTISTS_MBID)],
        [_album_artist("va-name", VARIOUS_ARTISTS_NAME)],
        [
            _album_artist("primary", "Primary Artist"),
            _album_artist("va-multiple", "VARIOUS-ARTISTS"),
        ],
    ],
    ids=["canonical-mbid", "canonical-name", "multiple-artists"],
)
def test_release_year_rejects_various_artists_album_credits(
    album_artists: list[Artist],
) -> None:
    """Reject the album year of a various-artists set typed as a regular album."""
    track = _track("various", "Various Track", "Artist")
    track.album = _full_album(
        "album",
        "Various Album",
        album_type=AlbumType.ALBUM,
        artists=album_artists,
        year=1998,
    )

    assert MusicTimelineQuizType._release_year(track) is None


def test_release_year_trusts_an_album_mapping_without_compilation_evidence() -> None:
    """Accept the album year of a mapping that carries no album type or artists."""
    track = _track("mapping", "Mapped Track", "Artist", album_year=1998)
    track.album = ItemMapping(
        media_type=MediaType.ALBUM,
        item_id="album-mapping",
        provider="prov",
        name=VARIOUS_ARTISTS_NAME,
        year=1998,
    )

    assert MusicTimelineQuizType._release_year(track) == 1998


def test_release_year_of_a_compilation_falls_back_to_the_track_year() -> None:
    """Read the track year when a compilation album supplies the only other year."""
    track = _track("compilation", "Compilation Track", "Artist", release_year=1962)
    track.album = _full_album(
        "album",
        "Greatest Hits",
        album_type=AlbumType.COMPILATION,
        year=1998,
    )

    assert MusicTimelineQuizType._release_year(track) == 1962


@pytest.mark.asyncio
async def test_compilation_tracks_are_excluded_from_the_eligible_pool() -> None:
    """Build the timeline pool from original releases only."""
    dated = [
        _track(f"dated-{index}", f"Dated {index}", "Artist", album_year=1990 + index)
        for index in range(2)
    ]
    compilations = [
        _track(f"compilation-{index}", f"Compilation {index}", "Artist") for index in range(2)
    ]
    for index, track in enumerate(compilations):
        track.album = _full_album(
            f"album-{index}",
            f"Party Hits {index}",
            album_type=AlbumType.COMPILATION,
            year=2005 + index,
        )
    tracks = [*dated, *compilations]
    quiz, mass = _quiz(tracks, round_count=1)
    tracks_by_id = {track.item_id: track for track in tracks}
    mass.music.tracks.get.side_effect = lambda item_id, _provider: tracks_by_id[item_id]

    await quiz.initialize()

    assert quiz._eligible_tracks is not None
    assert [track.uri for track in quiz._eligible_tracks] == [track.uri for track in dated]


def test_reject_track_removes_it_from_music_timeline_caches() -> None:
    """Exclude failed playback tracks from future Music Timeline selection."""
    failed = _track("failed", "Unavailable", "Artist", album_year=2000)
    available = _track("available", "Playable", "Artist", album_year=2001)
    quiz, _ = _quiz([failed, available])
    assert failed.uri is not None
    assert available.uri is not None
    quiz._source_track_pool = {failed.uri: failed, available.uri: available}
    quiz._eligible_tracks = [failed, available]
    artist_key = ("prov", "artist")
    failed_candidate = SuggestionCandidate(failed.name, uri=failed.uri)
    available_candidate = SuggestionCandidate(available.name, uri=available.uri)
    quiz._title_top_track_candidates[artist_key] = [
        failed_candidate,
        available_candidate,
    ]
    quiz._title_search_candidates[artist_key] = [
        available_candidate,
        failed_candidate,
    ]

    quiz.reject_track(failed.uri)

    assert quiz._source_track_pool == {available.uri: available}
    assert quiz._eligible_tracks == [available]
    assert quiz._title_top_track_candidates[artist_key] == [available_candidate]
    assert quiz._title_search_candidates[artist_key] == [available_candidate]


@pytest.mark.asyncio
async def test_prepare_round_seeds_anchor_and_prefetches_guaranteed_future_snapshot() -> None:
    """Build the next snapshot with the current song before that song is revealed."""
    anchor = _track("anchor", "Teardrop", "Massive Attack", album_year=1998)
    first = _track("first", "Genesis", "Justice", album_year=2007)
    second = _track("second", "Lisztomania", "Phoenix", album_year=2009)
    quiz, _ = _quiz([anchor, first, second], round_count=2)
    quiz._eligible_tracks = [anchor, first, second]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.secrets.choice",
        side_effect=[anchor, first],
    ):
        first_round = await quiz.prepare_round(0, [])
    reconnected_quiz, _ = _quiz([anchor, first, second], round_count=2)
    reconnected_quiz._eligible_tracks = [anchor, first, second]
    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.secrets.choice",
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
async def test_prepare_round_reserves_fresh_track_for_scored_timeline_round() -> None:
    """Use a recent track as the anchor so a fresh track remains for players to place."""
    recent_anchor = _track("recent-anchor", "Teardrop", "Massive Attack", album_year=1998)
    recent_other = _track("recent-other", "Genesis", "Justice", album_year=2007)
    fresh = _track("fresh", "Lisztomania", "Phoenix", album_year=2009)
    quiz, _ = _quiz([recent_anchor, recent_other, fresh])
    quiz._eligible_tracks = [recent_anchor, recent_other, fresh]
    assert recent_anchor.uri is not None
    assert recent_other.uri is not None
    quiz.add_recent_track_uris([recent_anchor.uri, recent_other.uri])

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.secrets.choice",
        side_effect=lambda candidates: candidates[0],
    ):
        game_round = await quiz.prepare_round(0, [])

    assert isinstance(game_round.answer_state, TimelineRoundState)
    assert game_round.answer_state.placement_snapshot[0].track_uri == recent_anchor.uri
    assert game_round.track_uri == fresh.uri


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
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.secrets.choice",
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
    mass.music.artists.top_tracks.assert_awaited_once()
    mass.music.artists.tracks.assert_not_awaited()
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
    mass.get_providers_supporting_feature.assert_not_called()


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
async def test_artist_source_fallback_stops_fuzzy_filtering_after_three_options() -> None:
    """Bound pairwise filtering even when the source pool is large."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    fallback_tracks = [
        _track("one", "Teardrop", "Massive Attack", album_year=1998),
        _track("two", "Glory Box", "Portishead", album_year=1994),
        _track("three", "Roads", "Tricky", album_year=1995),
        *[
            _track(
                f"unused-{index}",
                f"Unused Track {index}",
                f"Unused Artist {index}",
                album_year=2000,
            )
            for index in range(1000)
        ],
    ]
    quiz, _ = _quiz(
        [current, *fallback_tracks],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, *fallback_tracks]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline."
        "answer_labels_are_too_close",
        wraps=answer_labels_are_too_close,
    ) as compare_labels:
        options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Massive Attack",
        "Portishead",
        "Tricky",
    }
    assert compare_labels.call_count == 6


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
    mass.music.artists.top_tracks.assert_not_awaited()
    mass.music.artists.tracks.assert_not_awaited()
    mass.music.search.assert_not_awaited()
    mass.get_providers_supporting_feature.assert_not_called()


@pytest.mark.asyncio
async def test_title_bonus_uses_filtered_top_tracks_before_unrelated_sources() -> None:
    """Supplement source titles with real primary-artist top tracks only."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_same_artist = _track("source-same", "D.A.N.C.E.", "Justice", album_year=2007)
    unrelated = _track("unrelated", "Teardrop", "Massive Attack", album_year=1998)
    secondary_artist = _track("secondary", "Waters of Nazareth", "Mr. Oizo")
    secondary_artist.artists.append(_artist("justice", "Justice"))
    quiz, mass = _quiz(
        [current, source_same_artist, unrelated],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, source_same_artist, unrelated]
    mass.music.artists.top_tracks.return_value = [
        current,
        _track("close", "Genesis (Remix)", "Justice"),
        _track("duplicate", "D.A.N.C.E.", "Justice"),
        secondary_artist,
        _artist("not-a-track", "Not a track"),
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
    mass.music.artists.top_tracks.assert_awaited_once_with(
        item_id=current.artists[0].item_id,
        provider_instance_id_or_domain=current.artists[0].provider,
    )
    mass.music.artists.tracks.assert_not_awaited()
    mass.music.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_title_bonus_search_is_bounded_and_cached_per_artist() -> None:
    """Reuse one top-track and bounded search result for repeated artist rounds."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    other = _track("other", "Stress", "Justice", album_year=2007)
    other.artists = UniqueList([current.artists[0]])
    unrelated = _track("unrelated", "Teardrop", "Massive Attack", album_year=1998)
    quiz, mass = _quiz(
        [current, other, unrelated],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, other, unrelated]
    mass.music.artists.top_tracks.return_value = [
        _track("top", "Phantom", "Justice"),
    ]
    mass.music.search.return_value = SimpleNamespace(
        tracks=[
            _track("wrong-artist", "Sexy Boy", "Air"),
            _track("search-one", "Audio, Video, Disco", "Justice"),
            _track("search-two", "Civilization", "Justice"),
        ]
    )

    current_options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)
    other_options = await quiz._create_bonus_options(other, TimelineBonusType.TITLE)

    assert "Teardrop" not in {option.label for option in current_options}
    assert "Teardrop" not in {option.label for option in other_options}
    assert "Sexy Boy" not in {option.label for option in current_options}
    assert "Sexy Boy" not in {option.label for option in other_options}
    mass.music.artists.top_tracks.assert_awaited_once()
    mass.music.search.assert_awaited_once_with(
        search_query="Justice",
        media_types=[MediaType.TRACK],
        limit=BONUS_CANDIDATE_LIMIT,
        library_only=False,
    )
    mass.music.artists.tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_title_bonus_uses_unrelated_source_only_when_grounded_pool_is_sparse() -> None:
    """Use unrelated source titles only after bounded same-artist sources run out."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_same_artist = _track("source-same", "D.A.N.C.E.", "Justice", album_year=2007)
    unrelated = _track("unrelated", "Teardrop", "Massive Attack", album_year=1998)
    unused_fallback = _track("other", "Glory Box", "Portishead", album_year=1994)
    quiz, mass = _quiz(
        [current, source_same_artist, unrelated, unused_fallback],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [
        current,
        source_same_artist,
        unrelated,
        unused_fallback,
    ]
    mass.music.artists.top_tracks.return_value = [
        _track("top", "Phantom", "Justice"),
    ]

    options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "D.A.N.C.E.",
        "Phantom",
        "Teardrop",
    }
    mass.music.artists.top_tracks.assert_awaited_once()
    mass.music.search.assert_awaited_once()
    mass.music.artists.tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_title_bonus_uses_same_artist_source_tracks_outside_playback_set() -> None:
    """Ground same-artist titles in the complete source pool without extra lookups."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    undated_same_artist = [
        _track("one", "D.A.N.C.E.", "Justice"),
        _track("two", "Phantom", "Justice"),
        _track("three", "Audio, Video, Disco", "Justice"),
    ]
    unrelated = _track("unrelated", "Teardrop", "Massive Attack", album_year=1998)
    quiz, mass = _quiz(
        [current, *undated_same_artist, unrelated],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, unrelated]

    with patch.object(
        quiz,
        "_source_pool_bonus_distractors",
        wraps=quiz._source_pool_bonus_distractors,
    ) as source_pool_fallback:
        options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "D.A.N.C.E.",
        "Phantom",
        "Audio, Video, Disco",
    }
    source_pool_fallback.assert_not_awaited()
    mass.music.artists.top_tracks.assert_not_awaited()
    mass.music.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_bonus_fallback_uses_source_tracks_outside_playback_set() -> None:
    """Fill the last-resort bonus options from source tracks the timeline cannot use."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    undated = [
        _track("one", "Teardrop", "Massive Attack"),
        _track("two", "Glory Box", "Portishead"),
        _track("three", "Roads", "Tricky"),
    ]
    quiz, _ = _quiz(
        [current, *undated],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current]

    artist_options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)
    title_options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in artist_options if not option.is_correct} == {
        "Massive Attack",
        "Portishead",
        "Tricky",
    }
    assert {option.label for option in title_options if not option.is_correct} == {
        "Teardrop",
        "Glory Box",
        "Roads",
    }


@pytest.mark.asyncio
async def test_rejected_track_is_excluded_from_bonus_options() -> None:
    """Never reuse a rejected track as a bonus label while the pool can still fill options."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    failed = _track("failed", "Teardrop", "Massive Attack", album_year=1998)
    remaining = [
        _track("one", "Glory Box", "Portishead"),
        _track("two", "Roads", "Tricky"),
        _track("three", "Sexy Boy", "Air"),
    ]
    quiz, _ = _quiz(
        [current, failed, *remaining],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )
    quiz._eligible_tracks = [current, failed]
    assert failed.uri is not None

    quiz.reject_track(failed.uri)
    options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "Glory Box",
        "Roads",
        "Sexy Boy",
    }


@pytest.mark.asyncio
async def test_ai_artist_bonus_keeps_real_candidates_dominant() -> None:
    """Use two ranked similar artists and at most one synthetic artist."""
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
        _artist("daft-punk", "Daft Punk"),
        _artist("air", "Air"),
        _artist("phoenix", "Phoenix"),
    ]
    provider = _ai_provider(
        _ai_response(
            ["candidate_2", "candidate_1", "candidate_0", "candidate_3"],
            [("artist", "Neon Assembly")],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Air",
        "Daft Punk",
        "Neon Assembly",
    }
    assert "Portishead" not in {option.label for option in options}
    mass.music.tracks.similar_tracks.assert_not_awaited()
    provider.ai_query.assert_awaited_once()
    prompt = provider.ai_query.await_args.args[0]
    assert "Justice" in prompt
    assert "Massive Attack" in prompt
    assert "Portishead" not in prompt


@pytest.mark.asyncio
async def test_ai_artist_bonus_fills_only_real_candidate_shortfall() -> None:
    """Retain every available real artist and synthesize only missing options."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz(
        [current],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current]
    mass.music.artists.similar_artists.return_value = [_artist("daft-punk", "Daft Punk")]
    provider = _ai_provider(
        _ai_response(
            ["candidate_0"],
            [
                ("artist", "Neon Assembly"),
                ("artist", "Lunar Circuit"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Daft Punk",
        "Neon Assembly",
        "Lunar Circuit",
    }
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_artist_bonus_uses_synthetic_fill_before_source_fallbacks() -> None:
    """Measure AI shortfall against similar artists rather than unrelated sources."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_fallback = [
        _track("fallback-one", "Teardrop", "Massive Attack", album_year=1998),
        _track("fallback-two", "Glory Box", "Portishead", album_year=1994),
        _track("fallback-three", "Roads", "Tricky", album_year=1995),
    ]
    quiz, mass = _quiz(
        [current, *source_fallback],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current, *source_fallback]
    mass.music.artists.similar_artists.return_value = [_artist("daft-punk", "Daft Punk")]
    provider = _ai_provider(
        _ai_response(
            ["candidate_0"],
            [
                ("artist", "Neon Assembly"),
                ("artist", "Lunar Circuit"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    wrong_labels = {option.label for option in options if not option.is_correct}
    assert wrong_labels == {"Daft Punk", "Neon Assembly", "Lunar Circuit"}
    prompt = provider.ai_query.await_args.args[0]
    assert "Daft Punk" in prompt
    assert "Massive Attack" not in prompt
    assert "Portishead" not in prompt
    assert "Tricky" not in prompt


@pytest.mark.asyncio
async def test_invalid_ai_fill_falls_back_to_real_source_candidates() -> None:
    """Keep catalog-only resilience when a sparse AI response is invalid."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    source_fallback = [
        _track("fallback-one", "Teardrop", "Massive Attack", album_year=1998),
        _track("fallback-two", "Glory Box", "Portishead", album_year=1994),
        _track("fallback-three", "Roads", "Tricky", album_year=1995),
    ]
    quiz, mass = _quiz(
        [current, *source_fallback],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current, *source_fallback]
    provider = _ai_provider(
        _ai_response(
            [],
            [
                ("artist", "Neon Assembly"),
                ("artist", "Lunar Circuit"),
                ("artist", "Chrome Pilots"),
            ],
            extra=True,
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Massive Attack",
        "Portishead",
        "Tricky",
    }
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_title_bonus_keeps_same_artist_catalog_candidates_dominant() -> None:
    """Use two real same-artist titles and at most one synthetic title."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    same_artist = [
        _track("one", "D.A.N.C.E.", "Justice", album_year=2007),
        _track("two", "Phantom", "Justice", album_year=2007),
        _track("three", "Audio, Video, Disco", "Justice", album_year=2011),
    ]
    quiz, mass = _quiz(
        [current, *same_artist],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current, *same_artist]
    provider = _ai_provider(
        _ai_response(
            ["candidate_1", "candidate_0", "candidate_2"],
            [("title", "Chrome Cathedral")],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "Phantom",
        "D.A.N.C.E.",
        "Chrome Cathedral",
    }
    mass.music.artists.top_tracks.assert_not_awaited()
    mass.music.search.assert_not_awaited()
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_title_bonus_fills_only_missing_same_artist_titles() -> None:
    """Fill a sparse grounded title pool without replacing its real candidate."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz(
        [current],
        title_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current]
    mass.music.artists.top_tracks.return_value = [
        _track("top", "Phantom", "Justice"),
    ]
    provider = _ai_provider(
        _ai_response(
            ["candidate_0"],
            [
                ("title", "Chrome Cathedral"),
                ("title", "Electric Pilgrims"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.TITLE)

    assert {option.label for option in options if not option.is_correct} == {
        "Phantom",
        "Chrome Cathedral",
        "Electric Pilgrims",
    }
    mass.music.artists.top_tracks.assert_awaited_once()
    mass.music.search.assert_awaited_once()
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "synthetic",
    [
        [("artist", "Justice")],
        [("artist", "Justice!")],
        [("artist", "Fake Artist - Fake Song")],
        [("artist", "Fake Artist \u2013 Fake Song")],
    ],
)
async def test_invalid_ai_bonus_labels_fall_back_to_real_catalog(
    synthetic: list[tuple[str, str]],
) -> None:
    """Reject correct, near-correct, and malformed synthetic bonus labels."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz(
        [current],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current]
    mass.music.artists.similar_artists.return_value = [
        _artist("daft-punk", "Daft Punk"),
        _artist("air", "Air"),
        _artist("phoenix", "Phoenix"),
    ]
    provider = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            synthetic,
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Daft Punk",
        "Air",
        "Phoenix",
    }
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_primary_ai_provider_does_not_try_another_provider() -> None:
    """Make one deterministic AI request before falling back to real candidates."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz(
        [current],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current]
    mass.music.artists.similar_artists.return_value = [
        _artist("daft-punk", "Daft Punk"),
        _artist("air", "Air"),
        _artist("phoenix", "Phoenix"),
    ]
    invalid = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            [("artist", "Neon Assembly")],
            extra=True,
        )
    )
    invalid.instance_id = "ai--a"
    later = _ai_provider()
    later.instance_id = "ai--b"
    mass.get_providers_supporting_feature.return_value = [later, invalid]

    options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Daft Punk",
        "Air",
        "Phoenix",
    }
    invalid.ai_query.assert_awaited_once()
    later.ai_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_bonus_timeout_falls_back_to_real_catalog() -> None:
    """Use only catalog candidates when the single AI request times out."""
    current = _track("current", "Genesis", "Justice", album_year=2007)
    quiz, mass = _quiz(
        [current],
        artist_mode=TimelineBonusMode.MULTIPLE_CHOICE,
        use_ai=True,
    )
    quiz._eligible_tracks = [current]
    mass.music.artists.similar_artists.return_value = [
        _artist("daft-punk", "Daft Punk"),
        _artist("air", "Air"),
        _artist("phoenix", "Phoenix"),
    ]
    provider = _ai_provider()

    async def _stall(_prompt: str) -> str:
        await asyncio.sleep(1)
        return ""

    provider.ai_query.side_effect = _stall
    mass.get_providers_supporting_feature.return_value = [provider]

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.AI_QUERY_TIMEOUT_SECONDS",
        AI_QUERY_TIMEOUT_SECONDS / 30_000,
    ):
        options = await quiz._create_bonus_options(current, TimelineBonusType.ARTIST)

    assert {option.label for option in options if not option.is_correct} == {
        "Daft Punk",
        "Air",
        "Phoenix",
    }
    provider.ai_query.assert_awaited_once()


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
        "music_assistant.providers.music_quiz.quiz_types.music_timeline.secrets.choice",
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
