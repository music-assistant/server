"""Tests for the hourly duplicate track reconciliation maintenance task."""

from __future__ import annotations

from typing import NamedTuple
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


class _TrackSpec(NamedTuple):
    """How one side of a duplicate pair should differ from the other."""

    provider: str = "spotify_instance"
    title: str = _DUPLICATE_NAME
    album_name: str = "Shared Album"
    duration: int = 200
    version: str = ""
    track_number: int = 1
    explicit: bool | None = None
    mbid: str | None = None


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
    mbid: str | None = None,
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
            external_ids={(ExternalID.MB_RECORDING, mbid)} if mbid else set(),
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


async def _make_titles_look_alike(mass: MusicAssistant, track: Track, title: str) -> None:
    """
    Give a track row its final title plus the shared normalized title.

    Fixture tracks are inserted under unique titles so that insert-time matching keeps them
    as separate rows; equalizing search_name afterwards reproduces the end state of a library
    where that matching failed.
    """
    normalized = create_safe_string(_DUPLICATE_NAME, True, True)
    await mass.music.database.update(
        DB_TABLE_TRACKS,
        {"item_id": int(track.item_id)},
        {
            "name": title,
            "sort_name": title.lower(),
            "search_name": normalized,
            "search_sort_name": normalized,
        },
    )


async def _build_duplicate_pair(
    mass: MusicAssistant,
    first: _TrackSpec | None = None,
    second: _TrackSpec | None = None,
) -> tuple[Track, Track]:
    """Create two same-titled library tracks that differ only as the specs say."""
    first = first or _TrackSpec()
    second = second or _TrackSpec(provider="qobuz_instance")
    artist = await _add_artist(mass, first.provider)
    tracks: list[Track] = []
    for index, spec in enumerate((first, second)):
        album = await _add_album(mass, spec.provider, artist, name=spec.album_name)
        track = await _add_track(
            mass,
            spec.provider,
            artist,
            album,
            name=f"Fixture Title {index}",
            duration=spec.duration,
            version=spec.version,
            track_number=spec.track_number,
            explicit=spec.explicit,
            mbid=spec.mbid,
        )
        await _make_titles_look_alike(mass, track, spec.title)
        tracks.append(track)
    return tracks[0], tracks[1]


def _bare_controller(candidate_rows: list[dict[str, int]]) -> MusicController:
    """Create a bare MusicController whose candidate query returns the given rows."""
    ctrl = MusicController.__new__(MusicController)
    ctrl._track_reconciliation_cursor = (0, 0)
    ctrl._track_reconciliation_rescan_due = False
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
    track_1, track_2 = await _build_duplicate_pair(
        mass, second=_TrackSpec("qobuz_instance", version="Remastered 2011")
    )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_merges_despite_disagreement_on_the_explicit_flag(mass: MusicAssistant) -> None:
    """Providers routinely disagree on the explicit flag; that alone must not block a merge."""
    track_1, track_2 = await _build_duplicate_pair(
        mass, _TrackSpec(explicit=False), _TrackSpec("qobuz_instance", explicit=True)
    )

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_1.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
    }
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_2.item_id)


