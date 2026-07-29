"""
Distributed sequential strobe overlay for the Hue Lights Sync analyzer.

This is an *overlay*: the normal visualization mode (smooth / ambient / flashing
/ energetic) keeps running on every light. When an energetic moment is detected,
the strobe takes over the user-selected lights for a short burst, then hands them
back to the base mode ("return to preset").

Behaviour:
  * Only the lights ticked in the "Strobe lights" setting participate.
  * ``coverage`` is a HARD cap on how many of the selected lights may be lit in a
    single flash: k = max(1, round(n_selected * coverage / 100)).
  * Each flash picks a fresh random subset, avoiding the exact same set as the
    previous flash whenever more than k of the selected lights would be lit.
  * ``blackout`` controls the OFF phase: True = the selected lights go black
    between flashes (maximum contrast); False = they fall back to the base effect
    so the preset shows through and only the flashing subset blips.
  * Engagement is energy-gated by ``sensitivity`` (0-100).
  * Flash timing (Hz, duty) and the hysteresis (min-hold / release) are live
    parameters.

The overlay decides *which* channels it controls and *whether* they are on (1.0)
or off (0.0) this tick. The actual strobe colour and brightness cap are applied
by the analyzer, so the strobe stays independent of the base brightness.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import (
    CONF_STROBE_AUTO,
    CONF_STROBE_BEAT_SYNC,
    CONF_STROBE_BLACKOUT,
    CONF_STROBE_BRIGHTNESS,
    CONF_STROBE_COLOR,
    CONF_STROBE_COVERAGE,
    CONF_STROBE_DUTY,
    CONF_STROBE_ENABLED,
    CONF_STROBE_FLASH_HZ,
    CONF_STROBE_MIN_HOLD_MS,
    CONF_STROBE_RELEASE_MS,
    CONF_STROBE_SENSITIVITY,
    DEFAULT_STROBE_AUTO,
    DEFAULT_STROBE_BEAT_SYNC,
    DEFAULT_STROBE_BLACKOUT,
    DEFAULT_STROBE_BRIGHTNESS,
    DEFAULT_STROBE_COLOR,
    DEFAULT_STROBE_COVERAGE,
    DEFAULT_STROBE_DUTY,
    DEFAULT_STROBE_ENABLED,
    DEFAULT_STROBE_FLASH_HZ,
    DEFAULT_STROBE_MIN_HOLD_MS,
    DEFAULT_STROBE_RELEASE_MS,
    DEFAULT_STROBE_SENSITIVITY,
)
from .structure import SECTION_DROP, SECTION_RISE, SECTION_SUSTAIN

if TYPE_CHECKING:
    from .structure import SectionState

# --- defaults (all overridable live via update_config) ---
_DEF_FLASH_HZ = 11.0
_DEF_DUTY = 0.30  # fraction of the flash period that is "on"
_DEF_MIN_HOLD_US = 300_000  # once engaged, stay for at least this long
_DEF_RELEASE_US = 250_000  # release after this long below threshold

# --- energy gate (relative, genre-agnostic) ---
_GATE_FAST_A = 0.35  # fast envelope follower coefficient
_GATE_SLOW_A = 0.02  # slow baseline coefficient (~seconds)
_GATE_MARGIN_MAX = 1.2  # at sensitivity 0: engage when fast > slow * 2.2
_GATE_MIN_ENERGY = 0.02  # never engage on near-silence

# --- auto strobe (structure-driven; ignores the energy gate) ---
_AUTO_RISE_MIN = 0.25  # start the build-strobe a bit earlier into the rise
# Coverage at the very start of a build. Low enough that a single light ticks first
# and more are recruited as the build grows (a club build lights up gradually).
_AUTO_RISE_COVERAGE_MIN = 20
_AUTO_SUSTAIN_ACCENT_BEATS = 6  # strobe only the drop LANDING (first ~1.5 bars of a
# sustain), then let the groove breathe - a club strobe punctuates, not runs forever.
_AUTO_DROP_SUB = 4.0  # 1/16-note flashes on the drop
_AUTO_SUSTAIN_SUB = 2.0  # 1/8-note flashes on a peak sustain
_AUTO_DROP_HZ = 12.0  # fallback rate (no BPM) on a drop
_AUTO_SUSTAIN_HZ = 8.0  # fallback rate (no BPM) on a sustain
_AUTO_HZ_CAP = 25.0


def _hex_to_rgb(value: object) -> tuple[float, float, float]:
    """Parse a '#RRGGBB' (or '#RGB') hex colour to an (r, g, b) tuple of 0-1 floats."""
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        return (
            int(text[0:2], 16) / 255.0,
            int(text[2:4], 16) / 255.0,
            int(text[4:6], 16) / 255.0,
        )
    except ValueError, IndexError:
        return (1.0, 1.0, 1.0)


@dataclass(slots=True)
class StrobeSettings:
    """All strobe parameters except the per-area light selection."""

    enabled: bool = DEFAULT_STROBE_ENABLED
    coverage: int = DEFAULT_STROBE_COVERAGE
    sensitivity: int = DEFAULT_STROBE_SENSITIVITY
    blackout: bool = DEFAULT_STROBE_BLACKOUT
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    brightness: int = DEFAULT_STROBE_BRIGHTNESS
    flash_hz: float = float(DEFAULT_STROBE_FLASH_HZ)
    duty: float = DEFAULT_STROBE_DUTY / 100.0
    min_hold_ms: int = DEFAULT_STROBE_MIN_HOLD_MS
    release_ms: int = DEFAULT_STROBE_RELEASE_MS
    beat_sync: bool = DEFAULT_STROBE_BEAT_SYNC
    auto: bool = DEFAULT_STROBE_AUTO

    @classmethod
    def from_config(cls, config: object) -> StrobeSettings:
        """Build a settings bundle from a provider config exposing ``get_value``."""
        get = config.get_value  # type: ignore[attr-defined]

        def _int(key: str, default: int) -> int:
            value = get(key)
            if value is None or value == "":
                return default
            try:
                return int(float(str(value)))
            except TypeError, ValueError:
                return default

        def _bool(key: str, default: bool) -> bool:
            value = get(key)
            return default if value is None else bool(value)

        return cls(
            enabled=_bool(CONF_STROBE_ENABLED, DEFAULT_STROBE_ENABLED),
            coverage=_int(CONF_STROBE_COVERAGE, DEFAULT_STROBE_COVERAGE),
            sensitivity=_int(CONF_STROBE_SENSITIVITY, DEFAULT_STROBE_SENSITIVITY),
            blackout=_bool(CONF_STROBE_BLACKOUT, DEFAULT_STROBE_BLACKOUT),
            color=_hex_to_rgb(get(CONF_STROBE_COLOR) or DEFAULT_STROBE_COLOR),
            brightness=_int(CONF_STROBE_BRIGHTNESS, DEFAULT_STROBE_BRIGHTNESS),
            # flash_hz is an integer config field; coerced via _int then widened (a
            # fractional Hz would be truncated, but the UI only offers whole numbers).
            flash_hz=float(_int(CONF_STROBE_FLASH_HZ, DEFAULT_STROBE_FLASH_HZ)),
            duty=_int(CONF_STROBE_DUTY, DEFAULT_STROBE_DUTY) / 100.0,
            min_hold_ms=_int(CONF_STROBE_MIN_HOLD_MS, DEFAULT_STROBE_MIN_HOLD_MS),
            release_ms=_int(CONF_STROBE_RELEASE_MS, DEFAULT_STROBE_RELEASE_MS),
            beat_sync=_bool(CONF_STROBE_BEAT_SYNC, DEFAULT_STROBE_BEAT_SYNC),
            auto=_bool(CONF_STROBE_AUTO, DEFAULT_STROBE_AUTO),
        )


class StrobeOverlay:
    """Decides, per render tick, which selected lights flash vs stay off/base."""

    def __init__(
        self,
        channel_order: list[int],
        selected_ids: object = (),
        settings: StrobeSettings | None = None,
    ) -> None:
        """Build the overlay; ``channel_order`` is every channel_id in render order."""
        self._order = list(channel_order)
        self._selected: list[int] = []
        self._coverage = DEFAULT_STROBE_COVERAGE
        self._sensitivity = DEFAULT_STROBE_SENSITIVITY
        self._k = 0
        self._enabled = DEFAULT_STROBE_ENABLED
        self._blackout = DEFAULT_STROBE_BLACKOUT
        self._flash_hz = _DEF_FLASH_HZ
        self._duty = _DEF_DUTY
        self._min_hold_us = _DEF_MIN_HOLD_US
        self._release_us = _DEF_RELEASE_US
        self._beat_sync = DEFAULT_STROBE_BEAT_SYNC
        self._auto = DEFAULT_STROBE_AUTO

        self._fast = 0.0
        self._slow = 0.0
        self._initialized = False
        self._engaged = False
        self._engaged_since_us = 0
        self._below_since_us: int | None = None
        self._cycle_start_us = 0
        self._cycle_index = -1
        self._current_subset: frozenset[int] = frozenset()
        self._last_subset: frozenset[int] = frozenset()

        if settings is not None:
            self.apply_settings(settings)
        self.update_config(selected_ids)

    # -- configuration --

    def update_config(  # noqa: PLR0913
        self,
        selected_ids: object = None,
        coverage: int | None = None,
        sensitivity: int | None = None,
        *,
        enabled: bool | None = None,
        blackout: bool | None = None,
        flash_hz: float | None = None,
        duty: float | None = None,
        min_hold_ms: int | None = None,
        release_ms: int | None = None,
        beat_sync: bool | None = None,
        auto: bool | None = None,
    ) -> None:
        """Update selection / coverage / sensitivity / flash params in place."""
        if isinstance(selected_ids, (list, tuple, set, frozenset)):
            sel = {int(c) for c in selected_ids}
            # keep render order, drop ids that aren't in this area
            self._selected = [cid for cid in self._order if cid in sel]
        if coverage is not None:
            self._coverage = max(0, min(100, int(coverage)))
        if sensitivity is not None:
            self._sensitivity = max(0, min(100, int(sensitivity)))
        if enabled is not None:
            self._enabled = bool(enabled)
        if blackout is not None:
            self._blackout = bool(blackout)
        if flash_hz is not None:
            self._flash_hz = max(1.0, min(25.0, float(flash_hz)))
        if duty is not None:
            self._duty = max(0.05, min(0.9, float(duty)))
        if min_hold_ms is not None:
            self._min_hold_us = max(0, int(min_hold_ms)) * 1000
        if release_ms is not None:
            self._release_us = max(0, int(release_ms)) * 1000
        if beat_sync is not None:
            self._beat_sync = bool(beat_sync)
        if auto is not None:
            self._auto = bool(auto)
        self._k = max(1, round(len(self._selected) * self._coverage / 100)) if self._selected else 0

    @property
    def configured(self) -> bool:
        """True if the strobe is enabled and at least one light is selected."""
        return self._enabled and self._k > 0

    def apply_settings(self, settings: StrobeSettings) -> None:
        """Apply a settings bundle (everything except colour/brightness)."""
        self.update_config(
            coverage=settings.coverage,
            sensitivity=settings.sensitivity,
            enabled=settings.enabled,
            blackout=settings.blackout,
            flash_hz=settings.flash_hz,
            duty=settings.duty,
            min_hold_ms=settings.min_hold_ms,
            release_ms=settings.release_ms,
            beat_sync=settings.beat_sync,
            auto=settings.auto,
        )

    # -- per-tick --

    def tick(
        self,
        now_us: int,
        energy: float,
        beat_period_us: int | None = None,
        beat_anchor_us: int | None = None,
        section: SectionState | None = None,
    ) -> dict[int, float] | None:
        """
        Return {channel_id: level} for the channels the strobe controls, or None.

        level is 1.0 (flashing this instant) or 0.0 (forced black). A channel that
        is selected but not in the dict shows the base effect (used in the off
        phase when blackout is disabled). None = strobe inactive this tick.

        :param beat_period_us: Current beat interval (for beat-sync); None if unknown.
        :param beat_anchor_us: Timestamp of the prior beat, used to phase-align flashes.
        :param section: Current musical section; drives engagement when auto is on.
        """
        if not self.configured:
            return None
        if not self._update_envelopes(energy):
            return None  # warm-start frame: nothing to decide from yet
        if self._auto and section is not None:
            if not self._auto_gate(now_us, section):
                return None
        else:
            self._update_gate(now_us)
            if not self._engaged:
                return None

        period_us, cycle_origin = self._flash_period(now_us, beat_period_us, beat_anchor_us)
        hit_us = min(period_us - 1, max(1, int(period_us * self._duty)))
        elapsed = now_us - cycle_origin
        cycle_index = elapsed // period_us
        if cycle_index != self._cycle_index:
            self._cycle_index = cycle_index
            self._pick_subset()

        on_phase = (elapsed % period_us) < hit_us
        # blackout: the strobe owns every selected light (dark between flashes).
        # otherwise it only owns the flashing subset and lets the rest show base.
        levels: dict[int, float] = dict.fromkeys(self._selected, 0.0) if self._blackout else {}
        if on_phase:
            for cid in self._current_subset:
                levels[cid] = 1.0
        return levels

    def _flash_period(
        self, now_us: int, beat_period_us: int | None, beat_anchor_us: int | None
    ) -> tuple[int, int]:
        """
        Return (flash period in us, cycle origin in us).

        Free-running at ``flash_hz`` by default. With beat-sync and a known beat,
        the period becomes the musical subdivision of the beat closest to the
        configured rate, phased to the prior beat so flashes land on the beat.
        """
        if self._beat_sync and beat_period_us and beat_period_us > 0:
            target = 1_000_000.0 / max(1.0, self._flash_hz)
            subdivisions = max(1, min(8, round(beat_period_us / target)))
            period = max(2, int(beat_period_us / subdivisions))
            origin = beat_anchor_us if beat_anchor_us is not None else self._cycle_start_us
            return period, origin
        return max(2, int(1_000_000 / self._flash_hz)), self._cycle_start_us

    # -- internals --

    def _update_envelopes(self, energy: float) -> bool:
        """
        Advance the fast/slow energy envelopes; both gates read them.

        :return: False on the warm-start frame, when there is no history to judge yet.
        """
        # Warm start: seed both envelopes from the first sample so a cold
        # baseline (0.0) doesn't make the opening of a track look like a jump.
        if not self._initialized:
            self._initialized = True
            self._fast = energy
            self._slow = energy
            return False
        self._fast += _GATE_FAST_A * (energy - self._fast)
        self._slow += _GATE_SLOW_A * (energy - self._slow)
        return True

    def _update_gate(self, now_us: int) -> None:
        if self._sensitivity >= 100:
            want = True
        else:
            margin = 1.0 + (1.0 - self._sensitivity / 100.0) * _GATE_MARGIN_MAX
            want = self._fast > max(_GATE_MIN_ENERGY, self._slow * margin)

        if want:
            if not self._engaged:
                self._engaged = True
                self._engaged_since_us = now_us
                self._cycle_start_us = now_us
                self._cycle_index = -1
            self._below_since_us = None
        elif self._engaged:
            # min-hold then debounce, so the burst doesn't stutter on/off
            if now_us - self._engaged_since_us < self._min_hold_us:
                return
            if self._below_since_us is None:
                self._below_since_us = now_us
            elif now_us - self._below_since_us >= self._release_us:
                self._engaged = False

    def _auto_gate(self, now_us: int, section: SectionState) -> bool:
        """
        Structure-driven engagement + flash rate for auto strobe.

        Engages on drops (hard 1/16-note flashes, every selected light), the back
        half of builds (accelerating 1/4 -> 1/16, recruiting more lights), and
        peak-energy sustains (gentle 1/8); silent in verses and breakdowns. The
        flash rate locks to the detected BPM, falling back to a fixed Hz when the
        tempo is unknown. Returns True when the strobe should flash this tick.

        Shares engagement/cycle state with the manual gate (``_engaged``, ``_k``,
        ``_flash_hz``, the cycle fields): the local ``_k`` override is undone by
        ``_restore_user_coverage`` when control hands back to the manual gate, and
        ``_below_since_us`` is owned by the manual gate and not touched here.
        """
        # Never strobe into silence. The section state lags the audio, so at the end of a
        # track it can still read DROP/SUSTAIN while the music has already stopped - and
        # flashing into the gap between tracks just looks broken. The manual gate has the
        # same floor; auto was missing it.
        if self._fast < _GATE_MIN_ENERGY:
            self._engaged = False
            self._cycle_index = -1
            self._restore_user_coverage()
            return False
        st = section.state
        if st == SECTION_DROP:
            sub, coverage, fallback_hz = _AUTO_DROP_SUB, 100, _AUTO_DROP_HZ
        elif st == SECTION_RISE and section.rise_progress >= _AUTO_RISE_MIN:
            sub = 1.0 + (_AUTO_DROP_SUB - 1.0) * section.rise_progress
            # Recruit lights one at a time as the build grows: at the start of the rise
            # a single light ticks, then 2, 3, ... and the whole room lands on the drop.
            # Normalised over the active part of the rise so the ramp always spans it.
            grown = (section.rise_progress - _AUTO_RISE_MIN) / max(1e-6, 1.0 - _AUTO_RISE_MIN)
            coverage = int(_AUTO_RISE_COVERAGE_MIN + (100 - _AUTO_RISE_COVERAGE_MIN) * grown)
            fallback_hz = 4.0 + 8.0 * section.rise_progress
        elif st == SECTION_SUSTAIN and section.beats_since_change < _AUTO_SUSTAIN_ACCENT_BEATS:
            # Accent the drop LANDING only (first ~1.5 bars of the sustain), then go
            # quiet so the groove breathes instead of strobing the whole loud stretch.
            sub, coverage, fallback_hz = _AUTO_SUSTAIN_SUB, 70, _AUTO_SUSTAIN_HZ
        else:
            # Release - but honour the min-hold so a momentary dip doesn't chatter.
            if self._engaged and now_us - self._engaged_since_us < self._min_hold_us:
                return True
            self._engaged = False
            self._cycle_index = -1
            self._restore_user_coverage()
            return False
        beat_hz = section.bpm / 60.0 if section.bpm > 0 else 0.0
        # Keep auto inside the same 1-25 Hz band the manual gate clamps to (at very
        # low BPM beat_hz * sub could otherwise dip below the 1 Hz floor).
        self._flash_hz = max(1.0, min(_AUTO_HZ_CAP, beat_hz * sub)) if beat_hz > 0 else fallback_hz
        # Auto coverage overrides _k locally without clobbering the user's _coverage.
        self._k = max(1, round(len(self._selected) * coverage / 100)) if self._selected else 0
        if not self._engaged:
            self._engaged = True
            self._engaged_since_us = now_us
            self._cycle_start_us = now_us
            self._cycle_index = -1
        return True

    def _restore_user_coverage(self) -> None:
        """Recompute _k from the user's configured coverage (after auto overrode it)."""
        self._k = max(1, round(len(self._selected) * self._coverage / 100)) if self._selected else 0

    def _pick_subset(self) -> None:
        if len(self._selected) <= self._k:
            self._current_subset = frozenset(self._selected)
            return
        subset = frozenset(random.sample(self._selected, self._k))
        for _ in range(8):  # avoid the exact same subset twice in a row
            if subset != self._last_subset:
                break
            subset = frozenset(random.sample(self._selected, self._k))
        self._current_subset = subset
        self._last_subset = subset
