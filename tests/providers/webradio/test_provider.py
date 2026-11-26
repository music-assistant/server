"""Tests for the Web Broadcast provider's broadcast state and listener responses."""

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import ContentType, MediaType, PlaybackState
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.providers.webradio.provider import (
    WebRadioPlayer,
    WebRadioProvider,
    _Station,
    _Subscriber,
)

if TYPE_CHECKING:
    from music_assistant.models.player import Player

CHUNK_SIZE = 64
CHUNKS = [bytes([65 + index]) * CHUNK_SIZE for index in range(3)]
METADATA = b"\x01StreamTitle='T';"
PLAYER_ID = "pl000000000001"
PCM_FORMAT = AudioFormat(content_type=ContentType.PCM_S16LE)


def _make_player(
    provider: WebRadioProvider | None = None, *, fixed_content_length: bool = False
) -> WebRadioPlayer:
    """Return a playing station, attached to a player, that records its state updates."""
    player = WebRadioPlayer(
        provider=provider or MagicMock(),
        protocol_id="webradio_kitchen",
        station_slug="kitchen",
        display_name="Kitchen",
    )
    attached_to = MagicMock(player_id=PLAYER_ID)
    attached_to.name = "Kitchen"
    player._player = attached_to
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 30
    player._attr_elapsed_time_last_updated = 1.0
    player._attr_current_media = MagicMock()
    player.__dict__["update_state"] = MagicMock()
    # every other setting keeps the default the provider asks for
    player.__dict__["get_config_value"] = lambda key, default=None, **_: (
        fixed_content_length if key == CONF_REQUIRES_FIXED_CONTENT_LENGTH else default
    )
    return player


def _make_provider(*, fixed_content_length: bool = False) -> WebRadioProvider:
    """Return a provider with one station and a mocked Music Assistant instance."""
    provider = WebRadioProvider.__new__(WebRadioProvider)
    provider.mass = MagicMock()
    provider.config = MagicMock()
    provider.logger = MagicMock()
    station = _Station(
        slug="kitchen",
        protocol=_make_player(provider, fixed_content_length=fixed_content_length),
        url_path="/webradio/kitchen.mp3",
        unregister_route=lambda: None,
    )
    station.output_format = AudioFormat(content_type=ContentType.MP3, bit_rate=320)
    station.icy_preference = "basic"
    station.icy_chunk_size = CHUNK_SIZE
    station.current_metadata = METADATA
    provider._stations = {station.slug: station}
    return provider


def _item(item_id: str, media_type: MediaType = MediaType.TRACK) -> MagicMock:
    """Return a queue item without stream details."""
    return MagicMock(
        queue_item_id=item_id, queue_id=PLAYER_ID, media_type=media_type, streamdetails=None
    )


def _queue_with_items(provider: WebRadioProvider, current_item_id: str | None) -> MagicMock:
    """Make the station's queue resolve items by id, with the given current item."""
    queue = MagicMock()
    queue.current_item = _item(current_item_id) if current_item_id is not None else None
    player_queues = cast("MagicMock", provider.mass.player_queues)
    player_queues.get.return_value = queue
    player_queues.get_item.side_effect = lambda _queue_id, item_id: _item(item_id)
    return queue


def _store_station_names(provider: WebRadioProvider, names: list[str]) -> None:
    """Make the provider read its station names from, and write them to, the given list."""
    cast("MagicMock", provider.config).get_value.side_effect = lambda _key: names
    provider.__dict__["_update_config_value"] = lambda _key, value: names.__setitem__(
        slice(None), value
    )


async def test_a_station_with_the_url_of_another_is_renamed() -> None:
    """A station whose name gives the URL of an earlier one is stored under a numbered name."""
    provider = _make_provider()
    provider._stations = {}
    names = ["Rock", "rock!", "Rock 2", "Rock", "Jazz"]
    _store_station_names(provider, names)
    mass = cast("MagicMock", provider.mass)
    mass.config.get_protocol_ids_of_provider.return_value = []
    mass.players.get_protocol.return_value = None
    mass.players.register = AsyncMock()

    await provider.loaded_in_mass()
    await provider.discover_players()

    assert names == ["Rock", "rock! (2)", "Rock 2 (2)", "Rock (3)", "Jazz"]
    assert [(slug, station.protocol.name) for slug, station in provider._stations.items()] == [
        ("rock", "Rock"),
        ("rock-2", "rock! (2)"),
        ("rock-2-2", "Rock 2 (2)"),
        ("rock-3", "Rock (3)"),
        ("jazz", "Jazz"),
    ]
    assert [call.args[0].protocol_id for call in mass.players.register.await_args_list] == [
        "webradio_rock",
        "webradio_rock-2",
        "webradio_rock-2-2",
        "webradio_rock-3",
        "webradio_jazz",
    ]


