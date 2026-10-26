"""Tests for the publish_base_url override that lets the stream server sit behind a proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from music_assistant.constants import (
    CONF_PUBLISH_BASE_URL,
    CONF_PUBLISH_IP,
    CONF_VALUE_AUTO,
)
from music_assistant.controllers.streams.controller import (
    StreamsController,
    _is_valid_publish_base_url,
)

from .conftest import setup_streams

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant

LOOPBACK_IP = "127.0.0.1"
PROXY_URL = "https://musicstream.example.com"
PROXY_URL_WITH_PORT = "https://musicstream.example.com:8443"


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        # unset / default
        (None, True),
        ("", True),
        # straightforward valid URLs
        ("https://musicstream.example.com", True),
        ("http://musicstream.example.com", True),
        ("https://musicstream.example.com:8443", True),
        ("http://192.168.1.10:8097", True),
        # IPv6 literal in URL form
        ("http://[fd00::10]:8097", True),
        # a bare trailing slash is tolerated (stripped at use time)
        ("https://musicstream.example.com/", True),
        # unsupported schemes
        ("ftp://musicstream.example.com", False),
        ("ws://musicstream.example.com", False),
        # missing scheme or host
        ("musicstream.example.com", False),
        ("https://", False),
        # malformed authority
        ("https://host:", False),  # empty port after hostname
        ("https://[::1]:", False),  # empty port after IPv6 literal
        ("https://host:abc", False),  # non-integer port
        ("  https://host  ", False),  # surrounding whitespace
        # userinfo would leak credentials into every stream URL and log line
        ("https://user:pass@host", False),
        ("https://user@host", False),
        # non-root path, query, fragment all forbidden
        ("https://host/music", False),
        ("https://host//", False),
        ("https://host/?x=1", False),
        ("https://host#frag", False),
        # wrong type
        (123, False),
        (True, False),
    ],
)
def test_publish_base_url_validator(value: object, accepted: bool) -> None:
    """The validator admits only unset or well-formed http(s) base URLs."""
    assert _is_valid_publish_base_url(value) is accepted  # type: ignore[arg-type]


async def test_publish_base_url_unset_falls_back_to_ip_base_url(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """Without the override set, the base URL stays http://<publish_ip>:<port> (unchanged behavior)."""
    await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert streams_controller.base_url == f"http://{LOOPBACK_IP}:{streams_controller.publish_port}"


async def test_publish_base_url_overrides_ip_derived_base_url(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """When set to a valid URL, base_url is that URL verbatim regardless of publish_ip."""
    mass_minimal.config.set_raw_core_config_value(
        StreamsController.domain, CONF_PUBLISH_BASE_URL, PROXY_URL
    )
    await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert streams_controller.base_url == PROXY_URL
    # publish_ip is still resolved for discovery consumers
    assert streams_controller.publish_ip == LOOPBACK_IP


async def test_publish_base_url_with_explicit_port_is_kept(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A non-default port in the override URL is preserved."""
    mass_minimal.config.set_raw_core_config_value(
        StreamsController.domain, CONF_PUBLISH_BASE_URL, PROXY_URL_WITH_PORT
    )
    await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert streams_controller.base_url == PROXY_URL_WITH_PORT


async def test_publish_base_url_trailing_slash_is_stripped(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """A tolerated trailing slash is removed so composed URLs don't contain `//`."""
    mass_minimal.config.set_raw_core_config_value(
        StreamsController.domain, CONF_PUBLISH_BASE_URL, PROXY_URL + "/"
    )
    await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert streams_controller.base_url == PROXY_URL


async def test_publish_base_url_coexists_with_ipv6_publish_ip(
    streams_controller: StreamsController, mass_minimal: MusicAssistant
) -> None:
    """The override does not disturb IPv6 resolution of publish_ip for discovery."""
    mass_minimal.config.set_raw_core_config_value(
        StreamsController.domain, CONF_PUBLISH_BASE_URL, PROXY_URL
    )
    await setup_streams(
        streams_controller, mass_minimal, bind_ip="::", all_ip_addresses=("fd00::10",)
    )

    assert streams_controller.publish_ip == "fd00::10"
    assert streams_controller.base_url == PROXY_URL


async def test_stored_invalid_publish_base_url_is_reset_with_warning(
    streams_controller: StreamsController,
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored value that fails validation (e.g. after an upgrade) is warned and cleared."""
    # stored raw, so we bypass the save-time validate= path and emulate an upgrade
    mass_minimal.config.set_raw_core_config_value(
        StreamsController.domain, CONF_PUBLISH_BASE_URL, "https://user:pass@host"
    )

    with caplog.at_level("WARNING", logger=streams_controller.logger.name):
        await setup_streams(streams_controller, mass_minimal, bind_ip=LOOPBACK_IP)

    assert any(
        "publish_base_url" not in record.message
        and "Published base URL" in record.message
        and "not a valid URL" in record.message
        for record in caplog.records
    )
    assert (
        mass_minimal.config.get_raw_core_config_value(
            StreamsController.domain, CONF_PUBLISH_BASE_URL
        )
        is None
    )
    # falls back to the IP-derived form once cleared
    assert streams_controller.base_url == f"http://{LOOPBACK_IP}:{streams_controller.publish_port}"


@pytest.mark.parametrize(
    ("value", "rejected"),
    [
        (None, False),
        ("", False),
        ("https://musicstream.example.com", False),
        ("http://192.168.1.10:8097", False),
        ("https://musicstream.example.com/", False),
        ("ftp://musicstream.example.com", True),
        ("musicstream.example.com", True),
        ("https://host:", True),
        ("https://user:pass@host", True),
        ("https://host/music", True),
    ],
)
async def test_publish_base_url_entry_rejects_invalid_at_save(
    streams_controller: StreamsController,
    mass_minimal: MusicAssistant,
    value: str | None,
    rejected: bool,
) -> None:
    """The ConfigEntry's validate= callback blocks invalid values at save time."""
    config = await mass_minimal.config.get_core_config(streams_controller.domain)

    # ensure publish_ip stays valid so only publish_base_url is under test
    update: dict[str, ConfigValueType] = {
        CONF_PUBLISH_IP: CONF_VALUE_AUTO,
        CONF_PUBLISH_BASE_URL: value,
    }

    if rejected:
        with pytest.raises(ValueError, match="not a valid value"):
            config.update(update)
    else:
        config.update(update)
