#!/usr/bin/env python3
"""
Minimal Chromecast HTTP-stream debug tool.

This script starts a tiny HTTP server that serves 4 synthetic audio "tracks" and instructs a
Chromecast device to play them as buffered queue items using the Default Media Receiver app.

The 4 tracks intentionally use different HTTP behaviors to help reproduce buffering/disconnect
issues in isolation:
  1) Content-Length, send as fast as possible
  2) Chunked, send as fast as possible
  3) Content-Length, throttled to (near) realtime
  4) Chunked, throttled to (near) realtime

Run it inside the same environment/venv as Music Assistant (so PyChromecast is available):

  python3 scripts/cast_stream_debug.py --cast-host 172.20.10.228

Notes:
- The Chromecast must be able to reach your machine over HTTP (same LAN).
- If you have multiple interfaces, pass --publish-ip explicitly.
"""

from __future__ import annotations

import argparse
import math
import re
import socket
import struct
import sys
import threading
import time
from array import array
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID


APP_MEDIA_RECEIVER = "CC1AD845"


def _guess_publish_ip(target_host: str) -> str:
    """Guess the local IP used to reach target_host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_host, 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def _wav_header(*, sample_rate: int, channels: int, bits_per_sample: int, frames: int) -> bytes:
    """Generate a standard PCM WAV header for a fixed number of frames."""
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    data_size = frames * block_align
    riff_size = 36 + data_size
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", riff_size),
            b"WAVE",
            b"fmt ",
            struct.pack("<I", 16),  # fmt chunk size
            struct.pack("<H", 1),  # PCM
            struct.pack("<H", channels),
            struct.pack("<I", sample_rate),
            struct.pack("<I", byte_rate),
            struct.pack("<H", block_align),
            struct.pack("<H", bits_per_sample),
            b"data",
            struct.pack("<I", data_size),
        ]
    )


def _pcm_s16le_1s(*, freq_hz: float, sample_rate: int, channels: int, amplitude: float) -> bytes:
    """Generate 1 second of signed 16-bit PCM (little endian)."""
    if channels not in (1, 2):
        raise ValueError("Only mono/stereo supported for this debug script.")
    if not (0.0 < amplitude <= 1.0):
        raise ValueError("Amplitude must be in (0.0, 1.0].")
    max_i16 = 32767
    amp = int(max_i16 * amplitude)
    two_pi_f = 2.0 * math.pi * freq_hz
    samples = array("h")
    for i in range(sample_rate):
        val = int(amp * math.sin(two_pi_f * (i / sample_rate)))
        if channels == 2:
            samples.append(val)
            samples.append(val)
        else:
            samples.append(val)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


@dataclass(frozen=True)
class TrackSpec:
    path: str
    title: str
    freq_hz: float
    duration_s: int
    chunked: bool
    throttle: bool
    sample_rate: int = 44100
    channels: int = 2
    bits_per_sample: int = 16
    amplitude: float = 0.2
    # For throttled streams: allow the client to buffer some seconds before enforcing realtime.
    initial_burst_s: float = 6.0

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * (self.bits_per_sample // 8)


def _build_track_assets(track: TrackSpec) -> tuple[bytes, bytes, int]:
    """Return (wav_header, pcm_1s_block, total_size_bytes)."""
    pcm_1s = _pcm_s16le_1s(
        freq_hz=track.freq_hz,
        sample_rate=track.sample_rate,
        channels=track.channels,
        amplitude=track.amplitude,
    )
    if len(pcm_1s) != track.bytes_per_second:
        raise RuntimeError("Internal error: pcm_1s size mismatch")
    frames = track.sample_rate * track.duration_s
    header = _wav_header(
        sample_rate=track.sample_rate,
        channels=track.channels,
        bits_per_sample=track.bits_per_sample,
        frames=frames,
    )
    total_size = len(header) + (track.duration_s * len(pcm_1s))
    return header, pcm_1s, total_size


class _CastDebugListener:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def new_connection_status(self, status: Any) -> None:  # pychromecast ConnectionStatus
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] CAST connection_status: {getattr(status, 'status', status)}")

    def new_cast_status(self, status: Any) -> None:  # pychromecast CastStatus
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            app_id = getattr(status, "app_id", None)
            vol = getattr(status, "volume_level", None)
            print(f"[{ts}] CAST cast_status: app_id={app_id} volume={vol}")

    def new_media_status(self, status: Any) -> None:  # pychromecast MediaStatus
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            state = getattr(status, "player_state", None)
            content_id = getattr(status, "content_id", None)
            cur = getattr(status, "current_time", None)
            dur = getattr(status, "duration", None)
            print(f"[{ts}] CAST media_status: state={state} t={cur}/{dur} content_id={content_id}")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # quiet default http.server logging; we print custom lines instead.
        return

    def do_HEAD(self) -> None:  # noqa: N802 (stdlib naming)
        self._handle(send_body=False)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self._handle(send_body=True)

    def _handle(self, *, send_body: bool) -> None:
        tracks: dict[str, TrackSpec] = self.server.tracks  # type: ignore[attr-defined]
        if self.path.split("?", 1)[0] not in tracks:
            self.send_error(404, "Not Found")
            return

        track = tracks[self.path.split("?", 1)[0]]
        header, pcm_1s, total_size = self.server.track_assets[track.path]  # type: ignore[attr-defined]
        advertise_range: bool = bool(getattr(self.server, "advertise_range", True))  # type: ignore[attr-defined]
        honor_range: bool = bool(getattr(self.server, "honor_range", True))  # type: ignore[attr-defined]

        ts = time.strftime("%H:%M:%S")
        range_header = self.headers.get("Range")
        user_agent = self.headers.get("User-Agent")
        print(
            f"[{ts}] HTTP {self.command} {self.path} from {self.client_address[0]} "
            f"range={range_header!r} ua={user_agent!r}"
        )
        if range_header and not advertise_range:
            print(
                f"[{ts}] !!! CLIENT SENT Range header but server is NOT advertising range support "
                f"(Accept-Ranges: none)"
            )

        # Parse optional Range header.
        start = 0
        end = total_size - 1
        is_partial = False
        if range_header and honor_range:
            match = re.match(r"bytes=(\d+)-(\d+)?$", range_header.strip())
            if match:
                start = int(match.group(1))
                if match.group(2) is not None:
                    end = int(match.group(2))
                end = min(end, total_size - 1)
                if start <= end:
                    is_partial = True
        elif range_header and not honor_range:
            print(f"[{ts}] !!! Server is configured to IGNORE Range requests (honor_range=false)")

        if start > end:
            self.send_response(416, "Requested Range Not Satisfiable")
            self.send_header("Content-Range", f"bytes */{total_size}")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        content_len = (end - start) + 1
        if is_partial:
            self.send_response(206, "Partial Content")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        else:
            self.send_response(200, "OK")

        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes" if advertise_range else "none")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        if track.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(content_len))
        self.end_headers()

        if not send_body:
            return

        def write(data: bytes) -> None:
            if not data:
                return
            if track.chunked:
                self.wfile.write(f"{len(data):X}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
            else:
                self.wfile.write(data)
            self.wfile.flush()

        data_start_time = time.monotonic()
        header_bytes_sent = 0
        pcm_bytes_sent = 0

        header_len = len(header)
        data_size = total_size - header_len
        block_len = len(pcm_1s)  # == bytes_per_second

        def maybe_throttle(pcm_chunk_len: int) -> None:
            nonlocal pcm_bytes_sent
            if not track.throttle:
                return
            if pcm_chunk_len <= 0:
                return
            seconds_sent = pcm_bytes_sent / block_len
            target_elapsed = max(0.0, seconds_sent - track.initial_burst_s)
            elapsed = time.monotonic() - data_start_time
            if target_elapsed > elapsed:
                time.sleep(target_elapsed - elapsed)

        try:
            # Serve header portion.
            pos = start
            if pos < header_len:
                header_end = min(header_len, end + 1)
                chunk = header[pos:header_end]
                write(chunk)
                header_bytes_sent += len(chunk)
                pos = header_end

            # Serve PCM data portion (repeating 1-second block).
            if pos <= end:
                data_pos = max(0, pos - header_len)
                data_end = min(data_size, (end + 1) - header_len)  # exclusive
                remaining = data_end - data_pos
                block_index = data_pos // block_len
                in_block_off = data_pos % block_len

                # Fast-forward logical index without allocating anything.
                _ = block_index

                while remaining > 0:
                    chunk = pcm_1s[in_block_off : min(block_len, in_block_off + remaining)]
                    write(chunk)
                    pcm_bytes_sent += len(chunk)
                    maybe_throttle(len(chunk))
                    remaining -= len(chunk)
                    in_block_off = 0

            if track.chunked:
                # terminator
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            ts2 = time.strftime("%H:%M:%S")
            seconds_sent = pcm_bytes_sent / block_len if block_len else 0.0
            print(
                f"[{ts2}] HTTP client disconnected while streaming {track.path} "
                f"(sent_header_bytes={header_bytes_sent} sent_pcm_seconds≈{seconds_sent:.1f})"
            )


def _start_http_server(
    *,
    bind: str,
    port: int,
    tracks: list[TrackSpec],
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer((bind, port), _Handler)
    server.tracks = {t.path: t for t in tracks}  # type: ignore[attr-defined]
    server.track_assets = {t.path: _build_track_assets(t) for t in tracks}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="http-server")
    thread.start()
    return server, thread


def _launch_app(cast: Any, app_id: str, timeout: float = 10.0) -> None:
    ev = threading.Event()

    def launched_cb(success: bool, response: dict[str, Any] | None) -> None:  # noqa: ARG001
        ev.set()

    if getattr(cast, "app_id", None) == app_id:
        return
    cast.socket_client.receiver_controller.launch_app(  # type: ignore[attr-defined]
        app_id, force_launch=True, callback_function=launched_cb
    )
    if not ev.wait(timeout):
        raise TimeoutError("Timed out waiting for receiver app launch")


def _cc_media_item(*, url: str, title: str, duration_s: int) -> dict[str, Any]:
    # Keep it close to what Music Assistant sends.
    return {
        "contentId": url,
        "customData": {"uri": url},
        "contentType": "audio/wav",
        "streamType": "BUFFERED",
        "metadata": {
            "metadataType": 3,
            "title": title,
        },
        "duration": duration_s,
    }


def _wait_for_media_session_id(cast: Any, timeout: float = 15.0) -> int:
    start = time.monotonic()
    while True:
        cast.media_controller.update_status()
        msid = getattr(cast.media_controller.status, "media_session_id", None)
        if msid is not None:
            return int(msid)
        if (time.monotonic() - start) > timeout:
            raise TimeoutError("Timed out waiting for media_session_id")
        time.sleep(0.25)


def _connect_cast(pychromecast: Any, *, host: str, port: int) -> Any:
    """Create a Chromecast object from a host/port across PyChromecast versions."""
    # Older API: construct from host tuple.
    if hasattr(pychromecast, "get_chromecast_from_host"):
        try:
            # Some versions accept (host, port).
            return pychromecast.get_chromecast_from_host((host, port))  # type: ignore[attr-defined]
        except Exception:
            # Newer signature expects (ip, port, uuid, model_name, friendly_name).
            dev_uuid: UUID = UUID(int=0)
            model_name: str | None = None
            friendly_name: str | None = None
            try:
                import pychromecast.dial as dial  # type: ignore

                device_status = dial.get_device_info(host, services=None, timeout=5)  # type: ignore[arg-type]
                if device_status:
                    dev_uuid = getattr(device_status, "uuid", None) or dev_uuid
                    model_name = getattr(device_status, "model_name", None)
                    friendly_name = getattr(device_status, "friendly_name", None)
            except Exception:
                pass
            return pychromecast.get_chromecast_from_host(  # type: ignore[attr-defined]
                (host, port, dev_uuid, model_name, friendly_name)
            )

    # Very old fallback: Chromecast(host, port=...).
    if hasattr(pychromecast, "Chromecast"):
        return pychromecast.Chromecast(host, port=port)  # type: ignore[call-arg]

    raise RuntimeError("Unsupported PyChromecast version: cannot create Chromecast object.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cast-host", required=True, help="Chromecast IP/hostname (e.g. 172.20.10.228)")
    parser.add_argument("--cast-port", type=int, default=8009, help="Chromecast port (default: 8009)")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=0, help="HTTP port (0 chooses a random free port)")
    parser.add_argument("--publish-ip", default="", help="IP to publish to Chromecast (default: auto)")
    parser.add_argument("--duration", type=int, default=600, help="Duration per track in seconds (default: 600)")
    parser.add_argument(
        "--start-track",
        type=int,
        choices=[1, 2, 3, 4],
        default=1,
        help="Which track to LOAD first (default: 1)",
    )
    parser.add_argument(
        "--no-queue",
        action="store_true",
        help="Only LOAD the start track (do not QUEUE_INSERT the remaining tracks)",
    )
    parser.add_argument(
        "--preload-time",
        type=int,
        default=0,
        help="Chromecast preloadTime for QUEUE_INSERT items (default: 0)",
    )
    parser.add_argument(
        "--advertise-range",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Advertise byte range support via Accept-Ranges header (default: true).",
    )
    parser.add_argument(
        "--honor-range",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Honor incoming Range requests (default: true).",
    )
    args = parser.parse_args()

    publish_ip = args.publish_ip or _guess_publish_ip(args.cast_host)

    tracks = [
        TrackSpec(
            path="/track1.wav",
            title="Track 1 (Content-Length, fast)",
            freq_hz=220.0,
            duration_s=args.duration,
            chunked=False,
            throttle=False,
        ),
        TrackSpec(
            path="/track2.wav",
            title="Track 2 (Chunked, fast)",
            freq_hz=330.0,
            duration_s=args.duration,
            chunked=True,
            throttle=False,
        ),
        TrackSpec(
            path="/track3.wav",
            title="Track 3 (Content-Length, throttled)",
            freq_hz=440.0,
            duration_s=args.duration,
            chunked=False,
            throttle=True,
        ),
        TrackSpec(
            path="/track4.wav",
            title="Track 4 (Chunked, throttled)",
            freq_hz=550.0,
            duration_s=args.duration,
            chunked=True,
            throttle=True,
        ),
    ]

    server, _thread = _start_http_server(bind=args.bind, port=args.port, tracks=tracks)
    server.advertise_range = bool(args.advertise_range)  # type: ignore[attr-defined]
    server.honor_range = bool(args.honor_range)  # type: ignore[attr-defined]
    srv_host, srv_port = server.server_address[:2]
    base_url = f"http://{publish_ip}:{srv_port}"
    print(f"HTTP server listening on {srv_host}:{srv_port} (published as {base_url})")
    print(
        "Range settings:"
        f" advertise_range={server.advertise_range} honor_range={server.honor_range}"  # type: ignore[attr-defined]
    )
    print("Track URLs:")
    for t in tracks:
        print(f"  - {base_url}{t.path}  [{t.title}]")

    try:
        import pychromecast  # type: ignore
    except ImportError:
        print("PyChromecast is not installed. Run this script inside the MA venv or install it:")
        print("  python3 -m pip install PyChromecast")
        return 2

    cast: Any = _connect_cast(pychromecast, host=args.cast_host, port=args.cast_port)

    # Start/attach listeners.
    listener = _CastDebugListener()
    try:
        cast.register_status_listener(listener)
        cast.socket_client.media_controller.register_status_listener(listener)
        cast.register_connection_listener(listener)
    except Exception:
        # Not critical; depends on pychromecast internals.
        pass

    try:
        cast.start()
    except Exception:
        pass

    try:
        cast.wait()
    except TypeError:
        cast.wait(10)

    print(f"Connected to cast: {getattr(cast, 'name', None) or args.cast_host}")

    # Launch Default Media Receiver app.
    _launch_app(cast, APP_MEDIA_RECEIVER)

    # Load first item.
    media_controller = cast.media_controller
    start_track = tracks[args.start_track - 1]
    load_msg = {
        "type": "LOAD",
        "autoplay": True,
        "currentTime": 0,
        "media": _cc_media_item(
            url=f"{base_url}{start_track.path}",
            title=start_track.title,
            duration_s=start_track.duration_s,
        ),
    }
    media_controller.send_message(data=load_msg, inc_session_id=True)

    if args.no_queue:
        print(f"Loaded start track only (QUEUE_INSERT disabled): {start_track.title}")
    else:
        # Insert the remaining items into the Chromecast queue.
        media_session_id = _wait_for_media_session_id(cast)
        queue_tracks = [t for t in tracks if t != start_track]
        insert_msg = {
            "type": "QUEUE_INSERT",
            "mediaSessionId": media_session_id,
            "insertBefore": None,
            "items": [
                {
                    "autoplay": True,
                    "startTime": 0,
                    "preloadTime": args.preload_time,
                    "media": _cc_media_item(
                        url=f"{base_url}{t.path}",
                        title=t.title,
                        duration_s=t.duration_s,
                    ),
                }
                for t in queue_tracks
            ],
        }
        media_controller.send_message(data=insert_msg, inc_session_id=True)
        print(f"Queue loaded (preloadTime={args.preload_time}).")

    print("Leave this running and watch for HTTP disconnects / cast socket drops.")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
