"""Load feature recordings and generate synthetic ones for testing effects."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Synthetic layout: a playroom-ish ring of lights in Hue's -1..1 coordinate space
# (x = left/right, y = back/front, z = floor/ceiling).
_SYNTH_CHANNELS: list[dict[str, Any]] = [
    {"id": 0, "name": "Ceiling front left", "pos": [-0.7, 0.7, 1.0]},
    {"id": 1, "name": "Ceiling front right", "pos": [0.7, 0.7, 1.0]},
    {"id": 2, "name": "Ceiling back left", "pos": [-0.7, -0.7, 1.0]},
    {"id": 3, "name": "Ceiling back right", "pos": [0.7, -0.7, 1.0]},
    {"id": 4, "name": "Lamp left", "pos": [-1.0, 0.0, 0.0]},
    {"id": 5, "name": "Lamp right", "pos": [1.0, 0.0, 0.0]},
]

# (name, bars, energy 0-1, kick on?) — intro, build, drop, breakdown, drop, outro.
_SYNTH_SECTIONS: list[tuple[str, int, float, bool]] = [
    ("intro", 8, 0.25, False),
    ("build", 8, 0.55, True),
    ("drop", 16, 1.0, True),
    ("breakdown", 8, 0.35, False),
    ("drop", 16, 1.0, True),
    ("outro", 4, 0.2, False),
]


@dataclass
class Recording:
    """A parsed feature recording."""

    header: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None

    @property
    def channels(self) -> list[dict[str, Any]]:
        """Return the entertainment area channels from the header."""
        return list(self.header.get("channels", []))


def load_recording(path: Path) -> Recording:
    """
    Load a JSON-lines recording written by the provider's FeatureRecorder.

    :param path: Path to the .jsonl file.
    """
    header: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("t") == "header":
                header = item
            elif header is None:
                msg = f"{path}:{line_no}: event before header"
                raise ValueError(msg)
            else:
                events.append(item)
    if header is None:
        msg = f"{path}: no header line"
        raise ValueError(msg)
    events.sort(key=lambda e: e["rx"])
    return Recording(header=header, events=events, path=path)


def synthesize(bpm: float = 124.0, seed: int = 7, lead_s: float = 2.0) -> Recording:
    """
    Build a synthetic recording with a clear song structure.

    Features are delivered ``lead_s`` seconds ahead of their playback time, mimicking
    how Sendspin pushes buffered audio features before the playhead.

    :param bpm: Tempo of the synthetic track.
    :param seed: Random seed so runs are reproducible.
    :param lead_s: How far ahead of playback features arrive.
    """
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic noise
    beat_us = int(60_000_000 / bpm)
    frame_us = 1_000_000 // 30
    lead_us = int(lead_s * 1_000_000)
    t0 = 10_000_000
    n_bins = 17

    events: list[dict[str, Any]] = [
        {"t": "stream_start", "rx": t0 - lead_us},
        {
            "t": "color",
            "rx": t0 - lead_us,
            "palette": {
                "primary": [232, 64, 120],
                "accent": [40, 180, 230],
                "on_dark": [250, 210, 90],
                "background_dark": [30, 20, 60],
            },
        },
    ]

    beats: list[tuple[int, bool, float, bool]] = []
    ts = t0
    for _name, bars, energy, kick in _SYNTH_SECTIONS:
        for bar in range(bars):
            for beat_in_bar in range(4):
                # Builds ramp up across the section rather than stepping.
                ramp = (bar + beat_in_bar / 4) / bars
                e = energy * (0.6 + 0.4 * ramp) if _name == "build" else energy
                beats.append((ts, beat_in_bar == 0, e, kick))
                ts += beat_us
    end_us = ts

    # The server pushes the beat schedule in chunks a few seconds ahead.
    chunk = 16
    for i in range(0, len(beats), chunk):
        part = beats[i : i + chunk]
        events.append(
            {"t": "beats", "rx": part[0][0] - lead_us, "beats": [[b[0], b[1]] for b in part]}
        )

    beat_idx = 0
    t = t0
    while t < end_us:
        while beat_idx + 1 < len(beats) and beats[beat_idx + 1][0] <= t:
            beat_idx += 1
        b_ts, _down, energy, kick = beats[beat_idx]
        since = (t - b_ts) / beat_us
        kick_env = math.exp(-since * 9) if kick else 0.0
        hat_env = math.exp(-((since * 2) % 1) * 14) * energy
        bins = []
        for k in range(n_bins):
            if k <= 2:
                v = 0.25 * energy + 0.75 * kick_env * energy
            elif k <= 8:
                v = 0.35 * energy + 0.15 * math.sin(t / 400_000 + k)
            else:
                v = 0.15 * energy + 0.5 * hat_env * (k / n_bins)
            v += rng.uniform(-0.04, 0.04)
            bins.append(max(0, min(65535, int(v * 65535))))
        events.append({"t": "spectrum", "rx": t - lead_us, "ts": t, "bins": bins})
        if kick and since * beat_us < frame_us:
            events.append({"t": "peak", "rx": t - lead_us, "ts": t, "s": int(150 + 100 * energy)})
        t += frame_us

    events.append({"t": "stream_end", "rx": end_us})
    events.sort(key=lambda e: e["rx"])
    header = {
        "t": "header",
        "rx": t0 - lead_us,
        "v": 1,
        "area": {"id": "synthetic", "name": f"Synthetic {bpm:g} bpm"},
        "channels": _SYNTH_CHANNELS,
        "spectrum": {"bins": n_bins, "scale": "mel", "f_min": 20, "f_max": 20000},
        "hue_latency_us": 20_000,
        "wall_time": 0,
        "synthetic": {"bpm": bpm, "t0_us": t0, "sections": _SYNTH_SECTIONS},
    }
    return Recording(header=header, events=events)


def write_recording(rec: Recording, path: Path) -> None:
    """
    Write a recording back out as JSON lines.

    :param rec: Recording to write.
    :param path: Destination .jsonl path.
    """
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec.header, separators=(",", ":")) + "\n")
        for event in rec.events:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
