"""Tests for the AI Radio media item surface."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping, SoundEffect, Track

from music_assistant.providers.ai_radio.constants import (
    ATTR_FEED_CLIP,
    ATTR_HOST_ID,
    ATTR_PROMPT,
    ATTR_STATION_ID,
)
from music_assistant.providers.ai_radio.media import AIRadioMediaMixin
from music_assistant.providers.ai_radio.queue_dj import AIRadioQueueDJMixin
from music_assistant.providers.ai_radio.runtime import AIRadioRuntimeMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin

STATION = {
    "id": "morning_show",
    "name": "Morning Show",
    "source_playlist_id": "42",
    "source_playlist_provider": "library",
    "default_player_id": "",
    "max_duration_minutes": 0.0,
    "shuffle_source_tracks": True,
    "host_id": "amy",
}


class _Media(AIRadioQueueDJMixin, AIRadioMediaMixin):
    """Bare media harness; the queue DJ mixin only supplies its queue item reads."""

    def __init__(self, stations: dict[str, dict[str, Any]]) -> None:
        """Stamp the attrs AIRadioMediaMixin reads, skipping real provider init."""
        self._stations = stations
        self._show_runs: dict[str, Any] = {}
        self._show_runs_lock = asyncio.Lock()
        self._show_library_ids: dict[str, str] = {}
        self._hosts: dict[str, Any] = {}
        self._feed_clip_contracts: dict[str, dict[str, Any]] = {}
        self.instance_id = "ai_radio"
        self.domain = "ai_radio"
        # the mixins declare `mass: MusicAssistant`; a mock stands in for tests
        self.mass: Any = MagicMock()
        self.logger = MagicMock()

    def _ai_radio_cover_image_path(self) -> str:
        """Return a fake cover image path."""
        return "/tmp/air.png"  # noqa: S108


class _StubConfig:
    """Minimal ProviderConfig stand-in exposing get_value."""

    def get_value(self, key: str, default: Any = None) -> Any:
        """Return the default for every config key."""
        return default


class _ShowMedia(AIRadioRuntimeMixin, AIRadioStorageMixin, _Media):
    """Harness combining the media mixin with the real planner, for the intro-in-feed path."""

    def __init__(self, stations: dict[str, dict[str, Any]]) -> None:
        """Stamp the planner state on top of the bare media harness."""
        super().__init__(stations)
        self.config = cast("Any", _StubConfig())
        self._sections: dict[str, dict[str, Any]] = {
            "Intro": {
                "id": "Intro",
                "name": "Show Intro",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": "Welcome, first up <next_songinfo>",
                "constraints": {"max_chars": 200},
            }
        }
        self._hosts["amy"] = {
            "id": "amy",
            "name": "Amy",
            "instructions": "x",
            "tts_engine": "",
            "section_ids": ["Intro"],
            "section_order": [{"when": "start_of_playlist", "flow": [{"MUST": "Intro"}]}],
            "merge_section_id": "",
        }


async def test_get_radio_builds_dynamic_radio() -> None:
    """get_radio builds a dynamic Radio item with a unique provider mapping."""
    media = _Media({"morning_show": STATION})
    radio = await media.get_radio("morning_show")
    assert radio.item_id == "morning_show"
    assert radio.provider == "ai_radio"
    assert radio.is_dynamic is True
    assert radio.uri == "ai_radio://radio/morning_show"
    mapping = next(iter(radio.provider_mappings))
    assert mapping.is_unique is True


async def test_get_radio_unknown_station_raises() -> None:
    """get_radio raises when the station id is unknown."""
    media = _Media({})
    with pytest.raises(MediaNotFoundError):
        await media.get_radio("nope")


async def test_library_upkeep_adds_missing_show() -> None:
    """_sync_show_library_items adds a show that has no library item yet."""
    media = _Media({"morning_show": STATION})
    radio_ctrl = media.mass.music.radio
    radio_ctrl.get_library_item_by_prov_mappings = AsyncMock(return_value=None)
    added = MagicMock(item_id="7", name="Morning Show")
    radio_ctrl.add_item_to_library = AsyncMock(return_value=added)

    async def _no_items() -> AsyncGenerator[Any]:
        return
        yield

    radio_ctrl.iter_library_items = MagicMock(return_value=_no_items())
    await media._sync_show_library_items()
    radio_ctrl.add_item_to_library.assert_awaited_once()
    # the sync also rebuilds the map that resolves a library row back to its station
    assert media._show_library_ids == {"7": "morning_show"}


def _track(item_id: str) -> Track:
    """Build a minimal Track with one provider mapping."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="filesystem_local",
                provider_instance="filesystem_local",
            )
        },
    )


