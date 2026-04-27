"""Sonic analysis helper — feature extraction and semantic audio analysis.

Extracts per-block spectral/timbral features from raw PCM audio using librosa,
then collapses accumulated blocks into a populated AudioAnalysisData with
semantic descriptors.

Fields NOT computed here are left as None and expected to be supplied by
overlay providers (see `sonic_similarity.OVERLAY_SOURCES`):

- `bpm`                       ← smart_fades (beat_this CNN)
- `key`, `mode`               ← smart_fades (S-KEY neural classifier)
- `danceability`              ← clap_analysis (zero-shot, Platt-calibrated)
- `valence`, `arousal`,
  `instrumentalness`,
  `acousticness`              ← clap_analysis (zero-shot, Platt-calibrated)
- `loudness_integrated`,
  `loudness_range`,
  `true_peak`                 ← loudness_analysis (ebur128) when enabled;
                                 fallback approximations populated here
"""

from __future__ import annotations

from dataclasses import dataclass, field

import librosa
import numpy as np
import numpy.typing as npt
import torch
import torchaudio.transforms as torchaudio_transforms

from music_assistant.models.audio_analysis import AudioAnalysisData

# Fixed resolution for time-series fields (rms_energy, spectral_centroid) on
# AudioAnalysisData — matches the upstream contract shared with other analysis
# providers. Produces a consistent x-axis resolution regardless of track length.
_TIME_SERIES_BINS = 1800

# Energy threshold below which spectral centroid becomes noise-dominated; centroid
# bins with RMS below this are zeroed to keep the signal musically meaningful.
_SILENCE_THRESHOLD = 0.01

# STFT parameters. These match librosa's defaults so the shared STFT is
# numerically equivalent to what each of the downstream feature functions
# would compute internally if called with `y=audio`.
_STFT_N_FFT = 2048
_STFT_HOP = 512

# Cached Hann window — torch.stft requires an explicit window; caching
# avoids rebuilding it on every block. periodic=False gives the SYMMETRIC
# hann that scipy/librosa use by default; torch's default is periodic=True
# (periodic=True would produce ~1e-3 per-sample drift in the STFT output,
# causing material differences in downstream features).
_HANN_WINDOW = torch.hann_window(_STFT_N_FFT, periodic=False, dtype=torch.float32)

# Chroma + spectral-contrast filterbank / band masks, computed once at
# module import via librosa. Runtime computation is then pure torch:
# - chroma: single matmul + per-column max-norm
# - contrast: per-band top/bottom-quantile log-ratio
# The one-time librosa calls are cheap (~milliseconds) and bake librosa's
# well-calibrated filter shapes into our torch pipeline.
_CHROMA_SR = 22050  # sample rate baked into filterbank — must match audio SR
_CONTRAST_N_BANDS = 6
_CONTRAST_FMIN = 200.0
_CONTRAST_QUANTILE = 0.02

_CHROMA_FILTERBANK = torch.from_numpy(
    librosa.filters.chroma(sr=_CHROMA_SR, n_fft=_STFT_N_FFT, n_chroma=12)
).to(dtype=torch.float32)  # shape (12, n_freqs)


def _compute_contrast_band_masks(
    sample_rate: int, n_fft: int, n_bands: int, fmin: float
) -> list[torch.Tensor]:
    """Return a list of boolean bin-masks, one per (n_bands+1) contrast bands.

    Band 0: [0, fmin]. Bands 1..n_bands: octave intervals starting at fmin.
    The final band extends to Nyquist regardless of fmin * 2**n_bands.

    :param sample_rate: Sample rate in Hz.
    :param n_fft: FFT length used for the source STFT.
    :param n_bands: Number of octave bands above fmin.
    :param fmin: Lowest band edge in Hz.
    """
    n_freqs = n_fft // 2 + 1
    freqs = torch.linspace(0.0, sample_rate / 2.0, n_freqs)
    masks: list[torch.Tensor] = [freqs <= fmin]
    for i in range(n_bands):
        lo = fmin * (2.0**i)
        hi = fmin * (2.0 ** (i + 1)) if i < n_bands - 1 else sample_rate / 2.0
        masks.append((freqs > lo) & (freqs <= hi))
    return masks


