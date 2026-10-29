"""
Hue lab command line.

Usage:
    python -m scripts.hue_lab synth -o synth.jsonl
    python -m scripts.hue_lab render recording.jsonl -o frames.json --mode energetic
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .recording import load_recording, synthesize, write_recording
from .replay import DEFAULT_ENGINE, ReplayOptions, replay

# ruff: noqa: T201


def main() -> None:
    """Parse arguments and run the requested lab command."""
    parser = argparse.ArgumentParser(prog="hue_lab", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    synth = sub.add_parser("synth", help="write a synthetic recording with a known structure")
    synth.add_argument("-o", "--out", type=Path, default=Path("synth.jsonl"))
    synth.add_argument("--bpm", type=float, default=124.0)
    synth.add_argument("--seed", type=int, default=7)

    render = sub.add_parser("render", help="replay a recording through an engine")
    render.add_argument("recording", type=Path)
    render.add_argument("-o", "--out", type=Path)
    render.add_argument("--engine", default=DEFAULT_ENGINE, help="path/to/file.py:ClassName")
    render.add_argument("--mode", help="color mode passed to the engine")
    render.add_argument("--brightness", type=int, default=100)
    render.add_argument("--fps", type=int, default=30)
    render.add_argument("--latency-ms", type=int, help="override the recorded Hue latency")

    args = parser.parse_args()
    if args.cmd == "synth":
        rec = synthesize(bpm=args.bpm, seed=args.seed)
        write_recording(rec, args.out)
        print(f"Wrote {len(rec.events)} events to {args.out}")
        return

    rec = load_recording(args.recording)
    opts = ReplayOptions(
        engine=args.engine,
        mode=args.mode,
        brightness=args.brightness,
        fps=args.fps,
        latency_us=args.latency_ms * 1000 if args.latency_ms is not None else None,
    )
    result = replay(rec, opts)
    out = args.out or args.recording.with_suffix(f".{opts.mode or 'default'}.frames.json")
    out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    print(
        f"Rendered {len(result['frames'])} frames ({result['duration_s']:.1f}s, "
        f"{len(result['channels'])} lights) to {out}"
    )


if __name__ == "__main__":
    main()
