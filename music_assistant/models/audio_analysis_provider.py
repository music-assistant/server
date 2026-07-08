"""Model/base for an Audio Analysis Provider implementation."""

from __future__ import annotations

import asyncio
import functools
import sqlite3
import time
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from music_assistant.models.audio_analysis import AudioAnalysisError

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from typing import Any, Literal

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

_T = TypeVar("_T")

# DEBUG-log offloads that wait longer than this (seconds) for a permit -- time
# spent queued behind the concurrency cap rather than computing.
_PERMIT_WAIT_DEBUG_THRESHOLD = 0.5

# Providers that accumulate whole-track feature state (beat grids, per-chunk VQT, CLAP windows)
# cap analysis at this duration. Multi-hour mixes/audiobooks would hold hundreds of MB of
# feature state and tie up the scan for hours, and smart crossfade/similarity carry no meaning
# at that length. Providers opt in by setting max_analysis_duration.
ACCUMULATING_ANALYSIS_MAX_DURATION_SECONDS = 1800.0


# Subclass rather than wrap so existing isinstance(..., asyncio.Semaphore) checks keep
# working, and keep the counters here so callers read contention state without touching
# asyncio internals (_value/_waiters).
class InstrumentedSemaphore(asyncio.Semaphore):
    """asyncio.Semaphore that also exposes live in_flight/capacity/waiters counts."""

    def __init__(self, value: int) -> None:
        """Initialize with ``value`` permits."""
        super().__init__(value)
        self.capacity = value
        self.in_flight = 0
        self.waiters = 0

    async def acquire(self) -> Literal[True]:
        """Acquire a permit."""
        self.waiters += 1
        try:
            acquired = await super().acquire()
        finally:
            self.waiters -= 1
        self.in_flight += 1
        return acquired

    def release(self) -> None:
        """Release a permit."""
        self.in_flight -= 1
        super().release()


@dataclass
class AnalysisSessionData:
    """Base session data stored per analysis session."""

    streamdetails: StreamDetails
    audio_format: AudioFormat


