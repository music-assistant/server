"""Shared constants for the config controller package."""

from __future__ import annotations

from typing import TypeVar

from music_assistant_models.config_entries import ConfigValueType

__all__ = [
    "BASE_KEYS",
    "DEFAULT_SAVE_DELAY",
    "PLAYER_QUEUE_CONFIG_OWNER",
    "_ConfigValueT",
]

DEFAULT_SAVE_DELAY = 5

BASE_KEYS = ("enabled", "name", "available", "default_name", "provider", "type")

# owner namespace for per-queue config entry strings (controllers/player_queues/strings.json)
PLAYER_QUEUE_CONFIG_OWNER = "core.player_queues"

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)
