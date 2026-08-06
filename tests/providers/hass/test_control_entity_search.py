"""Tests for the Home Assistant player control entity search."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.errors import InvalidDataError, ProviderUnavailableError

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.helpers.api import APICommandHandler
from music_assistant.providers.hass import (
    CONF_AUTH_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    SEARCH_CONTROL_ENTITIES_COMMAND,
    HomeAssistantProvider,
    setup,
)
from music_assistant.providers.hass.constants import (
    CONF_MUTE_CONTROLS,
    CONF_POWER_CONTROLS,
    CONF_VOLUME_CONTROLS,
    CONTROL_DOMAINS,
    MediaPlayerEntityFeature,
)
from music_assistant.providers.hass.control_entities import (
    CONTROL_TYPE_CAPABILITIES,
    CONTROL_TYPE_DOMAINS,
    HassControlEntity,
    HassControlEntityGroup,
    HassControlEntitySearchResult,
)
from music_assistant.providers.hass.helpers import get_control_capabilities
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from hass_client.models import State

REGISTRY_LIST_COMMAND = "config/entity_registry/list_for_display"

FULL_MEDIA_PLAYER_FEATURES = int(
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
)

AREAS: list[dict[str, Any]] = [
    {"area_id": "area_living", "name": "Living Room", "aliases": [], "picture": None},
    {"area_id": "area_kitchen", "name": "Kitchen", "aliases": [], "picture": None},
    {"area_id": "area_office", "name": "Office", "aliases": [], "picture": None},
]

DEVICES: list[dict[str, Any]] = [
    {"id": "dev_living", "name": "Living Room Amp", "name_by_user": None, "area_id": "area_living"},
    {
        "id": "dev_kitchen",
        "name": "Speaker",
        "name_by_user": "Kitchen Speaker",
        "area_id": "area_kitchen",
    },
    {"id": "dev_attic", "name": "Attic Box", "name_by_user": None, "area_id": None},
]

# entity_id -> (device_id, area_id override, friendly_name, extra state attributes)
ENTITIES: dict[str, tuple[str | None, str | None, str, dict[str, Any]]] = {
    "media_player.living_amp": (
        "dev_living",
        None,
        "Amplifier",
        {"supported_features": FULL_MEDIA_PLAYER_FEATURES},
    ),
    "media_player.mass_player": (
        "dev_living",
        None,
        "MA Player",
        {"supported_features": FULL_MEDIA_PLAYER_FEATURES, "mass_player_type": "player"},
    ),
    "media_player.featureless": (None, None, "Featureless Player", {"supported_features": 0}),
    "switch.kitchen_power": ("dev_kitchen", None, "Kitchen Power", {}),
    "number.kitchen_volume": ("dev_kitchen", "area_office", "Kitchen Volume", {}),
    "input_boolean.standalone": (None, "area_living", "Standalone Toggle", {}),
    "input_number.attic_volume": ("dev_attic", None, "Attic Volume", {}),
    # not a control domain at all, so it must never surface
    "light.hallway": (None, "area_living", "Hallway Light", {}),
}


class _Cache:
    """Provide the slice of the cache controller that @use_cache relies on."""

    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}

    async def get_with_freshness(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        """Return the (data, is_fresh, found) triplet for the given key."""
        # the real controller reads the cache database here, so yield like it does:
        # @use_cache stores in the background, and only a yield lets that store land
        await asyncio.sleep(0)
        if key not in self.entries:
            return None, False, False
        return self.entries[key], True, True

    async def set(self, key: str, data: Any, **kwargs: Any) -> None:
        """Store data under the given key."""
        self.entries[key] = data


class _HomeAssistantClient:
    """Serve the registries and states of a small Home Assistant install."""

    def __init__(self) -> None:
        self.connected = False
        self.listener_started = asyncio.Event()
        # entity_ids per subscribe_entities call, in call order
        self.state_requests: list[list[str]] = []
        self.registry_list_calls = 0
        self.device_registry_calls = 0
        self.area_registry_calls = 0
        self.event_subscriptions: list[tuple[str, Callable[[dict[str, Any]], None]]] = []
        self.send_command = AsyncMock(side_effect=self._send_command)

    async def connect(self) -> None:
        """Connect the client."""
        self.connected = True

    async def start_listening(self) -> None:
        """Listen until the provider stops the listener task."""
        self.listener_started.set()
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self.connected = False

    async def get_device_registry(self) -> list[dict[str, Any]]:
        """Return the device registry listing."""
        self.device_registry_calls += 1
        return DEVICES

    async def get_area_registry(self) -> list[dict[str, Any]]:
        """Return the area registry listing."""
        self.area_registry_calls += 1
        return AREAS

    async def subscribe_entities(
        self, cb_func: Callable[[dict[str, Any]], None], entity_ids: list[str]
    ) -> Callable[[], None]:
        """Deliver the requested states and return the unsubscribe callable."""
        self.state_requests.append(list(entity_ids))
        initial = {
            entity_id: {"s": "idle", "a": _attributes(entity_id)}
            for entity_id in entity_ids
            if entity_id in ENTITIES
        }
        asyncio.get_running_loop().call_soon(cb_func, {"a": initial})
        return lambda: None

    async def subscribe_events(
        self, cb_func: Callable[[dict[str, Any]], None], event_type: str
    ) -> Callable[[], None]:
        """Register the event callback after command responses can be received."""
        await self.listener_started.wait()
        self.event_subscriptions.append((event_type, cb_func))
        return lambda: None

    def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Deliver an event to every subscriber of the given event type."""
        for subscribed_type, cb_func in self.event_subscriptions:
            if subscribed_type == event_type:
                cb_func({"event_type": event_type, "data": data})

    async def _send_command(self, command: str, **kwargs: Any) -> Any:
        """Return the response Home Assistant sends for the given websocket command."""
        if command != REGISTRY_LIST_COMMAND:
            return {}
        await self.listener_started.wait()
        self.registry_list_calls += 1
        return {
            "entity_categories": {},
            "entities": [
                {"ei": entity_id, "pl": "test", "di": device_id, "ai": area_id}
                for entity_id, (device_id, area_id, _, _) in ENTITIES.items()
            ],
        }


