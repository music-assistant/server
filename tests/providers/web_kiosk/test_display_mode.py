"""Tests for the read-only display mode and its embed policy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.web_kiosk.constants import parse_frame_ancestors
from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider


def _queue_item(
    queue_item_id: str = "qi1",
    *,
    guest: bool = False,
    boosted: bool = False,
) -> Mock:
    item = Mock(queue_item_id=queue_item_id)
    item.name = "Artist - Song"
    item.duration = 200
    item.extra_attributes = {"party_guest": guest, "party_boosted": boosted}
    item.media_item = Mock()
    item.media_item.name = "Song"
    item.media_item.artist_str = "Artist"
    item.media_item.media_type = MediaType.TRACK
    item.image = None
    return item


def _generic_player() -> Mock:
    player = Mock(player_id="speaker_1")
    player.name = "Kitchen"
    player.config = Mock()
    player.config.name = "Kitchen"
    state = Mock()
    state.playback_state = PlaybackState.PLAYING
    state.current_media = PlayerMedia(
        uri="library://track/1",
        title="Song",
        artist="Artist",
        duration=200,
        image_url="http://ma.local/image.jpg",
        media_type=MediaType.TRACK,
        source_id="speaker_1",
        queue_item_id="qi1",
    )
    state.corrected_elapsed_time = 10.0
    state.volume_level = 40
    state.volume_muted = False
    state.active_group = None
    state.synced_to = None
    state.group_members = []
    player.state = state
    return player


def _queue(queue_id: str = "speaker_1") -> Mock:
    return Mock(
        queue_id=queue_id,
        current_index=0,
        current_item=Mock(queue_item_id="qi1"),
        corrected_elapsed_time=10.0,
        playback_speed=1.0,
    )


def _wire_player(mass_mock: Mock, player: Mock) -> None:
    mass_mock.players.get_player = Mock(
        side_effect=lambda player_id, **_kwargs: player if player_id == player.player_id else None
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=_queue())
    mass_mock.player_queues.items = Mock(return_value=[_queue_item()])
    mass_mock.player_queues.get_item = Mock(return_value=_queue_item())
    mass_mock.metadata.get_image_url = Mock(return_value="http://ma.local/cover.jpg")
    mass_mock.metadata.get_track_lyrics = AsyncMock(
        return_value=(None, "[00:01.00]First line\n[00:05.00]Second line")
    )


async def _receive_type(ws: Any, message_type: str) -> dict[str, Any]:
    """Read messages until the requested type arrives."""
    async with asyncio.timeout(2):
        while True:
            message = cast("dict[str, Any]", await ws.receive_json())
            if message.get("type") == message_type:
                return message


@pytest.fixture
async def display_client(
    provider: WebKioskProvider,
) -> AsyncGenerator[TestClient[Any, Any]]:
    """Serve the display HTTP app with a provider whose server is reachable."""
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    client = TestClient(TestServer(server.app))
    await client.start_server()
    yield client
    await client.close()
    provider.http_server = None


async def test_view_requires_display_mode(
    display_client: TestClient[Any, Any],
) -> None:
    """The display page is not served unless the provider setting is on."""
    response = await display_client.get("/view")

    assert response.status == 404
    assert "disabled" in (await response.text()).lower()


async def test_view_and_display_socket_serve_a_player_without_registering(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
    mass_mock: Mock,
) -> None:
    """A display page shows an existing player without creating a kiosk player."""
    provider.display_mode_enabled = True
    player = _generic_player()
    _wire_player(mass_mock, player)

    page = await display_client.get("/view")
    assert page.status == 200
    assert "display.js" in await page.text()

    ws = await display_client.ws_connect("/ws?display=1&player=speaker_1")
    messages = [await ws.receive_json() for _ in range(4)]

    assert messages[0]["type"] == "welcome"
    assert messages[0]["display"] is True
    assert messages[0]["player_id"] == "speaker_1"
    state = next(message for message in messages if message["type"] == "display_state")
    assert state["media"]["title"] == "Song"
    assert state["elapsed"] == 10.0
    assert state["duration"] == 200
    queue = next(message for message in messages if message["type"] == "queue")
    assert queue["items"][0]["is_current"] is True
    lyrics = next(message for message in messages if message["type"] == "lyrics")
    assert lyrics["lines"][0]["text"] == "First line"

    mass_mock.players.register.assert_not_called()
    await ws.close()
    assert not provider.http_server._display_clients  # type: ignore[union-attr]
    mass_mock.players.unregister.assert_not_called()


async def test_display_rejects_unknown_player(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
    mass_mock: Mock,
) -> None:
    """A display socket for a player MA does not know gets 404."""
    provider.display_mode_enabled = True
    mass_mock.players.get_player = Mock(return_value=None)

    with pytest.raises(WSServerHandshakeError) as err:
        await display_client.ws_connect("/ws?display=1&player=missing")

    assert err.value.status == 404


async def test_display_is_rejected_when_disabled(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
) -> None:
    """The display socket is closed with 403 while the setting is off."""
    provider.display_mode_enabled = False

    with pytest.raises(WSServerHandshakeError) as err:
        await display_client.ws_connect("/ws?display=1&player=speaker_1")

    assert err.value.status == 403


async def test_party_player_is_resolved_for_a_display(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
    mass_mock: Mock,
) -> None:
    """`party=1` resolves the Party plugin's player without a player id."""
    provider.display_mode_enabled = True
    player = _generic_player()
    _wire_player(mass_mock, player)
    mass_mock.command_handlers = {"party/player": Mock(target=AsyncMock(return_value="speaker_1"))}

    ws = await display_client.ws_connect("/ws?display=1&party=1")
    welcome = await ws.receive_json()

    assert welcome["type"] == "welcome"
    assert welcome["player_id"] == "speaker_1"
    await ws.close()


