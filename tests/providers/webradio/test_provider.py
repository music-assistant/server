"""Tests for the Web Broadcast provider's broadcast state and listener responses."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.webradio.provider import (
    WebRadioPlayer,
    WebRadioProvider,
    _Station,
    _Subscriber,
)

CHUNK_SIZE = 64
CHUNKS = [bytes([65 + index]) * CHUNK_SIZE for index in range(3)]
METADATA = b"\x01StreamTitle='T';"


def _make_player() -> WebRadioPlayer:
    """Return a station player that records its state updates."""
    player = WebRadioPlayer.__new__(WebRadioPlayer)
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 30
    player._attr_elapsed_time_last_updated = 1.0
    player._attr_current_media = MagicMock()
    player._attr_name = "Kitchen"
    player.__dict__["update_state"] = MagicMock()
    return player


def _make_provider(http_profile: str = "no_content_length") -> WebRadioProvider:
    """Return a provider with one station and a mocked Music Assistant instance."""
    provider = WebRadioProvider.__new__(WebRadioProvider)
    provider.mass = MagicMock()
    provider.mass.config.get_raw_player_config_value.return_value = http_profile
    provider.logger = MagicMock()
    station = _Station(
        slug="kitchen",
        player_id="webradio_kitchen",
        player=_make_player(),
        url_path="/webradio/kitchen.mp3",
        unregister_route=lambda: None,
    )
    station.output_format = AudioFormat(content_type=ContentType.MP3, bit_rate=320)
    station.icy_preference = "basic"
    station.icy_chunk_size = CHUNK_SIZE
    station.current_metadata = METADATA
    provider._stations = {station.slug: station}
    return provider


def _item(item_id: str) -> MagicMock:
    """Return a queue item without stream details."""
    return MagicMock(queue_item_id=item_id, streamdetails=None)


def _queue_with_items(provider: WebRadioProvider, current_item_id: str | None) -> MagicMock:
    """Make the station's queue resolve items by id, with the given current item."""
    queue = MagicMock()
    queue.current_item = _item(current_item_id) if current_item_id is not None else None
    player_queues = cast("MagicMock", provider.mass.player_queues)
    player_queues.get.return_value = queue
    player_queues.get_item.side_effect = lambda _queue_id, item_id: _item(item_id)
    return queue


async def test_a_station_with_the_url_of_another_is_renamed() -> None:
    """A station whose name gives the URL of an earlier one is stored under a numbered name."""
    provider = _make_provider()
    provider._stations = {}
    names = ["Rock", "rock!", "Rock 2", "Rock", "Jazz"]
    provider.config = MagicMock()
    provider.config.get_value.side_effect = lambda _key: names
    provider.__dict__["_update_config_value"] = lambda _key, value: names.__setitem__(
        slice(None), value
    )
    register_station = AsyncMock()
    provider.__dict__["_register_station"] = register_station

    await provider._sync_stations()

    assert names == ["Rock", "rock! (2)", "Rock 2 (2)", "Rock (3)", "Jazz"]
    assert [call.args for call in register_station.await_args_list] == [
        ("rock", "Rock"),
        ("rock-2", "rock! (2)"),
        ("rock-2-2", "Rock 2 (2)"),
        ("rock-3", "Rock (3)"),
        ("jazz", "Jazz"),
    ]


def test_broadcast_starts_from_the_item_handed_to_play_media() -> None:
    """After play_media the broadcast starts from that item, not the queue's current one."""
    provider = _make_provider()
    _queue_with_items(provider, "previous")
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="first"))

    _, start_item = provider._resolve_pending_media(provider._stations["kitchen"])

    assert start_item is not None
    assert start_item.queue_item_id == "first"


def test_broadcast_resumes_from_the_current_item_after_the_last_listener_left() -> None:
    """A listener that returns hears the item the queue moved on to."""
    provider = _make_provider()
    station = provider._stations["kitchen"]
    _queue_with_items(provider, "third")
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="first"))
    station.resume_from_queue = True

    _, start_item = provider._resolve_pending_media(station)
    assert start_item is not None
    assert start_item.queue_item_id == "third"

    # a new play request takes over again
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="fifth"))
    _, start_item = provider._resolve_pending_media(station)
    assert start_item is not None
    assert start_item.queue_item_id == "fifth"


