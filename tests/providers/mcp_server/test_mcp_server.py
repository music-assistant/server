"""Tests for the builtin MCP server: transport, authentication, tools and discovery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import StreamReader, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.enums import PlaybackState, PlayerType, QueueOption, RepeatMode
from music_assistant_models.media_items import Playlist, SearchResults, Track

from music_assistant.constants import HASSIO_SUPERVISOR_IP, INGRESS_SERVER_PORT
from music_assistant.controllers.discovery import controller as discovery_module
from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from music_assistant.helpers.hassio import SupervisorError
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.providers.mcp_server import CONF_HOME_ASSISTANT_SECRET
from music_assistant.providers.mcp_server.auth import META_HA_USER_ID
from music_assistant.providers.mcp_server.registry import TOOLS
from music_assistant.providers.mcp_server.transport import MCPTransport

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant

INGRESS_IP = "172.30.32.1"
# the LAN address of the host this server runs on, which Home Assistant may connect from
HOST_IP = "192.168.1.2"
LAN_IP = "192.168.1.10"
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def host_addresses() -> Any:
    """Give the host this server runs on a LAN and a bridge address."""
    with patch(
        "music_assistant.providers.mcp_server.auth.get_ip_addresses",
        AsyncMock(return_value=(HOST_IP, INGRESS_IP)),
    ):
        yield


@pytest.fixture
async def admin_token(mass: MusicAssistant) -> str:
    """Create an admin user and return a token of it."""
    admin = await mass.webserver.auth.create_user(username="admin", role=UserRole.ADMIN)
    return await mass.webserver.auth.create_token(admin, "test")


async def test_route_is_registered(mass: MusicAssistant) -> None:
    """The plugin serves /mcp on the webserver, which only accepts POST."""
    assert mass.get_provider("mcp_server") is not None
    url = f"{mass.webserver.internal_base_url}/mcp"
    async with mass.http_session_no_ssl.get(url) as response:
        assert response.status == 405
        assert response.headers["Allow"] == "POST"


async def test_initialize_negotiates_version(mass: MusicAssistant, admin_token: str) -> None:
    """Initialize answers with the requested version when supported, else the latest one."""
    response = await _post(mass, _rpc("initialize", {"protocolVersion": "2025-06-18"}), admin_token)
    result = _body(response)["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "Music Assistant", "version": mass.version}
    assert result["instructions"]

    response = await _post(mass, _rpc("initialize", {"protocolVersion": "2024-11-05"}), admin_token)
    assert _body(response)["result"]["protocolVersion"] == "2025-11-25"


async def test_unsupported_protocol_version_header(mass: MusicAssistant, admin_token: str) -> None:
    """A request with an unsupported MCP-Protocol-Version header is refused."""
    response = await _post(
        mass, _rpc("ping"), admin_token, headers={"MCP-Protocol-Version": "2024-11-05"}
    )
    assert response.status == 400


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "text/plain"}, 415),
        ({"Accept": "text/event-stream"}, 406),
    ],
)
async def test_refuses_wrong_media_types(
    mass: MusicAssistant, admin_token: str, headers: dict[str, str], status: int
) -> None:
    """A request must send JSON and accept JSON."""
    response = await _post(mass, _rpc("ping"), admin_token, headers=headers)
    assert response.status == status


async def test_notification_and_batch(mass: MusicAssistant, admin_token: str) -> None:
    """A notification is accepted without a body, a batch is refused."""
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = await _post(mass, notification, admin_token)
    assert response.status == 202
    assert not response.body

    response = await _post(mass, [_rpc("ping")], admin_token)
    assert response.status == 400


async def test_unknown_method(mass: MusicAssistant, admin_token: str) -> None:
    """An unknown method is a JSON-RPC error."""
    response = await _post(mass, _rpc("resources/list"), admin_token)
    assert response.status == 200
    assert _body(response)["error"]["code"] == -32601


async def test_tools_list(mass: MusicAssistant, admin_token: str) -> None:
    """Every tool is listed with a description and a valid input schema."""
    response = await _post(mass, _rpc("tools/list"), admin_token)
    tools = _body(response)["result"]["tools"]
    assert {tool["name"] for tool in tools} == set(TOOLS)
    for tool in tools:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])
        for name, prop in schema["properties"].items():
            assert prop["type"] in ("string", "integer", "number", "boolean", "array"), name
            assert prop["description"], f"{tool['name']}.{name} has no description"


async def test_control_player(mass: MusicAssistant, admin_token: str) -> None:
    """control_player sends the command to the player named in the call."""
    player = _fake_player("kitchen_id", "Kitchen")
    with (
        patch.object(mass.players, "all_players", return_value=[player]),
        patch.object(mass.players, "cmd_pause", AsyncMock()) as cmd_pause,
    ):
        response = await _post(
            mass,
            _call("control_player", {"player": "kitchen", "command": "pause"}),
            admin_token,
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )
    result = _body(response)["result"]
    assert not result["isError"]
    cmd_pause.assert_awaited_once_with("kitchen_id")
    assert result["structuredContent"]["player_id"] == "kitchen_id"
    assert json_loads(result["content"][0]["text"]) == result["structuredContent"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"media": "Song"}, "track"),
        ({"media": "library://track/2"}, "track"),
        ({"media": "Song", "radio_mode": True}, "radio_playlist://playlist/library://track/2"),
    ],
)
async def test_play(
    mass: MusicAssistant, admin_token: str, arguments: dict[str, Any], expected: str
) -> None:
    """Play resolves a name or a uri and starts an endless mix from the item with radio_mode."""
    track = _track("2", "Song")
    search = AsyncMock(return_value=SearchResults(tracks=[_track("1", "Song (Live)"), track]))
    with (
        patch.object(mass.players, "all_players", return_value=[_fake_player("k_id", "Kitchen")]),
        patch.object(mass.music, "search", search),
        patch.object(mass.music, "get_item_by_uri", AsyncMock(return_value=track)) as by_uri,
        patch.object(mass.player_queues, "play_media", AsyncMock()) as play_media,
    ):
        response = await _post(mass, _call("play", {"player": "Kitchen", **arguments}), admin_token)
    assert not _body(response)["result"]["isError"]
    to_play = track if expected == "track" else expected
    play_media.assert_awaited_once_with("k_id", to_play, QueueOption.REPLACE)
    if "://" in arguments["media"]:
        by_uri.assert_awaited_once_with("library://track/2")
        search.assert_not_awaited()
    else:
        by_uri.assert_not_awaited()


async def test_add_to_playlist(mass: MusicAssistant, admin_token: str) -> None:
    """add_to_playlist adds to a library playlist and refuses a playlist of a provider."""
    library_playlist = Playlist(
        item_id="5", provider="library", name="Mix", provider_mappings=set()
    )
    provider_playlist = Playlist(
        item_id="abc", provider="spotify", name="Mix", provider_mappings=set()
    )
    uris = ["library://track/1"]
    for playlist, refused in ((provider_playlist, True), (library_playlist, False)):
        with (
            patch.object(mass.music, "get_item_by_uri", AsyncMock(return_value=playlist)),
            patch.object(mass.music.playlists, "add_playlist_tracks", AsyncMock()) as add_tracks,
        ):
            response = await _post(
                mass,
                _call("add_to_playlist", {"playlist": playlist.uri, "uris": uris}),
                admin_token,
            )
        assert _body(response)["result"]["isError"] is refused
        if refused:
            add_tracks.assert_not_awaited()
        else:
            add_tracks.assert_awaited_once_with("5", uris)


@pytest.mark.parametrize(("limit", "expected"), [(None, 20), (-1, 1), (1000, 50)])
async def test_get_queue(
    mass: MusicAssistant, admin_token: str, limit: int | None, expected: int
) -> None:
    """get_queue lists the items after the current one, with the limit kept within bounds."""
    queue = MagicMock()
    queue.queue_id = "k_id"
    queue.current_index = 3
    queue.current_item = None
    queue.state = PlaybackState.PLAYING
    queue.repeat_mode = RepeatMode.OFF
    arguments: dict[str, Any] = {"player": "Kitchen"}
    if limit is not None:
        arguments["limit"] = limit
    with (
        patch.object(mass.players, "all_players", return_value=[_fake_player("k_id", "Kitchen")]),
        patch.object(mass.player_queues, "get_player_active_queue", return_value=queue),
        patch.object(mass.player_queues, "get_queue_items", return_value=[]) as get_queue_items,
    ):
        response = await _post(mass, _call("get_queue", arguments), admin_token)
    assert not _body(response)["result"]["isError"]
    get_queue_items.assert_called_once_with("k_id", expected, 4)


async def test_tool_errors(mass: MusicAssistant, admin_token: str) -> None:
    """An unknown player or a bad argument comes back as a tool error, not a protocol error."""
    player = _fake_player("kitchen_id", "Kitchen")
    with patch.object(mass.players, "all_players", return_value=[player]):
        response = await _post(
            mass, _call("control_player", {"player": "Attic", "command": "stop"}), admin_token
        )
        result = _body(response)["result"]
        assert result["isError"]
        assert "Kitchen" in result["content"][0]["text"]
        assert "structuredContent" not in result

        response = await _post(
            mass, _call("control_player", {"player": "Kitchen", "command": "dance"}), admin_token
        )
        assert _body(response)["result"]["isError"]

        response = await _post(
            mass, _call("control_player", {"player": "Kitchen", "command": None}), admin_token
        )
        assert (
            "Invalid value for argument command" in _body(response)["result"]["content"][0]["text"]
        )

    response = await _post(mass, _call("no_such_tool", {}), admin_token)
    assert _body(response)["error"]["code"] == -32602


async def test_guest_refused_on_write_tool(mass: MusicAssistant) -> None:
    """A guest may not use a tool that changes the library."""
    guest = await mass.webserver.auth.create_user(username="guest", role=UserRole.GUEST)
    token = await mass.webserver.auth.create_token(guest, "test")
    with patch.object(mass.music, "set_item_favorite", AsyncMock()) as set_favorite:
        response = await _post(mass, _call("add_favorite", {"uri": "library://track/1"}), token)
    result = _body(response)["result"]
    assert result["isError"]
    assert "Not allowed" in result["content"][0]["text"]
    set_favorite.assert_not_awaited()


async def test_public_site_requires_token(mass: MusicAssistant) -> None:
    """Without a token the public site answers 401 with a bearer challenge."""
    await _create_admin(mass)
    response = await _post(mass, _rpc("ping"), None)
    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="Music Assistant"'


async def test_origin_check(mass: MusicAssistant, admin_token: str) -> None:
    """A browser request from a foreign origin is refused, one from the server itself not."""
    response = await _post(
        mass, _rpc("ping"), admin_token, headers={"Origin": "https://evil.example"}
    )
    assert response.status == 403
    response = await _post(
        mass, _rpc("ping"), admin_token, headers={"Origin": mass.webserver.base_url}
    )
    assert response.status == 200


@pytest.mark.parametrize("peer", [HOST_IP, INGRESS_IP, "127.0.0.1", f"::ffff:{HOST_IP}"])
async def test_home_assistant_needs_no_token(mass: MusicAssistant, peer: str) -> None:
    """Home Assistant on the host reaches the internal site without a token, as its system user."""
    await _create_admin(mass)
    seen = _capture_user(mass)
    with seen.patch:
        response = await _post(
            mass, _call("get_players", {}), None, peer=peer, ingress=True, home_assistant=True
        )
    assert not _body(response)["result"]["isError"]
    system_user = await mass.webserver.auth.get_homeassistant_system_user()
    assert seen.users == [system_user]


@pytest.mark.parametrize(
    ("peer", "home_assistant"),
    [
        ("172.30.33.7", True),
        (HASSIO_SUPERVISOR_IP, True),
        ("172.30.33.7", False),
        (HASSIO_SUPERVISOR_IP, False),
        # the host itself on the public endpoint, without the secret
        (INGRESS_IP, False),
        (HOST_IP, False),
    ],
)
async def test_other_requests_on_internal_site_need_token(
    mass: MusicAssistant, peer: str, home_assistant: bool
) -> None:
    """Only the host on the private endpoint goes without a token, the Ingress proxy included."""
    await _create_admin(mass)
    response = await _post(
        mass, _rpc("ping"), None, peer=peer, ingress=True, home_assistant=home_assistant
    )
    assert response.status == 401


async def test_private_endpoint_routes(mass: MusicAssistant) -> None:
    """The private endpoint is only served on the secret path, which still needs a token here."""
    await _create_admin(mass)
    base_url = mass.webserver.internal_base_url
    for path, status in ((f"/mcp/{_secret(mass)}", 401), ("/mcp/not-the-secret", 404)):
        async with mass.http_session_no_ssl.post(
            f"{base_url}{path}", json=_rpc("ping"), headers=JSON_HEADERS
        ) as response:
            assert response.status == status, path


async def test_well_known_probes_not_logged(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The OAuth metadata probes on the private endpoint answer 404 without logging the secret."""
    secret = _secret(mass)
    base_url = mass.webserver.internal_base_url
    for path in (
        f"/.well-known/oauth-protected-resource/mcp/{secret}",
        f"/.well-known/oauth-authorization-server/mcp/{secret}",
        f"/.well-known/openid-configuration/mcp/{secret}",
        f"/mcp/{secret}/.well-known/openid-configuration",
    ):
        async with mass.http_session_no_ssl.get(f"{base_url}{path}") as response:
            assert response.status == 404, path
    assert secret not in caplog.text