def _media_with_show(track_count: int, max_duration_minutes: float = 0.0) -> _Media:
    """
    Build a bare _Media harness with a station whose source playlist has track_count tracks.

    Its show names a host the harness does not hold, so no intro is ever planned for it.
    """
    station = {**STATION, "max_duration_minutes": max_duration_minutes}
    return _stub_source_tracks(_Media({"morning_show": station}), track_count)


def _show_media(track_count: int) -> _ShowMedia:
    """Build a planner-backed harness whose show's host opens every show with an intro."""
    return _stub_source_tracks(_ShowMedia({"morning_show": STATION}), track_count)


def _stub_source_tracks[MediaT: _Media](media: MediaT, track_count: int) -> MediaT:
    """Stub the harness's source playlist with track_count tracks and no queue playing it."""
    media._fetch_source_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            [
                {
                    "index": i,
                    "item_id": f"t{i}",
                    "name": f"Track t{i}",
                    "artist": "",
                    "songinfo": f"Track t{i}",
                    "duration": 210,
                    "media_item": _track(f"t{i}"),
                }
                for i in range(track_count)
            ],
            "My Playlist",
        )
    )
    media.mass.player_queues.all = MagicMock(return_value=[])
    media.mass.player_queues.items = MagicMock(return_value=[])
    return media


def _show_queue(
    queue_id: str = "q1",
    uri: str = "ai_radio://radio/morning_show",
    items: int = 0,
    ended: bool = False,
) -> SimpleNamespace:
    """Build a fake queue sourcing the show, empty and live by default."""
    return SimpleNamespace(
        queue_id=queue_id, sources=[SimpleNamespace(uri=uri)], items=items, ended=ended
    )


def _attach_show_queues(media: _Media, *queues: SimpleNamespace) -> None:
    """Register the given fake queues, in that order, on the harness's player_queues."""
    media.mass.player_queues.all = MagicMock(return_value=list(queues))
    media.mass.player_queues.get = MagicMock(
        side_effect=lambda queue_id: next((q for q in queues if q.queue_id == queue_id), None)
    )


def _attach_show_queue(
    media: _Media, uri: str = "ai_radio://radio/morning_show"
) -> SimpleNamespace:
    """Register one fake queue playing the show, in a consuming state."""
    queue = _show_queue(uri=uri)
    _attach_show_queues(media, queue)
    return queue


def _queued(media: _Media, *uris: str) -> None:
    """Stub the harness's queue item reads to return items with the given uris."""
    media.mass.player_queues.items = MagicMock(
        return_value=[SimpleNamespace(uri=uri) for uri in uris]
    )


