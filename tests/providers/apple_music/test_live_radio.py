"""Unit tests for the Apple Music live radio stations."""

import asyncio
import base64
import logging
import shutil
import struct
import subprocess
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError, ClientResponseError
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.streamdetails import StreamDetails
from mutagen import id3

from music_assistant.helpers import ffmpeg
from music_assistant.helpers.ffmpeg import _raise_feeder_error
from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.providers.apple_music.live_radio import (
    RENEW_RETRY_SECONDS,
    AppleMusicLiveRadioManager,
    WidevinePlaylistParser,
    parse_stream_metadata,
    segment_uri,
    select_substream,
)
from music_assistant.providers.apple_music.parsers import parse_station
from music_assistant.providers.apple_music.provider import AppleMusicProvider
from music_assistant.providers.apple_music.streaming import LicenseRefusedError

MODULE = "music_assistant.providers.apple_music.live_radio"
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:CODECS="mp4a.40.2",BANDWIDTH=281600,AUDIO="a"
https://primary.example/index_1.m3u8
#EXT-X-STREAM-INF:CODECS="mp4a.40.5",BANDWIDTH=70400,AUDIO="b"
https://primary.example/index_2.m3u8
#EXT-X-STREAM-INF:CODECS="mp4a.40.2",BANDWIDTH=281600,AUDIO="a"
https://backup.example/index_1.m3u8
"""
MEDIA_URL = "https://cdn.example/index_1.m3u8"
ASSET = {
    "url": "https://origin.example/master.m3u8?sig=secret",
    "keyServerUrl": "https://key.example",
}


def _wv(key: str) -> str:
    """Return the Widevine key uri of a key name."""
    return "data:text/plain;base64," + base64.b64encode(f"pssh-{key}".encode()).decode()


def _media(first: int, keys: list[str]) -> str:
    """Return a live media playlist with one segment per given key, from sequence first."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:16", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
    current = None
    for sequence, key in enumerate(keys, start=first):
        if key != current:
            current = key
            lines += [
                f'#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",'
                f'URI="skd://linear/{key}"',
                f'#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-'
                f'27dcd51d21ed",URI="{_wv(key)}"',
                f'#EXT-X-MAP:URI="init_{key}"',
            ]
        lines += ["#EXTINF:16.0,", f"s{sequence}"]
    return "\n".join(lines) + "\n"


def _http_error(status: int) -> ClientResponseError:
    """Return an aiohttp error with a signed url, as a failing request raises it."""
    request_info = MagicMock(real_url="https://cdn.example/seg.mp4?token=secret")
    return ClientResponseError(request_info, (), status=status)


def _tagged_segment(version: int, title: str = "Song") -> bytes:
    """Return a media segment with an ID3 tag in an emsg box in front of its audio."""
    frames: Any = id3
    tag = frames.ID3()
    tag.add(frames.TIT2(encoding=3, text=title))
    tag.add(frames.TPE1(encoding=3, text="Artist"))
    tag.add(frames.TALB(encoding=3, text="Album"))
    tag.add(frames.WXXX(encoding=3, desc="artworkURL_640x", url="https://img.example/a.jpg"))
    buffer = BytesIO()
    tag.save(buffer, v2_version=4, padding=lambda _info: 0)
    scheme = b"https://aomedia.org/emsg/ID3\0" + b"0\0"
    if version == 1:
        body = b"\x01\0\0\0" + b"\0" * 20 + scheme + buffer.getvalue()
    else:
        body = b"\0\0\0\0" + scheme + b"\0" * 16 + buffer.getvalue()
    emsg = struct.pack(">I4s", 8 + len(body), b"emsg") + body
    return emsg + struct.pack(">I4s", 8, b"moof")


def _radio() -> AppleMusicLiveRadioManager:
    provider = MagicMock(instance_id="am", domain="apple_music", max_concurrent_streams=1)
    provider.name = "Apple Music"
    provider.logger = logging.getLogger("test.apple_music")
    provider.storefront = "de"
    provider.music_user_token = "token"
    return AppleMusicLiveRadioManager(provider)


