"""Unit tests for the Apple Music live radio playlist, metadata and segment loop."""

import asyncio
import struct
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from aiohttp import ClientError, ClientResponseError
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.providers.apple_music.live_radio import (
    REFUSED_LICENSE_SECONDS,
    RENEW_RETRY_SECONDS,
    SEGMENT_RETRIES,
    AppleMusicLiveRadioManager,
    LivePlaylist,
    LiveSegment,
    LiveStreamRestartError,
    parse_master_playlist,
    parse_media_playlist,
    parse_stream_metadata,
)
from music_assistant.providers.apple_music.parsers import parse_station
from music_assistant.providers.apple_music.provider import AppleMusicProvider

MODULE = "music_assistant.providers.apple_music.live_radio"
BASE = "https://cdn.example/out/"
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:CODECS="mp4a.40.2",BANDWIDTH=281600,AUDIO="a"
https://primary.example/index_1.m3u8
#EXT-X-STREAM-INF:CODECS="mp4a.40.5",BANDWIDTH=70400,AUDIO="b"
https://primary.example/index_2.m3u8
#EXT-X-STREAM-INF:CODECS="mp4a.40.2",BANDWIDTH=281600,AUDIO="a"
https://backup.example/index_1.m3u8
"""
WV = "data:text/plain;contentId=1;base64,AAAA"
MEDIA = f"""#EXTM3U
#EXT-X-TARGETDURATION:16
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",URI="skd://linear/AABBCCDDAABBCCDDAABBCCDDAABBCCDD?x=1"
#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",URI="{WV}"
#EXT-X-MAP:URI="seg_init.mp4"
#EXT-X-PROGRAM-DATE-TIME:2026-11-04T12:00:00.000Z
#EXTINF:16.0,
seg_100.mp4
#EXTINF:15.5,
seg_101.mp4
"""


def _syncsafe(value: int) -> bytes:
    return bytes((value >> shift) & 0x7F for shift in (21, 14, 7, 0))


def _frame(name: str, body: bytes) -> bytes:
    return name.encode() + _syncsafe(len(body)) + b"\0\0" + body


def _segment_with_track() -> bytes:
    frames = (
        _frame("TIT2", b"\x03Song\0")
        + _frame("TPE1", b"\x03Artist\0")
        + _frame("TALB", b"\x03Album\0")
        + _frame("WXXX", b"\x03artworkURL_640x\0https://img.example/a.jpg\0")
    )
    tag = b"ID3\x04\0\0" + _syncsafe(len(frames)) + frames
    body = b"\x01\0\0\0" + b"\0" * 20 + b"https://aomedia.org/emsg/ID3\0" + b"0\0" + tag
    emsg = struct.pack(">I4s", 8 + len(body), b"emsg") + body
    return emsg + struct.pack(">I4s", 8, b"moof")


def test_master_playlist_keeps_best_quality_with_backup() -> None:
    """Only the best quality is kept, the primary before its mirror."""
    assert parse_master_playlist(MASTER, BASE) == [
        "https://primary.example/index_1.m3u8",
        "https://backup.example/index_1.m3u8",
    ]


def test_media_playlist_numbers_segments_and_reads_the_key() -> None:
    """Segments carry their sequence, duration, init segment and key."""
    playlist = parse_media_playlist(MEDIA, BASE)
    assert playlist.target_duration == 16
    assert [segment.sequence for segment in playlist.segments] == [100, 101]
    assert playlist.segments[1].duration == 15.5
    assert playlist.segments[0].url == f"{BASE}seg_100.mp4"
    assert playlist.segments[0].init_url == f"{BASE}seg_init.mp4"
    assert playlist.segments[0].key_id == "aabbccddaabbccddaabbccddaabbccdd"
    assert playlist.segments[0].widevine_uri == WV


def test_stream_metadata_comes_from_the_timed_metadata() -> None:
    """The ID3 tag in front of the audio names the track."""
    track = parse_stream_metadata(_segment_with_track())
    assert track is not None
    assert (track.title, track.artist, track.album) == ("Song", "Artist", "Album")
    assert track.image_url == "https://img.example/a.jpg"


def test_stream_metadata_is_none_without_it() -> None:
    """A segment that starts with its audio names no track."""
    assert parse_stream_metadata(struct.pack(">I4s", 8, b"moof")) is None


@pytest.mark.parametrize(("is_live", "dynamic"), [(True, False), (False, True)])
def test_only_track_based_stations_are_dynamic(is_live: bool, dynamic: bool) -> None:
    """A live station is one continuous stream, other stations hand out tracks."""
    provider = MagicMock(instance_id="am", domain="apple_music")
    station = {"id": "ra.1", "attributes": {"name": "Club", "isLive": is_live}}
    assert parse_station(provider, station).is_dynamic is dynamic


class _Harness:
    """A live radio manager on scripted playlists."""

    def __init__(
        self,
        playlists: list[LivePlaylist | Exception],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = MagicMock(instance_id="am", logger=MagicMock())
        self.radio = AppleMusicLiveRadioManager(provider)
        self.playlists = playlists

        async def fetch_playlist(_url: str) -> LivePlaylist:
            result = self.playlists.pop(0) if len(self.playlists) > 1 else self.playlists[0]
            if isinstance(result, Exception):
                raise result
            return result

        async def fetch_bytes(url: str) -> bytes:
            return url.encode()

        self.audio_configs: dict[str, bytes] = {}
        self.key_failures: dict[str, int] = {}
        self.key_error: Exception = TimeoutError()
        self.key_attempts: dict[str, int] = {}
        self.renew_at_of_key: dict[str, float] = {}

        async def load_key(segment: LiveSegment, data: dict[str, Any]) -> None:
            key_id = cast("str", segment.key_id)
            self.key_attempts[key_id] = self.key_attempts.get(key_id, 0) + 1
            if self.key_failures.get(key_id, 0) > 0:
                self.key_failures[key_id] -= 1
                raise self.key_error
            config = self.audio_configs.get(cast("str", segment.key_id), b"config")
            data["keys"][segment.key_id] = (
                cast("str", segment.key_id).encode(),
                MagicMock(audio_config=config),
            )
            if key_id in self.renew_at_of_key:
                data["renew_at"] = self.renew_at_of_key[key_id]

        def decrypt(content: bytes, key: bytes, _encryption: Any) -> bytes:
            return content + b":" + key

        monkeypatch.setattr(f"{MODULE}.decrypt_segment", decrypt)
        self.radio._fetch_playlist = fetch_playlist  # type: ignore[method-assign, assignment]
        self.radio._fetch_bytes = fetch_bytes  # type: ignore[method-assign]
        self.radio._load_key = load_key  # type: ignore[method-assign]
        self.details = StreamDetails(
            provider="am",
            item_id="ra.1",
            media_type=MediaType.RADIO,
            audio_format=MagicMock(),
            data={
                "media_url": "https://cdn.example/index_1.m3u8",
                "key_server_url": "https://key.example",
                "keys": {"k1": (b"k1", MagicMock(audio_config=b"config"))},
                "mirror": 0,
                "key_id": "k1",
                "init_url": "init",
                "audio_config": b"config",
                "widevine_uri": WV,
                "renew_at": None,
            },
        )

    def events(self) -> list[str]:
        return [event["event"] for event in self.radio.monitor.as_dict()["recent_events"]]  # type: ignore[union-attr, index]


def _playlist(first: int, count: int, key: str = "k1") -> LivePlaylist:
    return LivePlaylist(
        target_duration=0.001,
        segments=[
            LiveSegment(seq, f"s{seq}", 16.0, "init", key, WV, None)
            for seq in range(first, first + count)
        ],
    )


def _rotating_playlist() -> LivePlaylist:
    """Return a playlist with a segment on the current key and one on the next key."""
    return LivePlaylist(
        target_duration=0.001,
        segments=[
            LiveSegment(13, "s13", 16.0, "init", "k1", WV, None),
            LiveSegment(14, "s14", 16.0, "init", "k2", WV, None),
        ],
    )


def _http_error(status: int) -> ClientResponseError:
    """Return an aiohttp error with a signed url, as a failing request raises it."""
    request_info = MagicMock(real_url="https://cdn.example/seg.mp4?token=secret")
    return ClientResponseError(request_info, (), status=status)


async def _read(
    radio: AppleMusicLiveRadioManager, details: StreamDetails, count: int
) -> list[bytes]:
    chunks: list[bytes] = []
    stream = radio.get_audio_stream(details)
    async for chunk in stream:
        chunks.append(chunk)
        if len(chunks) == count:
            break
    await stream.aclose()
    return chunks


async def test_starts_at_the_live_edge_and_sends_each_segment_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream starts three segments back, then sends only what is new."""
    harness = _Harness([_playlist(10, 5), _playlist(11, 5), _playlist(12, 5)], monkeypatch)
    chunks = await _read(harness.radio, harness.details, 6)
    assert chunks == [b"init", b"s12:k1", b"s13:k1", b"s14:k1", b"s15:k1", b"s16:k1"]
    events = harness.events()
    assert events[0] == "session_start"
    assert events[-1] == "session_end"


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Return the list that collects the waits of the segment loop."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(f"{MODULE}.asyncio.sleep", sleep)
    return sleeps


