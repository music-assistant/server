"""Browser transport registration against the real MA player registry."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
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
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import Track
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider
from music_assistant.providers.universal_player.provider import UniversalPlayerProvider
from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.ownership import VerifiedSendspinClaim
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider
from music_assistant.providers.web_kiosk.sendspin_bridge import (
    WebKioskSendspinBridge,
    WebKioskSendspinBridgeManager,
)

CLIENT_KEY = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
CLIENT_ID = (
    base64.urlsafe_b64encode(CLIENT_KEY.public_key().public_bytes_raw()).decode().rstrip("=")
)


async def connect_owned_ws(http: TestClient[Any, Any], path: str) -> Any:
    """Answer the connection-bound challenge with this test browser's private key."""
    ws = await http.ws_connect(path)
    challenge = await ws.receive_json()
    assert challenge["type"] == "sendspin_challenge"
    public = X25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(challenge["server_key"] + "=")
    )
    proof = hmac.new(
        CLIENT_KEY.exchange(public), challenge["message"].encode(), hashlib.sha256
    ).hexdigest()
    await ws.send_json({"type": "sendspin_proof", "proof": proof})
    return ws


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
        get_config(
            "players/" + pid + "/values/" + key, get_config("players/" + pid + "/" + key, default)
        )
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
    ws = await connect_owned_ws(
        http_client, "/ws?device_id=browser&sendspin_client_id=" + CLIENT_ID
    )
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
    kiosk = await provider.get_or_register_player(
        "wk_group",
        sendspin_client_id=CLIENT_ID,
        sendspin_claim=VerifiedSendspinClaim("wk_group", CLIENT_ID),
    )
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
    kiosk = await provider.get_or_register_player(
        "wk_dashboard",
        sendspin_client_id=CLIENT_ID,
        sendspin_claim=VerifiedSendspinClaim("wk_dashboard", CLIENT_ID),
    )
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