async def test_discovering_again_skips_attached_stations_and_keeps_the_others() -> None:
    """A second discovery leaves an attached station alone and offers the same object again."""
    provider = _make_provider()
    provider._stations = {}
    _store_station_names(provider, ["Rock", "Jazz"])
    mass = cast("MagicMock", provider.mass)
    mass.players.get_protocol.return_value = None
    mass.players.register = AsyncMock()

    await provider.discover_players()
    first = [call.args[0] for call in mass.players.register.await_args_list]
    assert [protocol.protocol_id for protocol in first] == ["webradio_rock", "webradio_jazz"]

    # the rock station is attached by now, the jazz station is still waiting (e.g. disabled)
    mass.players.get_protocol.side_effect = lambda protocol_id: (
        first[0] if protocol_id == "webradio_rock" else None
    )
    mass.players.register.reset_mock()
    await provider.discover_players()

    assert [call.args[0] for call in mass.players.register.await_args_list] == [first[1]]


async def test_only_a_station_that_left_the_config_is_removed_on_load() -> None:
    """A stored station that is no longer configured is removed, a configured one is kept."""
    provider = _make_provider()
    _store_station_names(provider, ["Kitchen"])
    mass = cast("MagicMock", provider.mass)
    mass.config.get_protocol_ids_of_provider.return_value = ["webradio_kitchen", "webradio_gone"]
    mass.players.remove_protocol = AsyncMock()

    await provider.loaded_in_mass()

    mass.players.remove_protocol.assert_awaited_once_with("webradio_gone")


async def test_a_removed_station_leaves_the_config() -> None:
    """Removing a station's player takes its name out of the stations config."""
    provider = _make_provider()
    names = ["Kitchen", "Jazz"]
    _store_station_names(provider, names)

    await provider.on_protocol_removed("webradio_kitchen")

    assert names == ["Jazz"]
    assert "kitchen" not in provider._stations


async def test_a_station_has_a_url_only_while_it_is_attached() -> None:
    """Attaching a station serves its URL, detaching ends the broadcast and the URL."""
    provider = _make_provider()
    station = provider._stations["kitchen"]
    station.url_path = ""
    station.unregister_route = None
    unregister_route = MagicMock()
    streams = cast("MagicMock", provider.mass).streams
    streams.register_dynamic_route.return_value = unregister_route

    # a config update of a station that is not attached leaves it without a URL
    await station.protocol.on_config_updated()
    streams.register_dynamic_route.assert_not_called()

    await station.protocol.on_attach(cast("Player", station.protocol.player))

    streams.register_dynamic_route.assert_called_once_with(
        "/webradio/kitchen.mp3", provider._handle_stream_request, "*"
    )
    assert station.url_path == "/webradio/kitchen.mp3"

    async def producer() -> None:
        await station.stop_event.wait()

    station.producer_task = asyncio.create_task(producer())
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="first"))

    await station.protocol.on_detach()

    unregister_route.assert_called_once()
    assert station.producer_task.done()
    assert station.unregister_route is None
    assert station.url_path == ""
    assert station.pending_media is None
    assert station.protocol.playback_state == PlaybackState.IDLE


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
    player.mass.player_queues.get.return_value = MagicMock(items=3)
    while_playing = player.source_list[0].can_play_pause
    player.mark_idle()
    while_idle = player.source_list[0].can_play_pause
    player.mass.player_queues.get.return_value = MagicMock(items=0)
    with_empty_queue = player.source_list[0].can_play_pause

    assert (while_playing, while_idle, with_empty_queue) == (False, True, False)


