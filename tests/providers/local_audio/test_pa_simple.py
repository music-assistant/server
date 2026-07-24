"""Tests for the local_audio pa_simple helper module."""

from __future__ import annotations

import logging
import subprocess
from typing import Final

import pytest

from music_assistant.helpers.ffmpeg import _CHANNEL_LAYOUT
from music_assistant.providers.local_audio.pa_simple import (
    _SOURCE_CHANNEL_ORDER,
    build_channel_remap_index,
    remap_pcm_channels,
)

_LOGGER: Final = logging.getLogger(__name__)

# FFmpeg channel token -> PulseAudio position name.
_TOKEN_TO_PULSE_NAME: Final[dict[str, str]] = {
    "FL": "front-left",
    "FR": "front-right",
    "FC": "front-center",
    "LFE": "lfe",
    "BL": "rear-left",
    "BR": "rear-right",
    "BC": "rear-center",
    "SL": "side-left",
    "SR": "side-right",
}


def _ffmpeg_layout_order(layout: str) -> list[str]:
    """Return FFmpeg's own channel order for a layout name, as PulseAudio names."""
    output = subprocess.run(
        ["ffmpeg", "-hide_banner", "-layouts"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == layout:
            return [_TOKEN_TO_PULSE_NAME[token] for token in parts[1].split("+")]
    raise AssertionError(f"layout {layout} not reported by ffmpeg -layouts")


# -- channel table agreement --


def test_channel_tables_cover_the_same_counts() -> None:
    """
    Both channel tables must describe exactly the same channel counts.

    helpers/ffmpeg.py writes the channel order and pa_simple.py interprets it,
    so a count present in only one of them means audio is written in an order
    nothing knows how to interpret.
    """
    assert set(_SOURCE_CHANNEL_ORDER) == set(_CHANNEL_LAYOUT)


@pytest.mark.parametrize("channels", sorted(_CHANNEL_LAYOUT))
def test_source_channel_order_matches_ffmpeg_layout(channels: int) -> None:
    """Each source order must match FFmpeg's real order for the selected layout."""
    layout = _CHANNEL_LAYOUT[channels]
    assert _SOURCE_CHANNEL_ORDER[channels] == _ffmpeg_layout_order(layout)


@pytest.mark.parametrize("channels", sorted(_SOURCE_CHANNEL_ORDER))
def test_source_channel_order_is_well_formed(channels: int) -> None:
    """Every source order lists exactly as many distinct positions as its count."""
    order = _SOURCE_CHANNEL_ORDER[channels]
    assert len(order) == channels
    assert len(set(order)) == channels


# -- build_channel_remap_index --


def test_remap_index_none_when_physical_order_unknown() -> None:
    """No physical map means no remap can be computed."""
    assert build_channel_remap_index(6, None, logger=_LOGGER) is None
    assert build_channel_remap_index(6, [], logger=_LOGGER) is None


def test_remap_index_none_when_orders_already_match() -> None:
    """An identical physical order needs no remap."""
    assert build_channel_remap_index(6, list(_SOURCE_CHANNEL_ORDER[6]), logger=_LOGGER) is None


def test_remap_index_none_for_unknown_channel_count() -> None:
    """A channel count with no known source order is refused rather than guessed."""
    assert build_channel_remap_index(9, ["front-left"] * 9, logger=_LOGGER) is None


def test_remap_index_none_on_length_mismatch() -> None:
    """A physical map of the wrong length is refused rather than guessed."""
    assert build_channel_remap_index(6, ["front-left", "front-right"], logger=_LOGGER) is None


def test_remap_index_none_on_unknown_position_names() -> None:
    """Positions outside the source vocabulary can't be matched up."""
    physical = ["front-left", "front-right", "front-center", "lfe", "side-left", "side-right"]
    assert build_channel_remap_index(6, physical, logger=_LOGGER) is None


def test_remap_index_swapped_centre_and_lfe() -> None:
    """A device reporting LFE before front-center yields a swap of those two slots."""
    physical = [
        "front-left",
        "front-right",
        "lfe",
        "front-center",
        "rear-left",
        "rear-right",
    ]
    assert build_channel_remap_index(6, physical, logger=_LOGGER) == [0, 1, 3, 2, 4, 5]


def test_remap_index_applies_physical_position_aliases() -> None:
    """HD Audio RLC/RRC positions are aliased onto the source side pair."""
    physical = [
        "front-left",
        "front-right",
        "front-center",
        "lfe",
        "rear-left-of-center",
        "rear-right-of-center",
        "rear-left",
        "rear-right",
    ]
    assert build_channel_remap_index(8, physical, logger=_LOGGER) == [0, 1, 2, 3, 6, 7, 4, 5]


def test_remap_index_aliases_can_still_be_a_no_op() -> None:
    """Aliased positions that land in source order need no remap."""
    physical = [
        "front-left",
        "front-right",
        "front-center",
        "lfe",
        "rear-left",
        "rear-right",
        "rear-left-of-center",
        "rear-right-of-center",
    ]
    assert build_channel_remap_index(8, physical, logger=_LOGGER) is None


@pytest.mark.parametrize("channels", sorted(_SOURCE_CHANNEL_ORDER))
def test_remap_index_is_a_permutation(channels: int) -> None:
    """A reversed physical order produces a full permutation of the source slots."""
    reversed_order = list(reversed(_SOURCE_CHANNEL_ORDER[channels]))
    index = build_channel_remap_index(channels, reversed_order, logger=_LOGGER)
    if channels == 1:
        # a single channel can never be reordered
        assert index is None
        return
    assert index is not None
    assert sorted(index) == list(range(channels))


# -- remap_pcm_channels --


def test_remap_pcm_reorders_each_frame() -> None:
    """Every frame is reordered by the index and the length is preserved."""
    index = [0, 1, 3, 2, 4, 5]
    data = bytes(range(6)) * 3  # 3 frames, 1 byte per sample
    result = remap_pcm_channels(data, channels=6, bytes_per_sample=1, index=index)
    assert len(result) == len(data)
    assert list(result) == [0, 1, 3, 2, 4, 5] * 3


def test_remap_pcm_identity_index_returns_input() -> None:
    """An identity index leaves the data untouched."""
    data = bytes(range(12))
    result = remap_pcm_channels(data, channels=6, bytes_per_sample=1, index=[0, 1, 2, 3, 4, 5])
    assert result == data


def test_remap_pcm_moves_whole_samples_not_bytes() -> None:
    """Multi-byte samples are moved intact, not split."""
    # 2 channels, 2 bytes per sample: frame 1122|3344 -> swapped 3344|1122
    data = bytes([0x11, 0x22, 0x33, 0x44])
    result = remap_pcm_channels(data, channels=2, bytes_per_sample=2, index=[1, 0])
    assert list(result) == [0x33, 0x44, 0x11, 0x22]


def test_remap_pcm_passes_partial_trailing_frame_through() -> None:
    """A partial trailing frame is passed through unchanged, preserving length."""
    index = [0, 1, 3, 2, 4, 5]
    data = bytes(range(6)) * 2 + bytes([0xFF, 0xEE])
    result = remap_pcm_channels(data, channels=6, bytes_per_sample=1, index=index)
    assert len(result) == len(data)
    assert list(result[-2:]) == [0xFF, 0xEE]
    assert list(result[:12]) == [0, 1, 3, 2, 4, 5] * 2


def test_remap_pcm_empty_input() -> None:
    """Empty input produces empty output rather than raising."""
    assert remap_pcm_channels(b"", channels=6, bytes_per_sample=2, index=[0, 1, 2, 3, 4, 5]) == b""


def test_remap_pcm_round_trip_restores_original() -> None:
    """Applying a permutation and then its inverse returns the original bytes."""
    index = [0, 1, 3, 2, 4, 5]
    inverse = [index.index(i) for i in range(6)]
    data = bytes(range(6)) * 4
    once = remap_pcm_channels(data, channels=6, bytes_per_sample=1, index=index)
    twice = remap_pcm_channels(once, channels=6, bytes_per_sample=1, index=inverse)
    assert twice == data
