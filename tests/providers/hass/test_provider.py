"""Tests for the Home Assistant provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hass_client.exceptions import BaseHassClientError
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import SetupFailedError

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


def _mock_state_http(mass: MagicMock, states: list[dict[str, Any]]) -> None:
    """Serve the given states via the mocked REST ``/api/states/<entity_id>`` endpoint."""
    states_by_id = {state["entity_id"]: state for state in states}

    def _get(url: str, headers: dict[str, str] | None = None) -> MagicMock:  # noqa: ARG001
        entity_id = url.rsplit("/api/states/", 1)[-1]
        state = states_by_id.get(entity_id)
        response = MagicMock()
        response.status = 200 if state is not None else 404
        response.json = AsyncMock(return_value=state)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    mass.http_session.get.side_effect = _get


def _mass() -> MagicMock:
    """Return the Music Assistant dependencies used during provider startup."""
    mass = MagicMock()
    mass.cache = MagicMock()
    mass.http_session = MagicMock()
    mass.http_session_no_ssl = MagicMock()
    mass.create_task.side_effect = asyncio.create_task
    return mass


class _HomeAssistantClient:
    """Provide lifecycle-aware Home Assistant behavior for provider tests."""

    def __init__(
        self,
        states: list[dict[str, Any]],
        registry_error: Exception | None = None,
        listener_error: Exception | None = None,
        connect_error: Exception | None = None,
        block_registry: bool = False,
    ) -> None:
        self.connected = False
        self.disconnected = False
        self.listener_started = asyncio.Event()
        self.listener_cancelled = asyncio.Event()
        self.listener_stopped = asyncio.Event()
        self.registry_started = asyncio.Event()
        self.registry_cancelled = asyncio.Event()
        self.registry_stopped = asyncio.Event()
        self.calls: list[str] = []
        self._states = states
        self._registry_error = registry_error
        self._listener_error = listener_error
        self._connect_error = connect_error
        self._registry_result = (
            asyncio.get_running_loop().create_future() if block_registry else None
        )
        self.send_command = AsyncMock(return_value={"response": {"data": "answer"}})

    async def connect(self) -> None:
        """Connect the client."""
        self.calls.append("connect")
        if self._connect_error:
            raise self._connect_error
        self.connected = True

    async def start_listening(self) -> None:
        """Listen until the provider stops the listener task."""
        self.calls.append("start_listening")
        self.listener_started.set()
        try:
            if self._listener_error:
                raise self._listener_error
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.listener_cancelled.set()
            raise
        finally:
            if self._registry_result and not self._registry_result.done():
                self._registry_result.cancel()
            self.listener_stopped.set()

    async def get_entity_registry(self) -> list[dict[str, Any]]:
        """Return the entity registry after command responses can be received."""
        await self.listener_started.wait()
        self.calls.append("get_entity_registry")
        self.registry_started.set()
        try:
            if self._registry_result:
                await self._registry_result
            if self._registry_error:
                raise self._registry_error
            return [{"entity_id": state["entity_id"]} for state in self._states]
        except asyncio.CancelledError:
            self.registry_cancelled.set()
            raise
        finally:
            self.registry_stopped.set()

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self.calls.append("disconnect")
        self.connected = False
        self.disconnected = True


@asynccontextmanager
async def _start_provider(
    states: list[dict[str, Any]], **config_values: str
) -> AsyncIterator[tuple[HomeAssistantProvider, _HomeAssistantClient]]:
    """Start the provider with a connected mocked Home Assistant client."""
    hass = _HomeAssistantClient(states)
    mass = _mass()
    _mock_state_http(mass, states)
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(mass, manifest, _config(**config_values))
        assert isinstance(provider, HomeAssistantProvider)
        async with asyncio.timeout(1):
            await provider.handle_async_init()
    try:
        yield provider, hass
    finally:
        await provider.unload()


async def test_feature_resolution_starts_listener_first() -> None:
    """Resolve startup features only after the Home Assistant listener starts."""
    states = [
        _state("ai_task.default", "Default AI"),
        _state("tts.default", "Default TTS"),
    ]

    async with _start_provider(states) as (provider, hass):
        assert hass.calls[:3] == ["connect", "start_listening", "get_entity_registry"]
        assert ProviderFeature.AI_QUERY in provider.supported_features
        assert ProviderFeature.TTS in provider.supported_features


async def test_feature_resolution_failure_cleans_up_connection() -> None:
    """Clean up the listener and connection when feature resolution fails."""
    hass = _HomeAssistantClient([], BaseHassClientError("Unable to load Home Assistant states"))
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(_mass(), manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)

        with pytest.raises(SetupFailedError, match="Unable to load Home Assistant states"):
            async with asyncio.timeout(1):
                await provider.handle_async_init()

    assert hass.listener_started.is_set()
    assert hass.listener_stopped.is_set()
    assert hass.disconnected
    assert hass.calls == ["connect", "start_listening", "get_entity_registry", "disconnect"]
    assert provider._listen_task is None


async def test_listener_failure_does_not_mask_feature_resolution_failure() -> None:
    """Preserve the startup error when the listener also fails."""
    hass = _HomeAssistantClient(
        [],
        BaseHassClientError("Unable to load Home Assistant states"),
        RuntimeError("Listener failed"),
    )
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(_mass(), manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)

        with pytest.raises(SetupFailedError, match="Unable to load Home Assistant states"):
            async with asyncio.timeout(1):
                await provider.handle_async_init()

    assert hass.disconnected
    assert provider._listen_task is None


async def test_listener_exit_terminates_pending_feature_resolution() -> None:
    """Fail startup when the listener exits while feature resolution is pending."""
    hass = _HomeAssistantClient(
        [],
        listener_error=BaseHassClientError("Listener failed"),
        block_registry=True,
    )
    mass = _mass()
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(mass, manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)

        with pytest.raises(SetupFailedError, match="listener stopped during startup"):
            async with asyncio.timeout(1):
                await provider.handle_async_init()

    assert hass.listener_started.is_set()
    assert hass.listener_stopped.is_set()
    assert hass.registry_stopped.is_set()
    assert hass.disconnected
    assert provider._listen_task is None
    mass.call_later.assert_not_called()


async def test_feature_resolution_timeout_cleans_up_connection() -> None:
    """Clean up startup when Home Assistant feature resolution times out."""
    hass = _HomeAssistantClient([], block_registry=True)
    mass = _mass()
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with (
        patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass),
        patch("music_assistant.providers.hass.FEATURE_DISCOVERY_TIMEOUT", 0.1),
    ):
        provider = await setup(mass, manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)
        init_task = asyncio.create_task(provider.handle_async_init())
        async with asyncio.timeout(1):
            await hass.registry_started.wait()

        with pytest.raises(
            SetupFailedError, match="Timed out while resolving Home Assistant feature entities"
        ):
            await init_task

    assert hass.registry_stopped.is_set()
    assert hass.registry_cancelled.is_set()
    assert hass.listener_stopped.is_set()
    assert hass.listener_cancelled.is_set()
    assert hass.disconnected
    assert provider._listen_task is None
    mass.call_later.assert_not_called()


async def test_feature_resolution_cancellation_cleans_up_connection() -> None:
    """Clean up startup when Home Assistant initialization is cancelled."""
    hass = _HomeAssistantClient([], block_registry=True)
    mass = _mass()
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(mass, manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)
        init_task = asyncio.create_task(provider.handle_async_init())
        async with asyncio.timeout(1):
            await hass.registry_started.wait()
        init_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await init_task

    assert hass.registry_stopped.is_set()
    assert hass.registry_cancelled.is_set()
    assert hass.listener_stopped.is_set()
    assert hass.listener_cancelled.is_set()
    assert hass.disconnected
    assert provider._listen_task is None
    mass.call_later.assert_not_called()


async def test_connection_failure_cleans_up_client() -> None:
    """Clean up the client when connecting to Home Assistant fails."""
    hass = _HomeAssistantClient([], connect_error=BaseHassClientError("Unable to connect"))
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(_mass(), manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)

        with pytest.raises(SetupFailedError, match="Unable to connect"):
            await provider.handle_async_init()

    assert hass.disconnected
    assert provider._listen_task is None


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

    async with _start_provider(states, **config_values) as (provider, hass):
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
    async with _start_provider([_state("sensor.example", "Example")], **config_values) as (
        provider,
        _,
    ):
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
    async with _start_provider(states, **config_values) as (provider, _):
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
    async with _start_provider([_state("sensor.example", "Example")], **config_values) as (
        provider,
        _,
    ):
        assert ProviderFeature.TTS not in provider.supported_features
