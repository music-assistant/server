"""AudioAnalysisData: typed array fields, the legacy extra_data lift and the energy score."""

from __future__ import annotations

from typing import Any

import pytest

from music_assistant.models.audio_analysis import AudioAnalysisData


def test_typed_array_fields_round_trip() -> None:
    """Typed array fields survive a to_dict/from_dict round trip."""
    data = AudioAnalysisData(
        clap_embedding=[0.1] * 1024,
        vocal_activity=[0.5] * 1800,
        band_rms_low=[0.1] * 1800,
        band_rms_low_mid=[0.2] * 1800,
        band_rms_mid=[0.3] * 1800,
        band_rms_high=[0.4] * 1800,
    )
    restored = AudioAnalysisData.from_dict(data.to_dict())
    assert restored.clap_embedding == data.clap_embedding
    assert restored.band_rms_high == data.band_rms_high
    assert restored.extra_data is None


def test_legacy_extra_data_is_lifted_into_fields() -> None:
    """A legacy row with arrays under extra_data decodes into the typed fields."""
    legacy = {
        "bpm": 120.0,
        "extra_data": {
            "clap_embedding": [0.1] * 1024,
            "vocal_activity": [0.5] * 1800,
            "band_rms": {
                "low": [0.1] * 1800,
                "low_mid": [0.2] * 1800,
                "mid": [0.3] * 1800,
                "high": [0.4] * 1800,
            },
            "unrelated": "kept",
        },
    }
    data = AudioAnalysisData.from_dict(legacy)
    assert data.clap_embedding == [0.1] * 1024
    assert data.vocal_activity == [0.5] * 1800
    assert data.band_rms_mid == [0.3] * 1800
    assert data.extra_data == {"unrelated": "kept"}


def test_lift_leaves_no_empty_extra_data() -> None:
    """extra_data is None once every key it held has been lifted."""
    data = AudioAnalysisData.from_dict({"extra_data": {"vocal_activity": [0.0] * 1800}})
    assert data.vocal_activity == [0.0] * 1800
    assert data.extra_data is None


def test_typed_field_wins_over_legacy_duplicate() -> None:
    """When both are set, the typed field wins and the legacy duplicate is discarded."""
    data = AudioAnalysisData(
        vocal_activity=[1.0] * 1800, extra_data={"vocal_activity": [0.0] * 1800}
    )
    assert data.vocal_activity == [1.0] * 1800
    assert data.extra_data is None


def test_partial_band_rms_is_lifted_field_by_field() -> None:
    """A partial legacy band_rms dict lifts only the bands it contains."""
    data = AudioAnalysisData.from_dict({"extra_data": {"band_rms": {"low": [0.1] * 1800}}})
    assert data.band_rms_low == [0.1] * 1800
    assert data.band_rms_high is None
    assert data.extra_data is None


def test_update_merges_new_fields() -> None:
    """update() merges the new typed array fields alongside existing scalar fields."""
    base = AudioAnalysisData(bpm=100.0)
    base.update(AudioAnalysisData(clap_embedding=[0.2] * 1024))
    assert base.bpm == 100.0
    assert base.clap_embedding == [0.2] * 1024


@pytest.mark.parametrize("smart_fades_first", [True, False])
def test_update_keeps_both_providers_arrays(smart_fades_first: bool) -> None:
    """A legacy smart_fades row and a typed CLAP row keep each other's arrays in either order."""
    smart_fades = AudioAnalysisData.from_dict(
        {
            "extra_data": {
                "vocal_activity": [0.5] * 1800,
                "band_rms": {"low": [0.1] * 1800, "high": [0.4] * 1800},
            }
        }
    )
    sonic = AudioAnalysisData(clap_embedding=[0.2] * 1024)
    first, second = (smart_fades, sonic) if smart_fades_first else (sonic, smart_fades)

    first.update(second)

    assert first.clap_embedding == [0.2] * 1024
    assert first.vocal_activity == [0.5] * 1800
    assert first.band_rms_low == [0.1] * 1800
    assert first.band_rms_high == [0.4] * 1800


def test_non_dict_extra_data_is_left_alone() -> None:
    """A non-dict extra_data (malformed row) is left untouched, not coerced or raised on."""
    data = AudioAnalysisData(extra_data="garbage")  # type: ignore[arg-type]
    extra_data: Any = data.extra_data
    assert extra_data == "garbage"
    assert data.clap_embedding is None
    assert data.vocal_activity is None
    assert data.band_rms_low is None


def test_lift_keeps_non_list_legacy_values() -> None:
    """A non-list legacy value (the old wrapped vocal contract) is kept in extra_data."""
    wrapped = {"model": "some_other_model", "probabilities": [0.9] * 1800}
    data = AudioAnalysisData.from_dict({"extra_data": {"vocal_activity": wrapped}})
    assert data.vocal_activity is None
    assert data.extra_data == {"vocal_activity": wrapped}


def test_lift_keeps_unknown_band_rms_entries() -> None:
    """A band_rms dict with an unrecognized band name keeps that band in extra_data."""
    data = AudioAnalysisData.from_dict(
        {"extra_data": {"band_rms": {"low": [0.1] * 1800, "sub": [0.2] * 1800}}}
    )
    assert data.band_rms_low == [0.1] * 1800
    assert data.extra_data == {"band_rms": {"sub": [0.2] * 1800}}


@pytest.mark.parametrize(
    ("brightness", "arousal", "loudness", "score", "level"),
    [
        (0.1404, 0.2405, -11.4158, 0.0, 4),
        (0.2, 0.6, -8.0, 1.0796, 9),
        (0.07, 0.1, -18.0, -1.4136, 1),
        (0.3, 0.95, -5.0, 2.5302, 10),
        (0.0, 0.0, -40.0, -2.7237, 1),
        (1.0, 1.0, 0.0, 3.0, 10),
    ],
)
def test_energy_score_and_level(
    brightness: float, arousal: float, loudness: float, score: float, level: int
) -> None:
    """energy_score and energy_level match the reference values."""
    data = AudioAnalysisData(brightness=brightness, arousal=arousal, loudness_integrated=loudness)
    assert data.energy_score == pytest.approx(score, abs=1e-3)
    assert data.energy_level == level


@pytest.mark.parametrize("missing", ["brightness", "arousal", "loudness_integrated"])
def test_energy_score_needs_all_inputs(missing: str) -> None:
    """Without brightness, arousal or loudness_integrated there is no score or level."""
    values: dict[str, Any] = {"brightness": 0.2, "arousal": 0.6, "loudness_integrated": -8.0}
    values[missing] = None
    data = AudioAnalysisData(**values)
    assert data.energy_score is None
    assert data.energy_level is None


def test_energy_score_is_not_serialised() -> None:
    """energy_score and energy_level are derived, not stored."""
    data = AudioAnalysisData(brightness=0.2, arousal=0.6, loudness_integrated=-8.0)
    serialised = data.to_dict()
    assert "energy_score" not in serialised
    assert "energy_level" not in serialised