def test_the_queue_source_carries_the_id_of_the_player() -> None:
    """The station's queue source replaces the one of its player, so it has the player's id."""
    player = _make_player()
    player.mass = MagicMock()

    assert player.source_list[0].id == PLAYER_ID
    player.mass.player_queues.get.assert_called_once_with(PLAYER_ID)


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
    provider: WebRadioProvider,
    exit_reason: str,
    *,
    exhausted: bool = False,
    start_item: MagicMock | None = None,
    resume_from_queue: bool = False,
    elapsed_time: float = 0,
) -> _Station:
    """
    Run the station's producer over one flow session that ends for the given reason.

    :param provider: The provider serving the station.
    :param exit_reason: How the session ends.
    :param exhausted: Whether the session played the queue to its end.
    :param start_item: The queue item handed to play_media, a track by default.
    :param resume_from_queue: Whether the broadcast carries on from the queue's current item,
        as after the last listener left, instead of starting on a play_media.
    :param elapsed_time: How far the queue is into its current item, in seconds.
    """
    station = provider._stations["kitchen"]
    queue = _queue_with_items(provider, "third")
    queue.elapsed_time = elapsed_time
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="first"))
    station.resume_from_queue = resume_from_queue
    mass = cast("MagicMock", provider.mass)
    if start_item is not None:
        queue.current_item = start_item
        mass.player_queues.get_item.side_effect = lambda _queue_id, _item_id: start_item
    mass.streams.audio.select_flow_pcm_format = AsyncMock()
    mass.streams.audio.get_output_format = AsyncMock(return_value=station.output_format)
    mass.player_queues.queue_data_or_none.return_value = MagicMock(session_id="session")
    mass.player_queues.flow_queue_exhausted.return_value = exhausted
    provider.__dict__["_run_one_flow_session"] = AsyncMock(return_value=exit_reason)
    player = MagicMock(playback_state=PlaybackState.PLAYING)
    player.mark_idle.side_effect = lambda: setattr(player, "playback_state", PlaybackState.IDLE)
    player.get_config_value.return_value = "basic"
    station.protocol = player
    await asyncio.wait_for(provider._run_producer(station), timeout=5)
    return station


async def test_producer_resumes_from_the_queue_after_the_last_listener_left() -> None:
    """When the last listener leaves, the next broadcast starts from the queue's position."""
    station = await _run_producer(_make_provider(), "no_subscribers")

    assert station.resume_from_queue
    assert station.pending_media is not None
    cast("MagicMock", station.protocol).mark_idle.assert_not_called()


async def test_producer_goes_idle_when_the_queue_ran_out() -> None:
    """At the end of the queue the station goes idle and has nothing left to start from."""
    station = await _run_producer(_make_provider(), "ended", exhausted=True)

    cast("MagicMock", station.protocol).mark_idle.assert_called_once()
    assert station.pending_media is None


async def test_producer_stops_when_no_item_follows_a_flow_that_ended_early() -> None:
    """A flow that core ended early leaves the station idle, and stopped if nothing follows."""
    with patch("music_assistant.providers.webradio.provider._NEXT_SESSION_TIMEOUT", 0.01):
        station = await _run_producer(_make_provider(), "ended", exhausted=False)

    cast("MagicMock", station.protocol).mark_idle.assert_called_once()
    assert station.pending_media is None


async def test_live_start_item_is_not_joined_at_a_position() -> None:
    """A broadcast that carries on from a radio item does not seek, one from a track does."""
    joined: list[MediaType] = []
    for media_type in (MediaType.RADIO, MediaType.AUDIO_SOURCE, MediaType.TRACK):
        provider = _make_provider()
        with patch(
            "music_assistant.providers.webradio.provider._join_at_live_position"
        ) as join_at_live_position:
            await _run_producer(
                provider,
                "stopped",
                start_item=_item("first", media_type),
                resume_from_queue=True,
            )
        if join_at_live_position.called:
            joined.append(media_type)

    assert joined == [MediaType.TRACK]


@pytest.mark.parametrize(
    ("resume_from_queue", "elapsed_time", "expected"),
    [
        # play_media just set the resume point of a podcast, no listener has heard anything yet
        pytest.param(False, 0, 300, id="after_play_media"),
        # the clock of the queue kept running while nobody listened
        pytest.param(True, 120, 120, id="carrying_on_from_the_queue"),
    ],
)
async def test_start_position_is_only_replaced_when_carrying_on_from_the_queue(
    resume_from_queue: bool, elapsed_time: int, expected: int
) -> None:
    """A resume point set by play_media survives the first listener tuning in."""
    start_item = _item("first")
    start_item.streamdetails = MagicMock(allow_seek=True, duration=1000, seek_position=300)

    await _run_producer(
        _make_provider(),
        "stopped",
        start_item=start_item,
        resume_from_queue=resume_from_queue,
        elapsed_time=elapsed_time,
    )

    assert start_item.streamdetails.seek_position == expected


