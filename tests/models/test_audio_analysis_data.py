"""AudioAnalysisData: typed array fields and the legacy extra_data lift."""

from __future__ import annotations

from typing import Any

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


def test_non_dict_extra_data_is_left_alone() -> None:
    """A non-dict extra_data (malformed row) is left untouched, not coerced or raised on."""
    data = AudioAnalysisData(extra_data="garbage")  # type: ignore[arg-type]
    extra_data: Any = data.extra_data
    assert extra_data == "garbage"
    assert data.clap_embedding is None
    assert data.vocal_activity is None
    assert data.band_rms_low is None
