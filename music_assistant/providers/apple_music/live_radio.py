"""Live radio stations of Apple Music: stream lookup, license and the segment loop."""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

from aiohttp import ClientError, ClientResponseError, ClientTimeout
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import AudioFormat, Radio
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from pywidevine import PSSH

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import utc

from .constants import PARSED_ITEM_CACHE_CHECKSUM
from .helpers.live_decrypt import TrackEncryption, decrypt_segment, parse_init_segment
from .helpers.live_monitor import LiveRadioMonitor, LiveSession
from .parsers import parse_station

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .provider import AppleMusicProvider

LIVE_RADIO_FILTER = "apple-music-live-radio"
WIDEVINE_KEY_FORMAT = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
# segments behind the live edge to start from, as the HLS specification asks of a client
START_SEGMENTS = 3
CHUNK_SIZE = 65536
HEARTBEAT_INTERVAL = 60
MIN_STALL_SECONDS = 45
DEFAULT_TARGET_DURATION = 16.0
SEGMENT_RETRIES = 3
RENEW_RETRY_SECONDS = 60
RESUME_WINDOW_SECONDS = 120
# long enough for every restart of the stream to give up, short enough to play again on purpose
REFUSED_LICENSE_SECONDS = 60
METADATA_UPDATE_SECONDS = 5
STATIONS_CACHE_SECONDS = 3600 * 24
SLOW_CONSUMER_SECONDS = 30
INPUT_BURST_SECONDS = "5"
_CDN_TIMEOUT = ClientTimeout(total=20)
_CDN_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
    ),
    "origin": "https://music.apple.com",
    "referer": "https://music.apple.com/",
}
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_KEY_ID = re.compile(r"skd://[^/]+/([0-9A-Fa-f]{32})")


class LiveStreamRestartError(MusicAssistantError):
    """The stream has to continue on fresh stream details."""


@dataclass(slots=True)
class LiveSegment:
    """One media segment of a live playlist."""

    sequence: int
    url: str
    duration: float
    init_url: str | None
    key_id: str | None
    widevine_uri: str | None
    program_time: datetime | None


@dataclass(slots=True)
class LivePlaylist:
    """A parsed live media playlist."""

    target_duration: float
    segments: list[LiveSegment]


def parse_master_playlist(text: str, base_url: str) -> list[str]:
    """
    Return the media playlists of the best quality, the primary before its mirrors.

    :param text: The master playlist.
    :param base_url: The url of the master playlist.
    """
    substreams: list[tuple[int, str]] = []
    bandwidth = 0
    for line in text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = dict(_ATTRIBUTE.findall(line))
            bandwidth = int(attributes.get("BANDWIDTH", "0"))
        elif line and not line.startswith("#"):
            substreams.append((bandwidth, urljoin(base_url, line)))
    best = max((rate for rate, _ in substreams), default=0)
    return [url for rate, url in substreams if rate == best]


