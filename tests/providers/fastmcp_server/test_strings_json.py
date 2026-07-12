"""Drift guard between ``provider/strings.json`` and the de-literalized config schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fastmcp_server import config as _config
from music_assistant.providers.fastmcp_server.config import build_config_entries

if TYPE_CHECKING:
    from unittest.mock import MagicMock

# Categories that MA core resolves from its own shared translation set, so they
# need no entry in this provider's ``config_categories``.
COMMON_CATEGORIES = {"server", "debug", "generic", "advanced"}

# Locate strings.json next to the config module so the path holds both here and
# when the provider is inlined upstream (``provider`` -> the inlined package).
STRINGS_PATH = Path(_config.__file__).resolve().parent / "strings.json"


def _load_strings() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))
    return data


def test_strings_json_is_valid_with_required_keys() -> None:
    """``strings.json`` is valid JSON exposing ``config_entries`` and ``config_categories``."""
    data = _load_strings()
    assert isinstance(data, dict)
    assert "config_entries" in data
    assert "config_categories" in data


def test_every_category_is_known_or_declared(mock_mass: MagicMock) -> None:
    """Each category emitted by the schema is a common one or declared in ``config_categories``."""
    data = _load_strings()
    declared = set(data["config_categories"])
    for entry in build_config_entries(mock_mass, {}):
        category = getattr(entry, "category", None)
        if not category:
            continue
        assert category in COMMON_CATEGORIES or category in declared, (
            f"category {category!r} is neither common nor declared in config_categories"
        )


def test_static_entries_carry_no_inline_text(mock_mass: MagicMock) -> None:
    """
    All static entries are de-literalized — ``strings.json`` owns their text.

    Only ``LABEL``-type entries may carry inline text: their content is composed
    at runtime (e.g. the endpoint info label embeds the live ``base_url``).
    """
    for entry in build_config_entries(mock_mass, {}):
        if entry.type is ConfigEntryType.LABEL:
            continue
        assert entry.label is None, f"inline label on {entry.key!r}"
        assert entry.description is None, f"inline description on {entry.key!r}"


def test_deliteralized_entries_have_strings(mock_mass: MagicMock) -> None:
    """Every static entry with ``label is None`` has full label+description text in strings.json."""
    data = _load_strings()
    config_entries = data["config_entries"]
    for entry in build_config_entries(mock_mass, {}):
        if entry.label is not None:
            continue
        assert entry.key in config_entries, f"missing strings.json entry for {entry.key!r}"
        text = config_entries[entry.key]
        assert text.get("label"), f"empty label for {entry.key!r}"
        assert text.get("description"), f"empty description for {entry.key!r}"
