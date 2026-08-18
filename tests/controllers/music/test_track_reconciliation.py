"""Tests for the hourly duplicate track reconciliation maintenance task."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID, TaskStatus
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.music.constants import TRACK_RECONCILIATION_BATCH_SIZE
from music_assistant.controllers.music.controller import MusicController
from music_assistant.mass import MusicAssistant

_DUPLICATE_NAME = "Shared Track Title"
_REPORT_FAILURE = "music_assistant.controllers.music.controller.report_current_task_failure"


def _mapping(provider_instance: str, item_id: str) -> ProviderMapping:
    """Create a provider mapping for a library fixture item."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain=provider_instance.removesuffix("_instance"),
        provider_instance=provider_instance,
        in_library=True,
    )


async def _add_artist(mass: MusicAssistant, provider_instance: str) -> Artist:
    """Create the shared fixture artist for a provider."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Shared Artist",
            provider_mappings={_mapping(provider_instance, f"{provider_instance}-artist")},
        )
    )


async def _add_album(
    mass: MusicAssistant, provider_instance: str, artist: Artist, name: str = "Shared Album"
) -> Album:
    """Create a fixture album for a provider."""
    slug = create_safe_string(name, True, True)
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=AlbumType.ALBUM,
            provider_mappings={_mapping(provider_instance, f"{provider_instance}-album-{slug}")},
            external_ids={(ExternalID.BARCODE, f"{provider_instance}-barcode-{slug}")},
            artists=UniqueList([artist]),
        )
    )


async def _add_track(
    mass: MusicAssistant,
    provider_instance: str,
    artist: Artist,
    album: Album,
    *,
    name: str,
    duration: int = 200,
    version: str = "",
    track_number: int = 1,
    explicit: bool | None = None,
) -> Track:
    """Create a fixture track under a unique name, so it is stored as its own row."""
    return await mass.music.tracks.add_item_to_library(
        Track(
            item_id="0",
            provider="library",
            name=name,
            version=version,
            duration=duration,
            metadata=MediaItemMetadata(explicit=explicit),
            provider_mappings={
                _mapping(
                    provider_instance,
                    f"{provider_instance}-track-{create_safe_string(name, True, True)}",
                )
            },
            artists=UniqueList([artist]),
            album=album,
            disc_number=1,
            track_number=track_number,
        )
    )


async def _rename_to_duplicate(mass: MusicAssistant, track: Track) -> None:
    """
    Rewrite a track row's title to the shared duplicate title.

    Fixture tracks are inserted under unique names so that insert-time matching keeps them
    as separate rows; this reproduces the end state of a library where matching failed.
    """
    await mass.music.database.update(
        DB_TABLE_TRACKS,
        {"item_id": int(track.item_id)},
        {
            "name": _DUPLICATE_NAME,
            "sort_name": _DUPLICATE_NAME.lower(),
            "search_name": create_safe_string(_DUPLICATE_NAME, True, True),
            "search_sort_name": create_safe_string(_DUPLICATE_NAME, True, True),
        },
    )


async def _set_track_title(mass: MusicAssistant, track: Track, name: str) -> None:
    """Give a track row a raw title, leaving its normalized title untouched."""
    await mass.music.database.update(
        DB_TABLE_TRACKS, {"item_id": int(track.item_id)}, {"name": name}
    )


async def _build_duplicate_pair(
    mass: MusicAssistant,
    *,
    second_provider: str = "qobuz_instance",
    second_duration: int = 200,
    second_version: str = "",
    second_track_number: int = 1,
    second_album_name: str = "Shared Album",
    first_explicit: bool | None = None,
    second_explicit: bool | None = None,
) -> tuple[Track, Track]:
    """Create two same-titled library tracks that differ only as the parameters say."""
    artist_1 = await _add_artist(mass, "spotify_instance")
    album_1 = await _add_album(mass, "spotify_instance", artist_1)
    track_1 = await _add_track(
        mass, "spotify_instance", artist_1, album_1, name="First Title", explicit=first_explicit
    )
    album_2 = await _add_album(mass, second_provider, artist_1, name=second_album_name)
    track_2 = await _add_track(
        mass,
        second_provider,
        artist_1,
        album_2,
        name="Second Title",
        duration=second_duration,
        version=second_version,
        track_number=second_track_number,
        explicit=second_explicit,
    )
    await _rename_to_duplicate(mass, track_1)
    await _rename_to_duplicate(mass, track_2)
    return track_1, track_2


def _bare_controller(candidate_rows: list[dict[str, int]]) -> MusicController:
    """Create a bare MusicController whose candidate query returns the given rows."""
    ctrl = MusicController.__new__(MusicController)
    ctrl._track_reconciliation_cursor = (0, 0)
    ctrl.logger = Mock()
    ctrl._database = Mock(get_rows_from_query=AsyncMock(return_value=candidate_rows))
    ctrl.mass = Mock(tasks=Mock(get_tasks_by_metadata=Mock(return_value=[])))
    return ctrl


# --------------------------------------------------------------------------- #
#  candidate selection and merging (against a real library database)           #
# --------------------------------------------------------------------------- #


async def test_merges_cross_provider_duplicate_tracks(mass: MusicAssistant) -> None:
    """Two same-titled tracks on the same album position are merged into one row."""
    track_1, track_2 = await _build_duplicate_pair(mass)

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_1.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
    }
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_2.item_id)


async def test_keeps_different_versions_apart(mass: MusicAssistant) -> None:
    """A remaster is never merged into the original recording."""
    track_1, track_2 = await _build_duplicate_pair(mass, second_version="Remastered 2011")

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_merges_despite_disagreement_on_the_explicit_flag(mass: MusicAssistant) -> None:
    """Providers routinely disagree on the explicit flag; that alone must not block a merge."""
    track_1, track_2 = await _build_duplicate_pair(mass, first_explicit=False, second_explicit=True)

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_1.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
    }
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_2.item_id)


async def test_keeps_differently_titled_tracks_apart(mass: MusicAssistant) -> None:
    """Titles that only look alike once normalized still have to survive the full compare."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    # both normalize to the same search_name, so the candidate query pairs them up
    await _set_track_title(mass, track_1, "Song, One")
    await _set_track_title(mass, track_2, "Song One!")

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_keeps_tracks_with_different_artists_apart(mass: MusicAssistant) -> None:
    """A shared title and album slot is not enough when the track artists differ."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    other_artist = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Different Artist",
            provider_mappings={_mapping("qobuz_instance", "qobuz-other-artist")},
        )
    )
    await mass.music.database.delete(DB_TABLE_TRACK_ARTISTS, {"track_id": int(track_2.item_id)})
    await mass.music.database.insert(
        DB_TABLE_TRACK_ARTISTS,
        {"track_id": int(track_2.item_id), "artist_id": int(other_artist.item_id)},
    )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_treats_an_unreported_disc_number_as_disc_one(mass: MusicAssistant) -> None:
    """A provider that reports no disc number still matches a track tagged as disc 1."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    await mass.music.database.update(
        DB_TABLE_ALBUM_TRACKS, {"track_id": int(track_2.item_id)}, {"disc_number": 0}
    )

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_1.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
    }


