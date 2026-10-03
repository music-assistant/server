"""Pure Smart DJ scoring, planning, and constraint logic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True, slots=True)
class DJWeights:
    bpm: float = 0.30
    key: float = 0.25
    energy: float = 0.15
    danceability: float = 0.10
    loudness: float = 0.05
    genre: float = 0.05
    artist_spacing: float = 0.05
    momentum: float = 0.05

@dataclass(frozen=True, slots=True)
class DJMode:
    name: str
    bpm_tolerance: float
    energy_direction: float
    variety: float
    weights: DJWeights

MODES = {
    "ai_dj": DJMode("ai_dj", 0.08, 0.15, 0.45, DJWeights()),
    "party": DJMode("party", 0.12, 0.05, 0.75, DJWeights(bpm=0.28, key=0.18, energy=0.16, danceability=0.18, loudness=0.06, genre=0.06, artist_spacing=0.04, momentum=0.04)),
    "chill": DJMode("chill", 0.10, -0.15, 0.65, DJWeights(bpm=0.22, key=0.18, energy=0.22, danceability=0.10, loudness=0.08, genre=0.10, artist_spacing=0.05, momentum=0.05)),
    "workout": DJMode("workout", 0.06, 0.12, 0.35, DJWeights(bpm=0.32, key=0.18, energy=0.20, danceability=0.16, loudness=0.05, genre=0.03, artist_spacing=0.03, momentum=0.03)),
    "custom": DJMode("custom", 0.08, 0.0, 0.50, DJWeights()),
}

def _norm_delta(a: Any, b: Any, scale: float) -> float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return 0.5
    return max(0.0, 1.0 - abs(float(a) - float(b)) / scale)

def camelot_affinity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.5
    if a == b:
        return 1.0
    try:
        na, nb = int(a[:-1]), int(b[:-1])
        ma, mb = a[-1].upper(), b[-1].upper()
    except (ValueError, TypeError):
        return 0.0
    if ma == mb and ((na - nb) % 12 in (1, 11)):
        return 0.85
    if na == nb and ma != mb:
        return 0.72
    if ((na - nb) % 12 in (1, 11)) and ma != mb:
        return 0.55
    return 0.0

def track_score(current: dict[str, Any] | None, candidate: dict[str, Any], mode: DJMode, *,
                bpm_tolerance: float | None = None, energy_target: float | None = None,
                same_artist: bool = False) -> tuple[float, list[str]]:
    if not candidate:
        return 0.0, ["No analysis available"]
    if current is None:
        return 0.5, ["No current-track analysis"]
    w = mode.weights
    reasons: list[str] = []
    bpm = _norm_delta(current.get("bpm"), candidate.get("bpm"), max(1.0, float(current.get("bpm") or 120) * (bpm_tolerance or mode.bpm_tolerance)))
    key = camelot_affinity(current.get("camelot"), candidate.get("camelot"))
    energy_target = float(energy_target if energy_target is not None else (float(current.get("energy") or 0.5) + mode.energy_direction))
    energy = max(0.0, 1.0 - abs(float(candidate.get("energy") or 0.5) - energy_target) / 0.35)
    dance = _norm_delta(current.get("danceability"), candidate.get("danceability"), 0.45)
    loud = _norm_delta(current.get("loudness"), candidate.get("loudness"), 8.0)
    genre = 1.0 if current.get("genre") and candidate.get("genre") and current["genre"] == candidate["genre"] else 0.5
    artist = 0.0 if same_artist else 1.0
    momentum = 1.0 if float(candidate.get("energy") or 0.5) >= float(current.get("energy") or 0.5) else 0.65
    values = {"BPM": bpm, "key": key, "energy": energy, "danceability": dance, "loudness": loud, "genre": genre, "artist spacing": artist, "momentum": momentum}
    score = sum(values[k] * getattr(w, k.replace(" ", "_")) for k in ("BPM", "key", "energy", "danceability", "loudness", "genre", "artist spacing", "momentum"))
    if bpm > 0.8: reasons.append("BPM-compatible")
    if key >= 0.85: reasons.append("harmonically compatible")
    if same_artist: reasons.append("same artist")
    if mode.energy_direction > 0 and candidate.get("energy") is not None and current.get("energy") is not None and candidate["energy"] >= current["energy"]:
        reasons.append("energy build")
    if mode.energy_direction < 0 and candidate.get("energy") is not None and current.get("energy") is not None and candidate["energy"] <= current["energy"]:
        reasons.append("energy decline")
    return round(max(0.0, min(1.0, score)), 4), reasons

def beam_optimize(tracks: list[dict[str, Any]], current: dict[str, Any] | None, mode: DJMode,
                  *, beam_width: int = 12, fixed_ids: set[str] | None = None,
                  excluded_ids: set[str] | None = None, artist_spacing: int = 1) -> list[dict[str, Any]]:
    """Near-global playlist optimization using bounded look-ahead beam search."""
    fixed_ids = fixed_ids or set()
    excluded_ids = excluded_ids or set()
    pool = [t for t in tracks if t.get("queue_item_id") not in excluded_ids]
    if not pool:
        return []
    fixed = [t for t in pool if t.get("queue_item_id") in fixed_ids]
    movable = [t for t in pool if t.get("queue_item_id") not in fixed_ids]
    beams: list[tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = [(0.0, [], movable)]
    for _ in range(len(movable)):
        next_beams = []
        for total, path, remaining in beams:
            anchor = path[-1].get("analysis") if path else current
            for candidate in remaining:
                same_artist = bool(path and candidate.get("artist") and candidate.get("artist") == path[-1].get("artist"))
                score, reasons = track_score(anchor, candidate.get("analysis") or {}, mode, same_artist=same_artist)
                penalty = 0.35 if same_artist and artist_spacing > 1 else 0.0
                item = {**candidate, "score": score, "reasons": reasons}
                next_beams.append((total + score - penalty, path + [item], [x for x in remaining if x is not candidate]))
        next_beams.sort(key=lambda x: x[0], reverse=True)
        beams = next_beams[:beam_width]
        if not beams:
            break
    best = beams[0][1] if beams else []
    # Fixed tracks are preserved and inserted at their original relative positions.
    if fixed:
        by_id = {t.get("queue_item_id"): t for t in fixed}
        for idx, original in enumerate(tracks):
            if original.get("queue_item_id") in by_id:
                best.insert(min(idx, len(best)), {**by_id[original["queue_item_id"]], "score": 1.0, "reasons": ["Fixed track"]})
    return best
