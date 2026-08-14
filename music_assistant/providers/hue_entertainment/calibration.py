"""
Latency calibration helper for the Hue Lights Sync provider.

Tuning ``Light latency (ms)`` by ear is hard. This serves a small ~10 minute
metronome MP3 of strong, broadband percussive ticks (a "ball drop" beat) that the
strobe/structure detector reacts to, and plays it to a chosen speaker through the
normal Music Assistant pipeline (so the analysis runs and the lights react). The
user then watches the physical lights and nudges the (live) latency slider until
the flash lines up with the heard tick.

The track is generated once with ffmpeg and cached, so it does not have to be
re-triggered like a one-shot announcement and can simply loop.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
import struct
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.enums import PlayerType

if TYPE_CHECKING:
    from .provider import HueEntertainmentProvider

# Metronome: one tick every _PERIOD_S, looped to ~_TOTAL_S. ~100 BPM reads clearly
# and leaves a gap between ticks so each flash is distinct.
_PERIOD_S = 0.6
_TOTAL_S = 600
_RATE = 44100
# Cached under Music Assistant's cache dir, which is writable on every install
# type (HA add-on, Docker, bare metal) - a hardcoded /data only exists on the HA
# add-on and would make calibration unusable elsewhere.
_CACHE_NAME = "hue_calib_metronome.mp3"
_SEG_NAME = "hue_calib_segment.wav"


def _make_segment_wav() -> bytes:
    """Render one metronome beat (a broadband percussive tick + silence) as a WAV."""
    total = int(_RATE * _PERIOD_S)
    click = int(_RATE * 0.09)
    frames = bytearray()
    for i in range(total):
        if i < click:
            t = i / _RATE
            env = math.exp(-t / 0.025)
            # Low thump + mid tone + noise burst => sharp broadband transient that
            # the onset/energy detection picks up reliably.
            mix = (
                0.6 * math.sin(2.0 * math.pi * 90.0 * t)
                + 0.3 * math.sin(2.0 * math.pi * 1500.0 * t)
                + 0.4 * (random.random() * 2.0 - 1.0)
            ) * env
            sample = int(max(-1.0, min(1.0, mix)) * 0.85 * 32767)
        else:
            sample = 0
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(_RATE)
        writer.writeframes(bytes(frames))
    return buf.getvalue()


class Calibrator:
    """Serves the metronome track and plays/stops it on a chosen player."""

    def __init__(self, provider: HueEntertainmentProvider) -> None:
        """Initialize the calibrator for the given provider."""
        self.provider = provider
        self._player_id: str = ""
        self._gen_lock = asyncio.Lock()
        cache_dir = Path(provider.mass.cache_path)
        self._cache_mp3 = cache_dir / _CACHE_NAME
        self._seg_wav = cache_dir / _SEG_NAME

    @property
    def track_url(self) -> str:
        """Public URL of the calibration metronome track."""
        return f"{self.provider.mass.webserver.base_url}/hue_preview/calib.mp3"

    async def serve_track(self, request: web.Request) -> web.StreamResponse:
        """Serve the (cached, generated-on-demand) metronome MP3."""
        path = await self._ensure_track()
        if path is None:
            return web.Response(status=503, text="calibration track unavailable (ffmpeg failed)")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def serve_control(self, request: web.Request) -> web.Response:
        """Handle calibration control commands from the preview page (POST JSON)."""
        try:
            data = await request.json()
        except ValueError, json.JSONDecodeError:
            data = {}
        action = str(data.get("action", ""))
        if action == "players":
            return web.json_response(
                {"players": self._players(), "period_ms": int(_PERIOD_S * 1000)}
            )
        if action in ("play", "stop") and not self.provider.authorize_preview(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=403)
        if action == "play":
            self._set_player(data.get("player"))
            ok = await self._play()
            return web.json_response({"ok": ok})
        if action == "stop":
            await self._stop()
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "unknown action"}, status=400)

    def close(self) -> None:
        """No background tasks to stop (kept for symmetry with provider unload)."""

    # -- internals --

    def _players(self) -> list[dict[str, Any]]:
        """Audio players that can play the track (excludes the Hue light players)."""
        result: list[dict[str, Any]] = []
        try:
            for player in self.provider.mass.players.all_players():
                if player.state.type == PlayerType.LIGHT or not player.state.available:
                    continue
                result.append({"id": player.player_id, "name": player.display_name})
        except Exception as err:
            self.provider.logger.debug("Could not list players for calibration: %s", err)
        return result

    def _set_player(self, player_id: object) -> None:
        if player_id:
            self._player_id = str(player_id)

    async def _ensure_track(self) -> Path | None:
        """Generate the metronome MP3 once (cached); return its path or None on failure."""
        if self._cache_mp3.exists() and self._cache_mp3.stat().st_size > 0:
            return self._cache_mp3
        async with self._gen_lock:
            if self._cache_mp3.exists() and self._cache_mp3.stat().st_size > 0:
                return self._cache_mp3
            try:
                self._seg_wav.write_bytes(_make_segment_wav())
                loops = max(1, round(_TOTAL_S / _PERIOD_S)) - 1
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-stream_loop",
                    str(loops),
                    "-i",
                    str(self._seg_wav),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "48k",
                    "-ac",
                    "1",
                    str(self._cache_mp3),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    self.provider.logger.warning(
                        "Calibration track ffmpeg failed: %s",
                        stderr.decode("utf-8", "replace")[:300],
                    )
                    return None
            except Exception as err:
                self.provider.logger.warning("Could not generate calibration track: %s", err)
                return None
        return self._cache_mp3 if self._cache_mp3.exists() else None

    async def _play(self) -> bool:
        """Play the metronome track on the selected player's queue."""
        if not self._player_id:
            return False
        await self._ensure_track()
        try:
            await self.provider.mass.player_queues.play_media(self._player_id, self.track_url)
        except Exception as err:
            self.provider.logger.warning("Could not play calibration track: %s", err)
            return False
        return True

    async def _stop(self) -> None:
        if not self._player_id:
            return
        try:
            await self.provider.mass.player_queues.stop(self._player_id)
        except Exception as err:
            self.provider.logger.debug("Could not stop calibration track: %s", err)
