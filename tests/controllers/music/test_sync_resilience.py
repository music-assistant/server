"""Tests that a single failing item does not abort a library sync or trigger deletions."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_DELETIONS,
    CONF_LOG_LEVEL,
)
from music_assistant.models.music_provider import (
    MAX_LOGGED_SYNC_FAILURES,
    MusicProvider,
    describe_sync_error,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

ALBUM_IDS = ("album_1", "album_2", "album_3")
# db id the mocked library controller hands out per provider album
DB_IDS = {"album_1": 1, "album_2": 2, "album_3": 3}


class FailingAlbumProvider(MusicProvider):
    """Provider yielding three albums, of which one may fail to sync."""

    #: provider album id whose ``get_album_tracks`` raises
    fail_album_tracks_for: str | None = None
    #: yield the albums as provider favorites, so the sync performs its favorite work
    mark_favorite: bool = False

    async def get_library_albums(self) -> AsyncGenerator[Any]:
        """Yield the three test albums."""
        for item_id in ALBUM_IDS:
            album = MagicMock()
            album.item_id = item_id
            album.name = f"Album {item_id}"
            album.uri = f"test://album/{item_id}"
            album.favorite = self.mark_favorite
            album.metadata.genres = None
            album.provider_mappings = [MagicMock()]
            yield album

    #: tracks handed back by ``get_album_tracks``
    album_tracks: list[Any] | None = None

    async def get_album_tracks(self, prov_album_id: str) -> list[Any]:
        """Return the configured tracks, or raise for the album under test."""
        if prov_album_id == self.fail_album_tracks_for:
            raise ValueError("malformed album tracks payload")
        return self.album_tracks or []


def _build_provider(
    mass: MagicMock,
    *,
    sync_album_tracks: bool = False,
    sync_deletions: bool = True,
    cls: type[FailingAlbumProvider] = FailingAlbumProvider,
) -> FailingAlbumProvider:
    """Return a provider instance wired to the given (mocked) mass."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.instance_id = "test--1"
    config.domain = "test"
    values = {
        CONF_LOG_LEVEL: "GLOBAL",
        CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.key: sync_album_tracks,
        CONF_ENTRY_LIBRARY_SYNC_DELETIONS.key: sync_deletions,
    }
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return cls(mass, manifest, config)


def _build_mass(prev_library_ids: list[int] | None = None) -> MagicMock:
    """Return a mocked mass whose album controller records every synced album."""
    mass = MagicMock()
    mass.cache = MagicMock()
    mass.cache.get = AsyncMock(return_value=prev_library_ids)
    mass.cache.set = AsyncMock()

    albums = mass.music.albums
    albums.get_library_item_sync_details = AsyncMock(return_value=None)

    async def add_item_to_library(prov_item: Any) -> Any:
        library_item = MagicMock()
        library_item.item_id = DB_IDS[prov_item.item_id]
        library_item.favorite = False
        return library_item

    albums.add_item_to_library = AsyncMock(side_effect=add_item_to_library)
    mass.music.genres.sync_media_item_genres = AsyncMock()
    mass.music.library_supported = MagicMock(return_value=True)

    # controller used by the deletion pass
    controller = AsyncMock()
    mass.music.get_controller = MagicMock(return_value=controller)
    return mass


def _fail_add_for(mass: MagicMock, item_id: str) -> None:
    """Make adding the given provider album raise a plain (non-MA) error."""
    original = mass.music.albums.add_item_to_library.side_effect

    async def add_item_to_library(prov_item: Any) -> Any:
        if prov_item.item_id == item_id:
            raise KeyError("release_date")
        return await original(prov_item)

    mass.music.albums.add_item_to_library = AsyncMock(side_effect=add_item_to_library)


def _synced_album_ids(mass: MagicMock) -> list[str]:
    """Return the provider album ids that were added to the library."""
    return [call.args[0].item_id for call in mass.music.albums.add_item_to_library.await_args_list]