async def test_the_stream_does_not_wait_while_new_segments_are_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait after sent audio can leave the paced consumer without input."""
    sleeps = _record_sleeps(monkeypatch)
    harness = _Harness([_playlist(10, 3), _playlist(13, 1), _playlist(14, 1)], monkeypatch)
    chunks = await _read(harness.radio, harness.details, 6)
    assert chunks[-1] == b"s14:k1"
    assert sleeps == []


async def test_the_stream_waits_when_the_playlist_has_nothing_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live playlist is read at the paced interval when no segment is new."""
    sleeps = _record_sleeps(monkeypatch)
    same = _playlist(10, 3)
    harness = _Harness([same, same, _playlist(13, 1)], monkeypatch)
    chunks = await _read(harness.radio, harness.details, 5)
    assert chunks[-1] == b"s13:k1"
    assert sleeps == [same.target_duration / 2]


async def test_a_gap_is_recorded_as_skipped_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Segments that left the playlist before they were sent show up in the monitor."""
    harness = _Harness([_playlist(10, 3), _playlist(20, 3)], monkeypatch)
    await _read(harness.radio, harness.details, 5)
    assert "segments_skipped" in harness.events()


async def test_a_key_change_continues_without_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new key is picked up in the running stream, which is what keeps Sonos playing."""
    harness = _Harness([_playlist(10, 3), _playlist(13, 2, key="k2")], monkeypatch)
    chunks = await _read(harness.radio, harness.details, 6)
    assert chunks == [b"init", b"s10:k1", b"s11:k1", b"s12:k1", b"s13:k2", b"s14:k2"]
    assert "key_change" in harness.events()
    assert harness.details.data["key_id"] == "k2"


