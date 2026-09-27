"""Tests for linking library items to music providers through MusicBrainz."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import (
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.controllers.music.media.base import MediaControllerBase
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.providers.musicbrainz.models import (
    MusicBrainzMedia,
    MusicBrainzRecording,
    MusicBrainzRelease,
    MusicBrainzTrack,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

RECORDING_ID = "0c1d2e3f-4a5b-4c6d-9e8f-7a6b5c4d3e22"
OTHER_RECORDING_ID = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c33"
SPOTIFY_ALBUM_URL = "https://open.spotify.com/album/7eyQXxuf2nGj9d2367Gi5f"
TIDAL_ALBUM_URL = "https://tidal.com/album/79280548"
DEEZER_ALBUM_URL = "https://www.deezer.com/album/5169381"
APPLE_ALBUM_URL = "https://music.apple.com/gb/album/in-rainbows/1109714933"
APPLE_TRACK_URL = "https://music.apple.com/gb/album/15-step/1109714933?i=1109714934"
SPOTIFY_TRACK_URL = "https://open.spotify.com/track/2Ex8hBvUhZjXjJpZjJZ0aA"
TIDAL_MAPPING = ProviderMapping(
    item_id="79280548", provider_domain="tidal", provider_instance="tidal_1", available=False
)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _library_album(item_id: str = "1", *mappings: ProviderMapping) -> Album:
    """Return a library album with the given provider mappings."""
    return Album(
        item_id=item_id,
        provider="library",
        name="In Rainbows",
        provider_mappings=set(mappings),
    )


def _library_track(
    item_id: str,
    number: int,
    *,
    name: str | None = None,
    duration: int = 237,
    disc_number: int = 0,
    isrcs: Sequence[str] = (),
    mbid: str | None = None,
    mappings: Sequence[ProviderMapping] = (),
    looked_up: bool = False,
) -> Track:
    """Return a library album track at the given position."""
    external_ids = {(ExternalID.ISRC, isrc) for isrc in isrcs}
    if mbid:
        external_ids.add((ExternalID.MB_RECORDING, mbid))
    track = Track(
        item_id=item_id,
        provider="library",
        name=name or f"Track {number}",
        duration=duration,
        disc_number=disc_number,
        track_number=number,
        external_ids=external_ids,
        provider_mappings=set(mappings),
    )
    if looked_up:
        track.metadata.last_musicbrainz_lookup = 1
    return track


def _provider_track(
    item_id: str,
    number: int,
    *,
    name: str | None = None,
    duration: int = 237,
    isrcs: Sequence[str] = (),
    instance: str = "spotify_1",
) -> Track:
    """Return a track as a provider lists it on an album."""
    return Track(
        item_id=item_id,
        provider=instance,
        name=name or f"Track {number}",
        duration=duration,
        disc_number=1,
        track_number=number,
        external_ids={(ExternalID.ISRC, isrc) for isrc in isrcs},
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=instance.split("_", maxsplit=1)[0],
                provider_instance=instance,
            )
        },
    )


def _recording(
    recording_id: str, title: str, length: int | None, *isrcs: str
) -> MusicBrainzRecording:
    """Return a MusicBrainz recording."""
    return MusicBrainzRecording(id=recording_id, title=title, length=length, isrcs=list(isrcs))


def _release(*media: Sequence[MusicBrainzRecording]) -> MusicBrainzRelease:
    """Return a MusicBrainz release with the given recordings, one sequence per medium."""
    return MusicBrainzRelease(
        id="5d9c6e8a-1c3b-4f2e-9a7d-2b8c4e6f0a11",
        title="In Rainbows",
        media=[
            MusicBrainzMedia(
                position=disc,
                track_count=len(recordings),
                tracks=[
                    MusicBrainzTrack(
                        id=f"t-{disc}-{position}",
                        number=str(position),
                        title=recording.title,
                        length=recording.length,
                        position=position,
                        recording=recording,
                    )
                    for position, recording in enumerate(recordings, start=1)
                ],
            )
            for disc, recordings in enumerate(media, start=1)
        ],
    )


def _instance(instance_id: str) -> Mock:
    """Return a loaded, available provider instance stub."""
    instance = Mock()
    instance.instance_id = instance_id
    instance.available = True
    return instance


@dataclass
class _Harness:
    """A media controller under test together with its mocked IO boundaries."""

    ctrl: MediaControllerBase[Any]
    set_provider_mappings: AsyncMock
    merge: AsyncMock
    get_provider_item: AsyncMock

    def stored_mappings(self) -> set[ProviderMapping]:
        """Return the provider mappings the controller persisted."""
        assert self.set_provider_mappings.await_count == 1
        assert self.set_provider_mappings.await_args is not None
        return set(self.set_provider_mappings.await_args.args[1])


@contextmanager
def _harness(
    db_item: Album | Track,
    *,
    loaded: dict[str, list[str]],
    owners: dict[tuple[str, str], str] | None = None,
    provider_errors: dict[str, Exception] | None = None,
) -> Iterator[_Harness]:
    """
    Yield the item's controller with every IO boundary mocked.

    :param db_item: The library item under test; also what the controller loads by id.
    :param loaded: Provider instance ids per loaded provider domain.
    :param owners: Library item id per (provider instance, provider item id) already held.
    :param provider_errors: Error the provider raises per provider item id.
    """
    ctrl: MediaControllerBase[Any]
    if isinstance(db_item, Album):
        ctrl = AlbumsController.__new__(AlbumsController)
    else:
        ctrl = TracksController.__new__(TracksController)
    ctrl.logger = logging.getLogger("test.musicbrainz.linking")
    ctrl.mass = Mock()
    ctrl._db_add_lock = asyncio.Lock()
    ctrl.mass.music.get_provider_instances = Mock(
        side_effect=lambda domain, **_kwargs: [_instance(x) for x in loaded.get(domain, [])]
    )
    ctrl.mass.music.match_provider_instances = Mock(return_value=False)
    ctrl.mass.signal_event = Mock()

    async def _owner(item_id: str, instance: str) -> Album | None:
        if owner_id := (owners or {}).get((instance, item_id)):
            return _library_album(owner_id)
        return None

    async def _provider_item(item_id: str, _instance: str, **_kwargs: object) -> Album:
        if error := (provider_errors or {}).get(item_id):
            raise error
        return _library_album(item_id)

    set_provider_mappings = AsyncMock()
    merge = AsyncMock()
    get_provider_item = AsyncMock(side_effect=_provider_item)
    with patch.multiple(
        ctrl,
        get_library_item_by_prov_id=AsyncMock(side_effect=_owner),
        get_library_item=AsyncMock(return_value=db_item),
        set_provider_mappings=set_provider_mappings,
        _merge_library_items_batched=merge,
        get_provider_item=get_provider_item,
    ):
        yield _Harness(ctrl, set_provider_mappings, merge, get_provider_item)


# ---------------------------------------------------------------------------
# link_musicbrainz_mappings
# ---------------------------------------------------------------------------


async def test_linker_adds_one_mapping_per_linked_provider_not_yet_mapped() -> None:
    """Linked providers get a mapping, except one the item already has (even unavailable)."""
    album = _library_album("1", TIDAL_MAPPING)
    with _harness(
        album, loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"], "apple_music": []}
    ) as harness:
        added = await harness.ctrl.link_musicbrainz_mappings(
            album, [SPOTIFY_ALBUM_URL, TIDAL_ALBUM_URL, DEEZER_ALBUM_URL]
        )

    assert [(m.provider_domain, m.item_id) for m in added] == [
        ("spotify", "7eyQXxuf2nGj9d2367Gi5f")
    ]
    assert added[0].in_library is False
    assert added[0].available is True
    assert harness.stored_mappings() == {TIDAL_MAPPING, *added}
    harness.get_provider_item.assert_not_awaited()


async def test_linker_drops_a_link_another_library_item_holds() -> None:
    """A linked item that belongs to another library item is neither added nor merged."""
    album = _library_album("1")
    with _harness(
        album,
        loaded={"spotify": ["spotify_1"], "tidal": ["tidal_1"]},
        owners={("spotify_1", "7eyQXxuf2nGj9d2367Gi5f"): "99"},
    ) as harness:
        added = await harness.ctrl.link_musicbrainz_mappings(
            album, [SPOTIFY_ALBUM_URL, TIDAL_ALBUM_URL]
        )

    assert [m.provider_domain for m in added] == ["tidal"]
    assert harness.stored_mappings() == set(added)
    harness.merge.assert_not_awaited()


async def test_linker_checks_apple_music_albums_and_trusts_the_other_providers() -> None:
    """An Apple Music album link is verified against the storefront; the rest is trusted."""
    album = _library_album("1")
    loaded = {"spotify": ["spotify_1"], "apple_music": ["apple_music_1"]}
    errors: dict[str, Exception] = {"1109714933": MediaNotFoundError("gone")}
    with _harness(album, loaded=loaded, provider_errors=errors) as harness:
        added = await harness.ctrl.link_musicbrainz_mappings(
            album, [APPLE_ALBUM_URL, SPOTIFY_ALBUM_URL]
        )

    assert [m.provider_domain for m in added] == ["spotify"]
    harness.get_provider_item.assert_awaited_once_with(
        "1109714933", "apple_music_1", allow_fallback=False
    )

    album = _library_album("1")
    with _harness(album, loaded=loaded) as harness:
        added = await harness.ctrl.link_musicbrainz_mappings(
            album, [APPLE_ALBUM_URL, SPOTIFY_ALBUM_URL]
        )

    assert [m.provider_domain for m in added] == ["apple_music", "spotify"]


@pytest.mark.parametrize(
    "error", [RateLimited("slow down", backoff_time=30), ResourceTemporarilyUnavailable("busy")]
)
async def test_linker_treats_an_apple_music_hiccup_as_unverified(error: Exception) -> None:
    """An Apple Music album whose check fails on the provider is not linked, nor merged."""
    album = _library_album("1")
    loaded = {"spotify": ["spotify_1"], "apple_music": ["apple_music_1"]}
    with _harness(album, loaded=loaded, provider_errors={"1109714933": error}) as harness:
        added = await harness.ctrl.link_musicbrainz_mappings(
            album, [APPLE_ALBUM_URL, SPOTIFY_ALBUM_URL]
        )

    assert [m.provider_domain for m in added] == ["spotify"]
    harness.merge.assert_not_awaited()


async def test_linker_trusts_apple_music_track_links() -> None:
    """Only albums are checked on Apple Music; a track link is taken as is."""
    track = _library_track("1", 1)
    with _harness(
        track, loaded={"apple_music": ["apple_music_1"], "spotify": ["spotify_1"]}
    ) as harness:
        added = await harness.ctrl.link_musicbrainz_mappings(
            track, [APPLE_TRACK_URL, SPOTIFY_TRACK_URL]
        )

    assert [m.provider_domain for m in added] == ["apple_music", "spotify"]
    harness.get_provider_item.assert_not_awaited()


async def test_unclaimed_mappings_never_merge_library_items() -> None:
    """Adding mappings never merges: one held by another item is dropped, the rest added."""
    track = _library_track("1", 1)
    free = ProviderMapping(item_id="free", provider_domain="tidal", provider_instance="tidal_1")
    taken = ProviderMapping(item_id="taken", provider_domain="deezer", provider_instance="deezer_1")
    with _harness(track, loaded={}, owners={("deezer_1", "taken"): "7"}) as harness:
        added = await harness.ctrl.add_unclaimed_provider_mappings("1", [taken, free])

    assert added == [free]
    assert harness.stored_mappings() == {free}
    harness.merge.assert_not_awaited()


async def test_unclaimed_mappings_are_checked_and_written_under_the_add_lock() -> None:
    """Ownership is checked and the mappings written while the add lock is held throughout."""
    track = _library_track("1", 1)
    free = ProviderMapping(item_id="free", provider_domain="tidal", provider_instance="tidal_1")
    taken = ProviderMapping(item_id="taken", provider_domain="deezer", provider_instance="deezer_1")
    locked_during: list[bool] = []
    with _harness(track, loaded={}, owners={("deezer_1", "taken"): "7"}) as harness:
        lock = harness.ctrl._db_add_lock
        owner_lookup = harness.ctrl.get_library_item_by_prov_id
        assert isinstance(owner_lookup, AsyncMock)
        owner_side_effect = owner_lookup.side_effect

        async def _owner_under_lock(item_id: str, instance: str) -> Album | None:
            locked_during.append(lock.locked())
            return cast("Album | None", await owner_side_effect(item_id, instance))

        owner_lookup.side_effect = _owner_under_lock
        harness.set_provider_mappings.side_effect = lambda *_args: locked_during.append(
            lock.locked()
        )

        added = await harness.ctrl.add_unclaimed_provider_mappings("1", [taken, free])

    assert added == [free]
    assert locked_during == [True, True, True]
    assert harness.stored_mappings() == {free}
    harness.merge.assert_not_awaited()
    assert not lock.locked()


# ---------------------------------------------------------------------------
# link_album_tracks
# ---------------------------------------------------------------------------


@dataclass
class _AlbumHarness:
    """An albums controller whose library tracks and provider tracklists are mocked."""

    ctrl: AlbumsController
    tracks: Mock
    provider_album_tracks: AsyncMock

    def updated_tracks(self) -> list[Track]:
        """Return the library tracks persisted by the release step."""
        return [call.args[1] for call in self.tracks.update_item_in_library.await_args_list]

    def linked(self) -> list[tuple[str, set[str]]]:
        """Return (library track id, provider item ids) per provider link made."""
        return [
            (call.args[0], {m.item_id for m in call.args[1]})
            for call in self.tracks.add_unclaimed_provider_mappings.await_args_list
        ]


SPOTIFY_ALBUM = ProviderMapping(
    item_id="sp-album", provider_domain="spotify", provider_instance="spotify_1"
)
TIDAL_ALBUM = ProviderMapping(
    item_id="td-album", provider_domain="tidal", provider_instance="tidal_1"
)


@contextmanager
def _album_harness(
    provider_tracks: dict[str, list[Track] | Exception] | None = None,
) -> Iterator[_AlbumHarness]:
    """
    Yield an albums controller with the provider tracklists and the track writes mocked.

    :param provider_tracks: Provider tracklist (or error to raise) per provider album id.
    """
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.logger = logging.getLogger("test.musicbrainz.linking")
    ctrl.mass = Mock()
    ctrl.mass.music.database.deferred_commit = MagicMock()
    ctrl.mass.music.tracks.update_item_in_library = AsyncMock()
    ctrl.mass.music.tracks.add_unclaimed_provider_mappings = AsyncMock()

    async def _tracklist(item_id: str, _instance: str) -> list[Track]:
        result = (provider_tracks or {}).get(item_id, [])
        if isinstance(result, Exception):
            raise result
        return result

    provider_album_tracks = AsyncMock(side_effect=_tracklist)
    with patch.object(ctrl, "_get_provider_album_tracks", provider_album_tracks):
        yield _AlbumHarness(ctrl, ctrl.mass.music.tracks, provider_album_tracks)


async def test_release_tracks_fill_recording_ids_isrcs_and_marker_by_position() -> None:
    """Library tracks at the release's positions get its recording id, ISRCs and the marker."""
    db_tracks = [_library_track("t1", 1), _library_track("t2", 2, disc_number=1)]
    release = _release(
        [
            _recording(RECORDING_ID, "Track 1", 238000, "GBSTK0700001", "GBSTK0700002"),
            _recording(OTHER_RECORDING_ID, "Track 2", None),
        ]
    )
    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), db_tracks, release)

    updated = harness.updated_tracks()
    assert [track.item_id for track in updated] == ["t1", "t2"]
    assert updated[0].mbid == RECORDING_ID
    assert {v for k, v in updated[0].external_ids if k == ExternalID.ISRC} == {
        "GBSTK0700001",
        "GBSTK0700002",
    }
    assert updated[1].mbid == OTHER_RECORDING_ID
    assert all(track.metadata.last_musicbrainz_lookup is not None for track in updated)