_CONTRAST_BAND_MASKS = _compute_contrast_band_masks(
    _CHROMA_SR, _STFT_N_FFT, _CONTRAST_N_BANDS, _CONTRAST_FMIN
)

# Onset strength uses a mel spectrogram at the same time resolution as the
# linear STFT, with 128 mel bands (librosa.onset.onset_strength default).
_MEL_N_MELS = 128
_MEL_TRANSFORM = torchaudio_transforms.MelSpectrogram(
    sample_rate=_CHROMA_SR,
    n_fft=_STFT_N_FFT,
    hop_length=_STFT_HOP,
    n_mels=_MEL_N_MELS,
    f_min=0.0,
    f_max=_CHROMA_SR / 2.0,
    power=2.0,
    center=True,
    # librosa.feature.melspectrogram's underlying STFT uses pad_mode="constant"
    # (zero-pad) by default. Matching that here is load-bearing: reflect-pad
    # would cause the first ~2 frames of the onset envelope to shift relative
    # to librosa, producing a near-zero correlation on the envelope and ~8%
    # drift in downstream rhythmic_regularity.
    pad_mode="constant",
    norm="slaney",
    mel_scale="slaney",
)


@dataclass
class BlockFeatures:
    """Per-block feature arrays accumulated across 10-second blocks.

    After all blocks are processed, collapse_to_analysis() aggregates these
    into a populated AudioAnalysisData.

    Only features actually consumed by the current collapse pipeline are
    extracted. (MFCC, tonnetz, rolloff, and ZCR were previously extracted
    but never read; removed to save ~100ms per 10s block.)
    """

    chroma_frames: list[np.ndarray] = field(default_factory=list)
    contrast_frames: list[np.ndarray] = field(default_factory=list)
    centroid_frames: list[np.ndarray] = field(default_factory=list)
    flatness_frames: list[np.ndarray] = field(default_factory=list)
    rms_frames: list[np.ndarray] = field(default_factory=list)
    onset_env_frames: list[np.ndarray] = field(default_factory=list)


MIN_BLOCK_SAMPLES: int = 4096


def extract_block_features(audio: np.ndarray, sample_rate: int) -> BlockFeatures | None:
    """Extract per-frame features from a single audio block (~10 seconds).

    Returns None if the audio is too short for STFT processing.

    Phase 4A architecture:
    - Compute STFT once via torch (faster than numpy FFT, SIMD-accelerated,
      plus paves the way for batched processing in Phase 4C).
    - Spectral features that need binning/band logic (chroma, contrast)
      stay on librosa and are fed the torch-computed STFT via the `S=` kwarg.
    - Simple reductions (centroid, flatness, RMS) run directly on the torch
      tensor as native ops — no librosa call at all.
    - onset_strength stays on librosa for now (uses a mel spectrogram with
      different parameters; moved to torch in Phase 4C).

    :param audio: Mono float32 audio samples for this block.
    :param sample_rate: Sample rate in Hz.
    """
    if len(audio) < MIN_BLOCK_SAMPLES:
        return None
    bf = BlockFeatures()

    audio_t = torch.from_numpy(audio).to(dtype=torch.float32)

    # One torch STFT, shared across all spectral features.
    stft_complex = torch.stft(
        audio_t,
        n_fft=_STFT_N_FFT,
        hop_length=_STFT_HOP,
        window=_HANN_WINDOW,
        center=True,
        pad_mode="reflect",
        return_complex=True,
        normalized=False,
    )
    stft_mag_t = stft_complex.abs()
    stft_power_t = stft_mag_t**2

    # All spectral features run natively in torch. librosa is no longer
    # called in the per-block hot path.
    bf.chroma_frames.append(_chroma_stft_torch(stft_power_t))
    bf.contrast_frames.append(_spectral_contrast_torch(stft_mag_t))
    bf.centroid_frames.append(_spectral_centroid_torch(stft_mag_t, sample_rate))
    bf.flatness_frames.append(_spectral_flatness_torch(stft_mag_t))
    bf.rms_frames.append(_rms_torch(audio_t))
    bf.onset_env_frames.append(_onset_strength_torch(audio_t))

    return bf


