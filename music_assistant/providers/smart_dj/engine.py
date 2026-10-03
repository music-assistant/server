"""Smart DJ scoring, hard constraints, and bounded playlist optimization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SignalControl:
    """Explicit user control for one Smart DJ signal."""

    state: str = "soft"  # hard | soft | disabled
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class DJWeights:
    """Default soft-preference weights."""

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


@dataclass(frozen=True, slots=True)
class DJControls:
    """Complete user-control contract.

    Hard rules are never violated. Soft signals may affect ordering. Disabled
    signals contribute nothing. This object is intentionally serializable so
    the frontend can own the complete decision policy.
    """

    bpm: SignalControl = SignalControl()
    key: SignalControl = SignalControl()
    energy: SignalControl = SignalControl()
    danceability: SignalControl = SignalControl()
    loudness: SignalControl = SignalControl()
    genre: SignalControl = SignalControl()
    artist_spacing: SignalControl = SignalControl()
    momentum: SignalControl = SignalControl()

    bpm_min: float | None = None
    bpm_max: float | None = None
    max_bpm_jump: float | None = None
    key_relation: str = "compatible"  # compatible | same | any
    max_artist_repeat: int = 1
    instrumental: str = "any"  # any | prefer | required
    explicit: str = "allow"  # allow | exclude
    transition_bars: int = 8
    automix_enabled: bool = False
    smart_reorder_enabled: bool = True
    lookahead: int = 4
    transition_aggressiveness: float = 0.5
    required_ids: frozenset[str] = frozenset()
    excluded_ids: frozenset[str] = frozenset()
    fixed_ids: frozenset[str] = frozenset()
    end_track_id: str | None = None


MODES = {
    "ai_dj": DJMode("ai_dj", 0.08, 0.15, 0.45, DJWeights()),
    "party": DJMode(
        "party",
        0.12,
        0.05,
        0.75,
        DJWeights(bpm=0.28, key=0.18, energy=0.16, danceability=0.18, loudness=0.06,
                  genre=0.06, artist_spacing=0.04, momentum=0.04),
    ),
    "chill": DJMode(
        "chill",
        0.10,
        -0.15,
        0.65,
        DJWeights(bpm=0.22, key=0.18, energy=0.22, danceability=0.10, loudness=0.08,
                  genre=0.10, artist_spacing=0.05, momentum=0.05),
    ),
    "workout": DJMode(
        "workout",
        0.06,
        0.12,
        0.35,
        DJWeights(bpm=0.32, key=0.18, energy=0.20, danceability=0.16, loudness=0.05,
                  genre=0.03, artist_spacing=0.03, momentum=0.03),
    ),
    "custom": DJMode("custom", 0.08, 0.0, 0.50, DJWeights()),
}


def _norm_delta(a: Any, b: Any, scale: float) -> float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return 0.5
    return max(0.0, 1.0 - abs(float(a) - float(b)) / max(scale, 0.001))


def camelot_affinity(a: str | None, b: str | None) -> float:
    """Return a normalized Camelot compatibility score."""
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


def _hard_fail(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
    controls: DJControls,
    *,
    same_artist: bool = False,
) -> list[str]:
    """Return all hard-rule violations. An empty list means the candidate is legal."""
    violations: list[str] = []
    if not candidate:
        violations.append("missing analysis")

    if candidate.get("queue_item_id") in controls.excluded_ids:
        violations.append("excluded track")

    bpm = candidate.get("bpm")
    if isinstance(bpm, (int, float)):
        if controls.bpm_min is not None and bpm < controls.bpm_min:
            violations.append("below minimum BPM")
        if controls.bpm_max is not None and bpm > controls.bpm_max:
            violations.append("above maximum BPM")

    if current and controls.max_bpm_jump is not None:
        a, b = current.get("bpm"), candidate.get("bpm")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(b) - float(a)) > controls.max_bpm_jump:
                violations.append("maximum BPM jump exceeded")

    if controls.key_relation != "any" and current:
        a, b = current.get("camelot"), candidate.get("camelot")
        if a and b:
            affinity = camelot_affinity(a, b)
            if controls.key_relation == "same" and affinity < 1.0:
                violations.append("key must match")
            elif controls.key_relation == "compatible" and affinity <= 0.0:
                violations.append("incompatible key")

    if controls.instrumental == "required" and not candidate.get("instrumental"):
        violations.append("instrumental required")

    if controls.explicit == "exclude" and candidate.get("explicit"):
        violations.append("explicit track excluded")

    if controls.bpm.state == "hard" and current and bpm is not None:
        a = current.get("bpm")
        if isinstance(a, (int, float)) and abs(float(bpm) - float(a)) > float(a) * 0.08:
            violations.append("hard BPM compatibility")

    if controls.key.state == "hard" and current:
        a, b = current.get("camelot"), candidate.get("camelot")
        if a and b and camelot_affinity(a, b) <= 0.0:
            violations.append("hard key compatibility")

    if controls.energy.state == "hard" and current:
        a, b = current.get("energy"), candidate.get("energy")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(float(a) - float(b)) > 0.35:
            violations.append("hard energy compatibility")

    for field, label, scale in (
        ("danceability", "danceability", 0.45),
        ("loudness", "loudness", 8.0),
    ):
        control = getattr(controls, field)
        if control.state == "hard" and current:
            a, b = current.get(field), candidate.get(field)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(float(a) - float(b)) > scale:
                violations.append(f"hard {label} compatibility")

    if controls.genre.state == "hard" and current:
        if current.get("genre") and candidate.get("genre") and current["genre"] != candidate["genre"]:
            violations.append("hard genre compatibility")

    if controls.artist_spacing.state == "hard" and same_artist:
        violations.append("hard artist spacing")

    if same_artist and controls.max_artist_repeat <= 0:
        violations.append("artist repeat prohibited")

    return violations


def _signal_values(
    current: dict[str, Any],
    candidate: dict[str, Any],
    mode: DJMode,
    controls: DJControls,
    *,
    same_artist: bool,
) -> tuple[float, list[str]]:
    """Calculate only enabled soft signals."""
    w = mode.weights
    values = {
        "bpm": _norm_delta(
            current.get("bpm"),
            candidate.get("bpm"),
            max(1.0, float(current.get("bpm") or 120) * mode.bpm_tolerance),
        ),
        "key": camelot_affinity(current.get("camelot"), candidate.get("camelot")),
        "energy": max(
            0.0,
            1.0
            - abs(
                float(candidate.get("energy") or 0.5)
                - (float(current.get("energy") or 0.5) + mode.energy_direction)
            )
            / 0.35,
        ),
        "danceability": _norm_delta(current.get("danceability"), candidate.get("danceability"), 0.45),
        "loudness": _norm_delta(current.get("loudness"), candidate.get("loudness"), 8.0),
        "genre": (
            1.0
            if current.get("genre") and candidate.get("genre") and current["genre"] == candidate["genre"]
            else 0.5
        ),
        "artist_spacing": 0.0 if same_artist else 1.0,
        "momentum": (
            1.0
            if float(candidate.get("energy") or 0.5) >= float(current.get("energy") or 0.5)
            else 0.65
        ),
    }
    reasons: list[str] = []
    total = 0.0
    total_weight = 0.0
    for name, value in values.items():
        control = getattr(controls, name)
        if control.state != "soft":
            continue
        base_weight = getattr(w, name)
        effective = max(0.0, control.weight) * base_weight
        total += value * effective
        total_weight += effective
        if value >= 0.85:
            reasons.append(f"{name.replace('_', ' ')} strong")
    if controls.instrumental == "prefer" and candidate.get("instrumental"):
        total += 0.10
        total_weight += 0.10
        reasons.append("instrumental preference")
    if total_weight == 0:
        return 0.5, reasons
    return max(0.0, min(1.0, total / total_weight)), reasons


def score_candidate(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
    mode: DJMode,
    controls: DJControls,
    *,
    same_artist: bool = False,
) -> tuple[float, list[str], list[str]]:
    """Score a candidate while explicitly reporting hard-rule violations."""
    if not candidate:
        return 0.0, [], ["missing analysis"]
    violations = _hard_fail(current, candidate, controls, same_artist=same_artist)
    if violations:
        return 0.0, [], violations
    if current is None:
        return 0.5, ["no current-track anchor"], []
    score, reasons = _signal_values(current, candidate, mode, controls, same_artist=same_artist)
    return score, reasons, []


def track_score(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
    mode: DJMode,
    *,
    bpm_tolerance: float | None = None,
    energy_target: float | None = None,
    same_artist: bool = False,
) -> tuple[float, list[str]]:
    """Backward-compatible scoring entry point."""
    controls = DJControls(
        bpm=SignalControl("soft", 1.0),
        key=SignalControl("soft", 1.0),
    )
    if bpm_tolerance is not None:
        mode = DJMode(mode.name, bpm_tolerance, mode.energy_direction, mode.variety, mode.weights)
    score, reasons, _ = score_candidate(current, candidate, mode, controls, same_artist=same_artist)
    return score, reasons


def beam_optimize(
    tracks: list[dict[str, Any]],
    current: dict[str, Any] | None,
    mode: DJMode,
    *,
    controls: DJControls | None = None,
    beam_width: int = 12,
    fixed_ids: set[str] | None = None,
    excluded_ids: set[str] | None = None,
    artist_spacing: int = 1,
) -> list[dict[str, Any]]:
    """Near-global playlist optimization with hard constraints."""
    controls = controls or DJControls()
    fixed_ids = fixed_ids or set(controls.fixed_ids)
    excluded_ids = excluded_ids or set(controls.excluded_ids)
    pool = [
        t for t in tracks
        if t.get("queue_item_id") not in excluded_ids
        and t.get("queue_item_id") not in controls.excluded_ids
    ]
    fixed = [t for t in pool if t.get("queue_item_id") in fixed_ids or t.get("queue_item_id") in controls.fixed_ids]
    movable = [t for t in pool if t not in fixed]

    beams: list[tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = [(0.0, [], movable)]
    for _ in range(len(movable)):
        next_beams: list[tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = []
        for total, path, remaining in beams:
            anchor = path[-1].get("analysis") if path else current
            for candidate in remaining:
                same_artist = bool(
                    path
                    and candidate.get("artist")
                    and candidate.get("artist") == path[-1].get("artist")
                )
                score, reasons, violations = score_candidate(
                    anchor, candidate.get("analysis") or {}, mode, controls, same_artist=same_artist
                )
                if violations:
                    continue
                item = {**candidate, "score": score, "reasons": reasons}
                next_beams.append(
                    (
                        total + score,
                        path + [item],
                        [x for x in remaining if x is not candidate],
                    )
                )
        next_beams.sort(key=lambda x: x[0], reverse=True)
        beams = next_beams[:beam_width]
        if not beams:
            break

    best = beams[0][1] if beams else []

    # Preserve fixed tracks in their original relative positions.
    by_id = {t.get("queue_item_id"): t for t in fixed}
    for idx, original in enumerate(tracks):
        item_id = original.get("queue_item_id")
        if item_id in by_id:
            best.insert(
                min(idx, len(best)),
                {**by_id[item_id], "score": 1.0, "reasons": ["fixed track"]},
            )

    if controls.end_track_id:
        end = next((x for x in best if x.get("queue_item_id") == controls.end_track_id), None)
        if end is not None:
            best = [x for x in best if x.get("queue_item_id") != controls.end_track_id] + [end]

    return best