def test_nothing_is_broadcast_without_pending_media() -> None:
    """A station that was stopped, or whose queue ran out, has nothing to start from."""
    provider = _make_provider()
    station = provider._stations["kitchen"]
    _queue_with_items(provider, "third")
    station.resume_from_queue = True

    assert provider._resolve_pending_media(station) == (None, None)


def test_play_is_offered_only_while_the_station_is_not_playing() -> None:
    """A stopped station with a queue can be started, a playing one can only be stopped."""
    player = _make_player()
    player.mass = MagicMock()
    player._player_id = "webradio_kitchen"
    player.mass.player_queues.get.return_value = MagicMock(items=3)
    while_playing = player.source_list[0].can_play_pause
    player.mark_idle()
    while_idle = player.source_list[0].can_play_pause
    player.mass.player_queues.get.return_value = MagicMock(items=0)
    with_empty_queue = player.source_list[0].can_play_pause

    assert (while_playing, while_idle, with_empty_queue) == (False, True, False)


def test_mark_idle_clears_the_playback_state() -> None:
    """An idle station reports no media and no elapsed time."""
    player = _make_player()

    player.mark_idle()

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_current_media is None
    assert player._attr_elapsed_time is None
    assert player._attr_elapsed_time_last_updated is None
    cast("MagicMock", player.update_state).assert_called_once()


def test_restart_elapsed_time_only_while_playing() -> None:
    """The elapsed time restarts for a playing station and stays untouched otherwise."""
    player = _make_player()
    player.restart_elapsed_time()
    assert player._attr_elapsed_time == 0
    assert player._attr_elapsed_time_last_updated != 1.0

    player.mark_idle()
    player.restart_elapsed_time()
    assert player._attr_elapsed_time is None


async def _run_producer(
    provider: WebRadioProvider, exit_reason: str, *, exhausted: bool = False
) -> _Station:
    """Run the station's producer over one flow session that ends for the given reason."""
    station = provider._stations["kitchen"]
    _queue_with_items(provider, "third")
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="first"))
    mass = cast("MagicMock", provider.mass)
    mass.streams.audio.select_flow_pcm_format = AsyncMock()
    mass.streams.audio.get_output_format = AsyncMock(return_value=station.output_format)
    mass.player_queues.queue_data_or_none.return_value = MagicMock(session_id="session")
    mass.player_queues.flow_queue_exhausted.return_value = exhausted
    provider.__dict__["_run_one_flow_session"] = AsyncMock(return_value=exit_reason)
    player = MagicMock(playback_state=PlaybackState.PLAYING)
    player.mark_idle.side_effect = lambda: setattr(player, "playback_state", PlaybackState.IDLE)
    station.player = player
    await asyncio.wait_for(provider._run_producer(station), timeout=5)
    return station


async def test_producer_resumes_from_the_queue_after_the_last_listener_left() -> None:
    """When the last listener leaves, the next broadcast starts from the queue's position."""
    station = await _run_producer(_make_provider(), "no_subscribers")

    assert station.resume_from_queue
    assert station.pending_media is not None
    cast("MagicMock", station.player).mark_idle.assert_not_called()


async def test_producer_goes_idle_when_the_queue_ran_out() -> None:
    """At the end of the queue the station goes idle and has nothing left to start from."""
    station = await _run_producer(_make_provider(), "ended", exhausted=True)

    cast("MagicMock", station.player).mark_idle.assert_called_once()
    assert station.pending_media is None


async def test_producer_stops_when_no_item_follows_a_flow_that_ended_early() -> None:
    """A flow that core ended early leaves the station idle, and stopped if nothing follows."""
    with patch("music_assistant.providers.webradio.provider._NEXT_SESSION_TIMEOUT", 0.01):
        station = await _run_producer(_make_provider(), "ended", exhausted=False)

    cast("MagicMock", station.player).mark_idle.assert_called_once()
    assert station.pending_media is None


