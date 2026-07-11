"""Tests for the Home Assistant provider."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderFeature

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.hass import (
    CONF_AI_TASK_ENTITY,
    CONF_AUTH_TOKEN,
    CONF_TTS_ENTITY,
    CONF_URL,
    CONF_VERIFY_SSL,
    HomeAssistantProvider,
    setup,
)


def _state(entity_id: str, friendly_name: str) -> dict[str, Any]:
    """Return a Home Assistant entity state."""
    return {
        "entity_id": entity_id,
        "state": "idle",
        "attributes": {"friendly_name": friendly_name},
    }


def _config(**values: str) -> MagicMock:
    """Return a provider config with the given persisted values."""
    persisted_values = {
        CONF_URL: "http://homeassistant.local:8123",
        CONF_AUTH_TOKEN: "token",
        CONF_VERIFY_SSL: True,
        CONF_LOG_LEVEL: "GLOBAL",
        **values,
    }
    config = MagicMock()
    config.instance_id = "hass--test"
    config.name = "Home Assistant"
    config.get_value.side_effect = persisted_values.get
    return config


def _mass() -> MagicMock:
    """Return the Music Assistant dependencies used during provider startup."""
    mass = MagicMock()
    mass.cache = MagicMock()
    mass.http_session = MagicMock()
    mass.http_session_no_ssl = MagicMock()

    def close_listener(coro: Coroutine[Any, Any, None]) -> MagicMock:
        coro.close()
        return MagicMock()

    mass.create_task.side_effect = close_listener
    return mass


async def _start_provider(
    states: list[dict[str, Any]], **config_values: str
) -> tuple[HomeAssistantProvider, MagicMock]:
    """Start the provider with a connected mocked Home Assistant client."""
    hass = MagicMock()
    hass.connect = AsyncMock()
    hass.get_states = AsyncMock(return_value=states)
    hass.send_command = AsyncMock(return_value={"response": {"data": "answer"}})
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(_mass(), manifest, _config(**config_values))
        assert isinstance(provider, HomeAssistantProvider)
        await provider.handle_async_init()
    return provider, hass


@pytest.mark.parametrize(
    ("states", "expected_entity"),
    [
        ([_state("ai_task.default", "Default")], "ai_task.default"),
        (
            [
                _state("ai_task.first", "First"),
                _state("ai_task.selected", "Selected"),
            ],
            "ai_task.selected",
        ),
    ],
)
async def test_ai_query_uses_resolved_entity_after_startup(
    states: list[dict[str, Any]], expected_entity: str
) -> None:
    """Advertise AI queries and use the default or explicitly selected entity."""
    config_values = (
        {CONF_AI_TASK_ENTITY: expected_entity} if expected_entity == "ai_task.selected" else {}
    )

    provider, hass = await _start_provider(states, **config_values)
    result = await provider.ai_query("What is this song?")

    assert ProviderFeature.AI_QUERY in provider.supported_features
    assert result == "answer"
    hass.send_command.assert_awaited_once_with(
        "call_service",
        domain="ai_task",
        service="generate_data",
        service_data={
            "task_name": "music_assistant",
            "instructions": "What is this song?",
            "entity_id": expected_entity,
        },
        return_response=True,
    )


@pytest.mark.parametrize(
    "config_values",
    [{}, {CONF_AI_TASK_ENTITY: "ai_task.missing"}],
)
async def test_ai_query_not_advertised_without_entity(config_values: dict[str, str]) -> None:
    """Do not advertise AI queries when Home Assistant has no AI Task entity."""
    provider, _ = await _start_provider([_state("sensor.example", "Example")], **config_values)

    assert ProviderFeature.AI_QUERY not in provider.supported_features


@pytest.mark.parametrize(
    ("states", "expected_entity"),
    [
        ([_state("tts.default", "Default")], "tts.default"),
        (
            [
                _state("tts.first", "First"),
                _state("tts.selected", "Selected"),
            ],
            "tts.selected",
        ),
    ],
)
async def test_tts_uses_resolved_entity_after_startup(
    states: list[dict[str, Any]], expected_entity: str
) -> None:
    """Advertise TTS and use the default or explicitly selected entity."""
    config_values = {CONF_TTS_ENTITY: expected_entity} if expected_entity == "tts.selected" else {}
    provider, _ = await _start_provider(states, **config_values)
    response = AsyncMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"url": "http://homeassistant.local/tts.mp3"}
    post = cast("MagicMock", provider.mass.http_session.post)
    post.return_value.__aenter__.return_value = response

    stream = await provider.get_tts_message("Hello")

    assert ProviderFeature.TTS in provider.supported_features
    assert stream.path == "http://homeassistant.local/tts.mp3"
    post.assert_called_once()
    request = post.call_args
    assert request.args == ("http://homeassistant.local:8123/api/tts_get_url",)
    assert request.kwargs["json"] == {"engine_id": expected_entity, "message": "Hello"}


@pytest.mark.parametrize(
    "config_values",
    [{}, {CONF_TTS_ENTITY: "tts.missing"}],
)
async def test_tts_not_advertised_without_entity(config_values: dict[str, str]) -> None:
    """Do not advertise TTS when Home Assistant has no TTS entity."""
    provider, _ = await _start_provider([_state("sensor.example", "Example")], **config_values)

    assert ProviderFeature.TTS not in provider.supported_features
