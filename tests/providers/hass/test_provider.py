"""Tests for the Home Assistant provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from math import ceil
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError
from hass_client.exceptions import BaseHassClientError
from music_assistant_models.enums import EventType, ProviderFeature
from music_assistant_models.errors import (
    MusicAssistantError,
    SetupFailedError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.hass import (
    CONF_AUTH_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    STATE_FETCH_BATCH_SIZE,
    HassRegistryEntity,
    HomeAssistantProvider,
    setup,
)
from music_assistant.providers.hass.constants import (
    CONF_MUTE_CONTROLS,
    CONF_POWER_CONTROLS,
    CONF_VOLUME_CONTROLS,
)
from tests.common import use_real_create_task

LAST_CHANGED = 1683832716.072648
LAST_CHANGED_ISO = "2023-05-11T19:18:36.072648+00:00"
CONTEXT_ID = "01H0640ES8JCY1NGTNW3V41T5T"
REGISTRY_LIST_COMMAND = "config/entity_registry/list_for_display"
REGISTRY_ENTRIES_COMMAND = "config/entity_registry/get_entries"
DEVICE_REGISTRY_LIST = "get_device_registry"


def _state(entity_id: str, friendly_name: str) -> dict[str, Any]:
    """Return a Home Assistant entity state."""
    return {
        "entity_id": entity_id,
        "state": "idle",
        "attributes": {"friendly_name": friendly_name},
    }


def _compressed(state: dict[str, Any]) -> dict[str, Any]:
    """Return the compressed form Home Assistant sends for the given entity state."""
    return {
        "s": state["state"],
        "a": state["attributes"],
        "lc": LAST_CHANGED,
        "c": CONTEXT_ID,
    }


def _config(**values: Any) -> MagicMock:
    """Return a provider config exposing the given values via get_value (entry defaults)."""
    persisted_values = {
        CONF_URL: "http://homeassistant.local:8123",
        CONF_AUTH_TOKEN: "token",
        CONF_VERIFY_SSL: True,
        CONF_LOG_LEVEL: "GLOBAL",
        CONF_POWER_CONTROLS: [],
        CONF_MUTE_CONTROLS: [],
        CONF_VOLUME_CONTROLS: [],
        **values,
    }
    config = MagicMock()
    config.instance_id = "hass--test"
    config.name = "Home Assistant"
    config.get_value.side_effect = persisted_values.get
    # get_setup_value falls through to config.values/get_value when setup_data is empty
    config.values = {}
    return config


class _Cache:
    """Provide the slice of the cache controller that @use_cache relies on."""

    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}
        self.fresh = True

    async def get_with_freshness(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        """Return the (data, is_fresh, found) triplet for the given key."""
        # the real controller reads the cache database here, so yield like it does:
        # @use_cache stores in the background, and only a yield lets that store land
        await asyncio.sleep(0)
        if key not in self.entries:
            return None, False, False
        if not self.fresh and not kwargs.get("include_expired"):
            return None, False, False
        return self.entries[key], self.fresh, True

    async def set(self, key: str, data: Any, **kwargs: Any) -> None:
        """Store data under the given key."""
        # the real controller serializes on a worker thread and then writes the cache
        # database, so a store lands well after the call that scheduled it returned
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.entries[key] = data

    def expire_all(self) -> None:
        """Mark every stored entry as no longer fresh."""
        self.fresh = False


def _mass() -> MagicMock:
    """Return the Music Assistant dependencies used during provider startup."""
    mass = MagicMock()
    mass.cache = _Cache()
    mass.http_session = MagicMock()
    mass.http_session_no_ssl = MagicMock()
    use_real_create_task(mass)
    mass.players.register_or_update_player_control = AsyncMock()
    # get_setup_value reads the (empty, here) live setup_data blob from the store, then
    # falls through to the provider config mock's get_value for the persisted test values
    mass.config.get = MagicMock(return_value={})
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
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
        self.subscribed = asyncio.Event()
        # entity_ids per subscribe_entities call and per invoked unsubscribe callable
        self.subscriptions: list[list[str]] = []
        self.unsubscribed: list[list[str]] = []
        self.active_subscriptions = 0
        # (event_type, callback) per subscribe_events call
        self.event_subscriptions: list[tuple[str, Callable[[dict[str, Any]], None]]] = []
        self.active_event_subscriptions = 0
        # compressed states keyed by entity_id, as delivered over the websocket
        self.compressed_states = {state["entity_id"]: _compressed(state) for state in states}
        # entities Home Assistant reports as disabled, so leaves out of the registry listing
        self.disabled_entities: set[str] = set()
        # devices as returned in full by the device registry listing
        self.devices: list[dict[str, Any]] = []
        # events delivered ahead of a subscription's initial state message
        self.leading_events: list[dict[str, Any]] = []
        self.deliver_initial_states = True
        self._registry_error = registry_error
        self._listener_error = listener_error
        self._connect_error = connect_error
        self._registry_result = (
            asyncio.get_running_loop().create_future() if block_registry else None
        )
        # resolve this to make an already running listener return, as a lost connection does
        self.connection_lost: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.send_command = AsyncMock(side_effect=self._send_command)

    async def connect(self) -> None:
        """Connect the client."""
        self.calls.append("connect")
        if self._connect_error:
            raise self._connect_error
        self.connected = True

    async def start_listening(self) -> None:
        """Listen until the connection is lost or the provider stops the listener task."""
        self.calls.append("start_listening")
        self.listener_started.set()
        try:
            if self._listener_error:
                raise self._listener_error
            await self.connection_lost
        except asyncio.CancelledError:
            self.listener_cancelled.set()
            raise
        finally:
            if self._registry_result and not self._registry_result.done():
                self._registry_result.cancel()
            self.listener_stopped.set()

    async def subscribe_entities(
        self, cb_func: Callable[[dict[str, Any]], None], entity_ids: list[str]
    ) -> Callable[[], None]:
        """Deliver the subscription's state messages and return the unsubscribe callable."""
        self.calls.append("subscribe_entities")
        self.subscriptions.append(list(entity_ids))
        self.active_subscriptions += 1
        self.subscribed.set()
        loop = asyncio.get_running_loop()
        for event in self.leading_events:
            loop.call_soon(cb_func, event)
        if self.deliver_initial_states:
            initial = {
                entity_id: self.compressed_states[entity_id]
                for entity_id in entity_ids
                if entity_id in self.compressed_states
            }
            loop.call_soon(cb_func, {"a": initial})

        def _unsubscribe() -> None:
            self.calls.append("unsubscribe_entities")
            self.unsubscribed.append(list(entity_ids))
            self.active_subscriptions -= 1

        return _unsubscribe

    async def subscribe_events(
        self, cb_func: Callable[[dict[str, Any]], None], event_type: str
    ) -> Callable[[], None]:
        """Register the event callback after command responses can be received."""
        await self.listener_started.wait()
        self.calls.append("subscribe_events")
        self.event_subscriptions.append((event_type, cb_func))
        self.active_event_subscriptions += 1

        def _unsubscribe() -> None:
            self.calls.append("unsubscribe_events")
            self.active_event_subscriptions -= 1

        return _unsubscribe

    async def get_device_registry(self) -> list[dict[str, Any]]:
        """Return the full device registry listing."""
        self.calls.append(DEVICE_REGISTRY_LIST)
        return list(self.devices)

    def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Deliver an event to every subscriber of the given event type."""
        for subscribed_type, cb_func in self.event_subscriptions:
            if subscribed_type == event_type:
                cb_func({"event_type": event_type, "data": data})

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self.calls.append("disconnect")
        self.connected = False
        self.disconnected = True

    async def _send_command(self, command: str, **kwargs: Any) -> Any:
        """Return the response Home Assistant sends for the given websocket command."""
        if command == REGISTRY_LIST_COMMAND:
            return await self._registry_for_display()
        self.calls.append(command)
        if command == REGISTRY_ENTRIES_COMMAND:
            return self._registry_entries(cast("list[str]", kwargs["entity_ids"]))
        return {"response": {"data": "answer"}}

    async def _registry_for_display(self) -> dict[str, Any]:
        """Return the entity registry listing after command responses can be received."""
        await self.listener_started.wait()
        self.calls.append(REGISTRY_LIST_COMMAND)
        self.registry_started.set()
        try:
            if self._registry_result:
                await self._registry_result
            if self._registry_error:
                raise self._registry_error
            return {
                "entity_categories": {},
                "entities": [
                    {"ei": entity_id, "pl": "test"}
                    for entity_id in self.compressed_states
                    if entity_id not in self.disabled_entities
                ],
            }
        except asyncio.CancelledError:
            self.registry_cancelled.set()
            raise
        finally:
            self.registry_stopped.set()

    def _registry_entries(self, entity_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Return the full registry entry of every requested entity, None when unknown."""
        return {
            entity_id: (
                {
                    "entity_id": entity_id,
                    "id": f"registry_id_{entity_id}",
                    "platform": "test",
                    "device_id": "device_id",
                    "config_entry_id": "config_entry_id",
                }
                if entity_id in self.compressed_states
                else None
            )
            for entity_id in entity_ids
        }