def _chroma_stft_torch(power: torch.Tensor) -> np.ndarray:
    """Torch-native chroma_stft equivalent to librosa.feature.chroma_stft(S=power).

    Uses librosa's precomputed chroma filterbank (baked at module load) for
    frequency-to-pitch-class mapping, then runs the matmul + per-column
    max-normalization in torch.

    :param power: Power spectrogram (magnitude**2), shape (n_freqs, n_frames).
    """
    raw = _CHROMA_FILTERBANK @ power  # (12, n_frames)
    # librosa default: norm=inf per column (max-normalize each frame)
    col_max = raw.abs().amax(dim=0, keepdim=True)
    col_max = torch.clamp(col_max, min=1e-10)
    return np.asarray((raw / col_max).numpy())


def _spectral_contrast_torch(mag: torch.Tensor) -> np.ndarray:
    """Torch-native spectral_contrast.

    Matches librosa.feature.spectral_contrast(S=mag, n_bands=6, quantile=0.02,
    linear=False) — default behavior. For each of n_bands+1 frequency bands,
    compute log(peak_mean) - log(valley_mean) where peak/valley are the top
    and bottom `quantile` fractions of magnitudes in that band.

    :param mag: Magnitude spectrogram, shape (n_freqs, n_frames).
    """
    n_bands = _CONTRAST_N_BANDS
    n_frames = mag.shape[1]
    result = torch.zeros((n_bands + 1, n_frames), dtype=mag.dtype)

    for band_idx, mask in enumerate(_CONTRAST_BAND_MASKS):
        band = mag[mask]  # (n_band_bins, n_frames)
        n = band.shape[0]
        if n == 0:
            continue
        # Top and bottom `quantile` fraction — at least 1 bin each
        k = max(1, int(np.ceil(_CONTRAST_QUANTILE * n)))
        # topk is O(n) per output bin; sort would be O(n log n). For k much
        # smaller than n (quantile=0.02) this is substantially faster and
        # produces identical peak/valley means.
        peak_vals, _ = torch.topk(band, k, dim=0, largest=True, sorted=False)
        valley_vals, _ = torch.topk(band, k, dim=0, largest=False, sorted=False)
        peak_mean = peak_vals.mean(dim=0)
        valley_mean = valley_vals.mean(dim=0)
        # Contrast in dB (10 * log10). librosa returns values in dB scale
        # when linear=False (its default) — verified empirically: natural-log
        # output is scaled by exactly 10/ln(10) ≈ 4.343 in librosa's output.
        result[band_idx] = 10.0 * (
            torch.log10(torch.clamp(peak_mean, min=1e-10))
            - torch.log10(torch.clamp(valley_mean, min=1e-10))
        )
    return np.asarray(result.numpy())


