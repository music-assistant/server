"""Tests for the WLED audioSyncPacket builder."""

from __future__ import annotations

import math
import struct

import pytest

from music_assistant.providers.wled.constants import DEFAULT_GAIN_DB
from music_assistant.providers.wled.packet import (
    NOISE_GATE,
    PACKET_SIZE,
    loudness_to_sample,
    pack_audio_sync_packet,
    spectrum_to_fft_result,
)


def _loudness_for_amplitude(amplitude: float) -> int:
    """Return the raw dB-linear uint16 loudness value that decodes to a given amplitude."""
    db = 20.0 * math.log10(amplitude)
    normalized_db = (db + 60.0) / 60.0
    return round(max(0.0, min(1.0, normalized_db)) * 65535.0)


class TestPacketSize:
    """Tests for the wire-format size invariant."""

    def test_packet_is_44_bytes(self) -> None:
        """The struct must match WLED's packed 44-byte audioSyncPacket."""
        assert PACKET_SIZE == 44


class TestPackAudioSyncPacket:
    """Tests for packing full packets."""

    def test_round_trip_field_values(self) -> None:
        """Every field should decode back to what was packed in."""
        packet = pack_audio_sync_packet(
            sample_raw=12.5,
            sample_smth=10.0,
            sample_peak=True,
            fft_result=bytes(range(16)),
            fft_magnitude=99.5,
            fft_major_peak=440.0,
        )
        assert len(packet) == 44
        (
            header,
            _reserved1,
            sample_raw,
            sample_smth,
            sample_peak,
            _reserved2,
            fft_result,
            _reserved3,
            fft_magnitude,
            fft_major_peak,
        ) = struct.unpack("<6s2sffBB16sHff", packet)

        assert header == b"00002\x00"
        assert sample_raw == pytest.approx(12.5)
        assert sample_smth == pytest.approx(10.0)
        assert sample_peak == 1
        assert fft_result == bytes(range(16))
        assert fft_magnitude == pytest.approx(99.5)
        assert fft_major_peak == pytest.approx(440.0)

    def test_sample_peak_false_encodes_zero(self) -> None:
        """A False peak flag must encode as 0, not a truthy non-zero byte."""
        packet = pack_audio_sync_packet(
            sample_raw=0.0,
            sample_smth=0.0,
            sample_peak=False,
            fft_result=bytes(16),
            fft_magnitude=0.0,
            fft_major_peak=0.0,
        )
        sample_peak = struct.unpack("<6s2sffBB16sHff", packet)[4]
        assert sample_peak == 0

    def test_wrong_fft_length_raises(self) -> None:
        """fft_result must be exactly 16 bytes."""
        with pytest.raises(ValueError, match="16 bytes"):
            pack_audio_sync_packet(
                sample_raw=0.0,
                sample_smth=0.0,
                sample_peak=False,
                fft_result=bytes(15),
                fft_magnitude=0.0,
                fft_major_peak=0.0,
            )


class TestLoudnessToSample:
    """Tests for the dB-linear -> amplitude -> perceptual-gamma conversion."""

    def test_zero_loudness_is_gated_to_exactly_zero(self) -> None:
        """Loudness of 0 (the -60dBFS floor, amplitude ~0.001) is below NOISE_GATE and reads as 0."""
        assert loudness_to_sample(0) == 0.0

    def test_full_scale_loudness_maps_to_top_of_range(self) -> None:
        """Loudness of 65535 (0dBFS) should map to the top of the 0-255 scale."""
        assert loudness_to_sample(65535) == pytest.approx(255.0, rel=1e-3)

    def test_moderate_loudness_is_not_pinned_near_max(self) -> None:
        """A -20dBFS-ish loudness value must not read as near-max on a linear rescale."""
        # -20dBFS -> normalized_db = 40/60 = 0.667 -> loudness ~= 43690
        moderate_loudness = round(65535 * (40 / 60))
        naive_linear_rescale = moderate_loudness / 65535 * 255.0
        converted = loudness_to_sample(moderate_loudness)
        assert converted < naive_linear_rescale


