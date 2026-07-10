"""Per-block spectral/timbral feature extraction and AudioAnalysisData collapse."""

from __future__ import annotations

from dataclasses import dataclass, field

import librosa
import numpy as np
import numpy.typing as npt
import torch
import torchaudio.transforms as torchaudio_transforms

from music_assistant.models.audio_analysis import AudioAnalysisData

_TIME_SERIES_BINS = 1800
_SILENCE_THRESHOLD = 0.01

_STFT_N_FFT = 2048
_STFT_HOP = 512

# periodic=False matches scipy/librosa's symmetric Hann; torch's periodic=True
# default produces ~1e-3 per-sample STFT drift that propagates into features.
_HANN_WINDOW = torch.hann_window(_STFT_N_FFT, periodic=False, dtype=torch.float32)

_CHROMA_SR = 22050  # filterbank-baked sample rate — must match audio SR
_CONTRAST_N_BANDS = 6
_CONTRAST_FMIN = 200.0
_CONTRAST_QUANTILE = 0.02
_ROUGHNESS_CONTRAST_MAX_DB = 80.0  # rough physical ceiling for spectral contrast range in dB
_ROUGHNESS_CONTRAST_WEIGHT = 0.6  # weighting of contrast-derived component
_ROUGHNESS_FLATNESS_WEIGHT = 0.4  # weighting of flatness-derived component

_CHROMA_FILTERBANK = torch.from_numpy(
    librosa.filters.chroma(sr=_CHROMA_SR, n_fft=_STFT_N_FFT, n_chroma=12)
).to(dtype=torch.float32)