async def test_release_tracks_skip_a_length_or_title_mismatch() -> None:
    """A recording that is 20 seconds off or named differently is not the library track."""
    db_tracks = [_library_track("t1", 1, duration=200), _library_track("t2", 2)]
    release = _release(
        [_recording(RECORDING_ID, "Track 1", 220000), _recording(OTHER_RECORDING_ID, "Other", None)]
    )
    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), db_tracks, release)

    assert harness.updated_tracks() == []


async def test_release_tracks_keep_an_existing_recording_id() -> None:
    """A recording id the library track already carries wins, its ISRCs are still filled in."""
    db_tracks = [_library_track("t1", 1, mbid=OTHER_RECORDING_ID)]
    release = _release([_recording(RECORDING_ID, "Track 1", 237000, "GBSTK0700001")])
    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), db_tracks, release)

    (updated,) = harness.updated_tracks()
    assert updated.mbid == OTHER_RECORDING_ID
    assert (ExternalID.ISRC, "GBSTK0700001") in updated.external_ids


async def test_release_tracks_are_matched_per_disc() -> None:
    """On a multi-disc release each disc's first track gets its own recording."""
    db_tracks = [
        _library_track("d1", 1, name="Opener", disc_number=1),
        _library_track("d2", 1, name="Closer", disc_number=2),
    ]
    release = _release(
        [_recording(RECORDING_ID, "Opener", None)], [_recording(OTHER_RECORDING_ID, "Closer", None)]
    )
    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), db_tracks, release)

    assert [(track.item_id, track.mbid) for track in harness.updated_tracks()] == [
        ("d1", RECORDING_ID),
        ("d2", OTHER_RECORDING_ID),
    ]


