"""Tests for the Rotation playlist provider, its playlists and its API commands."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.api import SuccessResultMessage
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.enums import EventType, MediaType, PlaybackState
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
)
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import Playlist, ProviderMapping, Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.cache import BYPASS_CACHE, CacheController
from music_assistant.controllers.music.favorites import DislikedTrackKeys
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.providers.rotation_playlist import RotationPlaylistProvider
from music_assistant.providers.rotation_playlist.constants import HOUSEHOLD, PREVIEW_MAX_LIMIT
from music_assistant.providers.rotation_playlist.engine import RunContext, iter_units
from music_assistant.providers.rotation_playlist.explain import explain_steps
from music_assistant.providers.rotation_playlist.models import (
    Condition,
    ExpandStep,
    FilterStep,
    ItemKind,
    SortKey,
    SortStep,
    SourceStep,
    Step,
    StepsError,
    parse_steps,
    steps_to_dict,
)
from music_assistant.providers.rotation_playlist.recipes import RECIPES
from music_assistant.providers.rotation_playlist.rounds import RoundState, derived_seed, next_batch
from music_assistant.providers.rotation_playlist.validation import validate_steps

from .test_engine import GLOCKEN, GLOCKEN_TRACKS, PAPAGEI_TRACKS, _die_drei_steps, _library, _mass

if TYPE_CHECKING:
    from pathlib import Path

INSTANCE = "rotation_playlist"
QUEUE = "queue1"


def _provider(tmp_path: Path, mass: MagicMock | None = None) -> RotationPlaylistProvider:
    library = _library()
    mass = mass or _mass(library)
    artists = {artist.item_id: artist for artist in library.artists}
    mass.music.artists.get_library_item = AsyncMock(side_effect=lambda item_id: artists[item_id])
    mass.storage_path = str(tmp_path)
    mass.music.playlists.add_item_to_library = AsyncMock(side_effect=lambda item, **_: item)
    mass.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.playlists.get_library_items_by_prov_id = AsyncMock(return_value=[])
    mass.music.playlists.get_library_item = AsyncMock(side_effect=MediaNotFoundError("gone"))
    mass.player_queues.queue_data_or_none = MagicMock(return_value=None)
    mass.player_queues.all = MagicMock(return_value=())
    mass.music.favorites.disliked_track_keys = AsyncMock(return_value=(set(), set()))
    mass.music.favorites.disliked_genre_ids = AsyncMock(return_value=set())
    mass.music.favorites.in_disliked_genres = AsyncMock(return_value=set())
    manifest = MagicMock()
    manifest.domain = "rotation_playlist"
    config = MagicMock()
    config.instance_id = INSTANCE
    config.get_value.return_value = "GLOBAL"
    return RotationPlaylistProvider(mass, manifest, config, set())


async def _loaded(tmp_path: Path, mass: MagicMock | None = None) -> RotationPlaylistProvider:
    provider = _provider(tmp_path, mass)
    await provider.handle_async_init()
    return provider


def _mock(provider: RotationPlaylistProvider) -> MagicMock:
    return cast("MagicMock", provider.mass)


def _steps() -> list[dict[str, Any]]:
    return steps_to_dict(_die_drei_steps())


def _over_websocket(result: Any) -> Any:
    """Serialize a result like the WebSocket API does and parse it back."""
    return json.loads(SuccessResultMessage("1", result).to_json())["result"]


def _queue_with(rotation_id: str) -> MagicMock:
    playlist = Playlist(
        item_id="12",
        provider="library",
        name="Die drei ???",
        provider_mappings={
            ProviderMapping(
                item_id=rotation_id, provider_domain="rotation_playlist", provider_instance=INSTANCE
            )
        },
    )
    queue_data = MagicMock()
    queue_data.source_items = [playlist]
    return queue_data


def _played(uri: str, userid: str | None, queue_id: str = QUEUE) -> MediaItemPlaybackProgressReport:
    return MediaItemPlaybackProgressReport(
        uri=uri,
        media_type=MediaType.TRACK,
        name="track",
        duration=600,
        seconds_played=3,
        fully_played=False,
        is_playing=True,
        userid=userid,
        player_id=queue_id,
    )


def _uris(tracks: list[Any]) -> list[str]:
    return [str(track.uri) for track in tracks]


def _all_played(state: RoundState) -> set[str]:
    """Return the uris of all tracks that played in a round, for a unit or for none."""
    return state.played | {
        uri for plays in state.unit_plays.values() for uris in plays.values() for uri in uris
    }


async def test_create_persists_and_reloads(tmp_path: Path) -> None:
    """A created Rotation is written to disk at once and loaded again by a new instance."""
    provider = await _loaded(tmp_path)
    playlist = await provider.create("Die drei ???", _steps())
    assert playlist.is_dynamic is True
    assert playlist.is_editable is True
    assert playlist.owner == "Rotation"
    mapping = next(iter(playlist.provider_mappings))
    assert mapping.is_unique is True
    assert mapping.in_library is True
    rotation_id = playlist.item_id
    assert playlist.uri == f"rotation_playlist://playlist/{rotation_id}"
    stored = json.loads((tmp_path / "rotation_playlists" / "rotations.json").read_text())
    assert stored["version"] == 1
    assert stored["rotations"][rotation_id]["name"] == "Die drei ???"

    reloaded = await _loaded(tmp_path)
    rotation = _over_websocket(await reloaded.get(rotation_id))
    assert rotation["id"] == rotation_id
    assert rotation["name"] == "Die drei ???"
    assert rotation["steps"] == _steps()
    assert rotation["round_status"]["round"] == 1
    assert rotation["round_status"]["units_total"] == 2
    assert [item["id"] for item in await reloaded.list_rotations()] == [rotation_id]


async def test_create_rejects_invalid_steps(tmp_path: Path) -> None:
    """Steps with shape or semantic errors are rejected with the failing step number."""
    provider = await _loaded(tmp_path)
    bad = _steps()
    bad[5] = {"type": "sort", "keys": [{"field": "name_of_the_rose"}]}
    with pytest.raises(StepsError) as err:
        await provider.create("Broken", bad)
    assert err.value.errors[0].step == 6
    with pytest.raises(StepsError) as err:
        await provider.create("Broken", [{"type": "expand", "to": "track"}])
    assert err.value.errors[0].step == 1
    assert not (tmp_path / "rotation_playlists" / "rotations.json").exists()


async def test_update_and_delete(tmp_path: Path) -> None:
    """A Rotation can be renamed, get new steps and be deleted with its progress."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    await provider.update(rotation_id, name="Die drei ??? Hörspiele")
    new_steps = steps_to_dict([SourceStep(ItemKind.TRACK)])
    await provider.update(rotation_id, steps=new_steps)
    rotation = _over_websocket(await provider.get(rotation_id))
    assert rotation["name"] == "Die drei ??? Hörspiele"
    assert rotation["steps"] == new_steps
    with pytest.raises(StepsError):
        await provider.update(rotation_id, steps=[])
    await provider.delete(rotation_id)
    assert await provider.list_rotations() == []
    stored = json.loads((tmp_path / "rotation_playlists" / "rotations.json").read_text())
    assert stored["rotations"] == {}


