"""Browser transport registration against the real MA player registry."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlaybackStateType
from aiosendspin.server import (
    ClientGroupChangedEvent,
    ClientUpdatedEvent,
    GroupMemberAddedEvent,
    GroupMemberRemovedEvent,
    GroupStateChangedEvent,
)
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType

from music_assistant.controllers.players import PlayerController
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider
from music_assistant.providers.universal_player.provider import UniversalPlayerProvider
from music_assistant.providers.web_kiosk.provider import WebKioskProvider
from music_assistant.providers.web_kiosk.sendspin_bridge import WebKioskSendspinBridgeManager

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient


CLIENT_ID = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"


def _configure_registry(mass_mock: Mock) -> tuple[PlayerController, list[asyncio.Task[Any]]]:
    """Configure isolated config dependencies for the actual player registry."""
    store: dict[str, Any] = {}
    configs: dict[str, PlayerConfig] = {}

    def create_config(
        player_id: str,
        provider: str,
        player_type: PlayerType = PlayerType.PLAYER,
        name: str | None = None,
        enabled: bool = True,
        **_kwargs: object,
    ) -> None:
        if player_id in configs:
            return
        configs[player_id] = PlayerConfig(
            values={},
            provider=provider,
            player_id=player_id,
            player_type=player_type,
            name=name,
            enabled=enabled,
        )
        store["players/" + player_id] = {
            "enabled": enabled,
            "provider": provider,
            "player_type": player_type.value,
            "values": _kwargs.get("values", {}),
        }

    mass_mock.config.create_default_player_config.side_effect = create_config
    mass_mock.config.get_base_player_config.side_effect = lambda pid, *_args: configs[pid]
    mass_mock.config.get_player_config = AsyncMock(side_effect=lambda pid, *_args: configs[pid])

    def get_config(key: str, default: Any = None) -> Any:
        if key in store:
            return store[key]
        parts = key.split("/")
        value = store.get("/".join(parts[:2]))
        for part in parts[2:]:
            if not isinstance(value, dict):
                return default
            value = value.get(part, default)
        return value if value is not None else default

    def set_config(key: str, value: Any) -> None:
        parts = key.split("/")
        if len(parts) < 3:
            store[key] = value
            return
        target = store.setdefault("/".join(parts[:2]), {})
        for part in parts[2:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    mass_mock.config.get.side_effect = get_config
    mass_mock.config.set.side_effect = set_config
    mass_mock.config.remove.side_effect = lambda key: store.pop(key, None)
    mass_mock.config.get_raw_player_config_value.side_effect = lambda pid, key, default=None: (
        get_config("players/" + pid + "/values/" + key, default)
    )
    mass_mock.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass_mock.player_queues.get_active_queue.return_value = None
    mass_mock.player_queues.on_player_register = AsyncMock()
    registry = PlayerController(mass_mock)
    mass_mock.players = registry
    tasks: list[asyncio.Task[Any]] = []

    def create_task(
        coro: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = create_task
    mass_mock.config.save = AsyncMock()
    mass_mock.player_queues.transfer_queue = AsyncMock()
    mass_mock.webserver.auth.replace_player_in_user_filters = AsyncMock()
    mass_mock.webserver.auth.remove_from_user_filters = AsyncMock()

    return registry, tasks


@pytest.fixture
def browser_registry(
    provider: WebKioskProvider, mass_mock: Mock
) -> tuple[
    PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
]:
    """Construct the actual MA providers and an already-connected browser client."""
    registry, tasks = _configure_registry(mass_mock)
    sendspin_manifest = Mock(domain="sendspin", name="Sendspin")
    sendspin_config = Mock(instance_id="sendspin", name="Sendspin")
    sendspin_config.get_value.side_effect = lambda _key, default=None: default
    sendspin = SendspinProvider(mass_mock, sendspin_manifest, sendspin_config)
    universal_manifest = Mock(domain="universal_player", name="Universal Player")
    universal_config = Mock(instance_id="universal_player", name="Universal Player")
    universal_config.get_value.side_effect = lambda _key, default=None: default
    universal = UniversalPlayerProvider(mass_mock, universal_manifest, universal_config)
    universal._lock = asyncio.Lock()
    providers = {
        "sendspin": sendspin,
        "universal_player": universal,
        provider.instance_id: provider,
    }
    mass_mock.get_provider.side_effect = lambda pid, **_: providers.get(pid)
    mass_mock.get_providers.return_value = list(providers.values())
    mass_mock.get_providers_supporting_feature.return_value = []

    hello = ClientHelloPayload(
        client_id=CLIENT_ID,
        name="Web Kiosk",
        version=1,
        supported_roles=["player@v1"],
        device_info=SendspinDeviceInfo(product_name="Web Kiosk", manufacturer="Google Inc."),
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM, sample_rate=48000, bit_depth=16, channels=2
                )
            ],
            buffer_capacity=1000,
        ),
    )
    client = Mock(
        info=hello, info_or_none=hello, is_connected=True, negotiated_role_ids=["player@v1"]
    )
    client.roles_by_family.return_value = []
    client.group.clients = []
    client.group.stop = AsyncMock()
    client.connection_security = None
    sendspin.server_api = Mock()
    sendspin.server_api.get_client.side_effect = lambda cid: client if cid == CLIENT_ID else None
    sendspin.server_api.remove_client = AsyncMock()
    sendspin.server_api.register_external_player.side_effect = RuntimeError(
        "Cannot register external player while client is connected"
    )
    return registry, sendspin, universal, client, tasks


async def test_connected_sendspin_is_adopted_by_kiosk(
    provider: WebKioskProvider,
    http_client: TestClient[Any, Any],
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A connected browser has one primary kiosk, including after client refreshes."""
    registry, sendspin, universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await universal.create_universal_player(
        "upb1adc7df", "Web Kiosk", deepcopy(native.device_info), [native.player_id]
    )
    await registry.register(native)
    assert native.protocol_parent_id == "upb1adc7df"
    manager = WebKioskSendspinBridgeManager(provider)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = manager
    ws = await http_client.ws_connect("/ws?device_id=browser&sendspin_client_id=" + CLIENT_ID)
    welcome = await ws.receive_json()
    kiosk = registry.get_player(welcome["player_id"])
    assert kiosk is not None
    try:
        for _ in range(2):
            await asyncio.gather(*tasks)
            assert [player.player_id for player in registry.iter_players()] == [kiosk.player_id]
            assert native.underlying_player_id == kiosk.player_id
            assert native.protocol_parent_id == kiosk.player_id
            assert (
                native.device_info.identifiers[IdentifierType.UUID]
                == kiosk.device_info.identifiers[IdentifierType.UUID]
            )
            assert PlayerFeature.VOLUME_SET in native.supported_features
            sendspin.event_cb(sendspin.server_api, ClientUpdatedEvent(CLIENT_ID))
        await asyncio.gather(*tasks)
    finally:
        await ws.close()
        provider.cancel_all_player_closes()
        await manager.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()


