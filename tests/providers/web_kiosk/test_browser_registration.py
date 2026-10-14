"""Browser transport registration against the real MA player registry."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Coroutine
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
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
        ws = await connect_owned_ws(http, "/ws?device_id=lyrics&sendspin_client_id=" + CLIENT_ID)
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
        ws = await connect_owned_ws(http, "/ws?device_id=lyrics&sendspin_client_id=" + CLIENT_ID)
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