async def test_release_tracks_without_a_disc_number_match_the_first_disc() -> None:
    """A digital single-disc library track stored as disc 0 is a track of medium 1."""
    db_tracks = [_library_track("t1", 1, disc_number=0)]
    release = _release([_recording(RECORDING_ID, "Track 1", None)])
    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), db_tracks, release)

    (updated,) = harness.updated_tracks()
    assert updated.mbid == RECORDING_ID


async def test_release_tracks_that_gain_nothing_are_not_rewritten() -> None:
    """A track that already carries the recording's ids is only written once, for its marker."""
    release = _release([_recording(RECORDING_ID, "Track 1", None, "GBSTK0700001")])
    complete = _library_track("t1", 1, isrcs=["GBSTK0700001"], mbid=RECORDING_ID)
    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), [complete], release)
    (updated,) = harness.updated_tracks()
    assert updated.metadata.last_musicbrainz_lookup is not None

    with _album_harness() as harness:
        await harness.ctrl.link_album_tracks(_library_album(), [complete], release)
    harness.tracks.update_item_in_library.assert_not_awaited()


async def test_provider_tracks_are_matched_by_isrc_before_position() -> None:
    """A shared ISRC links a provider track whatever its position; the rest by position."""
    db_tracks = [
        _library_track("t1", 1, isrcs=["GBSTK0700001"]),
        _library_track("t2", 2, isrcs=["GBSTK0700002"]),
        _library_track("t3", 3),
    ]
    provider_tracks = [
        # the provider lists the first two tracks in the other order
        _provider_track("sp-2", 1, name="Track 2", isrcs=["GBSTK0700002"]),
        _provider_track("sp-1", 2, name="Track 1", isrcs=["GBSTK0700001"]),
        _provider_track("sp-3", 3),
    ]
    with _album_harness({"sp-album": provider_tracks}) as harness:
        await harness.ctrl.link_album_tracks(_library_album("1", SPOTIFY_ALBUM), db_tracks, None)

    assert harness.linked() == [("t1", {"sp-1"}), ("t2", {"sp-2"}), ("t3", {"sp-3"})]
    harness.provider_album_tracks.assert_awaited_once_with("sp-album", "spotify_1")


