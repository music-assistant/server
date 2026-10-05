"""Tests for FireRed AED vocal activity inference."""

from __future__ import annotations

import math
from unittest.mock import patch

import numpy as np
import pytest
import torch

from music_assistant.providers.smart_fades.vocal_activity import (
    FIRERED_INFERENCE_CONTEXT_FRAMES,
    FIRERED_MAX_INFERENCE_FRAMES,
    FIRERED_PARAMETER_COUNT,
    FireRedFbank,
    infer_firered_chunk,
    load_firered_components,
    quantize_pcm_for_firered,
    split_firered_features,
    vocal_activity_probabilities,
)


def _reference_pcm() -> np.ndarray:
    """Return deterministic normalized PCM derived from signed 16-bit samples."""
    time = np.arange(16000 * 3.2, dtype=np.float64) / 16000
    pcm_int16 = np.rint(
        12000 * np.sin(2 * np.pi * 220 * time) + 4000 * np.sin(2 * np.pi * 997 * time)
    ).astype(np.int16)
    result: np.ndarray = pcm_int16.astype(np.float32) / 32768.0
    return result


def test_model_loads_weights_only_with_expected_parameter_count() -> None:
    """The bundled state dict loads safely into the expected FireRed AED model."""
    with patch(
        "music_assistant.providers.smart_fades.vocal_activity.torch.load",
        wraps=torch.load,
    ) as load:
        model, means, inverse_std = load_firered_components()

    assert load.call_args.kwargs["weights_only"] is True
    assert sum(parameter.numel() for parameter in model.parameters()) == (FIRERED_PARAMETER_COUNT)
    assert means.shape == inverse_std.shape == (80,)


def test_pcm_quantization_matches_reference_int16_scale() -> None:
    """Normalized PCM is rounded and clipped to the FireRed signed 16-bit scale."""
    pcm = np.array(
        [-1.1, -1.0, -0.5, -0.5 / 32768, 0.5 / 32768, 0.5, 1.0, 1.1],
        dtype=np.float32,
    )

    quantized = quantize_pcm_for_firered(pcm)

    np.testing.assert_array_equal(
        quantized,
        np.array(
            [-32768, -32768, -16384, 0, 0, 16384, 32767, 32767],
            dtype=np.float32,
        ),
    )


def test_streaming_fbank_cmvn_matches_reference_across_chunks() -> None:
    """Arbitrary chunk boundaries preserve exact online fbank and CMVN features."""
    pcm = _reference_pcm()
    _, means, inverse_std = load_firered_components()

    one_shot_extractor = FireRedFbank(means, inverse_std)
    one_shot = np.concatenate([one_shot_extractor.process(pcm), one_shot_extractor.finalize()])

    streaming_extractor = FireRedFbank(means, inverse_std)
    blocks = [
        features
        for chunk in np.array_split(pcm, 37)
        if (features := streaming_extractor.process(chunk)).size
    ]
    blocks.append(streaming_extractor.finalize())
    streaming = np.concatenate(blocks)

    np.testing.assert_array_equal(streaming, one_shot)
    assert one_shot.shape == (318, 80)
    np.testing.assert_allclose(
        one_shot[[0, 1, 50, -1]][:, [0, 10, 40, 79]],
        [
            [1.161562, 0.5089607, -2.330163, -1.5592412],
            [0.8259135, 0.50769424, -2.2977846, -1.700644],
            [1.1541193, 0.50818205, -2.3833172, -1.7334999],
            [0.9781029, 0.5056236, -2.3772168, -1.7556578],
        ],
        rtol=1e-4,
        atol=1e-6,
    )