async def test_listener_is_refused_right_away_when_nothing_is_playing() -> None:
    """A station with nothing to broadcast refuses a listener without starting a producer."""
    provider = _make_provider()
    request = MagicMock(path="/webradio/kitchen.mp3", method="GET")

    with pytest.raises(web.HTTPServiceUnavailable):
        await provider._handle_stream_request(request)

    cast("MagicMock", provider.mass).create_task.assert_not_called()
    assert not provider._stations["kitchen"].subscribers


async def _listen(
    provider: WebRadioProvider, request_line: str, *, broadcast_ends: bool = True
) -> tuple[dict[str, str], bytes]:
    """
    Tune in like a radio that reads the raw response, returning its headers and body.

    :param provider: The provider serving the station.
    :param request_line: The first line of the radio's HTTP request.
    :param broadcast_ends: Whether the broadcast ends after its three chunks.
    """
    station = provider._stations["kitchen"]
    station.player = MagicMock(display_name="Kitchen")
    subscriber = _Subscriber(queue=asyncio.Queue(), want_icy=True)
    for chunk in (*CHUNKS, None) if broadcast_ends else CHUNKS:
        subscriber.queue.put_nowait(chunk)

    async def handler(request: web.Request) -> web.StreamResponse:
        return await provider._serve_subscriber(request, station, subscriber)

    app = web.Application()
    app.router.add_get(station.url_path, handler)
    server = TestServer(app)
    await server.start_server()
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(f"{request_line}\r\nHost: radio\r\nIcy-MetaData: 1\r\n\r\n".encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(-1), timeout=5)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await server.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    headers: dict[str, str] = {}
    for line in head.decode().split("\r\n")[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers, body


def _dechunk(body: bytes) -> bytes:
    """Return the payload of a chunked transfer encoded body."""
    payload = b""
    while body:
        size, _, body = body.partition(b"\r\n")
        length = int(size, 16)
        payload += body[:length]
        body = body[length + 2 :]
    return payload


def _expected_body() -> bytes:
    """Return the audio with the title after the first chunk and no change after the others."""
    return CHUNKS[0] + METADATA + CHUNKS[1] + b"\x00" + CHUNKS[2] + b"\x00"


async def test_http_10_listener_gets_a_plain_stream() -> None:
    """An HTTP/1.0 radio gets the bare audio and titles, without chunk framing."""
    headers, body = await _listen(_make_provider(), "GET /webradio/kitchen.mp3 HTTP/1.0")

    assert "transfer-encoding" not in headers
    assert headers["icy-metaint"] == str(CHUNK_SIZE)
    assert body == _expected_body()


async def test_http_11_listener_gets_chunked_by_default() -> None:
    """Without a forced length an HTTP/1.1 listener gets chunked transfer encoding."""
    headers, body = await _listen(_make_provider(), "GET /webradio/kitchen.mp3 HTTP/1.1")

    assert headers["transfer-encoding"] == "chunked"
    assert _dechunk(body) == _expected_body()


async def test_chunked_profile_is_not_used_for_an_http_10_listener() -> None:
    """The chunked profile still gives an HTTP/1.0 radio a plain stream."""
    provider = _make_provider("chunked")

    headers, body = await _listen(provider, "GET /webradio/kitchen.mp3 HTTP/1.0")

    assert "transfer-encoding" not in headers
    assert body == _expected_body()


async def test_forced_content_length_ends_the_response_when_it_runs_out() -> None:
    """Once the forced length is used up the response ends instead of stalling."""
    provider = _make_provider("forced_content_length")

    with patch(
        "music_assistant.providers.webradio.provider.calculate_content_length",
        return_value=CHUNK_SIZE + 1,
    ):
        _, body = await _listen(
            provider, "GET /webradio/kitchen.mp3 HTTP/1.1", broadcast_ends=False
        )

    assert body == (CHUNKS[0] + METADATA)[: CHUNK_SIZE + 1]


async def test_forced_content_length_keeps_the_stream_plain_for_http_11() -> None:
    """The forced content length profile gives an HTTP/1.1 radio a stream without chunks."""
    provider = _make_provider("forced_content_length")

    headers, body = await _listen(provider, "GET /webradio/kitchen.mp3 HTTP/1.1")

    assert "transfer-encoding" not in headers
    assert int(headers["content-length"]) > len(body)
    assert body == _expected_body()