def _compute_contrast_band_masks(
    sample_rate: int, n_fft: int, n_bands: int, fmin: float
) -> list[torch.Tensor]:
    """
    Return a list of boolean bin-masks, one per (n_bands+1) contrast bands.

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

_MEL_N_MELS = 128
# pad_mode="constant" matches librosa.feature.melspectrogram; "reflect" drifts
# the onset envelope ~8% in downstream rhythmic_regularity.
_MEL_TRANSFORM = torchaudio_transforms.MelSpectrogram(
    sample_rate=_CHROMA_SR,
    n_fft=_STFT_N_FFT,
    hop_length=_STFT_HOP,
    n_mels=_MEL_N_MELS,
    f_min=0.0,
    f_max=_CHROMA_SR / 2.0,
    power=2.0,
    center=True,
    pad_mode="constant",
    norm="slaney",
    mel_scale="slaney",
)


@dataclass
class BlockFeatures:
    """Per-block feature arrays accumulated across 10-second blocks."""

    chroma_frames: list[np.ndarray] = field(default_factory=list)
    contrast_frames: list[np.ndarray] = field(default_factory=list)
    centroid_frames: list[np.ndarray] = field(default_factory=list)
    flatness_frames: list[np.ndarray] = field(default_factory=list)
    rms_frames: list[np.ndarray] = field(default_factory=list)
    onset_env_frames: list[np.ndarray] = field(default_factory=list)


MIN_BLOCK_SAMPLES: int = 4096


def extract_block_features(audio: np.ndarray, sample_rate: int) -> BlockFeatures | None:
    """
    Extract per-frame features from a single audio block, or None if too short.

    :param audio: Mono float32 audio samples for this block.
    :param sample_rate: Sample rate in Hz. Must equal ``_CHROMA_SR`` (22050) — the filterbanks
        are baked at this rate. Use soxr to resample before calling if needed.
    """
    assert sample_rate == _CHROMA_SR, (
        f"extract_block_features requires sample_rate={_CHROMA_SR} "
        f"(filterbanks baked at this rate); got {sample_rate}"
    )
    if len(audio) < MIN_BLOCK_SAMPLES:
        return None
    bf = BlockFeatures()

    audio_t = torch.from_numpy(audio).to(dtype=torch.float32)

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

    bf.chroma_frames.append(_chroma_stft_torch(stft_power_t))
    bf.contrast_frames.append(_spectral_contrast_torch(stft_mag_t))
    bf.centroid_frames.append(_spectral_centroid_torch(stft_mag_t, sample_rate))
    bf.flatness_frames.append(_spectral_flatness_torch(stft_mag_t))
    bf.rms_frames.append(_rms_torch(audio_t))
    bf.onset_env_frames.append(_onset_strength_torch(audio_t))

    return bf


def _chroma_stft_torch(power: torch.Tensor) -> np.ndarray:
    """
    Torch-native chroma_stft equivalent to librosa.feature.chroma_stft(S=power).

    :param power: Power spectrogram (magnitude**2), shape (n_freqs, n_frames).
    """
    raw = _CHROMA_FILTERBANK @ power
    col_max = raw.abs().amax(dim=0, keepdim=True)
    col_max = torch.clamp(col_max, min=1e-10)
    return np.asarray((raw / col_max).numpy())


def _spectral_contrast_torch(mag: torch.Tensor) -> np.ndarray:
    """
    Torch-native spectral_contrast matching librosa defaults (n_bands=6, quantile=0.02).

    :param mag: Magnitude spectrogram, shape (n_freqs, n_frames).
    """
    n_bands = _CONTRAST_N_BANDS
    n_frames = mag.shape[1]
    result = torch.zeros((n_bands + 1, n_frames), dtype=mag.dtype)

    for band_idx, mask in enumerate(_CONTRAST_BAND_MASKS):
        band = mag[mask]
        n = band.shape[0]
        if n == 0:
            continue
        k = max(1, int(np.ceil(_CONTRAST_QUANTILE * n)))
        peak_vals, _ = torch.topk(band, k, dim=0, largest=True, sorted=False)
        valley_vals, _ = torch.topk(band, k, dim=0, largest=False, sorted=False)
        peak_mean = peak_vals.mean(dim=0)
        valley_mean = valley_vals.mean(dim=0)
        # 10*log10 to match librosa's default linear=False dB output.
        result[band_idx] = 10.0 * (
            torch.log10(torch.clamp(peak_mean, min=1e-10))
            - torch.log10(torch.clamp(valley_mean, min=1e-10))
        )
    return np.asarray(result.numpy())


def _onset_strength_torch(audio: torch.Tensor) -> np.ndarray:
    """
    Torch-native onset strength envelope matching librosa.onset.onset_strength defaults.

    :param audio: 1D time-domain audio tensor.
    """
    # The dB conversion (vs plain log) is load-bearing: it clips silent
    # regions to a common floor, otherwise tiny diffs leak into
    # rhythmic_regularity and shift downstream scalars by ~8%.
    mel = _MEL_TRANSFORM(audio)
    ref = mel.max().clamp(min=1e-10)
    mel_db = 10.0 * torch.log10(torch.clamp(mel, min=1e-10 * ref) / ref)
    mel_db = torch.clamp(mel_db, min=mel_db.max() - 80.0)

    diff = torch.diff(mel_db, dim=-1)
    rectified = torch.clamp(diff, min=0.0)
    envelope = rectified.mean(dim=0)
    return np.asarray(envelope.numpy())


def _spectral_centroid_torch(mag: torch.Tensor, sample_rate: int) -> np.ndarray:
    """
    Per-frame spectral centroid (Hz), shape (1, n_frames) matching librosa.

    :param mag: Magnitude spectrogram, shape (n_freqs, n_frames).
    :param sample_rate: Sample rate in Hz.
    """
    n_freqs = mag.shape[0]
    freqs = torch.linspace(0.0, sample_rate / 2.0, n_freqs, dtype=mag.dtype)
    weighted = (freqs.unsqueeze(1) * mag).sum(dim=0)
    total = mag.sum(dim=0)
    centroid = torch.where(total > 0, weighted / total, torch.zeros_like(weighted))
    return np.asarray(centroid.unsqueeze(0).numpy())


def _spectral_flatness_torch(mag: torch.Tensor) -> np.ndarray:
    """
    Per-frame spectral flatness (geom/arith mean), shape (1, n_frames).

    :param mag: Magnitude spectrogram (NOT power; this function squares
        internally to match librosa's default behavior), shape (n_freqs, n_frames).
        Contrast with :func:`_chroma_stft_torch`, which accepts power directly.
    """
    power = mag**2
    power = torch.clamp(power, min=1e-10)
    log_power = torch.log(power)
    geom_mean = torch.exp(log_power.mean(dim=0))
    arith_mean = power.mean(dim=0)
    flatness = geom_mean / arith_mean
    return np.asarray(flatness.unsqueeze(0).numpy())


def _rms_torch(audio: torch.Tensor) -> np.ndarray:
    """
    Per-frame RMS, shape (1, n_frames), matching librosa.feature.rms defaults.

    :param audio: 1D time-domain audio tensor.
    """
    frame_length = _STFT_N_FFT
    hop_length = _STFT_HOP
    pad = frame_length // 2
    padded = torch.nn.functional.pad(audio, (pad, pad), mode="constant", value=0.0)
    frames = padded.unfold(0, frame_length, hop_length)
    rms = torch.sqrt((frames**2).mean(dim=1))
    return np.asarray(rms.unsqueeze(0).numpy())


def merge_block_features(target: BlockFeatures, source: BlockFeatures) -> None:
    """
    Merge source block features into target (in place).

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
    """
    Collapse accumulated per-block features into a populated AudioAnalysisData.

    :param accumulated: All block features accumulated during streaming.
    :param sample_rate: Sample rate used during extraction.
    """
    onset_env = np.concatenate([[0.0], np.concatenate(accumulated.onset_env_frames)])
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
        # the model stores plain float lists (numpy-free); convert the analysis arrays
        rms_energy=rms_energy_series.tolist(),
        spectral_centroid=spectral_centroid_series.tolist(),
    )


