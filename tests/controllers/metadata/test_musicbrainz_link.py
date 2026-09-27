"""Tests for the hourly MusicBrainz link run and its trigger after a library sync."""

from __future__ import annotations

import asyncio
from time import time
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import EventType, ExternalID, MediaType, TaskStatus
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_ALBUMS
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.metadata.constants import (
    CONF_LINK_PROVIDERS_VIA_MUSICBRAINZ,
    CONF_MUSICBRAINZ_LINKED_DOMAINS,
    CONF_THUMB_CACHE_MAX_SIZE,
    MUSICBRAINZ_LINK_BATCH_SIZE,
    MUSICBRAINZ_LINK_TASK_ID,
    REFRESH_INTERVAL,
)
from music_assistant.controllers.metadata.controller import (
    _albums_to_identify_query,
    _artists_to_identify_query,
    _relink_query,
    _tracks_to_identify_query,
)
from music_assistant.controllers.tasks import TasksController
from music_assistant.controllers.tasks.constants import TASK_UPDATE_TIMER_ID
from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator, Sequence

    from music_assistant.mass import MusicAssistant

_CONTROLLER = "music_assistant.controllers.metadata.controller"
_REPORT_FAILURE = f"{_CONTROLLER}.report_current_task_failure"
_PROGRESS_TEXT = f"{_CONTROLLER}.update_current_task_progress_text"
NOW = 1_700_000_000
RELEASE_ID = "5d9c6e8a-1c3b-4f2e-9a7d-2b8c4e6f0a11"
ARTIST_ID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"


@pytest.fixture(autouse=True)
def _no_pacing() -> Iterator[None]:
    """Run the link run without the pause between items."""
    with patch(f"{_CONTROLLER}.MUSICBRAINZ_LINK_ITEM_INTERVAL", 0):
        yield


@pytest.fixture
async def tasks_controller(mass_minimal: MusicAssistant) -> AsyncGenerator[TasksController]:
    """Set up the background tasks controller on a minimal Music Assistant instance."""
    controller = TasksController(mass_minimal)
    mass_minimal.tasks = controller
    await controller.setup(await mass_minimal.config.get_core_config(controller.domain))
    controller.initialized.set()
    try:
        yield controller
    finally:
        mass_minimal.cancel_timer(TASK_UPDATE_TIMER_ID)
        await controller.close()


# --------------------------------------------------------------------------- #
#  builders                                                                    #
# --------------------------------------------------------------------------- #


def _musicbrainz(rate_limited: bool = False) -> Mock:
    """Return a MusicBrainz provider stub reporting the given rate limit state."""
    musicbrainz = Mock()
    musicbrainz.rate_limited = rate_limited
    return musicbrainz


def _controller(
    *,
    linking: bool = True,
    musicbrainz: Mock | None = None,
    linked_domains: dict[str, int] | None = None,
    loaded_domains: Sequence[str] = (),
    unavailable_domains: Sequence[str] = (),
    stub_link: bool = True,
) -> MetaDataController:
    """
    Return a bare MetaDataController with its library, config and providers stubbed.

    :param linking: The state of the provider linking toggle.
    :param musicbrainz: The MusicBrainz provider, a fresh stub when not given.
    :param linked_domains: The persisted map of linked provider domains to first-seen epochs,
        None when no run has persisted one yet.
    :param loaded_domains: Domains with an available music provider instance.
    :param unavailable_domains: Domains whose only music provider instance is unavailable.
    :param stub_link: Whether to replace the per-item link step by a stub.
    """
    ctrl = MetaDataController.__new__(MetaDataController)
    ctrl._corrupt_metadata_rows = {}
    ctrl._musicbrainz_link_summary = None
    ctrl.logger = Mock()
    ctrl.config = Mock()
    ctrl.config.get_value = lambda key, default=None: (
        linking if key == CONF_LINK_PROVIDERS_VIA_MUSICBRAINZ else default
    )
    mass = Mock()
    for controller in (mass.music.albums, mass.music.artists, mass.music.tracks):
        controller.get_library_items_by_query = AsyncMock(return_value=[])
        controller.update_item_in_library = AsyncMock()
    mass.music.active_sync_tasks = []
    stored = (
        None
        if linked_domains is None
        else [f"{domain}:{seen}" for domain, seen in linked_domains.items()]
    )
    mass.config.get_raw_core_config_value = Mock(return_value=stored)
    mass.music.get_provider_instances = _provider_instances(loaded_domains, unavailable_domains)
    mass.get_provider = Mock(return_value=musicbrainz or _musicbrainz())
    ctrl.mass = mass
    if stub_link:
        ctrl.link_item_to_musicbrainz = AsyncMock()  # type: ignore[method-assign]
    return ctrl