async def test_secret_survives_reload(mass: MusicAssistant) -> None:
    """The secret is generated once and kept when the plugin loads again."""
    provider = mass.get_provider("mcp_server")
    assert provider is not None
    secret = _secret(mass)
    assert len(secret) >= 32
    await mass.unload_provider(provider.instance_id)
    await mass.load_provider(provider.instance_id)
    assert _secret(mass) == secret
    assert mass.get_provider("mcp_server") is not provider


@pytest.mark.parametrize(
    ("peer", "home_assistant", "with_token"),
    [
        # Home Assistant before 2026.12, without a token on the private endpoint
        (INGRESS_IP, True, False),
        # Home Assistant 2026.12 and newer, with the system token of the discovery
        (HOST_IP, False, True),
        ("172.30.33.7", True, True),
    ],
)
async def test_meta_impersonates_linked_home_assistant_user(
    mass: MusicAssistant, peer: str, home_assistant: bool, with_token: bool
) -> None:
    """For Home Assistant the HA user in _meta is impersonated; an unlinked one is not."""
    await _create_admin(mass)
    token = await mass.webserver.auth.get_homeassistant_system_user_token() if with_token else None
    member = await mass.webserver.auth.create_user(username="member")
    await mass.webserver.auth.link_user_to_provider(
        member, AuthProviderType.HOME_ASSISTANT, "ha-member"
    )
    seen = _capture_user(mass)
    with seen.patch:
        for ha_user_id in ("ha-member", "ha-unknown"):
            await _post(
                mass,
                # the literal key Home Assistant sends, so a rename of the constant shows here
                _call("get_players", {}, meta={"io.home-assistant/user_id": ha_user_id}),
                token,
                peer=peer,
                ingress=True,
                home_assistant=home_assistant,
            )
    system_user = await mass.webserver.auth.get_homeassistant_system_user()
    assert [user.user_id if user else None for user in seen.users] == [
        member.user_id,
        system_user.user_id,
    ]