async def test_a_new_audio_format_restarts_and_resumes_where_it_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only audio the first init segment no longer describes needs a new decoder."""
    harness = _Harness([_playlist(10, 3), _playlist(13, 1, key="k2")], monkeypatch)
    harness.audio_configs["k2"] = b"other"
    stream = harness.radio.get_audio_stream(harness.details)
    with pytest.raises(LiveStreamRestartError):
        async for _ in stream:
            pass
    assert harness.radio._resume_sequence("ra.1") == 13


async def test_a_license_failure_for_the_next_key_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Segments on the current key still play, then the next pass gets the new key."""
    harness = _Harness([_playlist(10, 3), _rotating_playlist()], monkeypatch)
    harness.key_failures["k2"] = 2
    chunks = await _read(harness.radio, harness.details, 6)
    assert chunks == [b"init", b"s10:k1", b"s11:k1", b"s12:k1", b"s13:k1", b"s14:k2"]
    events = harness.events()
    assert events.count("key_load_error") == 2
    assert events.index("key_load_error") < events.index("key_change")
    assert "playlist_error" not in events
    assert harness.radio.monitor.as_dict()["sessions"][0]["license_errors"] == 2  # type: ignore[index]


async def test_a_refused_key_ends_the_stream_when_it_is_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audio on the current key is sent first, then the refusal stops the stream."""
    harness = _Harness([_playlist(10, 3), _rotating_playlist()], monkeypatch)
    harness.key_failures["k2"] = 99
    harness.key_error = UnplayableMediaError("Apple refused the live radio license")
    chunks: list[bytes] = []

    async def collect() -> None:
        async for chunk in harness.radio.get_audio_stream(harness.details):
            chunks.append(chunk)

    with pytest.raises(UnplayableMediaError):
        await collect()
    assert chunks[-1] == b"s13:k1"
    assert harness.key_attempts["k2"] == 1
    session = harness.radio.monitor.as_dict()["sessions"][0]  # type: ignore[index]
    assert session["end_reason"] == "Apple refused the live radio license"  # type: ignore[index]


async def test_a_failed_stream_keeps_the_signed_url_out_of_everything_it_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The url of a failed request stays out of the diagnostics, the log and the raised error."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    error = _http_error(403)
    assert "secret" in str(error)
    harness.radio._fetch_bytes = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    with pytest.raises(UnplayableMediaError) as raised:
        async for _ in harness.radio.get_audio_stream(harness.details):
            pass

    assert "HTTP 403" in str(raised.value)
    assert "secret" not in str(raised.value)
    assert raised.value.__suppress_context__
    assert "secret" not in str(harness.radio.monitor.as_dict())
    logger = cast("MagicMock", harness.radio.provider.logger)
    assert "secret" not in str(logger.mock_calls)


async def test_a_failed_stream_lookup_keeps_the_signed_url_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing request for the stream details does not hand its url to the caller."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    harness.radio._get_stream_details = AsyncMock(  # type: ignore[method-assign]
        side_effect=_http_error(404)
    )
    with pytest.raises(UnplayableMediaError) as raised:
        await harness.radio.get_stream_details("ra.1")
    assert "secret" not in str(raised.value)
    assert raised.value.__suppress_context__


async def test_a_license_renewal_sets_the_next_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A renewal is counted, and the answer sets when the next one is due."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    get_license = AsyncMock(return_value=("00", 780.0))
    harness.radio._get_license = get_license  # type: ignore[method-assign]
    session = harness.radio.monitor.start_session("ra.1")
    assert await harness.radio._renew_license(harness.details.data, session, 100.0) == 880.0
    assert session.license_renewals == 1
    get_license.assert_awaited_once_with(ANY, ANY, renewal=True)


@pytest.mark.parametrize("error", [TimeoutError(), ClientError(), _http_error(503)])
async def test_a_license_renewal_retries_on_a_temporary_failure(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A network or server problem leaves playback alone and is tried again."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    harness.radio._get_license = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    session = harness.radio.monitor.start_session("ra.1")
    next_try = await harness.radio._renew_license(harness.details.data, session, 100.0)
    assert next_try == 100.0 + RENEW_RETRY_SECONDS
    assert session.license_errors == 1
    assert session.license_renewals == 0


async def test_a_new_key_brings_its_own_renewal_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The renewal follows the license of the newest key, not the one the stream started on."""
    harness = _Harness(
        [_playlist(10, 3), _playlist(13, 1, key="k2"), _playlist(14, 1, key="k2")], monkeypatch
    )
    harness.renew_at_of_key["k2"] = 0.0
    get_license = AsyncMock(return_value=("00", 780.0))
    harness.radio._get_license = get_license  # type: ignore[method-assign]

    await _read(harness.radio, harness.details, 6)

    get_license.assert_awaited_once_with(ANY, ANY, renewal=True)
    assert harness.details.data["renew_at"] > time.monotonic()


