"""Replay a feature recording through an effects engine and capture every rendered frame."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from hue_entertainment import LightChannel

if TYPE_CHECKING:
    from hue_entertainment import LightColorCommand

    from .recording import Recording

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE = (
    f"{REPO_ROOT / 'music_assistant/providers/hue_entertainment/analyzer.py'}:HueAudioAnalyzer"
)


class Engine(Protocol):
    """The analyzer interface the Hue bridge drives; any new engine must match it."""

    def apply_color_palette(self, update: dict[str, tuple[int, int, int] | None]) -> None:
        """Merge a palette update."""

    def apply_spectrum(self, bins: list[int], timestamp_us: int) -> None:
        """Queue a spectrum frame."""

    def apply_peak(self, strength: int, timestamp_us: int) -> None:
        """Queue an onset peak."""

    def push_beats(self, beats: list[Any]) -> None:
        """Append a beat schedule segment."""

    def clear_beat_schedule(self) -> None:
        """Drop the beat schedule."""

    def clear_beats(self) -> None:
        """Drop all queued state."""

    def render(self, now_us: int) -> list[LightColorCommand]:
        """Render every channel at server-clock time ``now_us``."""


@dataclass
class ReplayOptions:
    """Knobs for a replay run."""

    engine: str = DEFAULT_ENGINE
    mode: str | None = None
    brightness: int = 100
    fps: int = 30
    latency_us: int | None = None


def load_engine_class(spec: str) -> type[Any]:
    """
    Load an engine class from ``path/to/file.py:ClassName``.

    Loading by file path keeps the lab independent of the Music Assistant package
    import chain, so it runs with only ``hue-entertainment`` installed.

    :param spec: File path and class name separated by a colon.
    """
    file_part, _, class_name = spec.rpartition(":")
    path = Path(file_part).resolve()
    module_name = f"hue_lab_engine_{path.stem}_{abs(hash(path)) % 10_000}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        msg = f"Cannot load engine module from {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)
    return getattr(module, class_name)


def replay(rec: Recording, opts: ReplayOptions) -> dict[str, Any]:
    """
    Run a recording through an engine exactly as the bridge's render loop would.

    Events are delivered when the simulated server clock passes their arrival time
    (``rx``); every tick renders at ``now + latency`` like the live bridge does.

    :param rec: Recording to replay.
    :param opts: Engine and timing options.
    """
    channels = [
        LightChannel(
            channel_id=c["id"],
            service_id=str(c.get("service_id", c["id"])),
            name=c["name"],
            position=tuple(c["pos"]),
        )
        for c in rec.channels
    ]
    engine_cls = load_engine_class(opts.engine)
    kwargs: dict[str, Any] = {"channels": channels, "brightness": opts.brightness}
    if opts.mode:
        kwargs["color_mode"] = opts.mode
    engine: Engine = engine_cls(**kwargs)

    latency_us = (
        opts.latency_us if opts.latency_us is not None else rec.header.get("hue_latency_us", 0)
    )
    period_us = 1_000_000 // opts.fps
    events = rec.events
    playback = [e for e in events if "ts" in e]
    if not playback:
        msg = "Recording has no spectrum or peak events to replay"
        raise ValueError(msg)
    start_ts = min(e["ts"] for e in playback)
    end_ts = max(e["ts"] for e in playback)
    # Start the simulated clock early enough that the first frames see features
    # that were delivered ahead of the playhead.
    clock = start_ts - latency_us

    order = [c.channel_id for c in channels]
    frames: list[list[list[int]]] = []
    beats: list[list[float]] = []
    peaks: list[list[float]] = []
    energy: list[float] = []
    idx = 0

    while clock + latency_us <= end_ts:
        while idx < len(events) and events[idx]["rx"] <= clock:
            _deliver(engine, events[idx], start_ts, beats, peaks, energy)
            idx += 1
        commands = engine.render(clock + latency_us)
        by_id = {c.channel_id: c for c in commands}
        frames.append([_to_rgb8(by_id.get(cid)) for cid in order])
        clock += period_us

    return {
        "version": 1,
        "source": str(rec.path) if rec.path else rec.header["area"]["name"],
        "engine": opts.engine.rsplit("/", 1)[-1],
        "mode": opts.mode,
        "fps": opts.fps,
        "channels": rec.channels,
        "duration_s": len(frames) / opts.fps,
        "frames": frames,
        "beats": sorted(beats),
        "peaks": sorted(peaks),
        "energy": energy,
    }


def _deliver(
    engine: Engine,
    event: dict[str, Any],
    start_ts: int,
    beats: list[list[float]],
    peaks: list[list[float]],
    energy: list[float],
) -> None:
    kind = event["t"]
    if kind == "spectrum":
        engine.apply_spectrum(event["bins"], event["ts"])
        energy.append(round(sum(event["bins"]) / (65535 * max(1, len(event["bins"]))), 4))
    elif kind == "peak":
        engine.apply_peak(event["s"], event["ts"])
        peaks.append([_rel(event["ts"], start_ts), event["s"] / 255])
    elif kind == "beats":
        engine.push_beats(
            [SimpleNamespace(timestamp_us=ts, is_downbeat=down) for ts, down in event["beats"]]
        )
        beats.extend([_rel(ts, start_ts), 1.0 if down else 0.0] for ts, down in event["beats"])
    elif kind == "beats_clear":
        engine.clear_beat_schedule()
    elif kind == "stream_clear":
        engine.clear_beats()
    elif kind == "color":
        engine.apply_color_palette(
            {k: tuple(v) if v is not None else None for k, v in event["palette"].items()}
        )


def _rel(ts: int, start_ts: int) -> float:
    return round((ts - start_ts) / 1_000_000, 4)


def _to_rgb8(cmd: LightColorCommand | None) -> list[int]:
    if cmd is None:
        return [0, 0, 0]
    return [cmd.red >> 8, cmd.green >> 8, cmd.blue >> 8]