async def test_unexpected_error_skips_one_item_and_continues() -> None:
    """An error that is not a MusicAssistantError skips its item instead of the whole sync."""
    mass = _build_mass()
    provider = _build_provider(mass)

    _fail_add_for(mass, "album_2")

    await provider.sync_library(MediaType.ALBUM)

    # the failing album did not stop the loop: the one after it was synced too
    assert _synced_album_ids(mass) == list(ALBUM_IDS)


async def test_unexpected_error_from_provider_album_tracks_does_not_abort_sync() -> None:
    """A provider raising while returning album tracks does not take down the album sync."""
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, sync_album_tracks=True)
    provider.fail_album_tracks_for = "album_2"

    await provider.sync_library(MediaType.ALBUM)

    assert _synced_album_ids(mass) == list(ALBUM_IDS)
    # the album itself synced fine before its tracks were imported, so it stays in the result
    assert sorted(mass.cache.set.await_args.kwargs["data"]) == [1, 2, 3]
    # and the album id set is complete, so deletions are not held back
    mass.music.get_controller.return_value.get_library_item.assert_awaited_once_with(99)


async def test_deletions_skipped_when_an_item_failed() -> None:
    """A skipped item must not be read as removed from the provider."""
    mass = _build_mass(prev_library_ids=[1, 2, 3])
    provider = _build_provider(mass)

    _fail_add_for(mass, "album_2")

    await provider.sync_library(MediaType.ALBUM)

    controller = mass.music.get_controller.return_value
    controller.get_library_item.assert_not_called()
    controller.set_provider_mappings.assert_not_called()
    controller.remove_item_from_library.assert_not_called()


async def test_deletions_run_on_a_clean_sync() -> None:
    """A sync without failures still processes items that are gone from the provider."""
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass)

    controller = mass.music.get_controller.return_value
    library_item = MagicMock()
    mapping = MagicMock()
    mapping.provider_instance = provider.instance_id
    mapping.in_library = True
    library_item.provider_mappings = [mapping]
    library_item.favorite = False
    controller.get_library_item = AsyncMock(return_value=library_item)

    await provider.sync_library(MediaType.ALBUM)

    # only db id 99 is gone from the provider
    controller.get_library_item.assert_awaited_once_with(99)
    controller.set_provider_mappings.assert_awaited_once()
    assert mapping.in_library is False


async def test_library_generator_error_still_propagates() -> None:
    """
    A provider failing while listing its library aborts the sync.

    The listing is an async generator: once it raises it is closed, so there is no item
    to skip and no complete result set to run deletions against.
    """
    mass = _build_mass(prev_library_ids=[1, 2, 3])
    provider = _build_provider(mass)

    async def broken_library_albums() -> AsyncGenerator[Any]:
        raise KeyError("items")
        yield  # type: ignore[unreachable]  # pragma: no cover

    provider.get_library_albums = broken_library_albums  # type: ignore[method-assign]

    with pytest.raises(KeyError):
        await provider.sync_library(MediaType.ALBUM)

    mass.cache.set.assert_not_called()
    mass.music.get_controller.return_value.set_provider_mappings.assert_not_called()


async def test_expected_error_also_holds_back_deletions() -> None:
    """A MusicAssistantError leaves the same gap in the result set as any other error."""
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass)
    mass.music.albums.add_item_to_library = AsyncMock(side_effect=MediaNotFoundError("gone"))

    await provider.sync_library(MediaType.ALBUM)

    mass.music.get_controller.return_value.get_library_item.assert_not_called()


async def test_incomplete_run_keeps_the_previous_id_snapshot() -> None:
    """
    A run that skipped items merges into the cached id's instead of replacing them.

    Those id's are what a later run compares against; replacing them with an incomplete
    set would drop the deletions this run could not process.
    """
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass)
    _fail_add_for(mass, "album_2")

    await provider.sync_library(MediaType.ALBUM)

    # 99 (gone from the provider) and 2 (failed this run) both survive for the next run
    assert sorted(mass.cache.set.await_args.kwargs["data"]) == [1, 2, 3, 99]


async def test_deletions_not_reported_when_disabled() -> None:
    """A failed item is not reported as skipped deletions when deletions are turned off."""
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, sync_deletions=False)
    _fail_add_for(mass, "album_2")

    with patch("music_assistant.models.music_provider.report_current_task_failure") as reported:
        await provider.sync_library(MediaType.ALBUM)

    assert not any("Deletions skipped" in call.args[0] for call in reported.call_args_list)