class AudioAnalysisProvider(Provider):
    """
    Base representation of an Audio Analysis Provider.

    Receives PCM audio chunks during streaming and produces analysis results
    such as beat tracking, key detection, or loudness. The same hooks drive
    both live playback and background scans; providers do not need to know
    which context they are running in.
    """

    # Version of the analysis algorithm. Providers should increment this when
    # their algorithm changes significantly. The base class compares this against
    # the stored version to decide whether to re-analyze a track.
    analysis_version: int = 1

    # Maximum track duration (seconds) this provider will analyze; longer tracks are skipped by
    # start_analysis. None means no limit. Providers that accumulate whole-track state set it.
    max_analysis_duration: float | None = None

    # Whether this provider holds heavy ML models that can be unloaded while idle and reloaded
    # on demand. Providers that load such models set this True (see _load_models/_free_models).
    has_unloadable_models: bool = False

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize AudioAnalysisProvider."""
        super().__init__(mass, manifest, config, supported_features)
        self._sessions: dict[str, AnalysisSessionData] = {}
        # Serializes (re)loading of heavy models so concurrent session starts load them once.
        self._models_lock = asyncio.Lock()
        self._models_loaded = False

    async def start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """
        Start analysis for a new session.

        Returns True if the provider accepted the session.

        :param session_id: Session ID created by the AudioAnalysisController.
        :param streamdetails: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
        if (
            self.max_analysis_duration is not None
            and streamdetails.duration
            and streamdetails.duration > self.max_analysis_duration
        ):
            self.logger.debug(
                "Skipping analysis for %s: %.0fs exceeds the %.0fs limit",
                streamdetails.uri,
                streamdetails.duration,
                self.max_analysis_duration,
            )
            return False
        stored_version = await self.mass.streams.audio_analysis.get_audio_analysis_version(
            streamdetails.item_id,
            streamdetails.provider,
            self.domain,
            media_type=streamdetails.media_type,
        )
        if stored_version is not None and stored_version >= self.analysis_version:
            return False
        if self.has_unloadable_models and not await self.ensure_models_loaded():
            return False
        self._sessions[session_id] = AnalysisSessionData(
            streamdetails=streamdetails,
            audio_format=audio_format,
        )
        session = self._sessions[session_id]
        try:
            accepted = await self._start_analysis(session_id, streamdetails, audio_format)
        except AudioAnalysisError as err:
            await self._record_failure(session, err.reason, err.retry_at)
            self._sessions.pop(session_id, None)
            return False
        except asyncio.CancelledError:
            # Cancellation is not an analysis failure: let it propagate without recording.
            raise
        except Exception as err:
            # _start_analysis is provider-implemented (ffmpeg/torch/numpy decode); its
            # failure surface is open-ended, so the catch stays broad. Any unexpected
            # error is logged with a traceback and recorded as a failure.
            self.logger.error(
                "_start_analysis raised for session %s: %s", session_id, err, exc_info=err
            )
            await self._record_failure(session, str(err), None)
            self._sessions.pop(session_id, None)
            return False
        if not accepted:
            self._sessions.pop(session_id, None)
            return False
        return True

    @abstractmethod
    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """
        Process a PCM audio chunk.

        Implementations MUST `await` all chunk-processing work; the controller
        relies on this to backpressure the audio source.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """

    async def finalize(self, session_id: str) -> None:
        """Finalize analysis, persist the result, fire post_analysis, then clean up."""
        analysis: AudioAnalysisData | None = None
        session = self._sessions.get(session_id)
        try:
            analysis = await self._finalize(session_id)
        except AudioAnalysisError as err:
            if session is not None:
                await self._record_failure(session, err.reason, err.retry_at)
        except asyncio.CancelledError:
            # Cancellation is not an analysis failure: let it propagate without recording.
            raise
        except Exception as err:
            # _finalize is provider-implemented (torch/ffmpeg inference); its failure
            # surface is open-ended, so the catch stays broad — logged and recorded.
            self.logger.error("_finalize raised for session %s: %s", session_id, err, exc_info=err)
            if session is not None:
                await self._record_failure(session, str(err), None)
        if analysis is not None and session is not None:
            try:
                await self.mass.streams.audio_analysis.set_audio_analysis(
                    item_id=session.streamdetails.item_id,
                    provider_instance_id_or_domain=session.streamdetails.provider,
                    aa_provider_domain=self.domain,
                    analysis=analysis,
                    analysis_version=self.analysis_version,
                    media_type=session.streamdetails.media_type,
                )
            except Exception as err:
                # Persisting (DB write + provider lookup) must never break session
                # cleanup below, so the catch stays broad — logged and skipped.
                self.logger.warning(
                    "set_audio_analysis raised for %s: %s", self.domain, err, exc_info=err
                )
            else:
                try:
                    await self.post_analysis(session.streamdetails, analysis)
                except Exception as err:
                    # post_analysis is a provider-implemented hook with an open-ended
                    # failure surface; a failing side effect must not break cleanup.
                    self.logger.warning(
                        "post_analysis raised for %s: %s", self.domain, err, exc_info=err
                    )
        self._sessions.pop(session_id, None)

    async def post_analysis(
        self,
        streamdetails: StreamDetails,
        analysis: AudioAnalysisData,
    ) -> None:
        """
        Run side effects after analysis is finalized and persisted.

        Default is a no-op. Implementations MUST self-gate on whether
        `streamdetails.path` is a writable filesystem path, since this hook
        fires for both live and background-scan analyses.

        :param streamdetails: The stream details for the analyzed item.
        :param analysis: The analysis data that was persisted by `_finalize`.
        """
        return

    async def cancel(self, session_id: str) -> None:
        """Cancel an in-progress analysis session."""
        self._sessions.pop(session_id, None)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload, cancelling any active analysis sessions and freeing models."""
        for session_id in list(self._sessions):
            await self.cancel(session_id)
        async with self._models_lock:
            self._free_models()
            self._models_loaded = False
        await super().unload(is_removed)

    async def ensure_models_loaded(self) -> bool:
        """
        Load this provider's heavy models if they are not resident, returning success.

        Safe to call from concurrent sessions: the models are loaded once. Returns False
        when loading fails, so the caller can decline the session.
        """
        async with self._models_lock:
            if self._models_loaded:
                return True
            try:
                await self._load_models()
            except Exception as err:
                self.logger.error("Failed to load analysis models: %s", err, exc_info=err)
                return False
            self._models_loaded = True
            return True

    async def unload_idle_models(self) -> None:
        """Free heavy models to reclaim memory while idle; the next analysis reloads them."""
        async with self._models_lock:
            if not self._models_loaded:
                return
            self._free_models()
            self._models_loaded = False
            self.logger.debug("Unloaded idle analysis models")

    @abstractmethod
    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """
        Provider-specific initialization for a new analysis session.

        Return False to reject the session (e.g. unsupported format).

        :param session_id: The analysis session ID.
        :param streamdetails: The stream details for the item being analyzed.
        :param audio_format: PCM format of the audio stream.
        """

    @abstractmethod
    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """
        Compute and return the analysis for this session (or None to skip).

        The base class persists the returned value via set_audio_analysis() and
        then fires post_analysis(). Return None to skip both.

        :param session_id: The analysis session ID.
        """

    async def _load_models(self) -> None:
        """Load heavy models into memory. Override when has_unloadable_models is True."""
        return

    def _free_models(self) -> None:
        """Drop references to heavy models so memory can be reclaimed. Override as needed."""
        return

    async def _run_offloaded(self, func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """
        Run a blocking analysis function on the niced analysis thread pool.

        Route all CPU-heavy analysis work through this. The controller's semaphore caps
        concurrency, and while any player is streaming the solo lock holds analysis to one
        offload at a time so it stays below playback. Falls back to asyncio.to_thread when the
        controller has no pool configured.

        The slot (semaphore permit and solo lock) is held until the worker thread finishes,
        even if the awaiting coroutine is cancelled: the thread keeps running, so it must keep
        counting against the caps.

        :param func: The blocking callable to run off the event loop.
        :param args: Positional arguments passed to func.
        :param kwargs: Keyword arguments passed to func.
        """
        controller = self.mass.streams.audio_analysis
        semaphore = controller.analysis_semaphore
        if not isinstance(semaphore, asyncio.Semaphore):
            return await asyncio.to_thread(func, *args, **kwargs)

        # While a player streams, take the solo lock so analysis runs one offload at a time:
        # the GIL serializes Python execution, so concurrent inference starves the playback
        # loop. Idle, the semaphore alone caps concurrency and background work uses spare cores.
        solo = getattr(controller, "analysis_solo_lock", None)
        solo_lock = solo if isinstance(solo, asyncio.Lock) else None
        take_solo = False
        if solo_lock is not None:
            try:
                take_solo = bool(controller.playback_active())
            except Exception:
                take_solo = False

        solo_held = False

        def _release(done: asyncio.Future[_T]) -> None:
            if solo_held and solo_lock is not None:
                solo_lock.release()
            semaphore.release()
            # Retrieve any exception so a cancelled awaiter doesn't leave it unretrieved.
            if not done.cancelled():
                done.exception()

        wait_start = time.monotonic()
        await semaphore.acquire()
        try:
            if take_solo and solo_lock is not None:
                await solo_lock.acquire()
                solo_held = True
        except BaseException:
            # Cancelled (or failed) waiting for the solo lock — give the permit back.
            semaphore.release()
            raise
        wait_seconds = time.monotonic() - wait_start
        if wait_seconds >= _PERMIT_WAIT_DEBUG_THRESHOLD and isinstance(
            semaphore, InstrumentedSemaphore
        ):
            self.logger.debug(
                "Analysis offload waited %.1fs for a slot (%d/%d permits in use, %d queued)",
                wait_seconds,
                semaphore.in_flight,
                semaphore.capacity,
                semaphore.waiters,
            )
        executor = getattr(controller, "analysis_executor", None)
        try:
            if isinstance(executor, ThreadPoolExecutor):
                loop = asyncio.get_running_loop()
                future: asyncio.Future[_T] = asyncio.ensure_future(
                    loop.run_in_executor(executor, functools.partial(func, *args, **kwargs))
                )
            else:
                future = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
        except RuntimeError:
            # Scheduling the worker failed (e.g. the loop is shutting down) — release the
            # permit we just took so it isn't leaked, which would shrink the cap over time.
            if solo_held and solo_lock is not None:
                solo_lock.release()
            semaphore.release()
            raise
        future.add_done_callback(_release)
        # shield: a cancelled awaiter leaves the thread and its slot held until the work finishes.
        return await asyncio.shield(future)

    async def _record_failure(
        self,
        session: AnalysisSessionData,
        reason: str,
        retry_at: datetime | None,
    ) -> None:
        """Record a failure for this session's track."""
        # Swallow DB write errors so a failed recorder write never breaks the session
        # lifecycle (record_analysis_failure only performs a sqlite insert).
        try:
            sd = session.streamdetails
            await self.mass.streams.audio_analysis.record_analysis_failure(
                item_id=sd.item_id,
                provider_instance_id_or_domain=sd.provider,
                aa_provider_domain=self.domain,
                reason=reason,
                retry_at=retry_at,
                analysis_version=self.analysis_version,
                media_type=sd.media_type,
            )
        except sqlite3.Error as err:
            self.logger.warning(
                "record_analysis_failure raised for %s: %s", self.domain, err, exc_info=err
            )