async def test_sendspin_track_updates_deliver_ma_lyrics(  # noqa: PLR0915
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Effective Sendspin metadata reaches the socket without an HTML5 play command."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    manager = WebKioskSendspinBridgeManager(provider)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = manager
    mass_mock.metadata = MetaDataController(mass_mock)
    track = Track(item_id="song", provider="library", name="Group song", provider_mappings=set())
    track.metadata.lyrics = "[00:00]Opening line\n[00:10]Next line"
    assert (await mass_mock.metadata.get_track_lyrics(track))[0] == track.metadata.lyrics
    queue = PlayerQueue(
        queue_id="wk_lyrics",
        active=True,
        display_name="Lyrics",
        available=True,
        items=1,
        state=PlaybackState.PLAYING,
        current_item=QueueItem.from_media_item("wk_lyrics", track),
    )
    mass_mock.player_queues.get.side_effect = lambda pid: queue if pid == queue.queue_id else None

    def active_queue(pid: str) -> PlayerQueue | None:
        player = registry.get_player(pid)
        return registry.get_active_queue(player) if player else None

    mass_mock.player_queues.get_active_queue.side_effect = active_queue
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    async with TestClient(TestServer(server.app)) as http:
        ws = await connect_owned_ws(
            http, "/ws?device_id=lyrics&lyrics=1&sendspin_client_id=" + CLIENT_ID
        )
        await ws.receive_json()
        kiosk = registry.get_player("wk_lyrics")
        assert kiosk is not None
        native.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
        kiosk.refresh_state()
        assert kiosk.state.current_media is not None
        assert kiosk.state.current_media.title == "Group song"
        messages: list[dict[str, Any]] = []
        with suppress(TimeoutError):
            async with asyncio.timeout(0.3):
                while not any(message.get("type") == "lyrics" for message in messages):
                    messages.append(await ws.receive_json())
        lyrics = next((message for message in messages if message.get("type") == "lyrics"), None)
        assert lyrics is not None, "MA has lyrics, but the Sendspin kiosk receives no lyric message"
        assert lyrics["lines"] == [
            {"t": 0.0, "text": "Opening line"},
            {"t": 10.0, "text": "Next line"},
        ]
        assert not any(message.get("type") == "play" for message in messages)
        old_seq = lyrics["seq"]
        next_track = Track(
            item_id="next", provider="library", name="Next song", provider_mappings=set()
        )
        next_track.metadata.lyrics = "[00:00]Next song lyrics"
        queue.current_item = QueueItem.from_media_item(queue.queue_id, next_track)
        kiosk.refresh_state()
        async with asyncio.timeout(1):
            while True:
                updated = await ws.receive_json()
                if updated.get("type") == "lyrics" and updated["seq"] != old_seq:
                    break
        assert updated["lines"] == [{"t": 0.0, "text": "Next song lyrics"}]
        await ws.close()
        ws = await connect_owned_ws(
            http, "/ws?device_id=lyrics&lyrics=1&sendspin_client_id=" + CLIENT_ID
        )
        await ws.receive_json()
        async with asyncio.timeout(1):
            while True:
                snapshot = await ws.receive_json()
                assert snapshot.get("type") != "play"
                if snapshot.get("type") == "lyrics":
                    break
        assert snapshot["lines"] == updated["lines"]
        await ws.close()
    provider.cancel_all_player_closes()
    await manager.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    await native.on_unload()


async def test_group_websocket_follows_membership_and_name(  # noqa: PLR0915
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
    kiosk = await provider.get_or_register_player(
        "wk_group",
        sendspin_client_id=CLIENT_ID,
        sendspin_claim=VerifiedSendspinClaim("wk_group", CLIENT_ID),
    )
    assert kiosk is not None
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    http = TestClient(TestServer(server.app))
    await http.start_server()
    ws = await connect_owned_ws(http, "/ws?device_id=group&sendspin_client_id=" + CLIENT_ID)
    await ws.receive_json()

    async def next_group() -> dict[str, Any]:
        async with asyncio.timeout(1):
            while True:
                message = await ws.receive_json()
                if message.get("type") == "group":
                    return cast("dict[str, Any]", message)

    assert (await next_group())["group_id"] is None
    leader_client.client_id = leader_id
    client.client_id = CLIENT_ID
    client.group.clients = [leader_client, client]
    leader.group_event_cb(client.group, GroupMemberAddedEvent(CLIENT_ID))
    native.event_cb(client, ClientGroupChangedEvent(client.group))
    await asyncio.gather(*tasks)
    try:
        leader.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
        native.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
        kiosk.refresh_state()
        message = await next_group()
        assert message["group_id"] is not None
        assert message["group_name"] == leader.state.name
        leader._attr_name = "Renamed speaker"
        leader.refresh_state()
        assert (await next_group())["group_name"] == "Renamed speaker"
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
        assert (await next_group())["group_id"] is None
    finally:
        await ws.close()
        await http.close()
        provider.cancel_all_player_closes()
        await manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()
        await leader.on_unload()


async def test_custom_name_reaches_browser_on_connect_and_rename(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Configured names reach the kiosk while its technical Player ID stays stable."""
    registry, _sendspin, _universal, _client, tasks = browser_registry
    kiosk = await provider.get_or_register_player("wk_custom")
    assert kiosk is not None
    kiosk.set_config(replace(kiosk.config, name="Kitchen"))
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    async with TestClient(TestServer(server.app)) as http:
        ws = await http.ws_connect("/ws?device_id=custom")
        welcome = await ws.receive_json()
        assert welcome["player_id"] == "wk_custom"
        assert welcome["custom_name"] == "Kitchen"
        for name in ("Bedroom", kiosk.name, None):
            await registry.on_player_config_change(replace(kiosk.config, name=name), {"name"})
            async with asyncio.timeout(1):
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "player":
                        break
            assert message["custom_name"] == name
        await ws.close()
    provider.cancel_all_player_closes()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_socket_cannot_claim_an_unrelated_sendspin_client(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A public client ID alone cannot mutate or take ownership of its player."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    before = dict(native.device_info.identifiers)
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    try:
        async with TestClient(TestServer(server.app)) as http:
            ws = await http.ws_connect("/ws?device_id=attacker&sendspin_client_id=" + CLIENT_ID)
            challenge = await ws.receive_json()
            assert challenge["type"] == "sendspin_challenge"
            await ws.send_json({"type": "sendspin_proof", "proof": "00" * 32})
            async with asyncio.timeout(1):
                await ws.receive()
            assert registry.get_player("wk_attacker") is None
            assert native.underlying_player_id is None
            assert native.device_info.identifiers == before
            await ws.close()
    finally:
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()


class QueueGroupFixture(Player):
    """A captured group driver using MA's real state and queue contracts."""

    def __init__(self, provider: WebKioskProvider, member: str) -> None:
        """Capture one native member with a playing, static group."""
        super().__init__(provider, "test_group")
        self._attr_name = "Test group"
        self._attr_type = PlayerType.GROUP
        self._attr_available = True
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        self._attr_group_members = [member]
        self._attr_static_group_members = [member]

    @property
    def is_active_session(self) -> bool:
        """Keep the configured member captured during this test."""
        return True

    async def stop(self) -> None:
        """Fail if fallback accidentally stops the captured group."""
        raise AssertionError("Fallback must not stop the group")


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("trigger", ["watchdog", "browser"])
async def test_html5_fallback_resumes_real_parent_queue(  # noqa: PLR0915
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
    trigger: str,
    grouped: bool,
) -> None:
    """An adopted protocol has no queue; fallback resumes its parent's native output."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    kiosk = await provider.get_or_register_player(
        "wk_fallback",
        sendspin_client_id=CLIENT_ID,
        sendspin_claim=VerifiedSendspinClaim("wk_fallback", CLIENT_ID),
    )
    assert kiosk is not None
    queues = PlayerQueuesController(mass_mock)
    mass_mock.player_queues = queues
    await queues.on_player_register(kiosk)
    assert queues.get(CLIENT_ID) is None
    group = QueueGroupFixture(provider, kiosk.player_id) if grouped else None
    if group:
        await registry.register(group)
        kiosk.refresh_state()
        assert kiosk.state.active_group == group.player_id
    queue = queues.get(group.player_id if group else kiosk.player_id)
    assert queue is not None
    queue.active = True
    queue.state = PlaybackState.PLAYING
    native.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
    kiosk.refresh_state()
    assert kiosk.active_output_protocol == CLIENT_ID

    async def resume(queue_id: str) -> None:
        assert queues.get(queue_id) is queue
        assert kiosk.active_output_protocol == "native"
        await kiosk.play_media(PlayerMedia(uri="native-stream", duration=180, source_id=queue_id))

    queues.resume = AsyncMock(side_effect=resume)  # type: ignore[method-assign]
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    http = TestClient(TestServer(server.app))
    await http.start_server()
    ws = await connect_owned_ws(http, "/ws?device_id=fallback&sendspin_client_id=" + CLIENT_ID)
    await ws.receive_json()
    try:
        cast("Mock", sendspin.server_api).get_client.return_value = None
        cast("Mock", sendspin.server_api).get_client.side_effect = None
        cast("Mock", sendspin.server_api).register_external_player.side_effect = None
        cast("Mock", sendspin.server_api).register_external_player.return_value = Mock(
            is_connected=False
        )
        cast(
            "Mock", sendspin.server_api
        ).register_external_player.return_value.roles_by_family.return_value = []
        bridge = WebKioskSendspinBridge(provider, kiosk, sendspin.server_api, CLIENT_ID)
        with patch(
            "music_assistant.providers.web_kiosk.sendspin_bridge.SENDSPIN_CONNECT_TIMEOUT", 0
        ):
            await bridge.start()
            if trigger == "watchdog":
                cast("Mock", sendspin.server_api).register_external_player.call_args.kwargs[
                    "on_stream_start"
                ](Mock())
            else:
                await ws.send_json({"type": "sendspin_fallback"})
            async with asyncio.timeout(1):
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "play":
                        break
            assert message["path"].startswith("/stream/wk_fallback?token=")
            await asyncio.gather(*tasks)
        await bridge.stop()
        assert kiosk.current_stream_url == "native-stream"
        assert kiosk.playback_state == PlaybackState.PLAYING
        assert queues.get(CLIENT_ID) is None
        if group:
            assert group.is_active_session
            assert queue.state == PlaybackState.PLAYING
    finally:
        await ws.close()
        await http.close()
        provider.cancel_all_player_closes()
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()
        if group:
            await registry.unregister(group.player_id)


FallbackSetup = tuple[WebKioskPlayer, PlayerQueuesController, SendspinPlayer, asyncio.Event]


async def _fallback_on_real_players(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
    during_release: Callable[[], Awaitable[None]] | None = None,
) -> FallbackSetup:
    """
    Play a queue on a kiosk's real Sendspin output, with a bridge release that waits.

    Returns the kiosk, the queue controller, the Sendspin player and the event that
    lets the release finish. ``during_release`` runs inside the release first.
    """
    registry, sendspin, _universal, client, _tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    kiosk = await provider.get_or_register_player(
        "wk_fallback",
        sendspin_client_id=CLIENT_ID,
        sendspin_claim=VerifiedSendspinClaim("wk_fallback", CLIENT_ID),
    )
    assert kiosk is not None
    queues = PlayerQueuesController(mass_mock)
    mass_mock.player_queues = queues
    await queues.on_player_register(kiosk)
    queues.update_items(
        kiosk.player_id,
        [QueueItem(queue_id=kiosk.player_id, queue_item_id="item", name="item", duration=180)],
    )
    queue = queues.get(kiosk.player_id)
    assert queue is not None
    queue.current_index = 0
    queue.active = True
    queue.state = PlaybackState.PLAYING
    native.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
    kiosk.refresh_state()
    assert kiosk.active_output_protocol == CLIENT_ID

    async def resume(queue_id: str) -> None:
        await kiosk.play_media(PlayerMedia(uri="native-stream", duration=180, source_id=queue_id))

    queues.resume = AsyncMock(side_effect=resume)  # type: ignore[method-assign]
    release = asyncio.Event()

    async def slow_release(_player_id: str) -> None:
        if during_release is not None:
            await during_release()
        await release.wait()

    provider.bridge_manager = Mock(remove_bridge=AsyncMock(side_effect=slow_release))
    return kiosk, queues, native, release


async def _finish(
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
    native: SendspinPlayer,
) -> None:
    """Cancel the core's background tasks and unload the Sendspin player."""
    tasks = browser_registry[4]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await native.on_unload()


@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_command_during_fallback_teardown_is_not_undone(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
    command: str,
) -> None:
    """A pause or stop issued while the Sendspin output is released wins over the resume."""
    registry = browser_registry[0]
    releasing = asyncio.Event()

    async def note_release() -> None:
        releasing.set()

    kiosk, queues, native, release = await _fallback_on_real_players(
        provider, mass_mock, browser_registry, note_release
    )
    try:
        fallback = asyncio.create_task(
            provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)
        )
        await asyncio.wait_for(releasing.wait(), 1)
        if command == "pause":
            issued = asyncio.create_task(registry.cmd_pause(kiosk.player_id))
        else:
            issued = asyncio.create_task(queues.stop(kiosk.player_id))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(fallback, issued), 2)

        if command == "pause":
            # The pause reaches the kiosk itself, so the queue resumes paused
            # instead of the output turning the pause into a stop.
            assert kiosk.current_stream_url == "native-stream"
            assert kiosk.playback_state == PlaybackState.PAUSED
        else:
            assert kiosk.playback_state == PlaybackState.IDLE
    finally:
        release.set()
        await _finish(browser_registry, native)


async def test_fallback_keeps_a_play_issued_while_it_resumes(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A pause during the teardown is not applied again over a later play."""
    registry = browser_registry[0]

    async def pause() -> None:
        await registry.cmd_pause("wk_fallback")

    kiosk, queues, native, release = await _fallback_on_real_players(
        provider, mass_mock, browser_registry, pause
    )
    release.set()

    async def play_during_resume(queue_id: str) -> None:
        await kiosk.play()
        await kiosk.play_media(PlayerMedia(uri="native-stream", duration=180, source_id=queue_id))

    queues.resume = AsyncMock(side_effect=play_during_resume)  # type: ignore[method-assign]
    try:
        await provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)

        assert kiosk.playback_state == PlaybackState.PLAYING
    finally:
        await _finish(browser_registry, native)


async def test_pause_while_the_fallback_resumes_is_kept(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A pause issued while the resumed stream is prepared is not overwritten by it."""
    registry = browser_registry[0]
    kiosk, queues, native, release = await _fallback_on_real_players(
        provider, mass_mock, browser_registry
    )
    release.set()

    async def pause_during_resume(queue_id: str) -> None:
        await registry.cmd_pause(kiosk.player_id)
        await kiosk.play_media(PlayerMedia(uri="native-stream", duration=180, source_id=queue_id))

    queues.resume = AsyncMock(side_effect=pause_during_resume)  # type: ignore[method-assign]
    try:
        await provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)

        assert kiosk.current_stream_url == "native-stream"
        assert kiosk.playback_state == PlaybackState.PAUSED
    finally:
        await _finish(browser_registry, native)


async def test_fallback_without_a_resume_drops_the_taken_over_playback(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A new Sendspin connection during the teardown leaves no made-up native playback."""

    async def reconnect() -> None:
        kiosk.sendspin_claim = VerifiedSendspinClaim("wk_fallback", CLIENT_ID)

    kiosk, queues, native, release = await _fallback_on_real_players(
        provider, mass_mock, browser_registry, reconnect
    )
    release.set()
    try:
        await provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)

        cast("AsyncMock", queues.resume).assert_not_awaited()
        assert kiosk.playback_state == PlaybackState.IDLE
        assert kiosk.current_media is None
    finally:
        await _finish(browser_registry, native)


async def test_failed_fallback_resume_drops_the_taken_over_playback(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A resume that fails leaves no native playback that nothing plays."""
    kiosk, queues, native, release = await _fallback_on_real_players(
        provider, mass_mock, browser_registry
    )
    release.set()
    queues.resume = AsyncMock(side_effect=QueueEmpty("empty"))  # type: ignore[method-assign]
    try:
        with pytest.raises(QueueEmpty):
            await provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)

        assert kiosk.playback_state == PlaybackState.IDLE
        assert not kiosk.sendspin_fallback
    finally:
        await _finish(browser_registry, native)


@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_failed_fallback_teardown_passes_a_command_on_to_sendspin(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
    command: str,
) -> None:
    """A pause or stop the kiosk took during a failed teardown stops the Sendspin output."""

    async def command_then_fail() -> None:
        await getattr(kiosk, command)()
        raise RuntimeError("teardown")

    kiosk, _queues, native, release = await _fallback_on_real_players(
        provider, mass_mock, browser_registry, command_then_fail
    )
    release.set()
    try:
        with (
            patch.object(native, "stop", AsyncMock()) as stop_output,
            pytest.raises(RuntimeError, match="teardown"),
        ):
            await provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)

        stop_output.assert_awaited_once()
        assert kiosk.active_output_protocol == CLIENT_ID
    finally:
        await _finish(browser_registry, native)