async def test_get_playlist_tracks_page_one_is_empty(tmp_path: Path) -> None:
    """Dynamic playlists hand out one batch on page 0 only."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    assert await provider.get_playlist_tracks(rotation_id, page=1) == []
    assert len(await provider.get_playlist_tracks(rotation_id)) == 6


async def test_get_playlist_tracks_does_not_advance(tmp_path: Path) -> None:
    """Browsing the tracks never counts as played and writes nothing."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    first = await provider.get_playlist_tracks(rotation_id)
    second = await provider.get_playlist_tracks(rotation_id)
    assert _uris(first) == _uris(second)
    assert not (tmp_path / "rotation_playlists" / "progress.json").exists()


async def test_played_report_advances_for_user(tmp_path: Path) -> None:
    """A started track moves the user's round on; the household round stays."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    household_before = await provider.get_playlist_tracks(rotation_id)
    set_current_user(User(user_id="u1", username="u1", role=UserRole.USER))
    try:
        batch = await provider.get_playlist_tracks(rotation_id)
        _mock(provider).player_queues.queue_data_or_none = MagicMock(
            return_value=_queue_with(rotation_id)
        )
        await provider.on_media_item_played(_played(str(batch[0].uri), "u1"))
        _mock(provider).player_queues.queue_data_or_none.assert_called_with(QUEUE)
        _mock(provider).call_later.assert_called_once()
        assert _mock(provider).call_later.call_args.args[0] == 5
        assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch[1:])
        await provider.on_media_item_played(_played(str(batch[1].uri), "u1"))
        _mock(provider).call_later.assert_called_once()
    finally:
        set_current_user(None)
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(household_before)
    await provider.unload()
    saved = json.loads((tmp_path / "rotation_playlists" / "progress.json").read_text())
    assert saved["version"] == 1
    assert sorted(saved["entries"][rotation_id]["u1"]["played"]) == sorted(_uris(batch[:2]))


async def test_played_report_without_user_uses_household(tmp_path: Path) -> None:
    """Playback without a user counts for the household progress."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    _mock(provider).player_queues.queue_data_or_none = MagicMock(
        return_value=_queue_with(rotation_id)
    )
    await provider.on_media_item_played(_played(str(batch[0].uri), None))
    assert provider._progress.get(rotation_id, HOUSEHOLD).played == {str(batch[0].uri)}
    after = await provider.get_playlist_tracks(rotation_id)
    assert _uris(after) == _uris(batch[1:])


async def test_played_report_for_other_queue_is_ignored(tmp_path: Path) -> None:
    """A report from a queue without a Rotation source writes nothing."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    await provider.on_media_item_played(_played(str(batch[0].uri), "u1", queue_id="unknown"))
    other = MagicMock()
    other.source_items = [
        Playlist(item_id="7", provider="library", name="Other", provider_mappings=set())
    ]
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=other)
    await provider.on_media_item_played(_played(str(batch[0].uri), "u1"))
    _mock(provider).call_later.assert_not_called()
    await provider.unload()
    assert not (tmp_path / "rotation_playlists" / "progress.json").exists()


async def test_broken_files_load_empty(tmp_path: Path) -> None:
    """Truncated or wrongly typed files load as empty, without raising."""
    folder = tmp_path / "rotation_playlists"
    folder.mkdir()
    (folder / "rotations.json").write_text('{"version": 1, "rotations": {"a": ')
    (folder / "progress.json").write_text('["not", "a", "dict"]')
    provider = await _loaded(tmp_path)
    assert await provider.list_rotations() == []
    (folder / "rotations.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rotations": {
                    "bad": {"name": "Bad", "steps": [{"type": "nonsense"}]},
                    "worse": "text",
                    "good": {"name": "Good", "steps": _steps()},
                },
            }
        )
    )
    provider = await _loaded(tmp_path)
    assert [item["id"] for item in await provider.list_rotations()] == ["good"]


async def test_preview_requires_limit_in_range(tmp_path: Path) -> None:
    """The preview needs a limit from 1 to the maximum."""
    provider = await _loaded(tmp_path)
    for limit in (0, PREVIEW_MAX_LIMIT + 1):
        with pytest.raises(InvalidDataError):
            await provider.preview(_steps(), limit)


async def test_preview_shape_and_counts(tmp_path: Path) -> None:
    """The preview returns groups cut to the limit and the exact count per step."""
    provider = await _loaded(tmp_path)
    result = await provider.preview(_steps(), 3, include_removed=True, seed=7)
    assert [stat["count"] for stat in result["step_counts"]] == [6, 2, 2, 8, 6, 6]
    assert [stat["step"] for stat in result["step_counts"]] == [1, 2, 3, 4, 5, 6]
    assert all(stat["exact"] for stat in result["step_counts"])
    assert result["total_tracks"] == 6
    assert result["total_exact"] is True
    assert result["duration_seconds"] == 6 * 600
    kept = [
        entry["track"]["name"]
        for group in result["groups"]
        for entry in group["tracks"]
        if not entry["removed"]
    ]
    assert len(kept) == 3
    first = result["groups"][0]
    assert first["item"]["name"] in (GLOCKEN, "Der Super-Papagei")
    if first["item"]["name"] == GLOCKEN:
        assert [(entry["track"]["name"], entry["removed"]) for entry in first["tracks"]] == [
            (GLOCKEN_TRACKS[0], True),
            (GLOCKEN_TRACKS[1], True),
            (GLOCKEN_TRACKS[2], False),
            (GLOCKEN_TRACKS[3], False),
        ]
    else:
        assert [entry["track"]["name"] for entry in first["tracks"]] == PAPAGEI_TRACKS[:3]

    plain = await provider.preview(_steps(), 200, seed=7)
    names = [entry["track"]["name"] for group in plain["groups"] for entry in group["tracks"]]
    assert len(names) == 6
    assert not any(entry["removed"] for group in plain["groups"] for entry in group["tracks"])

    tracks = await provider.preview(steps_to_dict([SourceStep(ItemKind.TRACK)]), 2)
    assert [len(group["tracks"]) for group in tracks["groups"]] == [1, 1]
    assert tracks["groups"][0]["item"] == tracks["groups"][0]["tracks"][0]["track"]
    assert tracks["total_tracks"] == 24


async def test_preview_rejects_invalid_steps(tmp_path: Path) -> None:
    """The preview reports invalid steps as a clean error."""
    provider = await _loaded(tmp_path)
    with pytest.raises(StepsError):
        await provider.preview([{"type": "source", "media_type": "song"}], 10)


async def test_validate_explain_describe_and_recipes(tmp_path: Path) -> None:
    """The read-only commands reach the WebSocket API as plain data."""
    provider = await _loaded(tmp_path)
    assert _over_websocket(await provider.validate(_steps())) == []
    errors = _over_websocket(await provider.validate([{"type": "expand", "to": "track"}]))
    assert errors[0]["step"] == 1
    assert set(errors[0]) == {"step", "field", "message"}
    described = _over_websocket(await provider.describe_steps("album"))
    assert list(described["media_types"]) == ["album"]
    assert "schema" in described
    year = next(f for f in described["media_types"]["album"]["fields"] if f["name"] == "year")
    assert set(year) == {"name", "type", "label", "operators", "sortable"}
    explained = await provider.explain(_steps())
    assert explained[:2] == ["Take albums from my library", 'Keep where artist is "Die drei ???"']
    recipes = _over_websocket(await provider.recipes())
    assert [set(recipe) for recipe in recipes] == [{"id", "name", "goal", "steps"}] * 5


async def test_reset_round_starts_over(tmp_path: Path) -> None:
    """Resetting the round forgets the played tracks of the current user."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    provider._progress.mark_started(rotation_id, HOUSEHOLD, "library://track/13")
    await provider.reset_round(rotation_id)
    assert provider._progress.get(rotation_id, HOUSEHOLD).played == set()
    assert (tmp_path / "rotation_playlists" / "progress.json").exists()