async def test_meta_ignored_with_token(mass: MusicAssistant) -> None:
    """With a bearer token the _meta user is ignored and the call runs as the token's user."""
    admin = await _create_admin(mass)
    token = await mass.webserver.auth.create_token(admin, "test")
    member = await mass.webserver.auth.create_user(username="member")
    await mass.webserver.auth.link_user_to_provider(
        member, AuthProviderType.HOME_ASSISTANT, "ha-member"
    )
    seen = _capture_user(mass)
    with seen.patch:
        await _post(mass, _call("get_players", {}, meta={META_HA_USER_ID: "ha-member"}), token)
    assert [user.user_id if user else None for user in seen.users] == [admin.user_id]


@pytest.mark.parametrize("home_assistant", [False, True])
async def test_home_assistant_token_refused_on_public_site(
    mass: MusicAssistant, home_assistant: bool
) -> None:
    """The token of the Home Assistant system user only works on the internal site."""
    await _create_admin(mass)
    token = await mass.webserver.auth.get_homeassistant_system_user_token()
    response = await _post(mass, _rpc("ping"), token, peer=HOST_IP, home_assistant=home_assistant)
    assert response.status == 401


async def test_discovery_announced_and_withdrawn(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As add-on the MCP server is offered to Home Assistant and withdrawn on unload."""
    monkeypatch.setenv("HOSTNAME", "abcd1234-music-assistant")
    monkeypatch.setattr(mass, "running_as_hass_addon", True)
    supervisor_request = AsyncMock(return_value={"uuid": "discovery-uuid"})
    monkeypatch.setattr(discovery_module, "supervisor_request", supervisor_request)
    provider = mass.get_provider("mcp_server")
    assert provider is not None

    with patch.object(mass, "create_task") as create_task:
        await provider.loaded_in_mass()
    # run the announce the provider schedules
    await create_task.call_args.args[0]
    supervisor_request.assert_awaited_once_with(
        mass,
        "post",
        "/discovery",
        json_data={
            "service": "mcp",
            "config": {
                "url": f"http://abcd1234-music-assistant:{INGRESS_SERVER_PORT}/mcp/{_secret(mass)}",
                "token": await mass.webserver.auth.get_homeassistant_system_user_token(),
            },
        },
    )

    supervisor_request.reset_mock()
    await mass.unload_provider(provider.instance_id)
    supervisor_request.assert_awaited_once_with(mass, "delete", "/discovery/discovery-uuid")


async def test_discovery_refused_logs_once(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Supervisor that does not accept the mcp service yet is logged once, at info level."""
    monkeypatch.setenv("HOSTNAME", "abcd1234-music-assistant")
    monkeypatch.setattr(mass, "running_as_hass_addon", True)
    supervisor_request = AsyncMock(side_effect=SupervisorError("Forbidden", 403))
    monkeypatch.setattr(discovery_module, "supervisor_request", supervisor_request)

    await mass.discovery.announce_mcp_server("/mcp")
    await mass.discovery.announce_mcp_server("/mcp")

    records = [r for r in caplog.records if "MCP server discovery" in r.getMessage()]
    assert [record.levelname for record in records] == ["INFO"]


async def test_meta_disabled_linked_user_refused(mass: MusicAssistant) -> None:
    """A Home Assistant user linked to a disabled account is refused, not run as the service user."""
    admin = await _create_admin(mass)
    member = await mass.webserver.auth.create_user(username="member")
    await mass.webserver.auth.link_user_to_provider(
        member, AuthProviderType.HOME_ASSISTANT, "ha-member"
    )
    set_current_user(admin)
    await mass.webserver.auth.disable_user(member.user_id)
    seen = _capture_user(mass)
    with seen.patch:
        response = await _post(
            mass,
            _call("get_players", {}, meta={META_HA_USER_ID: "ha-member"}),
            None,
            peer=INGRESS_IP,
            ingress=True,
            home_assistant=True,
        )
    result = _body(response)["result"]
    assert result["isError"]
    assert "disabled" in result["content"][0]["text"]
    assert seen.users == []


async def test_ingress_proxy_runs_as_ingress_user(mass: MusicAssistant) -> None:
    """A request relayed by the Ingress proxy runs as the signed in HA user, ignoring _meta."""
    await _create_admin(mass)
    member = await mass.webserver.auth.create_user(username="member")
    await mass.webserver.auth.link_user_to_provider(
        member, AuthProviderType.HOME_ASSISTANT, "ha-member"
    )
    other = await mass.webserver.auth.create_user(username="other")
    await mass.webserver.auth.link_user_to_provider(
        other, AuthProviderType.HOME_ASSISTANT, "ha-other"
    )
    seen = _capture_user(mass)
    with (
        seen.patch,
        patch.object(
            auth_middleware, "get_ha_user_details", AsyncMock(return_value=(None, None, None))
        ),
    ):
        response = await _post(
            mass,
            _call("get_players", {}, meta={META_HA_USER_ID: "ha-other"}),
            None,
            headers={"X-Remote-User-ID": "ha-member", "X-Remote-User-Name": "member"},
            peer=HASSIO_SUPERVISOR_IP,
            ingress=True,
        )
    assert not _body(response)["result"]["isError"]
    assert [user.user_id if user else None for user in seen.users] == [member.user_id]


async def test_player_filter_limits_players(mass: MusicAssistant) -> None:
    """A user limited to some players neither sees nor controls the others."""
    await _create_admin(mass)
    member = await mass.webserver.auth.create_user(username="member")
    await mass.webserver.auth.update_user_filters(member, ["kitchen_id"])
    token = await mass.webserver.auth.create_token(member, "test")
    players = [_fake_player("kitchen_id", "Kitchen"), _fake_player("attic_id", "Attic")]
    with (
        patch.object(mass.players, "iter_players", return_value=iter(players)),
        patch.object(mass.players, "cmd_pause", AsyncMock()) as cmd_pause,
    ):
        response = await _post(
            mass, _call("control_player", {"player": "Attic", "command": "pause"}), token
        )
    result = _body(response)["result"]
    assert result["isError"]
    assert "Available players: Kitchen" in result["content"][0]["text"]
    cmd_pause.assert_not_awaited()


async def test_discovery_skipped_when_not_addon(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is offered to Home Assistant when not running as add-on."""
    monkeypatch.setenv("HOSTNAME", "abcd1234-music-assistant")
    supervisor_request = AsyncMock(return_value={"uuid": "discovery-uuid"})
    monkeypatch.setattr(discovery_module, "supervisor_request", supervisor_request)
    provider = mass.get_provider("mcp_server")
    assert provider is not None

    await provider.loaded_in_mass()
    await mass.discovery.announce_mcp_server("/mcp")
    await asyncio.sleep(0)
    supervisor_request.assert_not_awaited()


async def test_discovery_reannounced_daily(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily announce to Home Assistant posts the MCP server again, with the system token."""
    monkeypatch.setenv("HOSTNAME", "abcd1234-music-assistant")
    monkeypatch.setattr(mass, "running_as_hass_addon", True)
    supervisor_request = AsyncMock(return_value={"uuid": "discovery-uuid"})
    monkeypatch.setattr(discovery_module, "supervisor_request", supervisor_request)
    await mass.discovery.announce_mcp_server("/mcp")
    supervisor_request.reset_mock()

    await mass.discovery._announce_to_homeassistant()

    services = [call.kwargs["json_data"]["service"] for call in supervisor_request.await_args_list]
    assert services == ["music_assistant", "mcp"]
    configs = [call.kwargs["json_data"]["config"] for call in supervisor_request.await_args_list]
    assert configs[1]["token"] == configs[0]["auth_token"]


class _UserCapture:
    """Record the acting user each time the players are listed."""

    def __init__(self, mass: MusicAssistant) -> None:
        self.users: list[User | None] = []
        self.patch = patch.object(mass.players, "all_players", side_effect=self._record)

    def _record(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.users.append(get_current_user())
        return []


def _capture_user(mass: MusicAssistant) -> _UserCapture:
    """Return a capture of the user the get_players tool runs as."""
    return _UserCapture(mass)


async def _create_admin(mass: MusicAssistant) -> User:
    """Create an admin user, so the server is out of setup mode."""
    return await mass.webserver.auth.create_user(username="admin", role=UserRole.ADMIN)


def _secret(mass: MusicAssistant) -> str:
    """Return the secret of the private Home Assistant endpoint."""
    provider = mass.get_provider("mcp_server")
    assert provider is not None
    return str(provider.get_config_value(CONF_HOME_ASSISTANT_SECRET))


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a JSON-RPC request message."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _call(name: str, arguments: dict[str, Any], meta: dict[str, Any] | None = None) -> Any:
    """Return a tools/call request message."""
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return _rpc("tools/call", params)


async def _post(
    mass: MusicAssistant,
    message: Any,
    token: str | None,
    *,
    headers: dict[str, str] | None = None,
    peer: str = LAN_IP,
    ingress: bool = False,
    home_assistant: bool = False,
) -> web.Response:
    """
    Send a message to the MCP endpoint and return the response.

    :param mass: The server to send the message to.
    :param message: The JSON message to send.
    :param token: The bearer token to send, if any.
    :param headers: Extra headers, which replace the default ones.
    :param peer: The address the request comes from.
    :param ingress: Whether the request arrives on the internal (ingress) site.
    :param home_assistant: Whether the request is sent to the private Home Assistant endpoint.
    """
    all_headers = dict(JSON_HEADERS)
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    all_headers.update(headers or {})
    app = web.Application()
    app["mass"] = mass
    local_ip = INGRESS_IP if ingress else LAN_IP
    if ingress:
        app["ingress_site"] = (INGRESS_IP, INGRESS_SERVER_PORT)
    extra_info = {"sockname": (local_ip, INGRESS_SERVER_PORT), "peername": (peer, 54321)}
    transport = MagicMock()
    transport.get_extra_info.side_effect = extra_info.get
    payload = StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(json_dumps(message).encode())
    payload.feed_eof()
    request = make_mocked_request(
        "POST", "/mcp", headers=all_headers, app=app, transport=transport, payload=payload
    )
    mcp_transport = MCPTransport(mass)
    if home_assistant:
        return await mcp_transport.handle_home_assistant_request(request)
    return await mcp_transport.handle_request(request)


def _body(response: web.Response) -> dict[str, Any]:
    """Return the JSON body of a response."""
    assert response.text is not None
    body: dict[str, Any] = json_loads(response.text)
    return body


def _track(item_id: str, name: str) -> Track:
    """Return a library track."""
    return Track(item_id=item_id, provider="library", name=name, provider_mappings=set())


def _fake_player(player_id: str, name: str) -> MagicMock:
    """Return a stand-in for a player with the state the tools read."""
    player = MagicMock()
    player.player_id = player_id
    player.state.player_id = player_id
    player.state.name = name
    player.state.hide_in_ui = False
    player.state.playback_state = PlaybackState.PLAYING
    player.state.available = True
    player.state.powered = True
    player.state.volume_level = 30
    player.state.volume_muted = False
    player.state.type = PlayerType.PLAYER
    player.state.group_members = []
    player.state.current_media = None
    player.private = False
    return player