async def test_a_refused_license_renewal_ends_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Playback does not go on when Apple refuses this account the license."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    harness.details.data["renew_at"] = 0.0
    harness.radio._get_license = AsyncMock(  # type: ignore[method-assign]
        side_effect=UnplayableMediaError("Apple refused the live radio license (HTTP 403)")
    )
    stream = harness.radio.get_audio_stream(harness.details)
    with pytest.raises(UnplayableMediaError):
        async for _ in stream:
            pass
    assert harness.radio._get_license.await_count == 1


def _license_harness(monkeypatch: pytest.MonkeyPatch, answer: Any) -> tuple[_Harness, LiveSegment]:
    """Return a harness whose license request gives the given answer or error."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    monkeypatch.setattr(f"{MODULE}.PSSH", MagicMock())
    provider: Any = harness.radio.provider
    provider.music_user_token = "token"
    get_content_key = AsyncMock()
    if isinstance(answer, Exception):
        get_content_key.side_effect = answer
    else:
        get_content_key.return_value = ("aabb", answer)
    provider.streaming_manager.get_content_key = get_content_key
    return harness, LiveSegment(0, "", 0, None, "k1", WV, None)


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_a_license_http_refusal_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A client error from the key server is Apple's answer, so renewing does not catch it."""
    harness, segment = _license_harness(monkeypatch, _http_error(status))
    with pytest.raises(UnplayableMediaError, match=f"HTTP {status}"):
        await harness.radio._get_license("https://key.example", segment, renewal=True)
    assert "license_refused" in harness.events()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_license_server_problem_stays_temporary(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A rate limit or a server error stays a ClientError, which renewing tries again."""
    harness, segment = _license_harness(monkeypatch, _http_error(status))
    with pytest.raises(ClientResponseError):
        await harness.radio._get_license("https://key.example", segment, renewal=True)
    assert "license_refused" not in harness.events()


async def test_a_cached_station_list_does_not_call_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browsing the stations again is answered from the cache."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    provider: Any = harness.radio.provider
    provider.api_client.get_data = AsyncMock()
    harness.radio.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=([], True, True)
    )
    assert await harness.radio.get_stations() == []
    provider.api_client.get_data.assert_not_awaited()


async def test_end_reason_tells_a_closing_consumer_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the stream is recorded as the consumer's doing."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    await _read(harness.radio, harness.details, 2)
    session = harness.radio.monitor.as_dict()["sessions"][0]  # type: ignore[index]
    assert session["end_reason"] == "consumer_closed"  # type: ignore[index]


async def test_lasting_playlist_errors_restart_on_the_other_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playlist that stops answering is recorded and leads to a restart."""
    harness = _Harness([TimeoutError()], monkeypatch)
    module = "music_assistant.providers.apple_music.live_radio"
    monkeypatch.setattr(f"{module}.MIN_STALL_SECONDS", 0.01)
    monkeypatch.setattr(f"{module}.DEFAULT_TARGET_DURATION", 0.001)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "music_assistant.providers.apple_music.live_radio.asyncio.sleep",
        lambda _seconds: real_sleep(0.02),
    )
    stream = harness.radio.get_audio_stream(harness.details)
    with pytest.raises(LiveStreamRestartError):
        async for _ in stream:
            pass
    events = harness.events()
    assert "playlist_error" in events
    assert "playlist_stalled" in events
    assert harness.radio._mirror_choice == 1


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


