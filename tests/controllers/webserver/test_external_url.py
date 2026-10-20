"""Tests for validating the external URL of the webserver."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

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
        ("https://ma.example.com:notaport", False),
        (8095, False),
        # links are built by appending to the URL
        ("https://ma.example.com/#/home", False),
        ("https://ma.example.com/?foo=bar", False),
        # only reachable on the local network
        ("http://localhost:8095", False),
        ("http://musicassistant:8095", False),
        ("http://musicassistant.local:8095", False),
        ("http://musicassistant.local.:8095", False),
        ("http://ha.home.arpa:8123", False),
        ("http://ma.localhost:8095", False),
        ("https://ma.internal", False),
        ("http://homeassistant.lan:8123", False),
        ("http://MusicAssistant.Home", False),
        ("http://127.0.0.1.", False),
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


@pytest.mark.parametrize(
    ("stored_value", "cleared"),
    [
        ("http://192.168.1.5:8095", True),
        ("https://ma.example.com", False),
        (None, False),
    ],
)
async def test_setup_clears_invalid_stored_external_url(
    mock_mass: MagicMock, tmp_path: Path, stored_value: str | None, cleared: bool
) -> None:
    """Setup clears an invalid stored external URL, so it is reported only once."""
    mock_mass.config.get_raw_core_config_value.side_effect = lambda _domain, key, default=None: (
        stored_value if key == CONF_EXTERNAL_URL else default
    )
    webserver = WebserverController(mock_mass)
    webserver._server = MagicMock(setup=AsyncMock(), port=8095, bind_ip=None)
    webserver.auth = MagicMock(setup=AsyncMock())
    webserver.remote_access = MagicMock(setup=AsyncMock())
    config_values: dict[str, Any] = {"bind_port": 8095, "bind_ip": None, "enable_ssl": False}
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: config_values.get(key, default)

    with (
        patch(
            "music_assistant.controllers.webserver.controller.get_publish_ip_candidates",
            AsyncMock(return_value=("192.168.1.5",)),
        ),
        patch(
            "music_assistant.controllers.webserver.controller.locate_frontend",
            return_value=str(tmp_path),
        ),
    ):
        await webserver.setup(cast("CoreConfig", config))

    calls = [c.args for c in mock_mass.config.set_raw_core_config_value.call_args_list]
    assert (("webserver", CONF_EXTERNAL_URL, None) in calls) is cleared
