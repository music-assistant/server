"""
Calibration chirp signal of the Sendspin provider.

Builds the repeating calibration track: a short logarithmic sine sweep at the
start of every fixed-length period, followed by a near-silent noise bed.

Two properties of this signal are load-bearing for the latency measurement that
consumes it. The period is a metronome running on the server clock, so its
length is fixed in whole frames and the emitted audio is one precomputed period
repeated verbatim - anything that recomputes the waveform per chunk risks
drifting the spacing. And the pulse is a swept sine rather than a noise burst
because matched-filter correlation against a sweep compresses room reverberation
into a sharp peak, which onset detection on noise does not.
"""

from __future__ import annotations

import math
import random
import struct

SAMPLE_RATE = 48000
# 16 bit is enough resolution: the quietest part of the signal is the -60 dBFS bed,
# which is still ~33 LSB and so nowhere near the dither floor
BIT_DEPTH = 16
CHANNELS = 2

# one 60 ms chirp every second, both expressed in whole frames so the period
# cannot drift. The period is also the measurement's unambiguous range: an arrival
# more than half a period late is numbered onto the following chirp and comes back
# folded, so one second resolves speakers spread across 500 ms - comfortably past
# the ~200 ms real speakers have measured at.
PERIOD_FRAMES = SAMPLE_RATE
CHIRP_FRAMES = SAMPLE_RATE * 60 // 1000

CHIRP_START_HZ = 500.0
CHIRP_END_HZ = 8000.0

# peak of the windowed sweep, leaving headroom for the resampling and DSP the
# playback pipeline applies downstream
CHIRP_PEAK = 10 ** (-6 / 20)
# some hardware drops into auto-standby on digital silence and then truncates the
# start of the next chirp, so the gap carries an inaudible noise bed instead
BED_PEAK = 10 ** (-60 / 20)
# fixed seed so the generated period is byte-identical on every run
BED_SEED = 0

_INT16_PEAK = 32767
# natural log of the sweep's total frequency ratio; the log sweep is defined in terms of it
_SWEEP_RATIO_LOG = math.log(CHIRP_END_HZ / CHIRP_START_HZ)
_CHIRP_DURATION = CHIRP_FRAMES / SAMPLE_RATE


def build_chirp_period() -> bytes:
    """
    Return exactly one calibration period as interleaved stereo PCM_S16LE.

    The buffer is ``PERIOD_FRAMES`` frames long and carries identical content in
    both channels. Streaming it back to back reproduces the calibration track
    with sample-exact chirp spacing.
    """
    bed_noise = random.Random(BED_SEED)  # noqa: S311 - a repeatable noise bed, no secret
    samples: list[int] = []
    for frame in range(PERIOD_FRAMES):
        if frame < CHIRP_FRAMES:
            # Hann window across the whole sweep, so it fades in and out of the
            # bed without a click at either edge
            window = 0.5 * (1.0 - math.cos(2.0 * math.pi * frame / CHIRP_FRAMES))
            value = CHIRP_PEAK * window * math.sin(chirp_phase(frame))
        else:
            value = BED_PEAK * (bed_noise.random() * 2.0 - 1.0)
        sample = int(value * _INT16_PEAK)
        samples.append(sample)
        samples.append(sample)
    return struct.pack(f"<{len(samples)}h", *samples)


def chirp_phase(frame: int) -> float:
    """
    Return the sweep's instantaneous phase in radians at the given frame.

    The sweep is logarithmic: its instantaneous frequency rises from
    ``CHIRP_START_HZ`` at frame 0 to ``CHIRP_END_HZ`` at ``CHIRP_FRAMES``.
    """
    progress = frame / CHIRP_FRAMES
    scale = 2.0 * math.pi * CHIRP_START_HZ * _CHIRP_DURATION / _SWEEP_RATIO_LOG
    return scale * (math.exp(progress * _SWEEP_RATIO_LOG) - 1.0)
