"""Helpers for Audiobookshelf provider."""

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mashumaro.mixins.dict import DataClassDictMixin

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.media_progress import MediaProgress


@dataclass(kw_only=True)
class LibraryHelper(DataClassDictMixin):
    """Lib name + media items' uuids."""

    name: str
    item_ids: set[str] = field(default_factory=set)


@dataclass(kw_only=True)
class NarratorHelper(DataClassDictMixin):
    """Store narrator's name and id."""

    id_: str
    name: str

    def __hash__(self) -> int:
        """Hash."""
        return hash(self.id_)


@dataclass(kw_only=True)
class LibrariesHelper(DataClassDictMixin):
    """
    Helper class to store ABSLibrary name, id and the uuids of its media items.

    Dictionary is lib_id:LibraryHelper or lib_id:set[playlist_ids/narrator_ids/author_ids].
    """

    audiobooks: dict[str, LibraryHelper] = field(default_factory=dict)
    podcasts: dict[str, LibraryHelper] = field(default_factory=dict)
    playlists_audiobooks: dict[str, set[str]] = field(default_factory=dict)
    playlists_podcasts: dict[str, set[str]] = field(default_factory=dict)
    authors: dict[str, set[str]] = field(default_factory=dict)
    narrators: dict[str, set[str]] = field(default_factory=dict)
    # audiobook_id is key. Abs does not have a dedicated narrator endpoint.
    audiobook_narrators: dict[str, set[NarratorHelper]] = field(default_factory=dict)


@dataclass(kw_only=True)
class SessionHelper:
    """Helper class to store some session information."""

    abs_session_id: str
    last_sync_time: float
    failed_sync_count: int = 0


@dataclass(kw_only=True)
class _ProgressHelper:
    id_: str  # audiobook or podcast id
    episode_id: str | None = None
    last_update_ms: int  # last update in ms epoch (same as last_update in abs)


class ProgressGuard:
    """
    Class used to avoid ping pong between abs and mass.

    We continuously update the progress from mass to abs with the provider's on_played function.
    We also register callbacks for progress reports from abs to mass. This is not only triggered
    on external updates, but also on our own update. To avoid messages going back and forth, this
    class is used.
    """

    def __init__(self, applied: dict[str, tuple[int, bool]] | None = None) -> None:
        """
        Init.

        :param applied: Persisted state of applied abs progresses, see applied_to_dict.
        """
        self._progresses: list[_ProgressHelper] = []
        self._max_progresses = 100
        # 8s have to have passed before we accept an external progress update
        # abs updates every 10 s
        self._min_time_between_updates_ms = 8000
        # mass item id: (abs last_update of the last seen progress, finished state)
        self._applied: dict[str, tuple[int, bool]] = applied or {}

    @classmethod
    def from_applied_dict(cls, data: dict[str, list[int | bool]]) -> ProgressGuard:
        """Create a guard from the persisted state of applied abs progresses."""
        return cls(applied={key: (int(x[0]), bool(x[1])) for key, x in data.items()})

    def applied_to_dict(self) -> dict[str, list[int | bool]]:
        """Return the state of applied abs progresses for persistence."""
        return {
            key: [last_update, finished] for key, (last_update, finished) in self._applied.items()
        }

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

    def add_abs_progress(self, abs_progress: MediaProgress) -> None:
        """Store an abs progress, which is applied to mass."""
        self.add_progress(abs_progress.library_item_id, abs_progress.episode_id)
        self._set_applied(abs_progress)

    def set_finished(self, item_id: str, episode_id: str | None, is_finished: bool) -> bool:
        """
        Store the finished state mass reported to abs. Returns True, if it changed.

        :param item_id: Abs library item id.
        :param episode_id: Abs episode id.
        :param is_finished: Finished state reported to abs.
        """
        key = _get_key(item_id, episode_id)
        last_update, finished = self._applied.get(key, (0, False))
        self._applied[key] = (last_update, is_finished)
        return finished != is_finished

    def guard_ok_abs(self, abs_progress: MediaProgress) -> bool:
        """
        Check, if we may update against an abs media progress.

        Progresses already applied, or a finished state already known to mass, are rejected.
        """
        if not self.guard_ok_mass(abs_progress.library_item_id, abs_progress.episode_id):
            # echo of our own update
            self._set_applied(abs_progress)
            return False
        key = _get_key(abs_progress.library_item_id, abs_progress.episode_id)
        if (applied := self._applied.get(key)) is None:
            return True
        last_update, finished = applied
        if abs_progress.last_update <= last_update:
            return False
        if finished and abs_progress.is_finished:
            self._set_applied(abs_progress)
            return False
        return True

    def guard_ok_mass(self, item_id: str, episode_id: str | None = None) -> bool:
        """
        Check, if we may update against a mass internal item.

        Here, we use the current time and compare it against the stored time.
        """
        stored_progress = self._get_progress(item_id=item_id, episode_id=episode_id)
        if stored_progress is None:
            return True
        return (
            int(time.time() * 1000) - stored_progress.last_update_ms
            >= self._min_time_between_updates_ms
        )

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

    def _set_applied(self, abs_progress: MediaProgress) -> None:
        key = _get_key(abs_progress.library_item_id, abs_progress.episode_id)
        self._applied[key] = (abs_progress.last_update, abs_progress.is_finished)


def _get_key(item_id: str, episode_id: str | None) -> str:
    return item_id if episode_id is None else f"{item_id} {episode_id}"
