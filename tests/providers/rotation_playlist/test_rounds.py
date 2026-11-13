"""Tests for the rounds and the progress store of the Rotation playlists."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.providers.rotation_playlist.engine import RunContext
from music_assistant.providers.rotation_playlist.models import (
    Condition,
    ExpandStep,
    FilterStep,
    FlattenStep,
    ItemKind,
    LimitStep,
    SortKey,
    SortStep,
    SourceStep,
    Step,
)
from music_assistant.providers.rotation_playlist.rounds import (
    ProgressStore,
    RoundState,
    UnitTracks,
    next_batch,
    played_for,
    round_status,
    track_uris,
)

from .test_engine import (
    GLOCKEN_TRACKS,
    FakeLibrary,
    _die_drei_steps,
    _library,
    _mass,
    _names,
    _page,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from music_assistant_models.media_items import Track


def _factory() -> Callable[[int], RunContext]:
    mass = _mass(_library())
    return lambda seed: RunContext(mass, seed=seed)


def _album_name(track: Track) -> str:
    assert track.album is not None
    return track.album.name


def _uris(tracks: list[Track]) -> list[str]:
    return [str(track.uri) for track in tracks]


async def test_next_batch_continues_inside_album() -> None:
    """After two played tracks of an album, the batch starts with the third."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    state = RoundState(seed=3, played=set(_uris(full[:2])))
    batch = await next_batch(steps, state, factory, max_tracks=100)
    assert _uris(batch) == _uris(full[2:])
    assert len(full) == 6


async def test_next_batch_skips_finished_units() -> None:
    """A fully played album is skipped."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    first_album = _album_name(full[0])
    done = [track for track in full if _album_name(track) == first_album]
    batch = await next_batch(
        steps, RoundState(seed=3, played=set(_uris(done))), factory, max_tracks=100
    )
    assert all(_album_name(track) != first_album for track in batch)
    assert len(batch) == len(full) - len(done)


async def test_next_batch_is_stable_without_progress() -> None:
    """Browsing without marking tracks never advances."""
    factory = _factory()
    state = RoundState(seed=5)
    first = await next_batch(_die_drei_steps(), state, factory)
    second = await next_batch(_die_drei_steps(), state, factory)
    assert _uris(first) == _uris(second)
    assert state.played == set()
    assert state.round == 1


async def test_new_round_after_everything_played() -> None:
    """When all is played the next round starts with a new seed and no played tracks."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    state = RoundState(seed=3, played=set(_uris(full)))
    batch = await next_batch(steps, state, factory, max_tracks=100)
    assert state.round == 2
    assert state.seed != 3
    assert state.played == set()
    assert len(batch) == len(full)


async def test_empty_pipeline_returns_empty_no_round() -> None:
    """An empty pipeline gives nothing and does not start a new round."""
    factory = _factory()
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("title", "contains", "Nichts")]),
    ]
    state = RoundState()
    assert await next_batch(steps, state, factory) == []
    assert state.round == 1


async def test_removed_track_and_edited_steps_mid_round() -> None:
    """Played uris the pipeline does not know are ignored."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    state = RoundState(seed=3, played={"library://track/999", str(full[0].uri)})
    batch = await next_batch(steps, state, factory, max_tracks=100)
    assert _uris(batch) == _uris(full[1:])
    assert state.round == 1


async def test_batch_caps_at_max_tracks() -> None:
    """A batch holds at most max_tracks tracks."""
    factory = _factory()
    batch = await next_batch(_die_drei_steps(), RoundState(seed=3), factory, max_tracks=3)
    assert len(batch) == 3
    assert set(_names(batch)) <= set(GLOCKEN_TRACKS) | {
        f"Der Super-Papagei (Kapitel {number})" for number in range(1, 5)
    }


async def test_round_status_counts_played_units() -> None:
    """The status counts the units and those fully played."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    first_album = _album_name(full[0])
    done = [track for track in full if _album_name(track) == first_album]
    state = RoundState(seed=3, played=set(_uris(done)))
    assert await round_status(steps, state, factory) == {
        "round": 1,
        "units_total": 2,
        "units_played": 1,
        "exact": True,
    }


async def test_random_sort_order_follows_seed() -> None:
    """The unit order comes from the seed, so one seed gives one order."""
    factory = _factory()
    steps: list[Step] = [SourceStep(ItemKind.ALBUM), SortStep([SortKey("random")])]
    first = await next_batch(steps, RoundState(seed=1), factory, max_tracks=100)
    again = await next_batch(steps, RoundState(seed=1), factory, max_tracks=100)
    assert _uris(first) == _uris(again)