async def test_first_call_starts_a_run_and_pages() -> None:
    """The first call for a playing queue starts a run and pages 20 tracks at a time."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)
    page1 = await media.get_dynamic_radio_tracks("morning_show")
    assert len(page1) == 20
    page2 = await media.get_dynamic_radio_tracks("morning_show")
    assert len(page2) == 10
    # the run is exhausted: the empty batch is what ends the show's feed
    assert await media.get_dynamic_radio_tracks("morning_show") == []


async def test_concurrent_first_calls_start_only_one_run() -> None:
    """Two concurrent first-calls for the same station snapshot only once and page in turn."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)

    async def _fetch_source_tracks(
        _station: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        # yields control so the second concurrent call can race the first
        await asyncio.sleep(0)
        return (
            [
                {"index": i, "item_id": f"t{i}", "duration": 210, "media_item": _track(f"t{i}")}
                for i in range(30)
            ],
            "My Playlist",
        )

    media._fetch_source_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=_fetch_source_tracks
    )
    page1, page2 = await asyncio.gather(
        media.get_dynamic_radio_tracks("morning_show"),
        media.get_dynamic_radio_tracks("morning_show"),
    )
    media._fetch_source_tracks.assert_awaited_once()
    assert {len(page1), len(page2)} == {20, 10}


async def test_run_binds_to_a_queue_sourcing_the_shows_library_uri() -> None:
    """A run binds to a queue whose source names the show by its library identity."""
    media = _media_with_show(track_count=3)
    media._show_library_ids = {"7": "morning_show"}
    _attach_show_queue(media, uri="library://radio/7")

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert len(page) == 3
    assert media._show_runs["morning_show"].queue_id == "q1"


async def test_run_binds_to_a_queue_sourcing_the_provider_uri() -> None:
    """A run binds to a queue whose source names the show by its provider uri."""
    media = _media_with_show(track_count=3)
    _attach_show_queue(media)

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert len(page) == 3
    assert media._show_runs["morning_show"].queue_id == "q1"


async def test_run_skips_an_ended_queue_that_still_sources_the_show() -> None:
    """A persisted ended queue keeps its sources; the queue starting the show wins the run."""
    media = _media_with_show(track_count=3)
    _attach_show_queues(
        media, _show_queue(queue_id="q_ended", ended=True), _show_queue(queue_id="q_live")
    )

    await media.get_dynamic_radio_tracks("morning_show")

    assert media._show_runs["morning_show"].queue_id == "q_live"


async def test_run_prefers_the_queue_being_filled() -> None:
    """Among live queues sourcing the show, the emptied one is the one fetching its pool."""
    media = _media_with_show(track_count=3)
    _attach_show_queues(
        media, _show_queue(queue_id="q_playing", items=25), _show_queue(queue_id="q_filling")
    )

    await media.get_dynamic_radio_tracks("morning_show")

    assert media._show_runs["morning_show"].queue_id == "q_filling"


async def test_only_an_ended_queue_serves_a_one_off_batch() -> None:
    """With only an ended queue sourcing the show there is nothing live to bind a run to."""
    media = _media_with_show(track_count=3)
    _attach_show_queues(media, _show_queue(ended=True))

    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3
    assert media._show_runs == {}


async def test_queue_vanishing_during_the_snapshot_binds_no_run() -> None:
    """A queue cleared while the snapshot is fetched must not end up bound to a run."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)

    async def _fetch_source_tracks(
        _station: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        _attach_show_queues(media)
        return (
            [
                {"index": i, "item_id": f"t{i}", "duration": 210, "media_item": _track(f"t{i}")}
                for i in range(30)
            ],
            "My Playlist",
        )

    media._fetch_source_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=_fetch_source_tracks
    )

    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    assert media._show_runs == {}


async def test_run_end_allows_a_fresh_run() -> None:
    """Ending a run lets a later call start a fresh snapshot instead of staying exhausted."""
    media = _media_with_show(track_count=3)
    _attach_show_queue(media)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3
    assert await media.get_dynamic_radio_tracks("morning_show") == []
    media._end_show_run("morning_show")
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 3


async def test_sample_without_a_playing_queue_is_stateless() -> None:
    """A details-view sample serves a preview without creating or consuming a run."""
    media = _media_with_show(track_count=30)

    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20
    assert media._show_runs == {}
    # a repeated sample snapshots afresh instead of paging through hidden state
    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20
    assert media._show_runs == {}
    assert media._fetch_source_tracks.await_count == 2  # type: ignore[attr-defined]


async def test_sample_during_a_live_show_leaves_the_run_untouched() -> None:
    """A details-view sample while the show plays must not move the run's cursor."""
    media = _media_with_show(track_count=30)
    _attach_show_queue(media)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    run = media._show_runs["morning_show"]
    assert run.cursor == 20

    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20

    assert media._show_runs["morning_show"] is run
    assert run.cursor == 20