def parse_media_playlist(text: str, base_url: str) -> LivePlaylist:
    """
    Parse a live media playlist.

    :param text: The media playlist.
    :param base_url: The url of the media playlist.
    """
    target = DEFAULT_TARGET_DURATION
    sequence = 0
    duration = 0.0
    init_url: str | None = None
    key_id: str | None = None
    widevine_uri: str | None = None
    program_time: datetime | None = None
    segments: list[LiveSegment] = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program_time = datetime.fromisoformat(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            uri = dict(_ATTRIBUTE.findall(line)).get("URI", "").strip('"')
            init_url = urljoin(base_url, uri)
        elif line.startswith("#EXT-X-KEY:"):
            attributes = dict(_ATTRIBUTE.findall(line))
            uri = attributes.get("URI", "").strip('"')
            if match := _KEY_ID.search(uri):
                key_id = match.group(1).lower()
            if attributes.get("KEYFORMAT", "").strip('"') == WIDEVINE_KEY_FORMAT:
                widevine_uri = uri
        elif line and not line.startswith("#"):
            segments.append(
                LiveSegment(
                    sequence=sequence,
                    url=urljoin(base_url, line),
                    duration=duration,
                    init_url=init_url,
                    key_id=key_id,
                    widevine_uri=widevine_uri,
                    program_time=program_time,
                )
            )
            sequence += 1
            program_time = None
    return LivePlaylist(target_duration=target, segments=segments)


def parse_stream_metadata(segment: bytes) -> StreamMetadata | None:
    """
    Return what is on air according to the timed metadata in front of a segment's audio.

    :param segment: The bytes of a media segment.
    """
    position = 0
    while position + 8 <= len(segment):
        size, box = struct.unpack(">I4s", segment[position : position + 8])
        if size < 8 or box == b"moof":
            return None
        if box == b"emsg" and (found := _parse_emsg(segment[position + 8 : position + size])):
            return found
        position += size
    return None


def _parse_emsg(box: bytes) -> StreamMetadata | None:
    """Return the track of an emsg box that holds an ID3 tag, if it names one."""
    try:
        if box[0] == 1:
            scheme_end = box.index(0, 24)
            value_end = box.index(0, scheme_end + 1)
            payload = box[value_end + 1 :]
        else:
            scheme_end = box.index(0, 4)
            value_end = box.index(0, scheme_end + 1)
            payload = box[value_end + 1 + 16 :]
    except ValueError:
        return None
    return _parse_id3(payload) if payload[:3] == b"ID3" else None


def _parse_id3(tag: bytes) -> StreamMetadata | None:
    """Return the title, artist, album and artwork of an ID3v2.4 tag."""
    frames: dict[str, str] = {}
    image_url: str | None = None
    position = 10
    end = min(10 + _syncsafe(tag[6:10]), len(tag))
    while position + 10 <= end:
        name = tag[position : position + 4].decode("latin1")
        size = _syncsafe(tag[position + 4 : position + 8])
        if not size:
            break
        body = tag[position + 10 : position + 10 + size]
        position += 10 + size
        if name in ("TIT2", "TPE1", "TALB"):
            frames[name] = body[1:].rstrip(b"\0").decode("utf-8", "replace")
        elif name == "WXXX" and b"artworkURL_640x\0" in body:
            image_url = body.split(b"\0", 1)[1].rstrip(b"\0").decode("utf-8", "replace")
    if not frames.get("TIT2"):
        return None
    return StreamMetadata(
        title=frames["TIT2"],
        artist=frames.get("TPE1"),
        album=frames.get("TALB"),
        image_url=image_url,
    )


def _syncsafe(data: bytes) -> int:
    """Return the value of a four byte ID3 syncsafe integer."""
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3] if len(data) == 4 else 0