class _StreamDetailsApi:
    """The responses of Apple's catalog, play assets and the CDN for one live station."""

    def __init__(self, harness: _Harness, assets: list[dict[str, Any]], master: str) -> None:
        provider: Any = harness.radio.provider
        provider.storefront = "de"
        provider.api_client.get_data = AsyncMock(
            side_effect=[
                {"data": [{"attributes": {"playParams": {"id": "ra.1", "isLive": True}}}]},
                {"results": {"assets": assets}},
            ]
        )
        self.get_data = provider.api_client.get_data
        response = MagicMock(text=AsyncMock(return_value=master))
        mass: Any = harness.radio.mass
        mass.http_session.get.return_value.__aenter__.return_value = response


ASSET = {
    "url": "https://origin.example/master.m3u8?sig=secret",
    "keyServerUrl": "https://key.example",
}


def _media_playlist(key: str = "k1") -> LivePlaylist:
    return _playlist(10, 3, key=key)


async def test_stream_details_hold_the_stream_and_the_license_of_this_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asset leads to the playlist, whose newest segment decides the key to load."""
    harness = _Harness([_media_playlist()], monkeypatch)
    api = _StreamDetailsApi(harness, [ASSET], MASTER)

    details = await harness.radio.get_stream_details("ra.1")

    assert details.item_id == "ra.1"
    assert details.media_type == MediaType.RADIO
    assert details.stream_type == StreamType.CUSTOM
    assert details.can_seek is False
    assert details.data["media_url"] == "https://primary.example/index_1.m3u8"
    assert details.data["mirror"] == 0
    assert details.data["key_id"] == "k1"
    assert details.data["audio_config"] == b"config"
    assert details.data["key_server_url"] == "https://key.example"
    assert details.stream_metadata_update_callback == harness.radio._update_stream_metadata
    assert api.get_data.await_args_list[0].args == ("catalog/de/stations/ra.1",)
    assert api.get_data.await_args_list[1].args == ("play/assets",)
    assert api.get_data.await_args_list[1].kwargs == {
        "keyFormat": "web",
        "id": "ra.1",
        "isLive": "true",
    }


async def test_stream_details_use_the_other_mirror_after_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled stream moves the choice on, which the next start follows."""
    harness = _Harness([_media_playlist()], monkeypatch)
    _StreamDetailsApi(harness, [ASSET], MASTER)
    harness.radio._mirror_choice = 1

    details = await harness.radio.get_stream_details("ra.1")

    assert details.data["media_url"] == "https://backup.example/index_1.m3u8"
    assert details.data["mirror"] == 1


@pytest.mark.parametrize(
    ("assets", "master", "playlist"),
    [
        pytest.param([], MASTER, _media_playlist(), id="no-asset"),
        pytest.param([ASSET], "#EXTM3U", _media_playlist(), id="no-substream"),
        pytest.param(
            [ASSET],
            MASTER,
            LivePlaylist(16.0, [LiveSegment(1, "s1", 16.0, None, "k1", None, None)]),
            id="no-widevine",
        ),
    ],
)
async def test_stream_details_fail_clearly_without_a_playable_stream(
    monkeypatch: pytest.MonkeyPatch,
    assets: list[dict[str, Any]],
    master: str,
    playlist: LivePlaylist,
) -> None:
    """A station Apple offers no usable stream for is reported as unplayable."""
    harness = _Harness([playlist], monkeypatch)
    _StreamDetailsApi(harness, assets, master)
    with pytest.raises(UnplayableMediaError):
        await harness.radio.get_stream_details("ra.1")


