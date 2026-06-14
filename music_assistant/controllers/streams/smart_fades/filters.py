"""Smart Fades - Audio filter implementations."""

import logging
from abc import ABC, abstractmethod


class Filter(ABC):
    """Abstract base class for audio filters."""

    output_fadeout_label: str
    output_fadein_label: str

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize filter base class."""
        self.logger = logger

    @abstractmethod
    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Apply the filter and return the FFmpeg filter strings."""


class GradualTimeStretchFilter(Filter):
    """Gradual tempo change using asendcmd + rubberband with S-curve steps."""

    output_fadeout_label: str = "fadeout_gradstretch"
    output_fadein_label: str = "fadein_unchanged"

    def __init__(self, logger: logging.Logger, tempo_steps: list[tuple[float, float]]) -> None:
        """Initialize with tempo steps from compute_gradual_tempo_steps."""
        super().__init__(logger)
        # each tempo step is a tuple of (timestamp, tempo_ratio)
        self.tempo_steps = tempo_steps

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Build FFmpeg filter string for gradual time stretching."""
        if not self.tempo_steps:
            self.output_fadeout_label = input_fadeout_label.strip("[]")
            self.output_fadein_label = input_fadein_label.strip("[]")
            return []

        cmd_parts = [f"{ts:.3f} rubberband@rb tempo {ratio:.6f}" for ts, ratio in self.tempo_steps]
        cmd_string = "; ".join(cmd_parts)
        initial_ratio = self.tempo_steps[0][1]

        return [
            f"{input_fadeout_label} asendcmd=c='{cmd_string}',"
            f"rubberband@rb=tempo={initial_ratio:.6f}"
            f":transients=crisp:detector=compound:pitchq=quality"
            f" [{self.output_fadeout_label}]",
            f"{input_fadein_label} acopy [{self.output_fadein_label}]",
        ]

    def __repr__(self) -> str:
        """Return string representation."""
        n = len(self.tempo_steps)
        start = self.tempo_steps[0][1] if self.tempo_steps else 1.0
        end = self.tempo_steps[-1][1] if self.tempo_steps else 1.0
        return f"GradualTimeStretch(steps={n}, {start:.4f}->{end:.4f})"


class FadeInTrimFilter(Filter):
    """Filter that trims incoming track to align with downbeats."""

    output_fadeout_label: str = "fadeout_beatalign"
    output_fadein_label: str = "fadein_beatalign"

    def __init__(self, logger: logging.Logger, fadein_start_pos: float):
        """
        Initialize beat align filter.

        :param fadein_start_pos: Position in seconds to trim the incoming track to.
        """
        self.fadein_start_pos = fadein_start_pos
        super().__init__(logger)

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Trim the incoming track to align with downbeats."""
        return [
            f"{input_fadeout_label}anull[{self.output_fadeout_label}]",  # codespell:ignore anull
            f"{input_fadein_label}atrim=start={self.fadein_start_pos},asetpts=PTS-STARTPTS[{self.output_fadein_label}]",
        ]

    def __repr__(self) -> str:
        """Return string representation of FadeInTrimFilter."""
        return f"FadeInTrim(start={self.fadein_start_pos:.2f}s)"


class FadeOutTrimFilter(Filter):
    """Filter that trims trailing (silent) audio off the outgoing track's tail."""

    output_fadeout_label: str = "fadeout_tailtrim"
    output_fadein_label: str = "fadein_tailtrim"

    def __init__(self, logger: logging.Logger, fadeout_end_pos: float, trimmed_seconds: float):
        """
        Initialize fade-out trim filter.

        :param fadeout_end_pos: Position in seconds where the outgoing track's
            audible content ends; everything after it is dropped.
            Measured on the untrimmed input timeline, so this filter must precede
            any time-stretching filter in the chain.
        :param trimmed_seconds: Amount of trailing audio in seconds that the trim
            drops, for logging/debugging purposes.
        """
        self.fadeout_end_pos = fadeout_end_pos
        self.trimmed_seconds = trimmed_seconds
        super().__init__(logger)

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Trim the outgoing track's tail at the effective audio end."""
        return [
            f"{input_fadeout_label}atrim=end={self.fadeout_end_pos:.3f},"
            f"asetpts=PTS-STARTPTS[{self.output_fadeout_label}]",
            f"{input_fadein_label}anull[{self.output_fadein_label}]",  # codespell:ignore anull
        ]

    def __repr__(self) -> str:
        """Return string representation of FadeOutTrimFilter."""
        return f"FadeOutTrim(end={self.fadeout_end_pos:.2f}s, trimmed={self.trimmed_seconds:.2f}s)"