def _config() -> MagicMock:
    """Return a provider config exposing the persisted values via get_value."""
    persisted_values: dict[str, Any] = {
        CONF_URL: "http://homeassistant.local:8123",
        CONF_AUTH_TOKEN: "token",
        CONF_VERIFY_SSL: True,
        CONF_LOG_LEVEL: "GLOBAL",
        CONF_POWER_CONTROLS: [],
        CONF_MUTE_CONTROLS: [],
        CONF_VOLUME_CONTROLS: [],
    }
    config = MagicMock()
    config.instance_id = "hass--test"
    config.name = "Home Assistant"
    config.get_value.side_effect = persisted_values.get
    config.values = {}
    return config


def _mass() -> MagicMock:
    """Return the Music Assistant dependencies used during provider startup."""
    mass = MagicMock()
    mass.cache = _Cache()
    mass.http_session = MagicMock()
    mass.http_session_no_ssl = MagicMock()
    use_real_create_task(mass)
    mass.players.register_or_update_player_control = AsyncMock()
    mass.config.get = MagicMock(return_value={})
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    return mass


def _attributes(entity_id: str) -> dict[str, Any]:
    """Return the state attributes of the given entity."""
    _, _, friendly_name, extra = ENTITIES[entity_id]
    return {"friendly_name": friendly_name, **extra}


def _entity_ids(groups: list[HassControlEntityGroup]) -> list[str]:
    """Return the entity IDs of all groups, in result order."""
    return [entity["entity_id"] for group in groups for entity in group["entities"]]


@asynccontextmanager
async def _start_provider() -> AsyncIterator[tuple[HomeAssistantProvider, _HomeAssistantClient]]:
    """Start the provider with a connected mocked Home Assistant client."""
    hass = _HomeAssistantClient()
    manifest = MagicMock()
    manifest.domain = "hass"
    manifest.name = "Home Assistant"
    with patch("music_assistant.providers.hass.HomeAssistantClient", return_value=hass):
        provider = await setup(_mass(), manifest, _config())
        assert isinstance(provider, HomeAssistantProvider)
        async with asyncio.timeout(5):
            await provider.handle_async_init()
    try:
        yield provider, hass
    finally:
        await provider.unload()


