"""
Tests for how a config save reports a value that a config entry rejects.

The API client is told which setting holds the rejected value, by its label and in its own
language, and the server handles the rejection as an expected error rather than a crash.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.api import CommandMessage
from music_assistant_models.errors import InvalidConfigValueError

from music_assistant.constants import CONF_PUBLISH_IP
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_HOSTNAME = "homeassistant.local"
_SAVE_HOSTNAME_AS_PUBLISH_IP = CommandMessage(
    message_id="1",
    command="config/core/save",
    args={"domain": "streams", "values": {CONF_PUBLISH_IP: _HOSTNAME}},
)


async def test_a_rejected_value_is_refused_and_not_stored(mass: MusicAssistant) -> None:
    """A hostname as the published IP address is refused with a typed error and not stored."""
    stored_before = mass.config.get_raw_core_config_value("streams", CONF_PUBLISH_IP)

    with pytest.raises(InvalidConfigValueError, match=CONF_PUBLISH_IP):
        await mass.config.save_core_config("streams", {CONF_PUBLISH_IP: _HOSTNAME})

    assert mass.config.get_raw_core_config_value("streams", CONF_PUBLISH_IP) == stored_before


async def test_a_rejected_value_names_the_setting_by_its_label(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The client is told which setting is wrong, and the server logs no unexpected error."""
    reply = await _send_save(mass, locale=None)

    assert reply["error_code"] == InvalidConfigValueError.error_code
    assert reply["details"] == "The value for Published IP address is not valid."
    server_problems = [
        record.levelno
        for record in caplog.records
        if record.name == mass.webserver.logger.name and record.levelno >= logging.WARNING
    ]
    assert server_problems == [logging.WARNING]


async def test_a_rejected_value_reads_in_the_language_of_the_client(mass: MusicAssistant) -> None:
    """Both the message and the label of the setting are in the language of the client."""
    mass.translations._locales["nl"] = {
        "common.errors.invalid_config_value": "De waarde voor {0} is ongeldig.",
        "core.streams.config_entries.publish_ip.label": "Gepubliceerd IP-adres",
    }

    reply = await _send_save(mass, locale="nl")

    assert reply["details"] == "De waarde voor Gepubliceerd IP-adres is ongeldig."


async def _send_save(mass: MusicAssistant, locale: str | None) -> dict[str, Any]:
    """
    Save a hostname as the published IP address through the websocket API.

    :param mass: The running server.
    :param locale: The language the client declared, or None for the source language.
    """
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client = WebsocketClientHandler(mass.webserver, request)
    client._locale = locale
    handler = mass.command_handlers[_SAVE_HOSTNAME_AS_PUBLISH_IP.command]
    await client._run_handler(handler, _SAVE_HOSTNAME_AS_PUBLISH_IP)
    message = client._to_write.get_nowait()
    assert message is not None
    reply: dict[str, Any] = json_loads(message)
    return reply