async def test_payload_bearing_error_is_clipped() -> None:
    """
    A provider error carrying its whole api response is reported by type and clipped.

    Those messages reach the log and, via the task failure list, every connected client.
    """
    mass = _build_mass()
    provider = _build_provider(mass)
    payload = "x" * 5000
    mass.music.albums.add_item_to_library = AsyncMock(side_effect=KeyError(payload))

    with patch("music_assistant.models.music_provider.report_current_task_failure") as reported:
        await provider.sync_library(MediaType.ALBUM)

    item_failures = [
        call.args[0]
        for call in reported.call_args_list
        if call.args[0].startswith("Failed to sync")
    ]
    assert len(item_failures) == len(ALBUM_IDS)
    for message in item_failures:
        assert "KeyError" in message
        assert len(message) < 400
        assert payload not in message


def test_our_own_errors_are_reported_verbatim() -> None:
    """Our own error messages are already short and stay unchanged."""
    assert describe_sync_error(MediaNotFoundError("album not found")) == "album not found"


async def test_incomplete_run_keeps_newly_seen_items() -> None:
    """An item first seen on an incomplete run is still tracked for later cleanup."""
    # 1 and 2 were known before; 3 is new in this run
    mass = _build_mass(prev_library_ids=[1, 2])
    provider = _build_provider(mass)
    _fail_add_for(mass, "album_2")

    await provider.sync_library(MediaType.ALBUM)

    assert 3 in mass.cache.set.await_args.kwargs["data"]


async def test_item_lookup_failure_skips_only_that_item() -> None:
    """A failure while resolving an item's mappings skips the item, not the whole sync."""
    mass = _build_mass()
    provider = _build_provider(mass)
    lookups = {"album_2": TypeError("bad provider mapping")}

    async def get_sync_details(mappings: Any) -> Any:
        del mappings
        return None

    calls: list[str] = []

    async def sync_details_for(prov_mappings: Any) -> Any:
        del prov_mappings
        item_id = ALBUM_IDS[len(calls)]
        calls.append(item_id)
        if err := lookups.get(item_id):
            raise err
        return await get_sync_details(None)

    mass.music.albums.get_library_item_sync_details = AsyncMock(side_effect=sync_details_for)

    await provider.sync_library(MediaType.ALBUM)

    # all three were reached, and the two healthy ones were still added
    assert calls == list(ALBUM_IDS)
    assert _synced_album_ids(mass) == ["album_1", "album_3"]


async def test_item_stays_tracked_when_ancillary_work_fails() -> None:
    """
    An item whose favorite work fails is still recorded as seen.

    Its library row is committed regardless, so leaving it out of the id's would make it
    an orphan no later cleanup run could ever discover.
    """
    mass = _build_mass()
    provider = _build_provider(mass)
    provider.mark_favorite = True
    mass.music.albums.set_favorite = AsyncMock(side_effect=TypeError("bad favorite"))

    await provider.sync_library(MediaType.ALBUM)

    assert mass.music.albums.set_favorite.await_count == len(ALBUM_IDS)
    assert sorted(mass.cache.set.await_args.kwargs["data"]) == [1, 2, 3]