def _clamp(value: float) -> float:
    """Clamp a float to [0.0, 1.0]."""
    return float(max(0.0, min(1.0, value)))


def _derive_energy(rms: np.ndarray) -> float:
    """
    Compute normalized mean RMS energy in [0, 1].

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    return _clamp(float(rms.mean()))


def _derive_loudness(rms: np.ndarray) -> tuple[float, float]:
    """
    Compute RMS-based loudness approximations (NOT EBU R128).

    Returns (loudness_integrated, loudness_range) as RMS-derived proxies.
    loudness_integrated is mean RMS in dB (not gated LUFS), and loudness_range
    is the std-dev of per-frame RMS in dB (not the gated 95th-10th percentile LRA).

    :param rms: Per-frame RMS values (1D after squeeze).
    """
    rms_clipped = np.clip(rms, 1e-8, None)
    rms_db = 20.0 * np.log10(rms_clipped)
    loudness_integrated = float(rms_db.mean())
    loudness_range = float(rms_db.std())
    return loudness_integrated, loudness_range


def _derive_brightness(centroid: np.ndarray, sample_rate: int) -> float:
    """
    Compute mean spectral centroid normalized against the Nyquist frequency.

    :param centroid: Per-frame spectral centroid values in Hz (1D after squeeze).
    :param sample_rate: Sample rate in Hz.
    """
    nyquist = sample_rate / 2.0
    active_frames = centroid[centroid > 0]
    if len(active_frames) == 0:
        return 0.0
    return _clamp(float(active_frames.mean()) / nyquist)


def _derive_harmonic_complexity(chroma: np.ndarray) -> float:
    """
    Compute normalized Shannon entropy of the mean chroma vector.

    :param chroma: Concatenated chroma feature matrix (12 x N_frames).
    """
    mean_chroma = chroma.mean(axis=1).astype(np.float64)
    chroma_sum = mean_chroma.sum()
    if chroma_sum <= 0:
        return 0.0
    p = mean_chroma / chroma_sum
    p = np.clip(p, 1e-10, None)
    entropy = float(-np.sum(p * np.log(p)))
    max_entropy = float(np.log(12))  # max entropy for 12 bins is ln(12)
    return _clamp(entropy / max_entropy)


def _derive_roughness(contrast: np.ndarray, flatness: np.ndarray) -> float:
    """
    Combine spectral contrast range and spectral flatness into a roughness measure.

    :param contrast: Spectral contrast matrix (7 x N_frames).
    :param flatness: Per-frame spectral flatness values (1D after squeeze).
    """
    contrast_range = float(contrast.max() - contrast.min())
    contrast_score = _clamp(contrast_range / _ROUGHNESS_CONTRAST_MAX_DB)
    flatness_score = _clamp(float(flatness.mean()))
    return _clamp(
        _ROUGHNESS_CONTRAST_WEIGHT * contrast_score + _ROUGHNESS_FLATNESS_WEIGHT * flatness_score
    )


def _derive_rhythmic_regularity(onset_env: np.ndarray, sample_rate: int) -> float:
    """
    Estimate rhythmic regularity as 1 minus the normalized CV of inter-onset intervals.

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
    """
    Interpolate per-frame RMS onto fixed 1800 bins and peak-normalize.

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
    """
    Interpolate per-frame centroid onto fixed 1800 bins, zeroing silent regions.

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
