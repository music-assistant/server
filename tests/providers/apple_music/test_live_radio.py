"""Unit tests for the Apple Music live radio playlist, metadata and segment loop."""

import asyncio
import struct
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.apple_music.live_radio import (
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
        self.fail_key_once: str | None = None

        async def load_key(segment: LiveSegment, data: dict[str, Any]) -> None:
            if segment.key_id == self.fail_key_once:
                self.fail_key_once = None
                raise TimeoutError
            config = self.audio_configs.get(cast("str", segment.key_id), b"config")
            data["keys"][segment.key_id] = (
                cast("str", segment.key_id).encode(),
                MagicMock(audio_config=config),
            )

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
                "key_id": "k1",
                "init_url": "init",
                "audio_config": b"config",
                "widevine_uri": WV,
                "renew_after": 0,
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
    """Preparing the next key may fail while the stream still plays on the current one."""
    harness = _Harness(
        [_playlist(10, 3), _playlist(11, 3, key="k2"), _playlist(14, 1)], monkeypatch
    )
    harness.fail_key_once = "k2"
    assert (await _read(harness.radio, harness.details, 5))[0] == b"init"
    assert "playlist_error" in harness.events()


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


async def test_a_radio_item_streams_through_the_live_radio_manager() -> None:
    """Only a radio item takes the live path, songs keep their own."""
    provider: Any = MagicMock()
    live, song = MagicMock(), MagicMock()
    provider.live_radio_manager.get_stream_details = AsyncMock(return_value=live)
    provider.streaming_manager.get_stream_details = AsyncMock(return_value=song)

    assert await AppleMusicProvider.get_stream_details(provider, "ra.1", MediaType.RADIO) is live
    assert await AppleMusicProvider.get_stream_details(provider, "1", MediaType.TRACK) is song
