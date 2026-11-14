"""Apply BookOrbit audiobook progress into the Music Assistant playlog."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from aiohttp import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.constants import PLAYBACK_REPORT_INTERVAL_SECONDS
from music_assistant.providers.bookorbit.helpers import book_with_manifest_playback_order
from music_assistant.providers.bookorbit.parsers import _audio_files
from music_assistant.providers.bookorbit.progress import (
    parse_resume_position,
    parse_resume_position_from_playback_state,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class InboundProgressGuard:
    """Suppress inbound playlog updates during an outbound cool-down window."""

    min_interval_ms: int = 8_000
    _last_outbound_ms: dict[int, int] = field(default_factory=dict)

    def note_outbound(self, book_id: int) -> None:
        """Arm cool-down so inbound applies cannot race an outbound progress report."""
        now = self._now_ms()
        self._last_outbound_ms[book_id] = now
        # Drop expired stamps so long-lived providers do not retain every book id.
        cutoff = now - self.min_interval_ms
        stale = [key for key, stamped in self._last_outbound_ms.items() if stamped < cutoff]
        for key in stale:
            del self._last_outbound_ms[key]

    def allow_inbound(self, book_id: int) -> bool:
        """Return True when an inbound playlog apply for ``book_id`` is safe."""
        stamped = self._last_outbound_ms.get(book_id)
        if stamped is None:
            return True
        if self._now_ms() - stamped >= self.min_interval_ms:
            self._last_outbound_ms.pop(book_id, None)
            return True
        return False

    def _now_ms(self) -> int:
        return int(time.time() * 1000)


@dataclass
class PlaylogProgressMemory:
    """
    Last progress applied inbound or sent outbound, plus remote fingerprints.

    Used to skip no-op ``mark_item_played`` calls (which would refresh the playlog
    timestamp) and to skip manifest fetches when the remote payload is unchanged.
    """

    _progress: dict[int, tuple[bool, int]] = field(default_factory=dict)
    _remote_fp: dict[int, tuple[Any, ...]] = field(default_factory=dict)

    def note_progress(self, book_id: int, fully_played: bool, seconds_played: int) -> None:
        """Record progress that was applied to the playlog or sent to BookOrbit."""
        self._progress[book_id] = (fully_played, int(seconds_played))

    def matches_progress(self, book_id: int, fully_played: bool, seconds_played: int) -> bool:
        """Return True when ``book_id`` already has this progress recorded."""
        return self._progress.get(book_id) == (fully_played, int(seconds_played))

    def get_progress(self, book_id: int) -> tuple[bool, int] | None:
        """Return the last recorded progress for ``book_id``, if any."""
        return self._progress.get(book_id)

    def note_remote_fp(self, book_id: int, fingerprint: tuple[Any, ...]) -> None:
        """Record the remote playback-state / legacy-progress fingerprint."""
        self._remote_fp[book_id] = fingerprint

    def matches_remote_fp(self, book_id: int, fingerprint: tuple[Any, ...]) -> bool:
        """Return True when the remote payload fingerprint is unchanged."""
        return self._remote_fp.get(book_id) == fingerprint

    def clear(self, book_id: int) -> None:
        """Drop recorded progress and fingerprint for ``book_id``."""
        self._progress.pop(book_id, None)
        self._remote_fp.pop(book_id, None)

    def export_progress(self) -> dict[str, list[bool | int]]:
        """Serialize progress entries for ``mass.cache``."""
        return {
            str(book_id): [fully_played, seconds_played]
            for book_id, (fully_played, seconds_played) in self._progress.items()
        }

    def load_progress(self, raw: dict[str, Any]) -> None:
        """Restore progress entries previously exported to ``mass.cache``."""
        loaded: dict[int, tuple[bool, int]] = {}
        for key, value in raw.items():
            try:
                book_id = int(key)
                if (
                    not isinstance(value, list | tuple)
                    or len(value) != 2
                    or not isinstance(value[1], int | float)
                ):
                    continue
                loaded[book_id] = (bool(value[0]), int(value[1]))
            except TypeError, ValueError:
                continue
        self._progress = loaded


def _playback_state_fingerprint(state: dict[str, Any]) -> tuple[Any, ...]:
    """Fingerprint playback-state fields that affect absolute resume position."""
    return (
        "state",
        state.get("assetId"),
        state.get("positionMs"),
        state.get("percentage"),
        state.get("completed"),
    )


def _legacy_progress_fingerprint(progress: dict[str, Any]) -> tuple[Any, ...]:
    """Fingerprint legacy audio-progress fields that affect absolute resume position."""
    return (
        "legacy",
        progress.get("currentFileId"),
        progress.get("positionSeconds"),
        progress.get("percentage"),
    )


def _remote_has_progress(resolved: tuple[bool, int] | None) -> TypeGuard[tuple[bool, int]]:
    """Return True when remote progress is finished or has a positive position."""
    return resolved is not None and (resolved[0] or resolved[1] > 0)


async def resolve_remote_progress_seconds(
    client: Any,
    book_id: int,
    *,
    memory: PlaylogProgressMemory | None = None,
) -> tuple[bool, int] | None:
    """
    Resolve BookOrbit progress to (fully_played, seconds_played).

    When ``memory`` has a matching remote fingerprint and cached progress, returns
    that cached progress without fetching the manifest or book detail.

    :returns: Tuple when usable progress exists; ``None`` when playback-state and
        legacy audio-progress are both absent.
    :raises ValueError: When remote progress exists but cannot be converted.
    :raises InvalidDataError: When remote progress exists but cannot be converted.
    :raises ClientError: On BookOrbit HTTP client failures.
    :raises TimeoutError: On BookOrbit request timeouts.
    :raises LoginFailed: When re-authentication fails.
    """
    state = await client.get_playback_state(book_id)
    if state:
        fingerprint = _playback_state_fingerprint(state)
        if memory and memory.matches_remote_fp(book_id, fingerprint):
            cached = memory.get_progress(book_id)
            if cached is not None:
                return cached
        manifest = await client.get_audiobook_manifest(book_id)
        fully_played, position_ms, _ = parse_resume_position_from_playback_state(manifest, state)
        resolved = (fully_played, position_ms // 1000)
        if memory:
            memory.note_remote_fp(book_id, fingerprint)
        return resolved
    progress = await client.get_audio_progress(book_id)
    if not progress:
        if memory:
            memory.clear(book_id)
        return None
    fingerprint = _legacy_progress_fingerprint(progress)
    if memory and memory.matches_remote_fp(book_id, fingerprint):
        cached = memory.get_progress(book_id)
        if cached is not None:
            return cached
    manifest = await client.get_audiobook_manifest(book_id)
    book = await client.get_book(book_id)
    book = book_with_manifest_playback_order(book, manifest)
    fully_played, position_ms, _ = parse_resume_position(_audio_files(book), progress)
    resolved = (fully_played, position_ms // 1000)
    if memory:
        memory.note_remote_fp(book_id, fingerprint)
    return resolved


async def apply_book_progress(
    mass: Any,
    *,
    provider_instance_id: str,
    client: Any,
    book_id: int,
    guard: InboundProgressGuard,
    memory: PlaylogProgressMemory | None = None,
) -> bool | None:
    """
    Pull one book's BookOrbit progress into the MA playlog.

    :returns: True if BookOrbit still has progress for this book (even when the
        stamp was skipped because it is too short, guarded, or unchanged); False
        when both remote progress endpoints are empty (or zero-and-not-finished)
        and an inbound clear is allowed; None when the outcome is unknown
        (transport/conversion failure or outbound cool-down blocking a clear).
    :raises LoginFailed: When re-authentication fails; callers should abort the batch.
    """
    try:
        resolved = await resolve_remote_progress_seconds(client, book_id, memory=memory)
    except LoginFailed:
        raise
    except (ClientError, TimeoutError) as err:
        LOGGER.warning("BookOrbit progress fetch for %s failed: %s", book_id, err)
        return None
    except (ValueError, InvalidDataError) as err:
        LOGGER.warning("BookOrbit progress for %s is unusable: %s", book_id, err)
        return None
    if not _remote_has_progress(resolved):
        # Confirmed absent / zero: never clear during an outbound cool-down.
        if memory:
            memory.clear(book_id)
        if not guard.allow_inbound(book_id):
            return None
        return False
    fully_played, seconds_played = resolved
    if not fully_played and seconds_played < PLAYBACK_REPORT_INTERVAL_SECONDS:
        return True
    if memory and memory.matches_progress(book_id, fully_played, seconds_played):
        return True
    if not guard.allow_inbound(book_id):
        return True
    library_item = await mass.music.get_library_item_by_prov_id(
        media_type=MediaType.AUDIOBOOK,
        item_id=str(book_id),
        provider_instance_id_or_domain=provider_instance_id,
    )
    if library_item is None:
        return True
    # Recheck after the await: an outbound MA report may have started during lookup.
    if not guard.allow_inbound(book_id):
        return True
    if memory and memory.matches_progress(book_id, fully_played, seconds_played):
        return True
    await mass.music.mark_item_played(
        library_item,
        fully_played=fully_played,
        seconds_played=seconds_played,
        user_initiated=False,
        provider_instance_id=provider_instance_id,
    )
    if memory:
        memory.note_progress(book_id, fully_played, seconds_played)
    return True


async def _discard_cleared_playlog_entry(
    mass: Any,
    *,
    provider_instance_id: str,
    client: Any,
    item_id: str,
    book_id: int,
    guard: InboundProgressGuard,
    memory: PlaylogProgressMemory | None = None,
) -> bool:
    """
    Unplay one playlog entry when remote progress is still confirmed absent.

    :returns: False when authentication failed and the discard batch must abort;
        True when this entry was handled or skipped.
    """
    # Recheck before clearing: an outbound write may have started after apply
    # decided the remote was empty.
    if not guard.allow_inbound(book_id):
        return True
    try:
        library_item = await mass.music.get_library_item_by_prov_id(
            media_type=MediaType.AUDIOBOOK,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id,
        )
        if library_item is None:
            return True
        # Recheck after the await: outbound may have started during lookup.
        if not guard.allow_inbound(book_id):
            return True
        # Re-resolve: remote may have regained progress since the apply pass.
        try:
            resolved = await resolve_remote_progress_seconds(client, book_id, memory=memory)
        except LoginFailed as err:
            LOGGER.warning(
                "BookOrbit authentication failed during playlog discard; "
                "aborting remaining clears: %s",
                err,
            )
            return False
        except (ClientError, TimeoutError) as err:
            LOGGER.warning(
                "BookOrbit progress recheck for %s failed; skipping clear: %s",
                item_id,
                err,
            )
            return True
        except (ValueError, InvalidDataError) as err:
            LOGGER.warning(
                "BookOrbit progress recheck for %s is unusable; skipping clear: %s",
                item_id,
                err,
            )
            return True
        if _remote_has_progress(resolved):
            return True
        if not guard.allow_inbound(book_id):
            return True
        await mass.music.mark_item_unplayed(library_item, provider_instance_id=provider_instance_id)
        if memory:
            memory.clear(book_id)
    except Exception:
        LOGGER.warning("BookOrbit playlog discard for book %s failed", item_id, exc_info=True)
    return True


async def sync_playlog(
    *,
    mass: Any,
    provider_instance_id: str,
    client: Any,
    book_ids: Iterable[int],
    guard: InboundProgressGuard,
    memory: PlaylogProgressMemory | None = None,
    concurrency: int = 8,
) -> None:
    """Apply BookOrbit progress for ``book_ids`` and unplay cleared candidates."""
    candidates = sorted({int(book_id) for book_id in book_ids})
    if not candidates:
        return
    sem = asyncio.Semaphore(max(1, concurrency))
    remote_with_progress: set[str] = set()
    confirmed_absent: set[str] = set()
    auth_failed = False

    async def _one(book_id: int) -> None:
        nonlocal auth_failed
        async with sem:
            if auth_failed:
                return
            try:
                has_progress = await apply_book_progress(
                    mass,
                    provider_instance_id=provider_instance_id,
                    client=client,
                    book_id=book_id,
                    guard=guard,
                    memory=memory,
                )
            except LoginFailed as err:
                if not auth_failed:
                    auth_failed = True
                    LOGGER.warning(
                        "BookOrbit authentication failed during playlog sync; "
                        "aborting remaining books: %s",
                        err,
                    )
                return
            except Exception:
                LOGGER.exception("BookOrbit playlog sync for book %s failed", book_id)
                return
            if has_progress is True:
                remote_with_progress.add(str(book_id))
            elif has_progress is False:
                confirmed_absent.add(str(book_id))

    await asyncio.gather(*(_one(book_id) for book_id in candidates))

    if auth_failed:
        return

    playlog = await mass.music.get_playlog_provider_item_ids(provider_instance_id)
    for media_type, item_id in playlog:
        if media_type != MediaType.AUDIOBOOK or item_id not in confirmed_absent:
            continue
        if item_id in remote_with_progress:
            continue
        try:
            book_id = int(item_id)
        except ValueError:
            continue
        if not await _discard_cleared_playlog_entry(
            mass,
            provider_instance_id=provider_instance_id,
            client=client,
            item_id=item_id,
            book_id=book_id,
            guard=guard,
            memory=memory,
        ):
            return