async def test_diagnostics_hold_no_address_of_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whole lookup and playback leaves only the mirror number in the diagnostics."""
    harness = _Harness([_media_playlist()], monkeypatch)
    _StreamDetailsApi(harness, [ASSET], MASTER)
    harness.radio._mirror_choice = 1
    details = await harness.radio.get_stream_details("ra.1")

    await _read(harness.radio, details, 3)

    diagnostics = harness.radio.monitor.as_dict()
    for address in ("origin.example", "primary.example", "backup.example", "m3u8", "secret"):
        assert address not in str(diagnostics)
    assert diagnostics["sessions"][0]["mirror"] == 1  # type: ignore[index]
    logger = cast("MagicMock", harness.radio.provider.logger)
    assert "example" not in str(logger.mock_calls)


async def test_live_stations_come_from_the_featured_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup behind the cache asks for the live stations of the storefront."""
    harness = _Harness([_media_playlist()], monkeypatch)
    provider: Any = harness.radio.provider
    provider.storefront = "de"
    provider.domain = "apple_music"
    provider.api_client.get_data = AsyncMock(
        return_value={"data": [{"id": "ra.1", "attributes": {"name": "Club", "isLive": True}}]}
    )

    get_stations: Any = AppleMusicLiveRadioManager.get_stations
    stations = await get_stations.__wrapped__(harness.radio)

    assert [(station.item_id, station.name) for station in stations] == [("ra.1", "Club")]
    provider.api_client.get_data.assert_awaited_once_with(
        "catalog/de/stations", **{"filter[featured]": "apple-music-live-radio"}
    )


async def test_the_provider_stream_closes_the_whole_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the stream ends the session at once, before a next stream can start."""
    harness = _Harness([_media_playlist()], monkeypatch)
    closed: list[str] = []
    stream_segments = harness.radio._stream_segments

    async def tracked_segments(details: StreamDetails) -> AsyncGenerator[bytes]:
        try:
            async for chunk in stream_segments(details):
                yield chunk
        finally:
            closed.append("segments")

    harness.radio._stream_segments = tracked_segments  # type: ignore[method-assign, assignment]
    provider: Any = MagicMock(live_radio_manager=harness.radio)
    stream = AppleMusicProvider.get_audio_stream(provider, harness.details)

    assert await anext(stream) == b"init"
    assert harness.radio.monitor.as_dict()["current_session"] == 1
    await stream.aclose()

    assert closed == ["segments"]
    assert harness.radio.monitor.as_dict()["current_session"] is None
    session = harness.radio.monitor.as_dict()["sessions"][0]  # type: ignore[index]
    assert session["end_reason"] == "consumer_closed"  # type: ignore[index]


async def test_a_new_track_is_noted_once_with_the_audio_it_starts_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same track in later segments is not noted again, and nothing shows it yet."""
    harness = _Harness([_media_playlist()], monkeypatch)
    track = _segment_with_track()

    async def fetch_bytes(url: str) -> bytes:
        return b"init" if url == "init" else track

    harness.radio._fetch_bytes = fetch_bytes  # type: ignore[method-assign]
    await _read(harness.radio, harness.details, 3)

    pending = harness.details.data["pending_metadata"]
    assert [(start, meta.title, meta.artist) for start, meta in pending] == [
        (0.0, "Song", "Artist")
    ]
    assert harness.details.stream_metadata is None
    session = harness.radio.monitor.as_dict()["sessions"][0]  # type: ignore[index]
    assert session["stream_title"] == "Artist - Song"  # type: ignore[index]


