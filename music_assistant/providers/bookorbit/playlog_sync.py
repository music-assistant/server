"""Apply BookOrbit audiobook progress into the Music Assistant playlog."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

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
    """
    Suppress inbound playlog updates shortly after MA reported progress outbound.

    Mirrors Audiobookshelf ProgressGuard cool-down so a poll cannot stomp a
    fresher MA→BookOrbit write that has not yet been reflected remotely.
    """

    min_interval_ms: int = 8_000
    _last_outbound_ms: dict[int, int] = field(default_factory=dict)

    def note_outbound(self, book_id: int) -> None:
        """Record that MA just wrote progress for ``book_id`` to BookOrbit."""
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


async def resolve_remote_progress_seconds(client: Any, book_id: int) -> tuple[bool, int] | None:
    """
    Resolve BookOrbit progress to (fully_played, seconds_played).

    :returns: Tuple when usable progress exists; ``None`` when playback-state and
        legacy audio-progress are both absent.
    :raises TypeError, ValueError, KeyError, InvalidDataError: When remote progress
        exists but cannot be converted (callers must treat as unknown, not absent).
    May also raise transport errors from the client.
    """
    state = await client.get_playback_state(book_id)
    if state:
        manifest = await client.get_audiobook_manifest(book_id)
        fully_played, position_ms, _ = parse_resume_position_from_playback_state(manifest, state)
        return fully_played, position_ms // 1000
    progress = await client.get_audio_progress(book_id)
    if not progress:
        return None
    manifest = await client.get_audiobook_manifest(book_id)
    book = await client.get_book(book_id)
    book = book_with_manifest_playback_order(book, manifest)
    fully_played, position_ms, _ = parse_resume_position(_audio_files(book), progress)
    return fully_played, position_ms // 1000


def _local_progress_matches(library_item: Any, *, fully_played: bool, seconds_played: int) -> bool:
    """Return True when the library item already reflects this remote progress."""
    local_fully = bool(getattr(library_item, "fully_played", False))
    resume_ms = getattr(library_item, "resume_position_ms", None)
    local_seconds = int(resume_ms // 1000) if isinstance(resume_ms, int) else 0
    return local_fully == fully_played and local_seconds == seconds_played


async def apply_book_progress(
    mass: Any,
    *,
    provider_instance_id: str,
    client: Any,
    book_id: int,
    guard: InboundProgressGuard,
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
        resolved = await resolve_remote_progress_seconds(client, book_id)
    except LoginFailed:
        raise
    except (ClientError, TimeoutError, OSError) as err:
        LOGGER.warning("BookOrbit progress fetch for %s failed: %s", book_id, err)
        return None
    except (TypeError, ValueError, KeyError, InvalidDataError) as err:
        LOGGER.warning("BookOrbit progress for %s is unusable: %s", book_id, err)
        return None
    if resolved is None or (not resolved[0] and resolved[1] <= 0):
        # Confirmed absent / zero: never clear during an outbound cool-down.
        if not guard.allow_inbound(book_id):
            return None
        return False
    fully_played, seconds_played = resolved
    if not fully_played and seconds_played < PLAYBACK_REPORT_INTERVAL_SECONDS:
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
    if _local_progress_matches(
        library_item, fully_played=fully_played, seconds_played=seconds_played
    ):
        return True
    # Recheck after the await: an outbound MA report may have started during lookup.
    if not guard.allow_inbound(book_id):
        return True
    await mass.music.mark_item_played(
        library_item,
        fully_played=fully_played,
        seconds_played=seconds_played,
        user_initiated=False,
        provider_instance_id=provider_instance_id,
    )
    return True


async def sync_playlog(
    *,
    mass: Any,
    provider_instance_id: str,
    client: Any,
    book_ids: Iterable[int],
    guard: InboundProgressGuard,
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
        # Recheck immediately before clearing: an outbound write may have started
        # after apply_book_progress decided the remote was empty.
        if not guard.allow_inbound(book_id):
            continue
        try:
            library_item = await mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id,
            )
            if library_item is None:
                continue
            await mass.music.mark_item_unplayed(
                library_item, provider_instance_id=provider_instance_id
            )
        except Exception:
            LOGGER.warning("BookOrbit playlog discard for book %s failed", item_id, exc_info=True)