async def test_playback_after_a_sample_serves_the_full_show() -> None:
    """A sample before pressing play must not eat into the playback run's pages."""
    media = _media_with_show(track_count=30)
    # the user opens the show's details page first: a stateless sample
    assert len(await media.get_dynamic_radio_tracks("morning_show", sample=True)) == 20
    # then presses play: the queue stores the show as source before the first feed call
    _attach_show_queue(media)
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 10
    assert await media.get_dynamic_radio_tracks("morning_show") == []


async def test_consume_without_a_queue_serves_a_one_off_batch() -> None:
    """A consume call with no queue sourcing the show serves a batch but binds no run."""
    media = _media_with_show(track_count=30)

    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 20
    assert media._show_runs == {}


async def test_duration_cap_trims_snapshot() -> None:
    """The run's snapshot is trimmed to the station's configured maximum duration."""
    media = _media_with_show(track_count=10, max_duration_minutes=7.0)
    page = await media.get_dynamic_radio_tracks("morning_show")
    # 10 tracks of 210s; the cap keeps tracks until >= 7 minutes (2 tracks)
    assert len(page) == 2


async def test_unknown_station_raises() -> None:
    """get_dynamic_radio_tracks raises when the station id is unknown."""
    media = _Media({})
    with pytest.raises(MediaNotFoundError):
        await media.get_dynamic_radio_tracks("nope")


async def test_first_page_of_a_consume_opens_with_the_intro() -> None:
    """The first feed page leads with the show's intro clip, whose render contract is stored."""
    media = _show_media(30)
    _attach_show_queue(media)

    page1 = await media.get_dynamic_radio_tracks("morning_show")

    assert len(page1) == 20  # the intro takes one of the page's slots
    intro = page1[0]
    assert isinstance(intro, SoundEffect)
    assert intro.provider == "ai_radio"
    assert all(isinstance(item, Track) for item in page1[1:])
    contract = media._feed_clip_contracts[intro.item_id]
    assert contract[ATTR_STATION_ID] == "morning_show"
    assert contract[ATTR_HOST_ID] == "amy"
    # the pool may reorder the tracks behind the intro, so the song it announces is only
    # filled in at render time from the queue: the contract keeps the token and says so
    assert contract[ATTR_PROMPT].startswith("Welcome, first up <next_songinfo>")
    assert contract[ATTR_FEED_CLIP] is True
    assert media._show_runs["morning_show"].clip_ids == [intro.item_id]
    # the rest of the show pages on as before
    assert len(await media.get_dynamic_radio_tracks("morning_show")) == 11
    assert await media.get_dynamic_radio_tracks("morning_show") == []


async def test_a_queue_already_holding_show_tracks_resumes_without_an_intro() -> None:
    """After a restart the restored queue holds the show: no second intro, no repeats."""
    media = _show_media(30)
    _attach_show_queues(media, _show_queue(items=6))
    _queued(media, *(f"library://track/t{i}" for i in range(5)), "library://track/other")

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert all(isinstance(item, Track) for item in page)
    assert media._feed_clip_contracts == {}
    run = media._show_runs["morning_show"]
    assert run.clip_ids == []
    assert len(run.tracks) == 25
    assert not {item.uri for item in run.tracks} & {f"library://track/t{i}" for i in range(5)}