@asynccontextmanager
async def _start_provider(
    states: list[dict[str, Any]], **config_values: Any
) -> AsyncIterator[tuple[HomeAssistantProvider, _HomeAssistantClient]]:
    """Start the provider with a connected mocked Home Assistant client."""
    hass = _HomeAssistantClient(states)
    mass = _mass()
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


async def _wait_for_stored(provider: HomeAssistantProvider) -> None:
    """Wait until the background store of the device listing has landed in the cache."""
    cache = cast("_Cache", provider.mass.cache)
    async with asyncio.timeout(1):
        while not cache.entries:
            await asyncio.sleep(0)


def _hold_back_registry_fetch(hass: _HomeAssistantClient) -> asyncio.Future[None]:
    """Hold back the next registry listing and return the future that releases it."""
    registry_response: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    hass._registry_result = registry_response
    # the startup fetch left the event set, so re-arm it for the fetch under test
    hass.registry_started.clear()
    return registry_response


async def _wait_for_registry_fetch(hass: _HomeAssistantClient) -> None:
    """Wait until the held back registry listing is in flight."""
    async with asyncio.timeout(1):
        await hass.registry_started.wait()


def _registry_event(
    entity_id: str, action: str = "update", changes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the data Home Assistant sends in an entity_registry_updated event."""
    data: dict[str, Any] = {"action": action, "entity_id": entity_id}
    if action == "update":
        # an update carries the old value of every field it touched
        data["changes"] = changes or {}
    return data


async def _fire_registry_update(
    provider: HomeAssistantProvider,
    hass: _HomeAssistantClient,
    entity_id: str,
    action: str,
) -> None:
    """Deliver an entity registry update and wait for the engine rebuild it schedules."""
    with patch("music_assistant.providers.hass.ENGINE_REFRESH_DEBOUNCE", 0):
        hass.fire_event("entity_registry_updated", _registry_event(entity_id, action))
        assert provider._engine_refresh_task is not None
        async with asyncio.timeout(1):
            await provider._engine_refresh_task


def _providers_updated_events(provider: HomeAssistantProvider) -> list[Any]:
    """
    Return the PROVIDERS_UPDATED events the provider signalled so far.

    :param provider: The provider whose Music Assistant mock is inspected.
    """
    signal_event = cast("MagicMock", provider.mass.signal_event)
    return [
        call
        for call in signal_event.call_args_list
        if call.args[:1] == (EventType.PROVIDERS_UPDATED,)
    ]


async def test_feature_resolution_starts_listener_first() -> None:
    """Resolve startup features only after the Home Assistant listener starts."""
    states = [
        _state("ai_task.default", "Default AI"),
        _state("tts.default", "Default TTS"),
    ]

    async with _start_provider(states) as (provider, hass):
        assert hass.calls[:4] == [
            "connect",
            "start_listening",
            "subscribe_events",
            REGISTRY_LIST_COMMAND,
        ]
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
    assert hass.calls == [
        "connect",
        "start_listening",
        "subscribe_events",
        REGISTRY_LIST_COMMAND,
        "unsubscribe_events",
        "disconnect",
    ]
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


async def test_lost_connection_arms_the_reconnect_under_the_load_task_id() -> None:
    """A lost connection arms the reconnect so a (re)load starting first cancels it."""
    async with _start_provider([]) as (provider, hass):
        mass = cast("MagicMock", provider.mass)
        mass.call_later.reset_mock()
        assert provider._listen_task is not None

        hass.connection_lost.set_exception(BaseHassClientError("connection lost"))
        async with asyncio.timeout(1):
            await provider._listen_task

        assert provider.available is False
        retry = mass.call_later.call_args
        assert retry.args == (5, mass.load_provider, "hass--test")
        assert retry.kwargs == {"allow_retry": True, "task_id": "load_provider_hass--test"}


async def test_engines_are_listed_for_every_feature_entity() -> None:
    """Expose every Home Assistant TTS and AI Task entity as an engine."""
    states = [
        _state("tts.piper", "Piper"),
        _state("tts.cloud", "Home Assistant Cloud"),
        _state("ai_task.openai", "OpenAI"),
        _state("sensor.example", "Example"),
    ]

    async with _start_provider(states) as (provider, hass):
        calls_before = list(hass.calls)
        tts_engines = await provider.get_tts_engines()
        ai_engines = await provider.get_ai_engines()

        # listing engines is a hot path for consumers: it must not touch Home Assistant
        assert hass.calls == calls_before
        assert [(engine.id, engine.name) for engine in tts_engines] == [
            ("tts.cloud", "Home Assistant Cloud (tts.cloud)"),
            ("tts.piper", "Piper (tts.piper)"),
        ]
        assert [(engine.id, engine.name) for engine in ai_engines] == [
            ("ai_task.openai", "OpenAI (ai_task.openai)")
        ]
        assert tts_engines[0].uid == "hass--test/tts.cloud"


async def test_engine_without_friendly_name_falls_back_to_entity_id() -> None:
    """Name an engine after its entity_id when Home Assistant has no friendly name."""
    unnamed = {"entity_id": "tts.unnamed", "state": "idle", "attributes": {}}

    async with _start_provider([unnamed]) as (provider, _):
        engines = await provider.get_tts_engines()

        assert [engine.name for engine in engines] == ["tts.unnamed"]


async def test_ai_query_uses_the_first_engine_by_default() -> None:
    """Send an AI query to the first available engine when none is requested."""
    states = [_state("ai_task.first", "First"), _state("ai_task.second", "Second")]

    async with _start_provider(states) as (provider, hass):
        result = await provider.ai_query("What is this song?")

        assert ProviderFeature.AI_QUERY in provider.supported_features
        assert result == "answer"
        hass.send_command.assert_awaited_with(
            "call_service",
            domain="ai_task",
            service="generate_data",
            service_data={
                "task_name": "music_assistant",
                "instructions": "What is this song?",
                "entity_id": "ai_task.first",
            },
            return_response=True,
        )


async def test_ai_query_uses_the_requested_engine() -> None:
    """Send an AI query to the requested engine."""
    states = [_state("ai_task.first", "First"), _state("ai_task.second", "Second")]

    async with _start_provider(states) as (provider, hass):
        await provider.ai_query("What is this song?", engine_id="ai_task.second")

        service_data = hass.send_command.call_args.kwargs["service_data"]
        assert service_data["entity_id"] == "ai_task.second"


async def test_ai_query_not_advertised_without_entity() -> None:
    """Do not advertise AI queries when Home Assistant has no AI Task entity."""
    async with _start_provider([_state("sensor.example", "Example")]) as (provider, _):
        assert ProviderFeature.AI_QUERY not in provider.supported_features

        with pytest.raises(UnsupportedFeaturedException):
            await provider.ai_query("What is this song?")


def _mock_tts_response(provider: HomeAssistantProvider) -> MagicMock:
    """Let the Home Assistant tts_get_url endpoint return a URL and return the post mock."""
    response = AsyncMock()
    response.ok = True
    response.raise_for_status = MagicMock()
    response.json.return_value = {"url": "http://homeassistant.local/tts.mp3"}
    post = cast("MagicMock", provider.mass.http_session.post)
    post.return_value.__aenter__.return_value = response
    return post


def _mock_tts_error_response(provider: HomeAssistantProvider, error_message: str) -> MagicMock:
    """Let the tts_get_url endpoint fail with a 400 carrying the given HA error body."""
    response = AsyncMock()
    response.ok = False
    response.json.return_value = {"error": error_message}
    response.raise_for_status = MagicMock(
        side_effect=ClientResponseError(MagicMock(), (), status=400, message=error_message)
    )
    post = cast("MagicMock", provider.mass.http_session.post)
    post.return_value.__aenter__.return_value = response
    return post


async def test_tts_uses_the_first_engine_by_default() -> None:
    """Render speech on the first available engine when none is requested."""
    states = [_state("tts.first", "First"), _state("tts.second", "Second")]

    async with _start_provider(states) as (provider, _):
        post = _mock_tts_response(provider)

        stream = await provider.get_tts_message("Hello")

        assert ProviderFeature.TTS in provider.supported_features
        assert stream.path == "http://homeassistant.local/tts.mp3"
        post.assert_called_once()
        request = post.call_args
        assert request.args == ("http://homeassistant.local:8123/api/tts_get_url",)
        assert request.kwargs["json"] == {"engine_id": "tts.first", "message": "Hello"}


async def test_tts_uses_the_requested_engine() -> None:
    """Render speech on the requested engine."""
    states = [_state("tts.first", "First"), _state("tts.second", "Second")]

    async with _start_provider(states) as (provider, _):
        post = _mock_tts_response(provider)

        await provider.get_tts_message("Hello", engine_id="tts.second")

        assert post.call_args.kwargs["json"] == {"engine_id": "tts.second", "message": "Hello"}


async def test_tts_not_advertised_without_entity() -> None:
    """Do not advertise TTS when Home Assistant has no TTS entity."""
    async with _start_provider([_state("sensor.example", "Example")]) as (provider, _):
        assert ProviderFeature.TTS not in provider.supported_features

        with pytest.raises(UnsupportedFeaturedException):
            await provider.get_tts_message("Hello")


async def test_tts_sends_options_in_the_payload() -> None:
    """A host's TTS options are forwarded in the tts_get_url payload."""
    async with _start_provider([_state("tts.first", "First")]) as (provider, _):
        post = _mock_tts_response(provider)

        await provider.get_tts_message(
            "Hello", options={"voice": "en_US-lessac-medium", "length_scale": 1.2}
        )

        assert post.call_args.kwargs["json"] == {
            "engine_id": "tts.first",
            "message": "Hello",
            "options": {"voice": "en_US-lessac-medium", "length_scale": 1.2},
        }


async def test_tts_omits_options_from_the_payload_when_empty() -> None:
    """An empty options dict is not sent to Home Assistant."""
    async with _start_provider([_state("tts.first", "First")]) as (provider, _):
        post = _mock_tts_response(provider)

        await provider.get_tts_message("Hello", options={})

        assert post.call_args.kwargs["json"] == {"engine_id": "tts.first", "message": "Hello"}


async def test_tts_invalid_option_raises_music_assistant_error() -> None:
    """HA rejecting an unknown TTS option surfaces as MusicAssistantError with HA's message."""
    async with _start_provider([_state("tts.first", "First")]) as (provider, _):
        _mock_tts_error_response(provider, "Invalid options found: ['speaking_cadance']")

        with pytest.raises(MusicAssistantError, match=r"Invalid options found"):
            await provider.get_tts_message("Hello", options={"speaking_cadance": 1})


async def test_tts_error_body_that_is_not_json_still_raises_the_generic_way() -> None:
    """An unparsable error body leaves the status error to speak for itself."""
    async with _start_provider([_state("tts.first", "First")]) as (provider, _):
        post = _mock_tts_error_response(provider, "Invalid options found: ['x']")
        response = post.return_value.__aenter__.return_value
        response.json.side_effect = ValueError("not json")

        with pytest.raises(ClientResponseError):
            await provider.get_tts_message("Hello", options={"x": 1})


async def test_tts_unsupported_language_still_raises_the_generic_way() -> None:
    """An unsupported-language 400 keeps raising as before, so the caller's retry still fires."""
    async with _start_provider([_state("tts.first", "First")]) as (provider, _):
        _mock_tts_error_response(provider, "Language 'xx' not supported")

        with pytest.raises(ClientResponseError):
            await provider.get_tts_message("Hello", language="xx")


async def test_registry_update_refreshes_the_engines() -> None:
    """Pick up a feature entity that Home Assistant adds after startup."""
    async with _start_provider([_state("sensor.example", "Example")]) as (provider, hass):
        await provider.loaded_in_mass()
        assert ProviderFeature.TTS not in provider.supported_features

        hass.compressed_states["tts.new"] = _compressed(_state("tts.new", "New"))
        await _fire_registry_update(provider, hass, "tts.new", "create")

        assert [engine.id for engine in await provider.get_tts_engines()] == ["tts.new"]
        assert ProviderFeature.TTS in provider.supported_features


async def test_registry_update_discards_the_feature_of_a_removed_engine() -> None:
    """Stop advertising a feature once its last Home Assistant entity is gone."""
    states = [_state("tts.only", "Only"), _state("ai_task.only", "Only")]

    async with _start_provider(states) as (provider, hass):
        await provider.loaded_in_mass()
        assert ProviderFeature.TTS in provider.supported_features

        del hass.compressed_states["tts.only"]
        await _fire_registry_update(provider, hass, "tts.only", "remove")

        assert await provider.get_tts_engines() == []
        assert ProviderFeature.TTS not in provider.supported_features
        # the AI Task entity is untouched, so its feature stays declared
        assert ProviderFeature.AI_QUERY in provider.supported_features


async def test_changed_engines_notify_the_consumers_once() -> None:
    """Announce a refresh that changed the engine lists with a single PROVIDERS_UPDATED."""
    states = [_state("tts.only", "Only"), _state("ai_task.only", "Only")]

    async with _start_provider(states) as (provider, hass):
        await provider.loaded_in_mass()
        mass = cast("MagicMock", provider.mass)
        mass.signal_event.reset_mock()

        del hass.compressed_states["tts.only"]
        await _fire_registry_update(provider, hass, "tts.only", "remove")

        events = _providers_updated_events(provider)
        assert len(events) == 1
        assert events[0].kwargs["data"] is mass.get_providers.return_value


async def test_a_lost_ai_engine_notifies_the_consumers() -> None:
    """Announce a vanished AI engine, the selection AI Radio depends on."""
    states = [_state("tts.only", "Only"), _state("ai_task.only", "Only")]

    async with _start_provider(states) as (provider, hass):
        await provider.loaded_in_mass()
        cast("MagicMock", provider.mass).signal_event.reset_mock()

        del hass.compressed_states["ai_task.only"]
        await _fire_registry_update(provider, hass, "ai_task.only", "remove")

        assert await provider.get_ai_engines() == []
        assert [engine.id for engine in await provider.get_tts_engines()] == ["tts.only"]
        assert len(_providers_updated_events(provider)) == 1


async def test_engines_are_in_place_before_the_consumers_are_told() -> None:
    """Expose the rebuilt engine lists before signalling, as consumers read them at once."""
    states = [_state("tts.only", "Only"), _state("ai_task.only", "Only")]

    async with _start_provider(states) as (provider, hass):
        await provider.loaded_in_mass()
        signal_event = cast("MagicMock", provider.mass.signal_event)
        signal_event.reset_mock()
        engines_when_told: list[list[str]] = []
        signal_event.side_effect = lambda *_args, **_kwargs: engines_when_told.append(
            [engine.id for engine in provider._tts_engines]
        )

        del hass.compressed_states["tts.only"]
        await _fire_registry_update(provider, hass, "tts.only", "remove")

        assert engines_when_told == [[]]


async def test_unchanged_engines_do_not_notify_the_consumers() -> None:
    """Stay silent when a refresh rebuilds the very same engine lists."""
    states = [_state("tts.only", "Only"), _state("ai_task.only", "Only")]

    async with _start_provider(states) as (provider, hass):
        await provider.loaded_in_mass()
        cast("MagicMock", provider.mass).signal_event.reset_mock()

        # registry churn that leaves the feature entities as they are, as in a rename of
        # an entity that the engine name does not depend on
        await _fire_registry_update(provider, hass, "tts.only", "update")

        assert [engine.id for engine in await provider.get_tts_engines()] == ["tts.only"]
        assert [engine.id for engine in await provider.get_ai_engines()] == ["ai_task.only"]
        assert _providers_updated_events(provider) == []


async def test_startup_refresh_does_not_notify_the_consumers() -> None:
    """Leave the announcement of the engines found during startup to the load path."""
    states = [_state("tts.only", "Only"), _state("ai_task.only", "Only")]

    async with _start_provider(states) as (provider, _):
        # the refresh that filled the empty lists ran before startup was marked complete
        assert provider._startup_complete
        assert [engine.id for engine in await provider.get_tts_engines()] == ["tts.only"]
        assert _providers_updated_events(provider) == []


async def test_refresh_tracks_engines_and_features_in_both_directions() -> None:
    """Follow the engine lists and their features as feature entities appear and vanish."""
    async with _start_provider([_state("sensor.example", "Example")]) as (provider, hass):
        await provider.loaded_in_mass()
        cast("MagicMock", provider.mass).signal_event.reset_mock()
        assert ProviderFeature.TTS not in provider.supported_features
        assert ProviderFeature.AI_QUERY not in provider.supported_features

        hass.compressed_states["tts.new"] = _compressed(_state("tts.new", "New TTS"))
        hass.compressed_states["ai_task.new"] = _compressed(_state("ai_task.new", "New AI"))
        await _fire_registry_update(provider, hass, "tts.new", "create")

        assert [engine.id for engine in await provider.get_tts_engines()] == ["tts.new"]
        assert [engine.id for engine in await provider.get_ai_engines()] == ["ai_task.new"]
        assert ProviderFeature.TTS in provider.supported_features
        assert ProviderFeature.AI_QUERY in provider.supported_features

        del hass.compressed_states["tts.new"]
        del hass.compressed_states["ai_task.new"]
        await _fire_registry_update(provider, hass, "tts.new", "remove")

        assert await provider.get_tts_engines() == []
        assert await provider.get_ai_engines() == []
        assert ProviderFeature.TTS not in provider.supported_features
        assert ProviderFeature.AI_QUERY not in provider.supported_features
        assert len(_providers_updated_events(provider)) == 2


async def test_registry_update_of_another_domain_is_ignored() -> None:
    """Ignore registry updates for entities that cannot back a feature."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        await provider.loaded_in_mass()

        hass.fire_event("entity_registry_updated", _registry_event("light.kitchen", "create"))

        assert provider._engine_refresh_task is None


async def test_registry_update_of_a_feature_entity_refreshes_without_a_refetch() -> None:
    """Rebuild the engine lists for a renamed feature entity without refetching the registry."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        await provider.loaded_in_mass()
        registry = await provider.get_entity_registry()
        hass.compressed_states["tts.only"] = _compressed(_state("tts.only", "Renamed"))

        with patch("music_assistant.providers.hass.ENGINE_REFRESH_DEBOUNCE", 0):
            hass.fire_event(
                "entity_registry_updated", _registry_event("tts.only", changes={"name": "Only"})
            )
            assert provider._engine_refresh_task is not None
            async with asyncio.timeout(1):
                await provider._engine_refresh_task

        engines = await provider.get_tts_engines()
        assert [engine.name for engine in engines] == ["Renamed (tts.only)"]
        assert provider._entity_registry is registry


async def test_burst_of_registry_updates_triggers_a_single_refresh() -> None:
    """Collect a burst of registry updates into one rebuild of the engine lists."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        await provider.loaded_in_mass()
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)

        with patch("music_assistant.providers.hass.ENGINE_REFRESH_DEBOUNCE", 0.05):
            for index in range(3):
                hass.fire_event(
                    "entity_registry_updated", _registry_event(f"tts.new_{index}", "create")
                )
            assert provider._engine_refresh_task is not None
            async with asyncio.timeout(1):
                await provider._engine_refresh_task

        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches + 1


async def test_registry_update_of_another_domain_invalidates_the_registry() -> None:
    """Refresh the cached registry for an entity that appeared in any domain."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)
        hass.compressed_states["light.kitchen"] = _compressed(_state("light.kitchen", "Kitchen"))

        hass.fire_event("entity_registry_updated", _registry_event("light.kitchen", "create"))

        # an entity that can not back a feature must not trigger an engine rebuild
        assert provider._engine_refresh_task is None
        result = await provider.get_states(domains=("light",))
        assert [state["entity_id"] for state in result] == ["light.kitchen"]
        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches + 1


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"name": "Old name"}, id="rename"),
        pytest.param({"icon": None}, id="icon"),
        pytest.param({"labels": []}, id="labels"),
        pytest.param({"hidden_by": None}, id="hidden"),
        # an integration reload re-registers its entities, which touches these
        pytest.param({"capabilities": None, "supported_features": 0}, id="reload"),
    ],
)
async def test_registry_update_of_unmirrored_fields_keeps_the_registry(
    changes: dict[str, Any],
) -> None:
    """Keep the mirrored registry for an update that cannot change what it holds."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        registry = await provider.get_entity_registry()
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)

        hass.fire_event(
            "entity_registry_updated", _registry_event("light.kitchen", changes=changes)
        )

        assert await provider.get_entity_registry() is registry
        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"entity_id": "light.old"}, id="entity_id"),
        pytest.param({"platform": "old_platform"}, id="platform"),
        pytest.param({"device_id": None}, id="device_id"),
        pytest.param({"area_id": None}, id="area_id"),
        # the listing omits disabled entities, so this one enters or leaves it
        pytest.param({"disabled_by": None}, id="disabled_by"),
        # a device rename reports no changed fields at all
        pytest.param({}, id="changes_empty"),
        # a move to another config entry can silently re-enable the entity
        pytest.param({"config_entry_id": "other"}, id="config_entry_id"),
    ],
)
async def test_registry_update_of_mirrored_fields_invalidates_the_registry(
    changes: dict[str, Any],
) -> None:
    """Refetch the mirrored registry for an update that can change what it holds."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        await provider.get_entity_registry()
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)

        hass.fire_event(
            "entity_registry_updated", _registry_event("light.kitchen", changes=changes)
        )

        assert provider._entity_registry is None
        await provider.get_entity_registry()
        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches + 1


