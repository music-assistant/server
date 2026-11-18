"""Readers for the WLED Audio Sync playback settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

from .constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_SCALING_MODE,
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
    DEFAULT_SCALING_MODE,
    ScalingMode,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


class WledSettings(NamedTuple):
    """The playback settings of a WLED sync zone."""

    latency_ms: int
    gain_db: float
    scaling_mode: ScalingMode


def get_settings(config: ProviderConfig) -> WledSettings:
    """
    Return the playback settings stored in a provider config.

    :param config: Provider config to read the settings from.
    """
    return WledSettings(
        latency_ms=cast("int", config.get_value(CONF_LATENCY_MS, DEFAULT_LATENCY_MS)),
        gain_db=cast("float", config.get_value(CONF_GAIN_DB, DEFAULT_GAIN_DB)),
        scaling_mode=ScalingMode(
            cast("str", config.get_value(CONF_SCALING_MODE, DEFAULT_SCALING_MODE))
        ),
    )
