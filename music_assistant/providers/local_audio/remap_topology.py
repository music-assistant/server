"""
PulseAudio remap-sink topology computation for multi-channel cards.

For any output sink with more than 2 channels, computes the set of
module-remap-sink.c sinks local_audio should create:
  - one stereo "zone" sink per recognized channel pair present in the
    master's channel map (front/rear/side stereo, center+sub), always
    exposed to clients as front-left,front-right regardless of which
    physical channels they pull from, and
  - one full-passthrough sink (same channel count and channel map as the
    master) for sinks with 6+ channels, giving "play to all outputs"
    behavior with independent hardware volume control.

This mirrors the channel-pair definitions and naming convention previously
implemented by an external PulseAudio stereo-pairs addon, so that existing
sink names/layouts are preserved when local_audio takes over creating them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# Recognized stereo zone pairs: suffix -> (master channel 1, master channel 2).
# A zone is created only if BOTH channels are present in the master's
# channel map. The zone sink always exposes front-left,front-right to
# clients regardless of which physical channels it pulls from.
STEREO_PAIRS: Final[dict[str, tuple[str, str]]] = {
    "front_stereo": ("front-left", "front-right"),
    "rear_stereo": ("rear-left", "rear-right"),
    "side_stereo": ("side-left", "side-right"),
    "center_sub": ("front-center", "lfe"),
}

# Minimum master channel count for a full-passthrough "multichannel stereo"
# sink (named after the AVR "Multi Channel Stereo" / "All Channel Stereo"
# mode — plays the same content to all output pairs).
SURROUND_PASSTHROUGH_MIN_CHANNELS: Final = 6

ZONE_CHANNEL_MAP: Final[tuple[str, str]] = ("front-left", "front-right")


@dataclass(frozen=True, slots=True)
class RemapSinkSpec:
    """A single module-remap-sink.c sink to create."""

    sink_name: str
    master_channel_map: tuple[str, ...]
    channel_map: tuple[str, ...]
    channels: int


def normalize_card_name(alsa_card_name: str, card_index: str | None = None) -> str:
    """
    Normalize an alsa.card_name property into a sink-name prefix.

    Mirrors `tr ' -' '_' | tr -cd '[:alnum:]_'`, e.g. "Creative X-Fi" ->
    "Creative_X_Fi", "HD-Audio Generic" -> "HD_Audio_Generic".

    :param card_index: Optional ALSA card index (from alsa.card property).
        When provided, appended as ``_card{N}`` suffix to disambiguate
        identical cards (e.g. two X-Fi cards become ``Creative_X_Fi_card0``
        and ``Creative_X_Fi_card3``).
    """
    name = re.sub(r"[ -]", "_", alsa_card_name)
    name = re.sub(r"[^A-Za-z0-9_]", "", name)
    if card_index is not None:
        name = f"{name}_card{card_index}"
    return name


def compute_remap_topology(
    card_name: str, channel_map: list[str], max_output_channels: int
) -> list[RemapSinkSpec]:
    """
    Compute the remap-sink topology for one multi-channel master sink.

    :param card_name: Normalized card name prefix (see normalize_card_name),
        used as f"{card_name}_{suffix}" for each created sink.
    :param channel_map: The master sink's PA channel map (channel position
        names, e.g. ["front-left", "front-right", "rear-left", ...]).
    :param max_output_channels: The master sink's channel count.
    :returns: List of RemapSinkSpec to create. Empty if the master is
        already stereo or smaller (channels <= 2) — nothing to remap.
    """
    if max_output_channels <= 2:
        return []

    channel_set = set(channel_map)
    specs: list[RemapSinkSpec] = []

    for suffix, (ch_a, ch_b) in STEREO_PAIRS.items():
        if ch_a in channel_set and ch_b in channel_set:
            specs.append(
                RemapSinkSpec(
                    sink_name=f"{card_name}_{suffix}",
                    master_channel_map=(ch_a, ch_b),
                    channel_map=ZONE_CHANNEL_MAP,
                    channels=2,
                )
            )

    if max_output_channels >= SURROUND_PASSTHROUGH_MIN_CHANNELS:
        specs.append(
            RemapSinkSpec(
                sink_name=f"{card_name}_multichannel_stereo",
                master_channel_map=tuple(channel_map),
                channel_map=tuple(channel_map),
                channels=max_output_channels,
            )
        )

    return specs


def build_remap_sink_argument(spec: RemapSinkSpec, master_sink_name: str) -> str:
    """Build the module-remap-sink.c argument string for a RemapSinkSpec."""
    return (
        f"sink_name={spec.sink_name} "
        f"master={master_sink_name} "
        f"sink_properties=device.description={spec.sink_name} "
        f"channels={spec.channels} "
        f"master_channel_map={','.join(spec.master_channel_map)} "
        f"channel_map={','.join(spec.channel_map)} "
        f"remix=no"
    )