async def test_unmirrored_change_during_the_fetch_is_still_cached() -> None:
    """Cache a registry listing that only an irrelevant registry update raced."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        provider._entity_registry = None
        # hold back the registry response until the update has been delivered
        registry_response = _hold_back_registry_fetch(hass)
        lookup = asyncio.ensure_future(provider.get_states(domains=("tts",)))
        await _wait_for_registry_fetch(hass)

        hass.fire_event(
            "entity_registry_updated", _registry_event("light.kitchen", changes={"name": "Old"})
        )
        registry_response.set_result(None)
        async with asyncio.timeout(1):
            await lookup

        assert provider._entity_registry is not None


async def test_domains_are_resolved_through_the_display_registry() -> None:
    """Resolve domains through the compact registry listing only."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        await provider.get_states(domains=("tts",))

        assert REGISTRY_LIST_COMMAND in hass.calls
        assert "config/entity_registry/list" not in hass.calls


async def test_registry_is_fetched_once_per_connection() -> None:
    """Serve repeated domain lookups from a registry that is fetched only once."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)

        await provider.get_states(domains=("tts",))
        await provider.get_states(domains=("media_player",))

        assert registry_fetches == 1
        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches


async def test_concurrent_domain_lookups_share_one_registry_fetch() -> None:
    """Let concurrent domain lookups share a single registry fetch."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)
        provider._entity_registry = None
        # hold back the registry response until both lookups are waiting for it
        registry_response = _hold_back_registry_fetch(hass)

        lookups = asyncio.gather(
            provider.get_states(domains=("tts",)), provider.get_states(domains=("tts",))
        )
        await _wait_for_registry_fetch(hass)
        registry_response.set_result(None)
        async with asyncio.timeout(1):
            results = await lookups

        assert all(len(result) == 1 for result in results)
        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches + 1


