"""Helpers for Audiobookshelf provider."""

import time
from dataclasses import dataclass, field

from aioaudiobookshelf.schema.media_progress import MediaProgress
from mashumaro.mixins.dict import DataClassDictMixin


@dataclass(kw_only=True)
class LibraryHelper(DataClassDictMixin):
    """Lib name + media items' uuids."""

    name: str
    item_ids: set[str] = field(default_factory=set)


@dataclass(kw_only=True)
class LibrariesHelper(DataClassDictMixin):
    """Helper class to store ABSLibrary name, id and the uuids of its media items.

    Dictionary is lib_id:AbsLibraryWithItemIDs.
    """

    audiobooks: dict[str, LibraryHelper] = field(default_factory=dict)
    podcasts: dict[str, LibraryHelper] = field(default_factory=dict)


@dataclass(kw_only=True)
class SessionHelper:
    """Helper class to store some session information."""

    abs_session_id: str
    last_sync_time: float


@dataclass(kw_only=True)
class _ProgressHelper:
    id_: str  # audiobook or podcast id
    episode_id: str | None = None
    last_update_ms: int  # last update in ms epoch (same as last_update in abs)


class ProgressGuard:
    """Class used to avoid ping pong between abs and mass.

    We continuously update the progress from mass to abs with the provider's on_played function.
    We also register callbacks for progress reports from abs to mass. This is not only triggered
    on external updates, but also on our own update. To avoid messages going back and forth, this
    class is used.
    """

    def __init__(self) -> None:
        """Init."""
        self._progresses: list[_ProgressHelper] = []
        self._max_progresses = 100
        # 12s have to have passed before we accept an external progress update
        # abs updates every 15 s
        self._min_time_between_updates_ms = 12000

    def _get_progress(self, item_id: str, episode_id: str | None = None) -> _ProgressHelper | None:
        """Get a helper progress."""
        for x in self._progresses:
            if x.id_ == item_id and x.episode_id == episode_id:
                return x
        return None

    def _remove_oldest(self) -> None:
        """Remove oldest helper progress."""
        progresses = sorted(self._progresses, key=lambda x: x.last_update_ms)
        if len(progresses) > 0:
            self._progresses.remove(progresses[0])

    def remove_progress(self, item_id: str, episode_id: str | None = None) -> None:
        """Remove a helper progress."""
        progress = self._get_progress(item_id=item_id, episode_id=episode_id)
        if progress is not None:
            self._progresses.remove(progress)

    def add_progress(self, item_id: str, episode_id: str | None = None) -> None:
        """Store a timestamp for the last update of an audiobook or podcast episode, mass ids."""
        if len(self._progresses) > self._max_progresses:
            self._remove_oldest()
        self.remove_progress(item_id=item_id, episode_id=episode_id)
        progress = _ProgressHelper(
            id_=item_id, episode_id=episode_id, last_update_ms=int(time.time() * 1000)
        )
        self._progresses.append(progress)

    def guard_ok_abs(self, abs_progress: MediaProgress) -> bool:
        """Check, if we may update against an abs media progress.

        The abs media progress has a property last_update_ms, which also reflects non
        mass external updates. Here, we compare this property against a potential
        stored one.
        """
        item_id = abs_progress.library_item_id
        episode_id = abs_progress.episode_id
        stored_progress = self._get_progress(item_id=item_id, episode_id=episode_id)
        if stored_progress is None:
            return True
        return bool(
            abs_progress.last_update - stored_progress.last_update_ms
            >= self._min_time_between_updates_ms
        )

    def guard_ok_mass(self, item_id: str, episode_id: str | None = None) -> bool:
        """Check, if we may update against a mass internal item.

        Here, we use the current time and compare it against the stored time.
        """
        stored_progress = self._get_progress(item_id=item_id, episode_id=episode_id)
        if stored_progress is None:
            return True
        return (
            int(time.time() * 1000) - stored_progress.last_update_ms
            >= self._min_time_between_updates_ms
        )