async def test_explicit_kiosk_removal_deletes_persisted_config(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Explicit removal deletes the kiosk configuration, unlike temporary disconnects."""
    registry, _sendspin, _universal, _client, tasks = browser_registry
    kiosk = await provider.get_or_register_player("wk_delete")
    assert kiosk is not None
    assert mass_mock.config.get("players/wk_delete") is not None
    await provider.remove_player(kiosk.player_id)
    assert registry.get_player(kiosk.player_id) is None
    assert mass_mock.config.get("players/wk_delete") is None
    await asyncio.gather(*tasks)


async def test_kiosk_state_follows_incoming_sendspin_group(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """The MA-visible state follows an active linked output through group playback changes."""
    registry, sendspin, _universal, client, tasks = browser_registry
    leader_id = "A" * 43
    leader_hello = replace(client.info, client_id=leader_id, name="Other speaker")
    leader_client = Mock(
        info=leader_hello,
        info_or_none=leader_hello,
        is_connected=True,
        negotiated_role_ids=["player@v1"],
        group=client.group,
    )
    leader_client.roles_by_family.return_value = []
    clients = {CLIENT_ID: client, leader_id: leader_client}
    cast("Mock", sendspin.server_api).get_client.side_effect = clients.get
    leader = SendspinPlayer(sendspin, leader_id, initial_hello=leader_hello)
    await registry.register(leader)
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    manager = WebKioskSendspinBridgeManager(provider)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = manager
    kiosk = await provider.get_or_register_player("wk_group", sendspin_client_id=CLIENT_ID)
    assert kiosk is not None
    leader_client.client_id = leader_id
    client.client_id = CLIENT_ID
    client.group.clients = [leader_client, client]
    leader.group_event_cb(client.group, GroupMemberAddedEvent(CLIENT_ID))
    await asyncio.gather(*tasks)
    try:
        for group_state, expected in (
            (PlaybackStateType.PLAYING, PlaybackState.PLAYING),
            (PlaybackStateType.PAUSED, PlaybackState.PAUSED),
            (PlaybackStateType.STOPPED, PlaybackState.IDLE),
        ):
            leader.group_event_cb(client.group, GroupStateChangedEvent(group_state))
            native.group_event_cb(client.group, GroupStateChangedEvent(group_state))
            kiosk.refresh_state()
            assert kiosk.state.playback_state == expected
        leader.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
        previous_group = client.group
        previous_group.clients = [leader_client]
        leader.group_event_cb(previous_group, GroupMemberRemovedEvent(CLIENT_ID))
        client.group = Mock(state=PlaybackStateType.STOPPED, clients=[client])
        native.event_cb(client, ClientGroupChangedEvent(client.group))
        await asyncio.gather(*tasks)
        kiosk.refresh_state()
        assert kiosk.state.playback_state == PlaybackState.IDLE
        assert kiosk.state.synced_to is None
        assert leader.state.playback_state == PlaybackState.PLAYING
    finally:
        await manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()
        await leader.on_unload()


async def test_dashboard_shows_effective_sendspin_playback_state(
    provider: WebKioskProvider,
    http_client: TestClient[Any, Any],
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """The dashboard reports the active output rather than the idle native HTML5 output."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    manager = WebKioskSendspinBridgeManager(provider)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = manager
    kiosk = await provider.get_or_register_player("wk_dashboard", sendspin_client_id=CLIENT_ID)
    assert kiosk is not None
    client.group.clients = [client]
    try:
        for group_state, expected in (
            (PlaybackStateType.PLAYING, "playing"),
            (PlaybackStateType.PAUSED, "paused"),
            (PlaybackStateType.STOPPED, "idle"),
        ):
            native.group_event_cb(client.group, GroupStateChangedEvent(group_state))
            kiosk.refresh_state()
            response = await http_client.get("/")
            assert response.status == 200
            assert f"Web Kiosk (dashboard) — {expected}</li>" in await response.text()
    finally:
        await manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()
