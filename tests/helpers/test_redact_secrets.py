"""Tests for hiding secrets in API messages before they are logged."""

from __future__ import annotations

import pytest

from music_assistant.helpers.api import redact_json_secrets, redact_secrets
from music_assistant.helpers.json import json_dumps, json_loads

# made-up values, none of them is a real credential
FAKE_PASSWORD = "made-up-password"
FAKE_JWT = "eyJmYWtl.eyJmYWtlLXBheWxvYWQ.ZmFrZS1zaWduYXR1cmU"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "sxm_password",
        "token",
        "access_token",
        "client_secret",
        "api_key",
        "app_key",
        "ssl_private_key",
        "cookie",
        "Authorization",
    ],
)
def test_secret_key_value_is_hidden(key: str) -> None:
    """A string under a secret-looking key is replaced by the placeholder."""
    assert redact_secrets({key: FAKE_PASSWORD}) == {key: "<redacted>"}


@pytest.mark.parametrize("key", ["token_id", "author", "translation_key", "username"])
def test_look_alike_key_value_is_kept(key: str) -> None:
    """A key that only resembles a secret keeps its value."""
    assert redact_secrets({key: "visible"}) == {key: "visible"}


def test_shape_and_other_values_are_kept() -> None:
    """The keys, the other values and the absence of a secret stay readable."""
    args = {
        "share_type": "cifs",
        "server": "nas.local",
        "username": "someone",
        "password": FAKE_PASSWORD,
        "version": None,
        "read_only": False,
        "credentials": {"user": "someone", "pin": ""},
    }
    assert redact_secrets(args) == {
        "share_type": "cifs",
        "server": "nas.local",
        "username": "someone",
        "password": "<redacted>",
        "version": None,
        "read_only": False,
        "credentials": {"user": "<redacted>", "pin": ""},
    }


def test_config_values_strings_are_hidden() -> None:
    """Every string of a config values map is hidden, whatever its key."""
    args = {
        "provider_domain": "demo",
        "values": {"identity": FAKE_PASSWORD, "port": 4533, "enabled": True, "tags": ["a"]},
    }
    assert redact_secrets(args) == {
        "provider_domain": "demo",
        "values": {"identity": "<redacted>", "port": 4533, "enabled": True, "tags": ["<redacted>"]},
    }


def test_jwt_is_hidden_anywhere() -> None:
    """A JWT is hidden under any key, also inside a longer string."""
    data = {"result": FAKE_JWT, "items": [f"Bearer {FAKE_JWT}"]}
    assert redact_secrets(data) == {"result": "<redacted>", "items": ["Bearer <redacted>"]}


def test_json_message_is_redacted() -> None:
    """A JSON message comes back as JSON with its secrets hidden."""
    message = json_dumps(
        {"message_id": "1", "command": "auth/login", "args": {"password": FAKE_PASSWORD}}
    )
    result = redact_json_secrets(message.encode())
    assert json_loads(result) == {
        "message_id": "1",
        "command": "auth/login",
        "args": {"password": "<redacted>"},
    }


def test_json_message_without_secrets_is_unchanged() -> None:
    """A message that holds no secret is returned as it is, also with look-alike keys."""
    message = '{"message_id": "1", "result": {"author": "Someone", "token_id": "abc"}}'
    assert redact_json_secrets(message) == message


def test_invalid_json_that_may_hold_a_secret_is_hidden() -> None:
    """Text that is no valid JSON is hidden as a whole when it could hold a secret."""
    assert redact_json_secrets(f'{{"password": "{FAKE_PASSWORD}"') == "<redacted>"