async def test_provider_tracks_by_position_need_a_close_duration_and_title() -> None:
    """Without an ISRC, a provider track must sit at the position with the same name and length."""
    db_tracks = [_library_track("t1", 1), _library_track("t2", 2), _library_track("t3", 3)]
    provider_tracks = [
        _provider_track("sp-1", 1, duration=243),
        _provider_track("sp-2", 2, duration=260),
        _provider_track("sp-3", 3, name="Another Track"),
    ]
    with _album_harness({"sp-album": provider_tracks}) as harness:
        await harness.ctrl.link_album_tracks(_library_album("1", SPOTIFY_ALBUM), db_tracks, None)

    assert harness.linked() == [("t1", {"sp-1"})]


async def test_provider_tracks_ignore_a_placeholder_isrc() -> None:
    """An invalid ISRC both sides carry, such as "unknown", never links two tracks."""
    db_tracks = [_library_track("t1", 1, isrcs=["unknown"])]
    provider_tracks = [_provider_track("sp-2", 2, name="Another Track", isrcs=["unknown"])]
    with _album_harness({"sp-album": provider_tracks}) as harness:
        await harness.ctrl.link_album_tracks(_library_album("1", SPOTIFY_ALBUM), db_tracks, None)

    assert harness.linked() == []


async def test_provider_tracks_skip_a_library_track_already_mapped_to_the_provider() -> None:
    """A library track the provider is already mapped for is left alone."""
    mapped = ProviderMapping(
        item_id="sp-old", provider_domain="spotify", provider_instance="spotify_2"
    )
    db_tracks = [_library_track("t1", 1, mappings=[mapped]), _library_track("t2", 2)]
    provider_tracks = [_provider_track("sp-1", 1), _provider_track("sp-2", 2)]
    with _album_harness({"sp-album": provider_tracks}) as harness:
        await harness.ctrl.link_album_tracks(_library_album("1", SPOTIFY_ALBUM), db_tracks, None)

    assert harness.linked() == [("t2", {"sp-2"})]


