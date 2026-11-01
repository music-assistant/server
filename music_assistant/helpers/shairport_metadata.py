"""Decoder for shairport-sync metadata items, shared by its metadata pipe and MQTT outputs."""

from __future__ import annotations

import math
import plistlib
from typing import TYPE_CHECKING, Any, Final
from xml.parsers.expat import ExpatError

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

# 4.x hard-codes this rate for prgr; 5.x buffered streams may run at 48000.
PROGRESS_RTP_RATE: Final[int] = 44100
RTP_TIMESTAMP_MASK: Final[int] = 0xFFFFFFFF
RTP_TIMESTAMP_HALF_RANGE: Final[int] = 1 << 31

MUTED_AIRPLAY_VOLUME: Final[float] = -144.0
MIN_AIRPLAY_VOLUME: Final[float] = -30.0

CORE_TEXT_KEYS: Final[dict[str, str]] = {"minm": "title", "asar": "artist", "asal": "album"}
SSNC_EVENT_KEYS: Final[dict[str, str]] = {
    "pbeg": "play_begin",
    "pend": "play_end",
    "pfls": "flush",
    "paus": "paused",
    "prsm": "resumed",
    "pres": "resumed",  # codespell:ignore pres
    "abeg": "active_begin",
    "aend": "active_end",
}
SSNC_TEXT_KEYS: Final[dict[str, str]] = {
    "snam": "sender_name",
    "clip": "client_ip",
    "acre": "active_remote",
    "styp": "stream_type",
}

NOW_PLAYING_TEXT_KEYS: Final[dict[str, str]] = {
    "kMRMediaRemoteNowPlayingInfoTitle": "title",
    "kMRMediaRemoteNowPlayingInfoArtist": "artist",
    "kMRMediaRemoteNowPlayingInfoAlbum": "album",
}
NOW_PLAYING_DURATION: Final[str] = "kMRMediaRemoteNowPlayingInfoDuration"
NOW_PLAYING_ELAPSED_TIME: Final[str] = "kMRMediaRemoteNowPlayingInfoElapsedTime"
NOW_PLAYING_PLAYBACK_RATE: Final[str] = "kMRMediaRemoteNowPlayingInfoPlaybackRate"
NOW_PLAYING_UNIQUE_ID: Final[str] = "kMRMediaRemoteNowPlayingInfoUniqueIdentifier"
NOW_PLAYING_ARTWORK: Final[str] = "kMRMediaRemoteNowPlayingInfoArtworkData"
PLAYBACK_STATE_KEYS: Final[dict[int, str]] = {1: "resumed", 2: "paused", 3: "play_end"}