async def test_ignores_albums_whose_title_normalizes_to_nothing(mass: MusicAssistant) -> None:
    """Symbol-only album titles are not treated as agreement, they match everything."""
    track_1, track_2 = await _build_duplicate_pair(mass, second_album_name="+")
    for track in (track_1, track_2):
        album_id = (
            await mass.music.database.get_rows(
                DB_TABLE_ALBUM_TRACKS, {"track_id": int(track.item_id)}
            )
        )[0]["album_id"]
        await mass.music.database.update(
            DB_TABLE_ALBUMS, {"item_id": album_id}, {"name": "÷", "search_name": ""}
        )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_ignores_tracks_from_the_same_provider(mass: MusicAssistant) -> None:
    """Two rows of the same provider are left alone, however alike they look."""
    track_1, track_2 = await _build_duplicate_pair(mass, second_provider="spotify_instance")

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_requires_agreement_on_the_album(mass: MusicAssistant) -> None:
    """Tracks that sit on differently titled albums are not treated as duplicates."""
    track_1, track_2 = await _build_duplicate_pair(mass, second_album_name="Greatest Hits")

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_requires_agreement_on_the_album_position(mass: MusicAssistant) -> None:
    """Tracks at a different position on the same album are not treated as duplicates."""
    track_1, track_2 = await _build_duplicate_pair(mass, second_track_number=4)

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_ignores_tracks_with_a_large_duration_difference(mass: MusicAssistant) -> None:
    """A duration difference beyond the tolerance rules out a duplicate."""
    track_1, track_2 = await _build_duplicate_pair(mass, second_duration=260)

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_keeps_the_row_with_the_most_provider_mappings(mass: MusicAssistant) -> None:
    """The richest row survives the merge, so the fewest mappings have to move."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    await mass.music.tracks.add_provider_mapping(
        track_2.item_id, _mapping("tidal_instance", "tidal-track")
    )

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_2.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
        "tidal",
    }
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_1.item_id)


# --------------------------------------------------------------------------- #
#  batch mechanics                                                             #
# --------------------------------------------------------------------------- #


async def test_full_batch_advances_the_cursor() -> None:
    """A full batch resumes after the last examined row on the next run."""
    ctrl = _bare_controller(
        [
            {"item_id_1": index, "item_id_2": 1000 + index}
            for index in range(1, TRACK_RECONCILIATION_BATCH_SIZE + 1)
        ]
    )
    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=True)) as merge:
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_cursor == (
        TRACK_RECONCILIATION_BATCH_SIZE,
        1000 + TRACK_RECONCILIATION_BATCH_SIZE,
    )
    assert merge.await_count == TRACK_RECONCILIATION_BATCH_SIZE


async def test_full_batch_resumes_within_the_same_track() -> None:
    """A batch boundary between two pairs of one track resumes at that exact pair."""
    ctrl = _bare_controller(
        [
            {"item_id_1": 10, "item_id_2": 20 + offset}
            for offset in range(TRACK_RECONCILIATION_BATCH_SIZE)
        ]
    )

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=False)):
        await ctrl._reconcile_duplicate_tracks()

    # resuming at (10, ...) rather than past track 10 keeps its remaining pairs reachable
    assert ctrl._track_reconciliation_cursor == (10, 19 + TRACK_RECONCILIATION_BATCH_SIZE)


async def test_defers_while_a_library_sync_is_running() -> None:
    """Duplicates are judged against a settled library, never a half-synced one."""
    ctrl = _bare_controller([{"item_id_1": 1, "item_id_2": 2}])
    candidate_query = AsyncMock(return_value=[{"item_id_1": 1, "item_id_2": 2}])
    ctrl._database = Mock(get_rows_from_query=candidate_query)
    running = Mock(status=TaskStatus.RUNNING)
    ctrl.mass = Mock(tasks=Mock(get_tasks_by_metadata=Mock(return_value=[running])))

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock()) as merge:
        await ctrl._reconcile_duplicate_tracks()

    assert not merge.called
    assert not candidate_query.called


async def test_no_candidate_pair_is_starved() -> None:
    """Repeated runs reach every candidate, including pairs a batch boundary cut off."""
    # one track with more duplicate partners than fit in a single batch, so the boundary
    # falls in the middle of its pairs, followed by candidates that must stay reachable
    pairs = [(10, 20 + offset) for offset in range(TRACK_RECONCILIATION_BATCH_SIZE + 3)]
    pairs += [(11, 40), (12, 50)]
    seen: set[tuple[int, int]] = set()

    async def _candidates(_sql: str, params: dict[str, int], limit: int) -> list[dict[str, int]]:
        cursor = (params["cursor_item_id_1"], params["cursor_item_id_2"])
        return [
            {"item_id_1": item_id_1, "item_id_2": item_id_2}
            for item_id_1, item_id_2 in [pair for pair in pairs if pair > cursor][:limit]
        ]

    async def _refuse(item_id_1: int, item_id_2: int) -> bool:
        # refusing every pair keeps the candidate set intact, so a starved pair stays starved
        seen.add((item_id_1, item_id_2))
        return False

    ctrl = _bare_controller([])
    ctrl._database = Mock(get_rows_from_query=AsyncMock(side_effect=_candidates))

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(side_effect=_refuse)):
        for _ in range(4):
            await ctrl._reconcile_duplicate_tracks()

    assert seen == set(pairs)


async def test_partial_batch_restarts_the_cursor() -> None:
    """A partial batch means the table is drained, so the next run starts over."""
    ctrl = _bare_controller([{"item_id_1": 7, "item_id_2": 9}])
    ctrl._track_reconciliation_cursor = (5, 6)

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=True)):
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_cursor == (0, 0)


async def test_empty_batch_restarts_the_cursor() -> None:
    """With no candidates left the cursor rewinds so later additions are examined."""
    ctrl = _bare_controller([])
    ctrl._track_reconciliation_cursor = (500, 900)

    await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_cursor == (0, 0)


async def test_batch_continues_quietly_after_an_already_merged_row() -> None:
    """A row absorbed by an earlier merge in the same batch is expected, so it stays silent."""
    ctrl = _bare_controller([{"item_id_1": 1, "item_id_2": 2}, {"item_id_1": 3, "item_id_2": 4}])
    logger = Mock()
    ctrl.logger = logger

    with (
        patch.object(
            ctrl,
            "_merge_duplicate_track_pair",
            AsyncMock(side_effect=[MediaNotFoundError("gone"), True]),
        ) as merge,
        patch(_REPORT_FAILURE) as report_failure,
    ):
        await ctrl._reconcile_duplicate_tracks()

    assert merge.await_count == 2
    assert not logger.warning.called
    assert not report_failure.called


async def test_batch_continues_after_a_failing_pair() -> None:
    """A failing pair is reported but does not abort the rest of the batch."""
    ctrl = _bare_controller([{"item_id_1": 1, "item_id_2": 2}, {"item_id_1": 3, "item_id_2": 4}])
    logger = Mock()
    ctrl.logger = logger

    with (
        patch.object(
            ctrl,
            "_merge_duplicate_track_pair",
            AsyncMock(side_effect=[MusicAssistantError("boom"), True]),
        ) as merge,
        patch(_REPORT_FAILURE) as report_failure,
    ):
        await ctrl._reconcile_duplicate_tracks()

    assert merge.await_count == 2
    assert logger.warning.called
    assert report_failure.called