async def test_a_later_title_starts_at_the_audio_sent_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each title keeps the position of its own segment in the stream."""
    harness = _Harness([_media_playlist()], monkeypatch)
    titles = iter([StreamMetadata(title="Song"), StreamMetadata(title="News")])

    def next_title(_content: bytes) -> StreamMetadata | None:
        return next(titles, None)

    monkeypatch.setattr(f"{MODULE}.parse_stream_metadata", next_title)
    await _read(harness.radio, harness.details, 4)

    pending = harness.details.data["pending_metadata"]
    assert [(start, meta.title) for start, meta in pending] == [(0.0, "Song"), (16.0, "News")]


async def test_a_title_shows_when_its_audio_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    """The elapsed playback time decides the title, the newest one that has started wins."""
    harness = _Harness([_media_playlist()], monkeypatch)
    song, news, next_song = (
        StreamMetadata(title="Song"),
        StreamMetadata(title="News"),
        StreamMetadata(title="Next"),
    )
    harness.details.data["pending_metadata"] = [(0.0, song), (16.0, news), (32.0, next_song)]

    await harness.radio._update_stream_metadata(harness.details, 15)
    assert harness.details.stream_metadata == song

    await harness.radio._update_stream_metadata(harness.details, 40)
    assert harness.details.stream_metadata == next_song
    assert harness.details.data["pending_metadata"] == []

    await harness.radio._update_stream_metadata(harness.details, 41)
    assert harness.details.stream_metadata == next_song


async def test_a_restart_does_not_hold_back_its_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audio counts from 0 again while playback time goes on, titles of the old run go."""
    harness = _Harness([_media_playlist()], monkeypatch)
    harness.details.data["pending_metadata"] = [(500.0, StreamMetadata(title="Old"))]
    new_title = StreamMetadata(title="New")
    monkeypatch.setattr(f"{MODULE}.parse_stream_metadata", lambda _content: new_title)

    await _read(harness.radio, harness.details, 2)
    await harness.radio._update_stream_metadata(harness.details, 300)

    assert harness.details.stream_metadata == new_title


async def _refused_license_stream(harness: _Harness) -> None:
    """Read the stream until the refusal of Apple ends it."""
    with pytest.raises(UnplayableMediaError):
        async for _ in harness.radio.get_audio_stream(harness.details):
            pass


async def test_a_refused_renewal_keeps_the_station_from_a_new_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart must not take the stream back, so nothing is asked of Apple for a while."""
    harness = _Harness([_playlist(10, 3)], monkeypatch)
    harness.details.data["renew_at"] = 0.0
    harness.radio._get_license = AsyncMock(  # type: ignore[method-assign]
        side_effect=UnplayableMediaError("Apple refused the live radio license (HTTP 403)")
    )
    await _refused_license_stream(harness)
    api = _StreamDetailsApi(harness, [ASSET], MASTER)

    with pytest.raises(UnplayableMediaError):
        await harness.radio.get_stream_details("ra.1")

    api.get_data.assert_not_awaited()
    assert harness.radio._get_license.await_count == 1


async def test_a_refused_key_keeps_the_station_from_a_new_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key of a rotation is refused like a renewal, and nothing is asked of Apple either."""
    harness = _Harness([_playlist(10, 3), _rotating_playlist()], monkeypatch)
    harness.key_failures["k2"] = 99
    harness.key_error = UnplayableMediaError("Apple refused the live radio license")
    await _refused_license_stream(harness)
    api = _StreamDetailsApi(harness, [ASSET], MASTER)

    with pytest.raises(UnplayableMediaError):
        await harness.radio.get_stream_details("ra.1")

    api.get_data.assert_not_awaited()


async def test_a_network_error_on_a_key_does_not_refuse_the_station(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the answer of Apple counts as a refusal."""
    harness = _Harness([_playlist(10, 3), _rotating_playlist()], monkeypatch)
    harness.key_failures["k2"] = 1
    await _read(harness.radio, harness.details, 6)

    assert harness.radio._refused is None


async def test_a_refused_station_plays_again_after_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later start on purpose asks Apple again, and other stations never had to wait."""
    harness = _Harness([_media_playlist()], monkeypatch)

    _StreamDetailsApi(harness, [ASSET], MASTER)
    harness.radio._refused = ("ra.2", time.monotonic())
    assert (await harness.radio.get_stream_details("ra.1")).item_id == "ra.1"

    _StreamDetailsApi(harness, [ASSET], MASTER)
    harness.radio._refused = ("ra.1", time.monotonic() - REFUSED_LICENSE_SECONDS)
    assert (await harness.radio.get_stream_details("ra.1")).item_id == "ra.1"
    assert harness.radio._refused is None


async def test_a_failing_segment_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bad answer costs a retry and no audio."""
    harness = _Harness([_media_playlist()], monkeypatch)
    harness.radio._fetch_bytes = AsyncMock(  # type: ignore[method-assign]
        side_effect=[TimeoutError(), _http_error(503), b"audio"]
    )
    monkeypatch.setattr(f"{MODULE}.asyncio.sleep", AsyncMock())
    session = harness.radio.monitor.start_session("ra.1")

    content = await harness.radio._fetch_segment(_playlist(10, 1).segments[0], session)

    assert content == b"audio"
    assert session.segment_errors == 2
    assert session.segments_skipped == 0
    assert "secret" not in str(harness.radio.monitor.as_dict())


async def test_a_segment_that_stays_away_is_counted_as_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the retries the stream moves on, which the monitor records."""
    harness = _Harness([_media_playlist()], monkeypatch)
    harness.radio._fetch_bytes = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError()
    )
    monkeypatch.setattr(f"{MODULE}.asyncio.sleep", AsyncMock())
    session = harness.radio.monitor.start_session("ra.1")

    content = await harness.radio._fetch_segment(_playlist(10, 1).segments[0], session)

    assert content is None
    assert session.segment_errors == SEGMENT_RETRIES
    assert session.segments_skipped == 1
    assert "segment_lost" in harness.events()