async def test_keeps_an_album_edition_apart_from_the_original(mass: MusicAssistant) -> None:
    """An edition lives on the album, not the track, so equal track titles are not enough."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    album_id = (
        await mass.music.database.get_rows(
            DB_TABLE_ALBUM_TRACKS, {"track_id": int(track_2.item_id)}
        )
    )[0]["album_id"]
    await mass.music.database.update(
        DB_TABLE_ALBUMS, {"item_id": album_id}, {"version": "2014 Remaster"}
    )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_an_unrelated_appearance_cannot_approve_an_edition(mass: MusicAssistant) -> None:
    """Only the album appearance the pair shares a position on may vouch for the edition."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    artist = (await mass.music.artists.get_library_items_by_query(limit=1))[0]
    # the appearance that made them candidates is an original against a remaster
    for track, version in ((track_1, "Deluxe Edition"), (track_2, "2014 Remaster")):
        album_id = (
            await mass.music.database.get_rows(
                DB_TABLE_ALBUM_TRACKS, {"track_id": int(track.item_id)}
            )
        )[0]["album_id"]
        await mass.music.database.update(
            DB_TABLE_ALBUMS, {"item_id": album_id}, {"version": version}
        )
    # both also turn up on one compilation, but at different positions on it
    compilation = await _add_album(mass, "spotify_instance", artist, name="Some Compilation")
    for track, track_number in ((track_1, 5), (track_2, 9)):
        await mass.music.database.insert(
            DB_TABLE_ALBUM_TRACKS,
            {
                "track_id": int(track.item_id),
                "album_id": int(compilation.item_id),
                "disc_number": 1,
                "track_number": track_number,
            },
        )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_merges_across_an_ignorable_album_edition(mass: MusicAssistant) -> None:
    """A quality label like Hi-Res is not a different edition, so it must not block a merge."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    album_id = (
        await mass.music.database.get_rows(
            DB_TABLE_ALBUM_TRACKS, {"track_id": int(track_2.item_id)}
        )
    )[0]["album_id"]
    await mass.music.database.update(
        DB_TABLE_ALBUMS, {"item_id": album_id}, {"version": "Hi-Res Version"}
    )

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_1.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
    }
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_2.item_id)


async def test_merges_titles_that_differ_only_in_punctuation(mass: MusicAssistant) -> None:
    """A curly apostrophe against a straight one is the same title, so the rows still merge."""
    track_1, track_2 = await _build_duplicate_pair(
        mass,
        _TrackSpec(title="You\u2019re My Best Friend"),
        _TrackSpec("qobuz_instance", title="You're My Best Friend"),
    )

    await mass.music._reconcile_duplicate_tracks()

    surviving = await mass.music.tracks.get_library_item(track_1.item_id)
    assert {mapping.provider_domain for mapping in surviving.provider_mappings} == {
        "spotify",
        "qobuz",
    }
    with pytest.raises(MediaNotFoundError):
        await mass.music.tracks.get_library_item(track_2.item_id)


async def test_keeps_distinct_recordings_apart(mass: MusicAssistant) -> None:
    """Two different MusicBrainz recordings are different tracks, whatever else lines up."""
    track_1, track_2 = await _build_duplicate_pair(
        mass,
        _TrackSpec(mbid="11111111-1111-1111-1111-111111111111"),
        _TrackSpec("qobuz_instance", mbid="22222222-2222-2222-2222-222222222222"),
    )

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


async def test_ignores_tracks_with_an_unreported_album_position(mass: MusicAssistant) -> None:
    """Two unknown track numbers agree on nothing, so they are no evidence of a duplicate."""
    track_1, track_2 = await _build_duplicate_pair(mass)
    for track in (track_1, track_2):
        await mass.music.database.update(
            DB_TABLE_ALBUM_TRACKS, {"track_id": int(track.item_id)}, {"track_number": 0}
        )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_ignores_albums_whose_title_normalizes_to_nothing(mass: MusicAssistant) -> None:
    """Symbol-only album titles are not treated as agreement, they match everything."""
    track_1, track_2 = await _build_duplicate_pair(
        mass, second=_TrackSpec("qobuz_instance", album_name="+")
    )
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
    track_1, track_2 = await _build_duplicate_pair(mass, second=_TrackSpec("spotify_instance"))

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_requires_agreement_on_the_album(mass: MusicAssistant) -> None:
    """Tracks that sit on differently titled albums are not treated as duplicates."""
    track_1, track_2 = await _build_duplicate_pair(
        mass, second=_TrackSpec("qobuz_instance", album_name="Greatest Hits")
    )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_requires_agreement_on_the_album_position(mass: MusicAssistant) -> None:
    """Tracks at a different position on the same album are not treated as duplicates."""
    track_1, track_2 = await _build_duplicate_pair(
        mass, second=_TrackSpec("qobuz_instance", track_number=4)
    )

    await mass.music._reconcile_duplicate_tracks()

    assert await mass.music.tracks.get_library_item(track_1.item_id)
    assert await mass.music.tracks.get_library_item(track_2.item_id)


async def test_ignores_tracks_with_a_large_duration_difference(mass: MusicAssistant) -> None:
    """A duration difference beyond the tolerance rules out a duplicate."""
    track_1, track_2 = await _build_duplicate_pair(
        mass, second=_TrackSpec("qobuz_instance", duration=260)
    )

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


async def test_a_short_batch_ends_the_walk() -> None:
    """A batch that is not full means the end of the library has been reached."""
    ctrl = _bare_controller([{"item_id_1": 7, "item_id_2": 9}])
    ctrl._track_reconciliation_cursor = (5, 6)

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=True)):
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_cursor is None


async def test_a_finished_walk_does_not_query_again() -> None:
    """A duplicate-free library must not pay for a scan every hour to prove it."""
    ctrl = _bare_controller([])
    candidate_query = AsyncMock(return_value=[])
    ctrl._database = Mock(get_rows_from_query=candidate_query)
    ctrl._track_reconciliation_cursor = None

    await ctrl._reconcile_duplicate_tracks()

    assert not candidate_query.called


async def test_a_completed_sync_does_not_restart_a_walk_in_progress() -> None:
    """Rewinding mid-walk would re-examine the same prefix forever, starving the rest."""
    ctrl = _bare_controller(
        [
            {"item_id_1": index, "item_id_2": 900 + index}
            for index in range(1, TRACK_RECONCILIATION_BATCH_SIZE + 1)
        ]
    )
    ctrl.mass = Mock(tasks=Mock(get_tasks_by_metadata=Mock(return_value=[])))

    with patch.object(ctrl, "_queue_database_cleanup_task", Mock()):
        ctrl._handle_sync_completion_check()
    assert ctrl._track_reconciliation_cursor == (0, 0)

    # a full batch means the walk is still in progress, so it keeps its place
    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=False)):
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_cursor == (
        TRACK_RECONCILIATION_BATCH_SIZE,
        900 + TRACK_RECONCILIATION_BATCH_SIZE,
    )
    assert ctrl._track_reconciliation_rescan_due


async def test_the_next_pass_starts_once_the_walk_reaches_the_end() -> None:
    """The rescan a sync asked for happens as soon as the current walk drains."""
    ctrl = _bare_controller([{"item_id_1": 7, "item_id_2": 9}])
    ctrl._track_reconciliation_cursor = None
    ctrl._track_reconciliation_rescan_due = True

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=False)):
        await ctrl._reconcile_duplicate_tracks()

    # the rescan rewound to the top, walked to the end again and finished there
    assert ctrl._track_reconciliation_cursor is None
    assert not ctrl._track_reconciliation_rescan_due


def _persisting_mass(stored: dict[str, object]) -> Mock:
    """Build a mass stand-in whose raw core config survives being read back."""
    return Mock(
        tasks=Mock(get_tasks_by_metadata=Mock(return_value=[])),
        config=Mock(
            set_raw_core_config_value=lambda _domain, key, value: stored.__setitem__(key, value),
            get_raw_core_config_value=lambda _domain, key, default=None: stored.get(key, default),
        ),
    )


async def test_an_unexpected_failure_keeps_the_pairs_it_never_reached() -> None:
    """A run cut short resumes at the pair it got to, not past the whole batch."""
    ctrl = _bare_controller(
        [{"item_id_1": index, "item_id_2": 900 + index} for index in range(1, 6)]
    )

    async def _boom(item_id_1: int, _item_id_2: int) -> bool:
        if item_id_1 == 3:
            raise RuntimeError("something unexpected")
        return False

    with (
        patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(side_effect=_boom)),
        pytest.raises(RuntimeError),
    ):
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_cursor == (2, 902)


async def test_a_merge_asks_for_another_pass() -> None:
    """Merging moves relations onto the surviving row, which may duplicate an earlier one."""
    ctrl = _bare_controller([{"item_id_1": 1, "item_id_2": 2}])

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=True)):
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_rescan_due


async def test_a_failed_pair_asks_for_another_pass() -> None:
    """A pair that failed on something transient is looked at again rather than dropped."""
    ctrl = _bare_controller([{"item_id_1": 1, "item_id_2": 2}])

    with (
        patch.object(
            ctrl,
            "_merge_duplicate_track_pair",
            AsyncMock(side_effect=MusicAssistantError("transient")),
        ),
        patch(_REPORT_FAILURE),
    ):
        await ctrl._reconcile_duplicate_tracks()

    assert ctrl._track_reconciliation_rescan_due


async def test_a_run_without_merges_asks_for_nothing() -> None:
    """A walk that changed nothing has no reason to start over."""
    ctrl = _bare_controller([{"item_id_1": 1, "item_id_2": 2}])

    with patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(return_value=False)):
        await ctrl._reconcile_duplicate_tracks()

    assert not ctrl._track_reconciliation_rescan_due


async def test_the_walk_resumes_after_a_restart() -> None:
    """Progress is kept across restarts, so a long run of refusals is only crossed once."""
    stored: dict[str, object] = {}
    ctrl = _bare_controller([])
    ctrl.mass = _persisting_mass(stored)
    ctrl._set_track_reconciliation_state((42, 99), True)

    restarted = _bare_controller([])
    restarted.mass = _persisting_mass(stored)
    restarted._restore_track_reconciliation_state()

    assert restarted._track_reconciliation_cursor == (42, 99)
    assert restarted._track_reconciliation_rescan_due


async def test_a_finished_walk_stays_finished_after_a_restart() -> None:
    """A library with nothing left to do does not start scanning again on every boot."""
    stored: dict[str, object] = {}
    ctrl = _bare_controller([])
    ctrl.mass = _persisting_mass(stored)
    ctrl._set_track_reconciliation_state(None, False)

    restarted = _bare_controller([])
    restarted.mass = _persisting_mass(stored)
    restarted._restore_track_reconciliation_state()

    assert restarted._track_reconciliation_cursor is None


async def test_a_first_run_starts_at_the_beginning() -> None:
    """With nothing stored yet the walk starts at the top of the library."""
    ctrl = _bare_controller([])
    ctrl.mass = _persisting_mass({})

    ctrl._restore_track_reconciliation_state()

    assert ctrl._track_reconciliation_cursor == (0, 0)
    assert not ctrl._track_reconciliation_rescan_due


async def test_no_candidate_survives_a_sync_driven_rewind() -> None:
    """Repeated syncs must not keep a later candidate permanently out of reach."""
    pairs = [(index, 900 + index) for index in range(1, TRACK_RECONCILIATION_BATCH_SIZE * 3)]
    seen: set[tuple[int, int]] = set()

    async def _candidates(_sql: str, params: dict[str, int], limit: int) -> list[dict[str, int]]:
        cursor = (params["cursor_item_id_1"], params["cursor_item_id_2"])
        return [
            {"item_id_1": one, "item_id_2": two}
            for one, two in [pair for pair in pairs if pair > cursor][:limit]
        ]

    async def _refuse(item_id_1: int, item_id_2: int) -> bool:
        seen.add((item_id_1, item_id_2))
        return False

    ctrl = _bare_controller([])
    ctrl._database = Mock(get_rows_from_query=AsyncMock(side_effect=_candidates))
    ctrl.mass = Mock(tasks=Mock(get_tasks_by_metadata=Mock(return_value=[])))

    with (
        patch.object(ctrl, "_merge_duplicate_track_pair", AsyncMock(side_effect=_refuse)),
        patch.object(ctrl, "_queue_database_cleanup_task", Mock()),
    ):
        for _ in range(4):
            # a sync completes before every run, as a busy library would do
            ctrl._handle_sync_completion_check()
            await ctrl._reconcile_duplicate_tracks()

    assert seen == set(pairs)


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
