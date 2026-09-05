"""Tests for the WLED audioSyncPacket builder."""

from __future__ import annotations

import math
import struct

import pytest

from music_assistant.providers.wled.constants import DEFAULT_GAIN_DB
from music_assistant.providers.wled.packet import (
    FFT_MAGNITUDE_SCALE,
    NOISE_GATE,
    PACKET_SIZE,
    fft_magnitude_from_amplitude,
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


class TestFftMagnitudeFromAmplitude:
    """Tests for the FFT_Magnitude conversion, which is unlike the byte-clamped fields."""

    def test_silence_is_zero(self) -> None:
        """The -60dBFS floor must convert to exactly 0."""
        assert fft_magnitude_from_amplitude(0) == 0.0

    def test_full_scale_reaches_the_configured_ceiling(self) -> None:
        """Full-scale amplitude must map to FFT_MAGNITUDE_SCALE, not a 0-255-ish value."""
        assert fft_magnitude_from_amplitude(65535) == pytest.approx(FFT_MAGNITUDE_SCALE)

    def test_output_is_not_byte_clamped(self) -> None:
        """
        Unlike loudness_to_sample/spectrum_to_fft_result, this must not compress into 0-255.

        WLED's real FFT_Magnitude is a raw, unnormalized float routinely in the thousands
        on real hardware -- passing a 0-255-ish value here would make magnitude-based
        effects on the receiving device under-react.
        """
        loudness = 47537  # amplitude ~0.15
        assert fft_magnitude_from_amplitude(loudness) > 255.0


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

    def test_silent_bin_is_zero_for_a_lightly_compensated_band(self) -> None:
        """
        A bin at the -60dBFS floor must convert to 0 for a band with little compensation.

        Band 9 sits in the ~1kHz region where A-weighting is roughly neutral and the
        pink-noise table adds only a modest boost (see _A_WEIGHT_COMPENSATION_DB /
        _PINK_NOISE_COMPENSATION_DB), so the noise gate still holds there.
        """
        bins = [0] * 16
        result = spectrum_to_fft_result(bins)
        assert result[9] == 0

    def test_silent_band_zero_is_not_zero_after_heavy_compensation(self) -> None:
        """
        Band 0's silence-is-zero guarantee breaks down under its own large compensation.

        Band 0 needs ~35dB of compensation (A-weighting + pink noise combined) to match
        WLED's own raw-magnitude analysis, which is enough to lift even the -60dBFS floor
        above the noise gate -- an inherent, expected trade-off of correctly restoring
        genuine bass content, not a regression of the "silence is zero" fix.
        """
        bins = [0] * 16
        result = spectrum_to_fft_result(bins)
        assert result[0] > 0

    def test_full_scale_bin_reaches_near_max(self) -> None:
        """
        A full-scale band 0 lands just under 255, not exactly at it.

        Every scaling mode's per-band multiplier is 0.85 at band 0 (matching WLED's own
        square_root/linear/logarithmic FFTScalingMode formulas), a deliberate slight
        de-emphasis of the lowest band relative to the rest -- not a bug.
        """
        result = spectrum_to_fft_result([65535])
        assert result[0] == 217

    def test_full_scale_bin_reaches_max_at_higher_band(self) -> None:
        """A full-scale bin at a band whose multiplier exceeds 1.0 clips at 255."""
        bins = [0, 0, 0, 0, 65535]
        result = spectrum_to_fft_result(bins)
        assert result[4] == 255

    def test_mid_range_bin_is_far_below_naive_bitshift_value(self) -> None:
        """
        A bin at 50% of the dB-linear range (-30dBFS) must not read as ~50% brightness.

        Uses band 9 (~4dB combined compensation, the least of any band) to isolate the
        dB -> amplitude conversion itself: a naive bit-shift would put this at 128/255
        (50%). -30dBFS is genuinely quiet -- even after band 9's small compensation, it
        should read well below that naive value.
        """
        bins = [0] * 16
        bins[9] = 32768
        result = spectrum_to_fft_result(bins)
        assert result[9] < 180

    def test_pads_short_input(self) -> None:
        """Fewer than 16 bins should be zero-padded, not raise."""
        result = spectrum_to_fft_result([65535])
        assert len(result) == 16
        assert (
            result[0] == 217
        )  # band 0's multiplier is 0.85, see test_full_scale_bin_reaches_near_max
        # band 0 also needs heavy compensation (see test_silent_band_zero_is_not_zero_after_heavy_compensation),
        # so a padded-in 0 there isn't exactly 0 either.
        assert result[0] > 0
        assert all(b == 0 for b in result[1:])

    def test_truncates_long_input(self) -> None:
        """More than 16 bins should be truncated to 16."""
        result = spectrum_to_fft_result([65535] * 20)
        assert len(result) == 16

    def test_gain_boosts_quiet_bins(self) -> None:
        """A gain boost must raise a quiet bin's converted byte value."""
        bins_unboosted = [0] * 16
        bins_unboosted[9] = 20000
        bins_boosted = [0] * 16
        bins_boosted[9] = 20000
        unboosted = spectrum_to_fft_result(bins_unboosted, gain_db=0.0)[9]
        boosted = spectrum_to_fft_result(bins_boosted, gain_db=12.0)[9]
        assert boosted > unboosted

    def test_higher_bands_get_more_post_curve_boost_within_a_mode(self) -> None:
        """
        A band with a higher index must read louder than a lower one for the same input.

        Bands 9 and 10 have almost identical pre-curve A-weighting/pink compensation
        (within 0.1dB of each other), isolating the per-mode post-curve _band_multiplier
        this is meant to test.
        """
        bins = [0] * 16
        bins[9] = 30000
        bins[10] = 30000
        result = spectrum_to_fft_result(bins, scaling_mode="square_root")
        assert result[10] > result[9]


class TestScalingModes:
    """Tests that the three WLED-matching scaling modes actually behave differently."""

    def test_modes_produce_different_output_for_mid_range_input(self) -> None:
        """Logarithmic, square-root, and linear must not collapse to the same value."""
        loudness = 47537  # amplitude ~0.15, well above NOISE_GATE
        linear = loudness_to_sample(loudness, scaling_mode="linear")
        square_root = loudness_to_sample(loudness, scaling_mode="square_root")
        logarithmic = loudness_to_sample(loudness, scaling_mode="logarithmic")
        assert linear < square_root < logarithmic

    def test_all_modes_agree_at_full_scale(self) -> None:
        """Every curve maps amplitude 1.0 to 1.0, so full-scale input should match across modes."""
        assert (
            loudness_to_sample(65535, scaling_mode="linear")
            == loudness_to_sample(65535, scaling_mode="square_root")
            == loudness_to_sample(65535, scaling_mode="logarithmic")
            == pytest.approx(255.0)
        )

    def test_all_modes_gate_true_silence_to_zero(self) -> None:
        """The noise gate applies before the curve, so silence is 0 regardless of mode."""
        for mode in ("linear", "square_root", "logarithmic"):
            assert loudness_to_sample(0, scaling_mode=mode) == 0.0
