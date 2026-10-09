"""Tests for validating the external URL of the webserver."""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.config_entries import CoreConfig

from music_assistant.controllers.webserver.controller import (
    CONF_EXTERNAL_URL,
    WebserverController,
    _is_valid_external_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # the setting is optional
        (None, True),
        ("", True),
        ("https://ma.example.com", True),
        ("http://ma.example.com:8095/", True),
        # a reverse proxy may serve the server under a subpath
        ("https://example.com/music-assistant", True),
        ("https://8.8.8.8", True),
        ("https://[2001:4860:4860::8888]:8443", True),
        # not an http(s) URL
        ("ma.example.com", False),
        ("ftp://ma.example.com", False),
        ("https://", False),
        ("https://[::1", False),
        (8095, False),
        # only reachable on the local network
        ("http://localhost:8095", False),
        ("http://musicassistant:8095", False),
        ("http://musicassistant.local:8095", False),
        ("http://192.168.1.5:8095", False),
        ("http://10.0.0.5", False),
        ("http://127.0.0.1:8095", False),
        ("http://169.254.1.1", False),
        ("http://0.0.0.0", False),
        ("http://[::1]:8095", False),
        ("http://[fd00::5]:8095", False),
        ("http://[fe80::1]", False),
    ],
)
def test_is_valid_external_url(value: str | int | None, expected: bool) -> None:
    """Only empty or http(s) URLs on a host reachable from the internet are accepted."""
    assert _is_valid_external_url(value) is expected


@pytest.mark.parametrize(
    ("stored_value", "expected"),
    [
        ("https://ma.example.com/", "https://ma.example.com"),
        ("http://192.168.1.5:8095", None),
        (None, None),
    ],
)
async def test_external_url_ignores_invalid_stored_value(
    mock_mass: MagicMock, stored_value: str | None, expected: str | None
) -> None:
    """A stored external URL that fails validation is not handed out."""
    webserver = WebserverController(mock_mass)
    with patch(
        "music_assistant.controllers.webserver.controller.get_ip_addresses",
        return_value=["192.168.1.5"],
    ):
        entries = await webserver.get_config_entries()
    raw = {"domain": "webserver", "values": {CONF_EXTERNAL_URL: stored_value}}
    webserver.config = cast("CoreConfig", CoreConfig.parse(entries, raw))

    assert webserver.external_url == expected
