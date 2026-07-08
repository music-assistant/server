"""Lifecycle management for the shared airptpd PTP timing daemon."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant.helpers.process import AsyncProcess

from .helpers import get_airptpd_binary

if TYPE_CHECKING:
    import logging

# a run shorter than this is considered a startup failure
# (e.g. the PTP ports are already taken by another daemon)
HEALTHY_RUNTIME_SECS = 10
# give up after this many consecutive startup failures
MAX_STARTUP_FAILURES = 3
RESTART_DELAY_SECS = 5


class AirPtpDaemon:
    """
    Manage the shared airptpd daemon that provides PTP timing for AirPlay 2 players.

    A single daemon per host performs PTP (IEEE 1588) clock synchronization on
    UDP ports 319/320. The cliap2 stream processes attach to it through shared
    memory. Without a running daemon, AirPlay 2 playback falls back to NTP
    timing, which some devices (e.g. Samsung) do not support.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """
        Initialize the PTP daemon manager.

        :param logger: Parent logger to attach the daemon logs to.
        """
        self.logger = logger.getChild("airptpd")
        self._proc: AsyncProcess | None = None
        self._supervisor_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the PTP daemon (non-fatal: logs a warning if unavailable)."""
        try:
            binary = await get_airptpd_binary()
        except RuntimeError as err:
            self.logger.warning("%s - AirPlay 2 will fall back to NTP timing", err)
            return
        self._supervisor_task = asyncio.create_task(self._supervise(binary))

    async def stop(self) -> None:
        """Stop the PTP daemon and its supervisor."""
        if not self._supervisor_task:
            return
        self._supervisor_task.cancel()
        try:
            await self._supervisor_task
        except asyncio.CancelledError:
            # re-raise if stop() itself was cancelled instead of the supervisor
            if not self._supervisor_task.cancelled():
                raise
        except Exception:
            self.logger.exception("Unexpected error in PTP daemon supervisor")
        self._supervisor_task = None

    async def _supervise(self, binary: str) -> None:
        """Run the daemon and restart it when it exits unexpectedly."""
        startup_failures = 0
        while True:
            start_time = time.monotonic()
            returncode: int | None = None
            # airptpd must run in the foreground (-f): its daemonized mode
            # abandons the thread that refreshes the shared memory heartbeat,
            # causing cliap2 to consider the daemon stale and fall back to NTP.
            self._proc = proc = AsyncProcess([binary, "-f"], stderr=True, name="airptpd")
            try:
                await proc.start()
                self.logger.debug("PTP daemon started")
                async for line in proc.iter_stderr():
                    self.logger.debug(line)
                returncode = await proc.wait()
            except OSError as err:
                self.logger.error("Unable to start PTP daemon: %s", err)
            except Exception:
                self.logger.exception("Unexpected error while running PTP daemon")
            finally:
                try:
                    await proc.close()
                finally:
                    self._proc = None
            if time.monotonic() - start_time < HEALTHY_RUNTIME_SECS:
                startup_failures += 1
                if startup_failures >= MAX_STARTUP_FAILURES:
                    self.logger.warning(
                        "PTP daemon keeps exiting right after startup (exit code %s) - "
                        "giving up, AirPlay 2 will fall back to NTP timing",
                        returncode,
                    )
                    return
            else:
                startup_failures = 0
            self.logger.warning(
                "PTP daemon exited unexpectedly (exit code %s), restarting in %s seconds",
                returncode,
                RESTART_DELAY_SECS,
            )
            await asyncio.sleep(RESTART_DELAY_SECS)