class _Live:
    """A live radio manager on scripted playlists, with a decoder that passes its input on."""

    def __init__(
        self, playlists: list[str], monkeypatch: pytest.MonkeyPatch, real_decoder: bool = False
    ) -> None:
        self.radio = _radio()
        self.playlists = playlists
        self.decoders: list[dict[str, Any]] = []
        self.fetched: list[str] = []
        # how often a request of the named file fails before it works
        self.failures: dict[str, int] = {}
        # each license request as (key name, segments fetched before it)
        self.licenses: list[tuple[str, int]] = []
        self.segments: dict[str, bytes] = {}
        self._real_sleep = asyncio.sleep
        self.radio._fetch = self._fetch  # type: ignore[method-assign]
        self.radio._get_license = self._get_license  # type: ignore[method-assign, assignment]
        if not real_decoder:
            monkeypatch.setattr(f"{MODULE}.get_ffmpeg_stream", self._decoder)
        monkeypatch.setattr(f"{MODULE}.asyncio.sleep", self._sleep)
        self.details = StreamDetails(
            provider="am",
            item_id="ra.1",
            media_type=MediaType.RADIO,
            audio_format=MagicMock(),
            data={
                "media_url": MEDIA_URL,
                "key_server_url": "https://key.example",
                "keys": {_wv("A"): ("a" * 32, 780.0)},
                "key_uri": _wv("A"),
                "renew_at": None,
            },
        )

    async def _fetch(self, url: str) -> bytes:
        if url == MEDIA_URL:
            text = self.playlists.pop(0) if len(self.playlists) > 1 else self.playlists[0]
            return text.encode()
        name = url.rsplit("/", 1)[-1]
        self.fetched.append(name)
        if self.failures.get(name):
            self.failures[name] -= 1
            raise UnplayableMediaError("Live radio request failed: HTTP 503")
        return self.segments.get(name, name.encode())

    async def _get_license(self, _data: dict[str, Any], key_uri: str) -> tuple[str, float]:
        # the key of key name A is aaaa..., a valid hex key for ffmpeg
        name = base64.b64decode(key_uri.rsplit(",", 1)[-1]).decode()[-1]
        self.licenses.append((name, len(self.fetched)))
        return name.lower() * 32, 780.0

    async def _decoder(
        self, audio_input: AsyncGenerator[bytes], **kwargs: Any
    ) -> AsyncGenerator[bytes]:
        # like the real one: the input is read by a task of its own, and an error of the
        # input comes out wrapped after the output ends
        run: dict[str, Any] = {"args": kwargs["extra_input_args"], "input": []}
        self.decoders.append(run)
        output: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)
        failure: list[Exception] = []

        async def feed() -> None:
            try:
                async for chunk in audio_input:
                    run["input"].append(chunk)
                    await output.put(chunk)
            except Exception as err:
                failure.append(err)
            await output.put(None)

        feeder = asyncio.create_task(feed())
        try:
            while (chunk := await output.get()) is not None:
                yield chunk
        finally:
            feeder.cancel()
            with suppress(asyncio.CancelledError):
                await feeder
            await audio_input.aclose()
        if failure:
            _raise_feeder_error(failure[0])

    async def _sleep(self, delay: float) -> None:
        # the playlist loop waits for real, but at most a moment
        await self._real_sleep(min(delay, 0.01))

    async def read(self, count: int) -> list[bytes]:
        chunks: list[bytes] = []
        stream = self.radio.get_audio_stream(self.details)
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) == count:
                break
        await stream.aclose()
        return chunks


def test_the_best_substream_is_played() -> None:
    """The master playlist leads to the first media playlist of the best quality."""
    assert select_substream(MASTER, ASSET["url"]) == "https://primary.example/index_1.m3u8"


