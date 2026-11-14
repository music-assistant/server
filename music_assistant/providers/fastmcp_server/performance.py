"""Bounded in-memory latency samples for provider health diagnostics."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class PerformanceTracker:
    """Track a lifetime count and the latest 256 elapsed-time samples."""

    _samples: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    _count: int = 0

    def record(self, elapsed_ms: float) -> None:
        """Record one non-negative duration in milliseconds."""
        self._count += 1
        self._samples.append(max(0.0, float(elapsed_ms)))

    def summary(self) -> dict[str, int | float]:
        """Return deterministic nearest-rank percentile metrics."""
        values = sorted(self._samples)
        if not values:
            return {
                "count": self._count,
                "sample_count": 0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "max_ms": 0.0,
            }

        def percentile(value: float) -> float:
            index = max(0, math.ceil(value * len(values)) - 1)
            return round(values[index], 3)

        return {
            "count": self._count,
            "sample_count": len(values),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "max_ms": round(values[-1], 3),
        }