def test_deterministic_probability_parity() -> None:
    """Bundled inference matches deterministic FireRed reference probabilities."""
    pcm = _reference_pcm()
    model, means, inverse_std = load_firered_components()
    extractor = FireRedFbank(means, inverse_std)
    features = np.concatenate([extractor.process(pcm), extractor.finalize()])

    probabilities = infer_firered_chunk(model, features)
    vocal_activity = vocal_activity_probabilities(probabilities, 3.2)

    np.testing.assert_allclose(
        probabilities[[0, 1, 50, 150, -1]],
        [
            [8.8199326e-05, 6.3637628e-05, 1.9249251e-01],
            [8.2634397e-05, 6.1347811e-05, 1.5615767e-01],
            [5.5029866e-04, 5.3342991e-04, 9.9471587e-01],
            [5.2045441e-05, 4.0918807e-05, 9.9911863e-01],
            [5.1496401e-05, 1.5160468e-04, 9.8695940e-01],
        ],
        rtol=1e-4,
        atol=1e-6,
    )
    np.testing.assert_allclose(
        vocal_activity[:5],
        [8.7163455e-05, 2.1780634e-04, 4.3790121e-04, 5.1874004e-04, 5.9412332e-04],
        rtol=1e-4,
        atol=1e-7,
    )


@pytest.mark.parametrize(
    ("frame_count", "expected"),
    [
        (FIRERED_MAX_INFERENCE_FRAMES, [(30_000, 0, 30_000)]),
        (FIRERED_MAX_INFERENCE_FRAMES + 1, [(30_001, 0, 30_000), (161, 160, 1)]),
    ],
)
def test_long_input_split_boundaries(
    frame_count: int,
    expected: list[tuple[int, int, int]],
) -> None:
    """Long inference adds and discards the required context at the 30k boundary."""
    features = np.zeros((frame_count, 80), dtype=np.float32)

    chunks = [
        (len(chunk), core_offset, core_length)
        for chunk, core_offset, core_length in split_firered_features(features)
    ]

    assert FIRERED_INFERENCE_CONTEXT_FRAMES == 160
    assert chunks == expected


def test_long_input_split_matches_full_inference_at_boundary() -> None:
    """Context-trimmed chunks preserve full-model output at the split boundary."""
    model, _, _ = load_firered_components()
    # Keep the split far enough from the end that context cannot span the full input.
    features = np.random.default_rng(7).normal(size=(30_500, 80)).astype(np.float32)

    full = infer_firered_chunk(model, features)
    split = np.concatenate(
        [
            infer_firered_chunk(model, chunk)[core_offset : core_offset + core_length]
            for chunk, core_offset, core_length in split_firered_features(features)
        ]
    )

    # Different tensor lengths cause minor float32 non-associativity.
    np.testing.assert_allclose(split, full, atol=2e-6)


def test_vocal_timeline_uses_max_speech_singing_and_exact_length() -> None:
    """Each 100 ms value averages max(speech, singing) and pads the snipped tail."""
    frame_probabilities = np.zeros((18, 3), dtype=np.float32)
    frame_probabilities[:10, 0] = np.linspace(0.0, 0.9, 10)
    frame_probabilities[:10, 1] = np.linspace(0.9, 0.0, 10)
    frame_probabilities[10:, 0] = 1.2
    frame_probabilities[10:, 1] = -0.2

    probabilities = vocal_activity_probabilities(frame_probabilities, 0.21)

    expected_first = np.maximum(
        frame_probabilities[:10, 0],
        frame_probabilities[:10, 1],
    ).mean()
    assert len(probabilities) == math.ceil(0.21 / 0.1)
    assert probabilities[0] == pytest.approx(expected_first)
    assert probabilities[1:].tolist() == pytest.approx([1.0, 1.0])
    assert np.isfinite(probabilities).all()
    assert np.all((probabilities >= 0.0) & (probabilities <= 1.0))


def test_vocal_timeline_rejects_non_finite_model_output() -> None:
    """Non-finite FireRed output cannot reach persisted extra_data."""
    frame_probabilities = np.zeros((10, 3), dtype=np.float32)
    frame_probabilities[4, 1] = np.nan

    with pytest.raises(ValueError, match="non-finite"):
        vocal_activity_probabilities(frame_probabilities, 0.1)
