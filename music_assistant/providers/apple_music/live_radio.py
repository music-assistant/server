"""Live radio stations of Apple Music: stream lookup, license and the segment loop."""

from __future__ import annotations

import asyncio
import re
import struct
import time
from contextlib import aclosing
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

from aiohttp import ClientError, ClientResponseError, ClientTimeout
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    AudioError,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import AudioFormat, Radio
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from pywidevine import PSSH

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.hls import HLSMediaPlaylist, HLSMediaPlaylistParser, HLSMediaSegment
from music_assistant.helpers.playlists import parse_m3u
from music_assistant.models.music_provider import ProviderStreamLimitError

from .constants import PARSED_ITEM_CACHE_CHECKSUM
from .parsers import parse_station
from .streaming import LicenseRefusedError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .provider import AppleMusicProvider

LIVE_RADIO_FILTER = "apple-music-live-radio"
WIDEVINE_KEY_FORMAT = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
# segments behind the live edge to start from, as the HLS specification asks of a client
START_SEGMENTS = 3
DEFAULT_TARGET_DURATION = 16.0
SEGMENT_RETRIES = 3
RENEW_RETRY_SECONDS = 60
METADATA_UPDATE_SECONDS = 5
STATIONS_CACHE_SECONDS = 3600 * 24
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
_URI = re.compile(r'URI="([^"]*)"')


def live_pcm_format() -> AudioFormat:
    """Return the PCM that the decoder of every key delivers, so the stream keeps one format."""
    return AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32, channels=2
    )


class WidevinePlaylistParser(HLSMediaPlaylistParser):
    """HLS media playlist parser that keeps only the Widevine key of each segment."""

    def process_line(self, line: str) -> None:
        """Process a single line from the playlist, skipping the keys of other DRM systems."""
        if line.startswith("#EXT-X-KEY:") and WIDEVINE_KEY_FORMAT not in line:
            return
        super().process_line(line)


def select_substream(master: str, master_url: str) -> str:
    """
    Return the media playlist of the best quality from a master playlist.

    :param master: The master playlist.
    :param master_url: The url of the master playlist.
    """
    substreams = [item for item in parse_m3u(master) if item.stream_info]
    if not substreams:
        raise UnplayableMediaError("The live stream offers no substream")
    best = max(
        substreams,
        key=lambda item: int(cast("dict[str, str]", item.stream_info).get("BANDWIDTH", 0)),
    )
    return urljoin(master_url, best.path)


def segment_uri(line: str | None) -> str:
    """
    Return the URI of an EXT-X-KEY or EXT-X-MAP line.

    :param line: The playlist line, None when the segment has none.
    """
    if not line or not (match := _URI.search(line)):
        raise UnplayableMediaError("The live stream offers no Widevine stream")
    return match.group(1)


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


