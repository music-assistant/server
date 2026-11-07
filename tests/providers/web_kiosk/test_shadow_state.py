"""
Invalidation tests for the kiosk's copies of Music Assistant state.

Each test names the source of truth from ``CLAUDE.local.md`` and proves that the
copy follows it: a de-duplication cache lets a changed value through, and the
player's derived playback facts are replaced on every new playback instead of
surviving from the previous one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider

if TYPE_CHECKING:
    import pytest


def _page_server(
    provider: WebKioskProvider, player_id: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[WebKioskHTTPServer, Mock]:
    """Return a server with one connected page and capture what it posts."""
    server = WebKioskHTTPServer(provider, 0)
    posted = Mock()
    monkeypatch.setattr(server, "_post", posted)
    server._ws_clients.setdefault(player_id, set()).add(Mock(closed=False))
    return server, posted


def _playing_player(media: PlayerMedia) -> Mock:
    """Return the state Player exposes while it plays the given media."""
    player = Mock(
        player_id="wk_test",
        playback_generation="generation-1",
        sendspin_client_id="client",
        active_output_protocol="client",
        serves_flow=False,
        current_media=media,
        corrected_elapsed_time=1.0,
    )
    player.state = Mock(playback_state=PlaybackState.PLAYING, current_media=media)
    return player


def test_media_cache_follows_the_players_current_media(
    provider: WebKioskProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed track reaches the page, and an unchanged one is de-duplicated."""
    server, posted = _page_server(provider, "wk_test", monkeypatch)
    monkeypatch.setattr(provider, "spawn_wave", Mock())
    monkeypatch.setattr(provider, "spawn_lyrics", Mock())
    player = _playing_player(PlayerMedia(uri="track", title="First", duration=30))

    server._publish_effective_media(player)
    server._publish_effective_media(player)
    assert posted.call_count == 1

    media = PlayerMedia(uri="track", title="Second", duration=30)
    player.state.current_media = media
    server._publish_effective_media(player)
    assert posted.call_count == 2


def test_group_cache_follows_the_group_name(
    provider: WebKioskProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed group is published again; an unchanged group is not."""
    server = WebKioskHTTPServer(provider, 0)
    posted = Mock()
    monkeypatch.setattr(server, "_post", posted)
    server._ws_clients.setdefault("wk_test", set()).add(Mock(closed=False))
    group = Mock()
    group.state = Mock(name="Room")
    group.state.name = "Room"
    monkeypatch.setattr(
        provider.mass.players,
        "get_player",
        Mock(side_effect=lambda player_id, **_: group if player_id == "room" else None),
    )
    kiosk = Mock(player_id="wk_test", active_output_protocol=None)
    kiosk.state = Mock(active_group="room", synced_to=None, group_members=[])

    server._publish_group(kiosk)
    server._publish_group(kiosk)
    assert posted.call_count == 1

    group.state.name = "Kitchen"
    server._publish_group(kiosk)
    assert posted.call_count == 2


def test_player_name_cache_follows_the_configured_name(
    provider: WebKioskProvider,
    player: WebKioskPlayer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename reaches the page; repeating the same name does not."""
    server, posted = _page_server(provider, player.player_id, monkeypatch)

    player.config.name = "Hall"
    server.broadcast_player_name(player)
    server.broadcast_player_name(player)
    assert posted.call_count == 1

    player.config.name = "Kitchen"
    server.broadcast_player_name(player)
    assert posted.call_count == 2


def test_flow_item_follows_the_queue(
    provider: WebKioskProvider,
    player: WebKioskPlayer,
    mass_mock: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page hears the new queue item, and does not hear the same item twice."""
    server, posted = _page_server(provider, "wk_test", monkeypatch)
    monkeypatch.setattr(provider, "spawn_wave", Mock())
    monkeypatch.setattr(provider, "spawn_lyrics", Mock())
    mass_mock.player_queues.get = Mock(
        return_value=Mock(playback_speed=1.0, corrected_elapsed_time=0.0)
    )
    player = _playing_player(
        PlayerMedia(uri="flow", title="One", duration=30, source_id="wk_test", queue_item_id="q1")
    )

    # The play message announced the item the stream opened with.
    server._flow_items["wk_test"] = ("generation-1", "q0")

    server._publish_flow_item(player)
    server._publish_flow_item(player)
    assert posted.call_count == 1

    media = PlayerMedia(
        uri="flow", title="Two", duration=30, source_id="wk_test", queue_item_id="q2"
    )
    player.state.current_media = media
    player.current_media = media
    server._publish_flow_item(player)
    assert posted.call_count == 2


async def test_stream_shape_and_speed_are_replaced_by_every_play_media(
    player: WebKioskPlayer,
    mass_mock: Mock,
) -> None:
    """``serves_flow`` and ``playback_speed`` describe the playback being started."""
    mass_mock.streams.base_url = "http://ma.local:8095"
    mass_mock.streams.resolve_stream_url = AsyncMock(
        return_value="http://ma.local:8095/flow/wk_test"
    )
    mass_mock.player_queues.get_item = Mock(
        return_value=Mock(extra_attributes={"playback_speed": 1.5})
    )

    await player.play_media(
        PlayerMedia(uri="flow", source_id="wk_test", queue_item_id="q1", duration=30)
    )

    serves_flow = player.serves_flow
    speed = player.playback_speed
    assert serves_flow is True
    assert speed == 1.5

    mass_mock.streams.resolve_stream_url = AsyncMock(return_value="http://ma.local:8095/stream/1")
    mass_mock.player_queues.get_item = Mock(return_value=None)
    await player.play_media(
        PlayerMedia(uri="native", source_id="wk_test", queue_item_id="q2", duration=30)
    )

    serves_flow = player.serves_flow
    speed = player.playback_speed
    assert serves_flow is False
    assert speed == 1.0