async def test_search_command_is_exposed_on_the_api() -> None:
    """Expose the search as a read-only API command for as long as the provider is loaded."""
    async with _start_provider() as (provider, _):
        mass = cast("MagicMock", provider.mass)
        unregister = mass.register_api_command.return_value
        mass.register_api_command.assert_called_once_with(
            SEARCH_CONTROL_ENTITIES_COMMAND,
            provider.search_control_entities,
            required_scope=Scope.CONFIG_PROVIDERS_READ,
        )
        # the API layer must be able to resolve the command's signature and result type
        handler = APICommandHandler.parse(
            SEARCH_CONTROL_ENTITIES_COMMAND, provider.search_control_entities
        )
        assert handler.type_hints["return"] is HassControlEntitySearchResult
        unregister.assert_not_called()

    unregister.assert_called_once_with()


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        # entity id
        ("kitchen_power", ["switch.kitchen_power"]),
        # friendly name
        ("amplifier", ["media_player.living_amp"]),
        # device name (name_by_user wins over name)
        ("kitchen speaker", ["switch.kitchen_power", "number.kitchen_volume"]),
        # area name, inherited from the device
        ("living room", ["media_player.living_amp", "input_boolean.standalone"]),
        # area name of an entity that overrides its device's area
        ("office", ["number.kitchen_volume"]),
    ],
)
async def test_search_matches_every_searchable_field(search: str, expected: list[str]) -> None:
    """Match the search text against entity ID, entity name, device name and area name."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities(search=search)

    assert sorted(_entity_ids(result["groups"])) == sorted(expected)
    assert result["truncated"] is False


async def test_search_is_case_insensitive() -> None:
    """Match regardless of the casing of the search text."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities(search="AMPLIFIER")

    assert _entity_ids(result["groups"]) == ["media_player.living_amp"]


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        # surrounding whitespace is not part of any field
        ("  kitchen  ", ["switch.kitchen_power", "number.kitchen_volume"]),
        # the words may match different fields: entity name and area name here
        ("kitchen office", ["number.kitchen_volume"]),
        # every word has to match something
        ("kitchen hallway", []),
    ],
)
async def test_search_matches_every_word_separately(search: str, expected: list[str]) -> None:
    """Require each word of the search text to match a field, not the text as a whole."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities(search=search)

    assert sorted(_entity_ids(result["groups"])) == sorted(expected)


async def test_search_without_text_returns_all_eligible_entities() -> None:
    """Return every entity that can serve a control role when no search text is given."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities()

    assert sorted(_entity_ids(result["groups"])) == [
        "input_boolean.standalone",
        "input_number.attic_volume",
        "media_player.living_amp",
        "number.kitchen_volume",
        "switch.kitchen_power",
    ]


async def test_search_excludes_music_assistant_players() -> None:
    """Never offer Music Assistant's own exposed players as a control."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities(search="player")

    assert "media_player.mass_player" not in _entity_ids(result["groups"])
    # the entity without any usable feature is left out too
    assert "media_player.featureless" not in _entity_ids(result["groups"])


async def test_control_type_filters_on_capability() -> None:
    """Return only the entities that can serve the requested control role."""
    async with _start_provider() as (provider, hass):
        hass.state_requests.clear()
        volume = await provider.search_control_entities(control_type=CONF_VOLUME_CONTROLS)
        volume_requests = [entity_id for request in hass.state_requests for entity_id in request]
        power = await provider.search_control_entities(control_type=CONF_POWER_CONTROLS)
        mute = await provider.search_control_entities(control_type=CONF_MUTE_CONTROLS)

    assert sorted(_entity_ids(volume["groups"])) == [
        "input_number.attic_volume",
        "media_player.living_amp",
        "number.kitchen_volume",
    ]
    assert sorted(_entity_ids(power["groups"])) == [
        "input_boolean.standalone",
        "media_player.living_amp",
        "switch.kitchen_power",
    ]
    assert sorted(_entity_ids(mute["groups"])) == [
        "input_boolean.standalone",
        "media_player.living_amp",
        "switch.kitchen_power",
    ]
    # a volume search must not sweep the states of the on/off-only domains
    assert volume_requests == [
        "input_number.attic_volume",
        "media_player.featureless",
        "media_player.living_amp",
        "media_player.mass_player",
        "number.kitchen_volume",
    ]


def test_control_type_domains_cover_every_qualifying_domain() -> None:
    """Keep the per-role domain narrowing a superset of what can qualify for that role."""
    logger = logging.getLogger("test.hass")
    for control_type, domains in CONTROL_TYPE_DOMAINS.items():
        assert set(domains) <= set(CONTROL_DOMAINS)
        has_capability = CONTROL_TYPE_CAPABILITIES[control_type]
        for domain in CONTROL_DOMAINS:
            # the most capable entity the domain can hold, so no role is missed
            state = cast(
                "State",
                {
                    "entity_id": f"{domain}.probe",
                    "attributes": {"supported_features": FULL_MEDIA_PLAYER_FEATURES},
                },
            )
            capabilities = get_control_capabilities(state, logger)
            entity = cast(
                "HassControlEntity",
                {"entity_id": state["entity_id"], "name": "", **capabilities._asdict()},
            )
            if has_capability(entity):
                assert domain in domains, f"{domain} can serve {control_type} but is not swept"


async def test_control_type_reports_every_supported_role() -> None:
    """Report all roles an entity can serve, not just the one that was searched for."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities(
            search="kitchen_power", control_type=CONF_POWER_CONTROLS
        )

    entity = result["groups"][0]["entities"][0]
    assert (entity["power"], entity["volume"], entity["mute"]) == (True, False, True)