async def test_registry_changed_during_the_fetch_is_not_cached() -> None:
    """Keep a registry listing out of the cache when a registry update outdated it in flight."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        provider._entity_registry = None
        # hold back the registry response until the update has been delivered
        registry_response = _hold_back_registry_fetch(hass)
        lookup = asyncio.ensure_future(provider.get_states(domains=("tts",)))
        await _wait_for_registry_fetch(hass)

        hass.fire_event("entity_registry_updated", _registry_event("light.kitchen", "create"))
        registry_response.set_result(None)
        async with asyncio.timeout(1):
            await lookup

        assert provider._entity_registry is None
        registry_fetches = hass.calls.count(REGISTRY_LIST_COMMAND)
        await provider.get_states(domains=("tts",))
        assert hass.calls.count(REGISTRY_LIST_COMMAND) == registry_fetches + 1


async def test_shared_registry_rejects_writes() -> None:
    """Reject writes to the registry (and its entries) shared between all callers."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, _):
        registry = await provider.get_entity_registry()

        with pytest.raises(TypeError):
            registry["light.kitchen"] = HassRegistryEntity(  # type: ignore[index]
                platform="test", device_id=None, area_id=None
            )
        with pytest.raises(AttributeError):
            registry["tts.only"].platform = "test"  # type: ignore[misc]