async def test_recipes_are_valid_except_placeholders() -> None:
    """Every recipe parses and runs; only the artist placeholder needs a value."""
    assert len(RECIPES) == 5
    for recipe in RECIPES:
        steps = parse_steps(steps_to_dict(recipe.steps))
        errors = validate_steps(steps)
        if recipe.id == "albums_random_complete":
            assert len(errors) == 1
            assert "pick an artist" in errors[0].message
            continue
        assert errors == [], recipe.id
        ctx = RunContext(_mass(_library()), seed=1)
        async with aclosing(iter_units(steps, ctx)) as units:
            async for _unit in units:
                pass
        assert len(ctx.stats) == len(steps), recipe.id


def test_explain_die_drei() -> None:
    """Every step of the Die drei ??? example reads as one plain sentence."""
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("artist", "is", "library://artist/1")]),
        SortStep([SortKey("random")]),
        ExpandStep(ItemKind.TRACK),
        FilterStep(
            "remove",
            "any",
            [
                Condition("title", "contains", "Inhaltsangabe"),
                Condition("title", "contains", "Titelmusik"),
            ],
        ),
        SortStep([SortKey("track_number")]),
    ]
    assert explain_steps(steps, {"library://artist/1": "Die drei ???"}) == [
        "Take albums from my library",
        'Keep where artist is "Die drei ???"',
        "Order randomly",
        "Play each album's tracks",
        'Inside each album: leave out where title contains "Inhaltsangabe" or title contains '
        '"Titelmusik"',
        "Inside each album: order by track number",
    ]


def test_explain_says_which_group_a_step_works_in() -> None:
    """Filters, orders and limits after an expand read as working inside each group."""
    parsed = parse_steps(
        [
            {"type": "source", "media_type": "artist"},
            {"type": "expand", "to": "album"},
            {"type": "limit", "count": 2},
            {"type": "expand", "to": "track"},
            {"type": "limit", "count": 2},
            {"type": "sort", "keys": [{"field": "random"}]},
            {"type": "flatten"},
            {"type": "limit", "count": 1},
        ]
    )
    assert explain_steps(parsed) == [
        "Take artists from my library",
        "Play each artist's albums",
        "Inside each artist: only the first 2 albums",
        "Play each album's tracks",
        "Inside each album: only the first 2 tracks",
        "Inside each album: order randomly",
        "Mix all together",
        "Only the first track",
    ]


def test_explain_other_steps() -> None:
    """Limits, sort directions, flatten and placeholders read naturally too."""
    recipes = {recipe.id: recipe for recipe in RECIPES}
    assert explain_steps(recipes["albums_random_complete"].steps)[1] == ("Keep where artist is ...")
    parsed = parse_steps(
        [
            {"type": "source", "media_type": "artist"},
            {"type": "expand", "to": "album", "scope": "providers"},
            {"type": "flatten"},
            {"type": "sort", "keys": [{"field": "date_added", "descending": True}]},
            {"type": "limit", "count": 1},
            {"type": "limit", "minutes": 120},
        ]
    )
    assert explain_steps(parsed) == [
        "Take artists from my library",
        "Play each artist's albums, also those on the providers",
        "Mix all together",
        "Order by date added, newest first",
        "Only the first album",
        "Only the first 2 hours",
    ]


def test_explain_new_fields() -> None:
    """Fixed values, metadata and loaded fields read naturally."""
    parsed = parse_steps(
        [
            {"type": "source", "media_type": "album"},
            {
                "type": "filter",
                "mode": "keep",
                "match": "all",
                "conditions": [
                    {"field": "audio_quality", "op": "one_of", "value": ["lossless", "hi_res"]},
                    {"field": "date_favorited", "op": "in_last_days", "value": 30},
                    {"field": "track_count", "op": "more_than", "value": 8},
                    {"field": "language", "op": "is_not", "value": "eng"},
                ],
            },
            {"type": "sort", "keys": [{"field": "play_count", "descending": True}]},
        ]
    )
    assert explain_steps(parsed)[1:] == [
        "Keep where audio quality is one of lossless or hi-res and date favorited is in the "
        'last 30 days and number of tracks is more than 8 and language is not "eng"',
        "Order by play count, highest first",
    ]


def test_explain_quotes_texts_and_names_but_not_fixed_words() -> None:
    """A text and a looked up name read in quotes, an album type does not."""
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep(
            "keep",
            "all",
            [
                Condition("genre", "one_of", ["library://genre/1", "library://genre/2"]),
                Condition("album_type", "is_not", "compilation"),
                Condition("name", "starts_with", "Live"),
            ],
        ),
    ]
    names = {"library://genre/1": "House", "library://genre/2": "French Touch"}
    assert explain_steps(steps, names)[1] == (
        'Keep where genre is one of "House" or "French Touch" and album type is not '
        'compilation and name starts with "Live"'
    )


async def test_play_count_sort_after_an_expand_is_valid(tmp_path: Path) -> None:
    """A play count sort after an expand orders the loaded values, so it is no longer refused."""
    provider = await _loaded(tmp_path)
    steps = steps_to_dict(
        [
            SourceStep(ItemKind.ALBUM),
            ExpandStep(ItemKind.TRACK),
            SortStep([SortKey("play_count")]),
        ]
    )
    assert await provider.validate(steps) == []


async def test_broken_entry_survives_saves(tmp_path: Path) -> None:
    """An entry this version cannot read is written back unchanged until it is deleted."""
    folder = tmp_path / "rotation_playlists"
    folder.mkdir()
    broken = {"name": "Newer", "steps": [{"type": "shuffle_by_mood"}]}
    (folder / "rotations.json").write_text(
        json.dumps({"version": 1, "rotations": {"newer": broken}})
    )
    provider = await _loaded(tmp_path)
    await provider.create("Die drei ???", _steps())
    stored = json.loads((folder / "rotations.json").read_text())
    assert stored["rotations"]["newer"] == broken
    assert len(stored["rotations"]) == 2
    with pytest.raises(MediaNotFoundError):
        await provider.get_playlist("newer")
    await provider.delete("newer")
    stored = json.loads((folder / "rotations.json").read_text())
    assert "newer" not in stored["rotations"]
    assert len(stored["rotations"]) == 1