async def test_provider_tracklists_are_only_fetched_while_a_track_lacks_the_provider() -> None:
    """An album provider every track is mapped to is done; one some track lacks is fetched."""
    spotify_track = ProviderMapping(
        item_id="sp-1", provider_domain="spotify", provider_instance="spotify_1"
    )
    tidal_track = ProviderMapping(
        item_id="td-1", provider_domain="tidal", provider_instance="tidal_1"
    )
    album = _library_album("1", SPOTIFY_ALBUM, TIDAL_ALBUM)
    db_tracks = [
        _library_track("t1", 1, mappings=[spotify_track, tidal_track]),
        _library_track("t2", 2, mappings=[spotify_track]),
    ]
    provider_tracks: dict[str, list[Track] | Exception] = {
        "sp-album": [_provider_track("sp-1", 1), _provider_track("sp-2", 2)],
        "td-album": [
            _provider_track("td-1", 1, instance="tidal_1"),
            _provider_track("td-2", 2, instance="tidal_1"),
        ],
    }
    with _album_harness(provider_tracks) as harness:
        await harness.ctrl.link_album_tracks(album, db_tracks, None)

    harness.provider_album_tracks.assert_awaited_once_with("td-album", "tidal_1")
    assert harness.linked() == [("t2", {"td-2"})]

    db_tracks[1].provider_mappings.add(tidal_track)
    with _album_harness(provider_tracks) as harness:
        await harness.ctrl.link_album_tracks(album, db_tracks, None)

    harness.provider_album_tracks.assert_not_awaited()


async def test_provider_tracklist_failure_skips_that_provider_only() -> None:
    """A provider whose tracklist cannot be fetched is skipped; the next provider still links."""
    db_tracks = [_library_track("t1", 1)]
    provider_tracks: dict[str, list[Track] | Exception] = {
        "sp-album": RateLimited("slow down", backoff_time=30),
        "td-album": [_provider_track("td-1", 1, instance="tidal_1")],
    }
    with _album_harness(provider_tracks) as harness:
        await harness.ctrl.link_album_tracks(
            _library_album("1", SPOTIFY_ALBUM, TIDAL_ALBUM), db_tracks, None
        )

    assert harness.linked() == [("t1", {"td-1"})]


async def test_link_album_tracks_without_library_tracks_does_nothing() -> None:
    """An album without library tracks has nothing to carry its identity over to."""
    release = _release([_recording(RECORDING_ID, "Track 1", None)])
    with _album_harness({"sp-album": [_provider_track("sp-1", 1)]}) as harness:
        await harness.ctrl.link_album_tracks(_library_album("1", SPOTIFY_ALBUM), [], release)

    harness.provider_album_tracks.assert_not_awaited()
    assert harness.updated_tracks() == []