def _onset_strength_torch(audio: torch.Tensor) -> np.ndarray:
    """Torch-native onset strength envelope.

    Mirrors librosa.onset.onset_strength's default pipeline:
    1. Mel spectrogram (n_mels=128) with same hop/fft as main STFT.
    2. Convert to dB via power_to_db with ref=np.max semantics — values
       are 10*log10(S/max), floored at max_db - top_db (80dB dynamic
       range). This is what makes "silent" regions of the mel spectrum
       clip to a common floor so diffs there are exactly zero.
    3. First-order time difference, half-wave rectify.
    4. Mean across mel bins (librosa's default aggregate=np.mean).
    5. Prepend zero to restore (n_frames,) length.

    The dB conversion was load-bearing: without it (or with plain ln),
    silent regions produce small non-zero diffs that leak into
    rhythmic_regularity and shift downstream scalars by ~8%.

    :param audio: 1D time-domain audio tensor.
    """
    mel = _MEL_TRANSFORM(audio)  # (n_mels, n_frames), power spectrogram
    # power_to_db(S, ref=np.max, amin=1e-10, top_db=80) equivalent:
    ref = mel.max().clamp(min=1e-10)
    # dB with relative ref; amin clipping prevents log(0)
    mel_db = 10.0 * torch.log10(torch.clamp(mel, min=1e-10 * ref) / ref)
    # Clamp dynamic range to 80dB so silent regions all share a common floor
    mel_db = torch.clamp(mel_db, min=mel_db.max() - 80.0)

    diff = torch.diff(mel_db, dim=-1)  # (n_mels, n_frames - 1)
    rectified = torch.clamp(diff, min=0.0)
    envelope = rectified.mean(dim=0)  # (n_frames - 1,)
    # Prepend 0 so length matches STFT n_frames (librosa does the same)
    envelope = torch.cat([envelope.new_zeros(1), envelope])
    return np.asarray(envelope.numpy())


def _spectral_centroid_torch(mag: torch.Tensor, sample_rate: int) -> np.ndarray:
    """Compute per-frame spectral centroid in Hz from a magnitude spectrogram.

    Matches librosa.feature.spectral_centroid output shape (1, n_frames).

    :param mag: Magnitude spectrogram, shape (n_freqs, n_frames).
    :param sample_rate: Sample rate in Hz.
    """
    n_freqs = mag.shape[0]
    # Frequencies of each bin: linspace(0, sr/2, n_freqs)
    freqs = torch.linspace(0.0, sample_rate / 2.0, n_freqs, dtype=mag.dtype)
    # Weighted sum / total per frame
    weighted = (freqs.unsqueeze(1) * mag).sum(dim=0)  # (n_frames,)
    total = mag.sum(dim=0)  # (n_frames,)
    centroid = torch.where(total > 0, weighted / total, torch.zeros_like(weighted))
    return np.asarray(centroid.unsqueeze(0).numpy())  # shape (1, n_frames) to match librosa


def _spectral_flatness_torch(mag: torch.Tensor) -> np.ndarray:
    """Compute per-frame spectral flatness = geometric_mean / arithmetic_mean.

    Matches librosa.feature.spectral_flatness output shape (1, n_frames).

    :param mag: Magnitude spectrogram, shape (n_freqs, n_frames).
    """
    # librosa computes flatness on amplitude**2 (power) with amin=1e-10 clipping
    power = mag**2
    power = torch.clamp(power, min=1e-10)
    # Geometric mean via log-sum-exp: exp(mean(log(x)))
    log_power = torch.log(power)
    geom_mean = torch.exp(log_power.mean(dim=0))  # (n_frames,)
    arith_mean = power.mean(dim=0)  # (n_frames,)
    flatness = geom_mean / arith_mean
    return np.asarray(flatness.unsqueeze(0).numpy())  # shape (1, n_frames)


def _rms_torch(audio: torch.Tensor) -> np.ndarray:
    """Compute per-frame RMS from time-domain audio, matching librosa.feature.rms.

    librosa.feature.rms(y=audio) defaults: frame_length=2048, hop_length=512,
    center=True, pad_mode="constant" (zero-pad). Matches those exactly.

    :param audio: 1D time-domain audio tensor.
    """
    frame_length = _STFT_N_FFT
    hop_length = _STFT_HOP
    pad = frame_length // 2
    padded = torch.nn.functional.pad(audio, (pad, pad), mode="constant", value=0.0)
    # Unfold into (n_frames, frame_length) windows
    frames = padded.unfold(0, frame_length, hop_length)
    rms = torch.sqrt((frames**2).mean(dim=1))
    return np.asarray(rms.unsqueeze(0).numpy())  # shape (1, n_frames)