def _item(
    media_type: MediaType, item_id: str = "1", name: str = "Item", *, mbid: str | None = None
) -> Mock:
    """Build a lightweight stand-in for a library item."""
    item = Mock()
    item.item_id = item_id
    item.name = name
    item.media_type = media_type
    item.mbid = mbid
    item.provider_mappings = set()
    # what the (stubbed) link step leaves behind once it has looked the item up
    item.metadata.last_musicbrainz_lookup = NOW
    return item


def _albums(count: int) -> list[Mock]:
    """Build a batch of album stand-ins."""
    return [_item(MediaType.ALBUM, str(index), f"Album {index}") for index in range(count)]


def _queue(
    ctrl: MetaDataController,
    *,
    albums: Sequence[Mock] = (),
    artists: Sequence[Mock] = (),
    tracks: Sequence[Mock] = (),
    relink_albums: Sequence[Mock] = (),
    relink_artists: Sequence[Mock] = (),
) -> list[str]:
    """
    Stub the batch queries of a controller and return the phases queried, in order.

    :param albums: Albums the identify query returns; ``relink_albums`` those of a relink query.
    """
    queried: list[str] = []
    music = _mass(ctrl).music

    def _answer(name: str, items: Sequence[Mock], relink_items: Sequence[Mock]) -> AsyncMock:
        async def _query(**kwargs: Any) -> list[Mock]:
            params = kwargs.get("extra_query_params") or {}
            if "domain" in params:
                queried.append(f"relink {name} {params['domain']}")
                return list(relink_items)[: kwargs["limit"]]
            queried.append(name)
            return list(items)[: kwargs["limit"]]

        return AsyncMock(side_effect=_query)

    music.albums.get_library_items_by_query = _answer("albums", albums, relink_albums)
    music.artists.get_library_items_by_query = _answer("artists", artists, relink_artists)
    music.tracks.get_library_items_by_query = _answer("tracks", tracks, ())
    return queried


def _linked(ctrl: MetaDataController) -> AsyncMock:
    """Return the stubbed per-item link step of a controller."""
    return cast("AsyncMock", ctrl.link_item_to_musicbrainz)


def _mass(ctrl: MetaDataController) -> Mock:
    """Return the stubbed MusicAssistant instance of a controller."""
    return cast("Mock", ctrl.mass)


def _provider_instances(loaded: Sequence[str] = (), unavailable: Sequence[str] = ()) -> Mock:
    """
    Return a stand-in for the music provider instance lookup by domain.

    Like the real lookup, an unavailable instance only shows up when asked for.

    :param loaded: Domains with an available instance.
    :param unavailable: Domains whose only instance is unavailable.
    """

    def _instances(domain: str, return_unavailable: bool = False) -> list[Mock]:
        if domain in loaded:
            return [Mock(available=True)]
        if domain in unavailable and return_unavailable:
            return [Mock(available=False)]
        return []

    return Mock(side_effect=_instances)


# --------------------------------------------------------------------------- #
#  phases and budget                                                           #
# --------------------------------------------------------------------------- #


async def test_link_run_queries_the_phases_in_order_newest_first() -> None:
    """Albums, then artists, then tracks are selected by their queries, newest first."""
    ctrl = _controller()
    queried = _queue(ctrl)

    with patch(f"{_CONTROLLER}.time", return_value=float(NOW)):
        await ctrl._link_library_to_musicbrainz()

    assert queried == ["albums", "artists", "tracks"]
    music = _mass(ctrl).music
    for controller, query, params in (
        (music.albums, _albums_to_identify_query(), {"stale": NOW - REFRESH_INTERVAL}),
        (music.artists, _artists_to_identify_query(), {}),
        (music.tracks, _tracks_to_identify_query(), {}),
    ):
        controller.get_library_items_by_query.assert_awaited_once_with(
            limit=MUSICBRAINZ_LINK_BATCH_SIZE,
            order_by="timestamp_added_desc",
            extra_query_parts=[query],
            extra_query_params=params,
        )