def test_the_media_playlist_keeps_the_widevine_key() -> None:
    """The core parser numbers the segments, the Widevine key wins over the FairPlay one."""
    playlist = WidevinePlaylistParser(_media(100, ["A", "A", "B"])).parse()
    assert playlist.media_sequence == 100
    assert [segment_uri(segment.key_line) for segment in playlist.segments] == [
        _wv("A"),
        _wv("A"),
        _wv("B"),
    ]
    assert segment_uri(playlist.segments[2].map_line) == "init_B"


async def test_a_failing_init_segment_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The init segment gets the same retries as a media segment."""
    live = _Live([_media(10, ["A"] * 3)], monkeypatch)
    live.failures = {"init_A": 1}

    assert await live.read(2) == [b"init_A", b"s10"]


@pytest.mark.parametrize(("is_live", "dynamic"), [(True, False), (False, True)])
def test_only_track_based_stations_are_dynamic(is_live: bool, dynamic: bool) -> None:
    """A live station is one continuous stream, other stations hand out tracks."""
    provider = MagicMock(instance_id="am", domain="apple_music")
    station = {"id": "ra.1", "attributes": {"name": "Club", "isLive": is_live}}
    assert parse_station(provider, station).is_dynamic is dynamic


async def test_a_key_change_starts_a_new_decoder_and_the_stream_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each key gets its own ffmpeg, the outer stream continues without an error."""
    live = _Live([_media(10, ["A"] * 5), _media(12, ["A"] * 3 + ["B"] * 3)], monkeypatch)

    chunks = await live.read(8)

    assert chunks == [b"init_A", b"s12", b"s13", b"s14", b"init_B", b"s15", b"s16", b"s17"]
    assert [run["args"] for run in live.decoders] == [
        ["-decryption_key", "a" * 32],
        ["-decryption_key", "b" * 32],
    ]
    assert live.decoders[0]["input"] == [b"init_A", b"s12", b"s13", b"s14"]
    assert live.radio.state["key_changes"] == 1


@pytest.mark.parametrize("version", [0, 1])
def test_stream_metadata_comes_from_the_id3_tag_in_front_of_the_audio(version: int) -> None:
    """The emsg box of either version holds the track that is on air."""
    track = parse_stream_metadata(_tagged_segment(version))
    assert track is not None
    assert (track.title, track.artist, track.album) == ("Song", "Artist", "Album")
    assert track.image_url == "https://img.example/a.jpg"
    assert parse_stream_metadata(struct.pack(">I4s", 8, b"moof")) is None


async def test_a_title_shows_when_its_audio_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    """A title waits for the audio sent before it, and the same title is noted once."""
    live = _Live([_media(10, ["A"] * 3), _media(13, ["A"])], monkeypatch)
    live.segments = {"s11": _tagged_segment(1), "s12": _tagged_segment(1), "s13": b"s13"}

    await live.read(4)

    callback = live.radio._update_stream_metadata
    await callback(live.details, 15)
    assert live.details.stream_metadata is None
    await callback(live.details, 16)
    shown: Any = live.details.stream_metadata
    assert shown.title == "Song"
    assert live.details.data["pending_metadata"] == []