async def test_unknown_control_type_is_rejected() -> None:
    """Reject a control type that does not exist instead of returning everything."""
    async with _start_provider() as (provider, _):
        with pytest.raises(InvalidDataError, match="Invalid control type"):
            await provider.search_control_entities(control_type="brightness_controls")


@pytest.mark.parametrize("limit", [0, -1])
async def test_limit_below_one_is_rejected(limit: int) -> None:
    """Reject a limit that can never yield a result instead of returning nothing."""
    async with _start_provider() as (provider, _):
        with pytest.raises(InvalidDataError, match="Invalid limit"):
            await provider.search_control_entities(limit=limit)


async def test_results_are_grouped_by_device_and_area() -> None:
    """Group the entities by device and effective area, ordered by area, device and name."""
    async with _start_provider() as (provider, _):
        result = await provider.search_control_entities()

    assert [
        (group["device_id"], group["device_name"], group["area_name"], _entity_ids([group]))
        for group in result["groups"]
    ] == [
        ("dev_kitchen", "Kitchen Speaker", "Kitchen", ["switch.kitchen_power"]),
        # the entity inherits the area of its device
        ("dev_living", "Living Room Amp", "Living Room", ["media_player.living_amp"]),
        # an entity without a device falls back to a group of its own area
        (None, None, "Living Room", ["input_boolean.standalone"]),
        # the entity overrides the area it would inherit from its (Kitchen) device
        ("dev_kitchen", "Kitchen Speaker", "Office", ["number.kitchen_volume"]),
        # a device without an area sorts last
        ("dev_attic", "Attic Box", None, ["input_number.attic_volume"]),
    ]


async def test_limit_caps_entities_and_reports_truncation() -> None:
    """Cap the number of entities and tell the caller that matches were left out."""
    async with _start_provider() as (provider, _):
        limited = await provider.search_control_entities(limit=2)
        exact = await provider.search_control_entities(limit=5)

    assert _entity_ids(limited["groups"]) == ["switch.kitchen_power", "media_player.living_amp"]
    assert limited["truncated"] is True
    assert len(_entity_ids(exact["groups"])) == 5
    assert exact["truncated"] is False


async def test_limit_cannot_be_raised_past_the_maximum() -> None:
    """Cap what a caller can ask for, so no search can return an oversized response."""
    with patch(
        "music_assistant.providers.hass.control_entities.SEARCH_CONTROL_ENTITIES_MAX_LIMIT", 2
    ):
        async with _start_provider() as (provider, _):
            result = await provider.search_control_entities(limit=1000)

    assert _entity_ids(result["groups"]) == ["switch.kitchen_power", "media_player.living_amp"]
    assert result["truncated"] is True


async def test_result_does_not_alias_the_cached_candidates() -> None:
    """Keep a caller that edits the response from corrupting what the next search sees."""
    async with _start_provider() as (provider, _):
        first = await provider.search_control_entities(search="kitchen_power")
        first["groups"][0]["entities"][0]["name"] = "Mutated"
        second = await provider.search_control_entities(search="kitchen_power")

    assert second["groups"][0]["entities"][0]["name"] == "Kitchen Power"


