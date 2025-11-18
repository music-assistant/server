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


class TimeStretchFilter(Filter):
    """Filter that applies time stretching to match BPM using rubberband."""

    output_fadeout_label: str = "fadeout_stretched"
    output_fadein_label: str = "fadein_unchanged"

    def __init__(
        self,
        logger: logging.Logger,
        stretch_ratio: float,
    ):
        """Initialize time stretch filter."""
        self.stretch_ratio = stretch_ratio
        super().__init__(logger)

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Create FFmpeg filters to gradually adjust tempo from original BPM to target BPM."""
        return [
            f"{input_fadeout_label}rubberband=tempo={self.stretch_ratio:.6f}:transients=mixed:detector=soft:pitchq=quality"
            f"[{self.output_fadeout_label}]",
            f"{input_fadein_label}anull[{self.output_fadein_label}]",  # codespell:ignore anull
        ]

    def __repr__(self) -> str:
        """Return string representation of TimeStretchFilter."""
        return f"TimeStretch(ratio={self.stretch_ratio:.2f})"


class TrimFilter(Filter):
    """Filter that trims incoming track to align with downbeats."""

    output_fadeout_label: str = "fadeout_beatalign"
    output_fadein_label: str = "fadein_beatalign"

    def __init__(self, logger: logging.Logger, fadein_start_pos: float):
        """Initialize beat align filter.

        Args:
            fadein_start_pos: Position in seconds to trim the incoming track to
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
        """Return string representation of TrimFilter."""
        return f"Trim(trim={self.fadein_start_pos:.2f}s)"


class FrequencySweepFilter(Filter):
    """Filter that creates frequency sweep effects (lowpass/highpass transitions)."""

    output_fadeout_label: str = "frequency_sweep"
    output_fadein_label: str = "frequency_sweep"

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
        """Initialize frequency sweep filter.

        Args:
            sweep_type: 'lowpass' or 'highpass'
            target_freq: Target frequency for the filter
            duration: Duration of the sweep in seconds
            start_time: When to start the sweep
            sweep_direction: 'fade_in' (unfiltered->filtered) or 'fade_out' (filtered->unfiltered)
            poles: Number of poles for the filter
            curve_type: 'linear', 'exponential', or 'logarithmic'
            stream_type: 'fadeout' or 'fadein' - which stream to process
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

    def _generate_volume_expr(self, start: float, dur: float, direction: str, curve: str) -> str:
        t_expr = f"t-{start}"  # Time relative to start
        norm_t = f"min(max({t_expr},0),{dur})/{dur}"  # Normalized 0-1

        if curve == "exponential":
            # Exponential curve for smoother transitions
            if direction == "up":
                return f"'pow({norm_t},2)':eval=frame"
            else:
                return f"'1-pow({norm_t},2)':eval=frame"
        elif curve == "logarithmic":
            # Logarithmic curve for more aggressive initial change
            if direction == "up":
                return f"'sqrt({norm_t})':eval=frame"
            else:
                return f"'1-sqrt({norm_t})':eval=frame"
        elif direction == "up":
            return f"'{norm_t}':eval=frame"
        else:
            return f"'1-{norm_t}':eval=frame"

    def apply(self, input_fadein_label: str, input_fadeout_label: str) -> list[str]:
        """Generate FFmpeg filters for frequency sweep effect."""
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

        orig_label = f"{output_label}_orig"
        filter_label = f"{output_label}_to{self.sweep_type[:2]}"
        filtered_label = f"{output_label}_filtered"
        orig_faded_label = f"{output_label}_orig_faded"
        filtered_faded_label = f"{output_label}_filtered_faded"

        # Determine volume ramp directions based on sweep direction
        if self.sweep_direction == "fade_in":
            # Fade from dry to wet (unfiltered to filtered)
            orig_direction = "down"
            filter_direction = "up"
        else:  # fade_out
            # Fade from wet to dry (filtered to unfiltered)
            orig_direction = "up"
            filter_direction = "down"

        # Build filter chain
        orig_volume_expr = self._generate_volume_expr(
            self.start_time, self.duration, orig_direction, self.curve_type
        )
        filtered_volume_expr = self._generate_volume_expr(
            self.start_time, self.duration, filter_direction, self.curve_type
        )

        return [
            # Pass through the other stream unchanged
            f"{passthrough_input}anull[{passthrough_label}]",  # codespell:ignore anull
            # Split input into two paths
            f"{input_label}asplit=2[{orig_label}][{filter_label}]",
            # Apply frequency filter to one path
            f"[{filter_label}]{self.sweep_type}=f={self.target_freq}:poles={self.poles}[{filtered_label}]",
            # Apply time-varying volume to original path
            f"[{orig_label}]volume={orig_volume_expr}[{orig_faded_label}]",
            # Apply time-varying volume to filtered path
            f"[{filtered_label}]volume={filtered_volume_expr}[{filtered_faded_label}]",
            # Mix the two paths together
            f"[{orig_faded_label}][{filtered_faded_label}]amix=inputs=2:duration=longest:normalize=0[{output_label}]",
        ]

    def __repr__(self) -> str:
        """Return string representation of FrequencySweepFilter."""
        return f"FreqSweep({self.sweep_type}@{self.target_freq}Hz)"


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
        return [f"{input_fadeout_label}{input_fadein_label}acrossfade=d={self.crossfade_duration}"]

    def __repr__(self) -> str:
        """Return string representation of CrossfadeFilter."""
        return f"Crossfade(d={self.crossfade_duration:.1f}s)"