async def test_link_run_budget_carries_over_across_the_phases() -> None:
    """What the albums leave of the budget goes to the artists; an exhausted budget ends the run."""
    ctrl = _controller()
    queried = _queue(ctrl, albums=_albums(30), artists=_albums(30))

    await ctrl._link_library_to_musicbrainz()

    assert queried == ["albums", "artists"]
    artists_query = _mass(ctrl).music.artists.get_library_items_by_query
    assert artists_query.await_args.kwargs["limit"] == MUSICBRAINZ_LINK_BATCH_SIZE - 30
    assert _linked(ctrl).await_count == MUSICBRAINZ_LINK_BATCH_SIZE
    assert ctrl._musicbrainz_link_summary is not None
    assert ctrl._musicbrainz_link_summary["processed"] == {"albums": 30, "artists": 20}


async def test_link_run_with_an_empty_queue_is_a_noop() -> None:
    """Nothing to identify: no item is touched and the run still finishes cleanly."""
    ctrl = _controller()

    with patch(f"{_CONTROLLER}.update_current_task_progress") as progress:
        await ctrl._link_library_to_musicbrainz()

    _linked(ctrl).assert_not_awaited()
    progress.assert_called_once_with(100, "Processed 0 item(s)")
    assert ctrl._musicbrainz_link_summary is not None
    assert ctrl._musicbrainz_link_summary["processed"] == {}


async def test_link_run_summarizes_what_it_found_and_linked() -> None:
    """The run counts the items that gained a provider link and those MusicBrainz did not know."""
    ctrl = _controller()
    found = _item(MediaType.ALBUM, "1", "Linked", mbid=RELEASE_ID)
    unknown = _item(MediaType.ALBUM, "2", "Unknown")
    _queue(ctrl, albums=[found, unknown])

    async def _link(item: Mock) -> None:
        if item is found:
            item.provider_mappings.add(Mock())

    _linked(ctrl).side_effect = _link

    await ctrl._link_library_to_musicbrainz()

    assert ctrl._musicbrainz_link_summary is not None
    assert ctrl._musicbrainz_link_summary["linked"] == 1
    assert ctrl._musicbrainz_link_summary["not_found"] == 1
    assert ctrl._musicbrainz_link_summary["failed"] == 0
    assert ctrl._musicbrainz_link_summary["stopped_on_rate_limit"] is False


async def test_link_run_paces_between_items_but_not_around_the_last() -> None:
    """The pause separates two lookups; the first item starts right away and the last ends the run."""
    ctrl = _controller()
    _queue(ctrl, albums=_albums(3))

    with patch(f"{_CONTROLLER}.asyncio.sleep", AsyncMock()) as sleep:
        await ctrl._link_library_to_musicbrainz()

    assert sleep.await_count == 2


async def test_link_run_counts_its_progress_from_one() -> None:
    """The first item reads as item 1 of the batch, like the other scans."""
    ctrl = _controller()
    _queue(ctrl, albums=_albums(2))

    with patch(f"{_CONTROLLER}.update_current_task_progress_from_index") as progress:
        await ctrl._link_library_to_musicbrainz()

    assert [call.args[:2] for call in progress.call_args_list] == [
        (1, MUSICBRAINZ_LINK_BATCH_SIZE),
        (2, MUSICBRAINZ_LINK_BATCH_SIZE),
    ]


# --------------------------------------------------------------------------- #
#  failure isolation, rate limiting, waiting                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        MusicAssistantError("boom"),
        aiohttp.ClientError("connection reset"),
        TimeoutError("timed out"),
    ],
)
async def test_link_run_isolates_a_failing_item(error: Exception) -> None:
    """A failing item is reported and the rest of the batch is still processed."""
    ctrl = _controller()
    _queue(ctrl, albums=[_item(MediaType.ALBUM, "1", "Failing Album"), _albums(2)[1]])
    _linked(ctrl).side_effect = [error, None]

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._link_library_to_musicbrainz()  # must not raise

    report_failure.assert_called_once_with(f"Failing Album: {error}")
    assert _linked(ctrl).await_count == 2
    assert ctrl._musicbrainz_link_summary is not None
    assert ctrl._musicbrainz_link_summary["failed"] == 1