async def test_registry_reuses_repeated_strings() -> None:
    """Hold on to a single string object per distinct platform, device id and area id."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, _):
        # decode the listing like a real response, so the repeated platform, device id and
        # area id arrive as distinct string objects instead of shared literals
        entities = json.loads(
            json.dumps(
                [
                    {
                        "ei": f"light.lamp_{index}",
                        "pl": "esphome",
                        "di": "device",
                        "ai": "area",
                    }
                    for index in range(3)
                ]
            )
        )
        with patch.object(
            provider.hass, "send_command", AsyncMock(return_value={"entities": entities})
        ):
            registry = await provider._fetch_entity_registry()

        assert len(registry) == 3
        assert registry["light.lamp_0"] == HassRegistryEntity(
            platform="esphome", device_id="device", area_id="area"
        )
        assert len({id(entry.platform) for entry in registry.values()}) == 1
        assert len({id(entry.device_id) for entry in registry.values()}) == 1
        assert len({id(entry.area_id) for entry in registry.values()}) == 1


async def test_registry_leaves_out_the_device_and_area_it_is_not_told_about() -> None:
    """Report no device or area for the entities whose listing entry omits them."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, _):
        entities = [
            {"ei": "light.full", "pl": "esphome", "di": "device", "ai": "area"},
            {"ei": "light.bare", "pl": "esphome"},
            {"ei": "light.device_only", "pl": "esphome", "di": "other_device"},
        ]
        with patch.object(
            provider.hass, "send_command", AsyncMock(return_value={"entities": entities})
        ):
            registry = await provider._fetch_entity_registry()

        assert registry["light.bare"] == HassRegistryEntity(
            platform="esphome", device_id=None, area_id=None
        )
        assert registry["light.device_only"] == HassRegistryEntity(
            platform="esphome", device_id="other_device", area_id=None
        )