async def test_unreadable_file_is_kept_aside(tmp_path: Path) -> None:
    """A file that cannot be read is copied aside before the first save replaces it."""
    folder = tmp_path / "rotation_playlists"
    folder.mkdir()
    (folder / "rotations.json").write_text('{"version": 1, "rotations": {"a": ')
    provider = await _loaded(tmp_path)
    await provider.create("Die drei ???", _steps())
    assert (folder / "rotations.json.broken").read_text() == ('{"version": 1, "rotations": {"a": ')
    assert len(json.loads((folder / "rotations.json").read_text())["rotations"]) == 1


async def test_preview_counts_without_provider_calls(tmp_path: Path) -> None:
    """After the limit is reached, provider listings are not fetched just to count."""
    provider = await _loaded(tmp_path)
    albums = _mock(provider).music.artists.albums
    steps = steps_to_dict(
        [
            SourceStep(ItemKind.ARTIST),
            ExpandStep(ItemKind.ALBUM, scope="providers"),
            ExpandStep(ItemKind.TRACK),
        ]
    )
    result = await provider.preview(steps, 1)
    provider_calls = [call for call in albums.await_args_list if call.args[1] != "library"]
    assert len(provider_calls) == 1
    assert [stat["exact"] for stat in result["step_counts"]] == [True, False, False]
    assert result["total_exact"] is False
    assert result["total_tracks"] == 24


async def test_browse_at_end_of_round_keeps_progress(tmp_path: Path) -> None:
    """Browsing a used up round changes nothing; the first play starts the next round."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    await provider.get(rotation_id)
    assert provider._progress._entries == {}
    for track in await provider.get_playlist_tracks(rotation_id):
        provider._progress.mark_started(rotation_id, HOUSEHOLD, str(track.uri))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    before = (state.round, state.seed, set(state.played))
    batch = await provider.get_playlist_tracks(rotation_id)
    assert len(batch) == 6
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert (state.round, state.seed, state.played) == before
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch)

    _mock(provider).player_queues.queue_data_or_none = MagicMock(
        return_value=_queue_with(rotation_id)
    )
    await provider.on_media_item_played(_played(str(batch[0].uri), None))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert state.round == 2
    assert state.played == {str(batch[0].uri)}
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch[1:])


async def _end_of_round(provider: RotationPlaylistProvider) -> tuple[str, list[Any]]:
    """Create a Rotation whose household round 1 is fully played; return it and the batch."""
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    for track in await provider.get_playlist_tracks(rotation_id):
        provider._progress.mark_started(rotation_id, HOUSEHOLD, str(track.uri))
    return rotation_id, await provider.get_playlist_tracks(rotation_id)


def _playing_queue(rotation_id: str, current_uri: str) -> tuple[MagicMock, MagicMock]:
    queue_data = _queue_with(rotation_id)
    queue_data.userid = None
    queue = MagicMock()
    queue.queue_id = QUEUE
    queue.current_item.media_item.uri = current_uri
    return queue, queue_data


def _report(uri: str, is_playing: bool) -> MediaItemPlaybackProgressReport:
    return dataclasses.replace(_played(uri, None), is_playing=is_playing)


async def test_old_round_reports_do_not_start_the_next_round(tmp_path: Path) -> None:
    """Reports of the last track of a round leave the next round untouched."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    round_one = await provider.get_playlist_tracks(rotation_id)
    for track in round_one[:-1]:
        provider._progress.mark_started(rotation_id, HOUSEHOLD, str(track.uri))
    last = str(round_one[-1].uri)
    provider._progress.mark_started(rotation_id, HOUSEHOLD, last)
    queue, queue_data = _playing_queue(rotation_id, last)
    mock = _mock(provider)
    mock.player_queues.all = MagicMock(return_value=(queue,))
    mock.player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    # the refill hands out round 2 while the last track of round 1 still plays
    round_two = await provider.get_playlist_tracks(rotation_id)
    assert len(round_two) == 6
    await provider.on_media_item_played(_report(last, is_playing=True))
    await provider.on_media_item_played(_report(last, is_playing=False))
    assert provider._progress.get(rotation_id, HOUSEHOLD).round == 1
    first = str(round_two[0].uri)
    queue.current_item.media_item.uri = first
    await provider.on_media_item_played(_report(first, is_playing=True))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert state.round == 2
    assert state.played == {first}
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(round_two[1:])


async def test_batch_leaves_out_the_track_that_plays_now(tmp_path: Path) -> None:
    """A batch of the same round leaves out the current track, also before it is reported."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    provider._progress.mark_started(rotation_id, HOUSEHOLD, str(batch[0].uri))
    queue, queue_data = _playing_queue(rotation_id, str(batch[1].uri))
    mock = _mock(provider)
    mock.player_queues.all = MagicMock(return_value=(queue,))
    mock.player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch[2:])


async def test_next_round_batch_keeps_the_track_that_plays_now(tmp_path: Path) -> None:
    """A batch that starts the next round keeps the track that ends the old round."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    round_one = await provider.get_playlist_tracks(rotation_id)
    for track in round_one:
        provider._progress.mark_started(rotation_id, HOUSEHOLD, str(track.uri))
    queue, queue_data = _playing_queue(rotation_id, str(round_one[-1].uri))
    mock = _mock(provider)
    mock.player_queues.all = MagicMock(return_value=(queue,))
    mock.player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    round_two = await provider.get_playlist_tracks(rotation_id)
    assert sorted(_uris(round_two)) == sorted(_uris(round_one))


async def test_reset_round_clears_the_next_round_marker(tmp_path: Path) -> None:
    """After a reset, a play does not start the round that was pending before."""
    provider = await _loaded(tmp_path)
    rotation_id, round_two = await _end_of_round(provider)
    await provider.reset_round(rotation_id)
    reset_seed = provider._progress.get(rotation_id, HOUSEHOLD).seed
    _mock(provider).player_queues.queue_data_or_none = MagicMock(
        return_value=_queue_with(rotation_id)
    )
    await provider.on_media_item_played(_report(str(round_two[0].uri), is_playing=True))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert (state.round, state.seed) == (1, reset_seed)
    assert state.played == {str(round_two[0].uri)}