async def test_producer_carries_on_with_the_live_item_that_ended_the_flow() -> None:
    """A radio item core plays after ending the flow early starts the next session, unjoined."""
    provider = _make_provider()
    radio = _item("radio", MediaType.RADIO)
    sessions: list[MagicMock] = []

    async def run_session(**kwargs: Any) -> str:
        sessions.append(kwargs["start_item"])
        return "ended" if len(sessions) == 1 else "stopped"

    def play_radio() -> None:
        # core plays the item after the flow once the station reports idle
        station.protocol._attr_playback_state = PlaybackState.PLAYING
        provider.set_pending_media("kitchen", MagicMock(queue_item_id="radio"))

    station = provider._stations["kitchen"]
    cast("MagicMock", station.protocol.update_state).side_effect = lambda: (
        asyncio.get_running_loop().call_soon(play_radio)
    )
    _queue_with_items(provider, "first")
    mass = cast("MagicMock", provider.mass)
    mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: (
        radio if item_id == "radio" else _item(item_id)
    )
    provider.set_pending_media("kitchen", MagicMock(queue_item_id="first"))
    mass.streams.audio.select_flow_pcm_format = AsyncMock()
    mass.streams.audio.get_output_format = AsyncMock(return_value=station.output_format)
    mass.player_queues.queue_data_or_none.return_value = MagicMock(session_id="session")
    mass.player_queues.flow_queue_exhausted.return_value = False
    provider.__dict__["_run_one_flow_session"] = run_session

    with patch(
        "music_assistant.providers.webradio.provider._join_at_live_position"
    ) as join_at_live_position:
        await asyncio.wait_for(provider._run_producer(station), timeout=5)

    assert [item.queue_item_id for item in sessions] == ["first", "radio"]
    join_at_live_position.assert_not_called()


async def _audio(chunks: list[bytes], error: Exception | None = None) -> AsyncGenerator[bytes]:
    """Yield the given chunks of audio, then fail with the given error if there is one."""
    for chunk in chunks:
        yield chunk
    if error is not None:
        raise error


async def _broadcast_session(
    provider: WebRadioProvider,
    start_item: MagicMock,
    *,
    pending_item_id: str | None = None,
    live_audio: AsyncGenerator[bytes] | None = None,
) -> list[bytes]:
    """
    Run one session from the given item, returning what a listener of the station received.

    The flow stream carries the first two chunks and the stream of a live item all three.

    :param provider: The provider serving the station.
    :param start_item: The queue item the session starts from.
    :param pending_item_id: The item play_media named, the start item by default.
    :param live_audio: The audio core hands out for a live item, all three chunks by default.
    """
    station = provider._stations["kitchen"]
    subscriber = _Subscriber(queue=asyncio.Queue(), want_icy=False)
    station.subscribers.add(subscriber)
    provider.set_pending_media(
        "kitchen", MagicMock(queue_item_id=pending_item_id or start_item.queue_item_id)
    )

    async def encode(audio_input: AsyncGenerator[bytes], **_: Any) -> AsyncGenerator[bytes]:
        async for chunk in audio_input:
            yield chunk

    mass = cast("MagicMock", provider.mass)
    mass.player_queues.player_media_from_queue_item = AsyncMock()
    mass.streams.audio.get_queue_flow_stream.side_effect = lambda **_: _audio(CHUNKS[:2])
    mass.streams.get_stream.side_effect = lambda *_: live_audio or _audio(CHUNKS)
    with patch("music_assistant.providers.webradio.provider.get_ffmpeg_stream", encode):
        exit_reason = await provider._run_one_flow_session(
            station=station,
            queue=MagicMock(),
            start_item=start_item,
            flow_pcm_format=PCM_FORMAT,
            output_format=MagicMock(),
            icy_preference="disabled",
            chunk_size=CHUNK_SIZE,
        )
    assert exit_reason == "ended"
    received: list[bytes] = []
    while not subscriber.queue.empty():
        chunk = subscriber.queue.get_nowait()
        assert chunk is not None
        received.append(chunk)
    return received


