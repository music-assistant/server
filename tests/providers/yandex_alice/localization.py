"""Helpers for asserting against the provider's ``strings.json`` texts."""

from __future__ import annotations

import importlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_strings() -> dict[str, Any]:
    """Parse the provider ``strings.json`` once per test session."""
    # Resolve through the provider package so the lookup works in both
    # the source tree (provider/) and the upstream-synced layout
    # (music_assistant/providers/yandex_alice/).
    provider_pkg = importlib.import_module("music_assistant.providers.yandex_alice")
    strings_path = Path(str(provider_pkg.__file__)).parent / "strings.json"
    data: dict[str, Any] = json.loads(strings_path.read_text(encoding="utf-8"))
    return data


def entry_text(key: str, field: str = "label") -> str:
    """
    Return ``config_entries.<key>.<field>`` from ``strings.json``.

    :param key: The config entry key (or explicit translation key).
    :param field: The authored field: ``label`` / ``description`` / ``action_label``.
    :raises KeyError: When the key or field is not authored.
    """
    text: str = load_strings()["config_entries"][key][field]
    return text


def authored_texts(entry: Any) -> dict[str, str]:
    """
    Return the ``strings.json`` texts a config entry resolves to (``{}`` if unauthored).

    :param entry: A ConfigEntry-like object; its ``translation_key`` (or ``key``
        as the structural default) selects the ``config_entries`` record,
        mirroring MA's serialization-time lookup.
    """
    translation_key = getattr(entry, "translation_key", None) or entry.key
    texts: dict[str, str] = load_strings()["config_entries"].get(translation_key, {})
    return texts