def _fragmented_aac(tmp_path: Path) -> tuple[bytes, list[bytes]]:
    """Return the init segment and three segments of a 6 second clear AAC fMP4 from ffmpeg."""
    path = tmp_path / "clear.mp4"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-f",
            "mp4",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration",
            "2000000",
            str(path),
        ],
        check=True,
    )
    data = path.read_bytes()
    boxes: list[tuple[bytes, bytes]] = []
    position = 0
    while position < len(data):
        size, name = struct.unpack(">I4s", data[position : position + 8])
        boxes.append((name, data[position : position + size]))
        position += size
    init = b"".join(box for name, box in boxes if name in (b"ftyp", b"moov"))
    fragments = [
        moof + mdat
        for (first, moof), (second, mdat) in pairwise(boxes)
        if (first, second) == (b"moof", b"mdat")
    ]
    # ffmpeg may close with a short fragment, a segment can hold more than one
    return init, [*fragments[:2], b"".join(fragments[2:])]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_a_key_change_through_real_ffmpeg_keeps_the_pcm_continuous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first ffmpeg flushes all of its audio at the key change, and none is left over."""
    init, fragments = _fragmented_aac(tmp_path)
    assert len(fragments) == 3
    live = _Live(
        [_media(10, ["A"] * 3), _media(10, ["A"] * 3 + ["B"] * 3)], monkeypatch, real_decoder=True
    )
    live.segments = {"init_A": init, "init_B": init}
    for index, fragment in enumerate(fragments * 2):
        live.segments[f"s{10 + index}"] = fragment
    processes: list[ffmpeg.FFMpeg] = []

    class RecordedFFMpeg(ffmpeg.FFMpeg):
        async def start(self) -> None:
            processes.append(self)
            await super().start()

    monkeypatch.setattr(ffmpeg, "FFMpeg", RecordedFFMpeg)
    per_decoder: list[int] = []
    real_decoder = ffmpeg.get_ffmpeg_stream

    async def counting_decoder(
        audio_input: AsyncGenerator[bytes], **kwargs: Any
    ) -> AsyncGenerator[bytes]:
        per_decoder.append(0)
        index = len(per_decoder) - 1
        async with aclosing(real_decoder(audio_input, **kwargs)) as pcm:
            async for chunk in pcm:
                per_decoder[index] += len(chunk)
                yield chunk

    monkeypatch.setattr(f"{MODULE}.get_ffmpeg_stream", counting_decoder)
    second_per_second = 48000 * 2 * 4

    stream = live.radio.get_audio_stream(live.details)
    async for _chunk in stream:
        if len(per_decoder) == 2 and per_decoder[1] >= second_per_second:
            break
    await stream.aclose()

    # all 6 seconds of the first key came out, the AAC priming adds a few milliseconds
    assert per_decoder[0] / second_per_second == pytest.approx(6.0, abs=0.1)
    assert len(processes) == 2
    assert all(process.returncode is not None for process in processes)


async def test_an_error_of_the_input_keeps_its_own_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decoder wraps an input error, the stream passes the original on to the core."""
    live = _Live(
        [_media(10, ["A"] * 3), "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:13\n#EXTINF:16.0,\ns13\n"],
        monkeypatch,
    )

    with pytest.raises(UnplayableMediaError, match="no Widevine stream"):
        await live.read(10)

    assert "no Widevine stream" in live.radio.state["last_error"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_http_error(403), ProviderStreamLimitError),
        (LicenseRefusedError("live radio", 1, 5), ProviderStreamLimitError),
        (_http_error(401), LoginFailed),
        (_http_error(503), UnplayableMediaError),
        (ValueError("not json"), UnplayableMediaError),
        (MediaNotFoundError("Unable to get decryption key for live radio"), UnplayableMediaError),
    ],
    ids=["403", "refused", "401", "503", "not-json", "no-content-key"],
)
async def test_only_a_refusal_is_the_stream_limit_of_the_account(
    error: Exception, expected: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    """Apple's refusal is not retried by the radio restart, any other problem may be."""
    radio = _radio()
    streaming: Any = radio.provider.streaming_manager
    streaming.get_content_key = AsyncMock(side_effect=error)
    data = {"key_server_url": "https://key.example", "renew_at": None}
    caplog.set_level(logging.DEBUG)

    with pytest.raises(expected) as raised:
        await radio._get_license(data, _wv("A"))

    assert raised.type is expected
    assert streaming.get_content_key.await_args.kwargs["check_status"] is True
    assert "secret" not in caplog.text
    assert "secret" not in str(raised.value)


async def test_a_license_is_asked_like_apple_s_own_client() -> None:
    """The live license request carries the fields and headers of Apple's web player."""
    radio = _radio()
    streaming: Any = radio.provider.streaming_manager
    streaming.get_content_key = AsyncMock(return_value=("aabb", {"renew-after": 780}))
    data: dict[str, Any] = {"key_server_url": "https://key.example", "renew_at": None}

    await radio._get_license(data, _wv("A"))

    request = streaming.get_content_key.await_args
    assert request.args[2] == {"uri": _wv("A"), "event": "beats1"}
    assert request.kwargs["headers"]["x-apple-renewal"] == "true"


@pytest.mark.parametrize(
    ("answer", "next_renewal"),
    [
        (ProviderStreamLimitError(MagicMock(max_concurrent_streams=1), None), None),
        (UnplayableMediaError("HTTP 503"), RENEW_RETRY_SECONDS),
        (("aabb", 780.0), 780),
    ],
    ids=["refused", "failed", "renewed"],
)
async def test_a_renewal_stops_only_when_apple_refuses_it(
    answer: Any, next_renewal: int | None
) -> None:
    """A refused renewal ends the stream, a failed one is tried again later."""
    radio = _radio()
    radio._get_license = AsyncMock(side_effect=[answer])  # type: ignore[method-assign]
    data: dict[str, Any] = {"key_uri": _wv("A"), "renew_at": 0}

    if next_renewal is None:
        with pytest.raises(ProviderStreamLimitError):
            await radio._renew_license(data)
    else:
        await radio._renew_license(data)
        assert data["renew_at"] == pytest.approx(time.monotonic() + next_renewal, abs=5)


async def test_the_next_key_is_licensed_once_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next key's license comes before the first segment, its renewal from the change."""
    live = _Live([_media(10, ["A", "A", "B"])], monkeypatch)
    live.details.data["renew_at"] = renew_of_a = time.monotonic() + 999
    renew_at_while_a_plays: list[float] = []
    live.segments = {"s11": b"s11"}
    fetch = live.radio._fetch

    async def watching_fetch(url: str) -> bytes:
        if url.endswith("/s11"):
            renew_at_while_a_plays.append(live.details.data["renew_at"])
        return await fetch(url)

    live.radio._fetch = watching_fetch  # type: ignore[method-assign]

    chunks = await live.read(5)

    assert chunks == [b"init_A", b"s10", b"s11", b"init_B", b"s12"]
    assert live.licenses == [("B", 0)]
    assert renew_at_while_a_plays == [renew_of_a]
    assert live.details.data["renew_at"] == pytest.approx(time.monotonic() + 780, abs=5)


async def test_the_playlist_loop_renews_the_license_when_it_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal is asked for in the playlist loop once its time has come."""
    live = _Live([_media(10, ["A"] * 3)], monkeypatch)
    live.details.data["renew_at"] = 0
    renew = AsyncMock()
    live.radio._renew_license = renew  # type: ignore[method-assign]

    await live.read(2)

    renew.assert_awaited_once_with(live.details.data)


async def test_stream_details_hold_the_stream_and_the_license_of_this_account() -> None:
    """The asset leads to the best substream, whose newest segment decides the key to load."""
    radio = _radio()
    api: Any = radio.provider.api_client
    api.get_data = AsyncMock(
        side_effect=[
            {"data": [{"attributes": {"playParams": {"id": "ra.1", "isLive": True}}}]},
            {"results": {"assets": [ASSET]}},
        ]
    )
    pages = {ASSET["url"]: MASTER, "https://primary.example/index_1.m3u8": _media(1, ["A", "B"])}
    radio._fetch = AsyncMock(side_effect=lambda url: pages[url].encode())  # type: ignore[method-assign]
    radio._get_license = AsyncMock(return_value=("bb", 780.0))  # type: ignore[method-assign]

    details = await radio.get_stream_details("ra.1")

    assert details.stream_type == StreamType.CUSTOM
    assert details.can_seek is False
    assert details.decoded_audio_format is not None
    assert details.decoded_audio_format.content_type == ContentType.PCM_F32LE
    assert details.data["media_url"] == "https://primary.example/index_1.m3u8"
    assert details.data["keys"] == {_wv("B"): ("bb", 780.0)}
    assert details.data["renew_at"] == pytest.approx(time.monotonic() + 780, abs=5)
    assert api.get_data.await_args_list[1].kwargs == {
        "keyFormat": "web",
        "id": "ra.1",
        "isLive": "true",
    }


async def test_a_failing_request_keeps_the_signed_url_out_of_errors_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The url of the stream holds a signature, so only the HTTP status is reported."""
    radio = _radio()
    radio.provider.api_client.get_data = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"data": [{"attributes": {"playParams": {"id": "ra.1"}}}]},
            {"results": {"assets": [ASSET]}},
        ]
    )
    radio.mass.http_session.get.side_effect = _http_error(403)  # type: ignore[attr-defined]
    caplog.set_level(logging.DEBUG)

    with pytest.raises(UnplayableMediaError) as raised:
        await radio.get_stream_details("ra.1")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(f"{MODULE}.asyncio.sleep", AsyncMock())
        assert await radio._fetch_segment("https://cdn.example/seg.mp4?token=secret") is None

    assert "HTTP 403" in str(raised.value)
    assert raised.value.__cause__ is None
    assert "secret" not in str(raised.value)
    assert "secret" not in caplog.text
    assert "HTTP 403" in caplog.text