class FrequencySweepFilter(Filter):
    """Filter that sweeps a lowpass/highpass cutoff frequency over time."""

    output_fadeout_label: str = "frequency_sweep"
    output_fadein_label: str = "frequency_sweep"

    # cutoff at which the filter is perceptually transparent
    lowpass_open_freq: int = 20000
    highpass_open_freq: int = 20
    # seconds between cutoff updates; small enough to be perceived as continuous
    step_interval: float = 0.1

    def __init__(
        self,
        logger: logging.Logger,
        sweep_type: str,
        target_freq: int,
        duration: float,
        start_time: float,
        sweep_direction: str,
        poles: int,
        curve_type: str,
        stream_type: str = "fadeout",
    ):
        """
        Initialize frequency sweep filter.

        :param sweep_type: 'lowpass' or 'highpass'.
        :param target_freq: Target cutoff frequency for the filter.
        :param duration: Duration of the sweep in seconds.
        :param start_time: When to start the sweep.
        :param sweep_direction: 'fade_in' (open -> target) or 'fade_out' (target -> open).
        :param poles: Number of poles for the filter.
        :param curve_type: 'linear', 'exponential', or 'logarithmic'.
        :param stream_type: 'fadeout' or 'fadein' - which stream to process.
        """
        self.sweep_type = sweep_type
        self.target_freq = target_freq
        self.duration = duration
        self.start_time = start_time
        self.sweep_direction = sweep_direction
        self.poles = poles
        self.curve_type = curve_type
        self.stream_type = stream_type

        # Set output labels based on stream type
        if stream_type == "fadeout":
            self.output_fadeout_label = f"fadeout_{sweep_type}"
            self.output_fadein_label = "fadein_passthrough"
        else:
            self.output_fadeout_label = "fadeout_passthrough"
            self.output_fadein_label = f"fadein_{sweep_type}"

        super().__init__(logger)

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Generate FFmpeg filters for the frequency sweep effect."""
        # Select the correct input based on stream type
        if self.stream_type == "fadeout":
            input_label = input_fadeout_label
            output_label = self.output_fadeout_label
            passthrough_label = self.output_fadein_label
            passthrough_input = input_fadein_label
        else:
            input_label = input_fadein_label
            output_label = self.output_fadein_label
            passthrough_label = self.output_fadeout_label
            passthrough_input = input_fadeout_label

        # instance name is scoped per stream type so both asendcmd command
        # sequences address their own filter only
        instance = f"{self.sweep_type}@sweep_{self.stream_type}"
        steps = self._compute_sweep_steps()
        cmd_string = "; ".join(f"{ts:.3f} {instance} f {freq:.1f}" for ts, freq in steps)
        initial_freq = steps[0][1]

        return [
            # Pass through the other stream unchanged
            f"{passthrough_input}anull[{passthrough_label}]",  # codespell:ignore anull
            f"{input_label}asendcmd=c='{cmd_string}',"
            f"{instance}=f={initial_freq:.1f}:poles={self.poles}[{output_label}]",
        ]

    def __repr__(self) -> str:
        """Return string representation of FrequencySweepFilter."""
        return f"FreqSweep({self.sweep_type}@{self.target_freq}Hz, poles={self.poles})"

    def _compute_sweep_steps(self) -> list[tuple[float, float]]:
        """Compute the (timestamp, cutoff frequency) steps of the sweep."""
        open_freq = (
            self.lowpass_open_freq if self.sweep_type == "lowpass" else self.highpass_open_freq
        )
        if self.sweep_direction == "fade_in":
            freq_start, freq_end = float(open_freq), float(self.target_freq)
        else:
            freq_start, freq_end = float(self.target_freq), float(open_freq)

        n_steps = max(2, int(self.duration / self.step_interval))
        steps: list[tuple[float, float]] = []
        for i in range(n_steps + 1):
            progress = i / n_steps
            # exponential delays the cutoff movement, logarithmic front-loads it
            # (same perceived pacing as the volume curves this sweep replaced)
            if self.curve_type == "exponential":
                progress = progress**2
            elif self.curve_type == "logarithmic":
                progress = progress**0.5
            timestamp = self.start_time + (i / n_steps) * self.duration
            # interpolate in log-frequency space so the sweep is perceptually linear
            freq = freq_start * (freq_end / freq_start) ** progress
            steps.append((timestamp, freq))
        return steps


class CrossfadeFilter(Filter):
    """Filter that applies the final crossfade between fadeout and fadein streams."""

    output_fadeout_label: str = "crossfade"
    output_fadein_label: str = "crossfade"

    def __init__(self, logger: logging.Logger, crossfade_duration: float):
        """Initialize crossfade filter."""
        self.crossfade_duration = crossfade_duration
        super().__init__(logger)

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Apply the acrossfade filter."""
        # equal-power qsin curves; the default tri/tri dips ~3dB mid-fade on uncorrelated material
        return [
            f"{input_fadeout_label}{input_fadein_label}"
            f"acrossfade=d={self.crossfade_duration}:c1=qsin:c2=qsin"
        ]

    def __repr__(self) -> str:
        """Return string representation of CrossfadeFilter."""
        return f"Crossfade(d={self.crossfade_duration:.1f}s)"