async def test_queue_push_updates_on_queue_event(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
    mass_mock: Mock,
) -> None:
    """A queue event reaches the display with compact items and party badges."""
    provider.display_mode_enabled = True
    player = _generic_player()
    _wire_player(mass_mock, player)
    ws = await display_client.ws_connect("/ws?display=1&player=speaker_1")
    for _ in range(4):
        await ws.receive_json()

    second = _queue_item("qi2", guest=True, boosted=True)
    mass_mock.player_queues.items = Mock(return_value=[_queue_item(), second])
    provider.http_server._on_queue_updated(Mock(object_id="speaker_1"))  # type: ignore[union-attr]

    message = await ws.receive_json(timeout=2)
    assert message["type"] == "queue"
    assert message["items"][1]["queue_item_id"] == "qi2"
    assert message["items"][1]["party_guest"] is True
    assert message["items"][1]["party_boosted"] is True
    await ws.close()


async def test_player_page_receives_the_queue_over_the_socket(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
    mass_mock: Mock,
    player: Any,
) -> None:
    """A player page with queue=1 gets a push snapshot instead of polling the API."""
    provider.display_mode_enabled = True
    mass_mock.players.get_player = Mock(
        side_effect=lambda player_id, **_kwargs: player if player_id == "wk_test" else None
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=_queue("wk_test"))
    mass_mock.player_queues.items = Mock(return_value=[_queue_item()])

    ws = await display_client.ws_connect("/ws?device_id=test&queue=1")
    await ws.receive_json()  # welcome
    initial = await _receive_type(ws, "queue")
    assert initial["items"][0]["title"] == "Song"

    mass_mock.player_queues.items = Mock(return_value=[_queue_item("qi2")])
    provider.http_server._on_queue_updated(Mock(object_id="wk_test"))  # type: ignore[union-attr]
    pushed = await _receive_type(ws, "queue")
    assert pushed["items"][0]["queue_item_id"] == "qi2"
    await ws.close()


async def test_frame_ancestors_only_allow_the_display_page(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
) -> None:
    """Only display pages may be framed, and only for exact configured origins."""
    provider.display_mode_enabled = True
    provider.frame_ancestors = ("https://homeassistant.local:8123",)

    display = await display_client.get("/view")
    assert display.headers["Content-Security-Policy"] == (
        "frame-ancestors https://homeassistant.local:8123"
    )
    assert "X-Frame-Options" not in display.headers

    alias = await display_client.get("/kiosk?display=1")
    assert alias.headers["Content-Security-Policy"] == (
        "frame-ancestors https://homeassistant.local:8123"
    )

    player = await display_client.get("/kiosk")
    assert player.headers["X-Frame-Options"] == "DENY"
    assert player.headers["Content-Security-Policy"] == "frame-ancestors 'none'"

    builder = await display_client.get("/")
    assert builder.headers["X-Frame-Options"] == "DENY"


async def test_display_assets_and_manifest_are_served(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
) -> None:
    """The display page, its script, the manifest and the icon are part of the app."""
    provider.display_mode_enabled = True

    for path in ("/web/view.html", "/web/display.js", "/web/manifest.webmanifest", "/web/icon.svg"):
        response = await display_client.get(path)
        assert response.status == 200, path

    manifest = await (await display_client.get("/web/manifest.webmanifest")).json()
    assert manifest["display"] == "fullscreen"
    assert manifest["start_url"] == "/kiosk"


async def test_frame_ancestors_default_denies_everything(
    display_client: TestClient[Any, Any],
    provider: WebKioskProvider,
) -> None:
    """Without an allowlist the display page keeps the hard deny."""
    provider.display_mode_enabled = True
    provider.frame_ancestors = ()

    response = await display_client.get("/view")

    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ()),
        ("https://ha.local:8123", ("https://ha.local:8123",)),
        (
            "https://ha.local:8123, http://192.168.1.10:8123/",
            ("https://ha.local:8123", "http://192.168.1.10:8123"),
        ),
        ("https://ha.local:8123/path", ()),
        ("*", ()),
        ("https://*.example.com", ()),
        ("ftp://ha.local", ()),
        ("https://user:pass@ha.local", ()),
        ("https://ha.local/?x=1", ()),
    ],
)
def test_parse_frame_ancestors(value: str, expected: tuple[str, ...]) -> None:
    """Only exact HTTP(S) origins are accepted from the provider setting."""
    assert parse_frame_ancestors(value) == expected
