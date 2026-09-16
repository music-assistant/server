"""Packed codec for AudioAnalysisData: header + binary payload round trips."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from music_assistant.controllers.streams.audio_analysis_codec import (
    ARRAY_FIELDS,
    F16_FIELDS,
    decode,
    encode,
)
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.models.audio_analysis import AudioAnalysisData


def _full_record() -> AudioAnalysisData:
    rng = np.random.default_rng(1)
    series = rng.random(1800).astype(np.float32).tolist()
    return AudioAnalysisData(
        duration=231.5,
        bpm=124.9,
        beats=np.cumsum(np.full(480, 0.48, dtype=np.float32)).tolist(),
        downbeats=np.cumsum(np.full(120, 1.92, dtype=np.float32)).tolist(),
        beats_per_bar=4,
        key="F#",
        mode="minor",
        rms_energy=series,
        spectral_centroid=(rng.random(1800) * 8000).astype(np.float32).tolist(),
        vocal_activity=series,
        band_rms_low=series,
        band_rms_low_mid=series,
        band_rms_mid=series,
        band_rms_high=series,
        clap_embedding=rng.standard_normal(1024).astype(np.float32).tolist(),
        energy=0.62,
        extra_data={"acoustid": "abc"},
    )


def test_array_fields_match_the_model() -> None:
    """ARRAY_FIELDS lists the model's list[float] fields in declaration order."""
    assert ARRAY_FIELDS == (
        "beats",
        "downbeats",
        "rms_energy",
        "spectral_centroid",
        "vocal_activity",
        "band_rms_low",
        "band_rms_low_mid",
        "band_rms_mid",
        "band_rms_high",
        "clap_embedding",
    )
    assert frozenset(ARRAY_FIELDS) - {"beats", "downbeats"} == F16_FIELDS


def test_round_trip_preserves_scalars_and_float32_arrays_exactly() -> None:
    """Scalars and beat/downbeat timestamps survive encode/decode exactly."""
    original = _full_record()
    header, payload = encode(original)
    restored = decode(header, payload)
    assert restored.bpm == original.bpm
    assert restored.key == "F#"
    assert restored.extra_data == {"acoustid": "abc"}
    assert restored.beats == original.beats
    assert restored.downbeats == original.downbeats
    assert isinstance(restored.rms_energy, list)


def test_float16_fields_round_trip_within_tolerance() -> None:
    """Envelope and embedding fields tolerate float16 quantization within 0.1%."""
    original = _full_record()
    restored = decode(*encode(original))
    for name in F16_FIELDS:
        got = np.asarray(getattr(restored, name), dtype=np.float32)
        want = np.asarray(getattr(original, name), dtype=np.float32)
        assert got.shape == want.shape
        assert np.max(np.abs(got - want) / np.maximum(np.abs(want), 1e-3)) < 1e-3


def test_header_is_small_and_lists_every_array() -> None:
    """The header stays small and indexes every array; None fields are omitted."""
    header, payload = encode(_full_record())
    doc = json_loads(header)
    assert len(header) < 2000
    assert [entry[0] for entry in doc["arrays"]] == list(ARRAY_FIELDS)
    assert doc["arrays"][0][1] == "f32"  # beats
    assert doc["arrays"][2][1] == "f16"  # rms_energy
    assert sum(entry[3] for entry in doc["arrays"]) == len(payload)
    assert "rms_energy" not in doc  # arrays never appear as JSON lists
    assert "valence" not in doc  # None fields are omitted


def test_scalar_only_record_has_empty_payload() -> None:
    """A record with no arrays produces an empty payload."""
    header, payload = encode(AudioAnalysisData(loudness_integrated=-11.2, true_peak=-0.3))
    assert payload == b""
    restored = decode(header, payload)
    assert restored.loudness_integrated == -11.2
    assert restored.beats is None


def test_decode_skips_unknown_array_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """An array index entry for an unknown field is skipped, with a logged warning."""
    header, payload = encode(AudioAnalysisData(beats=[1.0, 2.0]))
    doc = json_loads(header)
    doc["arrays"].append(["no_such_field", "f32", 0, 8])

    with caplog.at_level(logging.WARNING):
        restored = decode(json_dumps(doc), payload)
    assert restored.beats == [1.0, 2.0]
    assert "no_such_field" in caplog.text


def test_decode_rejects_truncated_payload() -> None:
    """A payload shorter than the header's array index raises ValueError."""
    header, payload = encode(AudioAnalysisData(beats=[1.0, 2.0, 3.0]))
    with pytest.raises(ValueError):  # noqa: PT011
        decode(header, payload[:4])