@pytest.mark.parametrize("media_type", [MediaType.RADIO, MediaType.AUDIO_SOURCE])
async def test_live_item_is_broadcast_from_its_own_stream(media_type: MediaType) -> None:
    """A live item reaches the listener through core's stream of its media, not the flow."""
    provider = _make_provider()

    received = await _broadcast_session(provider, _item("radio", media_type))

    assert received == CHUNKS
    mass = cast("MagicMock", provider.mass)
    mass.streams.audio.get_queue_flow_stream.assert_not_called()
    mass.streams.get_stream.assert_called_once_with(
        provider._stations["kitchen"].pending_media, PCM_FORMAT, "webradio_kitchen"
    )
    mass.player_queues.player_media_from_queue_item.assert_not_awaited()


async def test_live_item_resumed_from_the_queue_gets_its_own_media() -> None:
    """A broadcast that resumes on a live item play_media did not name streams that item."""
    provider = _make_provider()
    radio = _item("radio", MediaType.RADIO)

    received = await _broadcast_session(provider, radio, pending_item_id="first")

    assert received == CHUNKS
    mass = cast("MagicMock", provider.mass)
    mass.player_queues.player_media_from_queue_item.assert_awaited_once_with(radio)
    mass.streams.get_stream.assert_called_once_with(
        mass.player_queues.player_media_from_queue_item.return_value,
        PCM_FORMAT,
        "webradio_kitchen",
    )


async def test_track_is_broadcast_from_the_flow_stream() -> None:
    """A track reaches the listener through the queue's flow stream."""
    provider = _make_provider()
    track = _item("first")

    received = await _broadcast_session(provider, track)

    assert received == CHUNKS[:2]
    mass = cast("MagicMock", provider.mass)
    mass.streams.get_stream.assert_not_called()
    assert mass.streams.audio.get_queue_flow_stream.call_args.kwargs["start_queue_item"] is track


async def test_live_item_whose_source_fails_ends_the_session() -> None:
    """A live source that fails ends the session with the audio it gave, without raising."""
    provider = _make_provider()

    received = await _broadcast_session(
        provider,
        _item("radio", MediaType.RADIO),
        live_audio=_audio(CHUNKS[:1], AudioError("source gone")),
    )

    assert received == CHUNKS[:1]


async def test_play_media_presets_flow_mode_only_for_media_that_ends() -> None:
    """A track is played as a flow from the start, a live item never is."""
    flow_modes: dict[MediaType, bool] = {}
    for media_type in (MediaType.TRACK, MediaType.RADIO, MediaType.AUDIO_SOURCE):
        player = _make_player()
        player.mass = MagicMock()
        queue = MagicMock(flow_mode=False)
        player.mass.player_queues.get.return_value = queue

        await player.play_media(MagicMock(media_type=media_type))

        flow_modes[media_type] = queue.flow_mode

    assert flow_modes == {
        MediaType.TRACK: True,
        MediaType.RADIO: False,
        MediaType.AUDIO_SOURCE: False,
    }


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


async def test_listener_is_told_the_name_of_the_player() -> None:
    """The station announces itself under the name its player has in Music Assistant."""
    provider = _make_provider()
    cast("MagicMock", provider._stations["kitchen"].protocol.player).name = "Garden radio"

    headers, _ = await _listen(provider, "GET /webradio/kitchen.mp3 HTTP/1.0")

    assert headers["icy-name"] == "Garden radio"


async def test_http_11_listener_gets_chunked_by_default() -> None:
    """Without a forced length an HTTP/1.1 listener gets chunked transfer encoding."""
    headers, body = await _listen(_make_provider(), "GET /webradio/kitchen.mp3 HTTP/1.1")

    assert headers["transfer-encoding"] == "chunked"
    assert _dechunk(body) == _expected_body()


async def test_forced_content_length_ends_the_response_when_it_runs_out() -> None:
    """Once the forced length is used up the response ends instead of stalling."""
    provider = _make_provider(fixed_content_length=True)

    with patch(
        "music_assistant.providers.webradio.provider.calculate_content_length",
        return_value=CHUNK_SIZE + 1,
    ):
        _, body = await _listen(
            provider, "GET /webradio/kitchen.mp3 HTTP/1.1", broadcast_ends=False
        )

    assert body == (CHUNKS[0] + METADATA)[: CHUNK_SIZE + 1]


async def test_forced_content_length_keeps_the_stream_plain_for_http_11() -> None:
    """A fixed content length gives an HTTP/1.1 radio a stream without chunks."""
    provider = _make_provider(fixed_content_length=True)

    headers, body = await _listen(provider, "GET /webradio/kitchen.mp3 HTTP/1.1")

    assert "transfer-encoding" not in headers
    assert int(headers["content-length"]) > len(body)
    assert body == _expected_body()