async def test_device_registry_is_reused_within_the_cache_window() -> None:
    """Serve a later device lookup from the cached listing."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        hass.devices = [{"id": "dev1"}]

        assert await provider.get_device_registry() == {"dev1": {"id": "dev1"}}
        await _wait_for_stored(provider)
        await provider.get_device_registry()

        assert hass.calls.count(DEVICE_REGISTRY_LIST) == 1


async def test_device_registry_is_refetched_once_the_cache_window_passed() -> None:
    """Fetch the device listing again once the cached entry is no longer fresh."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        hass.devices = [{"id": "dev1"}]
        await provider.get_device_registry()
        hass.devices = [{"id": "dev1"}, {"id": "dev2"}]

        cast("_Cache", provider.mass.cache).expire_all()

        assert list(await provider.get_device_registry()) == ["dev1", "dev2"]
        assert hass.calls.count(DEVICE_REGISTRY_LIST) == 2


async def test_device_entries_survive_the_cache_round_trip() -> None:
    """Return a cached device entry with all of its fields intact."""
    device = {
        "id": "dev1",
        "name": "Kitchen Speaker",
        "name_by_user": None,
        "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
        "manufacturer": "ESPHome",
    }
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        hass.devices = [device]

        fetched = await provider.get_device_registry()
        await _wait_for_stored(provider)
        cached = await provider.get_device_registry()

        assert hass.calls.count(DEVICE_REGISTRY_LIST) == 1
        # the cached read is reconstructed from the return annotation, so it must not
        # drop or reshape any of the device fields
        assert cached == fetched == {"dev1": device}