class AppleMusicLiveRadioManager:
    """Handles the live radio stations of an Apple Music account."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """
        Initialize the manager.

        :param provider: The Apple Music provider.
        """
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.monitor = LiveRadioMonitor(provider.logger)
        self._mirror_choice = 0
        self._resume: tuple[str, int, float] | None = None
        self._refused: tuple[str, float] | None = None

    @use_cache(
        STATIONS_CACHE_SECONDS,
        allow_expired_cache=True,
        cache_checksum=PARSED_ITEM_CACHE_CHECKSUM,
    )
    async def get_stations(self) -> list[Radio]:
        """Return the live stations of the account's storefront."""
        response = await self.provider.api_client.get_data(
            f"catalog/{self.provider.storefront}/stations",
            **{"filter[featured]": LIVE_RADIO_FILTER},
        )
        return [parse_station(self.provider, station) for station in response.get("data", [])]

    async def get_stream_details(self, station_id: str) -> StreamDetails:
        """
        Return the stream details of a live station, with the license of this account.

        :param station_id: The Apple Music station id.
        """
        try:
            return await self._get_stream_details(station_id)
        except ClientError as err:
            # the text of this error holds the url, which must not reach a caller's log
            raise UnplayableMediaError(
                f"No stream for live station {station_id}: {_describe_error(err)}"
            ) from None

    async def get_audio_stream(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes]:
        """
        Yield a live station as one continuous fragmented MP4, decrypted in memory to play it.

        It ends only by an error or the consumer closing it.

        :param streamdetails: The stream details from get_stream_details.
        """
        reason = "ended"
        session = self.monitor.start_session(streamdetails.item_id)
        session.mirror = streamdetails.data["mirror"]
        session.key_id = (streamdetails.data["key_id"] or "")[:8]
        try:
            async with aclosing(self._stream_segments(streamdetails)) as stream:
                async for chunk in stream:
                    yield chunk
        except GeneratorExit:
            reason = "consumer_closed"
            raise
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except LiveStreamRestartError as err:
            reason = f"restart: {err}"
            raise
        except Exception as err:
            reason = _describe_error(err)
            self.monitor.event("stream_error", level=logging.ERROR, error=reason)
            if isinstance(err, ClientError):
                # the text of this error holds the url, which must not reach a caller's log
                raise UnplayableMediaError(f"Live radio stream failed: {reason}") from None
            raise
        finally:
            self.monitor.end_session(reason)

    async def _get_stream_details(self, station_id: str) -> StreamDetails:
        """Return the stream details of a live station, as get_stream_details describes."""
        # a new license could take the stream back from a device that Apple moved it to
        if self._refused and self._refused[0] == station_id:
            if time.monotonic() - self._refused[1] < REFUSED_LICENSE_SECONDS:
                raise UnplayableMediaError(
                    f"Apple recently refused the license of live station {station_id}"
                )
            self._refused = None
        asset = await self._get_asset(station_id)
        async with self.mass.http_session.get(
            asset["url"], headers=_CDN_HEADERS, timeout=_CDN_TIMEOUT
        ) as response:
            response.raise_for_status()
            substreams = parse_master_playlist(await response.text(), asset["url"])
        if not substreams:
            raise UnplayableMediaError(f"No stream found for live station {station_id}")
        mirror = self._mirror_choice % len(substreams)
        media_url = substreams[mirror]
        playlist = await self._fetch_playlist(media_url)
        newest = playlist.segments[-1]
        if not newest.widevine_uri or not newest.init_url:
            raise UnplayableMediaError(f"Live station {station_id} offers no Widevine stream")
        data: dict[str, Any] = {
            "media_url": media_url,
            "mirror": mirror,
            "key_server_url": asset["keyServerUrl"],
            "keys": {},
            "key_id": newest.key_id,
            "widevine_uri": newest.widevine_uri,
            "init_url": newest.init_url,
            "audio_config": None,
            "renew_at": None,
            "pending_metadata": [],
        }
        await self._load_key(newest, data)
        data["audio_config"] = data["keys"][newest.key_id][1].audio_config
        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=station_id,
            media_type=MediaType.RADIO,
            stream_type=StreamType.CUSTOM,
            audio_format=AudioFormat(content_type=ContentType.MP4, codec_type=ContentType.AAC),
            extra_input_args=[
                # the same live pacing as other HLS radio stations
                "-readrate",
                "1",
                "-readrate_initial_burst",
                INPUT_BURST_SECONDS,
            ],
            can_seek=False,
            allow_seek=False,
            data=data,
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=METADATA_UPDATE_SECONDS,
        )

    async def _stream_segments(  # noqa: PLR0915
        self, streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes]:
        """Send the init segment, then every new segment from the live playlist."""
        data = cast("dict[str, Any]", streamdetails.data)
        session = cast("LiveSession", self.monitor.current)
        station_id = streamdetails.item_id
        media_url: str = data["media_url"]
        started = time.monotonic()
        last_new_segment = started
        last_heartbeat = started
        last_metadata: StreamMetadata | None = None
        next_sequence = self._resume_sequence(station_id)
        target = DEFAULT_TARGET_DURATION
        data["pending_metadata"] = []

        yield await self._fetch_bytes(data["init_url"])

        while True:
            try:
                playlist = await self._fetch_playlist(media_url)
                target = playlist.target_duration
            except (ClientError, TimeoutError, MusicAssistantError) as err:
                session.playlist_errors += 1
                self.monitor.event(
                    "playlist_error", level=logging.WARNING, error=_describe_error(err)
                )
                playlist = None
            stalled = time.monotonic() - last_new_segment
            if stalled > max(3 * target, MIN_STALL_SECONDS):
                # try the other mirror the next time this stream is started
                self._mirror_choice += 1
                self.monitor.event("playlist_stalled", level=logging.WARNING, seconds=int(stalled))
                raise LiveStreamRestartError(f"no new segment for {int(stalled)} seconds")

            sent_audio = False
            for segment in self._segments_to_send(playlist, next_sequence):
                try:
                    await self._switch_key(segment, data, session)
                except (ClientError, TimeoutError) as err:
                    # not sent yet, so the next pass tries again and a lasting failure stalls
                    session.license_errors += 1
                    self.monitor.event(
                        "key_load_error",
                        level=logging.WARNING,
                        key=(segment.key_id or "")[:8],
                        error=_describe_error(err),
                    )
                    break
                if next_sequence is not None and segment.sequence > next_sequence:
                    skipped = segment.sequence - next_sequence
                    session.segments_skipped += skipped
                    self.monitor.event(
                        "segments_skipped", level=logging.WARNING, count=skipped, at=next_sequence
                    )
                content = await self._fetch_segment(segment, session)
                next_sequence = segment.sequence + 1
                self._remember_position(station_id, next_sequence)
                if content is None:
                    continue
                last_new_segment = time.monotonic()

                metadata = parse_stream_metadata(content)
                if metadata and metadata != last_metadata:
                    last_metadata = metadata
                    session.stream_title = f"{metadata.artist} - {metadata.title}"
                    self.monitor.event(
                        "stream_metadata",
                        title=session.stream_title,
                        at_audio_second=round(session.audio_seconds_sent),
                    )
                    data["pending_metadata"].append((session.audio_seconds_sent, metadata))

                key, encryption = data["keys"][segment.key_id]
                content = await asyncio.to_thread(decrypt_segment, content, key, encryption)

                for start in range(0, len(content), CHUNK_SIZE):
                    wait_started = time.monotonic()
                    yield content[start : start + CHUNK_SIZE]
                    waited = time.monotonic() - wait_started
                    session.max_consumer_wait_s = round(max(session.max_consumer_wait_s, waited), 1)
                    if waited > SLOW_CONSUMER_SECONDS:
                        self.monitor.event(
                            "consumer_slow", level=logging.WARNING, waited=round(waited, 1)
                        )
                sent_audio = True
                session.segments_sent += 1
                session.audio_seconds_sent += segment.duration
                if segment.program_time:
                    session.behind_live_s = round((utc() - segment.program_time).total_seconds(), 1)

            now = time.monotonic()
            if data["renew_at"] is not None and now >= data["renew_at"]:
                data["renew_at"] = await self._renew_license(data, session, now)
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = now
                self.monitor.event(
                    "heartbeat",
                    segments=session.segments_sent,
                    audio_seconds=round(session.audio_seconds_sent),
                    ahead_seconds=round(session.audio_seconds_sent - (now - started), 1),
                    since_new_segment=round(now - last_new_segment, 1),
                    behind_live=session.behind_live_s,
                    playlist_errors=session.playlist_errors,
                    segment_errors=session.segment_errors,
                    skipped=session.segments_skipped,
                    consumer_wait_max=session.max_consumer_wait_s,
                    stream_title=session.stream_title,
                )
            # sending is paced by the consumer, a wait after it could leave the consumer empty
            if not sent_audio:
                await asyncio.sleep(target / 2)

    def _segments_to_send(
        self, playlist: LivePlaylist | None, next_sequence: int | None
    ) -> list[LiveSegment]:
        """Return the segments that were not sent yet, or those to start from."""
        if playlist is None or not playlist.segments:
            return []
        if next_sequence is None:
            return playlist.segments[-START_SEGMENTS:]
        return [seg for seg in playlist.segments if seg.sequence >= next_sequence]

    async def _switch_key(
        self, segment: LiveSegment, data: dict[str, Any], session: LiveSession
    ) -> None:
        """Move on to the key of a segment, without interrupting the stream."""
        if segment.key_id not in data["keys"]:
            try:
                await self._load_key(segment, data)
            except UnplayableMediaError:
                self._refused = (session.station_id, time.monotonic())
                raise
        if segment.key_id == data["key_id"]:
            return
        encryption: TrackEncryption = data["keys"][segment.key_id][1]
        if encryption.audio_config != data["audio_config"]:
            # the decoder keeps the first init segment, which no longer describes this audio
            self._remember_position(session.station_id, segment.sequence)
            raise LiveStreamRestartError("the stream changed its audio format")
        self.monitor.event(
            "key_change", old=(data["key_id"] or "")[:8], new=(segment.key_id or "")[:8]
        )
        session.key_id = (segment.key_id or "")[:8]
        session.key_changes += 1
        data["key_id"] = segment.key_id
        data["widevine_uri"] = segment.widevine_uri
        data["keys"] = {segment.key_id: data["keys"][segment.key_id]}

    async def _load_key(self, segment: LiveSegment, data: dict[str, Any]) -> None:
        """Store the content key and encryption settings of a segment's key."""
        key, renew_after = await self._get_license(data["key_server_url"], segment)
        encryption = parse_init_segment(await self._fetch_bytes(cast("str", segment.init_url)))
        if encryption.key_id != segment.key_id:
            self.monitor.event(
                "init_key_mismatch",
                level=logging.WARNING,
                playlist=(segment.key_id or "")[:8],
                init=encryption.key_id[:8],
            )
        data["keys"][segment.key_id] = (bytes.fromhex(key), encryption)
        data["renew_at"] = time.monotonic() + renew_after if renew_after else None

    def _resume_sequence(self, station_id: str) -> int | None:
        """Return where a restarted stream continues, None when it starts at the live edge."""
        if self._resume is None:
            return None
        resume_station, sequence, remembered = self._resume
        if resume_station != station_id or time.monotonic() - remembered > RESUME_WINDOW_SECONDS:
            return None
        return sequence

    def _remember_position(self, station_id: str, sequence: int) -> None:
        """Remember the next segment, so a restart continues without replaying audio."""
        self._resume = (station_id, sequence, time.monotonic())

    async def _update_stream_metadata(
        self, streamdetails: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Show the newest title whose audio has started to play.

        :param streamdetails: The stream details of the live station.
        :param elapsed_time: Elapsed playback time in seconds.
        """
        # a restart counts the audio from 0 again while the playback time goes on, so its
        # titles show when they are sent, a few seconds early, which a listener can accept
        pending: list[tuple[float, StreamMetadata]] = cast(
            "dict[str, Any]", streamdetails.data
        ).get("pending_metadata", [])
        while pending and pending[0][0] <= elapsed_time:
            _, streamdetails.stream_metadata = pending.pop(0)

    async def _get_asset(self, station_id: str) -> dict[str, Any]:
        """Return the stream url and key server Apple hands this account for a station."""
        api = self.provider.api_client
        station = await api.get_data(f"catalog/{self.provider.storefront}/stations/{station_id}")
        play_params = station["data"][0]["attributes"]["playParams"]
        params = {
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in play_params.items()
        }
        response = await api.get_data("play/assets", keyFormat="web", **params)
        assets = response.get("results", {}).get("assets") or []
        if not assets:
            raise UnplayableMediaError(f"Apple returned no stream for live station {station_id}")
        self.monitor.event("assets", station=station_id)
        return cast("dict[str, Any]", assets[0])

    async def _fetch_playlist(self, url: str) -> LivePlaylist:
        """Return a media playlist, which is paced by its target duration by the caller."""
        async with self.mass.http_session.get(
            url, headers=_CDN_HEADERS, timeout=_CDN_TIMEOUT
        ) as response:
            response.raise_for_status()
            playlist = parse_media_playlist(await response.text(), url)
        if not playlist.segments:
            raise UnplayableMediaError("The live playlist holds no segments")
        return playlist

    async def _fetch_bytes(self, url: str) -> bytes:
        """Return the content of an init or media segment."""
        async with self.mass.http_session.get(
            url, headers=_CDN_HEADERS, timeout=_CDN_TIMEOUT
        ) as response:
            response.raise_for_status()
            return await response.read()

    async def _fetch_segment(self, segment: LiveSegment, session: LiveSession) -> bytes | None:
        """Return a media segment, None when it stayed unavailable after some retries."""
        for attempt in range(1, SEGMENT_RETRIES + 1):
            fetch_started = time.monotonic()
            try:
                content = await self._fetch_bytes(segment.url)
            except (ClientError, TimeoutError) as err:
                session.segment_errors += 1
                self.monitor.event(
                    "segment_error",
                    level=logging.WARNING,
                    sequence=segment.sequence,
                    attempt=attempt,
                    error=_describe_error(err),
                )
                await asyncio.sleep(attempt)
                continue
            fetch_ms = int((time.monotonic() - fetch_started) * 1000)
            session.max_segment_fetch_ms = max(session.max_segment_fetch_ms, fetch_ms)
            if fetch_ms > segment.duration * 500:
                self.monitor.event(
                    "segment_slow",
                    level=logging.WARNING,
                    sequence=segment.sequence,
                    fetch_ms=fetch_ms,
                )
            return content
        session.segments_skipped += 1
        self.monitor.event("segment_lost", level=logging.ERROR, sequence=segment.sequence)
        return None

    async def _renew_license(
        self, data: dict[str, Any], session: LiveSession, now: float
    ) -> float | None:
        """Renew the license like Apple's own client, stop when Apple refuses it."""
        segment = LiveSegment(0, "", 0, None, data["key_id"], data["widevine_uri"], None)
        try:
            _, renew_after = await self._get_license(data["key_server_url"], segment, renewal=True)
        except UnplayableMediaError:
            self._refused = (session.station_id, time.monotonic())
            raise
        except (ClientError, TimeoutError) as err:
            session.license_errors += 1
            self.monitor.event(
                "license_renew_error", level=logging.WARNING, error=_describe_error(err)
            )
            return now + RENEW_RETRY_SECONDS
        session.license_renewals += 1
        return now + renew_after if renew_after else None

    async def _get_license(
        self, key_server_url: str, segment: LiveSegment, renewal: bool = False
    ) -> tuple[str, float]:
        """Return the content key and renewal interval from the license of this account."""
        streaming = self.provider.streaming_manager
        if not streaming.cdm_available or not segment.widevine_uri:
            raise UnplayableMediaError("Widevine CDM files are not available for live radio")
        started = time.monotonic()
        try:
            key, answer = await streaming.get_content_key(
                key_server_url,
                PSSH(segment.widevine_uri.split("base64,", 1)[-1]),
                {"uri": segment.widevine_uri, "event": "beats1"},
                headers={
                    "x-apple-music-user-token": cast("str", self.provider.music_user_token),
                    "x-apple-renewal": "true",
                },
            )
        except ClientResponseError as err:
            # a rate limit passes, any other client error is Apple's answer to this account
            if 400 <= err.status < 500 and err.status != 429:
                self.monitor.event("license_refused", level=logging.ERROR, http_status=err.status)
                raise UnplayableMediaError(
                    f"Apple refused the live radio license (HTTP {err.status})"
                ) from None
            raise
        except MediaNotFoundError as err:
            self.monitor.event("license_refused", level=logging.ERROR, error=str(err))
            raise UnplayableMediaError(f"Apple refused the live radio license: {err}") from None
        renew_after = float(answer.get("renew-after") or 0)
        self.monitor.event(
            "license_renewed" if renewal else "license",
            status=answer.get("status"),
            renew_after=renew_after,
            ms=int((time.monotonic() - started) * 1000),
        )
        return key, renew_after


def _describe_error(err: Exception) -> str:
    """Return an error for the monitor, without a url that could hold a signed part."""
    if isinstance(err, ClientResponseError):
        return f"HTTP {err.status}"
    if isinstance(err, MusicAssistantError):
        return str(err) or type(err).__name__
    return type(err).__name__