class ShairportMetadataDecoder:
    """Turn shairport-sync metadata items into metadata updates."""

    def __init__(self, on_update: Callable[[dict[str, Any]], None], logger: logging.Logger) -> None:
        """
        Initialize the decoder.

        :param on_update: Called synchronously with every metadata update the items produce.
        :param logger: Logger for malformed payloads.
        """
        self._on_update = on_update
        self.logger = logger
        self._block: dict[str, Any] | None = None
        self._now_playing: dict[str, Any] = {}

    def process_item(self, item_type: str, code: str, data: bytes) -> None:
        """
        Decode one shairport-sync metadata item and emit the updates it produces.

        Core items inside an mdst/mden block are emitted together when the block closes;
        everything else is emitted right away.

        :param item_type: Item type, "core" or "ssnc".
        :param code: Four-character item code.
        :param data: Raw item payload, b"" when the item has none.
        """
        if code == "PICT":
            if data:
                self._on_update({"artwork": data})
        elif item_type == "core":
            if update := self._decode_core(code, data):
                if self._block is None:
                    self._on_update(update)
                else:
                    self._block.update(update)
        elif item_type == "ssnc":
            self._process_ssnc(code, data)

    def _process_ssnc(self, code: str, data: bytes) -> None:
        """
        Emit the updates of one ssnc item.

        :param code: Four-character item code.
        :param data: Raw item payload.
        """
        if code == "mdst":
            self._block = {}
            self._on_update({"metadata_start": True})
        elif code == "mden":
            block, self._block = self._block, None
            if block:
                self._on_update(block)
        elif code == "copl":
            self._process_command_plist(data)
        elif update := self._decode_ssnc(code, data):
            self._on_update(update)

    def _decode_core(self, code: str, data: bytes) -> dict[str, Any] | None:
        """
        Decode one core item.

        :param code: Four-character item code.
        :param data: Raw item payload.
        """
        if key := CORE_TEXT_KEYS.get(code):
            return {key: data.decode("utf-8", errors="ignore")}
        if code == "astm":
            if len(data) < 4:
                self.logger.debug("Ignoring malformed astm payload: %s", data)
                return None
            return {"duration": int.from_bytes(data[:4], "big") // 1000}
        if code == "mper":
            if len(data) < 8:
                self.logger.debug("Ignoring malformed mper payload: %s", data)
                return None
            return {"track_id": int.from_bytes(data[:8], "big")}
        return None

    def _decode_ssnc(self, code: str, data: bytes) -> dict[str, Any] | None:
        """
        Decode one ssnc item that is emitted on its own.

        :param code: Four-character item code.
        :param data: Raw item payload.
        """
        if key := SSNC_EVENT_KEYS.get(code):
            return {key: True}
        text = data.decode("utf-8", errors="ignore")
        if key := SSNC_TEXT_KEYS.get(code):
            return {key: text}
        try:
            if code == "daid":
                return {"dacp_id": int(text, 16)}
            if code == "dapo":
                return {"dacp_port": int(text)}
            if code == "pvol":
                return self._decode_volume(text)
            if code == "prgr":
                return self._decode_progress(text)
        except ValueError as err:
            self.logger.debug("Ignoring malformed %s payload %r: %s", code, text, err)
        return None

    def _decode_volume(self, text: str) -> dict[str, Any]:
        """
        Decode a pvol payload.

        :param text: "airplay_volume,volume,lowest,highest", where the AirPlay volume runs
            from -30.0 dB to 0.0 dB and -144.0 means muted.
        """
        airplay_volume = float(text.split(",", 1)[0])
        if not math.isfinite(airplay_volume):
            msg = f"non-finite volume {airplay_volume}"
            raise ValueError(msg)
        if airplay_volume <= MUTED_AIRPLAY_VOLUME:
            volume = 0
        else:
            volume = int((airplay_volume - MIN_AIRPLAY_VOLUME) / -MIN_AIRPLAY_VOLUME * 100)
            volume = max(0, min(100, volume))
        return {"volume": volume, "airplay_volume": airplay_volume}

    def _decode_progress(self, text: str) -> dict[str, Any]:
        """
        Decode a prgr payload.

        :param text: "start/current/end" RTP timestamps of the current track.
        """
        start, current, end = (int(part) for part in text.split("/"))
        return {
            "elapsed_time": _rtp_frames_between(start, current) // PROGRESS_RTP_RATE,
            "duration": _rtp_frames_between(start, end) // PROGRESS_RTP_RATE,
        }

    def _process_command_plist(self, data: bytes) -> None:
        """
        Emit the updates of a copl command plist that AirPlay 2 senders post.

        :param data: The plist the sender posted.
        """
        try:
            plist = plistlib.loads(data)
        except (plistlib.InvalidFileException, ValueError, ExpatError) as err:
            self.logger.debug("Ignoring malformed copl plist: %s", err)
            return
        if not isinstance(plist, dict) or not isinstance(params := plist.get("params"), dict):
            return
        command = plist.get("type")
        if command == "updateMRNowPlayingInfo":
            self._process_now_playing(params)
        elif command == "updateMRPlaybackState":
            state = params.get("mrPlaybackState")
            if isinstance(state, int) and (key := PLAYBACK_STATE_KEYS.get(state)):
                self._on_update({key: True})

    def _process_now_playing(self, params: dict[str, Any]) -> None:
        """
        Merge an updateMRNowPlayingInfo command into the now-playing info and emit it.

        :param params: The command's params.
        """
        info = params.get("params")
        if params.get("type") != "npi-text" or not isinstance(info, dict):
            return
        if params.get("mergePolicy") == "replace":
            self._now_playing = {}
            self._on_update({"metadata_start": True})
        incoming = dict(info)
        artwork = incoming.pop(NOW_PLAYING_ARTWORK, None)
        self._now_playing.update(incoming)

        update: dict[str, Any] = {}
        for info_key, key in NOW_PLAYING_TEXT_KEYS.items():
            if isinstance(value := self._now_playing.get(info_key), str):
                update[key] = value
        if (duration := _seconds(self._now_playing.get(NOW_PLAYING_DURATION))) is not None:
            update["duration"] = duration
        if isinstance(track_id := self._now_playing.get(NOW_PLAYING_UNIQUE_ID), int):
            update["track_id"] = track_id
        # ElapsedTime only holds at arrival, so a stored value must not be re-reported.
        if (elapsed_time := _seconds(incoming.get(NOW_PLAYING_ELAPSED_TIME))) is not None:
            update["elapsed_time"] = elapsed_time
        if isinstance(artwork, bytes) and artwork:
            update["artwork"] = artwork
        rate = self._now_playing.get(NOW_PLAYING_PLAYBACK_RATE)
        update["resumed" if isinstance(rate, int | float) and rate else "paused"] = True
        self._on_update(update)


def _rtp_frames_between(start: int, end: int) -> int:
    """
    Return the frames from start to end, across a 32-bit RTP wraparound.

    :param start: Earlier RTP timestamp.
    :param end: Later RTP timestamp; one that lies before start counts as zero frames.
    """
    frames = (end - start) & RTP_TIMESTAMP_MASK
    return 0 if frames >= RTP_TIMESTAMP_HALF_RANGE else frames


def _seconds(value: object) -> int | None:
    """
    Return a plist duration in whole seconds, or None when it is not a finite number.

    :param value: Plist value in seconds.
    """
    if isinstance(value, int | float) and math.isfinite(value):
        return int(value)
    return None
