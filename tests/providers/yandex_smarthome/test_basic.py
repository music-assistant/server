"""Basic tests for Yandex Smart Home plugin provider."""

from __future__ import annotations

import json
from pathlib import Path


def test_manifest_valid() -> None:
    """Manifest should be valid JSON with required fields."""
    manifest_path = Path(__file__).parent.parent / "provider" / "manifest.json"
    data = json.loads(manifest_path.read_text())

    assert data["type"] == "plugin"
    assert data["domain"] == "yandex_smarthome"
    assert data["name"] == "Yandex Smart Home"
    assert data["stage"] == "alpha"
    assert data["multi_instance"] is False
    assert data["builtin"] is False
    assert isinstance(data["requirements"], list)
    assert "aiohttp>=3.9.0" in data["requirements"]


def test_manifest_has_codeowners() -> None:
    """Manifest should declare codeowners."""
    manifest_path = Path(__file__).parent.parent / "provider" / "manifest.json"
    data = json.loads(manifest_path.read_text())

    assert "codeowners" in data
    assert len(data["codeowners"]) > 0


def test_constants_defined() -> None:
    """Core constants should be importable and non-empty."""
    from music_assistant.providers.yandex_smarthome.constants import (
        CONF_CLOUD_INSTANCE_PASSWORD,
        CONF_INSTANCE_NAME,
        YANDEX_DEVICE_TYPE_RECEIVER,
    )

    assert CONF_INSTANCE_NAME
    assert CONF_CLOUD_INSTANCE_PASSWORD
    assert YANDEX_DEVICE_TYPE_RECEIVER


def test_cloud_plus_constants() -> None:
    """Cloud Plus constants should be importable and well-formed."""
    from music_assistant.providers.yandex_smarthome.constants import (
        CLOUD_SKILL_WEBHOOK_TEMPLATE,
        CONF_SKILL_TOKEN,
        CONNECTION_TYPE_CLOUD_PLUS,
        YANDEX_DIALOGS_CALLBACK_BASE,
        YANDEX_DIALOGS_DEVELOPER_URL,
        YANDEX_OAUTH_URL,
    )

    assert CONNECTION_TYPE_CLOUD_PLUS == "cloud_plus"
    assert CONF_SKILL_TOKEN == "skill_token"
    assert "dialogs.yandex.net" in YANDEX_DIALOGS_CALLBACK_BASE
    assert "dialogs.yandex.ru" in YANDEX_DIALOGS_DEVELOPER_URL
    assert "oauth.yandex.ru" in YANDEX_OAUTH_URL
    assert "yaha-cloud.ru" in CLOUD_SKILL_WEBHOOK_TEMPLATE


def test_constants_capability_types() -> None:
    """Yandex capability constants should be properly defined."""
    from music_assistant.providers.yandex_smarthome.constants import (
        CAPABILITY_ON_OFF,
        CAPABILITY_RANGE,
        CAPABILITY_TOGGLE,
        INSTANCE_MUTE,
        INSTANCE_PAUSE,
        INSTANCE_VOLUME,
    )

    assert "on_off" in CAPABILITY_ON_OFF
    assert "range" in CAPABILITY_RANGE
    assert "toggle" in CAPABILITY_TOGGLE
    assert INSTANCE_VOLUME == "volume"
    assert INSTANCE_MUTE == "mute"
    assert INSTANCE_PAUSE == "pause"
