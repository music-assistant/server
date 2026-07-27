"""
Regression test: a no-change library re-sync must be cheap.

The per-item sync loops resolve every provider item through the lightweight
``get_library_item_sync_details`` lookup, so an unchanged item must not be
hydrated into a full ``MediaItem`` (no mashumaro ``from_dict``) and must not
trigger any library writes (add/update/set_favorite).
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import patch

from music_assistant_models.media_items import Album, Artist, Audiobook, Podcast, Track

from tests.common import wait_for_sync_completion

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def _wait_until_sync_idle(mass: MusicAssistant, timeout: float = 60.0) -> None:
    """Wait until no provider sync tasks and no genre scan are pending or running."""
    elapsed = 0.0
    while elapsed < timeout:
        if not mass.music.active_sync_tasks and not mass.music.genres._genre_scan_running:
            return
        await asyncio.sleep(0.25)
        elapsed += 0.25
    raise TimeoutError("sync tasks did not become idle in time")


async def test_no_change_resync_is_hydration_free(e2e_mass: MusicAssistant) -> None:
    """A re-sync where nothing changed performs no writes and hydrates no media items."""
    mass = e2e_mass
    # the initial sync is auto-scheduled when the test provider loads;
    # wait for it (and the follow-up genre scan) to fully complete
    async with wait_for_sync_completion(mass):
        await mass.music.start_sync()
    await _wait_until_sync_idle(mass)

    counts_before = {
        ctrl.media_type: await ctrl.library_count()
        for ctrl in (
            mass.music.artists,
            mass.music.albums,
            mass.music.tracks,
            mass.music.podcasts,
            mass.music.audiobooks,
        )
    }
    assert counts_before[mass.music.tracks.media_type] > 0

    controllers = (
        mass.music.artists,
        mass.music.albums,
        mass.music.tracks,
        mass.music.podcasts,
        mass.music.audiobooks,
    )
    write_spies = {}
    from_dict_spies = {}
    with ExitStack() as stack:
        for ctrl in controllers:
            for method in ("add_item_to_library", "update_item_in_library", "set_favorite"):
                spy = stack.enter_context(patch.object(ctrl, method, wraps=getattr(ctrl, method)))
                write_spies[f"{ctrl.media_type.value}.{method}"] = spy
        for item_cls in (Track, Album, Artist, Audiobook):
            spy = stack.enter_context(
                patch.object(item_cls, "from_dict", side_effect=item_cls.from_dict)
            )
            from_dict_spies[item_cls.__name__] = spy
        # the podcast episodes precache legitimately hydrates each podcast once per
        # sync (pre-existing behavior), so podcasts are bounded rather than zero
        podcast_spy = stack.enter_context(
            patch.object(Podcast, "from_dict", side_effect=Podcast.from_dict)
        )

        async with wait_for_sync_completion(mass):
            await mass.music.start_sync()
        # keep the spies active until the follow-up genre scan has fully completed
        await _wait_until_sync_idle(mass)

    for name, spy in write_spies.items():
        assert not spy.called, f"unexpected library write during no-change re-sync: {name}"
    for name, spy in from_dict_spies.items():
        assert spy.call_count == 0, (
            f"{name}.from_dict called {spy.call_count}x during no-change re-sync"
        )
    assert podcast_spy.call_count <= counts_before[mass.music.podcasts.media_type]

    # the library contents must be unchanged
    for ctrl in controllers:
        assert await ctrl.library_count() == counts_before[ctrl.media_type]