def test_round_state_tolerates_wrong_types() -> None:
    """Wrong types fall back to the defaults."""
    state = RoundState.from_dict({"round": "x", "seed": None, "played": [1, "a"]})
    assert state.round == 1
    assert 0 <= state.seed < 2**31
    assert state.played == {"a"}
    assert RoundState.from_dict("nope").round == 1


def test_progress_store_tolerates_broken_file(tmp_path: Path) -> None:
    """A broken or oddly shaped progress file gives an empty store."""
    path = tmp_path / "progress.json"
    for content in ("{", json.dumps({"version": 1, "entries": []}), "[]"):
        path.write_text(content)
        store = ProgressStore(str(path))
        store.load()
        assert store.get("r1", "household").played == set()
    ProgressStore(str(tmp_path / "missing.json")).load()


async def test_progress_store_roundtrip(tmp_path: Path) -> None:
    """Saved progress loads again; reset and drop clear it."""
    path = str(tmp_path / "progress.json")
    store = ProgressStore(path)
    assert store.mark_started("r1", "household", "library://track/1") is True
    assert store.mark_started("r1", "household", "library://track/1") is False
    await store.save()
    other = ProgressStore(path)
    other.load()
    assert other.get("r1", "household").played == {"library://track/1"}
    other.reset("r1", "household")
    assert other.get("r1", "household").played == set()
    other.mark_started("r1", "anna", "library://track/2")
    other.drop_rotation("r1")
    assert other.get("r1", "anna").played == set()


async def test_empty_walk_keeps_the_round() -> None:
    """When the steps give nothing for now, the played tracks, round and seed stay."""
    factory = _factory()
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("title", "contains", "Nichts")]),
    ]
    state = RoundState(round=3, seed=9, played={"library://track/11"})
    assert await next_batch(steps, state, factory) == []
    assert (state.round, state.seed, state.played) == (3, 9, {"library://track/11"})


def test_store_get_does_not_store(tmp_path: Path) -> None:
    """Reading an unknown state gives a stable fresh state and stores nothing."""
    store = ProgressStore(str(tmp_path / "progress.json"))
    first = store.get("r1", "anna")
    assert store.get("r1", "anna").seed == first.seed
    assert store._entries == {}
    store.mark_started("r1", "anna", "library://track/1")
    assert store.get("r1", "anna").seed == first.seed
    store.next_round("r1", "anna")
    assert store.get("r1", "anna").round == 2
    assert store.get("r1", "anna").played == set()


async def test_round_status_makes_no_provider_call() -> None:
    """Units of provider albums are not fetched for the status; the counts say so."""
    library = _library()
    mass = _mass(library)
    provider_album = library.albums[0].__class__.from_dict(
        {**library.albums[0].to_dict(), "provider": "spotify", "item_id": "sp1"}
    )
    mass.music.albums.library_items = _page([provider_album, *library.albums[1:]])
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps: list[Step] = [SourceStep(ItemKind.ALBUM)]
    status = await round_status(steps, RoundState(), factory)
    providers = [call.args[1] for call in mass.music.albums.tracks.await_args_list]
    assert "spotify" not in providers
    assert status["units_total"] == 6
    assert status["units_played"] == 0
    assert status["exact"] is False