async def test_link_run_reports_an_item_whose_lookup_gave_up() -> None:
    """An item left without its lookup marker is reported: it heads the next run again."""
    ctrl = _controller()
    stuck = _item(MediaType.ALBUM, "1", "Stuck Album")
    stuck.metadata.last_musicbrainz_lookup = None
    _queue(ctrl, albums=[stuck, _item(MediaType.ALBUM, "2", "Fine Album", mbid=RELEASE_ID)])

    with patch(_REPORT_FAILURE) as report_failure:
        await ctrl._link_library_to_musicbrainz()

    report_failure.assert_called_once_with("Stuck Album: lookup failed")
    assert ctrl._musicbrainz_link_summary is not None
    assert ctrl._musicbrainz_link_summary["processed"] == {"albums": 2}
    assert ctrl._musicbrainz_link_summary["failed"] == 1
    assert ctrl._musicbrainz_link_summary["not_found"] == 0


async def test_link_run_stops_when_musicbrainz_starts_rate_limiting() -> None:
    """A cooldown armed mid-batch ends the run; the rest waits for the next run."""
    musicbrainz = _musicbrainz()
    type(musicbrainz).rate_limited = PropertyMock(side_effect=[False, True])
    ctrl = _controller(musicbrainz=musicbrainz)
    queried = _queue(ctrl, albums=_albums(3), artists=_albums(1))

    with patch(_PROGRESS_TEXT) as progress_text:
        await ctrl._link_library_to_musicbrainz()

    assert _linked(ctrl).await_count == 1
    assert queried == ["albums"]
    progress_text.assert_any_call("MusicBrainz is rate limiting, resuming next run")
    assert ctrl._musicbrainz_link_summary is not None
    assert ctrl._musicbrainz_link_summary["stopped_on_rate_limit"] is True


async def test_link_run_waits_while_a_library_sync_is_active() -> None:
    """A run during a sync does nothing; the completed sync queues it again."""
    ctrl = _controller()
    _mass(ctrl).music.active_sync_tasks = [Mock()]
    queried = _queue(ctrl, albums=_albums(1))

    with patch(_PROGRESS_TEXT) as progress_text:
        await ctrl._link_library_to_musicbrainz()

    assert queried == []
    progress_text.assert_called_once_with("Waiting for music sync completion")
    _mass(ctrl).config.set_raw_core_config_value.assert_not_called()


async def test_link_run_with_linking_disabled_returns_early_and_keeps_the_domains() -> None:
    """With the toggle off nothing is selected and the persisted map is left as it is."""
    ctrl = _controller(linking=False, linked_domains={"spotify": 100}, loaded_domains=["spotify"])
    queried = _queue(ctrl, albums=_albums(1))

    with patch(_PROGRESS_TEXT) as progress_text:
        await ctrl._link_library_to_musicbrainz()

    assert queried == []
    progress_text.assert_called_once_with("Linking through MusicBrainz is disabled")
    _mass(ctrl).config.set_raw_core_config_value.assert_not_called()


async def test_link_run_without_a_musicbrainz_provider_returns_early() -> None:
    """Without the MusicBrainz provider there is nothing to identify items with."""
    ctrl = _controller()
    _mass(ctrl).get_provider = Mock(return_value=None)
    queried = _queue(ctrl, albums=_albums(1))

    with patch(_PROGRESS_TEXT) as progress_text:
        await ctrl._link_library_to_musicbrainz()

    assert queried == []
    progress_text.assert_called_once_with("The MusicBrainz provider is not loaded")
    _mass(ctrl).config.set_raw_core_config_value.assert_not_called()


# --------------------------------------------------------------------------- #
#  relinking for a provider loaded later                                       #
# --------------------------------------------------------------------------- #