async def test_refill_reads_listings_from_the_cache(tmp_path: Path) -> None:
    """A refill asks for a fresh batch, but the album listings still come from the cache."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    mock = _mock(provider)
    cache = CacheController.__new__(CacheController)
    mock.cache.handle_refresh = cache.handle_refresh
    album_tracks = mock.music.albums.tracks.side_effect
    bypassed: list[bool] = []

    async def tracks(*args: Any, **kwargs: Any) -> Any:
        bypassed.append(BYPASS_CACHE.get())
        return await album_tracks(*args, **kwargs)

    mock.music.albums.tracks.side_effect = tracks
    # the playlists controller asks for a dynamic playlist with the cache bypassed
    async with cache.handle_refresh(True):
        assert await provider.get_playlist_tracks(rotation_id)
    assert bypassed
    assert not any(bypassed)


def _dislike(provider: RotationPlaylistProvider, tracks: list[Any]) -> None:
    """Let the user dislike tracks, as the favorites store reports them."""
    keys: DislikedTrackKeys = ({(MediaType.TRACK, int(track.item_id)) for track in tracks}, set())
    _mock(provider).music.favorites.disliked_track_keys = AsyncMock(return_value=keys)


async def test_disliked_tracks_do_not_block_the_round(tmp_path: Path) -> None:
    """When only disliked tracks are left, the next batch starts the next round."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    set_current_user(User(user_id="u1", username="u1", role=UserRole.USER))
    try:
        round_one = await provider.get_playlist_tracks(rotation_id)
        for track in round_one[:-2]:
            provider._progress.mark_started(rotation_id, "u1", str(track.uri))
        disliked = round_one[-2:]
        _dislike(provider, disliked)
        batch = await provider.get_playlist_tracks(rotation_id)
    finally:
        set_current_user(None)
    _mock(provider).music.favorites.disliked_track_keys.assert_awaited_with("u1")
    # round 2: every track again, without the disliked ones
    assert sorted(_uris(batch)) == sorted(_uris(round_one[:-2]))
    assert provider._pending_rounds


async def test_tracks_in_a_disliked_genre_do_not_block_the_round(tmp_path: Path) -> None:
    """Tracks in a genre the user disliked are not part of the round, like in the queue."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    set_current_user(User(user_id="u1", username="u1", role=UserRole.USER))
    try:
        round_one = await provider.get_playlist_tracks(rotation_id)
        for track in round_one[:-2]:
            provider._progress.mark_started(rotation_id, "u1", str(track.uri))
        in_genre = {(track.provider, track.item_id) for track in round_one[-2:]}
        favorites = _mock(provider).music.favorites
        favorites.disliked_genre_ids = AsyncMock(return_value={7})
        favorites.in_disliked_genres = AsyncMock(
            side_effect=lambda _user, tracks: {
                (track.provider, track.item_id)
                for track in tracks
                if (track.provider, track.item_id) in in_genre
            }
        )
        batch = await provider.get_playlist_tracks(rotation_id)
    finally:
        set_current_user(None)
    favorites.in_disliked_genres.assert_awaited()
    # round 2: every track again, without the ones in the disliked genre
    assert sorted(_uris(batch)) == sorted(_uris(round_one[:-2]))
    assert provider._pending_rounds


async def test_disliked_and_unavailable_tracks_do_not_fill_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch holds playable tracks even when more disliked tracks than fit come first."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    set_current_user(User(user_id="u1", username="u1", role=UserRole.USER))
    try:
        order = await provider.get_playlist_tracks(rotation_id)
        monkeypatch.setattr("music_assistant.providers.rotation_playlist.BATCH_MAX_TRACKS", 2)
        _dislike(provider, order[:2])
        for mapping in order[2].provider_mappings:
            mapping.available = False
        batch = await provider.get_playlist_tracks(rotation_id)
    finally:
        set_current_user(None)
    assert _uris(batch) == _uris(order[3:5])


def _queue_event(uri: str, item_id: str, state: PlaybackState) -> MassEvent:
    track = Track(
        item_id=uri.rsplit("/", 1)[-1], provider="library", name="track", provider_mappings=set()
    )
    queue = PlayerQueue(
        queue_id=QUEUE,
        active=True,
        display_name="Kitchen",
        available=True,
        items=6,
        state=state,
        current_item=QueueItem(
            queue_id=QUEUE, queue_item_id=item_id, name="track", duration=600, media_item=track
        ),
    )
    return MassEvent(EventType.QUEUE_UPDATED, object_id=QUEUE, data=queue)


async def test_track_counts_as_played_when_it_starts(tmp_path: Path) -> None:
    """A track counts as soon as its queue item starts playing, without a playback report."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    queue_data = _queue_with(rotation_id)
    queue_data.userid = None
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    first, second = str(batch[0].uri), str(batch[1].uri)
    await provider._on_queue_updated(_queue_event(first, "a", PlaybackState.PAUSED))
    assert provider._progress.get(rotation_id, HOUSEHOLD).played == set()
    await provider._on_queue_updated(_queue_event(first, "a", PlaybackState.PLAYING))
    await provider._on_queue_updated(_queue_event(second, "b", PlaybackState.PLAYING))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert state.played == set()
    assert _all_played(state) == {first, second}
    _mock(provider).call_later.assert_called_once()


async def _start_all(
    provider: RotationPlaylistProvider, rotation_id: str, tracks: list[Any], first_id: int = 0
) -> None:
    """Let a household queue playing the Rotation start the tracks one after the other."""
    queue_data = _queue_with(rotation_id)
    queue_data.userid = None
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    for number, track in enumerate(tracks, start=first_id):
        await provider._on_queue_updated(
            _queue_event(str(track.uri), f"item{number}", PlaybackState.PLAYING)
        )


async def test_last_started_track_of_an_album_marks_it_played(tmp_path: Path) -> None:
    """An album counts as played once its last kept track starts, and is not listed again."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    album = batch[0].album
    assert album is not None
    done = [track for track in batch if track.album and track.album.item_id == album.item_id]
    await _start_all(provider, rotation_id, done[:-1])
    assert provider._progress.get(rotation_id, HOUSEHOLD).played_units == set()
    await _start_all(provider, rotation_id, done[-1:], first_id=len(done))
    assert provider._progress.get(rotation_id, HOUSEHOLD).played_units == {
        f"library://album/{album.item_id}"
    }
    await provider.unload()
    saved = json.loads((tmp_path / "rotation_playlists" / "progress.json").read_text())
    assert saved["entries"][rotation_id][HOUSEHOLD]["played_units"] == [
        f"library://album/{album.item_id}"
    ]
    mock = _mock(provider)
    mock.music.albums.tracks.reset_mock()
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch[len(done) :])
    assert album.item_id not in [call.args[0] for call in mock.music.albums.tracks.await_args_list]


async def test_browse_never_changes_the_stored_progress(tmp_path: Path) -> None:
    """Browsing a Rotation with a fully played album leaves the progress file as it is."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    album = batch[0].album
    assert album is not None
    for track in batch:
        if track.album and track.album.item_id == album.item_id:
            provider._progress.mark_started(rotation_id, HOUSEHOLD, str(track.uri))
    await provider._save_progress()
    path = tmp_path / "rotation_playlists" / "progress.json"
    before = path.read_text()
    await provider.get_playlist_tracks(rotation_id)
    await provider.get_playlist_tracks(rotation_id)
    assert provider._progress.get(rotation_id, HOUSEHOLD).played_units == set()
    _mock(provider).call_later.assert_not_called()
    await provider.unload()
    assert path.read_text() == before