async def test_a_queue_holding_only_unrelated_items_gets_a_fresh_show() -> None:
    """Items of anything but this show leave the run fresh: intro first, full snapshot."""
    media = _show_media(3)
    _attach_show_queues(media, _show_queue(items=2))
    _queued(media, "library://track/other1", "library://track/other2")

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert isinstance(page[0], SoundEffect)
    assert len(page) == 4


async def test_a_sample_carries_no_intro() -> None:
    """A details-view sample lists the music only and stores no contract."""
    media = _show_media(30)
    _attach_show_queue(media)

    page = await media.get_dynamic_radio_tracks("morning_show", sample=True)

    assert len(page) == 20
    assert all(isinstance(item, Track) for item in page)
    assert media._feed_clip_contracts == {}
    assert media._show_runs == {}


async def test_a_consume_without_a_queue_carries_no_intro() -> None:
    """A one-off batch for a stray fetch binds no run, so it also plans no intro."""
    media = _show_media(30)

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert all(isinstance(item, Track) for item in page)
    assert media._feed_clip_contracts == {}


async def test_ending_the_run_drops_the_intros_contract() -> None:
    """Ending a run releases its intro's render contract."""
    media = _show_media(3)
    _attach_show_queue(media)
    intro = (await media.get_dynamic_radio_tracks("morning_show"))[0]
    assert intro.item_id in media._feed_clip_contracts

    media._end_show_run("morning_show")

    assert media._feed_clip_contracts == {}
    assert media._show_runs == {}


async def test_a_replay_gets_a_fresh_intro_clip_id() -> None:
    """A replayed show mints a new intro clip id, so it never collides with the played one."""
    media = _show_media(3)
    _attach_show_queue(media)
    first = (await media.get_dynamic_radio_tracks("morning_show"))[0]
    media._end_show_run("morning_show")

    second = (await media.get_dynamic_radio_tracks("morning_show"))[0]

    assert isinstance(second, SoundEffect)
    assert second.item_id != first.item_id


async def test_a_show_whose_intro_cannot_be_planned_still_plays() -> None:
    """A host without sections cannot plan an intro; the show plays its music regardless."""
    media = _show_media(3)
    media._hosts["amy"]["section_ids"] = []
    media._hosts["amy"]["section_order"] = []
    _attach_show_queue(media)

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert len(page) == 3
    assert all(isinstance(item, Track) for item in page)
    assert media._feed_clip_contracts == {}
    cast("MagicMock", media.logger).warning.assert_called_once()


def _weather_guarded_intro(media: _ShowMedia) -> None:
    """Make the harness host's intro OPTIONAL, guarded on a present hourly forecast."""
    media._hosts["amy"]["section_order"] = [
        {
            "when": "start_of_playlist",
            "flow": [
                {
                    "OPTIONAL": {
                        "section": "Intro",
                        "chance": 1.0,
                        "guards": {"require_placeholders_present": ["<weather_hourly>"]},
                    }
                }
            ],
        }
    ]


async def test_intro_planning_never_fetches_the_weather() -> None:
    """A weather-guarded intro sees no forecast unless a recent lookup is cached."""
    media = _show_media(3)
    _weather_guarded_intro(media)
    media._fetch_weather_tokens = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("playback start must not wait for a weather lookup")
    )
    _attach_show_queue(media)

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert all(isinstance(item, Track) for item in page)


async def test_intro_planning_uses_a_cached_forecast() -> None:
    """A weather-guarded intro is planned when a still-fresh forecast lookup is cached."""
    media = _show_media(3)
    _weather_guarded_intro(media)
    media._weather_tokens_cache = (time.monotonic(), {"<weather_hourly>": "sunny"})
    media._fetch_weather_tokens = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("a fresh cache entry must be served without a lookup")
    )
    _attach_show_queue(media)

    page = await media.get_dynamic_radio_tracks("morning_show")

    assert isinstance(page[0], SoundEffect)