class AppleMusicLiveRadioManager:
    """Handles the live radio stations of an Apple Music account."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """
        Initialize the manager.

        :param provider: The Apple Music provider.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        # what the provider diagnostics show of the live radio
        self.state: dict[str, Any] = {"station_id": None, "key_changes": 0, "last_error": None}

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
        :raises ProviderStreamLimitError: If Apple refuses this account the stream.
        """
        asset = await self._get_asset(station_id)
        master = (await self._fetch(asset["url"])).decode()
        media_url = select_substream(master, asset["url"])
        playlist = await self._fetch_playlist(media_url)
        key_uri = segment_uri(playlist.segments[-1].key_line)
        data: dict[str, Any] = {
            "media_url": media_url,
            "key_server_url": asset["keyServerUrl"],
            "keys": {},
            "key_uri": key_uri,
            "renew_at": None,
        }
        data["keys"][key_uri] = await self._get_license(data, key_uri)
        data["renew_at"] = _renewal_time(data["keys"][key_uri][1])
        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=station_id,
            media_type=MediaType.RADIO,
            stream_type=StreamType.CUSTOM,
            audio_format=AudioFormat(content_type=ContentType.MP4, codec_type=ContentType.AAC),
            decoded_audio_format=live_pcm_format(),
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

    async def get_audio_stream(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes]:
        """
        Yield a live station as one continuous PCM stream until the consumer closes it.

        :param streamdetails: The stream details from get_stream_details.
        """
        data = cast("dict[str, Any]", streamdetails.data)
        self.state.update(station_id=streamdetails.item_id, key_changes=0, last_error=None)
        # the titles wait for the audio that was sent before them to play
        data.update(audio_seconds=0.0, last_metadata=None, pending_metadata=[])
        try:
            async with aclosing(self._new_segments(data)) as new_segments:
                segment = await anext(new_segments)
                # each key gets its own ffmpeg, the next one starts where the key changes,
                # while the PCM of all of them goes out as one stream
                while True:
                    key_uri = segment_uri(segment.key_line)
                    if not data["keys"].get(key_uri):
                        data["keys"][key_uri] = await self._get_license(data, key_uri)
                    key, renew_after = data["keys"][key_uri]
                    if key_uri != data["key_uri"]:
                        self.state["key_changes"] += 1
                        self.logger.debug(
                            "Live radio of %s moves to a new key", streamdetails.item_id
                        )
                        data["key_uri"] = key_uri
                        data["renew_at"] = _renewal_time(renew_after)
                    init = await self._fetch_segment(
                        urljoin(data["media_url"], segment_uri(segment.map_line))
                    )
                    if init is None:
                        raise UnplayableMediaError("The live radio init segment is not available")
                    next_key: list[HLSMediaSegment] = []
                    async with aclosing(
                        get_ffmpeg_stream(
                            self._key_input(new_segments, segment, init, key_uri, next_key, data),
                            input_format=AudioFormat(
                                content_type=ContentType.MP4, codec_type=ContentType.AAC
                            ),
                            output_format=live_pcm_format(),
                            extra_input_args=["-decryption_key", key],
                        )
                    ) as pcm:
                        async for chunk in pcm:
                            yield chunk
                    if not next_key:
                        raise AudioError("The live radio decoder stopped")
                    segment = next_key[0]
        except MusicAssistantError as err:
            # ffmpeg wraps an error of its input, the core needs the original to handle it
            if isinstance(err.__cause__, MusicAssistantError):
                err = err.__cause__
            self.state["last_error"] = str(err)
            raise err from None

    async def _key_input(
        self,
        new_segments: AsyncGenerator[HLSMediaSegment],
        first: HLSMediaSegment,
        init: bytes,
        key_uri: str,
        next_key: list[HLSMediaSegment],
        data: dict[str, Any],
    ) -> AsyncGenerator[bytes]:
        """Yield the init segment and the segments of one key, keeping the first of the next."""
        yield init
        segment = first
        while True:
            if content := await self._fetch_segment(
                urljoin(data["media_url"], segment.segment_url)
            ):
                metadata = parse_stream_metadata(content)
                if metadata and metadata != data["last_metadata"]:
                    data["last_metadata"] = metadata
                    data["pending_metadata"].append((data["audio_seconds"], metadata))
                data["audio_seconds"] += segment.duration
                yield content
            segment = await anext(new_segments)
            if segment_uri(segment.key_line) != key_uri:
                next_key.append(segment)
                return

    async def _new_segments(self, data: dict[str, Any]) -> AsyncGenerator[HLSMediaSegment]:
        """Yield each new segment of the live playlist once, from a few segments back."""
        next_sequence: int | None = None
        target = DEFAULT_TARGET_DURATION
        while True:
            new: list[tuple[int, HLSMediaSegment]] = []
            try:
                playlist = await self._fetch_playlist(data["media_url"])
            except MusicAssistantError as err:
                self.logger.warning("Live radio playlist failed: %s", err)
            else:
                target = playlist.target_duration or DEFAULT_TARGET_DURATION
                numbered = list(enumerate(playlist.segments, start=playlist.media_sequence))
                if next_sequence is None:
                    new = numbered[-START_SEGMENTS:]
                else:
                    new = [
                        (sequence, seg) for sequence, seg in numbered if sequence >= next_sequence
                    ]
                    if new and new[0][0] > next_sequence:
                        self.logger.warning(
                            "Live radio skipped %s segments", new[0][0] - next_sequence
                        )
                await self._prepare_next_key(data, [segment for _, segment in new])
            if data["renew_at"] is not None and time.monotonic() >= data["renew_at"]:
                await self._renew_license(data)
            # a stream that stops moving is ended by the core's stall timeout, which restarts it
            for sequence, segment in new:
                next_sequence = sequence + 1
                yield segment
            # sending is paced by the consumer, a wait after it could leave the decoder empty
            if not new:
                await asyncio.sleep(target / 2)

    async def _prepare_next_key(self, data: dict[str, Any], ahead: list[HLSMediaSegment]) -> None:
        """Get the license of a key that comes up, so the change of key has no gap."""
        upcoming = {data["key_uri"], *(segment_uri(segment.key_line) for segment in ahead)}
        data["keys"] = {uri: key for uri, key in data["keys"].items() if uri in upcoming}
        for key_uri in upcoming - data["keys"].keys():
            # one try ahead of time, the change of key asks again if this one fails
            data["keys"][key_uri] = None
            try:
                data["keys"][key_uri] = await self._get_license(data, key_uri)
            except MusicAssistantError as err:
                self.logger.debug("Live radio license ahead of the key change failed: %s", err)

    async def _renew_license(self, data: dict[str, Any]) -> None:
        """Renew the license of the current key like Apple's own client does."""
        try:
            _, renew_after = await self._get_license(data, data["key_uri"])
        except ProviderStreamLimitError:
            raise
        except MusicAssistantError as err:
            self.logger.warning("Live radio license renewal failed: %s", err)
            data["renew_at"] = time.monotonic() + RENEW_RETRY_SECONDS
        else:
            data["renew_at"] = _renewal_time(renew_after)

    async def _get_license(self, data: dict[str, Any], key_uri: str) -> tuple[str, float]:
        """
        Return the content key of a key uri and the renewal interval of its license.

        :raises ProviderStreamLimitError: If Apple refuses this account the license.
        """
        streaming = self.provider.streaming_manager
        if not streaming.cdm_available:
            raise UnplayableMediaError("Widevine CDM files are not available for live radio")
        try:
            key, answer = await streaming.get_content_key(
                data["key_server_url"],
                PSSH(key_uri.split("base64,", 1)[-1]),
                {"uri": key_uri, "event": "beats1"},
                "live radio",
                headers={
                    "x-apple-music-user-token": cast("str", self.provider.music_user_token),
                    "x-apple-renewal": "true",
                },
                check_status=True,
            )
        except LicenseRefusedError as err:
            # Apple refuses the license while this account plays elsewhere
            self.logger.debug(
                "Live radio license refused: status %s, error %s", err.status, err.error_code
            )
            raise ProviderStreamLimitError(self.provider, None) from None
        except ClientResponseError as err:
            # the text of this error holds the url, so only its status is passed on
            self.logger.debug("Live radio license failed: HTTP %s", err.status)
            if err.status == 401:
                raise LoginFailed(
                    "Apple Music denied the live radio license: the account needs to be "
                    "signed in again"
                ) from None
            if err.status == 403:
                raise ProviderStreamLimitError(self.provider, None) from None
            raise UnplayableMediaError(f"Live radio license failed: HTTP {err.status}") from None
        except MediaNotFoundError as err:
            raise UnplayableMediaError(str(err)) from None
        except (ClientError, TimeoutError, ValueError) as err:
            raise UnplayableMediaError(f"Live radio license failed: {type(err).__name__}") from None
        return key, float(answer.get("renew-after") or 0)

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
        return cast("dict[str, Any]", assets[0])

    async def _fetch_playlist(self, url: str) -> HLSMediaPlaylist:
        """Return a live media playlist, with only the Widevine key of each segment."""
        return WidevinePlaylistParser((await self._fetch(url)).decode()).parse()

    async def _fetch_segment(self, url: str) -> bytes | None:
        """Return an init or media segment, None when it stayed unavailable after some retries."""
        for attempt in range(1, SEGMENT_RETRIES + 1):
            try:
                return await self._fetch(url)
            except MusicAssistantError as err:
                self.logger.warning("Live radio segment failed (attempt %s): %s", attempt, err)
                await asyncio.sleep(attempt)
        return None

    async def _fetch(self, url: str) -> bytes:
        """Return a playlist or segment from the CDN, without the account's login."""
        try:
            async with self.mass.http_session.get(
                url, headers=_CDN_HEADERS, timeout=_CDN_TIMEOUT
            ) as response:
                response.raise_for_status()
                return await response.read()
        except (ClientError, TimeoutError) as err:
            # the text of this error holds the url, which must not reach a log or a caller
            reason = (
                f"HTTP {err.status}" if isinstance(err, ClientResponseError) else type(err).__name__
            )
            raise UnplayableMediaError(f"Live radio request failed: {reason}") from None


