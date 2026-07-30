"""VBAN stream stats reporting."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic


class VBANStatsReporter:
    """VBAN stream stats reporter."""

    def __init__(self, pcm_sample_size: int, report_interval: int = 30) -> None:
        """Initialize stats reporter."""
        self._pcm_sample_size = pcm_sample_size
        self._report_interval = report_interval
        self._handle: asyncio.TimerHandle | None = None
        self._loop = asyncio.get_running_loop()
        self._logger = logging.getLogger(__name__)
        self._active = False
        self._stream_id: str | None = None
        self._stream_details = ""
        self._last_report_time = 0.0
        self._packet_count = 0
        self._kbytes_count = 0.0

    def start(self, stream_id: str, stream_details: str) -> None:
        """Start stats."""
        self.cancel()
        self._active = True
        self._stream_id = stream_id
        self._stream_details = stream_details
        self._reset(stream_id, call_interval=5)

    def _reset(self, instance_id: str, call_interval: int | None = None) -> None:
        """Reset stats."""
        if not self._active or instance_id != self._stream_id:
            return
        self._last_report_time = monotonic()
        self._packet_count = 0
        self._kbytes_count = 0.0
        _interval = (
            call_interval
            if (call_interval and call_interval < self._report_interval)
            else self._report_interval
        )
        self._handle = self._loop.call_later(_interval, self._report_cb, instance_id)

    def update(self, instance_id: str, vban_bytes_len: int) -> None:
        """Update stats."""
        if not self._active or instance_id != self._stream_id:
            return
        self._packet_count += 1
        self._kbytes_count += vban_bytes_len / 1024

    def _report_cb(self, instance_id: str) -> None:
        """Report stats callback."""
        if not self._active or instance_id != self._stream_id:
            return
        try:
            _secs_since_last_report = monotonic() - self._last_report_time
            _avg_pkts = self._packet_count / _secs_since_last_report
            _avg_kbytes = self._kbytes_count / _secs_since_last_report
            _avg_pkt_length = (
                (self._kbytes_count / self._packet_count) * 1024 if self._packet_count else 0
            )
            _target_kbytes = self._pcm_sample_size / 1024
            _target_pkts = self._pcm_sample_size / _avg_pkt_length if _avg_pkt_length else 0
        except ZeroDivisionError:
            self._logger.exception("")
        else:
            self._logger.debug(
                "VBAN stream stats: avg pkts/s processed/target: %d/%d // avg kB/s processed/target: %d/%d // avg pkt length: %d bytes // Stream %s",
                _avg_pkts,
                _target_pkts,
                _avg_kbytes,
                _target_kbytes,
                _avg_pkt_length,
                self._stream_details,
            )
        finally:
            if self._active and instance_id == self._stream_id:
                self._reset(instance_id)

    def cancel(self, instance_id: str | None = None) -> None:
        """Cancel stats reporter."""
        if instance_id is not None and instance_id != self._stream_id:
            return
        self._active = False
        self._stream_id = None
        if self._handle:
            self._handle.cancel()
            self._handle = None