async def test_steps_edit_shows_new_tracks_of_a_played_album(tmp_path: Path) -> None:
    """New steps that keep more tracks bring them back for an album that was fully played."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    glocken = [track for track in batch if track.album and track.album.name == GLOCKEN]
    await _start_all(provider, rotation_id, glocken)
    provider._progress.mark_started(rotation_id, "u1", str(glocken[0].uri))
    provider._progress._stored(rotation_id, "u1").played_units.add("library://album/1")
    assert provider._progress.get(rotation_id, HOUSEHOLD).played_units == {"library://album/1"}

    await provider.update(rotation_id, name="Die drei ??? Hörspiele")
    assert provider._progress.get(rotation_id, HOUSEHOLD).played_units == {"library://album/1"}
    without_filter = [step for index, step in enumerate(_steps()) if index != 4]
    await provider.update(rotation_id, steps=without_filter)

    for key in (HOUSEHOLD, "u1"):
        state = provider._progress.get(rotation_id, key)
        assert state.played_units == set()
        assert str(glocken[0].uri) in _all_played(state)
    names = [track.name for track in await provider.get_playlist_tracks(rotation_id)]
    assert names[:2] == GLOCKEN_TRACKS[:2] or names[-2:] == GLOCKEN_TRACKS[:2]
    assert not set(names) & {track.name for track in glocken}


def _queue_copy(track: Any, item_id: str = "99") -> Track:
    """Return another library copy of a track, known by the same provider mapping."""
    return Track(
        item_id=item_id,
        provider="library",
        name=track.name,
        provider_mappings=set(track.provider_mappings),
    )


async def test_started_queue_copy_counts_for_the_handed_out_track(tmp_path: Path) -> None:
    """A track counts when the queue plays another copy of it with the same mapping."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    queue_data = _queue_with(rotation_id)
    queue_data.userid = None
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    event = _queue_event("library://track/99", "a", PlaybackState.PLAYING)
    event.data.current_item.media_item = _queue_copy(batch[0])
    await provider._on_queue_updated(event)
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch[1:])


async def test_reported_queue_copy_counts_for_the_handed_out_track(tmp_path: Path) -> None:
    """A report of a queue copy with the same mapping counts for the handed out track."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    queue_data = _queue_with(rotation_id)
    queue_data.userid = None
    queue_data.items = [
        QueueItem(
            queue_id=QUEUE,
            queue_item_id="a",
            name="track",
            duration=600,
            media_item=_queue_copy(batch[0]),
        )
    ]
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    await provider.on_media_item_played(_played("library://track/99", None))
    # the report may also name the provider track itself
    mapping = next(iter(batch[1].provider_mappings))
    await provider.on_media_item_played(
        _played(f"{mapping.provider_instance}://track/{mapping.item_id}", None)
    )
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(batch[2:])


async def test_next_round_does_not_start_with_the_track_that_plays_now(tmp_path: Path) -> None:
    """A track that ends a round is not queued right behind itself as the next round starts."""
    provider = await _loaded(tmp_path)
    steps: list[Step] = [
        SourceStep(ItemKind.TRACK),
        FilterStep("keep", "all", [Condition("artist", "is", "library://artist/1")]),
        SortStep([SortKey("random")]),
    ]
    rotation_id = (await provider.create("Die drei ???", steps_to_dict(steps))).item_id
    for seed in range(500):
        provider._progress._stored(rotation_id, HOUSEHOLD).seed = seed
        round_one = await provider.get_playlist_tracks(rotation_id)
        state = RoundState(round=2, seed=derived_seed(seed, 2))
        round_two = await next_batch(steps, state, provider._ctx_factory)
        if round_two[0].uri == round_one[-1].uri:
            break
    else:
        pytest.fail("no seed found")
    for track in round_one:
        provider._progress.mark_started(rotation_id, HOUSEHOLD, str(track.uri))
    queue, queue_data = _playing_queue(rotation_id, str(round_one[-1].uri))
    mock = _mock(provider)
    mock.player_queues.all = MagicMock(return_value=(queue,))
    mock.player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    batch = await provider.get_playlist_tracks(rotation_id)
    assert _uris(batch) == _uris([*round_two[1:], round_two[0]])


async def test_report_from_a_queue_without_rotation_reads_no_queue_items(tmp_path: Path) -> None:
    """A report from a queue that does not play a Rotation stops before the queue items."""
    provider = await _loaded(tmp_path)
    await provider.create("Die drei ???", _steps())
    other = MagicMock()
    other.source_items = []
    type(other).items = property(lambda _self: pytest.fail("queue items were read"))
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=other)
    await provider.on_media_item_played(_played("library://track/11", None))
    _mock(provider).call_later.assert_not_called()


async def test_removed_player_forgets_its_started_item(tmp_path: Path) -> None:
    """The started item of a queue is forgotten when its player is removed."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    await _start_all(provider, rotation_id, batch[:1])
    assert provider._started_items == {QUEUE: "item0"}
    await provider._on_player_removed(MassEvent(EventType.PLAYER_REMOVED, object_id=QUEUE))
    assert provider._started_items == {}