def _parse_emsg(box: bytes) -> StreamMetadata | None:
    """Return the track of an emsg box that holds an ID3 tag, if it names one."""
    # imported here like in the tags helper, to keep mutagen off the startup path
    from mutagen import MutagenError  # noqa: PLC0415
    from mutagen.id3 import ID3  # noqa: PLC0415

    try:
        if box[0] == 1:
            # version 1 has its times (20 bytes) in front of the scheme and value
            scheme_end = box.index(0, 24)
            payload = box[box.index(0, scheme_end + 1) + 1 :]
        else:
            # version 0 has its times (16 bytes) after the scheme and value
            scheme_end = box.index(0, 4)
            payload = box[box.index(0, scheme_end + 1) + 1 + 16 :]
        tag: Any = ID3(BytesIO(payload))  # type: ignore[no-untyped-call]
    except ValueError, IndexError, MutagenError:
        return None
    title = tag.get("TIT2")
    if not title or not title.text:
        return None
    artist, album = tag.get("TPE1"), tag.get("TALB")
    return StreamMetadata(
        title=str(title.text[0]),
        artist=str(artist.text[0]) if artist and artist.text else None,
        album=str(album.text[0]) if album and album.text else None,
        image_url=next(
            (frame.url for frame in tag.getall("WXXX") if frame.desc == "artworkURL_640x"), None
        ),
    )


def _renewal_time(renew_after: float) -> float | None:
    """Return when a license with the given renewal interval is renewed, None for never."""
    return time.monotonic() + renew_after if renew_after else None