def _set_enabled(mass_mock: Mock, player_id: str, enabled: bool) -> PlayerConfig:
    """Persist an enabled flag the way the config manager exposes it to the registry."""
    base = mass_mock.config.get_base_player_config.side_effect
    config: PlayerConfig = replace(base(player_id), enabled=enabled)
    mass_mock.config.set(f"players/{player_id}/enabled", enabled)
    mass_mock.config.get_base_player_config.side_effect = lambda pid, *args: (
        config if pid == player_id else base(pid, *args)
    )
    mass_mock.config.get_player_config = AsyncMock(
        side_effect=lambda pid, *args: config if pid == player_id else base(pid, *args)
    )
    return config


async def test_disabling_connected_kiosk_stops_accepting_playback(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A disabled kiosk leaves the registry, and enabling restores its connected browser."""
    registry, _sendspin, _universal, _client, tasks = browser_registry
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    async with TestClient(TestServer(server.app)) as http:
        ws = await http.ws_connect("/ws?device_id=toggle&volume_control=1")
        await ws.receive_json()
        kiosk = registry.get_player("wk_toggle")
        assert isinstance(kiosk, WebKioskPlayer)
        assert PlayerFeature.VOLUME_SET in kiosk.supported_features

        disabled = _set_enabled(mass_mock, "wk_toggle", False)
        await registry.on_player_config_change(disabled, {"enabled"})
        await asyncio.gather(*tasks)
        assert registry.get_player("wk_toggle") is None
        assert not ws.closed

        enabled = _set_enabled(mass_mock, "wk_toggle", True)
        await registry.on_player_config_change(enabled, {"enabled"})
        await asyncio.gather(*tasks)
        restored = registry.get_player("wk_toggle")
        assert isinstance(restored, WebKioskPlayer)
        assert restored.enabled
        assert restored.available
        assert PlayerFeature.VOLUME_SET in restored.supported_features
        await ws.close()
    provider.cancel_all_player_closes()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_enabling_kiosk_disabled_at_startup_registers_connected_browser(
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A browser connected while its kiosk is disabled appears as soon as it is enabled."""
    registry, _sendspin, _universal, _client, tasks = browser_registry
    mass_mock.config.create_default_player_config(
        "wk_startup", provider.instance_id, PlayerType.PLAYER, enabled=False
    )
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    async with TestClient(TestServer(server.app)) as http:
        ws = await http.ws_connect("/ws?device_id=startup&volume_control=1")
        await ws.receive_json()
        assert registry.get_player("wk_startup") is None

        enabled = _set_enabled(mass_mock, "wk_startup", True)
        await registry.on_player_config_change(enabled, {"enabled"})
        await asyncio.gather(*tasks)
        kiosk = registry.get_player("wk_startup")
        assert isinstance(kiosk, WebKioskPlayer)
        assert kiosk.available
        assert PlayerFeature.VOLUME_SET in kiosk.supported_features
        await ws.close()
    provider.cancel_all_player_closes()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_first_ungrouped_play_uses_the_requested_sendspin_output(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Sendspin mode selects its linked output; fallback and explicit HTML5 stay native."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    try:
        kiosk = await provider.get_or_register_player(
            "wk_mode",
            sendspin_client_id=CLIENT_ID,
            sendspin_claim=VerifiedSendspinClaim("wk_mode", CLIENT_ID),
        )
        assert kiosk is not None
        assert kiosk.state.active_group is None
        target, protocol = registry._select_best_output_protocol(kiosk)
        assert target.player_id == CLIENT_ID
        assert protocol is not None

        kiosk.sendspin_fallback = True
        target, protocol = registry._select_best_output_protocol(kiosk)
        assert target is kiosk
        assert protocol is None

        html5 = await provider.get_or_register_player("wk_html5")
        assert html5 is not None
        target, protocol = registry._select_best_output_protocol(html5)
        assert target is html5
        assert protocol is None
    finally:
        provider.cancel_all_player_closes()
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()


async def test_same_device_reload_into_html5_restores_native_output(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Reloading a Sendspin kiosk with sendspin=0 inside the grace period plays natively."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    try:
        async with TestClient(TestServer(server.app)) as http:
            first = await connect_owned_ws(
                http, "/ws?device_id=mode&sendspin_client_id=" + CLIENT_ID
            )
            await first.receive_json()
            kiosk = registry.get_player("wk_mode")
            assert isinstance(kiosk, WebKioskPlayer)
            assert registry._select_best_output_protocol(kiosk)[0].player_id == CLIENT_ID

            await first.close()
            second = await http.ws_connect("/ws?device_id=mode&sendspin=0")
            await second.receive_json()
            assert registry.get_player("wk_mode") is kiosk
            target, protocol = registry._select_best_output_protocol(kiosk)
            assert target is kiosk
            assert protocol is None
            await second.close()
    finally:
        provider.cancel_all_player_closes()
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()


@pytest.mark.parametrize("paused", [False, True])
async def test_html5_reload_during_sendspin_playback_moves_it_to_native(  # noqa: PLR0915
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
    paused: bool,
) -> None:
    """Reloading the owner tab as HTML5 during Sendspin playback keeps audio; a pause is kept."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    http = TestClient(TestServer(server.app))
    await http.start_server()
    first = second = None
    try:
        first = await connect_owned_ws(http, "/ws?device_id=join&sendspin_client_id=" + CLIENT_ID)
        await first.receive_json()
        kiosk = registry.get_player("wk_join")
        assert isinstance(kiosk, WebKioskPlayer)
        queues = PlayerQueuesController(mass_mock)
        mass_mock.player_queues = queues
        await queues.on_player_register(kiosk)
        queue = queues.get(kiosk.player_id)
        assert queue is not None
        queue.active = True
        queue.state = PlaybackState.PAUSED if paused else PlaybackState.PLAYING
        state = PlaybackStateType.PAUSED if paused else PlaybackStateType.PLAYING
        native.group_event_cb(client.group, GroupStateChangedEvent(state))
        kiosk.refresh_state()
        assert kiosk.active_output_protocol == CLIENT_ID

        async def resume(queue_id: str) -> None:
            assert kiosk.active_output_protocol == "native"
            await kiosk.play_media(PlayerMedia(uri="native", duration=180, source_id=queue_id))

        queues.resume = AsyncMock(side_effect=resume)  # type: ignore[method-assign]
        # The owner tab reloads as HTML5 inside the disconnect grace period.
        await first.close()
        first = None
        await asyncio.sleep(0.05)
        second = await http.ws_connect("/ws?device_id=join&sendspin=0")
        await second.receive_json()
        played = None
        with suppress(TimeoutError):
            async with asyncio.timeout(0.5):
                while played is None:
                    message = await second.receive_json()
                    if message.get("type") == "play":
                        played = message
        assert kiosk.active_output_protocol == "native"
        if paused:
            queues.resume.assert_not_awaited()
            assert played is None
        else:
            queues.resume.assert_awaited_once_with(kiosk.player_id)
            assert played is not None
    finally:
        for socket in (first, second):
            if socket is not None:
                await socket.close()
        await http.close()
        provider.cancel_all_player_closes()
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()


async def test_unverified_tab_cannot_take_output_from_connected_sendspin_owner(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Knowing a device ID does not let another browser detach the owner's Sendspin output."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    try:
        async with TestClient(TestServer(server.app)) as http:
            owner = await connect_owned_ws(
                http, "/ws?device_id=owned&sendspin_client_id=" + CLIENT_ID
            )
            await owner.receive_json()
            kiosk = registry.get_player("wk_owned")
            assert isinstance(kiosk, WebKioskPlayer)
            native.group_event_cb(client.group, GroupStateChangedEvent(PlaybackStateType.PLAYING))
            kiosk.refresh_state()
            assert kiosk.active_output_protocol == CLIENT_ID

            intruder = await http.ws_connect("/ws?device_id=owned")
            await intruder.receive_json()
            await asyncio.sleep(0.05)
            assert not kiosk.sendspin_fallback
            assert kiosk.active_output_protocol == CLIENT_ID
            assert registry.get_player(CLIENT_ID) is native
            assert registry._select_best_output_protocol(kiosk)[0] is native
            await intruder.close()
            await owner.close()
    finally:
        provider.cancel_all_player_closes()
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()


def _proof(key: X25519PrivateKey, challenge: dict[str, Any]) -> str:
    """Answer a Sendspin ownership challenge with the given browser key."""
    public = X25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(challenge["server_key"] + "=")
    )
    return hmac.new(key.exchange(public), challenge["message"].encode(), hashlib.sha256).hexdigest()


async def test_overlapping_handshakes_cannot_replace_a_proven_identity(
    provider: WebKioskProvider,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """A second key proven during the first registration cannot take over the device."""
    registry, _sendspin, _universal, _client, tasks = browser_registry
    other_key = X25519PrivateKey.generate()
    other_id = (
        base64.urlsafe_b64encode(other_key.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    try:
        async with TestClient(TestServer(server.app)) as http:
            first = await http.ws_connect("/ws?device_id=race&sendspin_client_id=" + CLIENT_ID)
            second = await http.ws_connect("/ws?device_id=race&sendspin_client_id=" + other_id)
            first_challenge = await first.receive_json()
            second_challenge = await second.receive_json()
            await first.send_json(
                {"type": "sendspin_proof", "proof": _proof(CLIENT_KEY, first_challenge)}
            )
            assert (await first.receive_json())["type"] == "welcome"
            kiosk = registry.get_player("wk_race")
            assert isinstance(kiosk, WebKioskPlayer)
            assert kiosk.sendspin_client_id == CLIENT_ID

            await second.send_json(
                {"type": "sendspin_proof", "proof": _proof(other_key, second_challenge)}
            )
            async with asyncio.timeout(1):
                closed = await second.receive()
            assert closed.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
            assert kiosk.sendspin_client_id == CLIENT_ID
            assert kiosk.sendspin_claim is not None
            assert kiosk.sendspin_claim.client_id == CLIENT_ID
            await first.close()
    finally:
        provider.cancel_all_player_closes()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_sendspin_output_returns_after_an_html5_fallback(  # noqa: PLR0915
    provider: WebKioskProvider,
    mass_mock: Mock,
    browser_registry: tuple[
        PlayerController, SendspinProvider, UniversalPlayerProvider, Mock, list[asyncio.Task[Any]]
    ],
) -> None:
    """Fallback releases the kiosk's own bridge; a fresh claim brings the Sendspin output back."""
    registry, sendspin, _universal, client, tasks = browser_registry
    native = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
    await registry.register(native)
    provider.sendspin_bridge_enabled = True
    provider.bridge_manager = WebKioskSendspinBridgeManager(provider)
    server_api = cast("Mock", sendspin.server_api)
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    queues = PlayerQueuesController(mass_mock)
    mass_mock.player_queues = queues
    queues.resume = AsyncMock()  # type: ignore[method-assign]
    unregister = registry.unregister
    unregistered: list[str] = []

    async def track_unregister(player_id: str, *args: Any, **kwargs: Any) -> None:
        unregistered.append(player_id)
        await unregister(player_id, *args, **kwargs)

    registry.unregister = track_unregister  # type: ignore[method-assign]
    try:
        async with TestClient(TestServer(server.app)) as http:
            ws = await connect_owned_ws(http, "/ws?device_id=back&sendspin_client_id=" + CLIENT_ID)
            await ws.receive_json()
            kiosk = registry.get_player("wk_back")
            assert isinstance(kiosk, WebKioskPlayer)
            await queues.on_player_register(kiosk)
            queue = queues.get(kiosk.player_id)
            assert queue is not None
            queue.active = True
            assert registry._select_best_output_protocol(kiosk)[0] is native

            await provider.fallback_sendspin(kiosk.player_id, expected_client_id=CLIENT_ID)
            assert CLIENT_ID not in unregistered
            server_api.remove_client.assert_awaited_with(CLIENT_ID)
            assert registry._select_best_output_protocol(kiosk)[0] is kiosk

            # The Sendspin provider drops its player when the client is removed ...
            await unregister(CLIENT_ID)
            await ws.close()
            await asyncio.sleep(0.05)
            server_api.register_external_player.reset_mock()
            server_api.get_client.side_effect = lambda _cid: None
            server_api.register_external_player.side_effect = None
            bridge_client = Mock(is_connected=False)
            bridge_client.roles_by_family.return_value = []
            server_api.register_external_player.return_value = bridge_client

            # ... and the kiosk reloads in Sendspin mode with a fresh proof.
            again = await connect_owned_ws(
                http, "/ws?device_id=back&sendspin_client_id=" + CLIENT_ID
            )
            await again.receive_json()
            assert not kiosk.sendspin_fallback
            server_api.register_external_player.assert_called_once()

            # The new client registration makes the Sendspin provider add its player again.
            server_api.get_client.side_effect = lambda cid: client if cid == CLIENT_ID else None
            returned = SendspinPlayer(sendspin, CLIENT_ID, initial_hello=client.info)
            await registry.register(returned)
            assert registry._select_best_output_protocol(kiosk)[0] is returned
            await again.close()
    finally:
        provider.cancel_all_player_closes()
        await provider.bridge_manager.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await native.on_unload()