async def test_unexpected_lookup_errors_are_not_hidden(tmp_path: Path) -> None:
    """Only a missing or malformed playlist id counts as not found; other errors surface."""
    provider = await _loaded(tmp_path)
    with pytest.raises(MediaNotFoundError):
        await provider.get_playlist("12")
    _mock(provider).music.playlists.get_library_item = AsyncMock(side_effect=RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        await provider.get_playlist("12")


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("SECRET database detail"),
        InvalidDataError("SECRET bad data"),
        MediaNotFoundError("SECRET item gone"),
        InsufficientPermissions("SECRET no permission"),
    ],
)
async def test_reconcile_goes_on_when_the_library_refuses_one(
    tmp_path: Path, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A Rotation the library cannot take is skipped, and the next one is still added."""
    provider = await _loaded(tmp_path)
    first = (await provider.create("Die drei ???", _steps())).item_id
    second = (await provider.create("Die drei ??? Hörspiele", _steps())).item_id
    add = AsyncMock(side_effect=[error, None])
    _mock(provider).music.playlists.add_item_to_library = add
    await provider._reconcile_library()
    assert [call.args[0].item_id for call in add.await_args_list] == [first, second]
    assert type(error).__name__ in caplog.text
    assert str(error) not in caplog.text
    add.side_effect = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        await provider._reconcile_library()


async def test_browse_and_list_look_up_the_library_once(tmp_path: Path) -> None:
    """Browsing and listing Rotations take one library query, and descriptions are kept."""
    provider = await _loaded(tmp_path)
    ids = [(await provider.create(f"Die drei ??? {number}", _steps())).item_id for number in (1, 2)]
    library_copy = Playlist(
        item_id="12",
        provider="library",
        name="Die drei ??? 1",
        provider_mappings={
            ProviderMapping(
                item_id=ids[0], provider_domain="rotation_playlist", provider_instance=INSTANCE
            )
        },
    )
    mock = _mock(provider)
    mock.music.playlists.get_library_items_by_prov_id = AsyncMock(return_value=[library_copy])
    mock.music.playlists.get_library_item_by_prov_id.reset_mock()
    mock.music.artists.get_library_item.reset_mock()

    assert [item.item_id for item in await provider.browse("")] == ids
    listed = await provider.list_rotations()
    assert [item["playlist"]["item_id"] for item in listed] == ["12", ids[1]]
    mock.music.playlists.get_library_item_by_prov_id.assert_not_awaited()
    assert mock.music.playlists.get_library_items_by_prov_id.await_count == 2
    mock.music.artists.get_library_item.assert_not_awaited()

    await provider.update(ids[0], steps=steps_to_dict([SourceStep(ItemKind.TRACK)]))
    descriptions = {
        item.item_id: item.metadata.description
        for item in await provider.browse("")
        if isinstance(item, Playlist)
    }
    assert descriptions[ids[0]] != descriptions[ids[1]]


async def test_unload_cancels_the_library_check(tmp_path: Path) -> None:
    """The background check of the library playlists stops when the provider unloads."""
    provider = await _loaded(tmp_path)
    mock = _mock(provider)
    await provider.loaded_in_mass()
    task_id = mock.create_task.call_args.kwargs["task_id"]
    await provider.unload()
    mock.cancel_task.assert_any_call(task_id)


async def test_preview_is_json_serializable_over_websocket(tmp_path: Path) -> None:
    """Nested media items of a preview reach the WebSocket API as plain data."""
    provider = await _loaded(tmp_path)
    for include_removed in (True, False):
        result = _over_websocket(
            await provider.preview(_steps(), 3, include_removed=include_removed, seed=7)
        )
        group = result["groups"][0]
        assert group["item"]["name"]
        assert group["item"]["provider_mappings"]
        assert group["tracks"][0]["track"]["name"]
        assert group["tracks"][0]["track"]["provider_mappings"]
        assert isinstance(group["tracks"][0]["removed"], bool)


async def test_get_and_list_are_json_serializable_over_websocket(tmp_path: Path) -> None:
    """The playlist in get and list_rotations reaches the WebSocket API as plain data."""
    provider = await _loaded(tmp_path)
    playlist = await provider.create("Die drei ???", _steps())
    rotation = _over_websocket(await provider.get(playlist.item_id))
    assert rotation["playlist"]["name"] == "Die drei ???"
    assert rotation["playlist"]["provider_mappings"]
    listed = _over_websocket(await provider.list_rotations())
    assert listed[0]["playlist"]["name"] == "Die drei ???"
    assert listed[0]["playlist"]["provider_mappings"]


async def test_api_commands_require_the_library_scopes(tmp_path: Path) -> None:
    """Commands that change Rotations or progress need library write, the others library read."""
    provider = await _loaded(tmp_path)
    await provider.loaded_in_mass()
    scopes = {
        call.args[0]: call.kwargs["required_scope"]
        for call in _mock(provider).register_api_command.call_args_list
    }
    writes = {"rotations/create", "rotations/update", "rotations/delete", "rotations/reset_round"}
    reads = {
        "rotations/get",
        "rotations/list",
        "rotations/validate",
        "rotations/preview",
        "rotations/explain",
        "rotations/describe_steps",
        "rotations/recipes",
    }
    assert set(scopes) == writes | reads
    for command in writes:
        assert scopes[command] == Scope.LIBRARY_WRITE
    for command in reads:
        assert scopes[command] == Scope.LIBRARY_READ


def _library_playlist(rotation_id: str, name: str = "Die drei ???") -> Playlist:
    """Return the library playlist of a Rotation, as the library stores it."""
    return Playlist(
        item_id="12",
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=rotation_id,
                provider_domain="rotation_playlist",
                provider_instance=INSTANCE,
                in_library=True,
            )
        },
    )


async def test_update_changes_the_library_playlist(tmp_path: Path) -> None:
    """A rename or new steps update the existing library playlist with its new description."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    mock = _mock(provider)
    library_item = _library_playlist(rotation_id)
    mock.music.playlists.get_library_item_by_prov_id = AsyncMock(return_value=library_item)
    mock.music.playlists.update_item_in_library = AsyncMock(
        side_effect=lambda _item_id, item, **_: item
    )
    new_steps = steps_to_dict([SourceStep(ItemKind.TRACK)])
    result = await provider.update(rotation_id, name="Hörspiele", steps=new_steps)
    call = mock.music.playlists.update_item_in_library.await_args
    assert call.args[0] == "12"
    assert call.kwargs["overwrite"] is True
    updated = call.args[1]
    assert updated is not library_item
    assert updated.name == "Hörspiele"
    assert updated.metadata.description == "Take tracks from my library."
    assert result.name == "Hörspiele"
    stored = json.loads((tmp_path / "rotation_playlists" / "rotations.json").read_text())
    assert stored["rotations"][rotation_id] == {"name": "Hörspiele", "steps": new_steps}


async def test_deleted_library_playlist_deletes_the_rotation(tmp_path: Path) -> None:
    """Removing the library playlist deletes its Rotation and the progress of all users."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    other_id = (await provider.create("Other", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    await _start_all(provider, rotation_id, batch[:1])
    await provider._on_media_item_deleted(
        MassEvent(EventType.MEDIA_ITEM_DELETED, object_id="12", data=_library_playlist(rotation_id))
    )
    assert [item["id"] for item in await provider.list_rotations()] == [other_id]
    rotations = json.loads((tmp_path / "rotation_playlists" / "rotations.json").read_text())
    assert list(rotations["rotations"]) == [other_id]
    progress = json.loads((tmp_path / "rotation_playlists" / "progress.json").read_text())
    assert rotation_id not in progress["entries"]
    await provider._on_media_item_deleted(
        MassEvent(EventType.MEDIA_ITEM_DELETED, object_id="7", data=batch[0])
    )
    assert [item["id"] for item in await provider.list_rotations()] == [other_id]


async def test_renamed_library_playlist_renames_the_rotation(tmp_path: Path) -> None:
    """A rename of the library playlist is saved as the name of its Rotation."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    renamed = _library_playlist(rotation_id, name="Hörspiele")
    await provider._on_media_item_updated(
        MassEvent(EventType.MEDIA_ITEM_UPDATED, object_id="12", data=renamed)
    )
    stored = json.loads((tmp_path / "rotation_playlists" / "rotations.json").read_text())
    assert stored["rotations"][rotation_id]["name"] == "Hörspiele"
    # the provider playlist itself is not the library playlist and does not rename it
    provider_copy = await provider.get_playlist(rotation_id)
    provider_copy.name = "Something else"
    await provider._on_media_item_updated(
        MassEvent(EventType.MEDIA_ITEM_UPDATED, object_id=rotation_id, data=provider_copy)
    )
    assert (await provider.get(rotation_id)).name == "Hörspiele"


async def test_removed_provider_removes_its_playlists_and_files(tmp_path: Path) -> None:
    """Removing the provider removes the library playlists and the stored files."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    await provider.reset_round(rotation_id)
    folder = tmp_path / "rotation_playlists"
    assert (folder / "rotations.json").exists()
    assert (folder / "progress.json").exists()
    mock = _mock(provider)
    mock.music.playlists.get_library_item_by_prov_id = AsyncMock(
        return_value=_library_playlist(rotation_id)
    )
    mock.music.remove_item_from_library = AsyncMock()
    await provider.loaded_in_mass()
    await provider.unload(is_removed=True)
    mock.music.remove_item_from_library.assert_awaited_once_with(MediaType.PLAYLIST, "12")
    assert not (folder / "rotations.json").exists()
    assert not (folder / "progress.json").exists()
    assert provider._unregister_handles == []


async def _shared_rotation(
    tmp_path: Path,
) -> tuple[RotationPlaylistProvider, str, list[Any], Any, MagicMock]:
    """Return a provider whose Rotation has one track on two albums, its batch and a queue."""
    library = _library()
    shared = library.tracks["1"][2]
    library.tracks["2"].append(shared)
    provider = await _loaded(tmp_path, _mass(library))
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    queue_data = _queue_with(rotation_id)
    queue_data.userid = None
    queue_data.items = [
        QueueItem(
            queue_id=QUEUE, queue_item_id=f"q{index}", name="track", duration=600, media_item=track
        )
        for index, track in enumerate(batch)
    ]
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    return provider, rotation_id, batch, shared, queue_data


def _play_at(queue_data: MagicMock, index: int) -> None:
    """Let the queue play its item at an index."""
    queue_data.queue.current_index = index
    queue_data.queue.current_item = queue_data.items[index]


async def test_reports_of_a_track_on_two_albums_count_for_each_album(tmp_path: Path) -> None:
    """Each queued copy of a shared track counts once, for its own album, however often reported."""
    provider, rotation_id, batch, shared, queue_data = await _shared_rotation(tmp_path)
    first, second = (index for index, track in enumerate(batch) if track is shared)
    first_unit, second_unit = provider._handed_units[(rotation_id, HOUSEHOLD)].units
    uri = str(shared.uri)
    _play_at(queue_data, first)
    for _ in range(3):
        await provider.on_media_item_played(_report(uri, is_playing=True))
    await provider._on_queue_updated(_queue_event(uri, f"q{first}", PlaybackState.PLAYING))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert list(state.unit_plays) == [first_unit]
    assert list(state.unit_plays[first_unit]) == [f"q{first}"]
    _play_at(queue_data, first + 1)
    await provider.on_media_item_played(_report(uri, is_playing=False))
    assert list(state.unit_plays) == [first_unit]
    _play_at(queue_data, second)
    await provider.on_media_item_played(_report(uri, is_playing=True))
    await provider.on_media_item_played(_report(uri, is_playing=True))
    assert list(state.unit_plays[second_unit]) == [f"q{second}"]
    assert len(state.unit_plays[first_unit]) == 1
    assert state.played == set()


async def test_shared_track_played_in_one_album_stays_in_the_other(tmp_path: Path) -> None:
    """A refill after the shared track played for the first album still holds the second copy."""
    provider, rotation_id, batch, shared, _queue_data = await _shared_rotation(tmp_path)
    first = batch.index(shared)
    await _start_all(provider, rotation_id, batch[: first + 1])
    refill = await provider.get_playlist_tracks(rotation_id)
    assert _uris(refill) == _uris(batch[first + 1 :])
    assert str(shared.uri) in _uris(refill)


async def test_album_with_a_shared_track_is_played_once_its_own_copy_played(
    tmp_path: Path,
) -> None:
    """An album counts as played only when the shared track played as part of it."""
    provider, rotation_id, batch, shared, _queue_data = await _shared_rotation(tmp_path)
    first_unit, second_unit = provider._handed_units[(rotation_id, HOUSEHOLD)].units
    size = len(provider._handed_units[(rotation_id, HOUSEHOLD)].units[first_unit].tracks)
    second_album = [track for track in batch[size:] if track is not shared]
    await _start_all(provider, rotation_id, second_album)
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert state.played_units == set()
    await _start_all(provider, rotation_id, batch[:size], first_id=len(batch))
    assert state.played_units == {first_unit}
    await _start_all(provider, rotation_id, [shared], first_id=2 * len(batch))
    assert state.played_units == {first_unit, second_unit}


async def test_progress_of_the_old_format_loads_and_resumes(tmp_path: Path) -> None:
    """A progress file with the flat played tracks of the old format loads and still resumes."""
    provider = await _loaded(tmp_path)
    rotation_id = (await provider.create("Die drei ???", _steps())).item_id
    batch = await provider.get_playlist_tracks(rotation_id)
    folder = tmp_path / "rotation_playlists"
    old = {"round": 1, "seed": provider._progress.get(rotation_id, HOUSEHOLD).seed}
    (folder / "progress.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    rotation_id: {
                        HOUSEHOLD: {**old, "played": _uris(batch[:2]), "played_units": []},
                        "u1": {"unit_plays": {"x": "broken"}, "played_units": "broken"},
                    }
                },
            }
        )
    )
    reloaded = await _loaded(tmp_path)
    assert _uris(await reloaded.get_playlist_tracks(rotation_id)) == _uris(batch[2:])
    assert reloaded._progress.get(rotation_id, "u1").unit_plays == {}


async def test_refill_counts_the_playing_copy_of_a_shared_track_for_its_album(
    tmp_path: Path,
) -> None:
    """The copy that plays now, not yet counted, is left out of a refill for its own album."""
    provider, rotation_id, batch, shared, queue_data = await _shared_rotation(tmp_path)
    first, second = (index for index, track in enumerate(batch) if track is shared)
    await _start_all(provider, rotation_id, batch[: second - 1])
    _mock(provider).player_queues.queue_data_or_none = MagicMock(return_value=queue_data)
    queue = MagicMock(queue_id=QUEUE, current_item=queue_data.items[second])
    _mock(provider).player_queues.all = MagicMock(return_value=(queue,))
    refill = await provider.get_playlist_tracks(rotation_id)
    assert first < second - 1
    assert _uris(refill) == _uris([batch[second - 1], *batch[second + 1 :]])


async def test_play_without_a_known_unit_is_not_counted_again_for_one(tmp_path: Path) -> None:
    """A play counted for no unit (as after a restart) is not counted again once units are known."""
    provider, rotation_id, batch, shared, queue_data = await _shared_rotation(tmp_path)
    first = batch.index(shared)
    provider._handed_units.clear()
    uri = str(shared.uri)
    await provider._on_queue_updated(_queue_event(uri, f"q{first}", PlaybackState.PLAYING))
    state = provider._progress.get(rotation_id, HOUSEHOLD)
    assert (state.played, state.unit_plays) == ({uri}, {})
    expected = [track for index, track in enumerate(batch) if index != first]
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(expected)
    _play_at(queue_data, first)
    await provider.on_media_item_played(_report(uri, is_playing=True))
    assert state.unit_plays == {}
    assert _uris(await provider.get_playlist_tracks(rotation_id)) == _uris(expected)
    await provider.unload()
    saved = json.loads((tmp_path / "rotation_playlists" / "progress.json").read_text())
    assert saved["entries"][rotation_id][HOUSEHOLD]["pooled_items"] == [f"q{first}"]