async def test_round_status_leaves_out_units_without_wanted_tracks() -> None:
    """A unit whose tracks are all disliked or unavailable is not part of the round."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    first_album = _album_name(full[0])
    status = await round_status(
        steps,
        RoundState(seed=3),
        factory,
        keep=_keeping(lambda track: _album_name(track) != first_album),
    )
    assert (status["units_total"], status["units_played"]) == (1, 0)


def _keeping(wanted: Callable[[Any], bool]) -> Callable[[list[Any]], Awaitable[list[Any]]]:
    """Return a round filter that keeps the tracks a test wants."""

    async def keep(tracks: list[Any]) -> list[Any]:
        return [track for track in tracks if wanted(track)]

    return keep


def _album_calls(factory_mass: MagicMock, album_id: str) -> int:
    return sum(
        1 for call in factory_mass.music.albums.tracks.await_args_list if call.args[0] == album_id
    )


async def test_played_unit_is_not_expanded_again() -> None:
    """A walk does not mark units played, but hands out their tracks; a played unit is skipped."""
    mass = _mass(_library())
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps = _die_drei_steps()
    units: UnitTracks = {}
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100, units=units)
    first_album = full[0].album
    assert first_album is not None
    first_uri = f"library://album/{first_album.item_id}"
    done = [track for track in full if _album_name(track) == first_album.name]
    assert {uri for track in units[first_uri].tracks for uri in track} >= set(_uris(done))
    state = RoundState(seed=3, played=set(_uris(done)))
    batch = await next_batch(steps, state, factory, max_tracks=100)
    assert state.played_units == set()

    state.played_units.add(first_uri)
    mass.music.albums.tracks.reset_mock()
    assert _uris(await next_batch(steps, state, factory, max_tracks=100)) == _uris(batch)
    assert _album_calls(mass, first_album.item_id) == 0
    status = await round_status(steps, state, factory)
    assert (status["units_total"], status["units_played"]) == (2, 1)
    assert _album_calls(mass, first_album.item_id) == 0


async def test_played_units_end_the_round() -> None:
    """When every unit is remembered as played, the next batch starts a new round."""
    factory = _factory()
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    units = {f"library://album/{track.album.item_id}" for track in full if track.album}
    assert len(units) == 2
    state = RoundState(seed=3, played=set(_uris(full)), played_units=units)
    batch = await next_batch(steps, state, factory, max_tracks=100)
    assert (state.round, len(batch), state.played_units) == (2, 6, set())


async def test_limit_after_expand_keeps_its_units() -> None:
    """A limit on the expanded units still picks the same units when some were played."""
    mass = _mass(_library())
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        ExpandStep(ItemKind.TRACK),
        SortStep([SortKey("track_number")]),
        LimitStep(count=2),
    ]
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    first, second = full[:4], full[4:]
    assert {_album_name(track) for track in second} != {_album_name(full[0])}
    state = RoundState(seed=3, played=set(_uris(first)))
    await next_batch(steps, state, factory, max_tracks=100)
    assert _uris(await next_batch(steps, state, factory, max_tracks=100)) == _uris(second)


def test_round_state_loads_played_units_tolerantly() -> None:
    """Old progress without played units and wrong types load as no played units."""
    assert RoundState.from_dict({"round": 2, "seed": 5, "played": []}).played_units == set()
    assert RoundState.from_dict({"played_units": "x"}).played_units == set()
    state = RoundState.from_dict({"played_units": ["library://album/1", 3]})
    assert state.played_units == {"library://album/1"}
    assert RoundState.from_dict(state.to_dict()).played_units == {"library://album/1"}


def _failing_album_tracks(mass: MagicMock, failing_id: str, error: Exception | None = None) -> None:
    """Let the track listing of one album fail, by default like an unreachable provider."""
    listing = mass.music.albums.tracks.side_effect

    async def tracks(item_id: str, provider: str, **kwargs: Any) -> list[Track]:
        if item_id == failing_id:
            raise error or ProviderUnavailableError("down")
        return list(await listing(item_id, provider, **kwargs))

    mass.music.albums.tracks.side_effect = tracks


@pytest.mark.parametrize("error", [ProviderUnavailableError("down"), TimeoutError()])
@pytest.mark.parametrize("expanded", [True, False])
async def test_failed_fetch_at_the_end_of_a_round_keeps_the_round(
    expanded: bool, error: Exception
) -> None:
    """When the albums left in a round fail to load for now, the round stays as it is."""
    mass = _mass(_library())
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps: list[Step] = _die_drei_steps() if expanded else [SourceStep(ItemKind.ALBUM)]
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    first_album = full[0].album
    assert first_album is not None
    done = [track for track in full if _album_name(track) == first_album.name]
    others = {track.album.item_id for track in full if track.album} - {first_album.item_id}
    for album_id in others:
        _failing_album_tracks(mass, album_id, error)
    state = RoundState(seed=3, played=set(_uris(done)))
    units: UnitTracks = {}
    assert await next_batch(steps, state, factory, max_tracks=100, units=units) == []
    assert (state.round, state.seed, state.played, state.played_units) == (
        1,
        3,
        set(_uris(done)),
        set(),
    )
    assert units == {}


@pytest.mark.parametrize(
    "error",
    [
        MediaNotFoundError("gone"),
        InvalidDataError("broken"),
        InsufficientPermissions("no"),
        UnsupportedFeaturedException("no tracks"),
    ],
)
@pytest.mark.parametrize("expanded", [True, False])
async def test_gone_albums_are_left_out_and_the_round_ends(
    expanded: bool, error: Exception
) -> None:
    """Albums that cannot be listed for good drop out, so the round still ends."""
    mass = _mass(_library())
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps: list[Step] = _die_drei_steps() if expanded else [SourceStep(ItemKind.ALBUM)]
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    first_album = full[0].album
    assert first_album is not None
    done = [track for track in full if _album_name(track) == first_album.name]
    others = {track.album.item_id for track in full if track.album} - {first_album.item_id}
    for album_id in others:
        _failing_album_tracks(mass, album_id, error)
    state = RoundState(seed=3, played=set(_uris(done)))
    batch = await next_batch(steps, state, factory, max_tracks=100)
    assert state.round == 2
    assert sorted(_uris(batch)) == sorted(_uris(done))


def _shared_library() -> tuple[FakeLibrary, Track]:
    """Return the library with one Glocken track also on the Papagei album, and that track."""
    library = _library()
    shared = library.tracks["1"][2]
    library.tracks["2"].append(shared)
    return library, shared


async def test_track_on_two_albums_plays_in_both() -> None:
    """A track on two albums plays in each; playing it for one album keeps it in the other."""
    library, shared = _shared_library()
    mass = _mass(library)
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps = _die_drei_steps()
    units: UnitTracks = {}
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100, units=units)
    assert _uris(full).count(str(shared.uri)) == 2
    first, second = units
    state = RoundState(seed=3)
    assert played_for(state, units, track_uris(shared)) == first
    state.add_play(track_uris(shared), "item1", first)
    assert played_for(state, units, track_uris(shared)) == second
    expected = list(full)
    expected.remove(shared)
    assert _uris(await next_batch(steps, state, factory, max_tracks=100)) == _uris(expected)
    state.add_play(track_uris(shared), "item2", second)
    assert played_for(state, units, track_uris(shared)) is None
    expected.remove(shared)
    assert _uris(await next_batch(steps, state, factory, max_tracks=100)) == _uris(expected)


async def test_resume_skips_only_what_played_in_that_unit() -> None:
    """A rebuilt batch skips the tracks a unit played as part of it, and nothing in the others."""
    library, shared = _shared_library()
    mass = _mass(library)
    factory: Callable[[int], RunContext] = partial(RunContext, mass)
    steps = _die_drei_steps()
    units: UnitTracks = {}
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100, units=units)
    first, second = units
    size = len(units[first].tracks)
    state = RoundState(seed=3)
    # the first album played up to and including the shared track, the second album one other
    for number, track in enumerate(full[: full.index(shared) + 1]):
        state.add_play(track_uris(track), f"item{number}", first)
    other = next(track for track in full[size:] if track is not shared)
    state.add_play(track_uris(other), "item9", second)
    batch = await next_batch(steps, state, factory, max_tracks=100)
    expected = full[full.index(shared) + 1 : size] + [
        track for track in full[size:] if track is not other
    ]
    assert _uris(batch) == _uris(expected)
    assert str(shared.uri) in _uris(batch)


async def test_old_flat_plays_count_for_the_first_unit_that_holds_them() -> None:
    """A play of the old format counts once, for the first unit in play order that holds it."""
    library, shared = _shared_library()
    factory: Callable[[int], RunContext] = partial(RunContext, _mass(library))
    steps = _die_drei_steps()
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100)
    state = RoundState(seed=3, played=track_uris(shared))
    expected = list(full)
    expected.remove(shared)
    assert _uris(await next_batch(steps, state, factory, max_tracks=100)) == _uris(expected)
    status = await round_status(steps, state, factory)
    assert status["units_played"] == 0


async def test_mixed_copies_of_a_track_are_units_of_their_own() -> None:
    """After a flatten each copy of a track is its own unit, numbered in play order."""
    library, shared = _shared_library()
    factory: Callable[[int], RunContext] = partial(RunContext, _mass(library))
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("artist", "is", "library://artist/1")]),
        ExpandStep(ItemKind.TRACK),
        FlattenStep(),
    ]
    units: UnitTracks = {}
    full = await next_batch(steps, RoundState(seed=3), factory, max_tracks=100, units=units)
    uri = str(shared.uri)
    assert _uris(full).count(uri) == 2
    assert {uri, f"{uri}#2"} <= set(units)
    state = RoundState(seed=3)
    state.add_play(track_uris(shared), "item1", played_for(state, units, track_uris(shared)))
    assert state.plays_of(uri)
    assert _uris(await next_batch(steps, state, factory, max_tracks=100)).count(uri) == 1


def test_round_state_reads_old_and_broken_formats() -> None:
    """Stored progress of the old format or with broken plays loads without raising."""
    old = RoundState.from_dict(
        {
            "round": 2,
            "seed": 5,
            "played": ["library://track/13", 7],
            "played_units": ["library://album/9"],
        }
    )
    assert (old.round, old.seed, old.played, old.played_units) == (
        2,
        5,
        {"library://track/13"},
        {"library://album/9"},
    )
    assert old.unit_plays == {}
    for broken in ("garbage", ["a"], {"x": "y"}, {"x": {"q": "z", "r": [3]}}):
        assert RoundState.from_dict({"unit_plays": broken}).unit_plays == {}
    mixed = RoundState.from_dict({"unit_plays": {"x": {"q": "z", "r": ["a", 3]}}})
    assert mixed.unit_plays == {"x": {"r": frozenset({"a"})}}
    state = RoundState(seed=1, played={"a"})
    state.add_play({"b", "c"}, "item1", "library://album/1")
    assert RoundState.from_dict(json.loads(json.dumps(state.to_dict()))) == state