async def test_a_live_playlist_is_read_from_the_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login of the account is not sent to the CDN, and an empty playlist is an error."""
    harness = _Harness([_media_playlist()], monkeypatch)
    fetch_playlist: Any = AppleMusicLiveRadioManager._fetch_playlist
    mass: Any = harness.radio.mass
    response = MagicMock(text=AsyncMock(return_value=MEDIA))
    mass.http_session.get.return_value.__aenter__.return_value = response

    playlist = await fetch_playlist(harness.radio, f"{BASE}index.m3u8")

    assert [segment.sequence for segment in playlist.segments] == [100, 101]
    assert "authorization" not in mass.http_session.get.call_args.kwargs["headers"]
    response.text = AsyncMock(return_value="#EXTM3U")
    with pytest.raises(UnplayableMediaError):
        await fetch_playlist(harness.radio, f"{BASE}index.m3u8")


async def test_a_granted_license_gives_the_key_of_the_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading a key stores it with its encryption settings and the renewal interval."""
    harness, segment = _license_harness(monkeypatch, {"status": 0, "renew-after": 780})
    segment.init_url = "init"
    encryption = MagicMock(key_id="k1")
    monkeypatch.setattr(f"{MODULE}.parse_init_segment", MagicMock(return_value=encryption))
    data: dict[str, Any] = {"key_server_url": "https://key.example", "keys": {}, "renew_at": None}

    load_key: Any = AppleMusicLiveRadioManager._load_key
    await load_key(harness.radio, segment, data)

    assert data["keys"]["k1"] == (bytes.fromhex("aabb"), encryption)
    assert data["renew_at"] == pytest.approx(time.monotonic() + 780.0, abs=5)
    get_content_key: Any = harness.radio.provider.streaming_manager.get_content_key
    assert get_content_key.await_args.args[0] == "https://key.example"
    assert get_content_key.await_args.args[2] == {"uri": WV, "event": "beats1"}
    assert get_content_key.await_args.kwargs["headers"]["x-apple-music-user-token"] == "token"
    assert "init_key_mismatch" not in harness.events()


async def test_an_answer_without_a_license_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apple can refuse inside a successful HTTP answer."""
    harness, segment = _license_harness(monkeypatch, MediaNotFoundError("No license"))

    with pytest.raises(UnplayableMediaError):
        await harness.radio._get_license("https://key.example", segment)

    assert "license_refused" in harness.events()


async def test_a_new_key_does_not_hold_back_the_audio_on_the_current_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hanging key server can not delay segments that do not need the new key."""
    harness = _Harness([_playlist(10, 3), _rotating_playlist()], monkeypatch)
    chunks = await _read(harness.radio, harness.details, 5)
    assert chunks[-1] == b"s13:k1"
    assert "k2" not in harness.key_attempts


async def test_a_key_is_only_requested_when_a_segment_needs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key the stream has not reached costs no request, the segment that needs it does."""
    harness = _Harness([_playlist(10, 3), _rotating_playlist()], monkeypatch)

    async with aclosing(harness.radio.get_audio_stream(harness.details)) as stream:
        async for chunk in stream:
            if chunk == b"s13:k1":
                assert "k2" not in harness.key_attempts
            if chunk == b"s14:k2":
                break

    assert harness.key_attempts["k2"] == 1


async def test_a_key_that_stays_away_restarts_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the audio on the current key, a lasting network problem ends as a stall."""
    harness = _Harness([_rotating_playlist()], monkeypatch)
    harness.key_failures["k2"] = 99
    monkeypatch.setattr(f"{MODULE}.MIN_STALL_SECONDS", 0.01)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(f"{MODULE}.asyncio.sleep", lambda _seconds: real_sleep(0.02))
    chunks: list[bytes] = []

    async def collect() -> None:
        async for chunk in harness.radio.get_audio_stream(harness.details):
            chunks.append(chunk)

    with pytest.raises(LiveStreamRestartError):
        await collect()

    assert chunks == [b"init", b"s13:k1"]
    events = harness.events()
    assert "key_load_error" in events
    assert "playlist_stalled" in events
