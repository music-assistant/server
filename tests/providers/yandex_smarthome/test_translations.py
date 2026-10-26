"""Translation coverage for the guided setup flow."""

from __future__ import annotations

import json
from pathlib import Path

from music_assistant.providers.yandex_smarthome import setup_flow


def test_setup_flow_translation_keys_are_complete() -> None:
    """Every setup step and surfaced error has owner-provided English text."""
    strings = json.loads(Path(setup_flow.__file__).with_name("strings.json").read_text())

    assert set(strings["setup_flow"]) >= {
        "user",
        "registering",
        "skill_method",
        "skill_id",
        "creating_skill",
        "device_login",
        "skill_token",
        "fetching_otp",
        "cloud_otp",
        "cloud_confirm",
        "abort",
    }
    assert set(strings["errors"]) >= {
        "direct_requires_https",
        "skill_token_required",
        "no_borrowed_token",
    }
    assert "shown by Music Assistant" in strings["setup_flow"]["cloud_confirm"]["description"]