async def test_link_run_relinks_for_a_provider_loaded_since_the_previous_run() -> None:
    """A new provider starts now, one no longer loaded is dropped, and each kept gets a relink phase."""
    ctrl = _controller(
        linked_domains={"spotify": 100, "deezer": 200},
        loaded_domains=["tidal", "spotify", "filesystem_local"],
    )
    queried = _queue(ctrl)

    with patch(f"{_CONTROLLER}.time", return_value=float(NOW)):
        await ctrl._link_library_to_musicbrainz()

    _mass(ctrl).config.set_raw_core_config_value.assert_called_once_with(
        "metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, ["spotify:100", f"tidal:{NOW}"]
    )
    assert queried == [
        "albums",
        "artists",
        "tracks",
        "relink albums spotify",
        "relink artists spotify",
        "relink albums tidal",
        "relink artists tidal",
    ]
    relink_calls = [
        call.kwargs
        for call in _mass(ctrl).music.albums.get_library_items_by_query.await_args_list
        if "domain" in (call.kwargs["extra_query_params"] or {})
    ]
    assert relink_calls[0]["extra_query_parts"] == [
        _relink_query("albums", MediaType.ALBUM, ExternalID.MB_ALBUM)
    ]
    assert relink_calls[0]["extra_query_params"] == {"domain": "spotify", "seen": 100}
    assert relink_calls[1]["extra_query_params"] == {"domain": "tidal", "seen": NOW}
    artist_relink = _mass(ctrl).music.artists.get_library_items_by_query.await_args_list[1]
    assert artist_relink.kwargs["extra_query_parts"] == [
        _relink_query("artists", MediaType.ARTIST, ExternalID.MB_ARTIST)
    ]


async def test_link_run_keeps_the_linked_domains_when_nothing_changed() -> None:
    """The persisted map is only rewritten when a provider came or went."""
    ctrl = _controller(linked_domains={"spotify": 100}, loaded_domains=["spotify"])
    _queue(ctrl)

    await ctrl._link_library_to_musicbrainz()

    _mass(ctrl).config.set_raw_core_config_value.assert_not_called()


async def test_link_run_counts_an_unavailable_provider_instance_as_loaded() -> None:
    """A provider whose instance is loaded but unavailable keeps its place: the link step maps to it."""
    ctrl = _controller(linked_domains={"spotify": 100}, unavailable_domains=["spotify"])
    _queue(ctrl)

    await ctrl._link_library_to_musicbrainz()

    _mass(ctrl).music.get_provider_instances.assert_any_call("spotify", return_unavailable=True)
    _mass(ctrl).config.set_raw_core_config_value.assert_not_called()
    relink = _mass(ctrl).music.albums.get_library_items_by_query.await_args_list[1]
    assert relink.kwargs["extra_query_params"] == {"domain": "spotify", "seen": 100}


async def test_first_link_run_seeds_the_present_providers_with_zero() -> None:
    """Nothing persisted yet: the providers present start at 0, so no item predates them."""
    ctrl = _controller(loaded_domains=["spotify"])
    _queue(ctrl)

    with patch(f"{_CONTROLLER}.time", return_value=float(NOW)):
        await ctrl._link_library_to_musicbrainz()

    _mass(ctrl).config.set_raw_core_config_value.assert_called_once_with(
        "metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, ["spotify:0"]
    )
    relink = _mass(ctrl).music.albums.get_library_items_by_query.await_args_list[1]
    assert relink.kwargs["extra_query_params"] == {"domain": "spotify", "seen": 0}


async def test_first_link_run_without_a_linked_provider_persists_an_empty_map() -> None:
    """A first run without any linkable provider still leaves its mark for the next runs."""
    ctrl = _controller()
    _queue(ctrl)

    await ctrl._link_library_to_musicbrainz()

    _mass(ctrl).config.set_raw_core_config_value.assert_called_once_with(
        "metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, []
    )


async def test_link_run_starts_a_provider_loaded_after_the_first_run_now() -> None:
    """A provider loaded once the map exists starts now: all identified before it is relinked."""
    ctrl = _controller(linked_domains={}, loaded_domains=["spotify"])
    _queue(ctrl)

    with patch(f"{_CONTROLLER}.time", return_value=float(NOW)):
        await ctrl._link_library_to_musicbrainz()

    _mass(ctrl).config.set_raw_core_config_value.assert_called_once_with(
        "metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, [f"spotify:{NOW}"]
    )