def merge_block_features(target: BlockFeatures, source: BlockFeatures) -> None:
    """Merge source block features into target (in place).

    :param target: Accumulator to merge into.
    :param source: New block features to add.
    """
    target.chroma_frames.extend(source.chroma_frames)
    target.contrast_frames.extend(source.contrast_frames)
    target.centroid_frames.extend(source.centroid_frames)
    target.flatness_frames.extend(source.flatness_frames)
    target.rms_frames.extend(source.rms_frames)
    target.onset_env_frames.extend(source.onset_env_frames)


def collapse_to_analysis(accumulated: BlockFeatures, sample_rate: int) -> AudioAnalysisData:
    """Collapse accumulated per-block features into a populated AudioAnalysisData.

    Populates measurement-based scalar and time-series fields that librosa is
    well-suited to compute. Fields owned by overlay providers (bpm/key/mode via
    smart_fades, soft scalars via clap_analysis, real LUFS via loudness_analysis)
    are left as None and filled in at vector-assembly time by the similarity
    plugin's overlay system.

    :param accumulated: All block features accumulated during streaming.
    :param sample_rate: Sample rate used during extraction.
    """
    onset_env = np.concatenate(accumulated.onset_env_frames)
    chroma = np.concatenate(accumulated.chroma_frames, axis=1)
    rms = np.concatenate(accumulated.rms_frames, axis=1).squeeze()
    centroid = np.concatenate(accumulated.centroid_frames, axis=1).squeeze()
    contrast = np.concatenate(accumulated.contrast_frames, axis=1)
    flatness = np.concatenate(accumulated.flatness_frames, axis=1).squeeze()

    energy = _derive_energy(rms)
    loudness_integrated, loudness_range = _derive_loudness(rms)
    brightness = _derive_brightness(centroid, sample_rate)
    harmonic_complexity = _derive_harmonic_complexity(chroma)
    roughness = _derive_roughness(contrast, flatness)
    rhythmic_regularity = _derive_rhythmic_regularity(onset_env, sample_rate)
    rms_energy_series = _derive_rms_energy_series(rms)
    spectral_centroid_series = _derive_spectral_centroid_series(centroid, rms_energy_series)

    return AudioAnalysisData(
        energy=energy,
        loudness_integrated=loudness_integrated,
        loudness_range=loudness_range,
        brightness=brightness,
        harmonic_complexity=harmonic_complexity,
        roughness=roughness,
        rhythmic_regularity=rhythmic_regularity,
        rms_energy=rms_energy_series,
        spectral_centroid=spectral_centroid_series,
    )


def _clamp(value: float) -> float:
    """Clamp a float to [0.0, 1.0]."""
    return float(max(0.0, min(1.0, value)))


def _derive_energy(rms: np.ndarray) -> float:
    """Compute normalized mean RMS energy in [0, 1].

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    # RMS values are typically in [0, 1] for float32 audio; take mean and clamp
    return _clamp(float(rms.mean()))


def _derive_loudness(rms: np.ndarray) -> tuple[float, float]:
    """Compute RMS-derived dB approximations for integrated loudness and loudness range.

    Fallback only — real EBU R128 values come from the loudness_analysis
    provider when enabled; the similarity plugin does not currently overlay
    those onto primary rows, so these approximations remain the source of
    truth for loudness fields in the vector until that overlay exists.

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    rms_clipped = np.clip(rms, 1e-8, None)
    rms_db = 20.0 * np.log10(rms_clipped)
    loudness_integrated = float(rms_db.mean())
    loudness_range = float(rms_db.std())
    return loudness_integrated, loudness_range