async def test_live_stations_come_from_the_featured_filter() -> None:
    """The lookup behind the cache asks for the live stations of the storefront."""
    radio = _radio()
    api: Any = radio.provider.api_client
    api.get_data = AsyncMock(
        return_value={"data": [{"id": "ra.1", "attributes": {"name": "Club", "isLive": True}}]}
    )

    get_stations: Any = AppleMusicLiveRadioManager.get_stations
    stations = await get_stations.__wrapped__(radio)

    assert [(station.item_id, station.name) for station in stations] == [("ra.1", "Club")]
    api.get_data.assert_awaited_once_with(
        "catalog/de/stations", **{"filter[featured]": "apple-music-live-radio"}
    )


async def test_browse_lists_the_live_stations_first() -> None:
    """The stations folder holds the live stations and then the recommended ones."""
    provider: Any = MagicMock()
    provider.live_radio_manager.get_stations = AsyncMock(return_value=["live"])
    provider.recommendation_manager.browse_stations = AsyncMock(return_value=["recommended"])

    items: Any = await AppleMusicProvider.browse(provider, "am://stations")

    assert items == ["live", "recommended"]


@pytest.mark.parametrize(
    "error", [ClientError(), TimeoutError(), ResourceTemporarilyUnavailable("Apple is away")]
)
async def test_browse_keeps_the_recommended_stations_without_live_stations(
    error: Exception,
) -> None:
    """A failing lookup of the live stations does not take the rest of the folder down."""
    provider: Any = MagicMock()
    provider.live_radio_manager.get_stations = AsyncMock(side_effect=error)
    provider.recommendation_manager.browse_stations = AsyncMock(return_value=["recommended"])

    items: Any = await AppleMusicProvider.browse(provider, "am://stations")

    assert items == ["recommended"]
    provider.logger.warning.assert_called_once()


async def test_a_radio_item_streams_through_the_live_radio_manager() -> None:
    """Only a radio item takes the live path, songs keep their own."""
    provider: Any = MagicMock()
    live, song = MagicMock(), MagicMock()
    provider.live_radio_manager.get_stream_details = AsyncMock(return_value=live)
    provider.streaming_manager.get_stream_details = AsyncMock(return_value=song)

    assert await AppleMusicProvider.get_stream_details(provider, "ra.1", MediaType.RADIO) is live
    assert await AppleMusicProvider.get_stream_details(provider, "1", MediaType.TRACK) is song
