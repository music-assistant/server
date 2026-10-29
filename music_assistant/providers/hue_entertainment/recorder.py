"""
Dev-only recorder for the visualizer features that drive the Hue analyzer.

Enabled by pointing the ``MASS_HUE_RECORD_DIR`` environment variable at a writable
directory. Each light session (DTLS stream up → down) becomes one JSON-lines file
holding the analyzer's inputs exactly as they arrived: spectrum bins, onset peaks,
beat schedules and palette updates, each stamped with the server-clock time it was
received (``rx``) and, where relevant, the playback time it belongs to (``ts``).

Only extracted features are written — never audio. Recordings are replayed offline
by ``scripts/hue_lab`` so effects can be tuned without a bridge or a speaker.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.models.visualizer import BeatTiming
    from hue_entertainment import EntertainmentArea

RECORD_DIR_ENV = "MASS_HUE_RECORD_DIR"
RECORDING_FORMAT_VERSION = 1

_STOP = object()
_PREROLL_MAX = 64


class FeatureRecorder:
    """Write one JSON-lines recording per light session, off the event loop."""

    def __init__(
        self,
        area: EntertainmentArea,
        clock_now_us: Callable[[], int],
        spectrum_config: dict[str, Any],
        record_dir: Path,
    ) -> None:
        """
        Initialize the recorder.

        :param area: Entertainment area whose channels and positions go in the header.
        :param clock_now_us: Server clock used to stamp arrival time of every event.
        :param spectrum_config: Spectrum request sent to Sendspin (bins, scale, range).
        :param record_dir: Directory recordings are written to.
        """
        self._area = area
        self._now_us = clock_now_us
        self._spectrum_config = spectrum_config
        self._dir = record_dir
        self._queue: queue.SimpleQueue[object] | None = None
        self._thread: threading.Thread | None = None
        # Beat schedules and palettes can land before the lights come up; keep the
        # ones still in effect so each recording starts with the full analyzer state.
        self._preroll: list[dict[str, Any]] = []

    @classmethod
    def from_env(
        cls,
        area: EntertainmentArea,
        clock_now_us: Callable[[], int],
        spectrum_config: dict[str, Any],
    ) -> FeatureRecorder | None:
        """
        Create a recorder when ``MASS_HUE_RECORD_DIR`` is set, otherwise return None.

        :param area: Entertainment area being recorded.
        :param clock_now_us: Server clock used to stamp arrival times.
        :param spectrum_config: Spectrum request sent to Sendspin.
        """
        if not (record_dir := os.environ.get(RECORD_DIR_ENV)):
            return None
        return cls(area, clock_now_us, spectrum_config, Path(record_dir))

    @property
    def is_recording(self) -> bool:
        """Return True while a session file is open."""
        return self._queue is not None

    def start_session(self, hue_latency_us: int) -> None:
        """
        Open a new recording file (no-op if one is already open).

        :param hue_latency_us: Render lead configured on the bridge, stored for replay.
        """
        if self._queue is not None:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", self._area.name.lower()).strip("-") or "area"
        path = self._dir / f"{stamp}-{slug}.jsonl"
        self._queue = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._writer, args=(path, self._queue), name="hue-recorder", daemon=True
        )
        self._thread.start()
        self._emit(
            "header",
            v=RECORDING_FORMAT_VERSION,
            area={"id": self._area.id, "name": self._area.name},
            channels=[
                {"id": c.channel_id, "name": c.name, "pos": list(c.position)}
                for c in self._area.channels
            ],
            spectrum=self._spectrum_config,
            hue_latency_us=hue_latency_us,
            wall_time=time.time(),
        )
        for item in self._preroll:
            self._queue.put(item)
        self._preroll.clear()

    def end_session(self) -> None:
        """Flush and close the current recording file."""
        if self._queue is None:
            return
        self._emit("session_end")
        self._queue.put(_STOP)
        self._queue = None
        self._thread = None

    def spectrum(self, bins: list[int], timestamp_us: int) -> None:
        """Record a spectrum frame."""
        self._emit("spectrum", ts=timestamp_us, bins=bins)

    def peak(self, strength: int, timestamp_us: int) -> None:
        """Record an onset peak."""
        self._emit("peak", ts=timestamp_us, s=strength)

    def beats(self, beats: list[BeatTiming]) -> None:
        """Record a beat schedule segment."""
        self._emit("beats", beats=[[b.timestamp_us, bool(b.is_downbeat)] for b in beats])

    def color(self, palette: dict[str, tuple[int, int, int] | None]) -> None:
        """Record a palette update."""
        self._emit(
            "color", palette={k: list(v) if v is not None else None for k, v in palette.items()}
        )

    def event(self, kind: str) -> None:
        """
        Record a bare lifecycle event.

        :param kind: One of beats_clear, stream_start, stream_clear, stream_end.
        """
        self._emit(kind)

    def _emit(self, kind: str, **fields: Any) -> None:
        item = {"t": kind, "rx": self._now_us(), **fields}
        if self._queue is not None:
            self._queue.put(item)
        elif kind == "beats_clear":
            self._preroll = [p for p in self._preroll if p["t"] != "beats"]
        elif kind in ("beats", "color"):
            self._preroll.append(item)
            del self._preroll[:-_PREROLL_MAX]

    @staticmethod
    def _writer(path: Path, q: queue.SimpleQueue[object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            while (item := q.get()) is not _STOP:
                fh.write(json.dumps(item, separators=(",", ":")))
                fh.write("\n")