def _derive_brightness(centroid: np.ndarray, sample_rate: int) -> float:
    """Compute mean spectral centroid normalized against the Nyquist frequency.

    :param centroid: Per-frame spectral centroid values in Hz (1D after squeeze).
    :param sample_rate: Sample rate in Hz.
    """
    nyquist = sample_rate / 2.0
    return _clamp(float(centroid.mean()) / nyquist)


def _derive_harmonic_complexity(chroma: np.ndarray) -> float:
    """Compute normalized Shannon entropy of the mean chroma vector.

    :param chroma: Concatenated chroma feature matrix (12 x N_frames).
    """
    mean_chroma = chroma.mean(axis=1).astype(np.float64)
    # Normalize to a probability distribution
    chroma_sum = mean_chroma.sum()
    if chroma_sum <= 0:
        return 0.0
    p = mean_chroma / chroma_sum
    p = np.clip(p, 1e-10, None)
    entropy = float(-np.sum(p * np.log(p)))
    # Max entropy for 12 bins is ln(12)
    max_entropy = float(np.log(12))
    return _clamp(entropy / max_entropy)


def _derive_roughness(contrast: np.ndarray, flatness: np.ndarray) -> float:
    """Combine spectral contrast range and spectral flatness into a roughness measure.

    :param contrast: Spectral contrast matrix (7 x N_frames).
    :param flatness: Per-frame spectral flatness values (1D after squeeze).
    """
    # High contrast range → more tonal variation → rougher texture
    contrast_range = float(contrast.max() - contrast.min())
    # Normalize against a reasonable max contrast range (~80 dB)
    contrast_score = _clamp(contrast_range / 80.0)

    # High flatness (noise-like) → rougher; low flatness (tonal) → smoother
    flatness_score = _clamp(float(flatness.mean()))

    return _clamp(0.6 * contrast_score + 0.4 * flatness_score)


def _derive_rhythmic_regularity(onset_env: np.ndarray, sample_rate: int) -> float:
    """Estimate rhythmic regularity as 1 minus the normalized CV of inter-onset intervals.

    :param onset_env: Concatenated onset strength envelope.
    :param sample_rate: Sample rate in Hz.
    """
    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sample_rate)
    if len(onset_frames) < 2:
        return 0.0
    ioi = np.diff(onset_frames).astype(np.float64)
    cv = float(ioi.std() / (ioi.mean() + 1e-8))
    return _clamp(1.0 - cv)


def _derive_rms_energy_series(rms: np.ndarray) -> npt.NDArray[np.float32]:
    """Interpolate per-frame RMS onto fixed 1800 bins and peak-normalize.

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    if len(rms) == 0:
        return np.zeros(_TIME_SERIES_BINS, dtype=np.float32)
    src_x = np.linspace(0.0, 1.0, num=len(rms))
    dst_x = np.linspace(0.0, 1.0, num=_TIME_SERIES_BINS)
    result: npt.NDArray[np.float32] = np.interp(dst_x, src_x, rms).astype(np.float32)
    peak = result.max()
    if peak > 0:
        result = result / peak
    return result


def _derive_spectral_centroid_series(
    centroid: np.ndarray, rms_energy: npt.NDArray[np.float32]
) -> npt.NDArray[np.float32]:
    """Interpolate per-frame centroid onto fixed 1800 bins, zeroing silent regions.

    :param centroid: Per-frame spectral centroid values in Hz (1D after squeeze).
    :param rms_energy: Normalized RMS energy series (1800 bins) used to mask silence.
    """
    if len(centroid) == 0:
        return np.zeros(_TIME_SERIES_BINS, dtype=np.float32)
    src_x = np.linspace(0.0, 1.0, num=len(centroid))
    dst_x = np.linspace(0.0, 1.0, num=_TIME_SERIES_BINS)
    result: npt.NDArray[np.float32] = np.interp(dst_x, src_x, centroid).astype(np.float32)
    result[rms_energy < _SILENCE_THRESHOLD] = 0.0
    return result