async def test_concurrent_device_lookups_do_not_fetch_per_caller() -> None:
    """Keep a burst of concurrent device lookups off a fetch-per-caller path."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        hass.devices = [{"id": "dev1"}]

        async with asyncio.timeout(1):
            results = await asyncio.gather(*(provider.get_device_registry() for _ in range(20)))

        assert all(list(result) == ["dev1"] for result in results)
        # the lock keeps the fetch count flat instead of growing with the burst; it does
        # not reach one, because use_cache stores in the background and the caller right
        # behind the first one still finds the cache cold
        assert hass.calls.count(DEVICE_REGISTRY_LIST) <= 2


async def test_disabled_entity_is_never_requested() -> None:
    """Leave an entity that Home Assistant disabled out of the state fetch."""
    states = [_state("media_player.kitchen", "Kitchen"), _state("media_player.spare", "Spare")]

    async with _start_provider(states) as (provider, hass):
        # Home Assistant omits disabled entities from the registry, their state remains
        hass.disabled_entities.add("media_player.spare")
        hass.fire_event(
            "entity_registry_updated",
            _registry_event("media_player.spare", changes={"disabled_by": None}),
        )
        hass.subscriptions.clear()

        result = await provider.get_states(domains=("media_player",))

        assert [state["entity_id"] for state in result] == ["media_player.kitchen"]
        assert hass.subscriptions == [["media_player.kitchen"]]


async def test_registry_entries_are_fetched_for_the_given_entities_only() -> None:
    """Fetch the full registry entries of exactly the requested entities."""
    async with _start_provider([_state("media_player.kitchen", "Kitchen")]) as (provider, hass):
        entries = await provider.get_entity_registry_entries(
            ["media_player.kitchen", "media_player.unknown"]
        )

        # an entity Home Assistant does not know is absent from the result
        assert list(entries) == ["media_player.kitchen"]
        assert entries["media_player.kitchen"]["config_entry_id"] == "config_entry_id"
        hass.send_command.assert_awaited_with(
            REGISTRY_ENTRIES_COMMAND,
            entity_ids=["media_player.kitchen", "media_player.unknown"],
        )


async def test_registry_entries_of_nothing_skips_the_round_trip() -> None:
    """Do not contact Home Assistant when no entity is requested."""
    async with _start_provider([_state("media_player.kitchen", "Kitchen")]) as (provider, hass):
        calls_before = list(hass.calls)

        assert await provider.get_entity_registry_entries([]) == {}
        assert hass.calls == calls_before


async def test_entity_registry_subscription_is_replaced() -> None:
    """Replace the entity registry subscription instead of stacking a second one."""
    async with _start_provider([_state("tts.only", "Only")]) as (provider, hass):
        assert hass.active_event_subscriptions == 1
        assert hass.event_subscriptions[0][0] == "entity_registry_updated"

        await provider._subscribe_entity_registry()

        assert hass.active_event_subscriptions == 1
        assert hass.calls[-2:] == ["unsubscribe_events", "subscribe_events"]

        await provider.unload()

        assert hass.active_event_subscriptions == 0


async def test_config_entries_do_not_read_home_assistant() -> None:
    """Build the config entries without asking Home Assistant for anything."""
    async with _start_provider([_state("switch.example", "Example")]) as (provider, _):
        provider.available = True
        with patch.object(provider, "get_states", AsyncMock()) as get_states:
            entries = await provider.get_config_entries()

    get_states.assert_not_awaited()
    assert CONF_POWER_CONTROLS in {entry.key for entry in entries}


async def test_config_entries_offer_the_control_lists_without_options() -> None:
    """Offer the control lists as plain entity id lists, filled in by the entity picker."""
    async with _start_provider([_state("switch.example", "Example")]) as (provider, _):
        provider.available = True
        entries = await provider.get_config_entries()

    control_entries = [
        entry
        for entry in entries
        if entry.key in (CONF_POWER_CONTROLS, CONF_VOLUME_CONTROLS, CONF_MUTE_CONTROLS)
    ]
    assert len(control_entries) == 3
    for entry in control_entries:
        assert entry.options == []
        assert entry.multi_value is True


async def test_domain_states_use_a_single_subscription() -> None:
    """Fetch every entity of a domain in one websocket round-trip."""
    states = [_state(f"media_player.player_{index}", f"Player {index}") for index in range(50)]

    async with _start_provider(states) as (provider, hass):
        result = await provider.get_states(domains=("media_player",))

        assert len(result) == len(states)
        assert len(hass.subscriptions) == 1
        assert hass.subscriptions[0] == sorted(state["entity_id"] for state in states)


async def test_large_requests_are_split_into_batches() -> None:
    """Split a request that exceeds the batch size into bounded batches."""
    entity_ids = [
        f"media_player.player_{index:04d}" for index in range(STATE_FETCH_BATCH_SIZE * 2 + 1)
    ]
    states = [_state(entity_id, entity_id) for entity_id in entity_ids]

    async with _start_provider(states) as (provider, hass):
        result = await provider.get_states(domains=("media_player",))

        assert len(result) == len(entity_ids)
        assert len(hass.subscriptions) == ceil(len(entity_ids) / STATE_FETCH_BATCH_SIZE)
        assert all(len(batch) <= STATE_FETCH_BATCH_SIZE for batch in hass.subscriptions)
        requested = [entity_id for batch in hass.subscriptions for entity_id in batch]
        assert sorted(requested) == sorted(entity_ids)
        assert len(requested) == len(set(requested))


async def test_every_batch_is_unsubscribed() -> None:
    """Release the subscription of every batch once its states have been received."""
    entity_ids = [f"media_player.player_{index}" for index in range(5)]
    states = [_state(entity_id, entity_id) for entity_id in entity_ids]

    async with _start_provider(states) as (provider, hass):
        with patch("music_assistant.providers.hass.STATE_FETCH_BATCH_SIZE", 2):
            await provider.get_states(domains=("media_player",))

        assert len(hass.subscriptions) == 3
        assert hass.unsubscribed == hass.subscriptions
        assert hass.active_subscriptions == 0


async def test_timed_out_fetch_is_unsubscribed() -> None:
    """Release the subscription when Home Assistant never sends the states."""
    async with _start_provider([_state("media_player.kitchen", "Kitchen")]) as (provider, hass):
        hass.deliver_initial_states = False

        with (
            patch("music_assistant.providers.hass.STATE_FETCH_TIMEOUT", 0.05),
            pytest.raises(TimeoutError),
        ):
            await provider.get_states(entity_ids=["media_player.kitchen"])

        assert hass.unsubscribed == [["media_player.kitchen"]]
        assert hass.active_subscriptions == 0


async def test_cancelled_fetch_is_unsubscribed() -> None:
    """Release the subscription when the state fetch is cancelled."""
    async with _start_provider([_state("media_player.kitchen", "Kitchen")]) as (provider, hass):
        hass.deliver_initial_states = False
        fetch_task = asyncio.create_task(provider.get_states(entity_ids=["media_player.kitchen"]))
        async with asyncio.timeout(1):
            await hass.subscribed.wait()
        fetch_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await fetch_task

        assert hass.unsubscribed == [["media_player.kitchen"]]
        assert hass.active_subscriptions == 0


async def test_compressed_states_are_expanded() -> None:
    """Expand the compressed states of the initial message into full states."""
    async with _start_provider([]) as (provider, hass):
        hass.compressed_states = {
            "media_player.full": {
                "s": "playing",
                "a": {"friendly_name": "Full"},
                "lc": LAST_CHANGED,
                "lu": 1683838800.736819,
                "c": {"id": CONTEXT_ID, "parent_id": None, "user_id": "user"},
            },
            "media_player.unchanged": {"s": "idle", "lc": LAST_CHANGED, "c": CONTEXT_ID},
            "media_player.minimal": {},
        }

        result = {
            state["entity_id"]: state
            for state in await provider.get_states(entity_ids=list(hass.compressed_states))
        }

        assert result["media_player.full"]["state"] == "playing"
        assert result["media_player.full"]["attributes"] == {"friendly_name": "Full"}
        assert result["media_player.full"]["last_changed"] == LAST_CHANGED_ISO
        assert result["media_player.full"]["last_updated"] == "2023-05-11T21:00:00.736819+00:00"
        assert result["media_player.full"]["context"] == {
            "id": CONTEXT_ID,
            "parent_id": None,
            "user_id": "user",
        }
        # last_updated is omitted by HA when it is identical to last_changed
        assert result["media_player.unchanged"]["last_changed"] == LAST_CHANGED_ISO
        assert result["media_player.unchanged"]["last_updated"] == LAST_CHANGED_ISO
        assert result["media_player.unchanged"]["context"] == {
            "id": CONTEXT_ID,
            "parent_id": None,
            "user_id": None,
        }
        assert result["media_player.minimal"] == {
            "entity_id": "media_player.minimal",
            "state": "",
            "attributes": {},
            "last_changed": "",
            "last_updated": "",
            "context": {"id": "", "parent_id": None, "user_id": None},
        }


async def test_entity_without_state_is_absent() -> None:
    """Omit entities that Home Assistant has no state for."""
    async with _start_provider([_state("media_player.kitchen", "Kitchen")]) as (provider, hass):
        result = await provider.get_states(
            entity_ids=["media_player.kitchen", "media_player.removed"]
        )

        assert [state["entity_id"] for state in result] == ["media_player.kitchen"]
        assert hass.subscriptions == [["media_player.kitchen", "media_player.removed"]]


async def test_state_change_does_not_complete_the_fetch() -> None:
    """Ignore a state change that arrives before the initial state message."""
    async with _start_provider([_state("media_player.kitchen", "Kitchen")]) as (provider, hass):
        hass.leading_events = [{"c": {"media_player.kitchen": {"+": {"s": "playing"}}}}]

        result = await provider.get_states(entity_ids=["media_player.kitchen"])

        assert [state["entity_id"] for state in result] == ["media_player.kitchen"]
        assert result[0]["state"] == "idle"


async def test_player_control_subscription_is_replaced() -> None:
    """Replace the player control subscription instead of stacking a second one."""
    states = [_state("media_player.kitchen", "Kitchen")]
    async with _start_provider(states, **{CONF_POWER_CONTROLS: ["media_player.kitchen"]}) as (
        provider,
        hass,
    ):
        await provider.loaded_in_mass()
        assert hass.active_subscriptions == 1

        # only a changed selection results in a new subscription
        provider.config = _config(
            **{
                CONF_POWER_CONTROLS: ["media_player.kitchen"],
                CONF_MUTE_CONTROLS: ["switch.kitchen_amp"],
            }
        )
        await provider._register_player_controls()

        assert len(hass.subscriptions) == 4
        assert hass.active_subscriptions == 1
        # the previous control subscription is only released once its replacement is live
        assert hass.calls[-2:] == ["subscribe_entities", "unsubscribe_entities"]

        await provider.unload()

        assert hass.active_subscriptions == 0


async def test_failed_control_subscription_keeps_the_previous_one() -> None:
    """A failed subscription attempt leaves the controls watched by the earlier one."""
    states = [_state("media_player.kitchen", "Kitchen"), _state("switch.amp", "Amp")]
    async with _start_provider(states, **{CONF_POWER_CONTROLS: ["media_player.kitchen"]}) as (
        provider,
        hass,
    ):
        await provider.loaded_in_mass()
        unsubscribe_before = provider._unsubscribe_controls
        assert hass.active_subscriptions == 1

        async def _failing_subscribe(*_args: Any, **_kwargs: Any) -> Callable[[], None]:
            raise BaseHassClientError("connection lost")

        hass.subscribe_entities = _failing_subscribe  # type: ignore[method-assign]
        provider.config = _config(
            **{
                CONF_POWER_CONTROLS: ["media_player.kitchen"],
                CONF_MUTE_CONTROLS: ["switch.amp"],
            }
        )

        with pytest.raises(BaseHassClientError):
            await provider._register_player_controls()

        assert provider._unsubscribe_controls is unsubscribe_before
        assert hass.active_subscriptions == 1


async def test_unchanged_control_selection_skips_home_assistant() -> None:
    """Reconciling an unchanged selection does not talk to Home Assistant at all."""
    states = [_state("media_player.kitchen", "Kitchen")]
    async with _start_provider(states, **{CONF_POWER_CONTROLS: ["media_player.kitchen"]}) as (
        provider,
        hass,
    ):
        await provider.loaded_in_mass()
        calls_after_load = list(hass.calls)

        await provider._register_player_controls()

        assert hass.calls == calls_after_load


async def test_control_list_change_reconciles_without_reload() -> None:
    """A change confined to the control lists is applied without reloading the provider."""
    states = [_state("media_player.kitchen", "Kitchen"), _state("switch.amp", "Amp")]
    async with _start_provider(states, **{CONF_POWER_CONTROLS: ["media_player.kitchen"]}) as (
        provider,
        _hass,
    ):
        mass = cast("MagicMock", provider.mass)
        await provider.loaded_in_mass()
        assert set(provider._player_controls or {}) == {"media_player.kitchen"}
        mass.call_later.reset_mock()

        await provider.update_config(
            _config(**{CONF_POWER_CONTROLS: ["switch.amp"]}),
            {f"values/{CONF_POWER_CONTROLS}"},
        )

        mass.call_later.assert_not_called()
        assert set(provider._player_controls or {}) == {"switch.amp"}
        mass.players.remove_player_control.assert_called_once_with("media_player.kitchen")
        registered = mass.players.register_or_update_player_control.call_args[0][0]
        assert registered.id == "switch.amp"
        assert registered.supports_power is True


async def test_control_role_change_is_applied() -> None:
    """Moving an already selected entity to another role re-registers it in that role."""
    states = [_state("media_player.kitchen", "Kitchen")]
    async with _start_provider(states, **{CONF_POWER_CONTROLS: ["media_player.kitchen"]}) as (
        provider,
        _hass,
    ):
        await provider.loaded_in_mass()

        await provider.update_config(
            _config(**{CONF_VOLUME_CONTROLS: ["media_player.kitchen"]}),
            {f"values/{CONF_POWER_CONTROLS}", f"values/{CONF_VOLUME_CONTROLS}"},
        )

        control = (provider._player_controls or {})["media_player.kitchen"]
        assert control.supports_volume is True
        assert control.supports_power is False


async def test_control_selection_drops_a_value_that_is_no_entity_id() -> None:
    """A stored selection that is no entity ID must not take the other controls down."""
    states = [_state("media_player.kitchen", "Kitchen")]
    async with _start_provider(
        states, **{CONF_POWER_CONTROLS: ["power_controls", "media_player.kitchen"]}
    ) as (provider, hass):
        await provider.loaded_in_mass()

        assert set(provider._player_controls or {}) == {"media_player.kitchen"}
        # Home Assistant refuses the whole subscription when it carries the stray value
        assert hass.subscriptions[-1] == ["media_player.kitchen"]


@pytest.mark.parametrize(
    "changed_keys",
    [
        {f"values/{CONF_URL}"},
        # a control list changing alongside another value still needs the reload
        {f"values/{CONF_POWER_CONTROLS}", f"values/{CONF_URL}"},
    ],
)
async def test_other_config_change_reloads_provider(changed_keys: set[str]) -> None:
    """Any change beyond the control lists reloads the provider."""
    async with _start_provider([]) as (provider, _hass):
        mass = cast("MagicMock", provider.mass)
        await provider.loaded_in_mass()
        mass.call_later.reset_mock()

        await provider.update_config(_config(**{CONF_POWER_CONTROLS: ["switch.amp"]}), changed_keys)

        mass.call_later.assert_called_once()
        assert mass.call_later.call_args[0][1] is mass.load_provider_config
