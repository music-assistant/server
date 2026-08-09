"""Unit tests for AI Radio host normalization and persistence."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.ai_radio.constants import DEFAULT_LLM_INSTRUCTIONS
from music_assistant.providers.ai_radio.hosts import AIRadioHostsMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class DummyHosts(AIRadioHostsMixin, AIRadioStorageMixin):
    """Minimal harness combining host and storage helpers."""

    def __init__(self) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._sections: dict[str, dict[str, Any]] = {}
        self._stations: dict[str, dict[str, Any]] = {}
        self._hosts: dict[str, dict[str, Any]] = {}


def _section(section_id: str, section_type: str = "ai_text") -> dict[str, Any]:
    return {"id": section_id, "name": section_id, "type": section_type, "prompt": "Prompt"}


def _host(section_ids: list[str]) -> dict[str, Any]:
    return {
        "id": "rick",
        "name": "Rick",
        "instructions": "Laid-back evening host.",
        "tts_engine": "",
        "section_ids": section_ids,
        "section_order": [
            {"when": "between_songs", "flow": [{"MUST": section_ids[0]}]},
        ],
    }


def test_normalize_host_returns_known_schema() -> None:
    """Normalize a valid host payload to the known schema."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    normalized = dummy._normalize_host(_host(["Song_Transition"]))
    assert normalized["id"] == "rick"
    assert normalized["name"] == "Rick"
    assert normalized["instructions"] == "Laid-back evening host."
    assert normalized["tts_engine"] == ""
    assert normalized["section_ids"] == ["Song_Transition"]
    assert normalized["merge_section_id"] == ""


def test_normalize_host_defaults_empty_instructions() -> None:
    """Fall back to the default instructions when the payload's are blank."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["instructions"] = "   "
    normalized = dummy._normalize_host(payload)
    assert normalized["instructions"] == DEFAULT_LLM_INSTRUCTIONS


def test_normalize_host_requires_name() -> None:
    """Reject hosts with a blank name."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["name"] = " "
    with pytest.raises(InvalidDataError):
        dummy._normalize_host(payload)


def test_normalize_host_rejects_unknown_section_reference() -> None:
    """Reject hosts that reference shared sections that do not exist."""
    dummy = DummyHosts()
    dummy._sections = {}
    with pytest.raises(InvalidDataError):
        dummy._normalize_host(_host(["Missing_Section"]))


def test_normalize_host_rejects_non_meta_merge_section() -> None:
    """Reject merge sections that do not point to an ai_meta section."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["merge_section_id"] = "Song_Transition"
    with pytest.raises(InvalidDataError):
        dummy._normalize_host(payload)


def test_default_host_template_is_normalizable() -> None:
    """Ensure the built-in host template passes normalization."""
    dummy = DummyHosts()
    defaults = dummy._default_sections_template()
    dummy._sections = {item["id"]: item for item in defaults}
    template = dummy._default_host_template()
    normalized = dummy._normalize_host(template)
    assert normalized["merge_section_id"] == "Between_Songs_Smoother"