class TestGain:
    """Tests for the amplitude-domain gain boost applied to loudness/spectrum/peak values."""

    def test_zero_gain_is_a_no_op(self) -> None:
        """0dB gain (the default parameter) must not change the converted value."""
        assert loudness_to_sample(30000, 0.0) == loudness_to_sample(30000)

    def test_positive_gain_raises_moderate_values(self) -> None:
        """A positive gain must increase a mid-range value's converted output."""
        unboosted = loudness_to_sample(30000, 0.0)
        boosted = loudness_to_sample(30000, 12.0)
        assert boosted > unboosted

    def test_gain_clips_at_top_of_range(self) -> None:
        """Gain must not push a full-scale value past 255."""
        assert loudness_to_sample(65535, 20.0) == pytest.approx(255.0)

    def test_default_gain_keeps_true_silence_at_zero(self) -> None:
        """
        Even with the default gain applied, the -60dBFS floor must stay gated to exactly 0.

        This is what an additive shift on the raw dB-linear value would get wrong: it would
        lift a floor (silent) value away from zero by a fixed amount regardless of how quiet
        the signal actually is. A proper amplitude-domain multiplier plus the noise gate
        leaves true silence at exactly zero even with gain applied.
        """
        assert loudness_to_sample(0, DEFAULT_GAIN_DB) == 0.0

    def test_noise_gate_zeroes_values_below_threshold(self) -> None:
        """A value whose amplitude sits just below NOISE_GATE must convert to exactly 0."""
        below_gate_loudness = _loudness_for_amplitude(NOISE_GATE / 2)
        assert loudness_to_sample(below_gate_loudness) == 0.0

    def test_values_above_noise_gate_are_not_zeroed(self) -> None:
        """A value whose amplitude sits above NOISE_GATE must not be gated to 0."""
        above_gate_loudness = _loudness_for_amplitude(NOISE_GATE * 2.5)
        assert loudness_to_sample(above_gate_loudness) > 0.0


class TestSpectrumToFftResult:
    """Tests for spectrum bin conversion to WLED's fftResult bytes."""

    def test_silent_bin_is_zero(self) -> None:
        """A bin at the -60dBFS floor must convert to 0, not a partially-lit byte."""
        result = spectrum_to_fft_result([0])
        assert result[0] == 0

    def test_full_scale_bin_reaches_max(self) -> None:
        """A bin at 0dBFS must convert to the top of the byte range."""
        result = spectrum_to_fft_result([65535])
        assert result[0] == 255

    def test_mid_range_bin_is_far_below_half_scale(self) -> None:
        """
        A bin at 50% of the dB-linear range (-30dBFS) must map well below 50% of the byte range.

        This is the regression case for treating a dB-linear value as if it were already
        linear amplitude: a naive bit-shift would put this at 128/255 (50%). The correct
        conversion (dB -> amplitude ~0.032, then sqrt-compressed) lands around 45/255 --
        clearly quiet, not "half brightness", while still visible thanks to the gamma curve.
        """
        result = spectrum_to_fft_result([32768])
        assert result[0] < 60

    def test_pads_short_input(self) -> None:
        """Fewer than 16 bins should be zero-padded, not raise."""
        result = spectrum_to_fft_result([65535])
        assert len(result) == 16
        assert result[0] == 255
        assert all(b == 0 for b in result[1:])

    def test_truncates_long_input(self) -> None:
        """More than 16 bins should be truncated to 16."""
        result = spectrum_to_fft_result([65535] * 20)
        assert len(result) == 16

    def test_gain_boosts_quiet_bins(self) -> None:
        """A gain boost must raise a quiet bin's converted byte value."""
        unboosted = spectrum_to_fft_result([20000], gain_db=0.0)[0]
        boosted = spectrum_to_fft_result([20000], gain_db=12.0)[0]
        assert boosted > unboosted
