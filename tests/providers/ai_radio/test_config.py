"""Unit tests for AI Radio provider config entries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from music_assistant.providers.ai_radio.config import get_config_entries
from music_assistant.providers.ai_radio.provider import AIRadioProvider


def _make_mass(base_url: str, locale: str = "en_US") -> Any:
    """Build a minimal mass stub exposing webserver.base_url and metadata.locale."""
    return cast(
        "Any",
        SimpleNamespace(
            webserver=SimpleNamespace(base_url=base_url),
            metadata=SimpleNamespace(locale=locale),
        ),
    )


def test_web_ui_url_entry_carries_runtime_url_via_translation_params() -> None:
    """Web UI URL config entry must render via translation_params, not description."""
    mass = _make_mass("http://localhost:8095")

    entries = asyncio.run(get_config_entries(mass))

    entry = next(entry for entry in entries if entry.key == "web_ui_url")
    assert entry.translation_params == ["http://localhost:8095/#/ai-radio"]
    assert entry.description is None


def test_provider_instance_exposes_the_same_config_entries() -> None:
    """
    The options page reads the instance method, not the module-level setup hook.

    Without the override the base class returns an empty tuple, so the provider's own
    options silently vanish from the settings UI.
    """
    mass = _make_mass("http://localhost:8095")
    provider = cast("Any", AIRadioProvider.__new__(AIRadioProvider))
    provider.mass = mass
    # instance_id is a read-only property backed by the provider config
    provider.config = SimpleNamespace(instance_id="ai_radio")

    entries = asyncio.run(provider.get_config_entries())

    assert {entry.key for entry in entries} == {
        entry.key for entry in asyncio.run(get_config_entries(mass, "ai_radio"))
    }
    assert "timezone" in {entry.key for entry in entries}