async def test_link_run_skips_a_malformed_linked_domain_entry() -> None:
    """An entry without a usable epoch is dropped, so its provider starts over like a new one."""
    ctrl = _controller(
        linked_domains={"spotify": 100}, loaded_domains=["spotify", "deezer", "tidal"]
    )
    _mass(ctrl).config.get_raw_core_config_value.return_value = [
        "spotify:100",
        "deezer",
        "tidal:soon",
    ]
    _queue(ctrl)

    with patch(f"{_CONTROLLER}.time", return_value=float(NOW)):
        await ctrl._link_library_to_musicbrainz()  # must not raise

    _mass(ctrl).config.set_raw_core_config_value.assert_called_once_with(
        "metadata",
        CONF_MUSICBRAINZ_LINKED_DOMAINS,
        [f"deezer:{NOW}", "spotify:100", f"tidal:{NOW}"],
    )


# --------------------------------------------------------------------------- #
#  persistence of the linked domains                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entries", [["spotify:100"], []])
async def test_linked_domains_survive_a_core_config_save(
    metadata_controller: MetaDataController, entries: list[str]
) -> None:
    """The map is a declared, hidden setting, so saving the metadata settings carries it over."""
    mass = metadata_controller.mass
    mass.config.set_raw_core_config_value("metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, entries)

    await mass.config.save_core_config("metadata", {CONF_THUMB_CACHE_MAX_SIZE: 1000})

    assert (
        mass.config.get_raw_core_config_value("metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS)
        == entries
    )
    config = await mass.config.get_core_config("metadata")
    assert config.values[CONF_MUSICBRAINZ_LINKED_DOMAINS].hidden


# --------------------------------------------------------------------------- #
#  the per-item entry point                                                    #
# --------------------------------------------------------------------------- #


async def test_link_item_persists_an_album_without_touching_its_refresh_marker() -> None:
    """The identity step is stored through the album controller; the metadata refresh is not."""
    ctrl = _controller(stub_link=False)
    ctrl._link_album_to_musicbrainz = AsyncMock()  # type: ignore[method-assign]
    album = Album(
        item_id="1",
        provider="library",
        name="In Rainbows",
        provider_mappings=set(),
        metadata=MediaItemMetadata(last_refresh=123),
    )

    await ctrl.link_item_to_musicbrainz(album)

    ctrl._link_album_to_musicbrainz.assert_awaited_once_with(album)
    assert album.metadata.last_refresh == 123
    _mass(ctrl).music.albums.update_item_in_library.assert_awaited_once_with("1", album)


async def test_link_item_resolves_an_artists_musicbrainz_id_first() -> None:
    """An artist without a MusicBrainz id gets it resolved before it is linked and stored."""
    ctrl = _controller(stub_link=False)
    ctrl._get_artist_mbid = AsyncMock(return_value=ARTIST_ID)  # type: ignore[method-assign]
    ctrl._link_artist_to_musicbrainz = AsyncMock()  # type: ignore[method-assign]
    artist = Artist(item_id="1", provider="library", name="Radiohead", provider_mappings=set())

    await ctrl.link_item_to_musicbrainz(artist)

    assert artist.mbid == ARTIST_ID
    ctrl._link_artist_to_musicbrainz.assert_awaited_once_with(artist)
    _mass(ctrl).music.artists.update_item_in_library.assert_awaited_once_with("1", artist)


async def test_link_item_persists_a_track() -> None:
    """A track goes through the track identity step and is stored through its controller."""
    ctrl = _controller(stub_link=False)
    ctrl._link_track_to_musicbrainz = AsyncMock()  # type: ignore[method-assign]
    track = Track(item_id="1", provider="library", name="15 Step", provider_mappings=set())

    await ctrl.link_item_to_musicbrainz(track)

    ctrl._link_track_to_musicbrainz.assert_awaited_once_with(track)
    _mass(ctrl).music.tracks.update_item_in_library.assert_awaited_once_with("1", track)


# --------------------------------------------------------------------------- #
#  diagnostics                                                                 #
# --------------------------------------------------------------------------- #


async def test_diagnostics_report_the_last_run_and_the_pending_counts() -> None:
    """Diagnostics carry the last run summary and how many items each phase still has."""
    ctrl = _controller()
    _mass(ctrl).music.database.get_count_from_query = AsyncMock(side_effect=[3, 2, 1])

    assert await ctrl.get_diagnostics() == {
        "musicbrainz_link": {
            "last_run": None,
            "pending": {"albums": 3, "artists": 2, "tracks": 1},
        }
    }


# --------------------------------------------------------------------------- #
#  the sync trigger                                                            #
# --------------------------------------------------------------------------- #


async def test_sync_completion_queues_the_link_run_once(
    tasks_controller: TasksController, metadata_controller: MetaDataController
) -> None:
    """Every completed sync queues the run; one already running is not queued again."""
    release = asyncio.Event()
    handler = AsyncMock(side_effect=release.wait)
    event = MassEvent(event=EventType.MUSIC_SYNC_COMPLETED)
    with patch.object(metadata_controller, "_link_library_to_musicbrainz", handler):
        metadata_controller._register_maintenance_tasks()
        metadata_controller._on_music_sync_completed(event)
        await asyncio.sleep(0)
        assert tasks_controller.get_task(MUSICBRAINZ_LINK_TASK_ID).status == TaskStatus.RUNNING
        metadata_controller._on_music_sync_completed(event)
        release.set()
        deadline = asyncio.get_running_loop().time() + 2
        while tasks_controller.get_task(MUSICBRAINZ_LINK_TASK_ID).status != TaskStatus.SUCCESS:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

    assert handler.await_count == 1


# --------------------------------------------------------------------------- #
#  the selections, against real library rows                                   #
# --------------------------------------------------------------------------- #


async def _add_album(
    mass: MusicAssistant,
    name: str,
    *,
    looked_up: int | None,
    mbid: str | None = None,
    domains: Sequence[str] = ("qobuz",),
) -> Album:
    """Add a library album with the given MusicBrainz lookup marker, id and provider mappings."""
    return await mass.music.albums.add_item_to_library(
        Album(
            item_id="0",
            provider="library",
            name=name,
            artists=UniqueList(),
            provider_mappings={
                ProviderMapping(
                    item_id=f"{domain}-{name}",
                    provider_domain=domain,
                    provider_instance=f"{domain}_1",
                )
                for domain in domains
            },
            external_ids={(ExternalID.MB_ALBUM, mbid)} if mbid else set(),
            metadata=MediaItemMetadata(last_musicbrainz_lookup=looked_up),
        )
    )


async def _add_artist(mass: MusicAssistant, *, looked_up: int | None) -> Artist:
    """Add an identified library artist mapped to qobuz with the given MusicBrainz lookup marker."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Radiohead",
            provider_mappings={
                ProviderMapping(item_id="rh", provider_domain="qobuz", provider_instance="qobuz_1")
            },
            external_ids={(ExternalID.MB_ARTIST, ARTIST_ID)},
            metadata=MediaItemMetadata(last_musicbrainz_lookup=looked_up),
        )
    )


async def _linked_ids(mass: MusicAssistant) -> set[str]:
    """Run the link run against the real library and return the item ids it picked up."""
    with (
        patch.object(mass.metadata, "link_item_to_musicbrainz", AsyncMock()) as link,
        patch.object(
            type(mass.music), "active_sync_tasks", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(
            MusicbrainzProvider, "rate_limited", new_callable=PropertyMock, return_value=False
        ),
    ):
        await mass.metadata._link_library_to_musicbrainz()
    return {call.args[0].item_id for call in link.await_args_list}


async def test_link_run_selects_albums_by_their_lookup_marker(mass: MusicAssistant) -> None:
    """Never looked up: selected. Fresh: not. Stale: only while MusicBrainz did not know it."""
    now = int(time())
    stale = now - REFRESH_INTERVAL - 1
    never = await _add_album(mass, "Never", looked_up=None)
    await _add_album(mass, "Fresh", looked_up=now - 1)
    stale_unknown = await _add_album(mass, "Stale Unknown", looked_up=stale)
    await _add_album(mass, "Stale Known", looked_up=stale, mbid=RELEASE_ID)

    diagnostics = await mass.metadata.get_diagnostics()
    assert diagnostics is not None
    assert diagnostics["musicbrainz_link"] == {
        "last_run": None,
        "pending": {"albums": 2, "artists": 0, "tracks": 0},
    }

    assert await _linked_ids(mass) == {never.item_id, stale_unknown.item_id}


async def test_link_run_relinks_identified_items_that_miss_a_new_providers_links(
    mass: MusicAssistant,
) -> None:
    """An identified item looked up before a provider was loaded is relinked, a mapped one not."""
    now = int(time())
    unlinked = await _add_album(mass, "Unlinked", looked_up=now - 20, mbid=RELEASE_ID)
    await _add_album(
        mass, "Mapped", looked_up=now - 20, mbid=RELEASE_ID, domains=("qobuz", "spotify")
    )
    await _add_album(mass, "Recent", looked_up=now, mbid=RELEASE_ID)
    artist = await _add_artist(mass, looked_up=now - 20)
    mass.config.set_raw_core_config_value(
        "metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, [f"spotify:{now - 10}"]
    )

    with patch.object(mass.music, "get_provider_instances", _provider_instances(["spotify"])):
        linked = await _linked_ids(mass)

    assert linked == {unlinked.item_id, artist.item_id}


async def test_first_link_run_against_an_identified_library_relinks_nothing(
    mass: MusicAssistant,
) -> None:
    """Items identified before the first run were linked as they were looked up: nothing is owed."""
    now = int(time())
    await _add_album(mass, "Identified", looked_up=now - 20, mbid=RELEASE_ID)
    await _add_artist(mass, looked_up=now - 20)

    with patch.object(mass.music, "get_provider_instances", _provider_instances(["spotify"])):
        linked = await _linked_ids(mass)

    assert linked == set()
    assert mass.config.get_raw_core_config_value("metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS) == [
        "spotify:0"
    ]


async def test_link_run_relinks_what_was_identified_while_a_provider_was_not_loaded(
    mass: MusicAssistant,
) -> None:
    """A provider without an instance is dropped; back again, it relinks what was identified meanwhile."""
    mass.config.set_raw_core_config_value(
        "metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS, ["spotify:0"]
    )
    with patch.object(mass.music, "get_provider_instances", _provider_instances()):
        assert await _linked_ids(mass) == set()
    assert mass.config.get_raw_core_config_value("metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS) == []
    unlinked = await _add_album(mass, "Unlinked", looked_up=NOW - 10, mbid=RELEASE_ID)
    await _add_album(
        mass, "Mapped", looked_up=NOW - 10, mbid=RELEASE_ID, domains=("qobuz", "spotify")
    )

    with (
        patch.object(mass.music, "get_provider_instances", _provider_instances(["spotify"])),
        patch(f"{_CONTROLLER}.time", return_value=float(NOW)),
    ):
        linked = await _linked_ids(mass)

    assert linked == {unlinked.item_id}
    assert mass.config.get_raw_core_config_value("metadata", CONF_MUSICBRAINZ_LINKED_DOMAINS) == [
        f"spotify:{NOW}"
    ]


async def test_relink_query_selects_identified_items_never_looked_up(mass: MusicAssistant) -> None:
    """An id from tags leaves the marker unset; such an item was never linked and is selected."""
    now = int(time())
    tagged = await _add_album(mass, "Tagged", looked_up=None, mbid=RELEASE_ID)
    early = await _add_album(mass, "Early", looked_up=now - 20, mbid=RELEASE_ID)
    await _add_album(mass, "Late", looked_up=now, mbid=RELEASE_ID)
    await _add_album(mass, "Unidentified", looked_up=None)

    selected = await mass.music.albums.get_library_items_by_query(
        extra_query_parts=[_relink_query(DB_TABLE_ALBUMS, MediaType.ALBUM, ExternalID.MB_ALBUM)],
        extra_query_params={"domain": "spotify", "seen": now - 10},
    )

    assert {album.item_id for album in selected} == {tagged.item_id, early.item_id}
