"""Tests for the Home Assistant provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from math import ceil
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
    CONF_MUTE_CONTROLS,
    CONF_POWER_CONTROLS,
    CONF_TTS_ENTITY,
    CONF_URL,
    CONF_VERIFY_SSL,
    CONF_VOLUME_CONTROLS,
    STATE_FETCH_BATCH_SIZE,
    HomeAssistantProvider,
    setup,
)
from music_assistant.providers.hass.constants import MediaPlayerEntityFeature

LAST_CHANGED = 1683832716.072648
LAST_CHANGED_ISO = "2023-05-11T19:18:36.072648+00:00"
CONTEXT_ID = "01H0640ES8JCY1NGTNW3V41T5T"


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


def _mass() -> MagicMock:
    """Return the Music Assistant dependencies used during provider startup."""
    mass = MagicMock()
    mass.cache = MagicMock()
    mass.http_session = MagicMock()
    mass.http_session_no_ssl = MagicMock()
    mass.create_task.side_effect = asyncio.create_task
    mass.players.register_player_control = AsyncMock()
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
        # compressed states keyed by entity_id, as delivered over the websocket
        self.compressed_states = {state["entity_id"]: _compressed(state) for state in states}
        # events delivered ahead of a subscription's initial state message
        self.leading_events: list[dict[str, Any]] = []
        self.deliver_initial_states = True
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
            return [{"entity_id": entity_id} for entity_id in self.compressed_states]
        except asyncio.CancelledError:
            self.registry_cancelled.set()
            raise
        finally:
            self.registry_stopped.set()

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

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self.calls.append("disconnect")
        self.connected = False
        self.disconnected = True


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


async def test_config_entries_survive_a_state_fetch_timeout() -> None:
    """A Home Assistant too slow to list its entities yields entries without options."""
    async with _start_provider([_state("switch.example", "Example")]) as (provider, _):
        # the entity sweep only runs for a provider the load machinery marked available
        provider.available = True
        with patch.object(provider, "get_states", AsyncMock(side_effect=TimeoutError)):
            entries = await provider.get_config_entries()

    keys = {entry.key for entry in entries}
    assert CONF_POWER_CONTROLS in keys
    assert CONF_TTS_ENTITY in keys
    assert all(not entry.options for entry in entries)


async def test_config_entries_list_entities_as_options() -> None:
    """A responsive Home Assistant offers its entities as player control options."""
    async with _start_provider([_state("switch.example", "Example")]) as (provider, _):
        provider.available = True
        entries = await provider.get_config_entries()

    power_controls = next(entry for entry in entries if entry.key == CONF_POWER_CONTROLS)
    assert [option.value for option in power_controls.options] == ["switch.example"]


async def test_config_entries_survive_an_entity_with_invalid_features() -> None:
    """An entity reporting an uninterpretable supported_features value is skipped."""
    broken = {
        "entity_id": "media_player.new_receiver",
        "state": "idle",
        "attributes": {"friendly_name": "New Receiver", "supported_features": []},
    }
    usable = {
        "entity_id": "media_player.old_receiver",
        "state": "idle",
        "attributes": {
            "friendly_name": "Old Receiver",
            "supported_features": int(
                MediaPlayerEntityFeature.TURN_ON
                | MediaPlayerEntityFeature.TURN_OFF
                | MediaPlayerEntityFeature.VOLUME_SET
            ),
        },
    }

    async with _start_provider([broken, usable]) as (provider, _):
        provider.available = True
        entries = await provider.get_config_entries()

    options_by_key = {entry.key: entry.options for entry in entries}
    for key in (CONF_POWER_CONTROLS, CONF_VOLUME_CONTROLS):
        assert [option.value for option in options_by_key[key] or ()] == [
            "media_player.old_receiver"
        ]


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

        await provider._register_player_controls()

        assert len(hass.subscriptions) == 4
        assert hass.active_subscriptions == 1
        # the previous control subscription is released before the new one is created
        assert hass.calls[-2:] == ["unsubscribe_entities", "subscribe_entities"]

        await provider.unload()

        assert hass.active_subscriptions == 0