async def test_consecutive_searches_share_one_state_sweep() -> None:
    """Sweep the Home Assistant states once and serve the next search from the cache."""
    async with _start_provider() as (provider, hass):
        hass.state_requests.clear()
        await provider.search_control_entities(search="living")
        await provider.search_control_entities(search="living room")
        sweeps = len(hass.state_requests)

    assert sweeps == 1


async def test_concurrent_searches_share_one_state_sweep() -> None:
    """Let searches that arrive together wait for a single state sweep."""
    async with _start_provider() as (provider, hass):
        hass.state_requests.clear()
        results = await asyncio.gather(
            provider.search_control_entities(search="kitchen"),
            provider.search_control_entities(search="living"),
        )
        sweeps = len(hass.state_requests)

    assert sweeps == 1
    assert _entity_ids(results[0]["groups"]) == [
        "switch.kitchen_power",
        "number.kitchen_volume",
    ]


async def test_power_and_mute_searches_share_one_state_sweep() -> None:
    """Reuse one sweep for the control roles that live in the same entity domains."""
    async with _start_provider() as (provider, hass):
        hass.state_requests.clear()
        await provider.search_control_entities(control_type=CONF_POWER_CONTROLS)
        await provider.search_control_entities(control_type=CONF_MUTE_CONTROLS)
        after_power_and_mute = len(hass.state_requests)
        await provider.search_control_entities(control_type=CONF_VOLUME_CONTROLS)
        after_volume = len(hass.state_requests)

    assert after_power_and_mute == 1
    # the volume roles live in other domains, so they need a sweep of their own
    assert after_volume == 2


async def test_entity_registry_change_forces_a_fresh_state_sweep() -> None:
    """Sweep again after Home Assistant reports an entity registry change."""
    async with _start_provider() as (provider, hass):
        hass.state_requests.clear()
        await provider.search_control_entities()
        hass.fire_event(
            "entity_registry_updated",
            {"action": "update", "entity_id": "media_player.living_amp"},
        )
        await provider.search_control_entities()
        sweeps = len(hass.state_requests)

    assert sweeps == 2


async def test_cached_candidates_expire() -> None:
    """Sweep again once the cached candidates have outlived their TTL."""
    with patch("music_assistant.providers.hass.control_entities.CONTROL_ENTITY_CACHE_TTL", 0):
        async with _start_provider() as (provider, hass):
            hass.state_requests.clear()
            await provider.search_control_entities()
            await provider.search_control_entities()
            sweeps = len(hass.state_requests)

    assert sweeps == 2


async def test_search_reuses_the_cached_registries() -> None:
    """Consult Home Assistant for the registries once and reuse them on the next search."""
    async with _start_provider() as (provider, hass):
        registry_calls_after_startup = hass.registry_list_calls
        await provider.search_control_entities()
        after_first = (
            hass.registry_list_calls,
            hass.device_registry_calls,
            hass.area_registry_calls,
        )
        await provider.search_control_entities(search="kitchen")
        after_second = (
            hass.registry_list_calls,
            hass.device_registry_calls,
            hass.area_registry_calls,
        )

    assert registry_calls_after_startup == 1
    assert after_first == (1, 1, 1)
    assert after_second == (1, 1, 1)


async def test_search_is_refused_once_the_provider_unloaded() -> None:
    """Refuse a search that arrives after the provider was unloaded."""
    async with _start_provider() as (provider, _):
        await provider.search_control_entities()

    with pytest.raises(ProviderUnavailableError):
        await provider.search_control_entities()


async def test_search_in_flight_during_unload_leaves_no_cache_behind() -> None:
    """Refuse a search that was suspended over the unload instead of refilling its cache."""
    entered_sweep = asyncio.Event()
    release_sweep = asyncio.Event()

    async with _start_provider() as (provider, _):
        original_get_states = provider.get_states

        async def _blocked_get_states(**kwargs: Any) -> Any:
            entered_sweep.set()
            await release_sweep.wait()
            return await original_get_states(**kwargs)

        with patch.object(provider, "get_states", _blocked_get_states):
            search = asyncio.create_task(provider.search_control_entities())
            async with asyncio.timeout(5):
                await entered_sweep.wait()

    release_sweep.set()
    with pytest.raises(ProviderUnavailableError):
        await search
    assert provider._control_entity_search._entries == {}