async def test_standalone_import_keeps_its_own_failure_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Each ad-hoc album-track import starts from a clean failure state.

    They are launched as their own task when an album is added, so a shared counter would
    silence every import after the first one had used up the logging budget.
    """
    mass = _build_mass()
    provider = _build_provider(mass)
    mass.music.tracks.get_library_item_sync_details = AsyncMock(return_value=None)
    mass.music.tracks.add_item_to_library = AsyncMock(side_effect=KeyError("bad track"))
    provider.album_tracks = [
        MagicMock(item_id=f"t{i}", uri=f"test://track/{i}") for i in range(MAX_LOGGED_SYNC_FAILURES)
    ]

    await asyncio.create_task(provider.import_album_tracks("album_1"))
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        await asyncio.create_task(provider.import_album_tracks("album_2"))

    # the second import reports its failures just like the first one did
    assert len(caplog.records) == MAX_LOGGED_SYNC_FAILURES


class _AuthSignal(Exception):
    """Stands in for a provider error a wrapper around sync_library has to act on."""


class AuthSignallingProvider(FailingAlbumProvider):
    """Provider that declares its auth signal unskippable."""

    @property
    def unskippable_sync_errors(self) -> tuple[type[Exception], ...]:
        """Return the errors a library sync must not swallow as an item failure."""
        return (_AuthSignal,)


async def test_declared_unskippable_error_is_not_swallowed() -> None:
    """
    An error the provider declared unskippable escapes instead of skipping the item.

    Providers use these to signal something their own wrapper must handle, such as an
    expired token that needs a reauthenticate and a retry.
    """
    mass = _build_mass()
    provider = _build_provider(mass, cls=AuthSignallingProvider)
    mass.music.albums.add_item_to_library = AsyncMock(side_effect=_AuthSignal("token expired"))

    with pytest.raises(_AuthSignal):
        await provider.sync_library(MediaType.ALBUM)

    # the run aborted, so nothing was recorded as a completed sync
    mass.cache.set.assert_not_called()


SKIPPED_ID = "album_2"


class SkippingAlbumProvider(FailingAlbumProvider):
    """Provider that drops one album while listing its library."""

    #: item id handed to report_skipped_sync_item, None to report an unidentifiable item
    reported_item_id: str | None = SKIPPED_ID

    async def get_library_albums(self) -> AsyncGenerator[Any]:
        """Yield the test albums, dropping the one that cannot be read."""
        async for album in super().get_library_albums():
            if album.item_id == SKIPPED_ID:
                self.report_skipped_sync_item(
                    MediaType.ALBUM, self.reported_item_id, InvalidDataError("no artist")
                )
                continue
            yield album


def _library_holds(mass: MagicMock, known: dict[str, int]) -> None:
    """Let the deletion pass resolve the given provider item id's to library db id's."""

    async def get_library_items_by_prov_id(
        provider_instance: str, provider_item_ids: list[str], limit: int
    ) -> list[Any]:
        del limit
        # only this provider instance's own mappings may resolve, never a sibling instance
        if provider_instance != "test--1":
            return []
        return [
            MagicMock(item_id=known[item_id]) for item_id in provider_item_ids if item_id in known
        ]

    mass.music.get_controller.return_value.get_library_items_by_prov_id = AsyncMock(
        side_effect=get_library_items_by_prov_id
    )


async def test_skipped_item_is_reported_on_the_sync_task() -> None:
    """An item the provider drops while listing is reported instead of vanishing silently."""
    mass = _build_mass()
    provider = _build_provider(mass, cls=SkippingAlbumProvider)
    _library_holds(mass, {})

    with patch("music_assistant.models.music_provider.report_current_task_failure") as reported:
        await provider.sync_library(MediaType.ALBUM)

    assert SKIPPED_ID in reported.call_args.args[0]
    # the rest of the library still synced
    assert _synced_album_ids(mass) == ["album_1", "album_3"]


async def test_skipped_item_we_already_hold_survives_the_deletion_pass() -> None:
    """
    A skipped item stays in the result set, while the rest of the deletions still run.

    The item is still in the provider's library, so it must not be read as removed - but
    a permanently unreadable item may not disable the cleanup for everything else either.
    """
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, cls=SkippingAlbumProvider)
    _library_holds(mass, {SKIPPED_ID: 2})

    await provider.sync_library(MediaType.ALBUM)

    controller = mass.music.get_controller.return_value
    # only the album that is really gone is processed, the skipped one is left alone
    controller.get_library_item.assert_awaited_once_with(99)
    assert sorted(mass.cache.set.await_args.kwargs["data"]) == [1, 2, 3]


async def test_skipped_item_we_do_not_hold_does_not_hold_back_deletions() -> None:
    """An item that was never imported cannot be deleted, so the cleanup runs as usual."""
    mass = _build_mass(prev_library_ids=[1, 3, 99])
    provider = _build_provider(mass, cls=SkippingAlbumProvider)
    _library_holds(mass, {})

    await provider.sync_library(MediaType.ALBUM)

    mass.music.get_controller.return_value.get_library_item.assert_awaited_once_with(99)


SKIPPED_TRACK_ID = "track_9"


class TrackSkippingAlbumProvider(FailingAlbumProvider):
    """Provider that drops tracks while listing the tracks of an album."""

    async def get_album_tracks(self, prov_album_id: str) -> list[Any]:
        """Report the tracks that could not be read instead of returning them."""
        del prov_album_id
        self.report_skipped_sync_item(
            MediaType.TRACK, SKIPPED_TRACK_ID, InvalidDataError("no artist")
        )
        self.report_skipped_sync_item(MediaType.TRACK, None, InvalidDataError("no id"))
        return []


class UnidentifiedSkipProvider(SkippingAlbumProvider):
    """Provider that drops an album it cannot identify."""

    reported_item_id = None


async def test_unidentified_skip_holds_back_deletions() -> None:
    """A provider that cannot say what it dropped holds back the deletions for the whole run."""
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, cls=UnidentifiedSkipProvider)
    _library_holds(mass, {})

    await provider.sync_library(MediaType.ALBUM)

    mass.music.get_controller.return_value.get_library_item.assert_not_called()


async def test_skipped_item_stays_in_the_snapshot_when_deletions_are_disabled() -> None:
    """
    A skipped item is recorded as seen even while deletions are off.

    That snapshot is what a later run compares against, so dropping the item here would
    leave it untracked once the user turns deletions back on.
    """
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, cls=SkippingAlbumProvider, sync_deletions=False)
    _library_holds(mass, {SKIPPED_ID: 2})

    await provider.sync_library(MediaType.ALBUM)

    assert sorted(mass.cache.set.await_args.kwargs["data"]) == [1, 2, 3]


async def test_skipped_items_resolve_against_this_provider_instance_only() -> None:
    """
    A skipped id is resolved against the instance that skipped it, not the whole domain.

    A second instance of the same provider holds its own mappings under the same item id's,
    and protecting one instance's item would leave the other's exposed.
    """
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, cls=SkippingAlbumProvider)
    _library_holds(mass, {SKIPPED_ID: 2})

    await provider.sync_library(MediaType.ALBUM)

    lookup = mass.music.get_controller.return_value.get_library_items_by_prov_id
    assert lookup.await_args.kwargs["provider_instance"] == provider.instance_id


async def test_a_skipped_track_does_not_reach_the_album_deletion_pass() -> None:
    """
    Skips are kept apart per media type.

    Importing an album's tracks runs inside the album sync, so a track it had to skip must
    not put a track db id into the album result set, nor hold back the album deletions.
    """
    mass = _build_mass(prev_library_ids=[1, 2, 3, 99])
    provider = _build_provider(mass, cls=TrackSkippingAlbumProvider, sync_album_tracks=True)
    _library_holds(mass, {SKIPPED_TRACK_ID: 77})

    await provider.sync_library(MediaType.ALBUM)

    # 77 is a track db id, so it may not end up in the album snapshot
    assert sorted(mass.cache.set.await_args.kwargs["data"]) == [1, 2, 3]
    # and a track that could not be named does not stop the albums from being cleaned up
    mass.music.get_controller.return_value.get_library_item.assert_awaited_once_with(99)


class UnskippableSkipProvider(SkippingAlbumProvider):
    """Provider that declares the error behind its skip unskippable."""

    @property
    def unskippable_sync_errors(self) -> tuple[type[Exception], ...]:
        """Return the errors a library sync must not swallow as an item failure."""
        return (InvalidDataError,)


async def test_reported_skip_respects_unskippable_errors() -> None:
    """Reporting a skip re-raises an error the provider declared unskippable."""
    mass = _build_mass(prev_library_ids=[1, 2, 3])
    provider = _build_provider(mass, cls=UnskippableSkipProvider)

    with pytest.raises(InvalidDataError):
        await provider.sync_library(MediaType.ALBUM)

    mass.cache.set.assert_not_called()
